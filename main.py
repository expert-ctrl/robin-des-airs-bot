from flask import Flask, request, jsonify
import requests
import os
import json
import base64
from datetime import datetime, timedelta

app = Flask(__name__)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
WATI_API_TOKEN = os.environ.get("WATI_API_TOKEN", "")
WATI_BASE_URL = os.environ.get("WATI_BASE_URL", "")

# ===== MEMOIRE DES CONVERSATIONS =====
# Stocke historique + donnees collectees par numero
conversations = {}
last_message_time = {}  # Pour eviter messages multiples
MEMORY_HOURS = 24  # Garder l'historique 24h
MIN_SECONDS_BETWEEN = 2  # Anti-spam

SYSTEM_PROMPT = """Tu es l'agent IA de ROBIN DES AIRS, cabinet parisien specialise dans la justice aerienne pour la diaspora africaine.

═══════════════════════════════════════
INFOS ENTREPRISE
═══════════════════════════════════════
- Robin des Airs, 66 av. des Champs-Elysees, Paris 🇫🇷
- Site: robindesairs.eu
- WhatsApp Climbie: +33 7 56 86 36 30

═══════════════════════════════════════
LIENS A UTILISER SELON CONTEXTE
═══════════════════════════════════════
- Calculateur: robindesairs.eu/#funnel-box
- Mandat: robindesairs.eu/mandat-representation
- Depot dossier: robindesairs.eu/depot-express
- Suivi: robindesairs.eu/suivi-dossier
- Tarifs: robindesairs.eu/nos-tarifs
- Blog: robindesairs.eu/blog
- Parrainage 20 EUR: robindesairs.eu/parrainage

═══════════════════════════════════════
REGLES FORMAT - OBLIGATOIRES
═══════════════════════════════════════
1. Minimum 3 emojis par message
2. Bullet points avec emojis (✅ 💰 ✈️ 1️⃣ 2️⃣ 3️⃣)
3. Maximum 6 lignes par message
4. Ligne vide entre paragraphes
5. Toujours finir par un lien d'action
6. Ton chaleureux comme un ami

═══════════════════════════════════════
LANGUE - REGLE ABSOLUE
═══════════════════════════════════════
DETECTE LA LANGUE DU CLIENT et reponds DANS LA MEME LANGUE.
- Client ecrit en anglais → REPONDS EN ANGLAIS
- Client ecrit en francais → reponds en francais
- Client ecrit en wolof/bambara → reponds dans cette langue
NE JAMAIS melanger ou changer de langue en cours de conversation.

═══════════════════════════════════════
COLLECTE D'INFOS - ORDRE PRIORITAIRE
═══════════════════════════════════════
Tu dois collecter ces 5 infos pour creer le dossier:
1. NUMERO DE VOL (ex: AF718, SN271)
2. DATE DU VOL (jj/mm/aaaa)
3. NOMBRE DE PASSAGERS (TRES IMPORTANT - x600 EUR)
4. TYPE D'INCIDENT (retard +3h / annulation / refus embarquement)
5. NOM DE LA COMPAGNIE (Air France, Brussels Airlines, etc.)

POSE UNE SEULE QUESTION A LA FOIS.
Quand le client donne une info, RECONFIRME-LA et passe a la suivante.

QUAND TU AS LE NOMBRE DE PASSAGERS:
Calcule TOUJOURS le total: 600 EUR x nombre de passagers.
Exemple 3 personnes:
"Genial ! Pour 3 passagers c'est 1800 EUR au total 💰
Net pour vous: 1350 EUR"

═══════════════════════════════════════
ANALYSE DE CARTE D'EMBARQUEMENT
═══════════════════════════════════════
Si le client envoie une PHOTO de carte d'embarquement, EXTRAIT:
- Numero de vol
- Date
- Nom du passager
- Compagnie
- Itineraire

Reponds en confirmant: "Parfait, j'ai votre carte ✅
Vol: [numero]
Date: [date]
[autres infos]
Combien etiez-vous au total sur ce vol ? 👥"

═══════════════════════════════════════
INFOS CE 261
═══════════════════════════════════════
- Retard +3h a l'arrivee = 600 EUR par passager
- Annulation moins de 14 jours = 600 EUR
- Refus embarquement = 600 EUR + remboursement
- Retroactivite 5 ANS
- Vol +3500 km Europe-Afrique = 600 EUR
- Vol moyen courrier = 400 EUR
- Vol court = 250 EUR
- Net passager: 75% (450 EUR sur 600 EUR)
- Notre commission: 25% UNIQUEMENT si succes
- Delai: 4-12 semaines
- 9/10 dossiers gagnes
- Zero frais si on perd

COMPAGNIES:
- Europe → Afrique: TOUTES eligibles
- Afrique → Europe: SEULEMENT compagnies europeennes (Air France, KLM, Brussels Airlines, Lufthansa, TAP, Iberia)
- Royal Air Maroc, Air Senegal, Ethiopian: NON eligibles sur Afrique→Europe

═══════════════════════════════════════
QUAND TU AS TOUTES LES INFOS
═══════════════════════════════════════
Recapitule et propose:
"Parfait ! Recapitulons votre dossier 📋

✈️ Vol [numero] du [date]
👥 [X] passagers
💰 Total potentiel: [600 x X] EUR
✅ Net pour vous: [450 x X] EUR

Lancez votre dossier en 3 minutes:
👉 robindesairs.eu/depot-express"

═══════════════════════════════════════
ESCALADER A CLIMBIE (+33 7 56 86 36 30)
═══════════════════════════════════════
- Deces / Heritage
- Plus de 5 passagers
- Client tres mecontent
- Question juridique complexe
- Hajj groupe
"""

def get_or_create_conversation(phone):
    """Recupere ou cree l'historique d'une conversation"""
    if phone not in conversations:
        conversations[phone] = {
            "messages": [],
            "data": {
                "flight_number": None,
                "flight_date": None,
                "passengers": None,
                "incident_type": None,
                "airline": None,
                "language": None
            },
            "created": datetime.now()
        }
    
    # Nettoyage des vieilles conversations
    if (datetime.now() - conversations[phone]["created"]) > timedelta(hours=MEMORY_HOURS):
        conversations[phone] = {
            "messages": [],
            "data": {
                "flight_number": None, "flight_date": None,
                "passengers": None, "incident_type": None,
                "airline": None, "language": None
            },
            "created": datetime.now()
        }
    
    return conversations[phone]

def call_openai_with_memory(phone, user_message, image_data=None):
    """Appel OpenAI avec memoire de la conversation"""
    try:
        conv = get_or_create_conversation(phone)
        
        # Ajouter le message utilisateur
        if image_data:
            user_content = [
                {"type": "text", "text": user_message or "Voici ma carte d'embarquement"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_data}"}}
            ]
            conv["messages"].append({"role": "user", "content": user_content})
        else:
            conv["messages"].append({"role": "user", "content": user_message})
        
        # Limiter l'historique a 20 derniers messages pour eviter trop de tokens
        if len(conv["messages"]) > 20:
            conv["messages"] = conv["messages"][-20:]
        
        # Construire les messages pour OpenAI
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        
        # Ajouter les donnees collectees comme contexte
        data_context = "\n\nDONNEES DEJA COLLECTEES SUR CE CLIENT:\n"
        for k, v in conv["data"].items():
            if v:
                data_context += f"- {k}: {v}\n"
        if any(conv["data"].values()):
            messages[0]["content"] += data_context
        
        messages.extend(conv["messages"])
        
        # Choisir le modele (vision si image)
        model = "gpt-4o-mini" if not image_data else "gpt-4o"
        
        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": 400,
            "temperature": 0.7
        }
        
        response = requests.post(url, headers=headers, json=payload, timeout=45)
        data = response.json()
        print(f"OpenAI status: {response.status_code} (model: {model})")
        
        if "choices" in data:
            text = data["choices"][0]["message"]["content"].strip()
            
            # Sauvegarder reponse dans l'historique
            conv["messages"].append({"role": "assistant", "content": text})
            
            # Extraire infos automatiquement
            extract_data_from_message(conv, user_message if not image_data else "image carte embarquement", text)
            
            print(f"OpenAI succes: {text[:80]}")
            print(f"Donnees collectees: {conv['data']}")
            return text
        else:
            print(f"OpenAI erreur: {str(data)[:150]}")
            return None
    except Exception as e:
        print(f"OpenAI exception: {e}")
        import traceback
        traceback.print_exc()
        return None

def extract_data_from_message(conv, user_msg, ai_response):
    """Extrait automatiquement les donnees du message"""
    import re
    combined = (user_msg or "") + " " + (ai_response or "")
    
    # Numero de vol (2 lettres + 2-4 chiffres)
    flight_match = re.search(r'\b([A-Z]{2}\d{2,4})\b', combined.upper())
    if flight_match and not conv["data"]["flight_number"]:
        conv["data"]["flight_number"] = flight_match.group(1)
    
    # Date (formats divers)
    date_patterns = [
        r'\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b',
        r'\b(\d{1,2}\s+(?:janvier|fevrier|mars|avril|mai|juin|juillet|aout|septembre|octobre|novembre|decembre)\s+\d{4})\b'
    ]
    for pattern in date_patterns:
        date_match = re.search(pattern, combined.lower())
        if date_match and not conv["data"]["flight_date"]:
            conv["data"]["flight_date"] = date_match.group(1)
            break
    
    # Nombre de passagers
    pax_patterns = [
        r'(\d+)\s*(?:passagers?|personnes?|adultes?|pax)',
        r'(?:nous etions|on etait|we were)\s*(\d+)',
        r'famille de\s*(\d+)'
    ]
    for pattern in pax_patterns:
        pax_match = re.search(pattern, combined.lower())
        if pax_match and not conv["data"]["passengers"]:
            conv["data"]["passengers"] = int(pax_match.group(1))
            break
    
    # Type d'incident
    if not conv["data"]["incident_type"]:
        msg_lower = combined.lower()
        if "retard" in msg_lower or "delay" in msg_lower:
            conv["data"]["incident_type"] = "retard"
        elif "annul" in msg_lower or "cancel" in msg_lower:
            conv["data"]["incident_type"] = "annulation"
        elif "refus" in msg_lower or "denied" in msg_lower or "surbook" in msg_lower:
            conv["data"]["incident_type"] = "refus_embarquement"
    
    # Compagnie aerienne
    airlines = {
        "air france": "Air France", "klm": "KLM", "brussels": "Brussels Airlines",
        "lufthansa": "Lufthansa", "tap": "TAP", "iberia": "Iberia",
        "corsair": "Corsair", "air senegal": "Air Senegal",
        "british": "British Airways", "royal air maroc": "Royal Air Maroc",
        "turkish": "Turkish Airlines", "emirates": "Emirates"
    }
    if not conv["data"]["airline"]:
        msg_lower = combined.lower()
        for key, name in airlines.items():
            if key in msg_lower:
                conv["data"]["airline"] = name
                break

def download_wati_media(media_url):
    """Telecharge un media depuis Wati (image, etc.)"""
    try:
        headers = {"Authorization": f"Bearer {WATI_API_TOKEN}"}
        response = requests.get(media_url, headers=headers, timeout=30)
        if response.status_code == 200:
            return base64.b64encode(response.content).decode('utf-8')
    except Exception as e:
        print(f"Erreur download media: {e}")
    return None

def send_whatsapp_message(phone_number, message):
    """Envoie message WhatsApp via Wati"""
    message = message.strip()
    if not message:
        return 0
    
    url = f"{WATI_BASE_URL}/api/v1/sendSessionMessage/{phone_number}"
    headers = {"Authorization": f"Bearer {WATI_API_TOKEN}", "accept": "*/*"}
    params = {"messageText": message}
    
    response = requests.post(url, headers=headers, params=params, timeout=30)
    print(f"Wati: {response.status_code} - {response.text[:150]}")
    return response.status_code

def is_duplicate_message(phone):
    """Evite de traiter plusieurs fois le meme message"""
    now = datetime.now()
    if phone in last_message_time:
        diff = (now - last_message_time[phone]).total_seconds()
        if diff < MIN_SECONDS_BETWEEN:
            return True
    last_message_time[phone] = now
    return False

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.json
        if not data:
            return jsonify({"status": "no data"}), 200

        phone = data.get("waId") or data.get("from") or data.get("phone")
        if not phone:
            return jsonify({"status": "no phone"}), 200

        # Ignorer messages envoyes par le bot lui-meme
        if data.get("owner") == True:
            return jsonify({"status": "ignored own"}), 200

        # Anti-doublon
        if is_duplicate_message(phone):
            print(f"Message dupplique ignore pour {phone}")
            return jsonify({"status": "duplicate"}), 200

        # Detecter si c'est une image
        message_type = data.get("type", "text")
        image_data = None
        message_text = ""

        if message_type == "image" or "image" in data:
            # Image recue - tentative de telechargement
            print(f"Image recue de {phone}")
            media_url = data.get("data") or data.get("mediaUrl") or data.get("image", {}).get("url")
            if media_url:
                image_data = download_wati_media(media_url)
            message_text = data.get("caption", "") or data.get("text", {}).get("body", "") or "Voici ma carte d'embarquement"
        else:
            # Message texte
            if "text" in data:
                message_text = data["text"].get("body", "") if isinstance(data["text"], dict) else data["text"]
            elif "body" in data:
                message_text = data["body"]

        if not message_text and not image_data:
            return jsonify({"status": "ignored empty"}), 200

        print(f"Message recu de {phone}: {message_text[:100]} (image: {bool(image_data)})")

        response = call_openai_with_memory(phone, message_text, image_data)

        if not response:
            response = "Bonjour ! 😊\n\nJe suis Robin des Airs.\n\nVerifiez votre vol:\n👉 robindesairs.eu/#funnel-box"

        send_whatsapp_message(phone, response)
        return jsonify({"status": "ok"}), 200

    except Exception as e:
        print(f"Erreur webhook: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error"}), 500

@app.route("/test", methods=["GET"])
def test():
    test_response = call_openai_with_memory("test_user", "Hello, my flight was delayed")
    return jsonify({
        "status": "running",
        "version": "v3 - memoire + langue + image + anti-doublon",
        "openai_test": test_response[:150] if test_response else "FAILED",
        "active_conversations": len(conversations)
    }), 200

@app.route("/conversations", methods=["GET"])
def list_conversations():
    """Voir les conversations actives"""
    result = {}
    for phone, conv in conversations.items():
        result[phone] = {
            "data": conv["data"],
            "messages_count": len(conv["messages"]),
            "created": conv["created"].isoformat()
        }
    return jsonify(result), 200

@app.route("/reset/<phone>", methods=["GET"])
def reset_conversation(phone):
    """Reset la conversation d'un client"""
    if phone in conversations:
        del conversations[phone]
    return jsonify({"status": "reset", "phone": phone}), 200

@app.route("/", methods=["GET"])
def home():
    return "Robin des Airs AI Bot v3 - Running!", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
