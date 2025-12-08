import os
import sys
import logging
from flask import Flask, jsonify
import psycopg2

# Configuration des logs pour voir tout ce qui se passe
logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)

app = Flask(__name__)

@app.route('/')
def index():
    return "<h1>SERVER ONLINE</h1><p>Reset successful.</p>"

@app.route('/health')
def health():
    logger.info(">>> HEALTH CHECK: PING RECEIVED")
    return jsonify({"status": "alive"}), 200

@app.route('/test_db')
def test_db():
    # Test de connexion à Neon
    db_url = os.getenv("DATABASE_URL")
    try:
        conn = psycopg2.connect(db_url)
        conn.close()
        return "DATABASE CONNECTED ✅"
    except Exception as e:
        return f"DATABASE ERROR ❌: {str(e)}"

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)