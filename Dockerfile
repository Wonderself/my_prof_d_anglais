# ON PASSE À PYTHON 3.11 (Plus récent, plus rapide, et supprime les warnings Google)
FROM python:3.11-slim

# Installe les outils systèmes (FFmpeg pour l'audio, libpq pour la DB)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render définit la variable PORT automatiquement (souvent 10000)
ENV PORT=10000

# Lancement PROD avec Gunicorn
CMD gunicorn --workers 4 --bind 0.0.0.0:$PORT app:app