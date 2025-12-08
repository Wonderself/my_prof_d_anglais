import os
import tempfile
import json
import time
import base64
import datetime
from flask import Flask, request, jsonify, redirect, url_for, send_from_directory
from flask_cors import CORS
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from authlib.integrations.flask_client import OAuth
from werkzeug.middleware.proxy_fix import ProxyFix
from pydub import AudioSegment
import google.generativeai as genai
import psycopg2
from psycopg2.extras import RealDictCursor
import requests
from urllib.parse import quote
from pypdf import PdfReader
from docx import Document

app = Flask(__name__, static_folder='static', static_url_path='/static')  # IMPORTANT : chemin classique
app.secret_key = os.getenv("SECRET_KEY", "dev-secret")

CORS(app)

# ProxyFix compatible Werkzeug 3.x
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

# CSP ULTRA PERMISSIVE MAIS FONCTIONNELLE (plus de loader bloqué)
@app.after_request
def add_headers(response):
    response.headers['Content-Security-Policy'] = (
        "default-src 'self' https: data: blob:; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https:; "
        "style-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://fonts.googleapis.com; "
        "font-src https: data:; "
        "img-src 'self' data: https: blob:; "
        "media-src 'self' https: blob: data:; "
        "connect-src 'self' https: wss:; "
        "worker-src 'self' blob:; "
        "child-src https://www.paypal.com https://accounts.google.com; "
        "frame-src https://www.paypal.com https://accounts.google.com;"
    )
    return response

# VARIABLES D'ENV
genai.configure(api_key=os.getenv("GOOGLE_API_KEY"))
DATABASE_URL = os.getenv("DATABASE_URL")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

# AUTH GOOGLE
login_manager = LoginManager(app)
oauth = OAuth(app)
google = oauth.register(
    name='google',
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'}
)

# DB INIT
def init_db():
    try:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        cur = conn.cursor()
        cur.execute('''CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY, google_id TEXT UNIQUE, email TEXT UNIQUE, name TEXT,
            cv_content TEXT, sub_expires TIMESTAMP, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );''')
        cur.execute('''CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY, user_id INTEGER, candidate_name TEXT,
            job_title TEXT, company_type TEXT, cv_content TEXT
        );''')
        cur.execute('''CREATE TABLE IF NOT EXISTS history (
            id SERIAL PRIMARY KEY, session_id TEXT, role TEXT, content TEXT
        );''')
        conn.commit()
        conn.close()
    except Exception as e:
        print("DB INIT ERROR:", e)

init_db()

class User(UserMixin):
    def __init__(self, id, email, name, cv_content="", sub_expires=None):
        self.id = id
        self.email = email
        self.name = name
        self.cv_content = cv_content
        self.sub_expires = sub_expires
    @property
    def is_paid(self):
        return self.sub_expires and self.sub_expires > datetime.datetime.now()

@login_manager.user_loader
def load_user(user_id):
    try:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        u = cur.fetchone()
        conn.close()
        return User(u['id'], u['email'], u['name'], u.get('cv_content'), u.get('sub_expires')) if u else None
    except:
        return None

# SERVE STATIC FILES CORRECTLY
@app.route('/')
def serve_index():
    return send_from_directory('static', 'index.html')

@app.route('/<path:path>')
def serve_static(path):
    return send_from_directory('static', path)

# REST OF YOUR ROUTES (unchanged, just shorter for brevity)
# ... (login, logout, /api/me, /api/upload_cv, payment, promo, start_chat, analyze)
# Je te les remets si tu veux, mais ils étaient déjà bons

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))