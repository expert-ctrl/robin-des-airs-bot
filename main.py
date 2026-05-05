from flask import Flask, request, jsonify
import requests
import os
import json
import base64
import re
from datetime import datetime, timedelta

app = Flask(__name__)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
WATI_API_TOKEN = os.environ.get("WATI_API_TOKEN", "")
WATI_BASE_URL = os.environ.get("WATI_BASE_URL", "")
MANDAT_BASE_URL = os.environ.get("MANDAT_BASE_URL", "https://robindesairs.eu/mandat-representation")

# ===== MEMOIRE CONVERSATIONS =====
conversations = {}
last_message_time = {}
MEMORY_HOURS = 24
MIN_SECONDS_BETWEEN = 2

# ===== ETAPES DU FLUX GUIDE =====
STEPS = [
    "passengers",      # Nombre de passagers (TOUJOURS EN PREMIER)
    "incident_type",   # Retard / Annulation / Surbooking
    "flight_type",     # Direct / Correspondance
    "airline",         # Compagnie aerienne
    "flight_number",   # Numero de vol
    "flight_date",     # Date du vol
    "passenger_names", # Noms des passagers
    "minor_check",     # Mineurs ou non
    "summary"          # Recap + lien mandat
]

SYSTEM_PROMPT = """Tu es l'agent IA de ROBIN DES AIRS. Tu reponds dans la LANGUE DU CLIENT (FR/EN/etc).

REGLES FORMAT :
- 3+ emojis par message
- Bullet points avec emojis
- Max 6 lignes
- Toujours finir par lien d'action

INFOS CLES :
- 600 EUR par passager (vol +3500km Europe-Afrique)
- 25% commission UNIQUEMENT si succes
- Net passager: 75% (450 EUR sur 600 EUR)
- 5 ans de retroactivite
- Mineurs ont MEMES droits que adultes

LIENS :
- Mandat: robindesairs.eu/mandat-representation
- Calculateur: robindesairs.eu/#funnel-box
- Depot: robindesairs.eu/depot-express

ESCALADE A CLIMBIE +33 7 56 86 36 30 si :
- Plus de 5 passagers
- Deces / Heritage
- Question juridique complexe
"""

def get_or_create_conversation(phone):
    if phone not in conversations:
        conversations[phone] = {
            "messages": [],
            "current_step": None,  # None = pas encore demarre flux guide
            "data": {
                "passengers": None,
                "incident_type": None,
                "flight_type": None,
                "airline": None,
                "airline_other": None,
                "flight_number": None,
                "flight_date": None,
                "passenger_names": [],
                "has_minors": None,
                "minors_count": 0,
                "language": "fr"
            },
            "created": datetime.now()
        }
    
    if (datetime.now() - conversations[phone]["created"]) > timedelta(hours=MEMORY_HOURS):
        del conversations[phone]
        return get_or_create_conversation(phone)
    
    return conversations[phone]

def is_duplicate_message(phone):
    now = datetime.now()
    if phone in last_message_time:
        diff = (now - last_message_time[phone]).total_seconds()
        if diff < MIN_SECONDS_BETWEEN:
            return True
    last_message_time[phone] = now
    return False

# ============================================
# WATI - ENVOI MESSAGES
# ============================================

def send_whatsapp_text(phone, message):
    """Envoie un message texte simple"""
    message = message.strip()
    if not message:
        return 0
    url = f"{WATI_BASE_URL}/api/v1/sendSessionMessage/{phone}"
    headers = {"Authorization": f"Bearer {WATI_API_TOKEN}", "accept": "*/*"}
    params = {"messageText": message}
    response = requests.post(url, headers=headers, params=params, timeout=30)
    print(f"Wati TEXT: {response.status_code}")
    return response.status_code

def send_whatsapp_buttons(phone, body_text, buttons, header_text=None, footer_text=None):
    """Envoie un message avec boutons interactifs (max 3 boutons)
    buttons = [{"id": "btn_id", "title": "Texte bouton"}, ...]
    """
    url = f"{WATI_BASE_URL}/api/v1/sendInteractiveButtonsMessage"
    headers = {
        "Authorization": f"Bearer {WATI_API_TOKEN}",
        "Content-Type": "application/json",
        "accept": "*/*"
    }
    params = {"whatsappNumber": phone}
    
    payload = {
        "body": body_text,
        "buttons": [{"text": btn["title"]} for btn in buttons[:3]]
    }
    if header_text:
        payload["header"] = header_text
    if footer_text:
        payload["footer"] = footer_text
    
    response = requests.post(url, headers=headers, params=params, json=payload, timeout=30)
    print(f"Wati BUTTONS: {response.status_code} - {response.text[:200]}")
    
    # Si echec boutons, fallback texte
    if response.status_code != 200:
        fallback = body_text + "\n\n"
        for i, btn in enumerate(buttons, 1):
            fallback += f"{i}. {btn['title']}\n"
        fallback += "\nRepondez avec le numero de votre choix."
        send_whatsapp_text(phone, fallback)
    
    return response.status_code

def send_whatsapp_list(phone, body_text, button_label, sections, header_text=None, footer_text=None):
    """Envoie un menu liste (jusqu'a 10 options)
    sections = [{"title": "Section", "rows": [{"id": "row1", "title": "Option 1", "description": "..."}]}]
    """
    url = f"{WATI_BASE_URL}/api/v1/sendInteractiveListMessage"
    headers = {
        "Authorization": f"Bearer {WATI_API_TOKEN}",
        "Content-Type": "application/json",
        "accept": "*/*"
    }
    params = {"whatsappNumber": phone}
    
    # Normalisation defensive: certains endpoints WATI attendent rowId (pas id).
    normalized_sections = []
    for section in sections:
        rows = []
        for row in section.get("rows", []):
            rid = row.get("id") or row.get("rowId") or row.get("payload") or ""
            rows.append({
                "id": rid,
                "rowId": rid,
                "title": row.get("title", ""),
                "description": row.get("description", "")
            })
        normalized_sections.append({
            "title": section.get("title", ""),
            "rows": rows
        })

    payload = {
        "body": body_text,
        "buttonText": button_label,
        "sections": normalized_sections
    }
    if header_text:
        payload["header"] = header_text
    if footer_text:
        payload["footer"] = footer_text
    
    response = requests.post(url, headers=headers, params=params, json=payload, timeout=30)
    print(f"Wati LIST: {response.status_code} - {response.text[:200]}")
    
    if response.status_code != 200:
        # Fallback texte
        fallback = body_text + "\n\n"
        idx = 1
        for section in sections:
            for row in section["rows"]:
                fallback += f"{idx}. {row['title']}\n"
                idx += 1
        fallback += "\nRepondez avec le numero de votre choix."
        send_whatsapp_text(phone, fallback)
    
    return response.status_code

# ============================================
# QUESTIONS DU FLUX GUIDE
# ============================================

def ask_passengers(phone, lang="fr"):
    """ETAPE 1 : Nombre de passagers (TOUJOURS EN PREMIER)"""
    if lang == "en":
        body = "👋 Hello! Welcome to Robin des Airs ✈️\n\nLet's check your eligibility in 2 min.\n\n👥 First, how many passengers were on the flight?"
    else:
        body = "👋 Bonjour ! Bienvenue chez Robin des Airs ✈️\n\nVerifions votre eligibilite en 2 min.\n\n👥 D'abord, combien de passagers etaient sur le vol ?"
    
    sections = [{
        "title": "Nombre de passagers" if lang == "fr" else "Number of passengers",
        "rows": [
            {"id": "pax_1", "title": "1 passager" if lang == "fr" else "1 passenger", "description": "= 600 EUR"},
            {"id": "pax_2", "title": "2 passagers" if lang == "fr" else "2 passengers", "description": "= 1200 EUR"},
            {"id": "pax_3", "title": "3 passagers" if lang == "fr" else "3 passengers", "description": "= 1800 EUR"},
            {"id": "pax_4", "title": "4 passagers" if lang == "fr" else "4 passengers", "description": "= 2400 EUR"},
            {"id": "pax_5", "title": "5 passagers" if lang == "fr" else "5 passengers", "description": "= 3000 EUR"},
            {"id": "pax_more", "title": "6 ou plus" if lang == "fr" else "6 or more", "description": "Climbie vous appelle"}
        ]
    }]
    
    label = "Choisir 👥" if lang == "fr" else "Select 👥"
    send_whatsapp_list(phone, body, label, sections)

def ask_incident_type(phone, conv):
    """ETAPE 2 : Type d'incident"""
    lang = conv["data"]["language"]
    pax = conv["data"]["passengers"]
    total = 600 * pax if pax else 600
    
    if lang == "en":
        body = f"Great! 🎉 {pax} passenger(s) = up to {total} EUR potential 💰\n\n✈️ What happened with your flight?"
    else:
        body = f"Genial ! 🎉 {pax} passager(s) = jusqu'a {total} EUR potentiel 💰\n\n✈️ Que s'est-il passe avec votre vol ?"
    
    buttons = [
        {"id": "inc_delay", "title": "⏱️ Retard +3h" if lang == "fr" else "⏱️ Delay +3h"},
        {"id": "inc_cancel", "title": "❌ Annulation" if lang == "fr" else "❌ Cancellation"},
        {"id": "inc_denied", "title": "🚫 Surbooking" if lang == "fr" else "🚫 Denied boarding"}
    ]
    send_whatsapp_buttons(phone, body, buttons)

def ask_flight_type(phone, conv):
    """ETAPE 3 : Vol direct ou correspondance"""
    lang = conv["data"]["language"]
    
    if lang == "en":
        body = "✈️ Was it a direct flight or with connection?"
    else:
        body = "✈️ Etait-ce un vol direct ou avec correspondance ?"
    
    buttons = [
        {"id": "type_direct", "title": "✈️ Vol direct" if lang == "fr" else "✈️ Direct flight"},
        {"id": "type_connection", "title": "🔄 Avec correspondance" if lang == "fr" else "🔄 With connection"}
    ]
    send_whatsapp_buttons(phone, body, buttons)

def ask_airline(phone, conv):
    """ETAPE 4 : Compagnie aerienne (liste avec option Autre)"""
    lang = conv["data"]["language"]
    
    if lang == "en":
        body = "🛫 Which airline operated your flight?"
        label = "Select airline ✈️"
    else:
        body = "🛫 Quelle compagnie aerienne etait votre vol ?"
        label = "Choisir compagnie ✈️"
    
    sections = [
        {
            "title": "Compagnies europeennes" if lang == "fr" else "European airlines",
            "rows": [
                {"id": "air_af", "title": "Air France", "description": "🇫🇷 Eligible 100%"},
                {"id": "air_klm", "title": "KLM", "description": "🇳🇱 Eligible 100%"},
                {"id": "air_brussels", "title": "Brussels Airlines", "description": "🇧🇪 Eligible 100%"},
                {"id": "air_lufthansa", "title": "Lufthansa", "description": "🇩🇪 Eligible 100%"},
                {"id": "air_tap", "title": "TAP Portugal", "description": "🇵🇹 Eligible 100%"}
            ]
        },
        {
            "title": "Autres compagnies" if lang == "fr" else "Other airlines",
            "rows": [
                {"id": "air_corsair", "title": "Corsair", "description": "🇫🇷 Eligible"},
                {"id": "air_airsenegal", "title": "Air Senegal", "description": "Si vol depart UE"},
                {"id": "air_ram", "title": "Royal Air Maroc", "description": "Si vol depart UE"},
                {"id": "air_other", "title": "Autre" if lang == "fr" else "Other", "description": "Tapez le nom"}
            ]
        }
    ]
    send_whatsapp_list(phone, body, label, sections)

def ask_flight_number(phone, conv):
    """ETAPE 5 : Numero de vol (saisie libre)"""
    lang = conv["data"]["language"]
    airline = conv["data"]["airline"]
    
    if lang == "en":
        body = f"📝 Great! {airline} ✅\n\nWhat's your flight number?\n\nExample: AF718, KL563, SN271\n\n(If you don't know, send a photo of your boarding pass 📸)"
    else:
        body = f"📝 Parfait ! {airline} ✅\n\nQuel est votre numero de vol ?\n\nExemple : AF718, KL563, SN271\n\n(Si vous ne savez pas, envoyez une photo de votre carte d'embarquement 📸)"
    
    send_whatsapp_text(phone, body)

def ask_flight_date(phone, conv):
    """ETAPE 6 : Date du vol (par annee puis mois)"""
    lang = conv["data"]["language"]
    
    if lang == "en":
        body = "📅 What year was your flight?"
        label = "Select year"
    else:
        body = "📅 De quelle annee etait votre vol ?"
        label = "Choisir annee"
    
    current_year = datetime.now().year
    sections = [{
        "title": "Annee" if lang == "fr" else "Year",
        "rows": [
            {"id": f"year_{current_year}", "title": str(current_year), "description": "Cette annee" if lang == "fr" else "This year"},
            {"id": f"year_{current_year-1}", "title": str(current_year-1), "description": "L'annee derniere" if lang == "fr" else "Last year"},
            {"id": f"year_{current_year-2}", "title": str(current_year-2)},
            {"id": f"year_{current_year-3}", "title": str(current_year-3)},
            {"id": f"year_{current_year-4}", "title": str(current_year-4)},
            {"id": "year_other", "title": "Avant 2021" if lang == "fr" else "Before 2021", "description": "Hors retroactivite"}
        ]
    }]
    send_whatsapp_list(phone, body, label, sections)

def ask_passenger_names(phone, conv):
    """ETAPE 7 : Noms des passagers"""
    lang = conv["data"]["language"]
    pax = conv["data"]["passengers"]
    
    if lang == "en":
        body = f"👤 Great! Now I need the names of all {pax} passenger(s).\n\nPlease send them like this:\n1. John Doe\n2. Jane Doe\n{f'3. ...' if pax > 2 else ''}\n\n(First name + Last name for each)"
    else:
        body = f"👤 Parfait ! Maintenant les noms des {pax} passager(s).\n\nEnvoyez-les comme ca :\n1. Jean Dupont\n2. Marie Dupont\n{f'3. ...' if pax > 2 else ''}\n\n(Prenom + Nom pour chacun)"
    
    send_whatsapp_text(phone, body)

def ask_minors(phone, conv):
    """ETAPE 8 : Mineurs"""
    lang = conv["data"]["language"]
    pax = conv["data"]["passengers"]
    
    if pax == 1:
        # Si 1 seul passager, demander juste si majeur
        if lang == "en":
            body = "👤 Are you over 18 years old?"
        else:
            body = "👤 Etes-vous majeur(e) (18+ ans) ?"
        buttons = [
            {"id": "minor_no", "title": "✅ Oui, majeur" if lang == "fr" else "✅ Yes, adult"},
            {"id": "minor_self", "title": "👶 Non, mineur" if lang == "fr" else "👶 No, minor"}
        ]
    else:
        if lang == "en":
            body = f"👶 Among the {pax} passengers, are there any minors (under 18)?"
        else:
            body = f"👶 Parmi les {pax} passagers, y a-t-il des mineurs (moins de 18 ans) ?"
        buttons = [
            {"id": "minor_no", "title": "✅ Tous majeurs" if lang == "fr" else "✅ All adults"},
            {"id": "minor_yes", "title": "👶 Oui, mineurs" if lang == "fr" else "👶 Yes, minors"}
        ]
    send_whatsapp_buttons(phone, body, buttons)

def ask_minors_count(phone, conv):
    """Combien de mineurs"""
    lang = conv["data"]["language"]
    pax = conv["data"]["passengers"]
    
    if lang == "en":
        body = f"👶 How many minors among the {pax} passengers?"
        label = "Select number"
    else:
        body = f"👶 Combien de mineurs parmi les {pax} passagers ?"
        label = "Choisir nombre"
    
    rows = []
    for i in range(1, min(pax, 5) + 1):
        rows.append({
            "id": f"minors_count_{i}",
            "title": f"{i} mineur{'s' if i > 1 else ''}" if lang == "fr" else f"{i} minor{'s' if i > 1 else ''}"
        })
    
    sections = [{"title": "Nombre" if lang == "fr" else "Number", "rows": rows}]
    send_whatsapp_list(phone, body, label, sections)

def show_summary_and_mandat(phone, conv):
    """ETAPE FINALE : Recapitulatif + lien mandat pre-rempli"""
    lang = conv["data"]["language"]
    d = conv["data"]
    pax = d["passengers"]
    total = 600 * pax
    net = int(total * 0.75)
    
    incident_labels = {
        "delay": "Retard +3h" if lang == "fr" else "Delay +3h",
        "cancel": "Annulation" if lang == "fr" else "Cancellation",
        "denied": "Refus embarquement" if lang == "fr" else "Denied boarding"
    }
    incident = incident_labels.get(d["incident_type"], d["incident_type"])
    
    # Construction lien mandat pre-rempli
    params = {
        "pax": pax,
        "vol": d.get("flight_number", ""),
        "date": d.get("flight_date", ""),
        "compagnie": d.get("airline", ""),
        "incident": d.get("incident_type", ""),
        "type_vol": d.get("flight_type", ""),
        "noms": ",".join(d.get("passenger_names", [])),
        "mineurs": d.get("minors_count", 0),
        "source": "whatsapp_bot"
    }
    query = "&".join([f"{k}={requests.utils.quote(str(v))}" for k, v in params.items() if v])
    mandat_url = f"{MANDAT_BASE_URL}?{query}"
    
    names_str = "\n".join([f"  - {n}" for n in d.get("passenger_names", [])]) if d.get("passenger_names") else "  - A completer"
    
    if lang == "en":
        body = f"""🎉 PERFECT! Here's your file:

✈️ Flight: {d.get('flight_number', '?')} ({d.get('airline', '?')})
📅 Date: {d.get('flight_date', '?')}
👥 Passengers: {pax}
{names_str}
👶 Minors: {d.get('minors_count', 0)}
⚠️ Incident: {incident}

💰 TOTAL: {total} EUR
✅ NET FOR YOU: {net} EUR

👇 Sign your mandate (3 min, all info pre-filled):
{mandat_url}"""
    else:
        body = f"""🎉 PARFAIT ! Voici votre dossier :

✈️ Vol : {d.get('flight_number', '?')} ({d.get('airline', '?')})
📅 Date : {d.get('flight_date', '?')}
👥 Passagers : {pax}
{names_str}
👶 Mineurs : {d.get('minors_count', 0)}
⚠️ Incident : {incident}

💰 TOTAL : {total} EUR
✅ NET POUR VOUS : {net} EUR

👇 Signez votre mandat (3 min, infos pre-remplies) :
{mandat_url}"""
    
    send_whatsapp_text(phone, body)
    
    # Reset apres envoi
    conv["current_step"] = "completed"

# ============================================
# TRAITEMENT DES REPONSES (boutons + texte)
# ============================================

def process_button_reply(phone, button_id, button_title, conv):
    """Traite la reponse a un bouton/liste"""
    print(f"Bouton clique: {button_id} = {button_title}")
    
    button_id = (button_id or "").strip()
    button_title = (button_title or "").strip().lower()

    # Fallback titre -> id (utile quand WATI renvoie le texte mais pas l'id).
    if not button_id and button_title:
        if "1 passager" in button_title or "1 passenger" in button_title:
            button_id = "pax_1"
        elif "2 passager" in button_title or "2 passenger" in button_title:
            button_id = "pax_2"
        elif "3 passager" in button_title or "3 passenger" in button_title:
            button_id = "pax_3"
        elif "4 passager" in button_title or "4 passenger" in button_title:
            button_id = "pax_4"
        elif "5 passager" in button_title or "5 passenger" in button_title:
            button_id = "pax_5"
        elif "6 ou plus" in button_title or "6 or more" in button_title:
            button_id = "pax_more"

    # PASSAGERS
    if button_id.startswith("pax_"):
        if button_id == "pax_more":
            send_whatsapp_text(phone, "🙏 Pour les groupes de 6+, Climbie vous appelle directement.\n\n📱 +33 7 56 86 36 30\n\nOu remplissez : 👉 robindesairs.eu/depot-express")
            return
        conv["data"]["passengers"] = int(button_id.split("_")[1])
        conv["current_step"] = "incident_type"
        ask_incident_type(phone, conv)
        return
    
    # TYPE INCIDENT
    if button_id.startswith("inc_"):
        mapping = {"inc_delay": "delay", "inc_cancel": "cancel", "inc_denied": "denied"}
        conv["data"]["incident_type"] = mapping.get(button_id, "delay")
        conv["current_step"] = "flight_type"
        ask_flight_type(phone, conv)
        return
    
    # TYPE VOL
    if button_id.startswith("type_"):
        mapping = {"type_direct": "direct", "type_connection": "connection"}
        conv["data"]["flight_type"] = mapping.get(button_id, "direct")
        conv["current_step"] = "airline"
        ask_airline(phone, conv)
        return
    
    # COMPAGNIE
    if button_id.startswith("air_"):
        airlines_map = {
            "air_af": "Air France", "air_klm": "KLM",
            "air_brussels": "Brussels Airlines", "air_lufthansa": "Lufthansa",
            "air_tap": "TAP Portugal", "air_corsair": "Corsair",
            "air_airsenegal": "Air Senegal", "air_ram": "Royal Air Maroc"
        }
        if button_id == "air_other":
            lang = conv["data"]["language"]
            msg = "✍️ Tapez le nom de votre compagnie aerienne :" if lang == "fr" else "✍️ Type your airline name:"
            send_whatsapp_text(phone, msg)
            conv["current_step"] = "airline_other_input"
            return
        conv["data"]["airline"] = airlines_map.get(button_id, "Inconnue")
        conv["current_step"] = "flight_number"
        ask_flight_number(phone, conv)
        return
    
    # ANNEE
    if button_id.startswith("year_"):
        if button_id == "year_other":
            send_whatsapp_text(phone, "😔 Desole, la retroactivite est de 5 ans maximum.\n\nVotre vol est trop ancien pour etre indemnise.\n\n👉 robindesairs.eu/blog")
            return
        year = button_id.split("_")[1]
        # Demander le mois
        conv["data"]["temp_year"] = year
        conv["current_step"] = "flight_month"
        ask_flight_month(phone, conv)
        return
    
    # MOIS
    if button_id.startswith("month_"):
        month = button_id.split("_")[1]
        year = conv["data"].get("temp_year", str(datetime.now().year))
        conv["data"]["temp_month"] = month
        conv["current_step"] = "flight_day"
        ask_flight_day(phone, conv)
        return
    
    # JOUR
    if button_id.startswith("day_"):
        day = button_id.split("_")[1]
        year = conv["data"].get("temp_year", "")
        month = conv["data"].get("temp_month", "")
        conv["data"]["flight_date"] = f"{day}/{month}/{year}"
        conv["current_step"] = "passenger_names"
        ask_passenger_names(phone, conv)
        return
    
    # MINEURS
    if button_id == "minor_no":
        conv["data"]["has_minors"] = False
        conv["data"]["minors_count"] = 0
        conv["current_step"] = "summary"
        show_summary_and_mandat(phone, conv)
        return
    
    if button_id == "minor_self":
        # Mineur seul - escalade
        send_whatsapp_text(phone, "👶 Pour un mineur seul, un parent doit signer le mandat.\n\n📱 Climbie vous appelle : +33 7 56 86 36 30")
        return
    
    if button_id == "minor_yes":
        conv["data"]["has_minors"] = True
        conv["current_step"] = "minors_count"
        ask_minors_count(phone, conv)
        return
    
    if button_id.startswith("minors_count_"):
        count = int(button_id.split("_")[2])
        conv["data"]["minors_count"] = count
        conv["current_step"] = "summary"
        show_summary_and_mandat(phone, conv)
        return

def ask_flight_month(phone, conv):
    """Sous-etape : choisir le mois"""
    lang = conv["data"]["language"]
    body = "📅 Quel mois ?" if lang == "fr" else "📅 Which month?"
    
    months_fr = ["Jan", "Fev", "Mars", "Avril", "Mai", "Juin", "Juil", "Aout", "Sept", "Oct", "Nov", "Dec"]
    months_en = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    months = months_fr if lang == "fr" else months_en
    
    rows = [{"id": f"month_{i+1:02d}", "title": months[i]} for i in range(12)]
    sections = [{"title": "Mois" if lang == "fr" else "Month", "rows": rows[:10]}]
    send_whatsapp_list(phone, body, "Choisir mois", sections)

def ask_flight_day(phone, conv):
    """Sous-etape : choisir le jour (par tranches)"""
    lang = conv["data"]["language"]
    body = "📅 Quel jour ?" if lang == "fr" else "📅 Which day?"
    
    # Decoupage en 4 tranches pour respecter limite 10 options
    rows = [
        {"id": "day_01", "title": "1-7", "description": "Debut du mois"},
        {"id": "day_08", "title": "8-14"},
        {"id": "day_15", "title": "15-21"},
        {"id": "day_22", "title": "22-31", "description": "Fin du mois"}
    ]
    # En realite il faudrait 2 niveaux. Pour simplifier, on demande direct le jour en texte
    msg = "📅 Tapez le jour exact (1-31) :" if lang == "fr" else "📅 Type the exact day (1-31):"
    send_whatsapp_text(phone, msg)
    conv["current_step"] = "flight_day_input"

# ============================================
# OPENAI - CONVERSATION LIBRE (hors flux guide)
# ============================================

def call_openai(phone, user_message, image_data=None):
    try:
        conv = get_or_create_conversation(phone)
        
        if image_data:
            user_content = [
                {"type": "text", "text": user_message or "Voici ma carte d'embarquement"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_data}"}}
            ]
            conv["messages"].append({"role": "user", "content": user_content})
        else:
            conv["messages"].append({"role": "user", "content": user_message})
        
        if len(conv["messages"]) > 20:
            conv["messages"] = conv["messages"][-20:]
        
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        
        # Contexte donnees collectees
        data_context = "\n\nDONNEES DEJA COLLECTEES:\n"
        for k, v in conv["data"].items():
            if v and k != "language":
                data_context += f"- {k}: {v}\n"
        if any(v for k, v in conv["data"].items() if k != "language"):
            messages[0]["content"] += data_context
        
        messages.extend(conv["messages"])
        
        model = "gpt-4o" if image_data else "gpt-4o-mini"
        
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={"model": model, "messages": messages, "max_tokens": 400, "temperature": 0.7},
            timeout=45
        )
        data = response.json()
        
        if "choices" in data:
            text = data["choices"][0]["message"]["content"].strip()
            conv["messages"].append({"role": "assistant", "content": text})
            return text
        return None
    except Exception as e:
        print(f"OpenAI exception: {e}")
        return None

def detect_language(text):
    """Detecte si le message est en anglais"""
    text_lower = text.lower()
    en_words = ["hello", "hi", "the", "my", "flight", "delay", "delayed", "cancel", "what", "how", "yes", "no", "thanks"]
    fr_words = ["bonjour", "salut", "le", "mon", "vol", "retard", "annul", "que", "comment", "oui", "non", "merci"]
    
    en_count = sum(1 for w in en_words if w in text_lower.split())
    fr_count = sum(1 for w in fr_words if w in text_lower.split())
    
    return "en" if en_count > fr_count else "fr"

# ============================================
# WEBHOOK PRINCIPAL
# ============================================

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.json
        if not data:
            return jsonify({"status": "no data"}), 200

        try:
            print("[WEBHOOK_DEBUG] meta=", json.dumps({
                "top_keys": list(data.keys())[:30],
                "has_buttonReply": bool(data.get("buttonReply")),
                "has_interactiveButtonReply": bool(data.get("interactiveButtonReply")),
                "has_listReply": bool(data.get("listReply")),
                "has_interactive": bool(data.get("interactive")),
                "has_button_reply": bool(data.get("button_reply")),
                "has_list_reply": bool(data.get("list_reply")),
            }, ensure_ascii=False))
        except Exception:
            pass

        phone = data.get("waId") or data.get("from") or data.get("phone")
        if not phone:
            return jsonify({"status": "no phone"}), 200

        if data.get("owner") == True:
            return jsonify({"status": "ignored own"}), 200

        if is_duplicate_message(phone):
            return jsonify({"status": "duplicate"}), 200

        conv = get_or_create_conversation(phone)
        
        # ===== DETECTION CLIC SUR BOUTON OU LISTE =====
        # WATI peut varier les clés selon le type de message / version webhook.
        button_reply = (
            data.get("buttonReply")
            or data.get("interactiveButtonReply")
            or data.get("interactive", {}).get("button_reply")
            or data.get("button_reply")
        )
        list_reply = (
            data.get("listReply")
            or data.get("interactiveListReply")
            or data.get("interactive", {}).get("list_reply")
            or data.get("list_reply")
        )
        
        if button_reply:
            try:
                print("[WEBHOOK_DEBUG] button_reply=", json.dumps(button_reply, ensure_ascii=False))
            except Exception:
                pass
            btn_id = (
                button_reply.get("id")
                or button_reply.get("buttonId")
                or button_reply.get("payload")
                or button_reply.get("rowId")
                or ""
            )
            btn_title = (
                button_reply.get("title")
                or button_reply.get("text")
                or button_reply.get("body")
                or ""
            )
            process_button_reply(phone, btn_id, btn_title, conv)
            return jsonify({"status": "button processed"}), 200
        
        if list_reply:
            try:
                print("[WEBHOOK_DEBUG] list_reply=", json.dumps(list_reply, ensure_ascii=False))
            except Exception:
                pass
            row_id = (
                list_reply.get("id")
                or list_reply.get("rowId")
                or list_reply.get("buttonId")
                or list_reply.get("payload")
                or ""
            )
            row_title = (
                list_reply.get("title")
                or list_reply.get("text")
                or list_reply.get("description")
                or ""
            )
            process_button_reply(phone, row_id, row_title, conv)
            return jsonify({"status": "list processed"}), 200
        
        # ===== MESSAGE TEXTE OU IMAGE =====
        message_type = data.get("type", "text")
        image_data = None
        message_text = ""

        if message_type == "image" or "image" in data:
            print(f"Image recue de {phone}")
            media_url = data.get("data") or data.get("mediaUrl")
            if media_url:
                try:
                    headers = {"Authorization": f"Bearer {WATI_API_TOKEN}"}
                    r = requests.get(media_url, headers=headers, timeout=30)
                    if r.status_code == 200:
                        image_data = base64.b64encode(r.content).decode('utf-8')
                except Exception as e:
                    print(f"Erreur download: {e}")
            message_text = data.get("caption", "") or "Voici ma carte d'embarquement"
        else:
            if "text" in data:
                message_text = data["text"].get("body", "") if isinstance(data["text"], dict) else data["text"]
            elif "body" in data:
                message_text = data["body"]

        if not message_text and not image_data:
            return jsonify({"status": "ignored empty"}), 200

        print(f"Message de {phone}: {message_text[:80]} (image: {bool(image_data)})")
        
        # Detecter langue au premier message
        if not conv["data"].get("language") or conv["data"]["language"] == "fr":
            conv["data"]["language"] = detect_language(message_text)
        
        # ===== GESTION DES SAISIES TEXTE PENDANT FLUX GUIDE =====
        current_step = conv.get("current_step")
        
        # Saisie compagnie "Autre"
        if current_step == "airline_other_input":
            conv["data"]["airline"] = message_text.strip()
            conv["current_step"] = "flight_number"
            ask_flight_number(phone, conv)
            return jsonify({"status": "ok"}), 200
        
        # Saisie numero de vol
        if current_step == "flight_number":
            # Si image envoyee, GPT-4o vision lit la carte
            if image_data:
                response = call_openai(phone, "Extrait de cette carte d'embarquement: numero de vol, date, nom passager, compagnie. Reponds en JSON: {flight_number:..., date:..., name:..., airline:...}", image_data)
                if response:
                    # Tentative extraction JSON
                    try:
                        json_match = re.search(r'\{[^}]+\}', response)
                        if json_match:
                            extracted = json.loads(json_match.group())
                            if extracted.get("flight_number"):
                                conv["data"]["flight_number"] = extracted["flight_number"]
                            if extracted.get("date"):
                                conv["data"]["flight_date"] = extracted["date"]
                            send_whatsapp_text(phone, f"📸 Carte lue !\n\n✈️ Vol : {conv['data'].get('flight_number', '?')}\n📅 Date : {conv['data'].get('flight_date', '?')}\n\nOn continue 👇")
                            conv["current_step"] = "passenger_names"
                            ask_passenger_names(phone, conv)
                            return jsonify({"status": "ok"}), 200
                    except:
                        pass
            
            # Saisie texte du numero de vol
            flight_match = re.search(r'\b([A-Z]{2}\d{2,4})\b', message_text.upper())
            if flight_match:
                conv["data"]["flight_number"] = flight_match.group(1)
                conv["current_step"] = "flight_date"
                ask_flight_date(phone, conv)
            else:
                conv["data"]["flight_number"] = message_text.strip()
                conv["current_step"] = "flight_date"
                ask_flight_date(phone, conv)
            return jsonify({"status": "ok"}), 200
        
        # Saisie jour
        if current_step == "flight_day_input":
            day_match = re.search(r'\b(\d{1,2})\b', message_text)
            if day_match:
                day = day_match.group(1).zfill(2)
                year = conv["data"].get("temp_year", "")
                month = conv["data"].get("temp_month", "")
                conv["data"]["flight_date"] = f"{day}/{month}/{year}"
                conv["current_step"] = "passenger_names"
                ask_passenger_names(phone, conv)
            else:
                send_whatsapp_text(phone, "📅 Tapez juste le numero du jour (ex: 15)")
            return jsonify({"status": "ok"}), 200
        
        # Saisie noms des passagers
        if current_step == "passenger_names":
            # Extraction des noms (formats varies)
            lines = [l.strip() for l in message_text.split("\n") if l.strip()]
            names = []
            for line in lines:
                clean = re.sub(r'^[\d\.\)\-\s]+', '', line).strip()
                if len(clean) >= 3 and not clean.isdigit():
                    names.append(clean)
            
            if names:
                conv["data"]["passenger_names"] = names
                conv["current_step"] = "minor_check"
                ask_minors(phone, conv)
            else:
                lang = conv["data"]["language"]
                msg = "👤 Envoyez les noms comme ca :\n1. Jean Dupont\n2. Marie Dupont" if lang == "fr" else "👤 Send names like:\n1. John Doe\n2. Jane Doe"
                send_whatsapp_text(phone, msg)
            return jsonify({"status": "ok"}), 200
        
        # ===== DEMARRAGE DU FLUX GUIDE =====
        # Mots-cles qui declenchent le flux
        trigger_words = ["vol", "retard", "annul", "indemn", "flight", "delay", "cancel", "compensation", "claim", 
                        "bonjour", "hello", "salut", "hi", "start", "commencer", "demarrer", "menu"]
        
        is_trigger = any(w in message_text.lower() for w in trigger_words)
        
        # Si pas encore demarre OU si client tape un mot-cle de demarrage
        if conv.get("current_step") is None or current_step == "completed":
            if is_trigger or len(message_text) < 50:
                # Lancer le flux guide depuis le debut
                conv["current_step"] = "passengers"
                ask_passengers(phone, conv["data"]["language"])
                return jsonify({"status": "flow started"}), 200
        
        # ===== SINON, REPONSE LIBRE VIA OPENAI =====
        response = call_openai(phone, message_text, image_data)
        
        if not response:
            response = "Bonjour ! 😊\n\nJe suis Robin des Airs.\n\nTapez 'menu' pour demarrer la verification 👇\n\n👉 robindesairs.eu"
        
        send_whatsapp_text(phone, response)
        return jsonify({"status": "ok"}), 200

    except Exception as e:
        print(f"Erreur webhook: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error"}), 500

# ============================================
# ENDPOINTS UTILITAIRES
# ============================================

@app.route("/test_flow/<phone>", methods=["GET"])
def test_flow(phone):
    """Demarre le flux guide pour un numero"""
    conv = get_or_create_conversation(phone)
    conv["current_step"] = "passengers"
    conv["data"]["language"] = "fr"
    ask_passengers(phone, "fr")
    return jsonify({"status": "flow started", "phone": phone}), 200

@app.route("/conversations", methods=["GET"])
def list_conversations():
    result = {}
    for phone, conv in conversations.items():
        result[phone] = {
            "step": conv.get("current_step"),
            "data": conv["data"],
            "messages": len(conv["messages"]),
            "created": conv["created"].isoformat()
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
        "version": "v4 - boutons interactifs + flux guide + mandat pre-rempli",
        "features": [
            "Boutons interactifs Wati (passagers, incident, type vol, compagnie)",
            "Liste interactive (annee, mois, compagnie)",
            "Lecture cartes d'embarquement (GPT-4o Vision)",
            "Memoire conversation 24h",
            "Detection langue auto FR/EN",
            "Anti-doublons",
            "Mandat pre-rempli avec parametres URL",
            "Gestion mineurs/majeurs",
            "Escalade automatique pour cas complexes"
        ],
        "active_conversations": len(conversations)
    }), 200

@app.route("/", methods=["GET"])
def home():
    return "Robin des Airs Bot v4 - Running!", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
