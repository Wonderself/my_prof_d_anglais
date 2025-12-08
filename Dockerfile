FROM python:3.11-slim

WORKDIR /app

# On installe juste Flask
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# On copie le code
COPY . .

# On dit à Render qu'on est sur le port 10000
ENV PORT=10000
EXPOSE 10000

# COMMANDE DIRECTE (Pas de Gunicorn)
# Cela permet de voir les erreurs instantanément sans filtre
CMD ["python", "app.py"]