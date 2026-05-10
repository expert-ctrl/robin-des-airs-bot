from flask import Flask, request, jsonify
import requests
import os
import json
import base64
import re
import hashlib
import unicodedata
from datetime import datetime, timedelta

app = Flask(__name__)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
WATI_API_TOKEN = os.environ.get("WATI_API_TOKEN", "")
WATI_BASE_URL = os.environ.get("WATI_BASE_URL", "")
AIRTABLE_API_KEY = os.environ.get("AIRTABLE_API_KEY", "")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID", "appv72lKbQtjt7EIP")
AIRTABLE_TABLE_NAME = os.environ.get("AIRTABLE_TABLE_NAME", "Dossiers Passagers")

RDA_DOMAIN = "https://robindesairs.eu"
# Renvois utilisateur : site principal par defaut (surcharge possible)
RDA_SITE_URL = os.environ.get("RDA_SITE_URL", RDA_DOMAIN).rstrip("/") or RDA_DOMAIN
MANDAT_URL = f"{RDA_DOMAIN}/mandat-representation"
DEPOT_URL = f"{RDA_DOMAIN}/depot-express"
SUIVI_URL = f"{RDA_DOMAIN}/suivi-dossier"
BLOG_URL = f"{RDA_DOMAIN}/blog"  # optionnel ; les messages utilisateur preferent RDA_SITE_URL
# Formulaire dossier en ligne (Airtable/Notion/site) — option menu 2
RDA_ONLINE_DOSSIER_URL = os.environ.get("RDA_ONLINE_DOSSIER_URL", DEPOT_URL)

# Memoire conversations
conversations = {}
recent_event_ids = {}
MEMORY_HOURS = 24

# Derniere erreur API Airtable (visible sur GET /test pour debug)
last_airtable_error = None

# Noms des champs Airtable = EXACTEMENT comme les titres de colonnes dans la base.
# Defauts = intitules type liste client (anglais). Surcharge sans toucher au code :
#   AIRTABLE_FIELD_REF="Référence dossier"  etc.
# Cle logique absente ou defaut None => champ non envoye (evite UNKNOWN_FIELD_NAME).
AIRTABLE_FIELD_DEFAULTS = {
    "nom": "name of the passenger",
    "ref": "reference file",
    "date_dossier": "file date",
    "montant": "customer amount",
    "commission_rda": "RDA commission",
    "commission_agence": "agency commission",
    "agence_partenaire": "partner agency",
    "dossier_client": "customer file",
    "remarques": "remark",
    "date_naissance": None,
    "statut_mineur": "minor status",
    "nom_non_rep": None,
    "whatsapp": "phone number",
    "compagnie": "airline number",
    "vol": "flight number",
    "date_vol": "Flight date",
    "incident": None,
    "montant_brut": None,
    "copie_passeport": None,
    "copie_cni": None,
    "mandat_url": None,
    "statut_dossier": "file status",
    "iban": None,
}


def airtable_field(key):
    """Nom de colonne Airtable pour une cle logique (env > defaut). Defaut None => desactive."""
    return os.environ.get(f"AIRTABLE_FIELD_{key.upper()}", AIRTABLE_FIELD_DEFAULTS.get(key))


def airtable_put(fields, key, value, skip_if_empty=True):
    """Ajoute fields[nom_colonne]=value si la colonne est configuree et la valeur utilisable."""
    col = airtable_field(key)
    if not col:
        return
    if value is None or (skip_if_empty and value == ""):
        return
    fields[col] = value


def airtable_escape_formula_str(s):
    """Echappe les quotes simples pour filterByFormula (double '')."""
    return str(s).replace("'", "''")


def normalize_dossier_phone(raw):
    """Numero pour dossier / Airtable (style +33612345678)."""
    if not raw:
        return None
    t = str(raw).strip().replace(" ", "")
    if t.startswith("+"):
        d = re.sub(r"\D", "", t[1:])
        return ("+" + d) if d else None
    d = re.sub(r"\D", "", t)
    if len(d) < 8:
        return None
    if d.startswith("00"):
        d = d[2:]
    if d.startswith("0") and len(d) == 10:
        d = "33" + d[1:]
    return "+" + d


def parse_phone_input_line(text):
    """Parse une saisie utilisateur (ligne unique) vers +..."""
    return normalize_dossier_phone(text)

EU261_BANDS = {
    "band_250": {"amount_eur": 250, "label": "≤ 1500 km"},
    "band_400": {"amount_eur": 400, "label": "1500–3500 km"},
    "band_600": {"amount_eur": 600, "label": "> 3500 km"},
    "band_unknown": {"amount_eur": None, "label": "Distance inconnue"},
}


def band_from_distance_km(km):
    """Tranches fixes UE pour montants standard (retard/annulation/refus), hors nuances juridiques."""
    try:
        km = float(km)
    except (TypeError, ValueError):
        return "band_unknown"
    if km <= 1500:
        return "band_250"
    if km <= 3500:
        return "band_400"
    return "band_600"


# Etapes avec choix numerique strict — correction IA si reponse ambigue
MENU_ALLOWED_BY_STEP = {
    "confirm_phone": frozenset(str(i) for i in range(1, 3)),
    "passengers": frozenset(str(i) for i in range(1, 7)),
    "incident_type": frozenset(str(i) for i in range(1, 4)),
    "cancel_notice_period": frozenset(str(i) for i in range(1, 3)),
    "flight_type": frozenset(str(i) for i in range(1, 3)),
    "flight_date": frozenset(str(i) for i in range(1, 7)),
    "flight_month": frozenset(str(i) for i in range(1, 13)),
    "flight_day_input": frozenset(str(i) for i in range(1, 32)),
    "minor_check": frozenset(str(i) for i in range(1, 3)),
    "post_summary_edit": frozenset(str(i) for i in range(1, 7)),
}


STEPS = [
    "entry_intent",
    "confirm_phone",
    "phone_other_input",
    "passengers",
    "incident_type",
    "cancel_notice_period",
    "flight_type",
    "connection_escale",
    "airline",
    "airline_other_input",
    "flight_number",
    "flight_date",
    "flight_month",
    "flight_day_input",
    "departure_airport",
    "arrival_airport",
    "passenger_name_collect",
    "minor_check",
    "minor_who",
    "mailing_address",
    "summary",
]

# Etapes ou process_reply gere la reponse (dossier + recap termine + menu edition)
PROCESS_REPLY_STEPS = frozenset(STEPS) | frozenset({"completed", "post_summary_edit"})

AIRLINES_MAP = {
    "1": "Air France",
    "2": "KLM",
    "3": "Brussels Airlines",
    "4": "Lufthansa",
    "5": "TAP Portugal",
    "6": "Corsair",
    "7": "Air Senegal",
    "8": "Royal Air Maroc",
}

# Drapeaux pour liste compagnies (affichage WhatsApp)
AIRLINE_FLAGS = {
    "1": "🇫🇷",
    "2": "🇳🇱",
    "3": "🇧🇪",
    "4": "🇩🇪",
    "5": "🇵🇹",
    "6": "🇫🇷",
    "7": "🇸🇳",
    "8": "🇲🇦",
}

INCIDENT_LABELS = {"delay": "Retard +3h", "cancel": "Annulation", "denied": "Refus embarquement"}

SYSTEM_PROMPT = f"""Tu es l'agent IA de ROBIN DES AIRS. Tu reponds dans la LANGUE DU CLIENT.

REGLES FORMAT :
- 3+ emojis par message
- Bullet points avec emojis
- Max 6 lignes
- Toujours finir par lien {RDA_DOMAIN}

LIENS AUTORISES (uniquement) :
- Mandat : {MANDAT_URL}
- Depot : {DEPOT_URL}
- Suivi : {SUIVI_URL}

REGLEMENT EU261 :
- Retard +3h, Annulation, Refus = indemnisation
- 250/400/600 EUR selon distance
- Commission 25% si succes uniquement
- Net passager : 75%
- Retroactivite 5 ans

ESCALADE Climbie +33 7 56 86 36 30 si : 6+ pax / Deces / Juridique complexe
"""

SYSTEM_PROMPT_FAQ = f"""Tu es l'expert EU261 de ROBIN DES AIRS. Tu reponds dans la LANGUE DU CLIENT.

ROLE : reponses courtes d'information (droits, delais, montants indicatifs, annulation <14j vs >14j, reroutage).
Tu NE collectes PAS les donnees d'un dossier ici. Si la personne veut deposer un dossier, dis-lui d'ecrire : dossier ou 1.

REGLES :
- 3+ emojis, puces courtes, max 8 lignes
- Rappelle que ce n'est pas un conseil juridique personnalise
- Finir par un lien utile parmi : {MANDAT_URL} | {DEPOT_URL} | {SUIVI_URL} | site {RDA_SITE_URL}

EU261 (simplifie) :
- Vol UE ou compagnie UE selon cas ; trajets hors UE varies
- Retard : indemnisation si retard a l'arrivee >= 3h (sauf causes extraordinaires)
- Annulation : si informe du vol annule PLUS de 14 j avant le depart prevu, en regle generale PAS de compensation fixe art.7 (reste aide/remboursement/reroutage selon cas). Si 14 j ou MOINS : examiner reroutage et delai pour indemnisation possible
- Refus d'embarquement : droits forts souvent
- Montants indicatifs : 250 / 400 / 600 EUR selon distance du trajet
- Retroactivite courante jusqu'a 5 ans selon pays

Tel escalade : +33 7 56 86 36 30 (Climbie)
"""


# ===== REFERENCE DOSSIER =====


def generate_ref_dossier(phone):
    """Genere une reference unique : RDA-YYYYMMDD-XXXX"""
    today = datetime.now().strftime("%Y%m%d")
    suffix = hashlib.md5(f"{phone}{today}".encode()).hexdigest()[:4].upper()
    return f"RDA-{today}-{suffix}"


# ===== AIRTABLE =====


def airtable_headers():
    return {"Authorization": f"Bearer {AIRTABLE_API_KEY}", "Content-Type": "application/json"}


def airtable_url():
    table = requests.utils.quote(AIRTABLE_TABLE_NAME)
    return f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{table}"


def airtable_find_records_by_ref(ref):
    """Cherche les records existants par reference dossier (egalite stricte sur le champ Ref)."""
    global last_airtable_error
    if not AIRTABLE_API_KEY or not ref:
        return []
    try:
        ref_f = airtable_field("ref")
        r_esc = airtable_escape_formula_str(ref)
        formula = f"{{{ref_f}}}='{r_esc}'"
        url = f"{airtable_url()}?filterByFormula={requests.utils.quote(formula)}"
        r = requests.get(url, headers=airtable_headers(), timeout=10)
        if r.status_code == 200:
            return r.json().get("records", [])
        last_airtable_error = f"airtable_find GET {r.status_code}: {r.text[:2000]}"
        print(last_airtable_error)
    except Exception as e:
        print(f"Airtable find error: {e}")
    return []


def airtable_save_progressive(phone, conv):
    """
    Sauvegarde progressive : cree ou met a jour les records dans Airtable.
    Cree 1 ligne par passager des qu'on connait pax.
    Met a jour les champs au fur et a mesure de leur saisie.
    Utilise conv['data']['dossier_phone'] si defini (numero dossier), sinon le waId session.
    """
    global last_airtable_error
    if not AIRTABLE_API_KEY:
        return

    try:
        d = conv.get("data", {})
        ref = conv.get("ref_dossier")
        if not ref:
            ref = generate_ref_dossier(phone)
            conv["ref_dossier"] = ref

        pax = d.get("passengers") or 1
        names = d.get("passenger_names", [])

        contact_raw = d.get("dossier_phone") or phone
        contact = normalize_dossier_phone(contact_raw) or str(contact_raw)

        dep = d.get("departure_airport")
        arr = d.get("arrival_airport")
        km = d.get("route_distance_km")
        route_suffix = ""
        if dep or arr or d.get("connection_escale"):
            bits = []
            if dep:
                bits.append(f"Depart {dep}")
            if arr:
                bits.append(f"Arr {arr}")
            if km:
                bits.append(f"~{int(km)} km")
            if d.get("connection_escale"):
                bits.append(f"Escale {str(d['connection_escale'])[:180]}")
            route_suffix = " | " + " · ".join(bits)

        addr = (d.get("mailing_address") or "").strip()
        addr_suffix = ""
        if addr:
            addr_short = addr.replace("\n", " ")[:400]
            addr_suffix = f" | Adr: {addr_short}"

        band_id = d.get("distance_band", "band_unknown")
        per_pax = EU261_BANDS.get(band_id, EU261_BANDS["band_unknown"]).get("amount_eur")
        total_brut = (per_pax * pax) if per_pax else 0
        total_net = int(total_brut * 0.75) if total_brut else 0
        commission_rda_val = int(round(total_brut * 0.25)) if total_brut else 0
        try:
            commission_agence_val = float(os.environ.get("AIRTABLE_DEFAULT_AGENCY_COMMISSION", "0"))
        except ValueError:
            commission_agence_val = 0.0
        partner_agency = os.environ.get("AIRTABLE_DEFAULT_PARTNER_AGENCY", "").strip()
        file_status = os.environ.get("AIRTABLE_DEFAULT_FILE_STATUS", "WhatsApp — intake").strip()

        idxs = sorted(d.get("minor_passenger_indices") or [])
        names = d.get("passenger_names") or []
        minor_labels = [names[i - 1] for i in idxs if isinstance(i, int) and 1 <= i <= len(names)]
        if minor_labels:
            statut_mineur_txt = f"Mineur(s): {', '.join(minor_labels)}"
        elif d.get("has_minors") is True or (d.get("minors_count") or 0) > 0:
            mc = d.get("minors_count") or 0
            statut_mineur_txt = f"{mc} mineur(s)" if mc else "Mineur(s)"
        elif d.get("has_minors") is False:
            statut_mineur_txt = "Aucun mineur / tous majeurs"
        else:
            statut_mineur_txt = ""

        common = {}
        airtable_put(common, "ref", ref, skip_if_empty=False)
        airtable_put(common, "date_dossier", datetime.now().strftime("%Y-%m-%d"), skip_if_empty=False)
        airtable_put(common, "whatsapp", contact, skip_if_empty=False)
        airtable_put(common, "vol", d.get("flight_number"))
        airtable_put(common, "compagnie", d.get("airline"))
        airtable_put(common, "date_vol", d.get("flight_date"))
        if d.get("incident_type") and airtable_field("incident"):
            airtable_put(common, "incident", INCIDENT_LABELS.get(d["incident_type"], d["incident_type"]))
        airtable_put(common, "dossier_client", f"WhatsApp | {ref}", skip_if_empty=False)
        airtable_put(common, "agence_partenaire", partner_agency)
        airtable_put(common, "statut_dossier", file_status, skip_if_empty=False)
        airtable_put(common, "commission_agence", commission_agence_val, skip_if_empty=False)
        if statut_mineur_txt:
            airtable_put(common, "statut_mineur", statut_mineur_txt, skip_if_empty=False)

        existing = airtable_find_records_by_ref(ref)

        if not existing:
            records_to_create = []
            for i in range(pax):
                fields = dict(common)
                nom_val = names[i] if i < len(names) else f"Passager {i+1}"
                airtable_put(fields, "nom", nom_val, skip_if_empty=False)
                rem = f"Ref: {ref} | Passager {i+1}/{pax}{route_suffix}{addr_suffix}"
                airtable_put(fields, "remarques", rem, skip_if_empty=False)
                if i == 0 and total_net:
                    airtable_put(fields, "montant", float(total_net), skip_if_empty=False)
                    if commission_rda_val:
                        airtable_put(fields, "commission_rda", commission_rda_val, skip_if_empty=False)
                    if total_brut and airtable_field("montant_brut"):
                        airtable_put(fields, "montant_brut", float(total_brut), skip_if_empty=False)
                else:
                    airtable_put(fields, "montant", 0.0, skip_if_empty=False)
                    airtable_put(fields, "commission_rda", 0, skip_if_empty=False)
                records_to_create.append({"fields": fields})

            payload = {"records": records_to_create, "typecast": True}
            r = requests.post(airtable_url(), headers=airtable_headers(), json=payload, timeout=15)
            if r.status_code not in (200, 201):
                last_airtable_error = f"CREATE {r.status_code}: {r.text[:2500]}"
                print(f"Airtable CREATE {pax} records: {r.status_code} - {r.text[:500]}")
            else:
                last_airtable_error = None
                print(f"Airtable CREATE {pax} records: {r.status_code} OK")

        else:
            updates = []
            rem_col = airtable_field("remarques")
            for i, rec in enumerate(existing[:pax]):
                fields = dict(common)
                nom_val = names[i] if i < len(names) else f"Passager {i+1}"
                airtable_put(fields, "nom", nom_val, skip_if_empty=False)
                if i == 0 and total_net:
                    airtable_put(fields, "montant", float(total_net), skip_if_empty=False)
                    if commission_rda_val:
                        airtable_put(fields, "commission_rda", commission_rda_val, skip_if_empty=False)
                    if total_brut and airtable_field("montant_brut"):
                        airtable_put(fields, "montant_brut", float(total_brut), skip_if_empty=False)
                else:
                    airtable_put(fields, "montant", 0.0, skip_if_empty=False)
                    airtable_put(fields, "commission_rda", 0, skip_if_empty=False)
                if route_suffix or addr_suffix:
                    if not rem_col:
                        pass
                    else:
                        prev = (rec.get("fields") or {}).get(rem_col) or ""
                        merged = prev
                        if route_suffix and route_suffix.strip(" |") not in prev.replace(" · ", " "):
                            merged = (merged + route_suffix) if merged else f"Ref: {ref}{route_suffix}"
                        if addr_suffix and "Adr:" not in merged:
                            merged = (merged + addr_suffix) if merged else f"Ref: {ref}{addr_suffix}"
                        if merged != prev:
                            fields[rem_col] = merged[:8000]
                updates.append({"id": rec["id"], "fields": fields})

            if updates:
                payload = {"records": updates, "typecast": True}
                r = requests.patch(airtable_url(), headers=airtable_headers(), json=payload, timeout=15)
                if r.status_code != 200:
                    last_airtable_error = f"PATCH {r.status_code}: {r.text[:2500]}"
                    print(f"Airtable UPDATE {len(updates)} records: {r.status_code} - {r.text[:500]}")
                else:
                    last_airtable_error = None
                    print(f"Airtable UPDATE {len(updates)} records: OK")

    except Exception as e:
        last_airtable_error = f"EXC airtable_save: {e}"
        print(f"Airtable save error: {e}")
        import traceback

        traceback.print_exc()


# ===== CONVERSATIONS =====


def fresh_dossier_data(lang="fr"):
    """Etat initial pour un nouveau dossier (meme structure que nouvelle conversation)."""
    return {
        "flow_mode": "dossier",
        "passengers": None,
        "incident_type": None,
        "cancel_notice_gt14": None,
        "flight_type": None,
        "connection_escale": None,
        "airline": None,
        "flight_number": None,
        "flight_date": None,
        "departure_airport": None,
        "arrival_airport": None,
        "route_distance_km": None,
        "distance_band": None,
        "passenger_names": [],
        "passenger_name_collect_index": None,
        "has_minors": None,
        "minors_count": 0,
        "minor_passenger_indices": [],
        "language": lang,
        "temp_year": None,
        "temp_month": None,
        "temp_years": [],
        "mailing_address": None,
        "wa_id": None,
        "dossier_phone": None,
    }


def get_or_create_conversation(phone):
    if phone not in conversations:
        conversations[phone] = {
            "messages": [],
            "current_step": None,
            "ref_dossier": None,
            "data": fresh_dossier_data("fr"),
            "created": datetime.now(),
        }
    if (datetime.now() - conversations[phone]["created"]) > timedelta(hours=MEMORY_HOURS):
        del conversations[phone]
        return get_or_create_conversation(phone)
    return conversations[phone]


# ===== DEDUP (cle inclut l'etape du flux pour ne pas bloquer sur des "1" repetes) =====


def is_duplicate_event(phone, data, sig, flow_step=None):
    now = datetime.now()
    to_del = [k for k, ts in recent_event_ids.items() if (now - ts).total_seconds() > 900]
    for k in to_del:
        recent_event_ids.pop(k, None)

    event_id = data.get("messageId") or data.get("id") or data.get("whatsappMessageId")
    if event_id:
        if event_id in recent_event_ids:
            return True
        recent_event_ids[event_id] = now

    step_key = flow_step if flow_step is not None else "none"
    key = hashlib.sha256(f"{phone}|{sig}|step:{step_key}".encode()).hexdigest()
    if key in recent_event_ids:
        if (now - recent_event_ids[key]).total_seconds() < 25:
            return True
    recent_event_ids[key] = now
    return False


# ===== ENVOI WATI =====


def send_whatsapp_text(phone, message):
    message = message.strip()
    if not message:
        return 0
    url = f"{WATI_BASE_URL}/api/v1/sendSessionMessage/{phone}"
    headers = {"Authorization": f"Bearer {WATI_API_TOKEN}", "accept": "*/*"}
    try:
        r = requests.post(url, headers=headers, params={"messageText": message}, timeout=30)
        print(f"Wati: {r.status_code}")
        return r.status_code
    except Exception as e:
        print(f"Wati error: {e}")
        return 500


# ===== QUESTIONS DU FLUX =====


def ask_entry_intent(phone, lang="fr"):
    """Menu d'aiguillage : WhatsApp vs site vs question (filtre + conversion)."""
    if lang == "en":
        msg = (
            "👋 Hello! Welcome to **Robin des Airs** 🏹\n\n"
            "I'm here to help you recover money after a **delayed or cancelled** flight.\n\n"
            "What would you like to do? (Reply with the number)\n\n"
            "1️⃣ **Open my file now** (quick via WhatsApp)\n"
            "2️⃣ **Complete my file on the website** (if you already have all documents)\n"
            "3️⃣ **I have a specific question**\n\n"
            "Reply **1**, **2** or **3**"
        )
    else:
        msg = (
            "👋 Bonjour ! Bienvenue chez **Robin des Airs** 🏹\n\n"
            "Je suis votre assistant pour recuperer votre argent suite a un vol **retarde ou annule**.\n\n"
            "Que souhaitez-vous faire ? (Repondez avec le chiffre)\n\n"
            "1️⃣ **Ouvrir mon dossier maintenant** (rapide via WhatsApp)\n"
            "2️⃣ **Remplir mon dossier sur le site** (si vous avez tous vos documents)\n"
            "3️⃣ **J'ai une question specifique**\n\n"
            "Repondez **1**, **2** ou **3**"
        )
    send_whatsapp_text(phone, msg)


def ask_confirm_phone(phone, conv):
    """Demande si le numero WhatsApp (waId) est bien le contact dossier."""
    lang = conv["data"].get("language", "fr")
    ref = conv.get("ref_dossier") or "..."
    disp = normalize_dossier_phone(phone) or str(phone)
    conv["data"]["wa_id"] = phone
    if lang == "en":
        msg = (
            f"📱 For file **{ref}**, is this the number we should use to reach you?\n\n"
            f"👉 **{disp}**\n\n"
            "1️⃣ Yes — this is my number for the claim\n"
            "2️⃣ No — I'll send another number\n\n"
            "Reply **1** or **2**"
        )
    else:
        msg = (
            f"📂 J'ouvre votre dossier **{ref}**.\n\n"
            f"📱 **Numero pour vous joindre** (WhatsApp detecte) :\n👉 **{disp}**\n\n"
            "C'est bien ce numero qu'on garde pour ce dossier ?\n\n"
            "1️⃣ Oui\n"
            "2️⃣ Non — j'envoie un autre numero\n\n"
            "Repondez **1** ou **2**"
        )
    hint = (
        "\n\n⬅️ *retour* / *precedent* = question precedente."
        if lang == "fr"
        else "\n\n⬅️ *back* / *previous* = go one question back."
    )
    send_whatsapp_text(phone, msg + hint)


def ask_passengers(phone, lang="fr"):
    if lang == "en":
        msg = (
            "Super! 🎉 To start: **how many passengers** were on this flight with you?\n"
            "(The more passengers, the higher the potential compensation 💰)\n\n"
            "1️⃣ 1 passenger\n2️⃣ 2 passengers\n3️⃣ 3 passengers\n"
            "4️⃣ 4 passengers\n5️⃣ 5 passengers\n6️⃣ 6 or more — Climbie calls you\n\n"
            "Reply with the number (1-6)"
        )
    else:
        msg = (
            "Super ! 🎉 Pour commencer : **combien de passagers** etaient avec vous sur ce vol ?\n"
            "(Plus vous etes nombreux, plus l'indemnite potentielle est forte 💰)\n\n"
            "1️⃣ 1 passager\n2️⃣ 2 passagers\n3️⃣ 3 passagers\n"
            "4️⃣ 4 passagers\n5️⃣ 5 passagers\n6️⃣ 6 ou plus — Climbie vous appelle\n\n"
            "Repondez avec le numero (1-6)"
        )
    hint = (
        "\n\n⬅️ *retour* = question precedente."
        if lang == "fr"
        else "\n\n⬅️ *back* = previous question."
    )
    send_whatsapp_text(phone, msg + hint)


def send_passenger_potential_hook(phone, conv):
    """Apres le nombre de passagers : ancrage sur la tranche haute (long-courrier / 600 EUR par passager)."""
    lang = conv["data"].get("language", "fr")
    pax = conv["data"].get("passengers") or 1
    max_brut = pax * 600
    max_net = int(max_brut * 0.75)
    if pax == 1:
        pax_en = "**1 passenger**"
        pax_fr = "**1 passager**"
    else:
        pax_en = f"**{pax} passengers**"
        pax_fr = f"**{pax} passagers**"
    if lang == "en":
        msg = (
            f"💡 With {pax_en}, on a **long-haul** flights the EU261 **cap** is often **600 EUR per passenger** "
            f"(if the rules apply). For your group, the **maximum** for this claim is **{max_brut} EUR** gross.\n\n"
            f"👉 Often around **~{max_net} EUR** for you after our **25% fee (success only)**.\n\n"
            "📌 **Order of magnitude** — the final amount depends on your actual route.\n\n"
            "➡️ Shall we continue with a few quick questions?"
        )
    else:
        msg = (
            f"💡 Avec {pax_fr}, sur un vol **long-courrier**, le **plafond** UE261 est souvent **600 EUR par passager** "
            f"(si les regles s'appliquent). Pour votre groupe, le **maximum** pour ce dossier est **{max_brut} EUR** brut.\n\n"
            f"👉 Souvent **~{max_net} EUR** pour vous apres notre **commission 25%** (uniquement si succes).\n\n"
            "📌 **Ordre de grandeur** — le montant definitif depend du trajet reel.\n\n"
            "➡️ On enchaine avec quelques questions rapides ?"
        )
    send_whatsapp_text(phone, msg)


def ask_incident_type(phone, conv):
    lang = conv["data"]["language"]
    if lang == "en":
        msg = (
            "✈️ What happened on this flight?\n\n"
            "1️⃣ Arrival delay **3 hours or more**\n2️⃣ Flight cancelled\n3️⃣ Denied boarding\n\n"
            "Reply with 1, 2 or 3"
        )
    else:
        msg = (
            "✈️ Que s'est-il passe sur ce vol ?\n\n"
            "1️⃣ Retard a l'arrivee **d'au moins 3 heures**\n2️⃣ Vol annule\n3️⃣ Refus d'embarquement\n\n"
            "Repondez avec 1, 2 ou 3"
        )
    send_whatsapp_text(phone, msg)


def ask_cancel_notice_period(phone, conv):
    lang = conv["data"]["language"]
    if lang == "en":
        msg = (
            "📋 Cancellation — notice period (EU261)\n\n"
            "When did the airline inform you the flight was cancelled,\n"
            "relative to the scheduled departure?\n\n"
            "1️⃣ More than 14 days before departure\n"
            "2️⃣ 14 days or less before departure\n\n"
            "Reply 1 or 2"
        )
    else:
        msg = (
            "📋 Annulation — delai de prevenance (UE261)\n\n"
            "La compagnie vous a informe(e) de l'annulation :\n\n"
            "1️⃣ Plus de 14 jours avant l'heure de depart prevue\n"
            "2️⃣ 14 jours ou moins avant le depart prevu\n\n"
            "Repondez 1 ou 2"
        )
    send_whatsapp_text(phone, msg)


def ask_flight_type(phone, conv):
    lang = conv["data"]["language"]
    if lang == "en":
        msg = "✈️ Was your flight:\n\n1️⃣ Direct flight\n2️⃣ With connection(s)\n\nReply with 1 or 2"
    else:
        msg = "✈️ Votre vol etait :\n\n1️⃣ Vol direct\n2️⃣ Avec correspondance(s)\n\nRepondez avec 1 ou 2"
    send_whatsapp_text(phone, msg)


def ask_connection_escale(phone, conv):
    """Vol avec correspondance : localisation de l'escale."""
    lang = conv["data"]["language"]
    if lang == "en":
        msg = (
            "🛬 **Connecting flight** — where was your **layover** (city or airport)?\n\n"
            "Examples: Casablanca CMN, Lisbon, Brussels\n\n"
            "One short line is enough."
        )
    else:
        msg = (
            "🛬 Vol avec **correspondance** — **ou** etait votre **escale** (ville ou aeroport) ?\n\n"
            "Exemples : Casablanca CMN, Lisbonne, Bruxelles\n\n"
            "Une ligne courte suffit."
        )
    send_whatsapp_text(phone, msg)


def ask_airline(phone, conv):
    lang = conv["data"]["language"]
    lines = []
    for k in sorted(AIRLINES_MAP.keys(), key=int):
        flag = AIRLINE_FLAGS.get(k, "")
        name = AIRLINES_MAP[k]
        lines.append(f"{k}️⃣ {flag} {name}")
    block = "\n".join(lines)
    if lang == "en":
        msg = (
            "🛫 Which airline?\n\n"
            f"{block}\n"
            "9️⃣ Other (type the name)\n\n"
            "Reply with 1-9 OR type the airline name directly"
        )
    else:
        msg = (
            "🛫 Quelle compagnie aerienne ?\n\n"
            f"{block}\n"
            "9️⃣ Autre (tapez le nom)\n\n"
            "Repondez avec 1-9 OU tapez directement le nom"
        )
    send_whatsapp_text(phone, msg)


def ask_flight_number(phone, conv):
    lang = conv["data"]["language"]
    airline = conv["data"]["airline"] or ""
    if lang == "en":
        msg = (
            f"📝 {airline} ✅\n\nWhat is your flight number?\n\n"
            "Example: AF718, KL563, SN271\n\nOr send a photo of your boarding pass 📸"
        )
    else:
        msg = (
            f"📝 {airline} ✅\n\nQuel est votre numero de vol ?\n\n"
            "Exemple : AF718, KL563, SN271\n\nOu envoyez une photo de votre carte d'embarquement 📸"
        )
    send_whatsapp_text(phone, msg)


def ask_flight_date(phone, conv):
    lang = conv["data"]["language"]
    cy = datetime.now().year
    conv["data"]["temp_years"] = [cy, cy - 1, cy - 2, cy - 3, cy - 4]
    if lang == "en":
        msg = (
            f"📅 What year was your flight?\n\n"
            f"1️⃣ {cy}\n2️⃣ {cy-1}\n3️⃣ {cy-2}\n4️⃣ {cy-3}\n5️⃣ {cy-4}\n"
            f"6️⃣ Before {cy-4} (outside 5-year limit)\n\nReply with 1-6"
        )
    else:
        msg = (
            f"📅 De quelle annee etait votre vol ?\n\n"
            f"1️⃣ {cy}\n2️⃣ {cy-1}\n3️⃣ {cy-2}\n4️⃣ {cy-3}\n5️⃣ {cy-4}\n"
            f"6️⃣ Avant {cy-4} (hors retroactivite)\n\nRepondez avec 1-6"
        )
    send_whatsapp_text(phone, msg)


def ask_flight_month(phone, conv):
    lang = conv["data"]["language"]
    if lang == "en":
        msg = (
            "📅 Which month?\n\n"
            "1️⃣ January\n2️⃣ February\n3️⃣ March\n4️⃣ April\n5️⃣ May\n6️⃣ June\n"
            "7️⃣ July\n8️⃣ August\n9️⃣ September\n🔟 October\n"
            "**11** — November\n**12** — December\n\n"
            "Reply with the number (1-12)"
        )
    else:
        msg = (
            "📅 Quel mois ?\n\n"
            "1️⃣ Janvier\n2️⃣ Fevrier\n3️⃣ Mars\n4️⃣ Avril\n5️⃣ Mai\n6️⃣ Juin\n"
            "7️⃣ Juillet\n8️⃣ Aout\n9️⃣ Septembre\n🔟 Octobre\n"
            "**11** — Novembre\n**12** — Decembre\n\n"
            "Repondez avec le numero (1-12)"
        )
    send_whatsapp_text(phone, msg)


def ask_flight_day(phone, conv):
    lang = conv["data"]["language"]
    msg = "📅 Tapez le jour exact (1-31) :" if lang == "fr" else "📅 Type the exact day (1-31):"
    send_whatsapp_text(phone, msg)


def ask_departure_airport(phone, conv):
    lang = conv["data"]["language"]
    if lang == "en":
        msg = (
            "🛫 Departure airport (start of the disrupted flight)\n\n"
            "Send city or IATA code (e.g. Paris CDG, LOS, BRU).\n\n"
            "We estimate distance automatically — no need to know km."
        )
    else:
        msg = (
            "🛫 Aeroport (ou ville) de DEPART du vol concerne\n\n"
            "Envoyez la ville ou le code IATA (ex : Paris CDG, Abidjan ABJ, Bruxelles).\n\n"
            "On calcule la distance automatiquement."
        )
    send_whatsapp_text(phone, msg)


def ask_arrival_airport(phone, conv):
    lang = conv["data"]["language"]
    dep = conv["data"].get("departure_airport") or "?"
    if lang == "en":
        msg = (
            f"🛬 Final arrival airport (end of this flight)\n\n"
            f"Depart noted: {dep}\n\nSend city or IATA code."
        )
    else:
        msg = (
            f"🛬 Aeroport (ou ville) d'ARRIVEE FINALE de ce vol\n\n"
            f"Depart note : {dep}\n\nEnvoyez la ville ou le code IATA."
        )
    send_whatsapp_text(phone, msg)


def format_prenom_nom(clean_line):
    """Normalise en 'Prenom NOM' (premier token en titre, reste en majuscules)."""
    parts = re.split(r"\s+", clean_line.strip())
    if len(parts) < 2:
        return None
    prenom = parts[0].title()
    nom = " ".join(parts[1:]).upper()
    return f"{prenom} {nom}"


def ask_single_passenger_name(phone, conv):
    """Demande le nom du passager i/pax (un par un)."""
    lang = conv["data"]["language"]
    pax = conv["data"]["passengers"] or 1
    idx = conv["data"].get("passenger_name_collect_index") or 1
    already_en = ""
    already_fr = ""
    if idx > 1:
        names_so_far = conv["data"].get("passenger_names") or []
        if names_so_far:
            bullets_fr = "\n".join([f"✅ **{j}.** {n}" for j, n in enumerate(names_so_far, start=1)])
            bullets_en = "\n".join([f"✅ **{j}.** {n}" for j, n in enumerate(names_so_far, start=1)])
            already_fr = f"**Deja enregistre :**\n{bullets_fr}\n\n"
            already_en = f"**Already saved :**\n{bullets_en}\n\n"
    if lang == "en":
        msg = (
            f"{already_en}"
            f"👤 Passenger **{idx} of {pax}** — send **First LAST** (last name in CAPS).\n\n"
            "Example: John DOE\n\n"
            "One line only for this passenger."
        )
    else:
        msg = (
            f"{already_fr}"
            f"👤 Passager **{idx} sur {pax}** — envoyez **Prénom NOM** (nom de famille en MAJUSCULES).\n\n"
            "Exemple : Jean DUPONT\n\n"
            "Une seule ligne pour ce passager."
        )
    send_whatsapp_text(phone, msg)


def ask_minors(phone, conv):
    lang = conv["data"]["language"]
    pax = conv["data"]["passengers"] or 1
    if pax == 1:
        msg = (
            "👤 Etes-vous majeur(e) (18+ ans) ?\n\n1️⃣ Oui, majeur\n2️⃣ Non, mineur\n\nRepondez avec 1 ou 2"
            if lang == "fr"
            else "👤 Are you over 18?\n\n1️⃣ Yes, adult\n2️⃣ No, minor\n\nReply with 1 or 2"
        )
    else:
        msg = (
            f"👶 Parmi vos **{pax} passagers**, y a-t-il des mineurs (moins **de** 18 ans) ?\n\n"
            "1️⃣ Non, tous majeurs\n2️⃣ Oui, il y a des mineurs\n\nRepondez avec 1 ou 2"
            if lang == "fr"
            else f"👶 Among your **{pax} passengers**, any minors (under **18**)?\n\n1️⃣ No, all adults\n2️⃣ Yes, there are minors\n\nReply with 1 or 2"
        )
    send_whatsapp_text(phone, msg)


def ask_minor_who(phone, conv):
    """Demande quels passagers (par numero dans la liste) sont mineurs."""
    lang = conv["data"]["language"]
    names = conv["data"].get("passenger_names") or []
    block = "\n".join([f"**{i}.** {n}" for i, n in enumerate(names, start=1)])
    if lang == "en":
        msg = (
            f"👶 Which passenger(s) are **minors** (under 18)?\n\n{block}\n\n"
            "Reply with **passenger number(s)** separated by a comma if several (e.g. **2** or **1,2**)."
        )
    else:
        msg = (
            f"👶 Quel(s) passager(s) sont **mineurs** (moins de 18 ans) ?\n\n{block}\n\n"
            "Repondez avec le(s) **numero(s)** separe(s) par une **virgule** si besoin (ex. **2** ou **1,2**)."
        )
    send_whatsapp_text(phone, msg)


def ask_mailing_address(phone, conv):
    """Derniere question du flux : adresse postale."""
    lang = conv["data"].get("language", "fr")
    if lang == "en":
        msg = (
            "🏠 **Last question** — your **postal address** (for the file).\n\n"
            "Send **one message** with street, extra line if needed, postcode, city, country if relevant.\n\n"
            "**Example:**\n"
            "12 High Street, Flat 4\n"
            "SW1A 1AA London\n"
            "United Kingdom"
        )
    else:
        msg = (
            "🏠 **Derniere question** — votre **adresse postale** (dossier / courrier).\n\n"
            "Envoyez **un message** : rue, complement si besoin, code postal, ville, pays si utile.\n\n"
            "**Exemple :**\n"
            "12 rue de Rivoli, appartement 4\n"
            "75001 Paris\n"
            "France"
        )
    send_whatsapp_text(phone, msg)


def mailing_address_accepts(text):
    t = text.strip()
    if len(t) < 12:
        return False
    if not re.search(r"[a-zA-ZÀ-ÿ]", t):
        return False
    return True


def canonical_digit_choice(choice):
    """Normalise '08' -> '8', '12' -> '12' pour comparaison aux menus."""
    if choice is None or not str(choice).strip().isdigit():
        return None
    return str(int(str(choice).strip()))


def openai_extract_allowed_choice(user_text, allowed, lang="fr"):
    """Extrait un numero de menu strictement dans `allowed` (strings '1'..'31', etc.)."""
    if not OPENAI_API_KEY or not (user_text or "").strip():
        return None
    allowed = {str(x) for x in allowed}
    allowed_list = ", ".join(sorted(allowed, key=lambda x: int(x)))
    hint = "User writes in French." if lang == "fr" else "User may write in English."
    system = (
        "You extract exactly ONE menu option number from the user message. "
        f"{hint} Valid options only: {allowed_list}. "
        'Reply with ONLY compact JSON: {"c":"3"} where c is exactly one valid string from the list. '
        'If impossible, {"c":null}. No markdown, no prose.'
    )
    try:
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_text.strip()[:800]},
                ],
                "temperature": 0.1,
                "max_tokens": 60,
            },
            timeout=35,
        )
        txt = (r.json().get("choices") or [{}])[0].get("message", {}).get("content") or ""
        m = re.search(r"\{[\s\S]*?\}", txt)
        if not m:
            return None
        data = json.loads(m.group())
        c = data.get("c")
        if c is None:
            return None
        c = str(int(str(c))) if str(c).strip().isdigit() else None
        if c and c in allowed:
            return c
    except Exception as e:
        print(f"openai_extract_allowed_choice error: {e}")
    return None


def openai_extract_minor_indices(user_text, passenger_names):
    """Liste 1-based des passagers mineurs, ou None."""
    if not OPENAI_API_KEY or not (user_text or "").strip() or not passenger_names:
        return None
    listing = "\n".join([f"{i}. {n}" for i, n in enumerate(passenger_names, start=1)])
    mx = len(passenger_names)
    system = (
        "Passengers (line number = passenger index):\n"
        f"{listing}\n"
        f"Which line numbers (1 to {mx}) are minors under 18? "
        'Reply ONLY JSON like {"m":[2]} or {"m":[1,2]}. '
        'If impossible or unclear, {"m":null}. No markdown.'
    )
    try:
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_text.strip()[:800]},
                ],
                "temperature": 0.1,
                "max_tokens": 80,
            },
            timeout=35,
        )
        txt = (r.json().get("choices") or [{}])[0].get("message", {}).get("content") or ""
        m = re.search(r"\{[\s\S]*?\}", txt)
        if not m:
            return None
        data = json.loads(m.group())
        raw = data.get("m")
        if not isinstance(raw, list):
            return None
        out = []
        for x in raw:
            try:
                n = int(x)
            except (TypeError, ValueError):
                continue
            if 1 <= n <= mx:
                out.append(n)
        return sorted(set(out)) if out else None
    except Exception as e:
        print(f"openai_extract_minor_indices error: {e}")
        return None


def augment_menu_choice(step, text, choice, lang="fr"):
    """Si le premier nombre du message ne matche pas le menu, tente une extraction IA."""
    allowed = MENU_ALLOWED_BY_STEP.get(step)
    if not allowed:
        return choice
    c = canonical_digit_choice(choice)
    if c in allowed:
        return c
    if not OPENAI_API_KEY:
        return choice
    got = openai_extract_allowed_choice(text, allowed, lang=lang if lang == "en" else "fr")
    return got if got in allowed else choice


def openai_estimate_route_km(departure_text, arrival_text, lang):
    """
    Estime la distance orthodromique principale (km) entre departure et arrival.
    Ne modifie pas l'historique conversationnel du client.
    """
    if not OPENAI_API_KEY:
        return None
    system = (
        "You estimate great-circle distance in kilometers between two passenger flight endpoints for EU261 bracketing. "
        "Use the main commercial airport when a city has several (e.g. Paris: assume CDG if unspecified). "
        "Reply with ONLY compact JSON: {\"distance_km\": <integer>, \"departure_resolved\": <string>, \"arrival_resolved\": <string>}. "
        "distance_km must be a realistic positive integer. No markdown, no prose."
    )
    user = f"Departure (user): {departure_text}\nArrival (user): {arrival_text}\nLanguage: {lang}"
    try:
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.15,
                "max_tokens": 180,
            },
            timeout=45,
        )
        raw = r.json()
        txt = (raw.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        m = re.search(r"\{[\s\S]*\}", txt)
        if not m:
            return None
        data = json.loads(m.group())
        km = data.get("distance_km")
        if km is None:
            return None
        km = int(round(float(km)))
        if km < 50 or km > 20000:
            return None
        return {
            "distance_km": km,
            "departure_resolved": (data.get("departure_resolved") or departure_text or "").strip(),
            "arrival_resolved": (data.get("arrival_resolved") or arrival_text or "").strip(),
        }
    except Exception as e:
        print(f"openai_estimate_route_km error: {e}")
        return None


def finalize_route_after_arrival(phone, conv):
    """Apres saisie arrivee : calcule distance + tranche, puis enchaine les noms."""
    lang = conv["data"]["language"]
    dep_raw = (conv["data"].get("departure_airport") or "").strip()
    arr_raw = (conv["data"].get("arrival_airport") or "").strip()
    est = openai_estimate_route_km(dep_raw, arr_raw, lang)
    if est:
        conv["data"]["route_distance_km"] = est["distance_km"]
        conv["data"]["departure_airport"] = est["departure_resolved"] or dep_raw
        conv["data"]["arrival_airport"] = est["arrival_resolved"] or arr_raw
        conv["data"]["distance_band"] = band_from_distance_km(est["distance_km"])
    else:
        conv["data"]["distance_band"] = "band_unknown"
        conv["data"]["route_distance_km"] = None

    band_id = conv["data"].get("distance_band", "band_unknown")
    band_lbl = EU261_BANDS.get(band_id, EU261_BANDS["band_unknown"]).get("label")
    km_show = conv["data"].get("route_distance_km")

    if lang == "en":
        if km_show:
            line = (
                f"📏 Estimated route: ~{km_show} km ({band_lbl})\n"
                f"🛫 {conv['data'].get('departure_airport')} → 🛬 {conv['data'].get('arrival_airport')}"
            )
        else:
            line = "📏 Could not auto-estimate distance — amounts may need manual review."
    else:
        if km_show:
            line = (
                f"📏 Distance estimee : ~{km_show} km ({band_lbl})\n"
                f"🛫 {conv['data'].get('departure_airport')} → 🛬 {conv['data'].get('arrival_airport')}"
            )
        else:
            line = "📏 Distance non estimee automatiquement — montants a verifier au dossier."

    send_whatsapp_text(phone, line)
    conv["data"]["passenger_names"] = []
    conv["data"]["passenger_name_collect_index"] = 1
    conv["current_step"] = "passenger_name_collect"
    airtable_save_progressive(phone, conv)
    ask_single_passenger_name(phone, conv)


def show_summary(phone, conv):
    lang = conv["data"]["language"]
    d = conv["data"]
    pax = d["passengers"] or 1
    band_id = d.get("distance_band", "band_unknown")
    band_info = EU261_BANDS.get(band_id, EU261_BANDS["band_unknown"])
    per_pax = band_info.get("amount_eur")
    total = (per_pax * pax) if per_pax else None
    net = int(total * 0.75) if total else None
    band_label = band_info.get("label")

    incident = INCIDENT_LABELS.get(d.get("incident_type", ""), d.get("incident_type", "?"))

    ref = conv.get("ref_dossier") or generate_ref_dossier(phone)
    conv["ref_dossier"] = ref

    names_str = "\n".join([f"  - {n}" for n in d.get("passenger_names", [])]) or "  - A completer"

    route_line_fr = ""
    route_line_en = ""
    dep_a = d.get("departure_airport")
    arr_a = d.get("arrival_airport")
    rkm = d.get("route_distance_km")
    esc = (d.get("connection_escale") or "").strip()
    if dep_a and arr_a:
        route_line_fr = f"🛫🛬 Trajet : {dep_a} → {arr_a}"
        route_line_en = f"🛫🛬 Route: {dep_a} → {arr_a}"
        if rkm:
            route_line_fr += f" (~{int(rkm)} km)"
            route_line_en += f" (~{int(rkm)} km)"
        if esc:
            route_line_fr += f"\n🔄 Escale : {esc}"
            route_line_en += f"\n🔄 Layover: {esc}"
        route_line_fr += "\n"
        route_line_en += "\n"

    idxs_m = sorted(d.get("minor_passenger_indices") or [])
    names_m = d.get("passenger_names") or []
    minor_named = [names_m[i - 1] for i in idxs_m if isinstance(i, int) and 1 <= i <= len(names_m)]
    if minor_named:
        minor_line_fr = f"👶 Mineurs : {', '.join(minor_named)}\n"
        minor_line_en = f"👶 Minors: {', '.join(minor_named)}\n"
    elif d.get("has_minors") is False:
        minor_line_fr = "👶 Mineurs : aucun\n"
        minor_line_en = "👶 Minors: none\n"
    else:
        minor_line_fr = f"👶 Mineurs : {d.get('minors_count', 0)}\n"
        minor_line_en = f"👶 Minors: {d.get('minors_count', 0)}\n"

    cancel_hint_fr = ""
    cancel_hint_en = ""
    if d.get("incident_type") == "cancel":
        if d.get("cancel_notice_gt14") is True:
            cancel_hint_fr = (
                "⚠️ Annulation : prevenance >14 j avant depart → indemnisation fixe souvent "
                "NON due (UE261), selon reroutage/remboursement — analyse au dossier.\n"
            )
            cancel_hint_en = (
                "⚠️ Cancellation: informed >14 days before departure → fixed compensation often "
                "NOT owed (EU261), depending on rerouting/refund — case-by-case.\n"
            )
        elif d.get("cancel_notice_gt14") is False:
            cancel_hint_fr = (
                "📋 Annulation : prevenance ≤14 j → verifier reroutage/delais pour droits eventuels.\n"
            )
            cancel_hint_en = (
                "📋 Cancellation: notice ≤14 days → check rerouting/timing for possible rights.\n"
            )

    params_dict = {
        "ref": ref,
        "pax": pax,
        "vol": d.get("flight_number", ""),
        "date": d.get("flight_date", ""),
        "compagnie": d.get("airline", ""),
        "incident": d.get("incident_type", ""),
        "type_vol": d.get("flight_type", ""),
        "distance": band_id,
        "depart": dep_a or "",
        "arrivee": arr_a or "",
        "km_route": int(rkm) if rkm else "",
        "noms": ",".join(d.get("passenger_names", [])),
        "mineurs": d.get("minors_count", 0),
        "adresse": (d.get("mailing_address") or "").replace("\n", ", ")[:500],
        "tel": (normalize_dossier_phone(d.get("dossier_phone")) or d.get("dossier_phone") or "")[:32],
        "source": "whatsapp_bot",
    }
    query = "&".join([f"{k}={requests.utils.quote(str(v))}" for k, v in params_dict.items() if v])
    mandat_link = f"{MANDAT_URL}?{query}"

    if per_pax:
        money = f"💶 {per_pax} EUR/passager ({band_label})\n💰 TOTAL : {total} EUR\n✅ NET POUR VOUS : {net} EUR"
    else:
        money = "💶 Montant a confirmer selon distance"

    addr_show = (d.get("mailing_address") or "").strip()
    addr_line_en = f"📮 Address: {addr_show}\n" if addr_show else ""
    addr_line_fr = f"📮 Adresse : {addr_show}\n" if addr_show else ""
    tel_disp = normalize_dossier_phone(d.get("dossier_phone")) or (d.get("dossier_phone") or "")
    tel_line_en = f"📱 Contact: {tel_disp}\n" if tel_disp else ""
    tel_line_fr = f"📱 Contact dossier : {tel_disp}\n" if tel_disp else ""

    if lang == "en":
        body = (
            f"🎉 PERFECT!\n\n"
            f"📋 File ref: {ref}\n"
            f"{tel_line_en}"
            f"✈️ Flight: {d.get('flight_number','?')} ({d.get('airline','?')})\n"
            f"📅 Date: {d.get('flight_date','?')}\n"
            f"{route_line_en}"
            f"👥 Passengers: {pax}\n{names_str}\n"
            f"{addr_line_en}"
            f"{minor_line_en}"
            f"⚠️ Incident: {incident}\n{cancel_hint_en}"
            f"📏 Distance bracket: {band_label}\n\n{money}\n\n"
            f"👇 Sign your mandate (~3 min):\n{mandat_link}"
        )
    else:
        body = (
            f"🎉 PARFAIT !\n\n"
            f"📋 Ref dossier : {ref}\n"
            f"{tel_line_fr}"
            f"✈️ Vol : {d.get('flight_number','?')} ({d.get('airline','?')})\n"
            f"📅 Date : {d.get('flight_date','?')}\n"
            f"{route_line_fr}"
            f"👥 Passagers : {pax}\n{names_str}\n"
            f"{addr_line_fr}"
            f"{minor_line_fr}"
            f"⚠️ Incident : {incident}\n{cancel_hint_fr}"
            f"📏 Tranche distance : {band_label}\n\n{money}\n\n"
            f"👇 Signez votre mandat (~3 min) :\n{mandat_link}"
        )
    nav_hint = (
        "\n\n⬅️ **retour** / **precedent** = corriger la **derniere** reponse ; **modifier** = menu des sections."
        if lang == "fr"
        else "\n\n⬅️ **back** / **previous** = fix the **last** answer; **edit** = section menu."
    )
    send_whatsapp_text(phone, body + nav_hint)

    airtable_save_progressive(phone, conv)
    conv["current_step"] = "completed"


def text_command_normalize(text):
    """Minuscules + suppression des accents pour commandes texte."""
    if not text:
        return ""
    s = text.strip().lower()
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def is_previous_command(text):
    """Commande 'question precedente' / retour arriere (WhatsApp = texte)."""
    n = text_command_normalize(text)
    if not n:
        return False
    if n in ("precedent", "retour", "back", "previous", "<<", "<", "remonter"):
        return True
    if "question precedente" in n:
        return True
    return False


def is_modify_menu_command(text):
    """Ouvre le menu de modification apres le recap."""
    n = text_command_normalize(text)
    return n in ("modifier", "corriger", "edit", "changer", "correction")


def clear_post_incident_flight_data(d):
    """Efface tout ce qui est collecte apres l'incident (vol, trajet, passagers, adresse)."""
    for k, v in (
        ("flight_type", None),
        ("connection_escale", None),
        ("airline", None),
        ("flight_number", None),
        ("flight_date", None),
        ("temp_year", None),
        ("temp_month", None),
        ("temp_years", []),
        ("departure_airport", None),
        ("arrival_airport", None),
        ("route_distance_km", None),
        ("distance_band", None),
        ("passenger_names", []),
        ("passenger_name_collect_index", None),
        ("has_minors", None),
        ("minors_count", 0),
        ("minor_passenger_indices", []),
        ("mailing_address", None),
    ):
        d[k] = v


def clear_post_airline_data(d):
    """Efface de la compagnie jusqu'a la fin du dossier (saisie vol)."""
    for k, v in (
        ("airline", None),
        ("flight_number", None),
        ("flight_date", None),
        ("temp_year", None),
        ("temp_month", None),
        ("temp_years", []),
        ("departure_airport", None),
        ("arrival_airport", None),
        ("route_distance_km", None),
        ("distance_band", None),
        ("passenger_names", []),
        ("passenger_name_collect_index", None),
        ("has_minors", None),
        ("minors_count", 0),
        ("minor_passenger_indices", []),
        ("mailing_address", None),
    ):
        d[k] = v


def clear_post_flight_date_data(d):
    """Efface date de vol, aeroports, passagers, adresse (garde incident et compagnie/n° si deja la)."""
    for k, v in (
        ("flight_date", None),
        ("temp_year", None),
        ("temp_month", None),
        ("temp_years", []),
        ("departure_airport", None),
        ("arrival_airport", None),
        ("route_distance_km", None),
        ("distance_band", None),
        ("passenger_names", []),
        ("passenger_name_collect_index", None),
        ("has_minors", None),
        ("minors_count", 0),
        ("minor_passenger_indices", []),
        ("mailing_address", None),
    ):
        d[k] = v


def resend_current_question(phone, conv):
    """Renvoie la question correspondant a current_step."""
    step = conv.get("current_step")
    lang = conv["data"].get("language", "fr")
    if step == "confirm_phone":
        ask_confirm_phone(phone, conv)
    elif step == "phone_other_input":
        lang = conv["data"].get("language", "fr")
        send_whatsapp_text(
            phone,
            "📱 Envoyez votre numero en **international** (ex : +33 6 12 34 56 78 ou +225...), une seule ligne."
            if lang == "fr"
            else "📱 Send your number in **international** format (e.g. +33 6 12 34 56 78), one line.",
        )
    elif step == "passengers":
        ask_passengers(phone, lang)
    elif step == "incident_type":
        ask_incident_type(phone, conv)
    elif step == "cancel_notice_period":
        ask_cancel_notice_period(phone, conv)
    elif step == "flight_type":
        ask_flight_type(phone, conv)
    elif step == "connection_escale":
        ask_connection_escale(phone, conv)
    elif step == "airline":
        ask_airline(phone, conv)
    elif step == "airline_other_input":
        send_whatsapp_text(
            phone,
            "✍️ Tapez le nom de votre compagnie :" if lang == "fr" else "✍️ Type your airline name:",
        )
    elif step == "flight_number":
        ask_flight_number(phone, conv)
    elif step == "flight_date":
        ask_flight_date(phone, conv)
    elif step == "flight_month":
        ask_flight_month(phone, conv)
    elif step == "flight_day_input":
        ask_flight_day(phone, conv)
    elif step == "departure_airport":
        ask_departure_airport(phone, conv)
    elif step == "arrival_airport":
        ask_arrival_airport(phone, conv)
    elif step == "passenger_name_collect":
        ask_single_passenger_name(phone, conv)
    elif step == "minor_check":
        ask_minors(phone, conv)
    elif step == "minor_who":
        ask_minor_who(phone, conv)
    elif step == "mailing_address":
        ask_mailing_address(phone, conv)
    elif step == "post_summary_edit":
        ask_post_summary_edit_menu(phone, conv)


def ask_post_summary_edit_menu(phone, conv):
    """Menu sauter vers une partie du dossier (apres recap)."""
    lang = conv["data"].get("language", "fr")
    if lang == "en":
        msg = (
            "✏️ **What do you want to change?**\n\n"
            "1️⃣ Postal address\n2️⃣ Passenger names / minors\n3️⃣ Route (airports + flight date)\n"
            "4️⃣ Airline + flight number\n5️⃣ Incident (delay / cancellation…)\n"
            "6️⃣ File phone + number of passengers\n\n"
            "Reply **1**–**6**, or **back** to cancel."
        )
    else:
        msg = (
            "✏️ **Que voulez-vous modifier ?**\n\n"
            "1️⃣ Adresse postale\n2️⃣ Noms des passagers / mineurs\n3️⃣ Trajet (aeroports + date du vol)\n"
            "4️⃣ Compagnie + n° de vol\n5️⃣ Incident (retard / annulation…)\n"
            "6️⃣ Telephone dossier + nombre de passagers\n\n"
            "Repondez **1** a **6**, ou **retour** pour annuler."
        )
    send_whatsapp_text(phone, msg)


def apply_post_summary_edit_choice(phone, conv, choice):
    """Applique un saut depuis le menu post-recap (choice '1'..'6')."""
    d = conv["data"]
    lang = d.get("language", "fr")
    if choice == "1":
        d["mailing_address"] = None
        conv["current_step"] = "mailing_address"
        ask_mailing_address(phone, conv)
    elif choice == "2":
        d["passenger_names"] = []
        d["passenger_name_collect_index"] = 1
        d["has_minors"] = None
        d["minors_count"] = 0
        d["minor_passenger_indices"] = []
        d["mailing_address"] = None
        conv["current_step"] = "passenger_name_collect"
        airtable_save_progressive(phone, conv)
        ask_single_passenger_name(phone, conv)
    elif choice == "3":
        clear_post_flight_date_data(d)
        conv["current_step"] = "flight_date"
        airtable_save_progressive(phone, conv)
        ask_flight_date(phone, conv)
    elif choice == "4":
        clear_post_airline_data(d)
        d["connection_escale"] = None
        airtable_save_progressive(phone, conv)
        if d.get("flight_type") == "connection":
            conv["current_step"] = "connection_escale"
            ask_connection_escale(phone, conv)
        else:
            conv["current_step"] = "airline"
            ask_airline(phone, conv)
    elif choice == "5":
        clear_post_incident_flight_data(d)
        d["incident_type"] = None
        d["cancel_notice_gt14"] = None
        conv["current_step"] = "incident_type"
        airtable_save_progressive(phone, conv)
        ask_incident_type(phone, conv)
    elif choice == "6":
        d["dossier_phone"] = None
        d["passengers"] = None
        conv["current_step"] = "confirm_phone"
        airtable_save_progressive(phone, conv)
        ask_confirm_phone(phone, conv)
    else:
        ask_post_summary_edit_menu(phone, conv)


def navigate_previous_step(phone, conv):
    """
    Recule d'une etape dans le dossier (retour / precedent).
    Retourne True si navigation effectuee.
    """
    step = conv.get("current_step")
    d = conv["data"]

    if step == "confirm_phone":
        return False

    if step == "phone_other_input":
        conv["current_step"] = "confirm_phone"
        resend_current_question(phone, conv)
        return True

    if step == "passengers":
        conv["current_step"] = "confirm_phone"
        resend_current_question(phone, conv)
        return True

    if step == "incident_type":
        d["incident_type"] = None
        d["cancel_notice_gt14"] = None
        conv["current_step"] = "passengers"
        resend_current_question(phone, conv)
        return True

    if step == "cancel_notice_period":
        conv["current_step"] = "incident_type"
        resend_current_question(phone, conv)
        return True

    if step == "flight_type":
        d["flight_type"] = None
        d["connection_escale"] = None
        clear_post_airline_data(d)
        if d.get("incident_type") == "cancel":
            conv["current_step"] = "cancel_notice_period"
            resend_current_question(phone, conv)
        else:
            conv["current_step"] = "incident_type"
            resend_current_question(phone, conv)
        return True

    if step == "connection_escale":
        d["connection_escale"] = None
        clear_post_airline_data(d)
        conv["current_step"] = "flight_type"
        resend_current_question(phone, conv)
        return True

    if step == "airline":
        clear_post_airline_data(d)
        if d.get("flight_type") == "connection":
            conv["current_step"] = "connection_escale"
            resend_current_question(phone, conv)
        else:
            conv["current_step"] = "flight_type"
            resend_current_question(phone, conv)
        return True

    if step == "airline_other_input":
        conv["current_step"] = "airline"
        resend_current_question(phone, conv)
        return True

    if step == "flight_number":
        conv["current_step"] = "airline"
        d["flight_number"] = None
        resend_current_question(phone, conv)
        return True

    if step == "flight_date":
        conv["current_step"] = "flight_number"
        d["temp_year"] = None
        d["temp_month"] = None
        d["temp_years"] = []
        resend_current_question(phone, conv)
        return True

    if step == "flight_month":
        conv["current_step"] = "flight_date"
        d["temp_month"] = None
        resend_current_question(phone, conv)
        return True

    if step == "flight_day_input":
        conv["current_step"] = "flight_month"
        d["flight_date"] = None
        resend_current_question(phone, conv)
        return True

    if step == "departure_airport":
        conv["current_step"] = "flight_day_input"
        d["departure_airport"] = None
        d["arrival_airport"] = None
        d["route_distance_km"] = None
        d["distance_band"] = None
        d["flight_date"] = None
        resend_current_question(phone, conv)
        return True

    if step == "arrival_airport":
        conv["current_step"] = "departure_airport"
        d["arrival_airport"] = None
        d["route_distance_km"] = None
        d["distance_band"] = None
        resend_current_question(phone, conv)
        return True

    if step == "passenger_name_collect":
        pax = d.get("passengers") or 1
        idx = d.get("passenger_name_collect_index") or 1
        names = list(d.get("passenger_names") or [])
        if idx > 1:
            new_idx = idx - 1
            d["passenger_names"] = names[: new_idx - 1]
            d["passenger_name_collect_index"] = new_idx
            resend_current_question(phone, conv)
            return True
        d["passenger_names"] = []
        d["passenger_name_collect_index"] = None
        d["route_distance_km"] = None
        d["distance_band"] = None
        conv["current_step"] = "arrival_airport"
        resend_current_question(phone, conv)
        return True

    if step == "minor_check":
        pax = d.get("passengers") or 1
        names = list(d.get("passenger_names") or [])
        if names and len(names) == pax:
            names.pop()
        d["passenger_names"] = names
        d["passenger_name_collect_index"] = len(names) + 1 if names else 1
        d["has_minors"] = None
        d["minors_count"] = 0
        d["minor_passenger_indices"] = []
        conv["current_step"] = "passenger_name_collect"
        resend_current_question(phone, conv)
        return True

    if step == "minor_who":
        conv["current_step"] = "minor_check"
        d["minor_passenger_indices"] = []
        d["minors_count"] = 0
        resend_current_question(phone, conv)
        return True

    if step == "mailing_address":
        d["mailing_address"] = None
        if d.get("has_minors") is True and (d.get("passengers") or 1) > 1:
            conv["current_step"] = "minor_who"
            resend_current_question(phone, conv)
        else:
            conv["current_step"] = "minor_check"
            resend_current_question(phone, conv)
        return True

    return False


# ===== TRAITEMENT REPONSES =====


def process_reply(phone, text, conv):
    """Traite la reponse du client a chaque etape"""
    step = conv.get("current_step")
    text = text.strip()

    if is_previous_command(text):
        if step == "post_summary_edit":
            conv["current_step"] = "completed"
            lang = conv["data"].get("language", "fr")
            send_whatsapp_text(
                phone,
                "OK — menu annule. Le lien **mandat** est dans le message precedent. "
                "Tapez **modifier** pour rouvrir le menu, ou **retour** pour corriger l'adresse pas a pas."
                if lang == "fr"
                else "OK — menu cancelled. The **mandate** link is in the previous message. "
                "Type **edit** to reopen the menu, or **back** to fix the address step by step.",
            )
            return True
        if step == "completed" and conv["data"].get("flow_mode") == "dossier":
            conv["current_step"] = "mailing_address"
            ask_mailing_address(phone, conv)
            return True
        if navigate_previous_step(phone, conv):
            return True
        if step == "entry_intent":
            return False
        lang = conv["data"].get("language", "fr")
        send_whatsapp_text(
            phone,
            "⬅️ You are already on the **first question** of this file (phone). "
            "To start over: type **menu**."
            if lang == "en"
            else "⬅️ Vous etes deja a la **premiere question** du dossier (telephone). "
            "Pour tout recommencer : tapez **menu**.",
        )
        return True

    if step == "completed" and conv["data"].get("flow_mode") == "dossier":
        if is_modify_menu_command(text):
            conv["current_step"] = "post_summary_edit"
            ask_post_summary_edit_menu(phone, conv)
            return True
        return False

    if step == "post_summary_edit":
        num_match = re.search(r"^(\d+)", text)
        choice = num_match.group(1) if num_match else None
        menu_lang = conv["data"].get("language", "fr")
        choice = augment_menu_choice(step, text, choice, menu_lang)
        if choice in ("1", "2", "3", "4", "5", "6"):
            apply_post_summary_edit_choice(phone, conv, choice)
            return True
        ask_post_summary_edit_menu(phone, conv)
        return True

    num_match = re.search(r"^(\d+)", text)
    choice = num_match.group(1) if num_match else None
    menu_lang = conv["data"].get("language", "fr")
    choice = augment_menu_choice(step, text, choice, menu_lang)

    print(f"[REPLY] step={step} text='{text[:30]}' choice={choice}")

    if step == "entry_intent":
        if choice == "1":
            lang = conv["data"].get("language", "fr")
            conv["data"] = fresh_dossier_data(lang)
            conv["ref_dossier"] = generate_ref_dossier(phone)
            conv["data"]["wa_id"] = phone
            conv["current_step"] = "confirm_phone"
            ask_confirm_phone(phone, conv)
            return True
        if choice == "2":
            lang = conv["data"].get("language", "fr")
            url = RDA_ONLINE_DOSSIER_URL
            if lang == "en":
                msg = (
                    "📂 Perfect — the **online form** is best if you already have your documents "
                    "(boarding pass, proof of delay/cancellation, ID, etc.).\n\n"
                    f"👉 {url}\n\n"
                    "When you're done, you can come back here anytime: type **menu** or hello.\n\n"
                    "⏱️ With all documents ready, the online form usually takes **about 10–15 minutes**.\n\n"
                    "📱 Need help? +33 7 56 86 36 30"
                )
            else:
                msg = (
                    "📂 Parfait ! Le **formulaire en ligne** est ideal si vous avez deja vos documents "
                    "(carte d'embarquement, preuves, piece d'identite, etc.).\n\n"
                    f"👉 {url}\n\n"
                    "Une fois envoye, revenez ici quand vous voulez : tapez **menu** ou bonjour.\n\n"
                    "⏱️ Avec tout sous la main, comptez en general **10 a 15 min** pour le formulaire web.\n\n"
                    "📱 Une question ? +33 7 56 86 36 30"
                )
            send_whatsapp_text(phone, msg)
            conv["data"]["flow_mode"] = "web_redirect"
            conv["current_step"] = "completed"
            return True
        if choice == "3":
            conv["data"]["flow_mode"] = "faq"
            conv["current_step"] = "faq_chat"
            lang = conv["data"]["language"]
            if lang == "en":
                msg = (
                    "💬 Go ahead — **your specific question** (EU261, delays, cancellations, amounts…).\n\n"
                    "ℹ️ General information only — not personal legal advice.\n\n"
                    "📎 To open a WhatsApp file later: reply **1** or type **dossier**\n"
                    f"📎 Prefer the website? Reply **2** at the menu or go to: {RDA_ONLINE_DOSSIER_URL}\n\n"
                    f"📚 Site : {RDA_SITE_URL}"
                )
            else:
                msg = (
                    "💬 Allez-y : **votre question precise** (UE261, retards, annulations, montants…).\n\n"
                    "ℹ️ Information generale — pas un conseil juridique personnalise.\n\n"
                    "📎 Pour ouvrir un dossier WhatsApp apres : repondez **1** ou ecrivez **dossier**\n"
                    f"📎 Plutot le site ? Repondez **2** au menu ou : {RDA_ONLINE_DOSSIER_URL}\n\n"
                    f"📚 Site : {RDA_SITE_URL}"
                )
            send_whatsapp_text(phone, msg)
            return True
        return False

    if step == "confirm_phone":
        if choice == "1":
            conv["data"]["dossier_phone"] = normalize_dossier_phone(phone) or str(phone)
            conv["current_step"] = "passengers"
            ask_passengers(phone, conv["data"]["language"])
            return True
        if choice == "2":
            lang = conv["data"].get("language", "fr")
            send_whatsapp_text(
                phone,
                "📱 Envoyez votre numero en **international** (ex : +33 6 12 34 56 78 ou +225...), une seule ligne."
                if lang == "fr"
                else "📱 Send your number in **international** format (e.g. +33 6 12 34 56 78), one line.",
            )
            conv["current_step"] = "phone_other_input"
            return True
        return False

    if step == "phone_other_input":
        parsed = parse_phone_input_line(text)
        if parsed:
            conv["data"]["dossier_phone"] = parsed
            conv["current_step"] = "passengers"
            ask_passengers(phone, conv["data"]["language"])
            return True
        lang = conv["data"].get("language", "fr")
        send_whatsapp_text(
            phone,
            "📱 Numero non reconnu. Exemple : +33 6 12 34 56 78"
            if lang == "fr"
            else "📱 Number not recognized. Example: +33 6 12 34 56 78",
        )
        return True

    if step == "passengers":
        if choice in ["1", "2", "3", "4", "5"]:
            conv["data"]["passengers"] = int(choice)
            conv["current_step"] = "incident_type"
            airtable_save_progressive(phone, conv)
            send_passenger_potential_hook(phone, conv)
            ask_incident_type(phone, conv)
            return True
        elif choice == "6":
            send_whatsapp_text(
                phone,
                f"🙏 Pour 6+ passagers, Climbie vous appelle.\n\n📱 +33 7 56 86 36 30\n\n👉 {DEPOT_URL}",
            )
            return True
        return False

    if step == "incident_type":
        mapping = {"1": "delay", "2": "cancel", "3": "denied"}
        if choice in mapping:
            incident = mapping[choice]
            conv["data"]["incident_type"] = incident
            if incident != "cancel":
                conv["data"]["cancel_notice_gt14"] = None
            airtable_save_progressive(phone, conv)
            if incident == "cancel":
                conv["current_step"] = "cancel_notice_period"
                ask_cancel_notice_period(phone, conv)
            else:
                conv["current_step"] = "flight_type"
                ask_flight_type(phone, conv)
            return True
        return False

    if step == "cancel_notice_period":
        if conv["data"].get("incident_type") != "cancel":
            conv["current_step"] = "flight_type"
            ask_flight_type(phone, conv)
            return True
        if choice == "1":
            conv["data"]["cancel_notice_gt14"] = True
        elif choice == "2":
            conv["data"]["cancel_notice_gt14"] = False
        else:
            return False
        conv["current_step"] = "flight_type"
        airtable_save_progressive(phone, conv)
        ask_flight_type(phone, conv)
        return True

    if step == "flight_type":
        if choice == "1":
            conv["data"]["flight_type"] = "direct"
            conv["data"]["connection_escale"] = None
            conv["current_step"] = "airline"
        elif choice == "2":
            conv["data"]["flight_type"] = "connection"
            conv["data"]["connection_escale"] = None
            conv["current_step"] = "connection_escale"
        else:
            return False
        airtable_save_progressive(phone, conv)
        if conv["current_step"] == "airline":
            ask_airline(phone, conv)
        else:
            ask_connection_escale(phone, conv)
        return True

    if step == "connection_escale":
        if len(text.strip()) >= 2:
            conv["data"]["connection_escale"] = text.strip()[:300]
            conv["current_step"] = "airline"
            airtable_save_progressive(phone, conv)
            ask_airline(phone, conv)
            return True
        return False

    if step == "airline":
        if choice and choice in AIRLINES_MAP:
            conv["data"]["airline"] = AIRLINES_MAP[choice]
            conv["current_step"] = "flight_number"
            airtable_save_progressive(phone, conv)
            ask_flight_number(phone, conv)
            return True
        if choice == "9":
            lang = conv["data"]["language"]
            send_whatsapp_text(
                phone,
                "✍️ Tapez le nom de votre compagnie :" if lang == "fr" else "✍️ Type your airline name:",
            )
            conv["current_step"] = "airline_other_input"
            return True
        if not choice and len(text) >= 3:
            conv["data"]["airline"] = text
            conv["current_step"] = "flight_number"
            airtable_save_progressive(phone, conv)
            ask_flight_number(phone, conv)
            return True
        return False

    if step == "airline_other_input":
        conv["data"]["airline"] = text
        conv["current_step"] = "flight_number"
        airtable_save_progressive(phone, conv)
        ask_flight_number(phone, conv)
        return True

    if step == "flight_number":
        m = re.search(r"\b([A-Z]{2}\d{2,4})\b", text.upper())
        conv["data"]["flight_number"] = m.group(1) if m else text
        conv["current_step"] = "flight_date"
        airtable_save_progressive(phone, conv)
        ask_flight_date(phone, conv)
        return True

    if step == "flight_date":
        years = conv["data"].get("temp_years", [])
        if choice == "6":
            send_whatsapp_text(
                phone,
                f"😔 Retroactivite 5 ans max.\nVotre vol est trop ancien.\n\n👉 {RDA_SITE_URL}",
            )
            return True
        idx = int(choice) - 1 if choice and choice.isdigit() else -1
        if 0 <= idx < len(years):
            conv["data"]["temp_year"] = str(years[idx])
            conv["current_step"] = "flight_month"
            ask_flight_month(phone, conv)
            return True
        return False

    if step == "flight_month":
        if choice and choice.isdigit() and 1 <= int(choice) <= 12:
            conv["data"]["temp_month"] = f"{int(choice):02d}"
            conv["current_step"] = "flight_day_input"
            ask_flight_day(phone, conv)
            return True
        return False

    if step == "flight_day_input":
        if choice and choice.isdigit() and 1 <= int(choice) <= 31:
            day = f"{int(choice):02d}"
            year = conv["data"].get("temp_year", "")
            month = conv["data"].get("temp_month", "")
            conv["data"]["flight_date"] = f"{day}/{month}/{year}"
            conv["current_step"] = "departure_airport"
            airtable_save_progressive(phone, conv)
            ask_departure_airport(phone, conv)
            return True
        return False

    if step == "departure_airport":
        if len(text) >= 2:
            conv["data"]["departure_airport"] = text
            conv["current_step"] = "arrival_airport"
            airtable_save_progressive(phone, conv)
            ask_arrival_airport(phone, conv)
            return True
        return False

    if step == "arrival_airport":
        if len(text) >= 2:
            conv["data"]["arrival_airport"] = text
            airtable_save_progressive(phone, conv)
            finalize_route_after_arrival(phone, conv)
            return True
        return False

    if step == "passenger_name_collect":
        pax = conv["data"].get("passengers") or 1
        idx = conv["data"].get("passenger_name_collect_index") or 1
        first_line = text.split("\n", 1)[0].strip()
        clean = re.sub(r"^[\d\.\)\-\s]+", "", first_line).strip()
        formatted = format_prenom_nom(clean) if clean and not clean.isdigit() else None
        if not formatted:
            lang = conv["data"].get("language", "fr")
            send_whatsapp_text(
                phone,
                f"👤 Passager {idx}/{pax} : envoyez **Prénom NOM** (2 mots min., ex. Jean DUPONT)."
                if lang == "fr"
                else f"👤 Passenger {idx}/{pax}: send **First LAST** (2+ words, e.g. John DOE).",
            )
            return True
        names = list(conv["data"].get("passenger_names") or [])
        names.append(formatted)
        conv["data"]["passenger_names"] = names
        airtable_save_progressive(phone, conv)
        if len(names) >= pax:
            conv["data"]["passenger_name_collect_index"] = None
            conv["current_step"] = "minor_check"
            ask_minors(phone, conv)
        else:
            conv["data"]["passenger_name_collect_index"] = idx + 1
            ask_single_passenger_name(phone, conv)
        return True

    if step == "minor_check":
        if choice == "1":
            conv["data"]["has_minors"] = False
            conv["data"]["minors_count"] = 0
            conv["data"]["minor_passenger_indices"] = []
            conv["current_step"] = "mailing_address"
            ask_mailing_address(phone, conv)
            return True
        elif choice == "2":
            pax = conv["data"].get("passengers") or 1
            if pax == 1:
                send_whatsapp_text(
                    phone,
                    "👶 Mineur seul : un parent doit signer.\n\n📱 Climbie : +33 7 56 86 36 30",
                )
                return True
            conv["data"]["has_minors"] = True
            conv["current_step"] = "minor_who"
            ask_minor_who(phone, conv)
            return True
        return False

    if step == "minor_who":
        pax = conv["data"].get("passengers") or 1
        names = conv["data"].get("passenger_names") or []
        parts = re.split(r"[,;\n/]|(?:\bet\b)|(?:\bou\b)", text, flags=re.I)
        nums = []
        for p in parts:
            m = re.search(r"\b(\d+)\b", (p or "").strip())
            if m:
                n = int(m.group(1))
                if 1 <= n <= pax:
                    nums.append(n)
        nums = sorted(set(nums))
        if not nums:
            nums = openai_extract_minor_indices(text, names) or []
        if not nums:
            lang = conv["data"].get("language", "fr")
            send_whatsapp_text(
                phone,
                "👶 Indiquez le(s) numero(s) des mineurs (ex. **2** ou **1,2**)."
                if lang == "fr"
                else "👶 Please send minor passenger number(s) (e.g. **2** or **1,2**).",
            )
            ask_minor_who(phone, conv)
            return True
        conv["data"]["minor_passenger_indices"] = nums
        conv["data"]["minors_count"] = len(nums)
        conv["current_step"] = "mailing_address"
        airtable_save_progressive(phone, conv)
        ask_mailing_address(phone, conv)
        return True

    if step == "mailing_address":
        if mailing_address_accepts(text):
            conv["data"]["mailing_address"] = text.strip()[:800]
            conv["current_step"] = "summary"
            airtable_save_progressive(phone, conv)
            show_summary(phone, conv)
            return True
        lang = conv["data"].get("language", "fr")
        send_whatsapp_text(
            phone,
            "📮 Adresse trop courte ou incomplete.\n\n"
            "Collez l'adresse depuis Maps / Contacts, ou :\n"
            "Ligne 1 : n°, rue et ville\n"
            "Ligne 2 : code postal"
            if lang == "fr"
            else "📮 Address too short or incomplete.\n\n"
            "Paste from Maps/Contacts, or:\n"
            "Line 1: number, street and city\n"
            "Line 2: postcode",
        )
        return True

    return False


# ===== OPENAI =====


def call_openai_faq(user_message, lang="fr"):
    """Reponse UE261 sans collecte de dossier (pas d'historique persistant)."""
    if not OPENAI_API_KEY:
        return None
    hint = "Answer in English." if lang == "en" else "Reponds en francais."
    try:
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT_FAQ},
                    {"role": "system", "content": hint},
                    {"role": "user", "content": user_message.strip()},
                ],
                "temperature": 0.45,
                "max_tokens": 450,
            },
            timeout=45,
        )
        data = r.json()
        if "choices" in data:
            return data["choices"][0]["message"]["content"].strip()
        return None
    except Exception as e:
        print(f"OpenAI FAQ error: {e}")
        return None


def user_wants_start_dossier_from_faq(text):
    t = (text or "").strip().lower()
    if t == "1":
        return True
    return t in ("dossier", "mandat", "deposer", "commencer", "start", "claim file")


def call_openai(phone, user_message, image_data=None):
    try:
        conv = get_or_create_conversation(phone)
        if image_data:
            content = [
                {"type": "text", "text": user_message or "Carte d'embarquement"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_data}"}},
            ]
            conv["messages"].append({"role": "user", "content": content})
        else:
            conv["messages"].append({"role": "user", "content": user_message})

        if len(conv["messages"]) > 20:
            conv["messages"] = conv["messages"][-20:]

        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + conv["messages"]
        model = "gpt-4o" if image_data else "gpt-4o-mini"
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={"model": model, "messages": messages, "max_tokens": 400, "temperature": 0.7},
            timeout=45,
        )
        data = r.json()
        if "choices" in data:
            text = data["choices"][0]["message"]["content"].strip()
            conv["messages"].append({"role": "assistant", "content": text})
            return text
        return None
    except Exception as e:
        print(f"OpenAI error: {e}")
        return None


def detect_language(text):
    text_lower = text.lower()
    en_words = ["hello", "hi", "my", "flight", "delay", "yes", "no", "thanks"]
    fr_words = ["bonjour", "salut", "mon", "vol", "retard", "oui", "non", "merci"]
    en_count = sum(1 for w in en_words if w in text_lower.split())
    fr_count = sum(1 for w in fr_words if w in text_lower.split())
    return "en" if en_count > fr_count else "fr"


# ===== WEBHOOK =====


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.json
        if not data:
            return jsonify({"status": "no data"}), 200

        phone = data.get("waId") or data.get("from") or data.get("phone")
        if not phone:
            return jsonify({"status": "no phone"}), 200
        if data.get("owner") is True:
            return jsonify({"status": "ignored own"}), 200

        conv = get_or_create_conversation(phone)

        image_data = None
        message_text = ""
        message_type = data.get("type", "text")

        if message_type == "image" or "image" in data:
            media_url = data.get("data") or data.get("mediaUrl")
            if media_url:
                try:
                    r = requests.get(
                        media_url,
                        headers={"Authorization": f"Bearer {WATI_API_TOKEN}"},
                        timeout=30,
                    )
                    if r.status_code == 200:
                        image_data = base64.b64encode(r.content).decode("utf-8")
                except Exception:
                    pass
            message_text = data.get("caption", "") or "carte"
        else:
            if "text" in data:
                message_text = data["text"].get("body", "") if isinstance(data["text"], dict) else data["text"]
            elif "body" in data:
                message_text = data["body"]

        if not message_text and not image_data:
            return jsonify({"status": "ignored empty"}), 200

        sig = f"text|{message_text.strip().lower()}|img:{bool(image_data)}"
        flow_step = conv.get("current_step")
        if is_duplicate_event(phone, data, sig, flow_step):
            return jsonify({"status": "duplicate"}), 200

        print(f"[MSG] from={phone} step={conv.get('current_step')} text='{message_text[:50]}'")

        if message_text and not conv["data"].get("language_locked"):
            conv["data"]["language"] = detect_language(message_text)

        current_step = conv.get("current_step")

        if current_step == "faq_chat" and message_text:
            lang = conv["data"].get("language", "fr")
            low_menu = message_text.strip().lower()
            if low_menu in ("menu", "retour", "accueil", "0"):
                conv["current_step"] = "entry_intent"
                ask_entry_intent(phone, lang)
                return jsonify({"status": "menu from faq"}), 200
            if user_wants_start_dossier_from_faq(message_text):
                conv["data"] = fresh_dossier_data(lang)
                conv["ref_dossier"] = generate_ref_dossier(phone)
                conv["data"]["wa_id"] = phone
                conv["current_step"] = "confirm_phone"
                ask_confirm_phone(phone, conv)
                return jsonify({"status": "faq to dossier"}), 200
            ans = call_openai_faq(message_text, lang)
            if not ans:
                ans = (
                    "Je peux vous expliquer l'UE261 en general 😊\n\nPour un dossier concret : ecrivez dossier ou 1.\n\n👉 "
                    + RDA_SITE_URL
                    if lang == "fr"
                    else "I can explain EU261 in general 😊\n\nFor a real claim: type dossier or 1.\n\n👉 " + RDA_SITE_URL
                )
            send_whatsapp_text(phone, ans)
            return jsonify({"status": "faq"}), 200

        if image_data and current_step == "flight_number":
            response = call_openai(phone, "Extrait JSON: {flight_number, date, airline}", image_data)
            if response:
                try:
                    m = re.search(r"\{[^}]+\}", response)
                    if m:
                        ext = json.loads(m.group())
                        if ext.get("flight_number"):
                            conv["data"]["flight_number"] = ext["flight_number"]
                        if ext.get("date"):
                            conv["data"]["flight_date"] = ext["date"]
                        if ext.get("airline"):
                            conv["data"]["airline"] = ext["airline"]
                        send_whatsapp_text(
                            phone,
                            f"📸 Carte lue !\n✈️ {conv['data'].get('flight_number','?')}\n📅 {conv['data'].get('flight_date','?')}",
                        )
                        conv["current_step"] = "departure_airport"
                        airtable_save_progressive(phone, conv)
                        ask_departure_airport(phone, conv)
                        return jsonify({"status": "ok"}), 200
                except Exception:
                    pass

        if current_step and current_step in PROCESS_REPLY_STEPS:
            handled = process_reply(phone, message_text, conv)
            if handled:
                return jsonify({"status": "ok"}), 200
            lang = conv["data"].get("language", "fr")
            send_whatsapp_text(
                phone,
                "👆 Repondez avec le numero (ex : 1, 2, 3…) — ou **retour** pour la question precedente."
                if lang == "fr"
                else "👆 Reply with the number (e.g. 1, 2, 3…) — or **back** for the previous question.",
            )
            return jsonify({"status": "ok"}), 200

        trigger_words = [
            "vol",
            "retard",
            "annul",
            "indemn",
            "flight",
            "delay",
            "cancel",
            "compensation",
            "claim",
            "bonjour",
            "hello",
            "salut",
            "hi",
            "start",
            "commencer",
            "menu",
            "accueil",
            "aide",
            "help",
        ]
        is_trigger = any(w in message_text.lower() for w in trigger_words)

        if current_step is None or current_step == "completed":
            if is_trigger or len(message_text) < 50:
                conv["current_step"] = "entry_intent"
                conv["data"]["flow_mode"] = "dossier"
                ask_entry_intent(phone, conv["data"]["language"])
                return jsonify({"status": "menu"}), 200

        response = call_openai(phone, message_text, image_data)
        if not response:
            response = f"Bonjour ! 😊\n\nJe suis Robin des Airs.\n\nTapez 'menu' pour verifier votre vol 👇\n\n👉 {MANDAT_URL}"
        send_whatsapp_text(phone, response)
        return jsonify({"status": "ok"}), 200

    except Exception as e:
        print(f"Webhook error: {e}")
        import traceback

        traceback.print_exc()
        return jsonify({"status": "error"}), 500


@app.route("/test_flow/<phone>", methods=["GET"])
def test_flow(phone):
    conv = get_or_create_conversation(phone)
    conv["ref_dossier"] = generate_ref_dossier(phone)
    conv["data"]["language"] = "fr"
    conv["data"]["flow_mode"] = "dossier"
    conv["data"]["wa_id"] = phone
    conv["data"]["dossier_phone"] = normalize_dossier_phone(phone) or str(phone)
    conv["current_step"] = "passengers"
    ask_passengers(phone, "fr")
    return jsonify({"status": "flow passengers (tel pre-rempli test)", "phone": phone}), 200


@app.route("/conversations", methods=["GET"])
def list_conversations():
    result = {}
    for phone, conv in conversations.items():
        result[phone] = {
            "step": conv.get("current_step"),
            "ref": conv.get("ref_dossier"),
            "data": conv["data"],
        }
    return jsonify(result), 200


@app.route("/reset/<phone>", methods=["GET"])
def reset(phone):
    if phone in conversations:
        del conversations[phone]
    return jsonify({"status": "reset", "phone": phone}), 200


@app.route("/test", methods=["GET"])
def test():
    return jsonify(
        {
            "status": "running",
            "version": "v16 - retour / precedent + menu modifier apres recap",
            "domain": RDA_DOMAIN,
            "airtable": "OK" if AIRTABLE_API_KEY else "MISSING",
            "airtable_last_error": (last_airtable_error[:500] + "…")
            if last_airtable_error and len(last_airtable_error) > 500
            else last_airtable_error,
            "openai": "OK" if OPENAI_API_KEY else "MISSING",
            "wati": "OK" if WATI_API_TOKEN else "MISSING",
            "active_conversations": len(conversations),
            "airtable_field_hint": "AIRTABLE_FIELD_<CLE> ex: NOM, REF, MONTANT, REMARQUES, COMMISSION_RDA, STATUT_DOSSIER, IBAN... + AIRTABLE_DEFAULT_PARTNER_AGENCY, AIRTABLE_DEFAULT_FILE_STATUS, AIRTABLE_DEFAULT_AGENCY_COMMISSION",
        }
    ), 200


@app.route("/", methods=["GET"])
def home():
    return "Robin des Airs Bot v16", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
