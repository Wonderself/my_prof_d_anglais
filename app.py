import os
import sys
import logging
from flask import Flask, jsonify

# Configuration des logs pour voir si ça démarre
logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)

app = Flask(__name__)

@app.route('/')
def index():
    return "<h1>System Operational</h1><p>Le serveur est en ligne.</p>"

@app.route('/health')
def health():
    logger.info(">>> HEALTH CHECK RECEIVED")
    return jsonify({"status": "alive"}), 200

if __name__ == '__main__':
    # On utilise le PORT de Render ou 10000 par défaut
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)