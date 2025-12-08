FROM python:3.11-slim

# 1. Installation des dépendances système (FFmpeg)
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 2. Installation des libs Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3. Copie du code
COPY . .

# 4. EXPOSE le port pour que Render comprenne
EXPOSE 10000

# 5. COMMANDE DE LANCEMENT OPTIMISÉE
# - workers 2 : Suffisant pour le free tier (évite le crash mémoire)
# - access-logfile - : Affiche CHAQUE visite dans les logs (vital pour débugger)
# - error-logfile - : Affiche les erreurs
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:10000", "--workers", "2", "--timeout", "120", "--access-logfile", "-", "--error-logfile", "-"]