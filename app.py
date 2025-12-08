import os, sys, tempfile, json, time, base64, datetime, logging
from flask import Flask, request, jsonify, redirect, url_for, session, send_from_directory
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

# --- LOGGING CONFIG (Pour voir les erreurs sur Render) ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- CONFIGURATION ---
API_KEY = os.getenv("GOOGLE_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
SECRET_KEY = os.getenv("SECRET_KEY", "dev_secret_key")

app = Flask(__name__, static_folder='static', static_url_path='')
app.secret_key = SECRET_KEY
app.config['PREFERRED_URL_SCHEME'] = 'https' # Force HTTPS pour les URL Google

# FIX: CORS pour autoriser les requêtes fetch locales
CORS(app, resources={r"/*": {"origins": "*"}})

# FIX: ProxyFix pour Render (HTTPS)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# FIX: CSP Headers Robustes
@app.after_request
def add_security_headers(response):
    csp = (
        "default-src 'self' data: blob:; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.tailwindcss.com https://cdnjs.cloudflare.com https://www.paypal.com https://www.google.com https://www.gstatic.com; "
        "style-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://cdnjs.cloudflare.com https://fonts.googleapis.com; "
        "font-src 'self' https://cdnjs.cloudflare.com https://fonts.gstatic.com; "
        "connect-src 'self' https://www.paypal.com https://www.google.com https://www.google-analytics.com; "
        "img-src 'self' data: https:; "
        "media-src 'self' data: blob:;"
    )
    response.headers['Content-Security-Policy'] = csp
    return response

# --- AUTH ---
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'index' # Redirige vers la home si pas loggé

oauth = OAuth(app)
google = oauth.register(
    name='google',
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'},
)

if API_KEY: genai.configure(api_key=API_KEY)

# --- DATABASE ---
def get_db_connection():
    try:
        return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    except Exception as e:
        logger.error(f"DB CONNECTION ERROR: {e}")
        return None

def init_db():
    conn = get_db_connection()
    if not conn: return
    try:
        cur = conn.cursor()
        # Création Tables
        cur.execute('''CREATE TABLE IF NOT EXISTS users (id SERIAL PRIMARY KEY, google_id TEXT UNIQUE, email TEXT UNIQUE, name TEXT, cv_content TEXT, sub_expires TIMESTAMP, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);''')
        cur.execute('''CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, user_id INTEGER, candidate_name TEXT, job_title TEXT, company_type TEXT, cv_content TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);''')
        cur.execute('''CREATE TABLE IF NOT EXISTS history (id SERIAL PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP);''')
        
        # Self-Healing: Check columns
        try:
            cur.execute("ALTER TABLE sessions ADD COLUMN user_id INTEGER;")
        except psycopg2.errors.DuplicateColumn:
            conn.rollback()
        
        conn.commit()
        conn.close()
        logger.info("DB Initialized")
    except Exception as e:
        logger.error(f"DB INIT ERROR: {e}")

init_db()

class User(UserMixin):
    def __init__(self, id, email, name, cv_content, sub_expires):
        self.id = id; self.email = email; self.name = name; self.cv_content = cv_content; self.sub_expires = sub_expires
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

# --- CORE LOGIC ---

def generate_tts(text):
    # Fallback TTS simple et rapide via Google Translate API (non-officiel mais efficace pour MVP)
    try:
        url = f"https://translate.google.com/translate_tts?ie=UTF-8&client=tw-ob&tl=en&q={quote(text[:900])}"
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5)
        if r.status_code == 200:
            return base64.b64encode(r.content).decode('utf-8')
    except Exception as e:
        logger.error(f"TTS Error: {e}")
    return None

def get_ai_prompt(name, job, company, cv, history_len):
    # Logique simplifiée pour éviter les prompts trop longs
    stage = "INTRODUCTION"
    if history_len > 2: stage = "DEEP DIVE"
    if history_len > 8: stage = "CONCLUSION"
    
    cv_excerpt = (cv[:4000] + '...') if cv and len(cv) > 4000 else (cv or "No CV provided")
    
    return (f"You are an expert tech recruiter at {company}. Interviewing {name} for {job}.\n"
            f"STAGE: {stage}.\nCV CONTEXT: {cv_excerpt}\n"
            "Keep responses concise (max 3 sentences). Be professional yet challenging.\n"
            "OUTPUT JSON: {'coach_response_text': 'string', 'transcription_user': 'string', "
            "'score_pronunciation': int(0-10), 'feedback_grammar': 'string (brief)', "
            "'better_response_example': 'string', 'next_step_advice': 'string'}")

# --- ROUTES ---

@app.route('/')
def index(): return app.send_static_file('index.html')

@app.route('/health')
def health(): return jsonify({"status": "alive"})

@app.route('/auth/login')
def login(): return google.authorize_redirect(url_for('authorize', _external=True))

@app.route('/auth/callback')
def authorize():
    try:
        token = google.authorize_access_token()
        resp = google.get('userinfo')
        user_info = resp.json()
        conn = get_db_connection()
        cur = conn.cursor()
        # Upsert user
        cur.execute("""
            INSERT INTO users (google_id, email, name) 
            VALUES (%s, %s, %s) 
            ON CONFLICT (google_id) DO UPDATE SET name = EXCLUDED.name 
            RETURNING *;
        """, (user_info['id'], user_info['email'], user_info['name']))
        u = cur.fetchone()
        conn.commit()
        conn.close()
        login_user(User(u['id'], u['email'], u['name'], u['cv_content'], u['sub_expires']))
        return redirect('/')
    except Exception as e:
        logger.error(f"Auth Failed: {e}")
        return redirect('/')

@app.route('/logout')
@login_required
def logout(): logout_user(); return redirect('/')

@app.route('/api/me')
def get_me():
    if not current_user.is_authenticated: return jsonify({"logged_in": False})
    return jsonify({
        "logged_in": True, 
        "name": current_user.name, 
        "is_paid": current_user.is_paid, 
        "saved_cv": current_user.cv_content
    })

@app.route('/upload_cv', methods=['POST'])
@login_required
def upload_cv():
    if 'file' not in request.files: return jsonify({"error": "No file part"}), 400
    file = request.files['file']
    if file.filename == '': return jsonify({"error": "No selected file"}), 400
    
    try:
        text = ""
        filename = file.filename.lower()
        if filename.endswith('.pdf'):
            reader = PdfReader(file)
            for page in reader.pages: text += (page.extract_text() or "") + "\n"
        elif filename.endswith('.docx'):
            doc = Document(file)
            for para in doc.paragraphs: text += para.text + "\n"
        elif filename.endswith('.txt'):
            text = file.read().decode('utf-8', errors='ignore')
        else:
            return jsonify({"error": "Unsupported file type"}), 400
            
        return jsonify({"status": "ok", "text": text.strip()})
    except Exception as e:
        logger.error(f"CV Upload Error: {e}")
        return jsonify({"error": "Error processing file"}), 500

@app.route('/start_chat', methods=['POST'])
@login_required
def start_chat():
    if not current_user.is_paid: return jsonify({"error": "Upgrade required"}), 403
    data = request.json
    
    # Update CV if provided
    new_cv = data.get('cv_content')
    conn = get_db_connection()
    cur = conn.cursor()
    if new_cv:
        cur.execute("UPDATE users SET cv_content = %s WHERE id = %s", (new_cv, current_user.id))
    
    # Create Session
    cur.execute("""
        INSERT INTO sessions (session_id, user_id, candidate_name, job_title, company_type, cv_content) 
        VALUES (%s, %s, %s, %s, %s, %s) 
        ON CONFLICT (session_id) DO NOTHING
    """, (data.get('session_id'), current_user.id, data.get('candidate_name'), 
          data.get('job_title'), data.get('company_type'), new_cv or current_user.cv_content))
    conn.commit()
    conn.close()
    
    first_msg = f"Hello {data.get('candidate_name', 'there')}. I see you're applying for the {data.get('job_title')} position. Tell me about yourself."
    return jsonify({
        "coach_response_text": first_msg,
        "audio_base64": generate_tts(first_msg)
    })

@app.route('/analyze', methods=['POST'])
@login_required
def analyze():
    if not current_user.is_paid: return jsonify({"error": "Upgrade required"}), 403
    
    session_id = request.form.get('session_id')
    audio_file = request.files.get('audio')
    
    if not audio_file: return jsonify({"error": "No audio"}), 400

    temp_webm = tempfile.NamedTemporaryFile(suffix=".webm", delete=False)
    temp_mp3 = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    
    try:
        # 1. Save Webm
        audio_file.save(temp_webm.name)
        temp_webm.close() # Important: close handle before pydub opens it
        
        # 2. Convert to MP3 (Docker FFmpeg check)
        # Note: Render usually puts ffmpeg in /usr/bin/ffmpeg
        AudioSegment.converter = "/usr/bin/ffmpeg" 
        audio = AudioSegment.from_file(temp_webm.name)
        audio.export(temp_mp3.name, format="mp3")
        temp_mp3.close()

        # 3. Get Context
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM sessions WHERE session_id=%s", (session_id,))
        sess = cur.fetchone()
        cur.execute("SELECT role, content FROM history WHERE session_id=%s ORDER BY id ASC LIMIT 10", (session_id,))
        rows = cur.fetchall()
        
        chat_history = [{"role": r['role'], "parts": [r['content']]} for r in rows]
        
        # 4. Gemini Call
        sys_instr = get_ai_prompt(sess['candidate_name'], sess['job_title'], sess['company_type'], sess['cv_content'], len(rows))
        model = genai.GenerativeModel(MODEL_NAME, system_instruction=sys_instr)
        chat = model.start_chat(history=chat_history)
        
        uploaded_file = genai.upload_file(temp_mp3.name, mime_type="audio/mp3")
        
        # Wait for processing
        attempt = 0
        while uploaded_file.state.name == "PROCESSING" and attempt < 10:
            time.sleep(1)
            uploaded_file = genai.get_file(uploaded_file.name)
            attempt += 1

        response = chat.send_message([uploaded_file, "Listen and Respond."], generation_config={"response_mime_type": "application/json"})
        
        # 5. Parse JSON
        try:
            res_text = response.text.strip()
            if res_text.startswith('```json'): res_text = res_text[7:-3]
            ai_data = json.loads(res_text)
        except:
            # Fallback if JSON fails
            ai_data = {"coach_response_text": response.text, "transcription_user": "(Error transcribing)", "score_pronunciation": 5}

        # 6. Save History
        cur.execute("INSERT INTO history (session_id, role, content) VALUES (%s, 'user', %s)", (session_id, ai_data.get('transcription_user')))
        cur.execute("INSERT INTO history (session_id, role, content) VALUES (%s, 'model', %s)", (session_id, ai_data.get('coach_response_text')))
        conn.commit()
        conn.close()

        # 7. Add Audio
        ai_data['audio_base64'] = generate_tts(ai_data.get('coach_response_text', ''))
        
        return jsonify(ai_data)

    except Exception as e:
        logger.error(f"Analyze Error: {e}")
        return jsonify({"error": str(e)}), 500
        
    finally:
        # Cleanup crucial
        if os.path.exists(temp_webm.name): os.unlink(temp_webm.name)
        if os.path.exists(temp_mp3.name): os.unlink(temp_mp3.name)

# Payment & Promo Logic (Simplified for brevity but functional)
@app.route('/api/payment_success', methods=['POST'])
@login_required
def payment_success():
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE users SET sub_expires = %s WHERE id = %s", (datetime.datetime.now() + datetime.timedelta(days=90), current_user.id))
    conn.commit(); conn.close()
    return jsonify({"status": "ok"})

@app.route('/api/promo_code', methods=['POST'])
@login_required
def promo_code():
    code = request.json.get('code', '').upper()
    if code == "ZEROMONEY":
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("UPDATE users SET sub_expires = %s WHERE id = %s", (datetime.datetime.now() + datetime.timedelta(days=3650), current_user.id))
        conn.commit(); conn.close()
        return jsonify({"status": "free_access_granted", "message": "VIP Access Granted"})
    return jsonify({"status": "invalid", "message": "Invalid Code"}), 400

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)