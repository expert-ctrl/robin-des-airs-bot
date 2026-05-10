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
AIRTABLE_TABLE_ID = os.environ.get("AIRTABLE_TABLE_ID", "tblfg688AGxaywi7O")

# Domaine unique — jamais d'autre lien
RDA_DOMAIN = "https://robindesairs.eu"
MANDAT_URL = f"{RDA_DOMAIN}/mandat-representation"
DEPOT_URL = f"{RDA_DOMAIN}/depot-express"
CALCUL_URL = f"{RDA_DOMAIN}/#funnel-box"
SUIVI_URL = f"{RDA_DOMAIN}/suivi-dossier"
PARRAINAGE_URL = f"{RDA_DOMAIN}/parrainage"
BLOG_URL = f"{RDA_DOMAIN}/blog"

# Sources officielles (lecture seule — pas de liens sortants)
DGAC_URL = "https://droits-passagers-aeriens.aviation-civile.gouv.fr/public/je-m-informe"
ECONOMIE_URL = "https://www.economie.gouv.fr/particuliers/voyager-et-se-deplacer/vol-annule-ou-retarde-quels-sont-vos-droits"

# ===== MEMOIRE =====
conversations = {}
recent_event_ids = {}
recent_payload_keys = {}
recent_outbound_sends = {}
MEMORY_HOURS = 24
DEDUP_WINDOW_SECONDS = 25
EVENT_ID_TTL_SECONDS = 900
OUTBOUND_DEDUP_SECONDS = int(os.environ.get("OUTBOUND_DEDUP_SECONDS", "45"))
OUTBOUND_CACHE_TTL_SECONDS = int(os.environ.get("OUTBOUND_CACHE_TTL_SECONDS", "900"))

EU261_BANDS = {
    "band_250": {"amount_eur": 250, "label_fr": "≤ 1500 km", "label_en": "≤ 1500 km"},
    "band_400": {"amount_eur": 400, "label_fr": "1500–3500 km", "label_en": "1500–3500 km"},
    "band_600": {"amount_eur": 600, "label_fr": "> 3500 km", "label_en": "> 3500 km"},
    "band_unknown": {"amount_eur": None, "label_fr": "Distance inconnue", "label_en": "Unknown distance"},
}

STEPS = [
    "passengers",
    "incident_type",
    "flight_type",
    "airline",
    "flight_number",
    "flight_date",
    "distance_band",
    "passenger_names",
    "minor_check",
    "summary",
]

SYSTEM_PROMPT = f"""Tu es l'agent IA de ROBIN DES AIRS. Tu reponds dans la LANGUE DU CLIENT (FR/EN).

REGLES FORMAT ABSOLUES :
- Minimum 3 emojis par message
- Bullet points avec emojis pour les listes
- Maximum 6 lignes par message
- Toujours finir par un lien robindesairs.eu
- Ton chaleureux comme un ami

LIENS AUTORISES — UNIQUEMENT ces liens (jamais d'autre domaine) :
- Mandat : {MANDAT_URL}
- Calculateur : {CALCUL_URL}
- Depot express : {DEPOT_URL}
- Suivi dossier : {SUIVI_URL}
- Parrainage 20 EUR : {PARRAINAGE_URL}
- Blog : {BLOG_URL}

REGLEMENT EU261/2004 :
- Retard arrivee +3h = indemnisation
- Annulation <14j avant = indemnisation
- Refus embarquement = indemnisation
- Montants : 250 EUR (≤1500km) / 400 EUR (1500-3500km) / 600 EUR (>3500km hors UE)
- Commission RDA : 25% si succes UNIQUEMENT
- Net passager : 75%
- Retroactivite : 5 ans
- Mineurs = memes droits que adultes

SOURCES OFFICIELLES (cite si question juridique precise) :
- FAQ DGAC : {DGAC_URL}
- Ministere Economie : {ECONOMIE_URL}

ESCALADE A CLIMBIE +33 7 56 86 36 30 si :
- Plus de 5 passagers / Deces / Question juridique complexe
"""


# ===== AIRTABLE =====

def save_lead_to_airtable(phone, conv_data, message_text=""):
    """Enregistre ou met a jour un lead dans Airtable"""
    if not AIRTABLE_API_KEY:
        print("Airtable non configure - skip")
        return
    try:
        d = conv_data.get("data", {})
        pax = d.get("passengers") or 1
        band_id = d.get("distance_band", "band_unknown")
        per_pax = EU261_BANDS.get(band_id, EU261_BANDS["band_unknown"]).get("amount_eur")
        total = (per_pax * pax) if per_pax else None

        fields = {
            "fldsFH0PoWe3AV0sI": str(phone),  # Numero WhatsApp
            "fldCtJysGhTYF2LNf": d.get("passenger_names", [{}])[0] if d.get("passenger_names") else "Inconnu",
            "fldqks5asIPXar8BD": message_text[:500] if message_text else "",  # Remarques
        }
        if d.get("flight_number"):
            fields["fldcVnS4B86eZntjr"] = d["flight_number"]
        if d.get("airline"):
            fields["fld8Ku1jGMOPWnrQc"] = d["airline"]
        if d.get("flight_date"):
            fields["flduDNEC3osPnTMAv"] = d["flight_date"]
        if d.get("incident_type"):
            incident_map = {"delay": "Retard +3h", "cancel": "Annulation", "denied": "Refus embarquement"}
            fields["fldci5VnHb0HpOoKL"] = incident_map.get(d["incident_type"], d["incident_type"])
        if total:
            fields["fldlzkJOqqC8AYbIM"] = float(total)

        url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_ID}"
        headers = {
            "Authorization": f"Bearer {AIRTABLE_API_KEY}",
            "Content-Type": "application/json"
        }
        response = requests.post(url, headers=headers, json={"fields": fields}, timeout=10)
        print(f"Airtable: {response.status_code}")
    except Exception as e:
        print(f"Airtable error: {e}")


# ===== DEDUP =====

def _cleanup_dedup_caches(now):
    to_del = [k for k, ts in recent_event_ids.items() if (now - ts).total_seconds() > EVENT_ID_TTL_SECONDS]
    for k in to_del:
        recent_event_ids.pop(k, None)
    to_del2 = [k for k, ts in recent_payload_keys.items() if (now - ts).total_seconds() > EVENT_ID_TTL_SECONDS]
    for k in to_del2:
        recent_payload_keys.pop(k, None)


def _extract_event_id(data):
    if not isinstance(data, dict):
        return ""
    candidates = [data.get("messageId"), data.get("id"), data.get("whatsappMessageId")]
    for c in candidates:
        if c:
            return str(c).strip()
    return ""


def is_duplicate_event(phone, data, payload_signature):
    now = datetime.now()
    _cleanup_dedup_caches(now)
    event_id = _extract_event_id(data)
    if event_id:
        if event_id in recent_event_ids:
            return True
        recent_event_ids[event_id] = now
    sig = (payload_signature or "").strip()
    if sig:
        key = hashlib.sha256(f"{phone}|{sig.lower()}".encode()).hexdigest()
        if key in recent_payload_keys:
            if (now - recent_payload_keys[key]).total_seconds() < DEDUP_WINDOW_SECONDS:
                return True
        recent_payload_keys[key] = now
    return False


def _cleanup_outbound_cache(now):
    to_del = [k for k, ts in recent_outbound_sends.items() if (now - ts).total_seconds() > OUTBOUND_CACHE_TTL_SECONDS]
    for k in to_del:
        recent_outbound_sends.pop(k, None)


def _conversation_step_for_phone(phone):
    conv = conversations.get(phone)
    return str(conv.get("current_step") or "none") if conv else "none"


def _outbound_should_block(phone, step, kind, fingerprint):
    now = datetime.now()
    _cleanup_outbound_cache(now)
    key = hashlib.sha256(f"{phone}|{step}|{kind}|{fingerprint}".encode()).hexdigest()
    if key in recent_outbound_sends:
        if (now - recent_outbound_sends[key]).total_seconds() < OUTBOUND_DEDUP_SECONDS:
            return True
    return False


def _register_outbound_success(phone, step, kind, fingerprint):
    now = datetime.now()
    key = hashlib.sha256(f"{phone}|{step}|{kind}|{fingerprint}".encode()).hexdigest()
    recent_outbound_sends[key] = now


# ===== CONVERSATIONS =====

def get_or_create_conversation(phone):
    if phone not in conversations:
        conversations[phone] = {
            "messages": [],
            "current_step": None,
            "data": {
                "passengers": None, "incident_type": None, "flight_type": None,
                "airline": None, "flight_number": None, "flight_date": None,
                "distance_band": None, "passenger_names": [], "has_minors": None,
                "minors_count": 0, "language": "fr", "temp_year": None, "temp_month": None,
            },
            "created": datetime.now(),
        }
    if (datetime.now() - conversations[phone]["created"]) > timedelta(hours=MEMORY_HOURS):
        del conversations[phone]
        return get_or_create_conversation(phone)
    return conversations[phone]


# ===== ENVOI WATI =====

def send_whatsapp_text(phone, message, *, skip_outbound_dedup=False):
    message = message.strip()
    if not message:
        return 0
    step = _conversation_step_for_phone(phone)
    fp = hashlib.sha256(message.encode()).hexdigest()
    if not skip_outbound_dedup and _outbound_should_block(phone, step, "text", fp):
        return 429
    url = f"{WATI_BASE_URL}/api/v1/sendSessionMessage/{phone}"
    headers = {"Authorization": f"Bearer {WATI_API_TOKEN}", "accept": "*/*"}
    response = requests.post(url, headers=headers, params={"messageText": message}, timeout=30)
    print(f"Wati TEXT: {response.status_code}")
    if response.status_code == 200 and not skip_outbound_dedup:
        _register_outbound_success(phone, step, "text", fp)
    return response.status_code


# ===== FLUX GUIDE — MENUS TEXTE (sans boutons interactifs) =====
# Remplacement de tous les boutons/listes par des menus numerotes
# Le client repond avec 1, 2, 3 etc.

def ask_passengers(phone, lang="fr"):
    conv = get_or_create_conversation(phone)
    if lang == "en":
        msg = (
            "👋 Hello! Welcome to Robin des Airs ✈️\n\n"
            "Let's check your eligibility in 2 min ⏱️\n\n"
            "👥 How many passengers were on the flight?\n\n"
            "1️⃣ 1 passenger — up to 600 EUR\n"
            "2️⃣ 2 passengers — up to 1200 EUR\n"
            "3️⃣ 3 passengers — up to 1800 EUR\n"
            "4️⃣ 4 passengers — up to 2400 EUR\n"
            "5️⃣ 5 passengers — up to 3000 EUR\n"
            "6️⃣ 6 or more — Climbie calls you\n\n"
            "Reply with the number (1-6)"
        )
    else:
        msg = (
            "👋 Bonjour ! Bienvenue chez Robin des Airs ✈️\n\n"
            "Verifions votre eligibilite en 2 min ⏱️\n\n"
            "👥 Combien de passagers sur le vol ?\n\n"
            "1️⃣ 1 passager — jusqu'a 600 EUR\n"
            "2️⃣ 2 passagers — jusqu'a 1200 EUR\n"
            "3️⃣ 3 passagers — jusqu'a 1800 EUR\n"
            "4️⃣ 4 passagers — jusqu'a 2400 EUR\n"
            "5️⃣ 5 passagers — jusqu'a 3000 EUR\n"
            "6️⃣ 6 ou plus — Climbie vous appelle\n\n"
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
            "1️⃣ Delay of more than 3 hours\n"
            "2️⃣ Flight cancelled\n"
            "3️⃣ Denied boarding (overbooking)\n\n"
            "Reply with 1, 2 or 3"
        )
    else:
        msg = (
            f"Genial ! 🎉 {pax} passager(s) note(s).\n\n"
            "✈️ Que s'est-il passe avec votre vol ?\n\n"
            "1️⃣ Retard de plus de 3 heures\n"
            "2️⃣ Vol annule\n"
            "3️⃣ Refus d'embarquement (surbooking)\n\n"
            "Repondez avec 1, 2 ou 3"
        )
    send_whatsapp_text(phone, msg)


def ask_flight_type(phone, conv):
    lang = conv["data"]["language"]
    if lang == "en":
        msg = (
            "✈️ Was your flight:\n\n"
            "1️⃣ Direct flight\n"
            "2️⃣ With connection(s)\n\n"
            "Reply with 1 or 2"
        )
    else:
        msg = (
            "✈️ Votre vol etait :\n\n"
            "1️⃣ Vol direct\n"
            "2️⃣ Avec correspondance(s)\n\n"
            "Repondez avec 1 ou 2"
        )
    send_whatsapp_text(phone, msg)


def ask_airline(phone, conv):
    lang = conv["data"]["language"]
    if lang == "en":
        msg = (
            "🛫 Which airline?\n\n"
            "1️⃣ Air France 🇫🇷\n"
            "2️⃣ KLM 🇳🇱\n"
            "3️⃣ Brussels Airlines 🇧🇪\n"
            "4️⃣ Lufthansa 🇩🇪\n"
            "5️⃣ TAP Portugal 🇵🇹\n"
            "6️⃣ Corsair\n"
            "7️⃣ Air Senegal\n"
            "8️⃣ Royal Air Maroc\n"
            "9️⃣ Other — type the name\n\n"
            "Reply with 1-9 or type the airline name"
        )
    else:
        msg = (
            "🛫 Quelle compagnie aerienne ?\n\n"
            "1️⃣ Air France 🇫🇷\n"
            "2️⃣ KLM 🇳🇱\n"
            "3️⃣ Brussels Airlines 🇧🇪\n"
            "4️⃣ Lufthansa 🇩🇪\n"
            "5️⃣ TAP Portugal 🇵🇹\n"
            "6️⃣ Corsair\n"
            "7️⃣ Air Senegal\n"
            "8️⃣ Royal Air Maroc\n"
            "9️⃣ Autre — tapez le nom\n\n"
            "Repondez avec 1-9 ou tapez le nom"
        )
    send_whatsapp_text(phone, msg)


def ask_flight_number(phone, conv):
    lang = conv["data"]["language"]
    airline = conv["data"]["airline"] or ""
    if lang == "en":
        msg = (
            f"📝 {airline} ✅\n\n"
            "What is your flight number?\n\n"
            "Example: AF718, KL563, SN271\n\n"
            "Or send a photo of your boarding pass 📸"
        )
    else:
        msg = (
            f"📝 {airline} ✅\n\n"
            "Quel est votre numero de vol ?\n\n"
            "Exemple : AF718, KL563, SN271\n\n"
            "Ou envoyez une photo de votre carte d'embarquement 📸"
        )
    send_whatsapp_text(phone, msg)


def ask_flight_date(phone, conv):
    lang = conv["data"]["language"]
    current_year = datetime.now().year
    if lang == "en":
        msg = (
            "📅 What year was your flight?\n\n"
            f"1️⃣ {current_year}\n"
            f"2️⃣ {current_year-1}\n"
            f"3️⃣ {current_year-2}\n"
            f"4️⃣ {current_year-3}\n"
            f"5️⃣ {current_year-4}\n"
            "6️⃣ Before 2021 (outside 5-year limit)\n\n"
            "Reply with 1-6"
        )
    else:
        msg = (
            "📅 De quelle annee etait votre vol ?\n\n"
            f"1️⃣ {current_year}\n"
            f"2️⃣ {current_year-1}\n"
            f"3️⃣ {current_year-2}\n"
            f"4️⃣ {current_year-3}\n"
            f"5️⃣ {current_year-4}\n"
            "6️⃣ Avant 2021 (hors retroactivite)\n\n"
            "Repondez avec 1-6"
        )
    conv["data"]["temp_years"] = [current_year, current_year-1, current_year-2, current_year-3, current_year-4]
    send_whatsapp_text(phone, msg)


def ask_flight_month(phone, conv):
    lang = conv["data"]["language"]
    if lang == "en":
        msg = (
            "📅 Which month?\n\n"
            "1️⃣ Jan  2️⃣ Feb  3️⃣ Mar\n"
            "4️⃣ Apr  5️⃣ May  6️⃣ Jun\n"
            "7️⃣ Jul  8️⃣ Aug  9️⃣ Sep\n"
            "🔟 Oct  1️⃣1️⃣ Nov  1️⃣2️⃣ Dec\n\n"
            "Reply with the number (1-12)"
        )
    else:
        msg = (
            "📅 Quel mois ?\n\n"
            "1️⃣ Jan  2️⃣ Fev  3️⃣ Mars\n"
            "4️⃣ Avr  5️⃣ Mai  6️⃣ Juin\n"
            "7️⃣ Juil 8️⃣ Aout 9️⃣ Sept\n"
            "🔟 Oct  1️⃣1️⃣ Nov  1️⃣2️⃣ Dec\n\n"
            "Repondez avec le numero (1-12)"
        )
    send_whatsapp_text(phone, msg)


def ask_flight_day_input(phone, conv):
    lang = conv["data"]["language"]
    msg = "📅 Tapez le jour exact (1-31) :" if lang == "fr" else "📅 Type the exact day (1-31):"
    send_whatsapp_text(phone, msg)
    conv["current_step"] = "flight_day_input"


def ask_distance_band(phone, conv):
    lang = conv["data"]["language"]
    if lang == "en":
        msg = (
            "📏 Distance of the flight?\n\n"
            "1️⃣ ≤ 1500 km — 250 EUR/passenger\n"
            "2️⃣ 1500–3500 km — 400 EUR/passenger\n"
            "3️⃣ > 3500 km (Europe-Africa) — 600 EUR/passenger\n"
            "4️⃣ I don't know — we'll estimate later\n\n"
            "Reply with 1, 2, 3 or 4"
        )
    else:
        msg = (
            "📏 Distance du vol ?\n\n"
            "1️⃣ ≤ 1500 km — 250 EUR/passager\n"
            "2️⃣ 1500–3500 km — 400 EUR/passager\n"
            "3️⃣ > 3500 km (Europe-Afrique) — 600 EUR/passager\n"
            "4️⃣ Je ne sais pas — on estimera\n\n"
            "Repondez avec 1, 2, 3 ou 4"
        )
    send_whatsapp_text(phone, msg)


def ask_passenger_names(phone, conv):
    lang = conv["data"]["language"]
    pax = conv["data"]["passengers"] or 1
    if lang == "en":
        msg = (
            f"👤 Names of the {pax} passenger(s) please.\n\n"
            "Send like this:\n"
            "1. John Doe\n"
            "2. Jane Doe\n\n"
            "(First name + Last name for each)"
        )
    else:
        msg = (
            f"👤 Noms des {pax} passager(s) svp.\n\n"
            "Envoyez comme ca :\n"
            "1. Jean Dupont\n"
            "2. Marie Dupont\n\n"
            "(Prenom + Nom pour chacun)"
        )
    send_whatsapp_text(phone, msg)


def ask_minors(phone, conv):
    lang = conv["data"]["language"]
    pax = conv["data"]["passengers"] or 1
    if pax == 1:
        if lang == "en":
            msg = "👤 Are you over 18?\n\n1️⃣ Yes, adult\n2️⃣ No, minor\n\nReply with 1 or 2"
        else:
            msg = "👤 Etes-vous majeur(e) (18+ ans) ?\n\n1️⃣ Oui, majeur\n2️⃣ Non, mineur\n\nRepondez avec 1 ou 2"
    else:
        if lang == "en":
            msg = f"👶 Among the {pax} passengers, any minors (under 18)?\n\n1️⃣ No, all adults\n2️⃣ Yes, there are minors\n\nReply with 1 or 2"
        else:
            msg = f"👶 Parmi les {pax} passagers, des mineurs (moins de 18 ans) ?\n\n1️⃣ Non, tous majeurs\n2️⃣ Oui, il y a des mineurs\n\nRepondez avec 1 ou 2"
    send_whatsapp_text(phone, msg)


def ask_minors_count(phone, conv):
    lang = conv["data"]["language"]
    pax = conv["data"]["passengers"] or 1
    opts = "\n".join([f"{i}️⃣ {i} mineur{'s' if i > 1 else ''}" if lang == "fr" else f"{i}️⃣ {i} minor{'s' if i > 1 else ''}" for i in range(1, min(pax, 5)+1)])
    if lang == "en":
        msg = f"👶 How many minors among the {pax} passengers?\n\n{opts}\n\nReply with the number"
    else:
        msg = f"👶 Combien de mineurs parmi les {pax} passagers ?\n\n{opts}\n\nRepondez avec le numero"
    send_whatsapp_text(phone, msg)


def show_summary_and_mandat(phone, conv):
    lang = conv["data"]["language"]
    d = conv["data"]
    pax = d["passengers"] or 1
    band_id = d.get("distance_band", "band_unknown")
    band_info = EU261_BANDS.get(band_id, EU261_BANDS["band_unknown"])
    per_pax = band_info.get("amount_eur")
    total = (per_pax * pax) if per_pax else None
    net = int(total * 0.75) if total else None
    band_label = band_info.get("label_fr" if lang == "fr" else "label_en")

    incident_labels = {
        "delay": "Retard +3h" if lang == "fr" else "Delay +3h",
        "cancel": "Annulation" if lang == "fr" else "Cancellation",
        "denied": "Refus embarquement" if lang == "fr" else "Denied boarding",
    }
    incident = incident_labels.get(d.get("incident_type", ""), d.get("incident_type", "?"))

    names_str = "\n".join([f"  - {n}" for n in d.get("passenger_names", [])]) or "  - A completer"

    # Lien mandat pre-rempli — uniquement robindesairs.eu
    params_dict = {
        "pax": pax,
        "vol": d.get("flight_number", ""),
        "date": d.get("flight_date", ""),
        "compagnie": d.get("airline", ""),
        "incident": d.get("incident_type", ""),
        "type_vol": d.get("flight_type", ""),
        "distance": band_id,
        "noms": ",".join(d.get("passenger_names", [])),
        "mineurs": d.get("minors_count", 0),
        "source": "whatsapp_bot",
    }
    query = "&".join([f"{k}={requests.utils.quote(str(v))}" for k, v in params_dict.items() if v])
    mandat_link = f"{MANDAT_URL}?{query}"

    if per_pax:
        if d.get("incident_type") == "delay" and band_id == "band_600":
            money = f"💶 300 a 600 EUR/passager ({band_label})\n💰 TOTAL: {300*pax} a {600*pax} EUR\n✅ NET: {int(0.75*300*pax)} a {int(0.75*600*pax)} EUR"
        else:
            money = f"💶 {per_pax} EUR/passager ({band_label})\n💰 TOTAL: {total} EUR\n✅ NET POUR VOUS: {net} EUR"
    else:
        money = "💶 Montant a confirmer selon distance\n✅ Net: 75% si succes"

    if lang == "en":
        body = (
            f"🎉 PERFECT! Here's your file summary:\n\n"
            f"✈️ Flight: {d.get('flight_number','?')} ({d.get('airline','?')})\n"
            f"📅 Date: {d.get('flight_date','?')}\n"
            f"👥 Passengers: {pax}\n"
            f"{names_str}\n"
            f"👶 Minors: {d.get('minors_count',0)}\n"
            f"⚠️ Incident: {incident}\n"
            f"📏 Distance: {band_label}\n\n"
            f"{money}\n\n"
            f"👇 Sign your mandate (3 min, pre-filled):\n{mandat_link}"
        )
    else:
        body = (
            f"🎉 PARFAIT ! Voici votre dossier :\n\n"
            f"✈️ Vol : {d.get('flight_number','?')} ({d.get('airline','?')})\n"
            f"📅 Date : {d.get('flight_date','?')}\n"
            f"👥 Passagers : {pax}\n"
            f"{names_str}\n"
            f"👶 Mineurs : {d.get('minors_count',0)}\n"
            f"⚠️ Incident : {incident}\n"
            f"📏 Distance : {band_label}\n\n"
            f"{money}\n\n"
            f"👇 Signez votre mandat (3 min, pre-rempli) :\n{mandat_link}"
        )

    send_whatsapp_text(phone, body)
    
    # Enregistre dans Airtable
    save_lead_to_airtable(phone, conv, f"Dossier complet - {d.get('airline','')} {d.get('flight_number','')}")
    
    conv["current_step"] = "completed"


# ===== TRAITEMENT REPONSES NUMERIQUES =====

AIRLINES_MAP = {
    "1": "Air France", "2": "KLM", "3": "Brussels Airlines",
    "4": "Lufthansa", "5": "TAP Portugal", "6": "Corsair",
    "7": "Air Senegal", "8": "Royal Air Maroc",
}

def process_text_menu(phone, text, conv):
    """Traite les reponses numeriques aux menus texte"""
    current_step = conv.get("current_step")
    text = text.strip()
    num = re.search(r"^\d+", text)
    choice = num.group(0) if num else None

    # PASSAGERS
    if current_step == "passengers":
        if choice in ["1","2","3","4","5"]:
            conv["data"]["passengers"] = int(choice)
            conv["current_step"] = "incident_type"
            ask_incident_type(phone, conv)
            return True
        elif choice == "6":
            send_whatsapp_text(phone, f"🙏 Pour les groupes de 6+, Climbie vous appelle directement.\n\n📱 +33 7 56 86 36 30\n\nOu : 👉 {DEPOT_URL}")
            return True

    # INCIDENT
    if current_step == "incident_type":
        mapping = {"1": "delay", "2": "cancel", "3": "denied"}
        if choice in mapping:
            conv["data"]["incident_type"] = mapping[choice]
            conv["current_step"] = "flight_type"
            ask_flight_type(phone, conv)
            return True

    # TYPE VOL
    if current_step == "flight_type":
        if choice == "1":
            conv["data"]["flight_type"] = "direct"
            conv["current_step"] = "airline"
            ask_airline(phone, conv)
            return True
        elif choice == "2":
            conv["data"]["flight_type"] = "connection"
            conv["current_step"] = "airline"
            ask_airline(phone, conv)
            return True

    # COMPAGNIE
    if current_step == "airline":
        if choice in AIRLINES_MAP:
            conv["data"]["airline"] = AIRLINES_MAP[choice]
            conv["current_step"] = "flight_number"
            ask_flight_number(phone, conv)
            return True
        elif choice == "9":
            lang = conv["data"]["language"]
            send_whatsapp_text(phone, "✍️ Tapez le nom de votre compagnie :" if lang == "fr" else "✍️ Type your airline name:")
            conv["current_step"] = "airline_other_input"
            return True
        elif len(text) > 2:
            # Le client a tape directement le nom
            conv["data"]["airline"] = text
            conv["current_step"] = "flight_number"
            ask_flight_number(phone, conv)
            return True

    # ANNEE
    if current_step == "flight_date":
        years = conv["data"].get("temp_years", [])
        if choice == "6":
            lang = conv["data"]["language"]
            send_whatsapp_text(phone, f"😔 La retroactivite est de 5 ans max.\n\nVotre vol est trop ancien.\n\n👉 {BLOG_URL}" if lang == "fr" else f"😔 The 5-year limit has passed.\n\nYour flight is too old.\n\n👉 {BLOG_URL}")
            return True
        idx = int(choice) - 1 if choice and choice.isdigit() else -1
        if 0 <= idx < len(years):
            conv["data"]["temp_year"] = str(years[idx])
            conv["current_step"] = "flight_month"
            ask_flight_month(phone, conv)
            return True

    # MOIS
    if current_step == "flight_month":
        if choice and choice.isdigit() and 1 <= int(choice) <= 12:
            conv["data"]["temp_month"] = f"{int(choice):02d}"
            ask_flight_day_input(phone, conv)
            return True

    # JOUR
    if current_step == "flight_day_input":
        if choice and choice.isdigit() and 1 <= int(choice) <= 31:
            day = f"{int(choice):02d}"
            year = conv["data"].get("temp_year", "")
            month = conv["data"].get("temp_month", "")
            conv["data"]["flight_date"] = f"{day}/{month}/{year}"
            conv["current_step"] = "distance_band"
            ask_distance_band(phone, conv)
            return True

    # DISTANCE
    if current_step == "distance_band":
        band_map = {"1": "band_250", "2": "band_400", "3": "band_600", "4": "band_unknown"}
        if choice in band_map:
            conv["data"]["distance_band"] = band_map[choice]
            conv["current_step"] = "passenger_names"
            ask_passenger_names(phone, conv)
            return True

    # MINEURS
    if current_step == "minor_check":
        if choice == "1":
            conv["data"]["has_minors"] = False
            conv["data"]["minors_count"] = 0
            conv["current_step"] = "summary"
            show_summary_and_mandat(phone, conv)
            return True
        elif choice == "2":
            pax = conv["data"].get("passengers") or 1
            if pax == 1:
                send_whatsapp_text(phone, "👶 Pour un mineur seul, un parent doit signer.\n\n📱 Climbie : +33 7 56 86 36 30")
            else:
                conv["data"]["has_minors"] = True
                ask_minors_count(phone, conv)
            return True

    # NOMBRE MINEURS
    if current_step == "minors_count":
        pax = conv["data"].get("passengers") or 1
        if choice and choice.isdigit() and 1 <= int(choice) <= pax:
            conv["data"]["minors_count"] = int(choice)
            conv["current_step"] = "summary"
            show_summary_and_mandat(phone, conv)
            return True

    return False


# ===== OPENAI =====

def call_openai(phone, user_message, image_data=None):
    try:
        conv = get_or_create_conversation(phone)
        if image_data:
            user_content = [
                {"type": "text", "text": user_message or "Voici ma carte d'embarquement"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_data}"}},
            ]
            conv["messages"].append({"role": "user", "content": user_content})
        else:
            conv["messages"].append({"role": "user", "content": user_message})

        if len(conv["messages"]) > 20:
            conv["messages"] = conv["messages"][-20:]

        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        data_context = "\n\nDONNEES COLLECTEES:\n"
        for k, v in conv["data"].items():
            if v and k not in ["language", "temp_year", "temp_month", "temp_years"]:
                data_context += f"- {k}: {v}\n"
        if any(v for k, v in conv["data"].items() if k not in ["language", "temp_year", "temp_month", "temp_years"]):
            messages[0]["content"] += data_context

        messages.extend(conv["messages"])
        model = "gpt-4o" if image_data else "gpt-4o-mini"

        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={"model": model, "messages": messages, "max_tokens": 400, "temperature": 0.7},
            timeout=45,
        )
        data = response.json()
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
    en_words = ["hello", "hi", "my", "flight", "delay", "cancel", "what", "how", "yes", "no", "thanks", "i", "was"]
    fr_words = ["bonjour", "salut", "mon", "vol", "retard", "annul", "que", "comment", "oui", "non", "merci", "je"]
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

        # Images
        message_type = data.get("type", "text")
        image_data = None
        message_text = ""

        if message_type == "image" or "image" in data:
            media_url = data.get("data") or data.get("mediaUrl")
            if media_url:
                try:
                    r = requests.get(media_url, headers={"Authorization": f"Bearer {WATI_API_TOKEN}"}, timeout=30)
                    if r.status_code == 200:
                        image_data = base64.b64encode(r.content).decode("utf-8")
                except Exception as e:
                    print(f"Image download error: {e}")
            message_text = data.get("caption", "") or "Voici ma carte d'embarquement"
        else:
            if "text" in data:
                message_text = data["text"].get("body", "") if isinstance(data["text"], dict) else data["text"]
            elif "body" in data:
                message_text = data["body"]

        if not message_text and not image_data:
            return jsonify({"status": "ignored empty"}), 200

        payload_sig = f"text|{message_text.strip().lower()}|img:{bool(image_data)}"
        if is_duplicate_event(phone, data, payload_sig):
            return jsonify({"status": "duplicate"}), 200

        print(f"Message de {phone}: {message_text[:80]}")

        # Detecte langue
        if message_text:
            lang = detect_language(message_text)
            if lang != conv["data"].get("language"):
                conv["data"]["language"] = lang

        current_step = conv.get("current_step")

        # Image de carte d'embarquement
        if image_data and current_step == "flight_number":
            response = call_openai(phone, "Extrait: numero de vol, date, compagnie. JSON: {flight_number:..., date:..., airline:...}", image_data)
            if response:
                try:
                    m = re.search(r"\{[^}]+\}", response)
                    if m:
                        extracted = json.loads(m.group())
                        if extracted.get("flight_number"):
                            conv["data"]["flight_number"] = extracted["flight_number"]
                        if extracted.get("date"):
                            conv["data"]["flight_date"] = extracted["date"]
                        if extracted.get("airline"):
                            conv["data"]["airline"] = extracted["airline"]
                        send_whatsapp_text(phone, f"📸 Carte lue !\n\n✈️ Vol : {conv['data'].get('flight_number','?')}\n📅 Date : {conv['data'].get('flight_date','?')}\n✅ On continue...")
                        conv["current_step"] = "distance_band"
                        ask_distance_band(phone, conv)
                        return jsonify({"status": "ok"}), 200
                except Exception:
                    pass

        # Saisie compagnie autre
        if current_step == "airline_other_input":
            conv["data"]["airline"] = message_text.strip()
            conv["current_step"] = "flight_number"
            ask_flight_number(phone, conv)
            return jsonify({"status": "ok"}), 200

        # Saisie noms passagers
        if current_step == "passenger_names":
            lines = [l.strip() for l in message_text.split("\n") if l.strip()]
            names = [re.sub(r"^[\d\.\)\-\s]+", "", l).strip() for l in lines]
            names = [n for n in names if len(n) >= 3 and not n.isdigit()]
            if names:
                conv["data"]["passenger_names"] = names
                conv["current_step"] = "minor_check"
                ask_minors(phone, conv)
                # Enregistre le lead partiel dans Airtable
                save_lead_to_airtable(phone, conv, message_text)
            else:
                lang = conv["data"]["language"]
                send_whatsapp_text(phone, "👤 Envoyez les noms :\n1. Jean Dupont\n2. Marie Dupont" if lang == "fr" else "👤 Send names:\n1. John Doe\n2. Jane Doe")
            return jsonify({"status": "ok"}), 200

        # Menus numeriques — si flux en cours, on traite TOUJOURS ici
        if current_step in STEPS:
            if process_text_menu(phone, message_text, conv):
                return jsonify({"status": "ok"}), 200
            # Si le menu n'a pas reconnu la reponse, on redemande
            lang = conv["data"].get("language", "fr")
            send_whatsapp_text(phone, "👆 Repondez avec le numero correspondant (ex: 1, 2, 3...)" if lang == "fr" else "👆 Please reply with the number (e.g. 1, 2, 3...)")
            return jsonify({"status": "ok"}), 200

        # Demarrage flux — uniquement si pas de conversation en cours
        trigger_words = ["vol", "retard", "annul", "indemn", "flight", "delay", "cancel", "compensation",
                        "claim", "bonjour", "hello", "salut", "hi", "start", "commencer", "menu", "aide", "help"]
        is_trigger = any(w in message_text.lower() for w in trigger_words)

        if conv.get("current_step") is None or current_step == "completed":
            if is_trigger or len(message_text) < 50:
                conv["current_step"] = "passengers"
                # Enregistre le premier contact dans Airtable
                save_lead_to_airtable(phone, conv, message_text)
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


# ===== ENDPOINTS =====

@app.route("/test_flow/<phone>", methods=["GET"])
def test_flow(phone):
    conv = get_or_create_conversation(phone)
    conv["current_step"] = "passengers"
    conv["data"]["language"] = "fr"
    ask_passengers(phone, "fr")
    return jsonify({"status": "flow started", "phone": phone}), 200


@app.route("/conversations", methods=["GET"])
def list_conversations():
    result = {}
    for phone, conv in conversations.items():
        result[phone] = {"step": conv.get("current_step"), "data": conv["data"], "messages": len(conv["messages"])}
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
        "version": "v7 - menus texte + Airtable direct + domaine unique robindesairs.eu",
        "domain": RDA_DOMAIN,
        "airtable": "configured" if AIRTABLE_API_KEY else "MISSING",
        "openai": "configured" if OPENAI_API_KEY else "MISSING",
        "wati": "configured" if WATI_API_TOKEN else "MISSING",
        "active_conversations": len(conversations),
    }), 200


@app.route("/", methods=["GET"])
def home():
    return "Robin des Airs Bot v7 - Running!", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
