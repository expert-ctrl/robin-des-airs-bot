from flask import Flask, request, jsonify
import requests
import os

app = Flask(__name__)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
WATI_API_TOKEN = os.environ.get("WATI_API_TOKEN", "")
WATI_BASE_URL = os.environ.get("WATI_BASE_URL", "")

SYSTEM_PROMPT = """Tu es l'agent IA chaleureux de Robin des Airs, cabinet parisien base au 66 avenue des Champs-Elysees, Paris. Fondateur: Climbie. Avocate: Maitre Joyce Pitcher, Barreau de Paris.

REGLES FORMAT - OBLIGATOIRES DANS CHAQUE MESSAGE:
1. Minimum 3 emojis par message - toujours
2. Bullet points avec emojis: 1️⃣ 2️⃣ 3️⃣ pour etapes, ✅ 💰 ✈️ pour listes
3. Maximum 6 lignes par message
4. Ligne vide entre chaque paragraphe
5. Toujours finir par: 👉 robindesairs.eu/mandat
6. Ton chaleureux comme un ami parisien

EXEMPLE PARFAIT:
Question: "Combien je peux recuperer?"
Reponse:
"Bonne nouvelle ! 🎉

Pour les vols Europe-Afrique:
💰 600 EUR par passager
💰 ~40 000 D (Gambie)
💰 ~390 000 FCFA (Senegal/CI)

✅ Vous touchez 75% net
🚫 Zero frais si on perd

👉 robindesairs.eu/mandat"

INFOS CLES CE 261:
- Retard plus de 3h = 600 EUR par passager
- Annulation moins de 14 jours = 600 EUR
- Refus embarquement = 600 EUR + remboursement
- Retroactivite 5 ANS depuis 2021
- Brussels Airlines (priorite Gambie), Air France, KLM, TAP, British Airways, Lufthansa, Iberia, Vueling
- Turkish/Emirates/Qatar: eligible si vol depart d'Europe seulement
- Commission: 25% si on gagne, ZERO si on perd
- Net passager: 75% = environ 450 EUR sur 600 EUR
- Delai: 6 a 12 semaines
- Lien mandat: robindesairs.eu/mandat
- WhatsApp Climbie: +33 7 56 86 36 30"""

def call_openai(user_message):
    try:
        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message}
            ],
            "max_tokens": 350,
            "temperature": 0.7
        }
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        data = response.json()
        print(f"OpenAI status: {response.status_code}")
        if "choices" in data:
            text = data["choices"][0]["message"]["content"].strip()
            print(f"OpenAI succes: {text[:80]}")
            return text
        return None
    except Exception as e:
        print(f"OpenAI exception: {e}")
        return None

def send_whatsapp_message(phone_number, message):
    """Wati sendSessionMessage - messageText est un QUERY PARAMETER, pas dans le body"""
    message = message.strip()
    if not message:
        return 0
    
    # IMPORTANT : messageText va dans l'URL comme query param
    url = f"{WATI_BASE_URL}/api/v1/sendSessionMessage/{phone_number}"
    
    headers = {
        "Authorization": f"Bearer {WATI_API_TOKEN}",
        "accept": "*/*"
    }
    
    # messageText comme query parameter
    params = {"messageText": message}
    
    print(f"Envoi Wati - longueur: {len(message)} chars")
    
    response = requests.post(url, headers=headers, params=params, timeout=30)
    print(f"Wati: {response.status_code} - {response.text[:200]}")
    return response.status_code

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.json
        if not data:
            return jsonify({"status": "no data"}), 200

        phone = data.get("waId") or data.get("from") or data.get("phone")
        message_text = None

        if "text" in data:
            message_text = data["text"].get("body", "") if isinstance(data["text"], dict) else data["text"]
        elif "body" in data:
            message_text = data["body"]

        if not phone or not message_text or not message_text.strip():
            return jsonify({"status": "ignored"}), 200

        if data.get("owner") == True:
            return jsonify({"status": "ignored own"}), 200

        print(f"Message recu de {phone}: {message_text}")

        response = call_openai(message_text)

        if not response or not response.strip():
            response = "Bonjour ! 😊\n\nJe suis Robin des Airs.\n\n👉 robindesairs.eu/mandat"

        send_whatsapp_message(phone, response)
        return jsonify({"status": "ok"}), 200

    except Exception as e:
        print(f"Erreur: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error"}), 500

@app.route("/test_wati", methods=["GET"])
def test_wati():
    result = send_whatsapp_message("33677470122", "Test Robin des Airs ✅\n\nLe bot fonctionne ! 🎉\n\n👉 robindesairs.eu/mandat")
    return jsonify({"wati_status": result}), 200

@app.route("/test", methods=["GET"])
def test():
    test_response = call_openai("Dis bonjour en 1 ligne avec un emoji")
    return jsonify({
        "status": "running",
        "openai_test": test_response[:80] if test_response else "FAILED"
    }), 200

@app.route("/", methods=["GET"])
def home():
    return "Robin des Airs AI Bot - Running!", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
