FROM python:3.11-slim

# Installation des paquets systèmes (FFmpeg + Pilotes DB)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Installation des dépendances Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copie du code source
COPY . .

# On n'expose pas de port fixe, on laisse Render décider
# La variable d'environnement PORT est fournie par Render automatiquement

# COMMANDE DE DÉMARRAGE OPTIMISÉE
# 1. On utilise "sh -c" pour que la variable $PORT soit bien lue (ex: 10000)
# 2. On réduit les threads à 4 pour économiser la RAM (évite le crash mémoire)
# 3. On ajoute --access-logfile - pour voir les requêtes dans les logs
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT:-5000} --workers 1 --threads 4 --timeout 120 --access-logfile - --error-logfile - english_coach_backend:app"]