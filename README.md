# Robin des Airs AI Bot

Bot WhatsApp alimenté par Gemini 2.0 Flash pour Robin des Airs.

## Déploiement sur Railway.app

### Étape 1 — Variables d'environnement à configurer sur Railway

```
GEMINI_API_KEY=ta_cle_gemini_ici
WATI_API_TOKEN=ton_token_wati_ici
WATI_BASE_URL=https://live-mt-server.wati.io/XXXXXX
```

### Étape 2 — URL webhook
Une fois déployé, Railway te donne une URL comme:
https://robin-des-airs-bot.up.railway.app

Tu colles cette URL dans Wati:
Settings → Webhook → URL: https://robin-des-airs-bot.up.railway.app/webhook

### Étape 3 — Tester
Va sur: https://ton-url.up.railway.app/test
Tu dois voir: {"status": "Robin des Airs Bot is running!"}

## Structure des fichiers
- main.py — Le bot principal
- requirements.txt — Les dépendances Python
- Procfile — Instructions pour Railway
