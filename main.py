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


# ===== AIRTABLE =====

def airtable_headers():
    return {
        "Authorization": f"Bearer {AIRTABLE_API_KEY}",
        "Content-Type": "application/json"
    }


def airtable_url():
    table = requests.utils.quote(AIRTABLE_TABLE_NAME)
    return f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{table}"


def airtable_find_records_by_ref(ref):
    """Cherche les records existants par reference dossier"""
    if not AIRTABLE_API_KEY or not ref:
        return []
    try:
        # Filtre par formula : recherche dans le champ Remarques OU un champ Reference si existe
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

        # Construit les champs disponibles
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

        # Calcul montant
        band_id = d.get("distance_band", "band_unknown")
        per_pax = EU261_BANDS.get(band_id, EU261_BANDS["band_unknown"]).get("amount_eur")
        total_brut = (per_pax * pax) if per_pax else 0
        total_net = int(total_brut * 0.75) if total_brut else 0

        # Cherche records existants pour ce dossier
        existing = airtable_find_records_by_ref(ref)

        if not existing:
            # Premier enregistrement : cree N records (un par passager)
            records_to_create = []
            for i in range(pax):
                fields = dict(base_fields)
                if i < len(names):
                    fields["Nom"] = names[i]
                else:
                    fields["Nom"] = f"Passager {i+1}"
                fields["Remarques"] = f"Ref: {ref} | Passager {i+1}/{pax}"
                # Montant total uniquement sur la 1ere ligne
                if i == 0 and total_net:
                    fields["Montant"] = float(total_net)
                else:
                    fields["Montant"] = 0.0
                records_to_create.append({"fields": fields})

            payload = {"records": records_to_create, "typecast": True}
            r = requests.post(airtable_url(), headers=airtable_headers(), json=payload, timeout=15)
            print(f"Airtable CREATE {pax} records: {r.status_code} - {r.text[:200]}")

        else:
            # Update les records existants avec les nouvelles infos
            updates = []
            for i, rec in enumerate(existing[:pax]):
                fields = dict(base_fields)
                if i < len(names):
                    fields["Nom"] = names[i]
                if i == 0 and total_net:
                    fields["Montant"] = float(total_net)
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

    params_dict = {
        "ref": ref, "pax": pax, "vol": d.get("flight_number", ""),
        "date": d.get("flight_date", ""), "compagnie": d.get("airline", ""),
        "incident": d.get("incident_type", ""), "type_vol": d.get("flight_type", ""),
        "distance": band_id, "noms": ",".join(d.get("passenger_names", [])),
        "mineurs": d.get("minors_count", 0), "source": "whatsapp_bot",
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
        "version": "v8 - sauvegarde progressive Airtable + ref dossier + bug airline fixe",
        "domain": RDA_DOMAIN,
        "airtable": "OK" if AIRTABLE_API_KEY else "MISSING",
        "openai": "OK" if OPENAI_API_KEY else "MISSING",
        "wati": "OK" if WATI_API_TOKEN else "MISSING",
        "active_conversations": len(conversations),
    }), 200


@app.route("/", methods=["GET"])
def home():
    return "Robin des Airs Bot v8", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
