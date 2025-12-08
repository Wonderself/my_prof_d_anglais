import os, sys, tempfile, json, time, base64, datetime
from flask import Flask, request, jsonify, redirect, url_for, session, send_from_directory
from flask_cors import CORS
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from authlib.integrations.flask_client import OAuth
from werkzeug.middleware.proxy_fix import ProxyFix  # CORRECTION FINALE : Retour à ProxyFix avec syntaxe 3.x (x_for au lieu de x_forwarded_for)
from pydub import AudioSegment
import google.generativeai as genai
import psycopg2
from psycopg2.extras import RealDictCursor
import requests
from urllib.parse import quote
# Imports pour lecture de CV
from pypdf import PdfReader
from docx import Document

# ==========================================
# CONFIGURATION
# ==========================================
API_KEY = os.getenv("GOOGLE_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
SECRET_KEY = os.getenv("SECRET_KEY", "fallback_secret_key_if_missing") 

if not all([API_KEY, DATABASE_URL, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET]):
    print("⚠️ ALERTE : Variables manquantes sur Render !")

MODEL_NAME = 'gemini-2.5-flash'  # Corrigé : 'gemini-1.5-flash' si 2.5 non dispo
COACH_NAME = 'JIS_Recruiter'

app = Flask(__name__, static_folder='static', static_url_path='')
app.secret_key = SECRET_KEY
CORS(app)

# CORRECTION FINALE : Syntaxe ProxyFix pour Werkzeug 3.x
# Premier arg : l'app ; puis x_for=1 (équiv. x_forwarded_for), x_proto=1, etc.
# Pas de trusted_proxies ici – c'est implicite avec les x_ params
app.wsgi_app = ProxyFix(
    app.wsgi_app,
    x_for=1,    # Trust X-Forwarded-For
    x_proto=1,  # Trust X-Forwarded-Proto (pour HTTPS sur Render)
    x_host=1,   # Trust X-Forwarded-Host
    x_port=1,   # Trust X-Forwarded-Port
    x_prefix=0  # Pas de prefix
)

# FIX CSP: Headers Robustes pour PayPal/Tailwind (Autorise unsafe-inline/eval sans risque excessif)
@app.after_request
def add_header(response):
    csp = (
        "default-src 'self' https://*.paypal.com https://cdn.tailwindcss.com https://cdnjs.cloudflare.com 'unsafe-inline' 'unsafe-eval' data: blob:;"
        "script-src 'self' https://*.paypal.com https://cdn.tailwindcss.com https://cdnjs.cloudflare.com 'unsafe-inline' 'unsafe-eval';"
        "style-src 'self' https://fonts.googleapis.com https://cdn.tailwindcss.com 'unsafe-inline';"
        "img-src 'self' data: https:; font-src 'self' https://fonts.gstatic.com;"
    )
    response.headers['Content-Security-Policy'] = csp
    # Cache pour assets
    if request.path.endswith(('.mp4', '.png')):
        response.cache_control.max_age = 31536000
    return response

# --- AUTH INIT (Amélioré avec jwks_uri) ---
login_manager = LoginManager()
login_manager.init_app(app)
oauth = OAuth(app)

google = oauth.register(
    name='google',
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'},
    api_base_url='https://www.googleapis.com/oauth2/v1/',
    # FIX: Ajout explicite pour jwks
    jwks_uri='https://www.googleapis.com/oauth2/v3/certs',
)

if API_KEY: genai.configure(api_key=API_KEY)

# --- DATABASE (Self-Healing pour user_id) ---
def get_db_connection():
    try:
        return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    except Exception as e:
        print(f"DB ERROR: {e}")
        return None

def init_db():
    conn = get_db_connection()
    if not conn: return
    try:
        cur = conn.cursor()
        cur.execute('''CREATE TABLE IF NOT EXISTS users (id SERIAL PRIMARY KEY, google_id TEXT UNIQUE, email TEXT UNIQUE, name TEXT, cv_content TEXT, sub_expires TIMESTAMP, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);''')
        cur.execute('''CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, user_id INTEGER, candidate_name TEXT, job_title TEXT, company_type TEXT, cv_content TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);''')
        cur.execute('''CREATE TABLE IF NOT EXISTS history (id SERIAL PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP);''')
        # FIX Self-Healing: Ajout colonne si manquante
        try:
            cur.execute("ALTER TABLE sessions ADD COLUMN user_id INTEGER REFERENCES users(id);")
        except psycopg2.Error:
            pass  # Colonne existe déjà
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"DB INIT ERROR: {e}")

init_db()

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

# --- ROUTES (Préfixées /api/ pour éviter conflits statiques) ---

@app.route('/api/upload_cv', methods=['POST'])  # FIX: Préfixe /api/
@login_required
def upload_cv():
    if 'file' not in request.files: return jsonify({"error": "No file"}), 400
    file = request.files['file']
    if file.filename == '': return jsonify({"error": "No filename"}), 400
    
    text = ""
    try:
        filename = file.filename.lower()
        if filename.endswith('.pdf'):
            reader = PdfReader(file)
            for page in reader.pages: text += page.extract_text() + "\n"
        elif filename.endswith('.docx'):
            doc = Document(file)
            for para in doc.paragraphs: text += para.text + "\n"
        elif filename.endswith('.txt'):
            text = file.read().decode('utf-8', errors='ignore')
        else:
            return jsonify({"error": "Format not supported"}), 400
            
        return jsonify({"status": "ok", "text": text[:10000]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/login/google')
def login_google():
    return google.authorize_redirect(url_for('authorize', _external=True))

@app.route('/auth/callback')
def authorize():
    try:
        token = google.authorize_access_token()
        resp = google.get('userinfo')
        user_info = resp.json()
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("INSERT INTO users (google_id, email, name) VALUES (%s, %s, %s) ON CONFLICT (google_id) DO UPDATE SET name = EXCLUDED.name RETURNING *;", 
                   (user_info['id'], user_info['email'], user_info['name']))
        u = cur.fetchone()
        conn.commit()
        conn.close()
        login_user(User(u['id'], u['email'], u['name'], u['cv_content'], u['sub_expires']))
        return redirect('/')
    except Exception as e:
        return f"Auth Error: {e}", 500  # FIX: Code 500 explicite

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect('/')

@app.route('/api/payment_success', methods=['POST'])  # FIX: Préfixe
@login_required
def payment_success():
    new_expiry = datetime.datetime.now() + datetime.timedelta(days=90)
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET sub_expires = %s WHERE id = %s", (new_expiry, current_user.id))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

@app.route('/api/promo_code', methods=['POST'])  # FIX: Préfixe
@login_required
def promo_code():
    code = request.json.get('code', '').upper()
    if code == "ZEROMONEY":
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE users SET sub_expires = %s WHERE id = %s", (datetime.datetime.now() + datetime.timedelta(days=3650), current_user.id))
        conn.commit()
        conn.close()
        return jsonify({"status": "free_access_granted", "message": "10 Years Access Granted"})
    elif code == "FIFTYFIFTY":
        return jsonify({"status": "discount_applied", "message": "50% Discount Applied"})
    return jsonify({"status": "invalid", "message": "Invalid Code"}), 400

@app.route('/api/me')  # FIX: Préfixe
def get_me():
    if not current_user.is_authenticated:
        return jsonify({"logged_in": False})
    return jsonify({"logged_in": True, "name": current_user.name, "is_paid": current_user.is_paid, "saved_cv": current_user.cv_content})

def generate_tts(text):
    try:
        url = f"https://translate.google.com/translate_tts?ie=UTF-8&client=tw-ob&tl=en&q={quote(text[:500])}"
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5)
        if r.status_code == 200:
            return base64.b64encode(r.content).decode('utf-8')
    except:
        pass
    return None

def get_prompt(name, job, company, cv, history_len):
    stage = "INTRODUCTION"
    if history_len > 2: stage = "DEEP DIVE EXPERIENCE"
    if history_len > 6: stage = "HARD SKILLS"
    if history_len > 10: stage = "SOFT SKILLS"
    if history_len > 14: stage = "CLOSING"
    cv_context = f"\n=== CANDIDATE CV ===\n{cv[:5000]}\n=== END CV ===\n" if cv else ""
    return (f"ROLE: You are an expert recruiter for JOB ITV SIMULATOR at {company}. Interviewing {name} for {job}.\n" 
            f"CURRENT STAGE: {stage}.\n{cv_context} GOAL: Conduct a realistic, structured interview. Be professional but tough.\n"
            "JSON OUTPUT: {{'coach_response_text': '...', 'transcription_user': '...', 'score_pronunciation': 0-10, 'feedback_grammar': '...', 'better_response_example': '...', 'next_step_advice': '...'}}")

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')  # FIX: send_from_directory pour éviter conflits

@app.route('/health')
def health():
    # FIX: Health check FFmpeg
    try:
        from subprocess import check_output
        check_output(['ffmpeg', '-version'])
        return jsonify({"status": "alive", "ffmpeg": "ok"})
    except:
        return jsonify({"status": "alive", "ffmpeg": "error"}), 500

@app.route('/api/start_chat', methods=['POST'])  # FIX: Préfixe
@login_required
def start_chat():
    if not current_user.is_paid:
        return jsonify({"error": "Payment required"}), 403
    d = request.json
    cv_content = d.get('cv_content')
    conn = get_db_connection()
    cur = conn.cursor()
    if cv_content:
        cur.execute("UPDATE users SET cv_content = %s WHERE id = %s", (cv_content, current_user.id))
    else:
        cv_content = current_user.cv_content
    cur.execute("INSERT INTO sessions (session_id, user_id, candidate_name, job_title, company_type, cv_content) VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (session_id) DO NOTHING", 
               (d.get('session_id'), current_user.id, d.get('candidate_name'), d.get('job_title'), d.get('company_type'), cv_content))
    conn.commit()
    conn.close()
    msg = f"Hello {d.get('candidate_name')}. Welcome to JOB ITV SIMULATOR. Let's start. Briefly introduce yourself."
    return jsonify({"coach_response_text": msg, "audio_base64": generate_tts(msg), "transcription_user": ""})

@app.route('/api/analyze', methods=['POST'])  # FIX: Préfixe
@login_required
def analyze():
    if not current_user.is_paid:
        return jsonify({"error": "Payment required"}), 403
    sid = request.form.get('session_id')
    f = request.files.get('audio')
    if not f:
        return jsonify({"error": "No audio"}), 400
    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp:
        f.save(tmp.name)
        webm_path = tmp.name
    mp3_path = webm_path + ".mp3"
    try:
        # FIX: Validation FFmpeg avant export
        from subprocess import check_output
        check_output(['ffmpeg', '-version'])  # Throw si absent
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
        # FIX: Retry loop pour PROCESSING (timeout 30s)
        start_time = time.time()
        while uf.state.name == "PROCESSING" and (time.time() - start_time < 30):
            time.sleep(0.5)
            uf = genai.get_file(uf.name)
        if uf.state.name != "ACTIVE":
            raise Exception("Gemini file upload timeout")
        resp = chat.send_message([uf, "Analyze."], generation_config={"response_mime_type": "application/json"})
        try:
            data = json.loads(resp.text)
        except:
            data = json.loads(resp.text.replace('```json', '').replace('```', '').strip())
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("INSERT INTO history (session_id, role, content) VALUES (%s, 'user', %s)", (sid, data.get('transcription_user','')))
        cur.execute("INSERT INTO history (session_id, role, content) VALUES (%s, 'model', %s)", (sid, data.get('coach_response_text','')))
        conn.commit()
        conn.close()
        data['audio_base64'] = generate_tts(data.get('coach_response_text'))
        return jsonify(data)
    except Exception as e:
        print(f"Analyze Error: {e}")  # Log pour Render
        return jsonify({"error": str(e)}), 500
    finally:
        for path in [webm_path, mp3_path]:
            if os.path.exists(path):
                os.remove(path)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)  # Debug=False pour prod