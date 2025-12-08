import os
import sys
from flask import Flask

# On force l'affichage immédiat des logs (flush=True)
print(">>> INITIALISATION DU SQUELETTE...", flush=True)

app = Flask(__name__)

@app.route('/')
def home():
    return "<h1>ALIVE</h1><p>Le serveur fonctionne.</p>"

@app.route('/health')
def health():
    print(">>> PING HEALTH CHECK REÇU", flush=True)
    return "OK", 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    print(f">>> DÉMARRAGE SUR LE PORT {port}...", flush=True)
    # host='0.0.0.0' est OBLIGATOIRE pour Docker
    app.run(host='0.0.0.0', port=port)