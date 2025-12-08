FROM python:3.11-slim

WORKDIR /app

# Installation minimale
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 10000

# Lancement avec logs détaillés
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:10000", "--workers", "2", "--timeout", "120", "--access-logfile", "-", "--error-logfile", "-"]