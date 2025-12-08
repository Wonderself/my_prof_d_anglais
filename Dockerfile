FROM python:3.11-slim

# 1. Installation FFmpeg (Vital pour l'audio)
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 2. Installation Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3. Copie du code
COPY . .

# 4. Port Render
EXPOSE 10000

# 5. Lancement Sécurisé (2 workers pour économiser la RAM + Logs activés)
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:10000", "--workers", "2", "--timeout", "120", "--access-logfile", "-", "--error-logfile", "-"]