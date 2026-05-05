from flask import Flask, request, jsonify
import requests
import os
import time

app = Flask(__name__)

# Configuration
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
WATI_API_TOKEN = os.environ.get("WATI_API_TOKEN", "")
WATI_BASE_URL = os.environ.get("WATI_BASE_URL", "")

# 1. BASE DE CONNAISSANCES EXTRAITE DE ROBINDESAIRS.EU
CONTEXTE_SITE = """
INFOS OFFICIELLES ROBIN DES AIRS:
- Cabinet base au 66 avenue des Champs-Elysees, Paris. 
- Avocate: Maitre Joyce Pitcher. Fondateur: Climbie.
- Mission: Recuperer l'indemnite CE 261/2004 (vols retardes +3h, annules, surbookes).
- Montant: 600€ (env. 390 000 FCFA / 40 000 D).
- Commission: 25% TTC seulement si on gagne. 0€ si on perd.
- Delais: Paiement sous 6 a 12 semaines en moyenne.
- Retroactivite: On remonte jusqu'a 5 ans en arriere (vols depuis 2021).
- Processus: 1. Analyse carte embarquement / 2. Signature mandat / 3. Procedure juridique.
- Langues supportees pour appel: Francais, Anglais, Wolof.
"""

SYSTEM_PROMPT = f"""{CONTEXTE_SITE}

Tu es l'agent expert de Robin des Airs. Ton but est de qualifier le dossier et faire signer le mandat.

REGLES D'OR:
1. LANGUE: Reponds toujours dans la langue du client (Wolof, Francais ou Anglais).
2. MULTIPLIER: Si le client dit "on était 3", calcule 3 x 600€ = 1800€.
3. VISION: Demande toujours une photo de la CARTE D'EMBARQUEMENT (Boarding Pass).
4. APPEL: Si le dossier semble bon mais le client hesite, propose un rappel en Wolof/FR/EN.
5. FORMAT: 
   - Minimum 3 emojis.
   - Bullet points emojis: 1️⃣ 2️⃣ 3️⃣ ou ✅ 💰 ✈️.
   - Max 6 lignes par message (tres important pour WhatsApp).
   - Finit par le lien mandat: 👉 robindesairs.eu/mandat
"""

def call_openai(user_message):
    try:
        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json"
        }
        
        # On utilise gpt-4o-mini pour la vitesse et le coût
        payload = {
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message}
            ],
            "max_tokens": 400,
            "temperature": 0.5 # Plus bas pour etre plus precis sur les faits
        }
        
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        data = response.json()
        
        if "choices" in data:
            return data["choices"][0]["message"]["content"].strip()
        return None
    except Exception as e:
        print(f"Erreur OpenAI: {e}")
        return None

def send_whatsapp_message(phone_number, message):
    if not message: return
    
    # Simulation legere d'humain (attente de 2 secondes)
    time.sleep(2)
    
    url = f"{WATI_BASE_URL}/api/v1/sendSessionMessage/{phone_number}"
    headers = {"Authorization": f"Bearer {WATI_API_TOKEN}", "accept": "*/*"}
    params = {"messageText": message}
    
    response = requests.post(url, headers=headers, params=params, timeout=30)
    return response.status_code

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    if not data: return jsonify({"status": "no data"}), 200

    phone = data.get("waId") or data.get("from") or data.get("phone")
    
    # On verifie si c'est un message texte
    message_text = ""
    if "text" in data:
        message_text = data["text"].get("body", "")
    
    # Gestion des images (si le client envoie sa carte d'embarquement)
    if "type" in data and data["type"] == "image":
        message_text = "[L'utilisateur a envoye une image/carte d'embarquement. Demande-lui les details du vol s'il ne les a pas donnes ou confirme la reception.]"

    if not phone or not message_text:
        return jsonify({"status": "ignored"}), 200

    if data.get("owner") == True:
        return jsonify({"status": "ignored_own"}), 200

    # Appel OpenAI avec le contexte du site
    ai_response = call_openai(message_text)

    if not ai_response:
        ai_response = "J'ai bien recu votre message ! 😊\n\nEnvoyez-moi une photo de votre billet pour verifier vos 600€.\n\n👉 robindesairs.eu/mandat"

    send_whatsapp_message(phone, ai_response)
    return jsonify({"status": "ok"}), 200

@app.route("/", methods=["GET"])
def home():
    return "Robin des Airs Expert Bot - Online 🚀", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
