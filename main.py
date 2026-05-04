from flask import Flask, request, jsonify
import requests
import os

app = Flask(__name__)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
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

def call_gemini(user_message):
    # Modeles valides et stables en 2026 - API v1 plus stable
    models = [
        "gemini-3-flash",
        "gemini-1.5-flash",
        "gemini-1.5-pro"
    ]
    
    for model in models:
        try:
            # API v1 (pas v1beta)
            url = f"https://generativelanguage.googleapis.com/v1/models/{model}:generateContent?key={GEMINI_API_KEY}"
            payload = {
                "contents": [{
                    "role": "user",
                    "parts": [{"text": f"{SYSTEM_PROMPT}\n\nQuestion du client: {user_message}"}]
                }],
                "generationConfig": {
                    "temperature": 0.7,
                    "maxOutputTokens": 350
                }
            }
            response = requests.post(url, json=payload, timeout=30)
            data = response.json()
            print(f"Gemini {model} - Status: {response.status_code}")
            
            if response.status_code == 200 and "candidates" in data:
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                print(f"Succes avec: {model}")
                return text
            else:
                print(f"Erreur {model}: {str(data)[:150]}")
        except Exception as e:
            print(f"Exception {model}: {e}")
            continue
    
    return None

def send_whatsapp_message(phone_number, message):
    url = f"{WATI_BASE_URL}/api/v1/sendSessionMessage/{phone_number}"
    headers = {
        "Authorization": f"Bearer {WATI_API_TOKEN}",
        "Content-Type": "application/json"
    }
    response = requests.post(url, headers=headers, json={"messageText": message}, timeout=30)
    print(f"Wati: {response.status_code} - {response.text[:100]}")
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

        if not phone or not message_text:
            return jsonify({"status": "ignored"}), 200

        if data.get("owner") == True:
            return jsonify({"status": "ignored own message"}), 200

        print(f"Message recu de {phone}: {message_text}")

        response = call_gemini(message_text)

        if not response:
            response = "Bonjour ! 😊\n\nJe suis l'assistant Robin des Airs.\n\nPour recuperer jusqu'a 600 EUR:\n👉 robindesairs.eu/mandat\n\nOu Climbie: +33 7 56 86 36 30"

        print(f"Reponse: {response[:100]}")
        send_whatsapp_message(phone, response)

        return jsonify({"status": "ok"}), 200

    except Exception as e:
        print(f"Erreur: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/test", methods=["GET"])
def test():
    test_response = call_gemini("Dis bonjour en 1 ligne avec un emoji")
    return jsonify({
        "status": "Robin des Airs Bot is running!",
        "gemini_key": "configured" if GEMINI_API_KEY else "MISSING",
        "wati_token": "configured" if WATI_API_TOKEN else "MISSING",
        "wati_url": WATI_BASE_URL,
        "gemini_test": test_response[:100] if test_response else "FAILED"
    }), 200

@app.route("/", methods=["GET"])
def home():
    return "Robin des Airs AI Bot - Running!", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
