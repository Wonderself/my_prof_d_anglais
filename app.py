import os, sys, tempfile, json, time, base64, datetime
from flask import Flask, request, jsonify, redirect, url_for, session
from flask_cors import CORS
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from authlib.integrations.flask_client import OAuth
from pydub import AudioSegment
import google.generativeai as genai
import psycopg2
from psycopg2.extras import RealDictCursor
import requests
from urllib.parse import quote

# ==========================================
# CONFIGURATION SÉCURISÉE (VIA ENVIRONNEMENT)
# ==========================================
API_KEY = os.getenv("GOOGLE_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
SECRET_KEY = os.getenv("SECRET_KEY", "fallback_secret_key_if_missing") 

if not all([API_KEY, DATABASE_URL, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET]):
    print("⚠️ ALERTE SÉCURITÉ : Certaines variables d'environnement sont manquantes sur Render !")

MODEL_NAME = 'gemini-2.5-flash' # Modèle le plus récent
COACH_NAME = 'JIS_Recruiter' # Nom interne du rôle AI (ne s'affiche pas)

# --- DÉBUT DE L'APPLICATION FLASK (CRITIQUE : DOIT ÊTRE ICI) ---
app = Flask(__name__, static_folder='static', static_url_path='')
app.secret_key = SECRET_KEY
CORS(app)

# --- INIT AUTH & LOGIN ---
login_manager = LoginManager()
login_manager.init_app(app)
oauth = OAuth(app)

google = oauth.register(
    name='google',
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    api_base_url='https://www.googleapis.com/oauth2/v1/', 
    client_kwargs={'scope': 'openid email profile'},
)

if API_KEY: genai.configure(api_key=API_KEY)

# --- CACHE OPTIMIZATION (PWA) ---
@app.after_request
def add_header(response):
    if request.path.startswith('/') and (request.path.endswith('.mp4') or request.path.endswith('.png')):
        response.cache_control.max_age = 31536000
        response.cache_control.public = True
    return response

# --- DATABASE ---
def get_db_connection():
    try:
        # FIX CRITIQUE: On retire sslmode='require' car il est déjà dans la DATABASE_URL
        return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    except Exception as e:
        print(f"⚠️ DB ERROR: {e}")
        return None

def init_db():
    conn = get_db_connection()
    if not conn: return
    try:
        cur = conn.cursor()
        # 1. Création ou vérification des tables
        cur.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                google_id TEXT UNIQUE NOT NULL,
                email TEXT UNIQUE NOT NULL,
                name TEXT,
                cv_content TEXT,
                sub_expires TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')
        cur.execute('''CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, user_id INTEGER, candidate_name TEXT, job_title TEXT, company_type TEXT, cv_content TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);''')
        cur.execute('''CREATE TABLE IF NOT EXISTS history (id SERIAL PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP);''')
        
        # 2. FIX SCHEMA : AJOUTER LA COLONNE USER_ID SI ELLE MANQUE (Réparation de l'erreur)
        try:
            cur.execute("ALTER TABLE sessions ADD COLUMN user_id INTEGER;")
            print("✅ Colonne 'user_id' ajoutée à la table sessions (Correction de l'ancienne erreur).")
        except (psycopg2.errors.DuplicateColumn, psycopg2.errors.SyntaxError):
            # C'est normal si la colonne existe déjà ou si la commande plante pour une autre raison
            conn.rollback()
            pass
        
        conn.commit()
        conn.close()
    except Exception as e: print(f"DB INIT ERROR: {e}")

init_db()

# --- USER CLASS (Flask-Login) ---
class User(UserMixin):
    def __init__(self, id, email, name, cv_content, sub_expires):
        self.id = id
        self.email = email
        self.name = name
        self.cv_content = cv_content
        self.sub_expires = sub_expires

    @property
    def is_paid(self):
        if not self.sub_expires: return False
        return self.sub_expires > datetime.datetime.now()

@login_manager.user_loader
def load_user(user_id):
    conn = get_db_connection()
    if not conn: return None
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
    u = cur.fetchone()
    conn.close()
    if u: return User(u['id'], u['email'], u['name'], u['cv_content'], u['sub_expires'])
    return None

# ==========================================
# DÉFINITION DES ROUTES
# ==========================================

# --- AUTH ROUTES ---
@app.route('/login/google')
def login_google():
    redirect_uri = url_for('authorize', _external=True)
    if redirect_uri.startswith('http://') and 'onrender' in redirect_uri:
        redirect_uri = redirect_uri.replace('http://', 'https://')
    return google.authorize_redirect(redirect_uri)

@app.route('/auth/callback')
def authorize():
    try:
        token = google.authorize_access_token()
        resp = google.get('userinfo') 
        user_info = resp.json()
        
        google_id = user_info['id']
        email = user_info['email']
        name = user_info['name']

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO users (google_id, email, name) VALUES (%s, %s, %s)
            ON CONFLICT (google_id) DO UPDATE SET name = EXCLUDED.name
            RETURNING *;
        """, (google_id, email, name))
        u = cur.fetchone()
        conn.commit()
        conn.close()

        user_obj = User(u['id'], u['email'], u['name'], u['cv_content'], u['sub_expires'])
        login_user(user_obj)
        return redirect('/')
    except Exception as e:
        return f"Auth Error: {e}"

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect('/')

# --- PAYMENT ROUTES ---
@app.route('/api/payment_success', methods=['POST'])
@login_required
def payment_success():
    days = 90
    new_expiry = datetime.datetime.now() + datetime.timedelta(days=days)
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET sub_expires = %s WHERE id = %s", (new_expiry, current_user.id))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "new_expiry": new_expiry.isoformat()})

@app.route('/api/promo_code', methods=['POST'])
@login_required
def promo_code():
    d = request.json
    code = d.get('code', '').upper()
    
    if code == "ZEROMONEY":
        days = 3650 # 10 ans d'accès
        new_expiry = datetime.datetime.now() + datetime.timedelta(days=days)
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE users SET sub_expires = %s WHERE id = %s", (new_expiry, current_user.id))
        conn.commit()
        conn.close()
        return jsonify({"status": "free_access_granted", "message": "Accès de 10 ans débloqué. Bienvenue !"}), 200
        
    elif code == "FIFTYFIFTY":
        return jsonify({"status": "discount_applied", "message": "Félicitations ! Votre code 50% est activé."}), 200

    return jsonify({"status": "invalid", "message": "Code promo invalide."}), 400

# --- API INFO USER ---
@app.route('/api/me')
def get_me():
    if not current_user.is_authenticated:
        return jsonify({"logged_in": False})
    return jsonify({
        "logged_in": True,
        "name": current_user.name,
        "is_paid": current_user.is_paid,
        "saved_cv": current_user.cv_content
    })

# --- LOGIQUE CORE AI ---
def generate_tts(text):
    try:
        url = f"https://translate.google.com/translate_tts?ie=UTF-8&client=tw-ob&tl=en&q={quote(text[:500])}"
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5)
        if r.status_code == 200: return base64.b64encode(r.content).decode('utf-8')
    except: pass
    return None

def get_prompt(name, job, company, cv, history_len):
    stage = "INTRODUCTION"
    if history_len > 2: stage = "DEEP DIVE EXPERIENCE"
    if history_len > 6: stage = "HARD SKILLS"
    if history_len > 10: stage = "SOFT SKILLS"
    if history_len > 14: stage = "CLOSING"
    cv_context = f"\n=== CANDIDATE CV ===\n{cv[:5000]}\n=== END CV ===\n" if cv else ""
    return (
        f"ROLE: You are an expert recruiter for JOB ITV SIMULATOR at {company}. Interviewing {name} for {job}.\n" 
        f"CURRENT STAGE: {stage}.\n{cv_context}"
        "GOAL: Conduct a realistic, structured interview. Be professional but tough.\n"
        "RULES: Ask ONE short question at a time (max 15 words). Follow flow. Use CV facts.\n"
        "JSON OUTPUT: {'coach_response_text': '...', 'transcription_user': '...', 'score_pronunciation': 0-10, 'feedback_grammar': '...', 'better_response_example': '...', 'next_step_advice': '...'}"
    )

@app.route('/')
def index(): return app.send_static_file('index.html')

@app.route('/health')
def health(): return jsonify({"status": "alive"})

@app.route('/start_chat', methods=['POST'])
@login_required
def start_chat():
    if not current_user.is_paid: return jsonify({"error": "Payment required"}), 403
    d = request.json
    sid = d.get('session_id')
    cv_content = d.get('cv_content')
    conn = get_db_connection()
    cur = conn.cursor()
    if cv_content: cur.execute("UPDATE users SET cv_content = %s WHERE id = %s", (cv_content, current_user.id))
    else: cv_content = current_user.cv_content
    cur.execute("INSERT INTO sessions (session_id, user_id, candidate_name, job_title, company_type, cv_content) VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (session_id) DO NOTHING", 
               (sid, current_user.id, d.get('candidate_name'), d.get('job_title'), d.get('company_type'), cv_content))
    conn.commit()
    conn.close()
    msg = f"Hello {d.get('candidate_name')}. Welcome to JOB ITV SIMULATOR. Let's start. Briefly introduce yourself."
    return jsonify({"coach_response_text": msg, "audio_base64": generate_tts(msg), "transcription_user": ""})

@app.route('/analyze', methods=['POST'])
@login_required
def analyze():
    if not current_user.is_paid: return jsonify({"error": "Payment required"}), 403
    sid = request.form.get('session_id')
    f = request.files.get('audio')
    if not f or not sid: return jsonify({"error": "No audio"}), 400
    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp:
        f.save(tmp.name)
        webm_path = tmp.name
    mp3_path = webm_path + ".mp3"
    try:
        AudioSegment.from_file(webm_path).export(mp3_path, format="mp3")
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM sessions WHERE session_id=%s", (sid,))
        sess = cur.fetchone()
        cur.execute("SELECT role, content FROM history WHERE session_id=%s ORDER BY id ASC", (sid,))
        hist_rows = cur.fetchall()
        conn.close()
        hist = [{"role": r['role'], "parts": [r['content']]} for r in hist_rows[-10:]]
        sys_prompt = get_prompt(sess['candidate_name'], sess['job_title'], sess['company_type'], sess['cv_content'], len(hist_rows))
        model = genai.GenerativeModel(MODEL_NAME, system_instruction=sys_prompt)
        chat = model.start_chat(history=hist)
        uf = genai.upload_file(mp3_path, mime_type="audio/mp3")
        while uf.state.name == "PROCESSING": time.sleep(0.5); uf = genai.get_file(uf.name)
        resp = chat.send_message([uf, "Analyze."], generation_config={"response_mime_type": "application/json"})
        try: data = json.loads(resp.text)
        except: data = json.loads(resp.text.replace('```json', '').replace('```', '').strip())
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("INSERT INTO history (session_id, role, content) VALUES (%s, 'user', %s)", (sid, data.get('transcription_user','')))
        cur.execute("INSERT INTO history (session_id, role, content) VALUES (%s, 'model', %s)", (sid, data.get('coach_response_text','')))
        conn.commit()
        conn.close()
        data['audio_base64'] = generate_tts(data.get('coach_response_text'))
        return jsonify(data)
    except Exception as e: return jsonify({"error": str(e)}), 500
    finally:
        if os.path.exists(webm_path): os.remove(webm_path)
        if os.path.exists(mp3_path): os.remove(mp3_path)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)