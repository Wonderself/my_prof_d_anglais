import os, sys, tempfile, json, time, base64, datetime, logging
from flask import Flask, request, jsonify, redirect, url_for, session, send_from_directory
from flask_cors import CORS
# FIX CRITIQUE: Assure LoginManager est bien importé
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

# --- LOGGING ---
logging.basicConfig(level=logging.INFO, stream=sys.stdout, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- CONFIG ---
API_KEY = os.getenv("GOOGLE_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
SECRET_KEY = os.getenv("SECRET_KEY", "fallback_key_dev")

app = Flask(__name__, static_folder='static', static_url_path='/static')
app.secret_key = SECRET_KEY
app.config['PREFERRED_URL_SCHEME'] = 'https'

CORS(app)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# --- CSP HEADERS (Final) ---
@app.after_request
def add_security_headers(response):
    csp = (
        "default-src 'self' data: blob:; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.tailwindcss.com https://cdnjs.cloudflare.com https://www.paypal.com https://www.sandbox.paypal.com https://www.google.com https://www.gstatic.com; "
        "style-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://cdnjs.cloudflare.com https://fonts.googleapis.com; "
        "font-src 'self' data: https://cdnjs.cloudflare.com https://fonts.gstatic.com; "
        "connect-src 'self' https://www.paypal.com https://www.sandbox.paypal.com https://www.google.com https://www.google-analytics.com; "
        "img-src 'self' data: https:; "
        "media-src 'self' data: blob:;"
    )
    response.headers['Content-Security-Policy'] = csp
    return response

# --- AUTH INIT ---
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'index'

oauth = OAuth(app)
# FIX: Configuration Google Login (Résout l'erreur 'Invalid URL userinfo' et l'erreur '.json()')
google = oauth.register(
    name='google',
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    userinfo_endpoint='https://www.googleapis.com/oauth2/v1/userinfo', # URL COMPLÈTE
    client_kwargs={'scope': 'openid email profile'},
)

if API_KEY: genai.configure(api_key=API_KEY)

# --- DB & USER LOGIC (Lazy Loading) ---
DB_INITIALIZED = False

def get_db_connection():
    try: return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    except Exception as e: logger.error(f"DB CONNECT ERROR: {e}"); return None

def init_tables():
    conn = get_db_connection()
    if not conn: return
    try:
        cur = conn.cursor()
        cur.execute('''CREATE TABLE IF NOT EXISTS users (id SERIAL PRIMARY KEY, google_id TEXT UNIQUE, email TEXT UNIQUE, name TEXT, cv_content TEXT, sub_expires TIMESTAMP, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);''')
        cur.execute('''CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, user_id INTEGER, candidate_name TEXT, job_title TEXT, company_type TEXT, cv_content TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);''')
        cur.execute('''CREATE TABLE IF NOT EXISTS history (id SERIAL PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP);''')
        try: cur.execute("ALTER TABLE sessions ADD COLUMN user_id INTEGER;")
        except: conn.rollback()
        conn.commit(); conn.close()
        logger.info(">>> DB TABLES READY")
    except Exception as e: logger.error(f"DB INIT ERROR: {e}")

@app.before_request
def check_init():
    global DB_INITIALIZED
    if request.path.startswith('/health') or request.path.startswith('/static'): return
    if not DB_INITIALIZED:
        init_tables()
        DB_INITIALIZED = True

class User(UserMixin):
    def __init__(self, id, email, name, cv_content, sub_expires):
        self.id = id; self.email = email; self.name = name; self.cv_content = cv_content; self.sub_expires = sub_expires
    @property
    def is_paid(self): return self.sub_expires and self.sub_expires > datetime.datetime.now()

@login_manager.user_loader
def load_user(user_id):
    conn = get_db_connection(); 
    if not conn: return None
    try:
        cur = conn.cursor(); cur.execute("SELECT * FROM users WHERE id = %s", (user_id,)); u = cur.fetchone(); conn.close()
        if u: return User(u['id'], u['email'], u['name'], u['cv_content'], u['sub_expires'])
    except: pass
    return None

# --- AI & TTS ---
def generate_tts(text):
    try:
        url = f"https://translate.google.com/translate_tts?ie=UTF-8&client=tw-ob&tl=en&q={quote(text[:900])}"
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5)
        if r.status_code == 200: return base64.b64encode(r.content).decode('utf-8')
    except: pass
    return None

def get_ai_prompt(name, job, company, cv, history_len):
    stage = "INTRODUCTION"
    if history_len > 2: stage = "DEEP DIVE"
    if history_len > 8: stage = "CONCLUSION"
    cv_excerpt = (cv[:4000] + '...') if cv else "No CV"
    return (f"You are an expert recruiter at {company}. Interviewing {name} for {job}.\nSTAGE: {stage}.\nCV: {cv_excerpt}\n"
            "Keep responses short (max 3 sentences). Be professional.\n"
            "JSON OUTPUT: {'coach_response_text': '...', 'transcription_user': '...', 'score_pronunciation': 0-10, 'feedback_grammar': '...', 'better_response_example': '...', 'next_step_advice': '...'}")

# --- ROUTES ---

@app.route('/')
def index(): return app.send_static_file('index.html')

@app.route('/<path:filename>')
def serve_static(filename):
    return send_from_directory(app.static_folder, filename)

@app.route('/health')
def health():
    return jsonify({"status": "alive"}), 200

@app.route('/auth/login')
def login(): return google.authorize_redirect(url_for('authorize', _external=True))

@app.route('/auth/callback')
def authorize():
    try:
        token = google.authorize_access_token()
        
        # FIX: Utilisation directe de userinfo sans .json()
        user_info = oauth.google.userinfo(token=token) 
        
        conn = get_db_connection()
        if not conn:
            return "<h1>ERREUR CRITIQUE DB</h1><p>Impossible de se connecter à la base Neon. Vérifiez DATABASE_URL dans Render (pas de guillemets, bon mot de passe).</p>"

        cur = conn.cursor()
        cur.execute("INSERT INTO users (google_id, email, name) VALUES (%s, %s, %s) ON CONFLICT (google_id) DO UPDATE SET name = EXCLUDED.name RETURNING *;", 
                   (user_info['sub'], user_info['email'], user_info['name']))
        u = cur.fetchone()
        conn.commit(); conn.close()
        
        login_user(User(u['id'], u['email'], u['name'], u['cv_content'], u['sub_expires']))
        return redirect('/')
    except Exception as e: 
        logger.error(f"AUTH FAIL: {e}")
        return f"""
        <div style='font-family:sans-serif; padding:50px; text-align:center;'>
            <h1 style='color:red;'>ERREUR DE CONNEXION (CODE)</h1>
            <p style='background:#eee; padding:20px; border-radius:10px; font-family:monospace;'>{str(e)}</p>
            <p>Si l'erreur persiste, cela peut être une erreur dans l'URL de redirection Google.</p>
        </div>
        """

@app.route('/logout')
@login_required
def logout(): logout_user(); return redirect('/')

@app.route('/api/me')
def get_me():
    if not current_user.is_authenticated: return jsonify({"logged_in": False})
    return jsonify({"logged_in": True, "name": current_user.name, "is_paid": current_user.is_paid, "saved_cv": current_user.cv_content})

@app.route('/upload_cv', methods=['POST'])
@login_required
def upload_cv():
    if 'file' not in request.files: return jsonify({"error": "No file"}), 400
    file = request.files['file']
    try:
        text = ""
        if file.filename.endswith('.pdf'):
            for page in PdfReader(file).pages: text += page.extract_text() + "\n"
        elif file.filename.endswith('.docx'):
            for para in Document(file).paragraphs: text += para.text + "\n"
        else: text = file.read().decode('utf-8', errors='ignore')
        return jsonify({"status": "ok", "text": text.strip()})
    except: return jsonify({"error": "Error reading file"}), 500

@app.route('/start_chat', methods=['POST'])
@login_required
def start_chat():
    if not current_user.is_paid: return jsonify({"error": "Pay first"}), 403
    d = request.json
    conn = get_db_connection(); cur = conn.cursor()
    if d.get('cv_content'): cur.execute("UPDATE users SET cv_content = %s WHERE id = %s", (d.get('cv_content'), current_user.id))
    cur.execute("INSERT INTO sessions (session_id, user_id, candidate_name, job_title, company_type, cv_content) VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (session_id) DO NOTHING", 
               (d.get('session_id'), current_user.id, d.get('candidate_name'), d.get('job_title'), d.get('company_type'), d.get('cv_content') or current_user.cv_content))
    conn.commit(); conn.close()
    msg = f"Hello {d.get('candidate_name')}. Interview for {d.get('job_title')}. Introduce yourself."
    return jsonify({"coach_response_text": msg, "audio_base64": generate_tts(msg)})

@app.route('/analyze', methods=['POST'])
@login_required
def analyze():
    if not current_user.is_paid: return jsonify({"error": "Pay first"}), 403
    sid = request.form.get('session_id')
    f = request.files.get('audio')
    if not f: return jsonify({"error": "No audio"}), 400
    
    tw = tempfile.NamedTemporaryFile(suffix=".webm", delete=False)
    tm = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    try:
        f.save(tw.name); tw.close()
        AudioSegment.converter = "/usr/bin/ffmpeg"
        AudioSegment.from_file(tw.name).export(tm.name, format="mp3")
        tm.close()
        
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT * FROM sessions WHERE session_id=%s", (sid,))
        sess = cur.fetchone()
        cur.execute("SELECT role, content FROM history WHERE session_id=%s ORDER BY id ASC LIMIT 10", (sid,))
        hist = [{"role": r['role'], "parts": [r['content']]} for r in cur.fetchall()]
        conn.close()
        
        model = genai.GenerativeModel("gemini-1.5-flash", system_instruction=get_ai_prompt(sess['candidate_name'], sess['job_title'], sess['company_type'], sess['cv_content'], len(hist)))
        chat = model.start_chat(history=hist)
        
        uf = genai.upload_file(tm.name, mime_type="audio/mp3")
        while uf.state.name == "PROCESSING": time.sleep(1); uf = genai.get_file(uf.name)
        
        resp = chat.send_message([uf, "Analyze."], generation_config={"response_mime_type": "application/json"})
        data = json.loads(resp.text.replace('```json','').replace('```','').strip())
        
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("INSERT INTO history (session_id, role, content) VALUES (%s, 'user', %s), (%s, 'model', %s)", 
                   (sid, data.get('transcription_user'), sid, data.get('coach_response_text')))
        conn.commit(); conn.close()
        data['audio_base64'] = generate_tts(data.get('coach_response_text'))
        return jsonify(data)
    except Exception as e: return jsonify({"error": str(e)}), 500
    finally: 
        if os.path.exists(tw.name): os.remove(tw.name)
        if os.path.exists(tm.name): os.remove(tm.name)

# --- PROMOS ---
@app.route('/api/payment_success', methods=['POST'])
@login_required
def pay_ok():
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE users SET sub_expires = %s WHERE id = %s", (datetime.datetime.now() + datetime.timedelta(days=90), current_user.id))
    conn.commit(); conn.close(); return jsonify({"status": "ok"})

@app.route('/api/promo_code', methods=['POST'])
@login_required
def promo():
    if request.json.get('code','').upper() == "ZEROMONEY":
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("UPDATE users SET sub_expires = %s WHERE id = %s", (datetime.datetime.now() + datetime.timedelta(days=3650), current_user.id))
        conn.commit(); conn.close(); return jsonify({"status": "free_access_granted", "message": "VIP Access"})
    return jsonify({"status": "invalid", "message": "Invalid Code"}), 400

if __name__ == '__main__': 
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))