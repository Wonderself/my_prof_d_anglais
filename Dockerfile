FROM python:3.11-slim

# Installation des paquets systèmes nécessaires
# libpq-dev est souvent nécessaire pour compiler psycopg2, mais avec -binary on s'en sort
# ffmpeg est vital pour l'audio
RUN apt-get update && apt-get install -y \
    ffmpeg \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Gestion optimisée du cache des dépendances
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copie du code
COPY . .

EXPOSE 5000

# Gunicorn est le serveur de prod. 
# Timeout augmenté car l'IA peut prendre du temps
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "8", "--timeout", "120", "english_coach_backend:app"]