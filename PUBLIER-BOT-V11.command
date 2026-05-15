#!/bin/bash
cd "$(dirname "$0")"
echo "════════════════════════════════════════"
echo "  Publication bot v11 sur GitHub"
echo "════════════════════════════════════════"
if [ ! -d .git ] || ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "Initialisation du dépôt git..."
  rm -rf .git 2>/dev/null
  git init
  git remote add origin https://github.com/expert-ctrl/robin-des-airs-bot.git 2>/dev/null \
    || git remote set-url origin https://github.com/expert-ctrl/robin-des-airs-bot.git
fi
git add app.py requirements.txt .gitignore
git status -sb
if git diff --cached --quiet; then
  echo "Rien de nouveau à committer (déjà à jour ?)."
else
  git commit -m "Deploy bot v11: mandat.html, tunnel Wati, webhook /mandat_signed."
fi
echo ""
echo "Envoi vers expert-ctrl/robin-des-airs-bot (main)..."
git branch -M main 2>/dev/null
git push -u origin main
CODE=$?
echo ""
if [ $CODE -eq 0 ]; then
  echo "✅ Bot v11 poussé. Redéployez sur Render (Manual Deploy)."
  echo "   Variables Render : MANDAT_URL=https://robindesairs.eu/mandat.html"
  echo "   MANDAT_SIGNED_WEBHOOK_SECRET (identique à Netlify)"
else
  echo "❌ Échec push ($CODE). Utilisez GitHub Desktop sur ce dossier."
fi
read -p "Entrée pour fermer..."
