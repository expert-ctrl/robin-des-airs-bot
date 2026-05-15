#!/bin/bash
cd "$(dirname "$0")"
echo "════════════════════════════════════════"
echo "  Publication bot v11 sur GitHub"
echo "════════════════════════════════════════"
URL="https://github.com/expert-ctrl/robin-des-airs-bot.git"
if [ ! -d .git ]; then
  git init
fi
if git remote get-url origin >/dev/null 2>&1; then
  git remote set-url origin "$URL"
else
  git remote add origin "$URL"
fi
echo "Remote origin : $(git remote get-url origin)"
git add app.py main.py requirements.txt .gitignore
git status -sb
if ! git diff --cached --quiet 2>/dev/null; then
  git commit -m "Deploy bot v11: main.py -> app.py, mandat.html, webhook /mandat_signed."
elif [ -z "$(git log -1 --oneline 2>/dev/null)" ]; then
  git commit -m "Deploy bot v11: main.py -> app.py, mandat.html, webhook /mandat_signed."
else
  echo "Commit local OK (rien de nouveau à ajouter)."
fi
git branch -M main 2>/dev/null
echo ""
echo "Envoi vers GitHub..."
git push -u origin main
CODE=$?
echo ""
if [ $CODE -eq 0 ]; then
  echo "✅ Bot v11 en ligne sur GitHub."
  echo "   Render → Manual Deploy"
  echo "   MANDAT_URL=https://robindesairs.eu/mandat.html"
else
  echo "❌ Push échoué ($CODE)."
  echo "   Essayez : git pull origin main --allow-unrelated-histories"
  echo "   puis relancez ce script."
fi
read -p "Entrée pour fermer..."
