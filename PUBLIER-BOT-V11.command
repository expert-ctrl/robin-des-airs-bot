#!/bin/bash
set +H
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
echo "Remote : $(git remote get-url origin)"
git add app.py main.py requirements.txt .gitignore PUBLIER-BOT-V11.command 2>/dev/null
if ! git diff --cached --quiet 2>/dev/null; then
  git commit -m "Deploy bot v11: app.py, main.py, mandat.html."
fi
git branch -M main 2>/dev/null
echo ""
echo "Synchronisation avec GitHub..."
git fetch origin 2>/dev/null
if git rev-parse origin/main >/dev/null 2>&1; then
  if ! git merge-base --is-ancestor origin/main main 2>/dev/null; then
    git merge origin/main --no-edit 2>/dev/null || true
    if [ -f main.py ] && grep -q '<<<<<<<' main.py 2>/dev/null; then
      git checkout --ours main.py requirements.txt 2>/dev/null
      git add main.py requirements.txt app.py
      git commit -m "Bot v11: garde app.py + main.py (Railway)." 2>/dev/null || true
    fi
  fi
fi
echo "Envoi vers GitHub..."
if git push -u origin main 2>&1; then
  echo ""
  echo "✅ Bot v11 en ligne sur GitHub."
  echo "   Railway redéploie automatiquement."
  echo "   MANDAT_URL=https://robindesairs.eu/mandat.html"
else
  echo ""
  echo "Push normal bloqué → envoi forcé du bot v11 (normal)."
  if git push --force origin main 2>&1; then
    echo ""
    echo "✅ Bot v11 envoyé (force push)."
    echo "   Railway → attendez Success sur Deployments."
  else
    echo ""
    echo "❌ Échec. Vérifiez username + token GitHub."
  fi
fi
read -p "Entrée pour fermer..."
