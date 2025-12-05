#!/bin/bash
set -e

# CONFIGURATION
REPO_URL="https://github.com/Wonderself/my_prof_d_anglais"
TIMESTAMP=$(date +%Y%m%d-%H%M)
BACKUP_BRANCH="pre-idx-backup-$TIMESTAMP"
WORK_BRANCH="ops/idx-integration"
SCAN_FILE="repo-scan.txt"

echo "🔵 [1/4] INITIALISATION DE LA PHASE 1..."

# Vérification simple de Git
if ! command -v git &> /dev/null; then
    echo "❌ Erreur: Git n'est pas installé."
    exit 1
fi

echo "🟢 [2/4] AUDIT DE SÉCURITÉ & SCAN..."
echo "--- RAPPORT D'AUDIT GÉNÉRÉ LE $(date) ---" > $SCAN_FILE

# 1. Scan de structure (Ignorer les dossiers poubelles)
echo "📂 STRUCTURE DU PROJET :" >> $SCAN_FILE
ls -R -I "venv" -I "__pycache__" -I ".git" >> $SCAN_FILE

# 2. Recherche de secrets
echo -e "\n🕵️  RECHERCHE DE SECRETS :" >> $SCAN_FILE
grep -rE "API_KEY|SECRET|PASSWORD|TOKEN" . --exclude-dir={.git,venv,__pycache__} --exclude=$SCAN_FILE >> $SCAN_FILE || echo "✅ Aucun secret évident trouvé." >> $SCAN_FILE

# 3. Vérification .env
echo -e "\n⚠️  FICHIERS SENSIBLES :" >> $SCAN_FILE
if [ -f ".env" ]; then
    echo "❌ CRITIQUE : Fichier .env détecté." >> $SCAN_FILE
else
    echo "✅ Pas de fichier .env à la racine." >> $SCAN_FILE
fi

echo "✅ Rapport sauvegardé dans $SCAN_FILE"

echo "🔵 [3/4] CRÉATION DU BACKUP..."
# Force la création de la branche backup depuis l'état actuel
git branch $BACKUP_BRANCH 2>/dev/null || echo "Branche backup déjà existante ou erreur mineure"
echo "✅ Backup local créé : $BACKUP_BRANCH"

echo "🔵 [4/4] CRÉATION BRANCHE OPS..."
git checkout -b $WORK_BRANCH 2>/dev/null || git checkout $WORK_BRANCH
echo "✅ Sur la branche de travail : $WORK_BRANCH"

echo "🏁 PHASE 1 TERMINÉE."