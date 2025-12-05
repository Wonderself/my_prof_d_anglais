FROM python:3.11-slim

# 1. Installation des outils système (FFmpeg est vital pour l'audio)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    && apt-get autoremove -y && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 2. Copie et installation des dépendances (Mise en cache Docker optimisée)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3. Copie du reste du code
COPY . .

# 4. COMMANDE DE LANCEMENT DYNAMIQUE
# On utilise la forme "shell" (pas de crochets []) pour que la variable $PORT soit lue.
# --timeout 0 : Désactive le timeout des workers pour laisser Gemini réfléchir si besoin.
CMD exec gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 0 app:app