from flask import Flask, request, jsonify
import requests
import os
import json
import base64
import re
import hashlib
from datetime import datetime, timedelta

app = Flask(__name__)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
WATI_API_TOKEN = os.environ.get("WATI_API_TOKEN", "")
WATI_BASE_URL = os.environ.get("WATI_BASE_URL", "")
AIRTABLE_API_KEY = os.environ.get("AIRTABLE_API_KEY", "")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID", "appv72lKbQtjt7EIP")
AIRTABLE_TABLE_NAME = os.environ.get("AIRTABLE_TABLE_NAME", "Dossiers Passagers")

# ===== IDs EXACTS DES CHAMPS AIRTABLE (tblfg688AGxaywi7O) =====
AIRTABLE_TABLE_ID   = "tblfg688AGxaywi7O"
F_NOM_PASSAGER      = "fldCtJysGhTYF2LNf"
F_REF_DOSSIER       = "flduSWqrqxeNoQkKW"
F_DATE_DOSSIER      = "fldU8r9ME43CeOZ1T"
F_MONTANT_CLIENT    = "fldloBwQlvX9I3dyu"
F_COMMISSION_RDA    = "fld576OhR8Bi0AV1s"
F_COMMISSION_AGENCE = "fldRjsE00F1C8RExp"
F_STATUT_DOSSIER    = "fldw5cnmqhMjO2cSc"
F_REMARQUES         = "fldqks5asIPXar8BD"
F_WHATSAPP          = "fldsFH0PoWe3AV0sI"
F_COMPAGNIE         = "fld8Ku1jGMOPWnrQc"
F_NUMERO_VOL        = "fldcVnS4B86eZntjr"
F_DATE_VOL          = "flduDNEC3osPnTMAv"
F_ITINERAIRE        = "fldtCISegQZ58Yvrl"
F_PNR               = "fld7scWE20q3DRPUa"
F_TYPE_INCIDENT     = "fldci5VnHb0HpOoKL"
F_MONTANT_INDEMNITE = "fldlzkJOqqC8AYbIM"
F_STATUT_SUIVI      = "fldUnBUQFKeoKf8LL"

# Options singleSelect exactes dans Airtable
INCIDENT_AIRTABLE = {
    "delay":  "Retard +3h",
    "cancel": "Annulation",
    "denied": "Surbooking",
}
STATUT_DOSSIER_DEFAUT = "Ouvert"
STATUT_SUIVI_DEFAUT   = "Nouveau"


RDA_DOMAIN = os.environ.get("RDA_SITE", os.environ.get("RDA_DOMAIN", "https://robindesairs.eu"))
# Mandat HTML integre (copie public/mandat.html du site, compresse) — un seul fichier a deployer.
_MANDAT_ZLIB_B64 = (
    "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"
    "szbFPrLS4MmKPqn8ZAW3B/+OkrG39eD/AURaNcc="
)


def _decode_mandat_html():
    import zlib as _z, base64 as _b
    return _z.decompress(_b.b64decode("".join(_MANDAT_ZLIB_B64))).decode("utf-8")


_MANDAT_HTML_BODY = _decode_mandat_html()
MANDAT_URL = os.environ.get("MANDAT_URL", f"{RDA_DOMAIN.rstrip('/')}/mandat.html")
DEPOT_URL = f"{RDA_DOMAIN.rstrip('/')}/depot-express"
SUIVI_URL = f"{RDA_DOMAIN.rstrip('/')}/suivi-dossier"
BLOG_URL = f"{RDA_DOMAIN.rstrip('/')}/blog"

# Memoire conversations
conversations = {}
recent_event_ids = {}
MEMORY_HOURS = 24

EU261_BANDS = {
    "band_250": {"amount_eur": 250, "label": "≤ 1500 km"},
    "band_400": {"amount_eur": 400, "label": "1500–3500 km"},
    "band_600": {"amount_eur": 600, "label": "> 3500 km"},
    "band_unknown": {"amount_eur": None, "label": "Distance inconnue"},
}

STEPS = ["passengers", "incident_type", "flight_type", "airline", "airline_other_input",
         "flight_number", "flight_date", "flight_month", "flight_day_input",
         "distance_band", "passenger_names", "minor_check", "minors_count", "summary"]

AIRLINES_MAP = {
    "1": "Air France", "2": "KLM", "3": "Brussels Airlines",
    "4": "Lufthansa", "5": "TAP Portugal", "6": "Corsair",
    "7": "Air Senegal", "8": "Royal Air Maroc",
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


# ===== REFERENCE DOSSIER =====

def generate_ref_dossier(phone):
    """Genere une reference unique : RDA-YYYYMMDD-XXXX"""
    today = datetime.now().strftime("%Y%m%d")
    suffix = hashlib.md5(f"{phone}{today}".encode()).hexdigest()[:4].upper()
    return f"RDA-{today}-{suffix}"


def _mandat_wa_display(phone):
    p = str(phone or "").strip().replace(" ", "")
    if not p:
        return ""
    if p.startswith("+"):
        return p
    if p.startswith("00"):
        return "+" + p[2:]
    if p.isdigit():
        return "+" + p
    return p


def _mandat_fn_ln_from_names(names):
    if not names:
        return "", ""
    first = (names[0] or "").strip()
    parts = first.split()
    if len(parts) >= 2:
        return parts[0], " ".join(parts[1:]).upper()
    return "", ""


# ===== AIRTABLE =====

def airtable_headers():
    return {
        "Authorization": f"Bearer {AIRTABLE_API_KEY}",
        "Content-Type": "application/json"
    }


def airtable_url():
    return f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_ID}"


def airtable_find_records_by_ref(ref):
    """Cherche les records existants par Référence Dossier (champ F_REF_DOSSIER)"""
    if not AIRTABLE_API_KEY or not ref:
        return []
    try:
        ref_esc = ref.replace("'", "''")
        formula = f"{{{F_REF_DOSSIER}}}='{ref_esc}'"
        url = f"{airtable_url()}?filterByFormula={requests.utils.quote(formula)}"
        r = requests.get(url, headers=airtable_headers(), timeout=10)
        if r.status_code == 200:
            return r.json().get("records", [])
        print(f"Airtable find error {r.status_code}: {r.text[:300]}")
    except Exception as e:
        print(f"Airtable find error: {e}")
    return []


def airtable_save_progressive(phone, conv):
    """
    Sauvegarde progressive avec les vrais IDs de champs Airtable.
    Crée N records (1 par passager) dès la 1ère étape,
    puis PATCH à chaque nouvelle info disponible.
    """
    if not AIRTABLE_API_KEY:
        print("Airtable: AIRTABLE_API_KEY manquante")
        return

    try:
        d   = conv.get("data", {})
        ref = conv.get("ref_dossier")
        if not ref:
            ref = generate_ref_dossier(phone)
            conv["ref_dossier"] = ref

        pax   = d.get("passengers") or 1
        names = d.get("passenger_names") or []

        # Numéro de contact dossier
        contact = str(phone)

        # Calcul montant (défaut 600€ long-courrier Europe-Afrique)
        band_id = d.get("distance_band", "band_600")
        per_pax = EU261_BANDS.get(band_id, EU261_BANDS["band_600"]).get("amount_eur") or 600
        brut    = per_pax * pax
        net     = round(brut * 0.75)
        com_rda = round(brut * 0.25)

        # Itinéraire
        dep = (d.get("departure_airport") or "").strip()
        arr = (d.get("arrival_airport") or "").strip()
        itineraire = f"{dep} → {arr}" if dep and arr else ""

        # Date vol → YYYY-MM-DD
        date_vol_raw = (d.get("flight_date") or "").strip()
        date_vol_at  = None
        if date_vol_raw:
            parts = date_vol_raw.split("/")
            if len(parts) == 3:
                date_vol_at = f"{parts[2]}-{parts[1]}-{parts[0]}"

        # Incident → option singleSelect exacte
        incident_at = INCIDENT_AIRTABLE.get(d.get("incident_type") or "", "")

        # Champs communs
        common = {
            F_REF_DOSSIER:    ref,
            F_DATE_DOSSIER:   datetime.now().strftime("%Y-%m-%d"),
            F_WHATSAPP:       contact,
            F_STATUT_DOSSIER: STATUT_DOSSIER_DEFAUT,
            F_STATUT_SUIVI:   STATUT_SUIVI_DEFAUT,
        }
        if d.get("airline"):
            common[F_COMPAGNIE] = d["airline"]
        if d.get("flight_number"):
            common[F_NUMERO_VOL] = d["flight_number"]
        if date_vol_at:
            common[F_DATE_VOL] = date_vol_at
        if itineraire:
            common[F_ITINERAIRE] = itineraire
        if d.get("pnr"):
            common[F_PNR] = d["pnr"].strip()
        if incident_at:
            common[F_TYPE_INCIDENT] = incident_at

        existing = airtable_find_records_by_ref(ref)

        if not existing:
            # CRÉATION : 1 ligne par passager
            records_to_create = []
            for i in range(pax):
                fields = dict(common)
                fields[F_NOM_PASSAGER] = names[i] if i < len(names) else f"Passager {i+1}"
                fields[F_REMARQUES]    = f"Ref: {ref} | Passager {i+1}/{pax} | Bot WhatsApp"
                if i == 0:
                    fields[F_MONTANT_CLIENT]    = float(net)
                    fields[F_COMMISSION_RDA]    = float(com_rda)
                    fields[F_MONTANT_INDEMNITE] = float(brut)
                else:
                    fields[F_MONTANT_CLIENT]    = 0.0
                    fields[F_COMMISSION_RDA]    = 0.0
                    fields[F_MONTANT_INDEMNITE] = 0.0
                records_to_create.append({"fields": fields})

            r = requests.post(airtable_url(), headers=airtable_headers(),
                              json={"records": records_to_create}, timeout=15)
            if r.status_code in (200, 201):
                print(f"✅ Airtable CREATE {pax} records OK (ref={ref})")
            else:
                print(f"❌ Airtable CREATE {r.status_code}: {r.text[:500]}")

        else:
            # UPDATE : patch les records existants
            updates = []
            for i, rec in enumerate(existing[:pax]):
                fields = dict(common)
                fields[F_NOM_PASSAGER] = names[i] if i < len(names) else f"Passager {i+1}"
                if i == 0:
                    fields[F_MONTANT_CLIENT]    = float(net)
                    fields[F_COMMISSION_RDA]    = float(com_rda)
                    fields[F_MONTANT_INDEMNITE] = float(brut)
                updates.append({"id": rec["id"], "fields": fields})

            if updates:
                r = requests.patch(airtable_url(), headers=airtable_headers(),
                                   json={"records": updates}, timeout=15)
                if r.status_code == 200:
                    print(f"✅ Airtable UPDATE {len(updates)} records OK (ref={ref})")
                else:
                    print(f"❌ Airtable PATCH {r.status_code}: {r.text[:500]}")

    except Exception as e:
        print(f"❌ Airtable exception: {e}")
        import traceback
        traceback.print_exc()


# ===== CONVERSATIONS =====

def get_or_create_conversation(phone):
    if phone not in conversations:
        conversations[phone] = {
            "messages": [],
            "current_step": None,
            "ref_dossier": None,
            "data": {
                "passengers": None, "incident_type": None, "flight_type": None,
                "airline": None, "flight_number": None, "flight_date": None,
                "distance_band": None, "passenger_names": [], "has_minors": None,
                "minors_count": 0, "language": "fr",
                "temp_year": None, "temp_month": None, "temp_years": [],
            },
            "created": datetime.now(),
        }
    if (datetime.now() - conversations[phone]["created"]) > timedelta(hours=MEMORY_HOURS):
        del conversations[phone]
        return get_or_create_conversation(phone)
    return conversations[phone]


# ===== DEDUP =====

def is_duplicate_event(phone, data, sig):
    now = datetime.now()
    # cleanup
    to_del = [k for k, ts in recent_event_ids.items() if (now - ts).total_seconds() > 900]
    for k in to_del:
        recent_event_ids.pop(k, None)
    
    event_id = data.get("messageId") or data.get("id") or data.get("whatsappMessageId")
    if event_id:
        if event_id in recent_event_ids:
            return True
        recent_event_ids[event_id] = now
    
    key = hashlib.sha256(f"{phone}|{sig}".encode()).hexdigest()
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

def ask_passengers(phone, lang="fr"):
    if lang == "en":
        msg = (
            "👋 Hello! Welcome to Robin des Airs ✈️\n\n"
            "Let's check your eligibility in 2 min ⏱️\n\n"
            "👥 How many passengers were on the flight?\n\n"
            "1️⃣ 1 passenger\n2️⃣ 2 passengers\n3️⃣ 3 passengers\n"
            "4️⃣ 4 passengers\n5️⃣ 5 passengers\n6️⃣ 6 or more — Climbie calls you\n\n"
            "Reply with the number (1-6)"
        )
    else:
        msg = (
            "👋 Bonjour ! Bienvenue chez Robin des Airs ✈️\n\n"
            "Verifions votre eligibilite en 2 min ⏱️\n\n"
            "👥 Combien de passagers sur le vol ?\n\n"
            "1️⃣ 1 passager\n2️⃣ 2 passagers\n3️⃣ 3 passagers\n"
            "4️⃣ 4 passagers\n5️⃣ 5 passagers\n6️⃣ 6 ou plus — Climbie vous appelle\n\n"
            "Repondez avec le numero (1-6)"
        )
    send_whatsapp_text(phone, msg)


def ask_incident_type(phone, conv):
    lang = conv["data"]["language"]
    pax = conv["data"]["passengers"] or 1
    if lang == "en":
        msg = (
            f"Great! 🎉 {pax} passenger(s) noted.\n\n"
            "✈️ What happened with your flight?\n\n"
            "1️⃣ Delay over 3 hours\n2️⃣ Flight cancelled\n3️⃣ Denied boarding\n\n"
            "Reply with 1, 2 or 3"
        )
    else:
        msg = (
            f"Genial ! 🎉 {pax} passager(s) note(s).\n\n"
            "✈️ Que s'est-il passe avec votre vol ?\n\n"
            "1️⃣ Retard +3 heures\n2️⃣ Vol annule\n3️⃣ Refus d'embarquement\n\n"
            "Repondez avec 1, 2 ou 3"
        )
    send_whatsapp_text(phone, msg)


def ask_flight_type(phone, conv):
    lang = conv["data"]["language"]
    if lang == "en":
        msg = "✈️ Was your flight:\n\n1️⃣ Direct flight\n2️⃣ With connection(s)\n\nReply with 1 or 2"
    else:
        msg = "✈️ Votre vol etait :\n\n1️⃣ Vol direct\n2️⃣ Avec correspondance(s)\n\nRepondez avec 1 ou 2"
    send_whatsapp_text(phone, msg)


def ask_airline(phone, conv):
    lang = conv["data"]["language"]
    if lang == "en":
        msg = (
            "🛫 Which airline?\n\n"
            "1️⃣ Air France\n2️⃣ KLM\n3️⃣ Brussels Airlines\n"
            "4️⃣ Lufthansa\n5️⃣ TAP Portugal\n6️⃣ Corsair\n"
            "7️⃣ Air Senegal\n8️⃣ Royal Air Maroc\n9️⃣ Other (type the name)\n\n"
            "Reply with 1-9 OR type the airline name directly"
        )
    else:
        msg = (
            "🛫 Quelle compagnie aerienne ?\n\n"
            "1️⃣ Air France\n2️⃣ KLM\n3️⃣ Brussels Airlines\n"
            "4️⃣ Lufthansa\n5️⃣ TAP Portugal\n6️⃣ Corsair\n"
            "7️⃣ Air Senegal\n8️⃣ Royal Air Maroc\n9️⃣ Autre (tapez le nom)\n\n"
            "Repondez avec 1-9 OU tapez directement le nom"
        )
    send_whatsapp_text(phone, msg)


def ask_flight_number(phone, conv):
    lang = conv["data"]["language"]
    airline = conv["data"]["airline"] or ""
    if lang == "en":
        msg = f"📝 {airline} ✅\n\nWhat is your flight number?\n\nExample: AF718, KL563, SN271\n\nOr send a photo of your boarding pass 📸"
    else:
        msg = f"📝 {airline} ✅\n\nQuel est votre numero de vol ?\n\nExemple : AF718, KL563, SN271\n\nOu envoyez une photo de votre carte d'embarquement 📸"
    send_whatsapp_text(phone, msg)


def ask_flight_date(phone, conv):
    lang = conv["data"]["language"]
    cy = datetime.now().year
    conv["data"]["temp_years"] = [cy, cy-1, cy-2, cy-3, cy-4]
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
        msg = "📅 Which month?\n\n1=Jan 2=Feb 3=Mar 4=Apr 5=May 6=Jun\n7=Jul 8=Aug 9=Sep 10=Oct 11=Nov 12=Dec\n\nReply with the number (1-12)"
    else:
        msg = "📅 Quel mois ?\n\n1=Jan 2=Fev 3=Mars 4=Avr 5=Mai 6=Juin\n7=Juil 8=Aout 9=Sept 10=Oct 11=Nov 12=Dec\n\nRepondez avec le numero (1-12)"
    send_whatsapp_text(phone, msg)


def ask_flight_day(phone, conv):
    lang = conv["data"]["language"]
    msg = "📅 Tapez le jour exact (1-31) :" if lang == "fr" else "📅 Type the exact day (1-31):"
    send_whatsapp_text(phone, msg)


def ask_distance_band(phone, conv):
    lang = conv["data"]["language"]
    if lang == "en":
        msg = (
            "📏 Distance of the flight?\n\n"
            "1️⃣ ≤ 1500 km — 250 EUR/passenger\n"
            "2️⃣ 1500–3500 km — 400 EUR/passenger\n"
            "3️⃣ > 3500 km (Europe-Africa) — 600 EUR/passenger\n"
            "4️⃣ I don't know\n\nReply with 1, 2, 3 or 4"
        )
    else:
        msg = (
            "📏 Distance du vol ?\n\n"
            "1️⃣ ≤ 1500 km — 250 EUR/passager\n"
            "2️⃣ 1500–3500 km — 400 EUR/passager\n"
            "3️⃣ > 3500 km (Europe-Afrique) — 600 EUR/passager\n"
            "4️⃣ Je ne sais pas\n\nRepondez avec 1, 2, 3 ou 4"
        )
    send_whatsapp_text(phone, msg)


def ask_passenger_names(phone, conv):
    lang = conv["data"]["language"]
    pax = conv["data"]["passengers"] or 1
    if lang == "en":
        msg = f"👤 Names of the {pax} passenger(s) please.\n\nSend like this:\n1. John Doe\n2. Jane Doe\n\n(First + Last name each)"
    else:
        msg = f"👤 Noms des {pax} passager(s) svp.\n\nEnvoyez comme ca :\n1. Jean Dupont\n2. Marie Dupont\n\n(Prenom + Nom pour chacun)"
    send_whatsapp_text(phone, msg)


def ask_minors(phone, conv):
    lang = conv["data"]["language"]
    pax = conv["data"]["passengers"] or 1
    if pax == 1:
        msg = "👤 Etes-vous majeur(e) (18+ ans) ?\n\n1️⃣ Oui, majeur\n2️⃣ Non, mineur\n\nRepondez avec 1 ou 2" if lang == "fr" else "👤 Are you over 18?\n\n1️⃣ Yes, adult\n2️⃣ No, minor\n\nReply with 1 or 2"
    else:
        msg = f"👶 Parmi les {pax} passagers, des mineurs (moins 18 ans) ?\n\n1️⃣ Non, tous majeurs\n2️⃣ Oui, il y a des mineurs\n\nRepondez avec 1 ou 2" if lang == "fr" else f"👶 Among {pax} passengers, any minors?\n\n1️⃣ No, all adults\n2️⃣ Yes, there are minors\n\nReply with 1 or 2"
    send_whatsapp_text(phone, msg)


def ask_minors_count(phone, conv):
    lang = conv["data"]["language"]
    pax = conv["data"]["passengers"] or 1
    opts = "\n".join([f"{i}️⃣ {i} mineur{'s' if i > 1 else ''}" for i in range(1, min(pax, 5)+1)])
    msg = f"👶 Combien de mineurs parmi les {pax} passagers ?\n\n{opts}\n\nRepondez avec le numero" if lang == "fr" else f"👶 How many minors among {pax}?\n\n{opts}\n\nReply with the number"
    send_whatsapp_text(phone, msg)


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

    fn0, ln0 = _mandat_fn_ln_from_names(d.get("passenger_names") or [])
    wa_disp = _mandat_wa_display(phone)

    params_dict = {
        "ref": ref, "pax": pax, "vol": d.get("flight_number", ""),
        "date": d.get("flight_date", ""), "compagnie": d.get("airline", ""),
        "incident": d.get("incident_type", ""), "type_vol": d.get("flight_type", ""),
        "distance": band_id, "noms": ",".join(d.get("passenger_names", [])),
        "mineurs": d.get("minors_count", 0), "source": "whatsapp_bot",
        "fn": fn0, "ln": ln0, "wa": wa_disp,
    }
    query = "&".join([f"{k}={requests.utils.quote(str(v))}" for k, v in params_dict.items() if v])
    mandat_link = f"{MANDAT_URL}?{query}"

    if per_pax:
        money = f"💶 {per_pax} EUR/passager ({band_label})\n💰 TOTAL : {total} EUR\n✅ NET POUR VOUS : {net} EUR"
    else:
        money = "💶 Montant a confirmer selon distance"

    if lang == "en":
        body = (
            f"🎉 PERFECT!\n\n"
            f"📋 File ref: {ref}\n"
            f"✈️ Flight: {d.get('flight_number','?')} ({d.get('airline','?')})\n"
            f"📅 Date: {d.get('flight_date','?')}\n"
            f"👥 Passengers: {pax}\n{names_str}\n"
            f"👶 Minors: {d.get('minors_count',0)}\n"
            f"⚠️ Incident: {incident}\n📏 Distance: {band_label}\n\n{money}\n\n"
            f"👇 Sign your mandate (3 min):\n{mandat_link}"
        )
    else:
        body = (
            f"🎉 PARFAIT !\n\n"
            f"📋 Ref dossier : {ref}\n"
            f"✈️ Vol : {d.get('flight_number','?')} ({d.get('airline','?')})\n"
            f"📅 Date : {d.get('flight_date','?')}\n"
            f"👥 Passagers : {pax}\n{names_str}\n"
            f"👶 Mineurs : {d.get('minors_count',0)}\n"
            f"⚠️ Incident : {incident}\n📏 Distance : {band_label}\n\n{money}\n\n"
            f"👇 Signez votre mandat (3 min) :\n{mandat_link}"
        )
    send_whatsapp_text(phone, body)
    
    # Sauvegarde finale Airtable
    airtable_save_progressive(phone, conv)
    conv["current_step"] = "completed"


# ===== TRAITEMENT REPONSES =====

def process_reply(phone, text, conv):
    """Traite la reponse du client a chaque etape"""
    step = conv.get("current_step")
    text = text.strip()
    num_match = re.search(r"^(\d+)", text)
    choice = num_match.group(1) if num_match else None

    print(f"[REPLY] step={step} text='{text[:30]}' choice={choice}")

    # ===== PASSAGERS =====
    if step == "passengers":
        if choice in ["1","2","3","4","5"]:
            conv["data"]["passengers"] = int(choice)
            conv["current_step"] = "incident_type"
            airtable_save_progressive(phone, conv)
            ask_incident_type(phone, conv)
            return True
        elif choice == "6":
            send_whatsapp_text(phone, f"🙏 Pour 6+ passagers, Climbie vous appelle.\n\n📱 +33 7 56 86 36 30\n\n👉 {DEPOT_URL}")
            return True
        return False

    # ===== INCIDENT =====
    if step == "incident_type":
        mapping = {"1": "delay", "2": "cancel", "3": "denied"}
        if choice in mapping:
            conv["data"]["incident_type"] = mapping[choice]
            conv["current_step"] = "flight_type"
            airtable_save_progressive(phone, conv)
            ask_flight_type(phone, conv)
            return True
        return False

    # ===== TYPE VOL =====
    if step == "flight_type":
        if choice == "1":
            conv["data"]["flight_type"] = "direct"
        elif choice == "2":
            conv["data"]["flight_type"] = "connection"
        else:
            return False
        conv["current_step"] = "airline"
        ask_airline(phone, conv)
        return True

    # ===== COMPAGNIE — TRES IMPORTANT =====
    if step == "airline":
        # Cas 1 : choix numerique 1-8
        if choice and choice in AIRLINES_MAP:
            conv["data"]["airline"] = AIRLINES_MAP[choice]
            conv["current_step"] = "flight_number"
            airtable_save_progressive(phone, conv)
            ask_flight_number(phone, conv)
            return True
        # Cas 2 : choix 9 = Autre
        if choice == "9":
            lang = conv["data"]["language"]
            send_whatsapp_text(phone, "✍️ Tapez le nom de votre compagnie :" if lang == "fr" else "✍️ Type your airline name:")
            conv["current_step"] = "airline_other_input"
            return True
        # Cas 3 : le client a tape directement le nom (pas un chiffre)
        if not choice and len(text) >= 3:
            conv["data"]["airline"] = text
            conv["current_step"] = "flight_number"
            airtable_save_progressive(phone, conv)
            ask_flight_number(phone, conv)
            return True
        return False

    # ===== AIRLINE OTHER INPUT =====
    if step == "airline_other_input":
        conv["data"]["airline"] = text
        conv["current_step"] = "flight_number"
        airtable_save_progressive(phone, conv)
        ask_flight_number(phone, conv)
        return True

    # ===== NUMERO DE VOL =====
    if step == "flight_number":
        m = re.search(r"\b([A-Z]{2}\d{2,4})\b", text.upper())
        conv["data"]["flight_number"] = m.group(1) if m else text
        conv["current_step"] = "flight_date"
        airtable_save_progressive(phone, conv)
        ask_flight_date(phone, conv)
        return True

    # ===== ANNEE =====
    if step == "flight_date":
        years = conv["data"].get("temp_years", [])
        if choice == "6":
            send_whatsapp_text(phone, f"😔 Retroactivite 5 ans max.\nVotre vol est trop ancien.\n\n👉 {BLOG_URL}")
            return True
        idx = int(choice) - 1 if choice and choice.isdigit() else -1
        if 0 <= idx < len(years):
            conv["data"]["temp_year"] = str(years[idx])
            conv["current_step"] = "flight_month"
            ask_flight_month(phone, conv)
            return True
        return False

    # ===== MOIS =====
    if step == "flight_month":
        if choice and choice.isdigit() and 1 <= int(choice) <= 12:
            conv["data"]["temp_month"] = f"{int(choice):02d}"
            conv["current_step"] = "flight_day_input"
            ask_flight_day(phone, conv)
            return True
        return False

    # ===== JOUR =====
    if step == "flight_day_input":
        if choice and choice.isdigit() and 1 <= int(choice) <= 31:
            day = f"{int(choice):02d}"
            year = conv["data"].get("temp_year", "")
            month = conv["data"].get("temp_month", "")
            conv["data"]["flight_date"] = f"{day}/{month}/{year}"
            conv["current_step"] = "distance_band"
            airtable_save_progressive(phone, conv)
            ask_distance_band(phone, conv)
            return True
        return False

    # ===== DISTANCE =====
    if step == "distance_band":
        band_map = {"1": "band_250", "2": "band_400", "3": "band_600", "4": "band_unknown"}
        if choice in band_map:
            conv["data"]["distance_band"] = band_map[choice]
            conv["current_step"] = "passenger_names"
            airtable_save_progressive(phone, conv)
            ask_passenger_names(phone, conv)
            return True
        return False

    # ===== NOMS PASSAGERS =====
    if step == "passenger_names":
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        names = [re.sub(r"^[\d\.\)\-\s]+", "", l).strip() for l in lines]
        names = [n for n in names if len(n) >= 3 and not n.isdigit()]
        if names:
            conv["data"]["passenger_names"] = names
            conv["current_step"] = "minor_check"
            airtable_save_progressive(phone, conv)
            ask_minors(phone, conv)
            return True
        else:
            lang = conv["data"]["language"]
            send_whatsapp_text(phone, "👤 Format :\n1. Jean Dupont\n2. Marie Dupont")
            return True

    # ===== MINEURS =====
    if step == "minor_check":
        if choice == "1":
            conv["data"]["has_minors"] = False
            conv["data"]["minors_count"] = 0
            conv["current_step"] = "summary"
            show_summary(phone, conv)
            return True
        elif choice == "2":
            pax = conv["data"].get("passengers") or 1
            if pax == 1:
                send_whatsapp_text(phone, "👶 Mineur seul : un parent doit signer.\n\n📱 Climbie : +33 7 56 86 36 30")
                return True
            conv["data"]["has_minors"] = True
            conv["current_step"] = "minors_count"
            ask_minors_count(phone, conv)
            return True
        return False

    # ===== NOMBRE MINEURS =====
    if step == "minors_count":
        pax = conv["data"].get("passengers") or 1
        if choice and choice.isdigit() and 1 <= int(choice) <= pax:
            conv["data"]["minors_count"] = int(choice)
            conv["current_step"] = "summary"
            show_summary(phone, conv)
            return True
        return False

    return False


# ===== OPENAI =====

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

        # Image ou texte
        image_data = None
        message_text = ""
        message_type = data.get("type", "text")

        if message_type == "image" or "image" in data:
            media_url = data.get("data") or data.get("mediaUrl")
            if media_url:
                try:
                    r = requests.get(media_url, headers={"Authorization": f"Bearer {WATI_API_TOKEN}"}, timeout=30)
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
        if is_duplicate_event(phone, data, sig):
            return jsonify({"status": "duplicate"}), 200

        print(f"[MSG] from={phone} step={conv.get('current_step')} text='{message_text[:50]}'")

        # Detecte langue
        if message_text and not conv["data"].get("language_locked"):
            conv["data"]["language"] = detect_language(message_text)

        current_step = conv.get("current_step")

        # Image carte d'embarquement
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
                        send_whatsapp_text(phone, f"📸 Carte lue !\n✈️ {conv['data'].get('flight_number','?')}\n📅 {conv['data'].get('flight_date','?')}")
                        conv["current_step"] = "distance_band"
                        airtable_save_progressive(phone, conv)
                        ask_distance_band(phone, conv)
                        return jsonify({"status": "ok"}), 200
                except Exception:
                    pass

        # FLUX EN COURS — toujours traiter ici en priorite
        if current_step and current_step in STEPS:
            handled = process_reply(phone, message_text, conv)
            if handled:
                return jsonify({"status": "ok"}), 200
            else:
                # Reponse non reconnue
                lang = conv["data"].get("language", "fr")
                send_whatsapp_text(phone, "👆 Repondez avec le numero correspondant (ex: 1, 2, 3...)" if lang == "fr" else "👆 Reply with the number (e.g. 1, 2, 3...)")
                return jsonify({"status": "ok"}), 200

        # Demarrage flux
        trigger_words = ["vol", "retard", "annul", "indemn", "flight", "delay", "cancel",
                        "compensation", "claim", "bonjour", "hello", "salut", "hi",
                        "start", "commencer", "menu", "aide", "help"]
        is_trigger = any(w in message_text.lower() for w in trigger_words)

        if current_step is None or current_step == "completed":
            if is_trigger or len(message_text) < 50:
                conv["current_step"] = "passengers"
                conv["ref_dossier"] = generate_ref_dossier(phone)
                ask_passengers(phone, conv["data"]["language"])
                return jsonify({"status": "flow started"}), 200

        # Reponse libre OpenAI
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
    conv["current_step"] = "passengers"
    conv["data"]["language"] = "fr"
    conv["ref_dossier"] = generate_ref_dossier(phone)
    ask_passengers(phone, "fr")
    return jsonify({"status": "flow started", "phone": phone}), 200


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
    return jsonify({
        "status": "running",
        "version": "v8 - mandat integre (1 fichier) Airtable + ref dossier + bug airline fixe",
        "domain": RDA_DOMAIN,
        "airtable": "OK" if AIRTABLE_API_KEY else "MISSING",
        "openai": "OK" if OPENAI_API_KEY else "MISSING",
        "wati": "OK" if WATI_API_TOKEN else "MISSING",
        "active_conversations": len(conversations),
    }), 200


@app.route("/", methods=["GET"])
def home():
    return "Robin des Airs Bot v8 + mandat integre", 200


@app.route("/mandat.html", methods=["GET"])
def serve_mandat_html():
    """Sert le mandat embarque (mandat_html.py). Meme contenu que public/mandat.html sur le site."""
    if not _MANDAT_HTML_BODY:
        return (
            "<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>Mandat</title></head><body>"
            "<p>Ajoutez <code>mandat_html.py</code> a cote de <code>main.py</code> (copie GitHub).</p>"
            "<p>Public : <a href=\"https://robindesairs.eu/mandat.html\">robindesairs.eu/mandat.html</a></p>"
            "</body></html>",
            503,
            {"Content-Type": "text/html; charset=utf-8"},
        )
    return _MANDAT_HTML_BODY, 200, {"Content-Type": "text/html; charset=utf-8"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
