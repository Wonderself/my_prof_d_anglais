#!/usr/bin/env bash
set -e
echo "��� Installation des dépendances..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi
source venv/bin/activate
pip install --upgrade pip
if [ -f requirements.txt ]; then 
    pip install -r requirements.txt
else
    echo "⚠️ Pas de requirements.txt trouvé !"
fi
echo "✅ Environnement prêt."
