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

RDA_DOMAIN = "https://robindesairs.eu"
MANDAT_URL = f"{RDA_DOMAIN}/mandat-representation"
DEPOT_URL = f"{RDA_DOMAIN}/depot-express"
SUIVI_URL = f"{RDA_DOMAIN}/suivi-dossier"
BLOG_URL = f"{RDA_DOMAIN}/blog"
# Formulaire dossier en ligne (Airtable/Notion/site) — option menu 2
RDA_ONLINE_DOSSIER_URL = os.environ.get("RDA_ONLINE_DOSSIER_URL", DEPOT_URL)

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


STEPS = [
    "entry_intent",
    "passengers",
    "incident_type",
    "cancel_notice_period",
    "flight_type",
    "airline",
    "airline_other_input",
    "flight_number",
    "flight_date",
    "flight_month",
    "flight_day_input",
    "departure_airport",
    "arrival_airport",
    "passenger_names",
    "minor_check",
    "minors_count",
    "mailing_address",
    "summary",
]

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
- Finir par un lien utile parmi : {MANDAT_URL} | {DEPOT_URL} | {BLOG_URL}

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
    """Cherche les records existants par reference dossier"""
    if not AIRTABLE_API_KEY or not ref:
        return []
    try:
        formula = f"OR(FIND('{ref}', {{Remarques}}), {{Reference Dossier}}='{ref}')"
        url = f"{airtable_url()}?filterByFormula={requests.utils.quote(formula)}"
        r = requests.get(url, headers=airtable_headers(), timeout=10)
        if r.status_code == 200:
            return r.json().get("records", [])
    except Exception as e:
        print(f"Airtable find error: {e}")
    return []


def airtable_save_progressive(phone, conv):
    """
    Sauvegarde progressive : cree ou met a jour les records dans Airtable.
    Cree 1 ligne par passager des qu'on connait pax.
    Met a jour les champs au fur et a mesure de leur saisie.
    """
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

        base_fields = {
            "WhatsApp": str(phone),
            "Reference Dossier": ref,
            "Date Dossier": datetime.now().strftime("%Y-%m-%d"),
        }

        if d.get("flight_number"):
            base_fields["Vol"] = d["flight_number"]
        if d.get("airline"):
            base_fields["Compagnie"] = d["airline"]
        if d.get("flight_date"):
            base_fields["Date"] = d["flight_date"]
        if d.get("incident_type"):
            base_fields["Incident"] = INCIDENT_LABELS.get(d["incident_type"], d["incident_type"])

        dep = d.get("departure_airport")
        arr = d.get("arrival_airport")
        km = d.get("route_distance_km")
        route_suffix = ""
        if dep or arr:
            bits = []
            if dep:
                bits.append(f"Depart {dep}")
            if arr:
                bits.append(f"Arr {arr}")
            if km:
                bits.append(f"~{int(km)} km")
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

        existing = airtable_find_records_by_ref(ref)

        if not existing:
            records_to_create = []
            for i in range(pax):
                fields = dict(base_fields)
                if i < len(names):
                    fields["Nom"] = names[i]
                else:
                    fields["Nom"] = f"Passager {i+1}"
                fields["Remarques"] = f"Ref: {ref} | Passager {i+1}/{pax}{route_suffix}{addr_suffix}"
                if i == 0 and total_net:
                    fields["Montant"] = float(total_net)
                else:
                    fields["Montant"] = 0.0
                records_to_create.append({"fields": fields})

            payload = {"records": records_to_create, "typecast": True}
            r = requests.post(airtable_url(), headers=airtable_headers(), json=payload, timeout=15)
            print(f"Airtable CREATE {pax} records: {r.status_code} - {r.text[:200]}")

        else:
            updates = []
            for i, rec in enumerate(existing[:pax]):
                fields = dict(base_fields)
                if i < len(names):
                    fields["Nom"] = names[i]
                if i == 0 and total_net:
                    fields["Montant"] = float(total_net)
                if route_suffix or addr_suffix:
                    prev = rec.get("fields", {}).get("Remarques") or ""
                    merged = prev
                    if route_suffix and route_suffix.strip(" |") not in prev.replace(" · ", " "):
                        merged = (merged + route_suffix) if merged else f"Ref: {ref}{route_suffix}"
                    if addr_suffix and "Adr:" not in merged:
                        merged = (merged + addr_suffix) if merged else f"Ref: {ref}{addr_suffix}"
                    if merged != prev:
                        fields["Remarques"] = merged[:8000]
                updates.append({"id": rec["id"], "fields": fields})

            if updates:
                payload = {"records": updates, "typecast": True}
                r = requests.patch(airtable_url(), headers=airtable_headers(), json=payload, timeout=15)
                print(f"Airtable UPDATE {len(updates)} records: {r.status_code}")

    except Exception as e:
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
        "airline": None,
        "flight_number": None,
        "flight_date": None,
        "departure_airport": None,
        "arrival_airport": None,
        "route_distance_km": None,
        "distance_band": None,
        "passenger_names": [],
        "has_minors": None,
        "minors_count": 0,
        "language": lang,
        "temp_year": None,
        "temp_month": None,
        "temp_years": [],
        "mailing_address": None,
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
    send_whatsapp_text(phone, msg)


def send_passenger_potential_hook(phone, conv):
    """Apres le nombre de passagers : ancrage financier (plafond indicatif UE261)."""
    lang = conv["data"].get("language", "fr")
    pax = conv["data"].get("passengers") or 1
    max_brut = pax * 600
    max_net = int(max_brut * 0.75)
    if lang == "en":
        msg = (
            f"💡 With **{pax}** passenger(s), if EU261 applies, compensation can be up to **{max_brut} EUR** "
            f"total for the flight (from **250 to 600 EUR** per passenger depending on distance).\n\n"
            f"👉 That could mean up to **~{max_net} EUR** for you after our **25% fee (success only)**.\n\n"
            "📌 This is an **order of magnitude** — the final amount depends on your route and situation.\n\n"
            "➡️ Let's continue with a few quick questions?"
        )
    else:
        msg = (
            f"💡 Avec **{pax}** passager(s), si le reglement UE261 s'applique, l'indemnite peut aller jusqu'a **{max_brut} EUR** "
            f"au total pour le vol (de **250 a 600 EUR** par passager selon la distance).\n\n"
            f"👉 Soit souvent jusqu'a **~{max_net} EUR** pour vous apres notre **commission 25% (uniquement si succes)**.\n\n"
            "📌 C'est un **ordre de grandeur** — le montant final depend de votre trajet et de votre situation.\n\n"
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
            "1=Jan 2=Feb 3=Mar 4=Apr 5=May 6=Jun\n"
            "7=Jul 8=Aug 9=Sep 10=Oct 11=Nov 12=Dec\n\nReply with the number (1-12)"
        )
    else:
        msg = (
            "📅 Quel mois ?\n\n"
            "1=Jan 2=Fev 3=Mars 4=Avr 5=Mai 6=Juin\n"
            "7=Juil 8=Aout 9=Sept 10=Oct 11=Nov 12=Dec\n\nRepondez avec le numero (1-12)"
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


def parse_passenger_names_block(text, expected_pax):
    """
    Une ligne par passager : Prenom NOM (au moins 2 mots).
    Retourne (liste_normalisee, None) ou (None, code_erreur).
    """
    names = []
    for raw in text.split("\n"):
        stripped = raw.strip()
        if not stripped:
            continue
        clean = re.sub(r"^[\d\.\)\-\s]+", "", stripped).strip()
        if not clean or clean.isdigit():
            return None, "format"
        formatted = format_prenom_nom(clean)
        if not formatted:
            return None, "format"
        names.append(formatted)
    if len(names) < expected_pax:
        return None, "not_enough"
    return names[:expected_pax], None


def ask_passenger_names(phone, conv):
    lang = conv["data"]["language"]
    pax = conv["data"]["passengers"] or 1
    if lang == "en":
        msg = (
            f"👤 **{pax} passenger(s)** — one line each, format **First LAST** (last name in capitals).\n\n"
            "Example:\n"
            "1. John DOE\n"
            "2. Jane SMITH\n\n"
            "Same order as the number of tickets/passengers."
        )
    else:
        msg = (
            f"👤 **{pax} passager(s)** — **une ligne par personne**, format **Prénom NOM** "
            "(nom de famille tout en MAJUSCULES).\n\n"
            "Exemple :\n"
            "1. Jean DUPONT\n"
            "2. Marie MARTIN\n\n"
            "Meme ordre que sur les billets / la reservation."
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
            f"👶 Parmi les {pax} passagers, des mineurs (moins 18 ans) ?\n\n"
            "1️⃣ Non, tous majeurs\n2️⃣ Oui, il y a des mineurs\n\nRepondez avec 1 ou 2"
            if lang == "fr"
            else f"👶 Among {pax} passengers, any minors?\n\n1️⃣ No, all adults\n2️⃣ Yes, there are minors\n\nReply with 1 or 2"
        )
    send_whatsapp_text(phone, msg)


def ask_minors_count(phone, conv):
    lang = conv["data"]["language"]
    pax = conv["data"]["passengers"] or 1
    opts = "\n".join([f"{i}️⃣ {i} mineur{'s' if i > 1 else ''}" for i in range(1, min(pax, 5) + 1)])
    msg = (
        f"👶 Combien de mineurs parmi les {pax} passagers ?\n\n{opts}\n\nRepondez avec le numero"
        if lang == "fr"
        else f"👶 How many minors among {pax}?\n\n{opts}\n\nReply with the number"
    )
    send_whatsapp_text(phone, msg)


def ask_mailing_address(phone, conv):
    """Derniere saisie : adresse — privilegier copier-coller ou 2 lignes courtes."""
    lang = conv["data"].get("language", "fr")
    if lang == "en":
        msg = (
            "🏠 **Last step** — your **postal address** (for the file / mail).\n\n"
            "⏱️ **~30 seconds** if you **paste** the address; **1–2 min** if you type it short.\n\n"
            "**Fastest:** open Google Maps or Contacts → your home → **Copy address** → paste here (one message).\n\n"
            "**Or 2 short lines:**\n"
            "Line 1: **number, street and city** (e.g. 12 High Street, London)\n"
            "Line 2: **postcode** (e.g. SW1A 1AA)\n\n"
            "📋 **Total so far:** this WhatsApp questionnaire is usually about **5–8 minutes**; "
            "the mandate link below takes about **3 minutes** to sign.\n\n"
            "🌍 Country if outside the UK/EU — add at the end if needed."
        )
    else:
        msg = (
            "🏠 **Derniere etape** — votre **adresse postale** (dossier / courrier).\n\n"
            "⏱️ **~30 secondes** si vous **collez** l'adresse ; **1–2 min** si vous la tapez en court.\n\n"
            "**Le plus rapide :** Google Maps ou Contacts → chez vous → **Copier l'adresse** → collez-la ici (un seul message).\n\n"
            "**Ou 2 lignes courtes :**\n"
            "Ligne 1 : **n°, rue et ville** (ex : 12 rue de Rivoli, Paris)\n"
            "Ligne 2 : **code postal** (ex : 75001)\n\n"
            "📋 **Temps global :** ce questionnaire WhatsApp = en general **5 a 8 min** ; "
            "la signature du **mandat** sur le site ensuite = **environ 3 min**.\n\n"
            "🌍 Pays si hors France — ajoutez a la fin si besoin."
        )
    send_whatsapp_text(phone, msg)


def mailing_address_accepts(text):
    t = text.strip()
    if len(t) < 12:
        return False
    if not re.search(r"[a-zA-ZÀ-ÿ]", t):
        return False
    return True


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
    conv["current_step"] = "passenger_names"
    airtable_save_progressive(phone, conv)
    ask_passenger_names(phone, conv)


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
    if dep_a and arr_a:
        route_line_fr = f"🛫🛬 Trajet : {dep_a} → {arr_a}"
        route_line_en = f"🛫🛬 Route: {dep_a} → {arr_a}"
        if rkm:
            route_line_fr += f" (~{int(rkm)} km)"
            route_line_en += f" (~{int(rkm)} km)"
        route_line_fr += "\n"
        route_line_en += "\n"

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

    if lang == "en":
        body = (
            f"🎉 PERFECT!\n\n"
            f"📋 File ref: {ref}\n"
            f"✈️ Flight: {d.get('flight_number','?')} ({d.get('airline','?')})\n"
            f"📅 Date: {d.get('flight_date','?')}\n"
            f"{route_line_en}"
            f"👥 Passengers: {pax}\n{names_str}\n"
            f"{addr_line_en}"
            f"👶 Minors: {d.get('minors_count',0)}\n"
            f"⚠️ Incident: {incident}\n{cancel_hint_en}"
            f"📏 Distance bracket: {band_label}\n\n{money}\n\n"
            f"👇 Sign your mandate (~3 min):\n{mandat_link}"
        )
    else:
        body = (
            f"🎉 PARFAIT !\n\n"
            f"📋 Ref dossier : {ref}\n"
            f"✈️ Vol : {d.get('flight_number','?')} ({d.get('airline','?')})\n"
            f"📅 Date : {d.get('flight_date','?')}\n"
            f"{route_line_fr}"
            f"👥 Passagers : {pax}\n{names_str}\n"
            f"{addr_line_fr}"
            f"👶 Mineurs : {d.get('minors_count',0)}\n"
            f"⚠️ Incident : {incident}\n{cancel_hint_fr}"
            f"📏 Tranche distance : {band_label}\n\n{money}\n\n"
            f"👇 Signez votre mandat (~3 min) :\n{mandat_link}"
        )
    send_whatsapp_text(phone, body)

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

    if step == "entry_intent":
        if choice == "1":
            lang = conv["data"].get("language", "fr")
            conv["data"] = fresh_dossier_data(lang)
            conv["ref_dossier"] = generate_ref_dossier(phone)
            conv["current_step"] = "passengers"
            ask_passengers(phone, lang)
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
                    f"📚 More reading: {BLOG_URL}"
                )
            else:
                msg = (
                    "💬 Allez-y : **votre question precise** (UE261, retards, annulations, montants…).\n\n"
                    "ℹ️ Information generale — pas un conseil juridique personnalise.\n\n"
                    "📎 Pour ouvrir un dossier WhatsApp apres : repondez **1** ou ecrivez **dossier**\n"
                    f"📎 Plutot le site ? Repondez **2** au menu ou : {RDA_ONLINE_DOSSIER_URL}\n\n"
                    f"📚 A lire : {BLOG_URL}"
                )
            send_whatsapp_text(phone, msg)
            return True
        return False

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
        elif choice == "2":
            conv["data"]["flight_type"] = "connection"
        else:
            return False
        conv["current_step"] = "airline"
        airtable_save_progressive(phone, conv)
        ask_airline(phone, conv)
        return True

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
                f"😔 Retroactivite 5 ans max.\nVotre vol est trop ancien.\n\n👉 {BLOG_URL}",
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

    if step == "passenger_names":
        pax = conv["data"].get("passengers") or 1
        names, err = parse_passenger_names_block(text, pax)
        if names:
            conv["data"]["passenger_names"] = names
            conv["current_step"] = "minor_check"
            airtable_save_progressive(phone, conv)
            ask_minors(phone, conv)
            return True
        lang = conv["data"]["language"]
        if err == "not_enough":
            hint = (
                f"👤 Il manque des lignes : envoyez **{pax} lignes** (une par passager).\n\n"
                "Format : Prénom NOM\n"
                "Ex : Jean DUPONT"
                if lang == "fr"
                else f"👤 Missing lines: send **{pax} lines** (one per passenger).\n\n"
                "Format: First LAST\n"
                "e.g. John DOE"
            )
        else:
            hint = (
                "👤 Chaque ligne = **Prénom** + **NOM** (2 mots minimum, NOM en majuscules).\n\n"
                "Exemple :\n1. Jean DUPONT\n2. Marie MARTIN"
                if lang == "fr"
                else "👤 Each line = **First** + **LAST** name (2+ words, LAST in capitals).\n\n"
                "Example:\n1. John DOE\n2. Jane SMITH"
            )
        send_whatsapp_text(phone, hint)
        return True

    if step == "minor_check":
        if choice == "1":
            conv["data"]["has_minors"] = False
            conv["data"]["minors_count"] = 0
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
            conv["current_step"] = "minors_count"
            ask_minors_count(phone, conv)
            return True
        return False

    if step == "minors_count":
        pax = conv["data"].get("passengers") or 1
        if choice and choice.isdigit() and 1 <= int(choice) <= pax:
            conv["data"]["minors_count"] = int(choice)
            conv["current_step"] = "mailing_address"
            ask_mailing_address(phone, conv)
            return True
        return False

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
                conv["current_step"] = "passengers"
                ask_passengers(phone, lang)
                return jsonify({"status": "faq to dossier"}), 200
            ans = call_openai_faq(message_text, lang)
            if not ans:
                ans = (
                    "Je peux vous expliquer l'UE261 en general 😊\n\nPour un dossier concret : ecrivez dossier ou 1.\n\n👉 "
                    + BLOG_URL
                    if lang == "fr"
                    else "I can explain EU261 in general 😊\n\nFor a real claim: type dossier or 1.\n\n👉 " + BLOG_URL
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

        if current_step and current_step in STEPS:
            handled = process_reply(phone, message_text, conv)
            if handled:
                return jsonify({"status": "ok"}), 200
            lang = conv["data"].get("language", "fr")
            send_whatsapp_text(
                phone,
                "👆 Repondez avec le numero correspondant (ex: 1, 2, 3...)"
                if lang == "fr"
                else "👆 Reply with the number (e.g. 1, 2, 3...)",
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
    conv["current_step"] = "entry_intent"
    conv["data"]["language"] = "fr"
    conv["data"]["flow_mode"] = "dossier"
    ask_entry_intent(phone, "fr")
    return jsonify({"status": "menu", "phone": phone}), 200


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
            "version": "v12 - adresse postale derniere etape + durees indiquees",
            "domain": RDA_DOMAIN,
            "airtable": "OK" if AIRTABLE_API_KEY else "MISSING",
            "openai": "OK" if OPENAI_API_KEY else "MISSING",
            "wati": "OK" if WATI_API_TOKEN else "MISSING",
            "active_conversations": len(conversations),
        }
    ), 200


@app.route("/", methods=["GET"])
def home():
    return "Robin des Airs Bot v12", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
