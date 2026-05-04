from flask import Flask, request, jsonify
import requests
import os
import json

app = Flask(__name__)

# ===== CONFIGURATION =====
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "METS_TA_CLE_GEMINI_ICI")
WATI_API_TOKEN = os.environ.get("WATI_API_TOKEN", "METS_TON_TOKEN_WATI_ICI")
WATI_BASE_URL = os.environ.get("WATI_BASE_URL", "https://live-mt-server.wati.io/XXXXXX")

# ===== SYSTEM PROMPT GEMINI =====
SYSTEM_PROMPT = """Tu es l'agent IA chaleureux de Robin des Airs, cabinet parisien base au 66 avenue des Champs-Elysees, Paris. Fondateur: Climbie. Avocate: Maitre Joyce Pitcher, Barreau de Paris.

REGLES FORMAT - OBLIGATOIRES DANS CHAQUE MESSAGE:
1. Minimum 3 emojis par message - toujours
2. Bullet points avec emojis pour toute liste: utiliser 1️⃣ 2️⃣ 3️⃣ pour les etapes, ✅ 💰 ✈️ pour les listes
3. Maximum 6 lignes par message - jamais un gros bloc de texte
4. Une ligne vide entre chaque paragraphe
5. Toujours finir par: 👉 robindesairs.eu/mandat
6. Ton chaleureux - comme un ami parisien qui aide, jamais robotique

EXEMPLE PARFAIT DE REPONSE:
Question: "L'argent va ou?"
Reponse:
"Tres simple ! 😊

1️⃣ La compagnie paie sur notre compte
2️⃣ On preleve nos 25%
3️⃣ On vous reverse 75% 💰

Wave, Orange Money, virement... On s'adapte !

👉 robindesairs.eu/mandat"

OBJECTIF UNIQUE: Guider chaque passager vers la signature du mandat sur robindesairs.eu/mandat

INFOS CLES CE 261:
- Retard plus de 3h = 600 EUR par passager
- Annulation moins de 14 jours = 600 EUR
- Refus embarquement = 600 EUR + remboursement
- Retroactivite 5 ANS (depuis 2021)
- Compagnies eligibles: Brussels Airlines (priorite Gambie), Air France, KLM, TAP, British Airways, Lufthansa, Iberia, Vueling
- Turkish/Emirates/Qatar: eligible UNIQUEMENT si vol depart d'Europe
- Montant Dalasi: environ 40 000 D
- Montant FCFA: environ 390 000 FCFA
- Montant GBP: 520 GBP (vols UK)
- Commission Robin des Airs: 25% UNIQUEMENT si on gagne
- Net passager: 75% = environ 450 EUR sur 600 EUR
- Delai: 6 a 12 semaines apres signature mandat
- Zero frais si on perd

REPONSES TYPES:
Frais: "Zero avance ! 😊\n\n❌ Pas de frais d'inscription\n❌ Pas de frais de dossier\n✅ Juste 25% si on gagne\n✅ 75% pour vous\n\n👉 robindesairs.eu/mandat"
Arnaque: "Non absolument pas ! 😊\n\n🇪🇺 66 av. Champs-Elysees, Paris\n⚖️ Maitre Joyce Pitcher, Barreau Paris\n📜 Loi CE 261 officielle EU 20 ans\n\n👉 robindesairs.eu/mandat"
Mandat: "Super simple ! 5 min max ⏰\n\n1️⃣ Cliquez: robindesairs.eu/mandat\n2️⃣ Vos infos + billet\n3️⃣ Signez avec le doigt\n4️⃣ Email de confirmation ✅\n\n👉 robindesairs.eu/mandat"

ESCALADER A CLIMBIE (+33 7 56 86 36 30) si: deces/heritage, plus de 5 passagers, client tres mecontent, question juridique complexe.
"""

def call_gemini(user_message):
    """Appelle l'API Gemini et retourne la reponse"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    
    payload = {
        "system_instruction": {
            "parts": [{"text": SYSTEM_PROMPT}]
        },
        "contents": [{
            "role": "user",
            "parts": [{"text": user_message}]
        }],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 350
        }
    }
    
    response = requests.post(url, json=payload, timeout=30)
    data = response.json()
    
    if "candidates" in data and len(data["candidates"]) > 0:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    else:
        return "Je suis desole, une erreur s'est produite. Contactez Climbie directement: +33 7 56 86 36 30 👉 robindesairs.eu/mandat"

def send_whatsapp_message(phone_number, message):
    """Envoie un message WhatsApp via Wati"""
    url = f"{WATI_BASE_URL}/api/v1/sendSessionMessage/{phone_number}"
    
    headers = {
        "Authorization": f"Bearer {WATI_API_TOKEN}",
        "Content-Type": "application/json"
    }
    
    payload = {"messageText": message}
    
    response = requests.post(url, headers=headers, json=payload, timeout=30)
    return response.status_code

# ===== WEBHOOK PRINCIPAL =====
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.json
        
        # Extraire le message et le numero de telephone
        if not data:
            return jsonify({"status": "no data"}), 200
        
        # Format Wati webhook
        phone = data.get("waId") or data.get("from") or data.get("phone")
        message_text = None
        
        # Chercher le texte dans differents formats Wati
        if "text" in data:
            if isinstance(data["text"], dict):
                message_text = data["text"].get("body", "")
            else:
                message_text = data["text"]
        elif "body" in data:
            message_text = data["body"]
        
        # Ignorer si pas de message texte ou pas de numero
        if not phone or not message_text:
            return jsonify({"status": "ignored"}), 200
        
        # Ignorer les messages envoyes par le bot lui-meme
        if data.get("type") == "sent":
            return jsonify({"status": "ignored sent"}), 200
        
        print(f"Message recu de {phone}: {message_text}")
        
        # Appeler Gemini
        response = call_gemini(message_text)
        print(f"Reponse Gemini: {response[:100]}...")
        
        # Envoyer la reponse via Wati
        status = send_whatsapp_message(phone, response)
        print(f"Message envoye, status: {status}")
        
        return jsonify({"status": "ok"}), 200
        
    except Exception as e:
        print(f"Erreur: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

# ===== TEST ENDPOINT =====
@app.route("/test", methods=["GET"])
def test():
    """Pour verifier que le serveur fonctionne"""
    return jsonify({
        "status": "Robin des Airs Bot is running!",
        "gemini_key": "configured" if GEMINI_API_KEY != "METS_TA_CLE_GEMINI_ICI" else "MISSING",
        "wati_token": "configured" if WATI_API_TOKEN != "METS_TON_TOKEN_WATI_ICI" else "MISSING"
    }), 200

@app.route("/", methods=["GET"])
def home():
    return "Robin des Airs AI Bot - Running!", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
