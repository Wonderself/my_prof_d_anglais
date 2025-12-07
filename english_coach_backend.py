import os, sys, tempfile, json, datetime, time, shutil, base64
from dotenv import load_dotenv
from flask import Flask, request, jsonify
from flask_cors import CORS
from pydub import AudioSegment
import google.generativeai as genai
import psycopg2
from psycopg2.extras import RealDictCursor
import requests # <--- NOUVEL IMPORT OBLIGATOIRE
from urllib.parse import quote # <--- NOUVEL IMPORT OBLIGATOIRE

# --- CONFIGURATION ---
load_dotenv(override=True)

API_KEY = os.getenv("GOOGLE_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL") 
MODEL_NAME = 'gemini-2.5-flash' 

if not API_KEY: sys.exit("❌ CLÉ GEMINI MANQUANTE")
if not DATABASE_URL: sys.exit("❌ DATABASE_URL MANQUANTE")

try:
    genai.configure(api_key=API_KEY.strip())
except Exception as e: sys.exit(f"❌ ERREUR GEMINI: {e}")

app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app)

# --- DB ---
def get_db_connection():
    try:
        return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    except Exception as e:
        print(f"⚠️ Erreur DB: {e}")
        return None

def init_db():
    conn = get_db_connection()
    if not conn: return
    try:
        cur = conn.cursor()
        cur.execute('''CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, candidate_name TEXT, job_title TEXT, company_type TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);''')
        cur.execute('''CREATE TABLE IF NOT EXISTS history (id SERIAL PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP);''')
        conn.commit()
        conn.close()
        print("✅ DB Initialisée")
    except: pass

init_db()

def save_msg(sid, role, txt):
    conn = get_db_connection()
    if not conn: return
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO history (session_id, role, content, timestamp) VALUES (%s, %s, %s, NOW())", (sid, role, txt))
        conn.commit()
        conn.close()
    except: pass

def get_hist(sid):
    conn = get_db_connection()
    if not conn: return []
    try:
        cur = conn.cursor()
        cur.execute("SELECT role, content FROM history WHERE session_id = %s ORDER BY id ASC", (sid,))
        rows = cur.fetchall()
        conn.close()
        return [{"role": r['role'], "parts": [r['content']]} for r in rows[-20:]]
    except: return []

def get_sess(sid):
    conn = get_db_connection()
    if not conn: return None
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM sessions WHERE session_id = %s", (sid,))
        row = cur.fetchone()
        conn.close()
        return row
    except: return None

# --- AUDIO BASSE QUALITÉ (GOOGLE TRANSLATE TTS - ROBUSTE) ---
def generate_ai_voice(text):
    """Utilise l'API Google Translate TTS pour garantir l'audio."""
    try:
        # Nettoyage et encodage du texte pour l'URL
        clean_text = text.replace('\n', ' ').strip()
        encoded_text = quote(clean_text)
        
        # Endpoint public TTS (tl=en pour l'anglais)
        url = f"https://translate.google.com/translate_tts?ie=UTF-8&client=tw-ob&tl=en&q={encoded_text}"
        
        # Appel API simple (synchrone, pas de processus asynchrone qui plante)
        response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'})
        response.raise_for_status() # Lève une exception si le statut n'est pas 200
        
        # Conversion en Base64
        b64 = base64.b64encode(response.content).decode('utf-8')
        return b64
        
    except Exception as e:
        print(f"❌ CRITIQUE: Échec Audio G-TTS: {e}")
        # Laisse l'application répondre sans audio (Plan B si même G-TTS plante)
        return ""

# --- GEMINI ---
def clean_json(text):
    t = text.strip()
    if t.startswith("`" * 3):
        lines = t.split('\n')
        if len(lines) > 2: return "\n".join(lines[1:-1]).strip()
    return t

def get_sys_prompt(name, job, company):
    return (f"ROLE: Coach Mike, recruiter at {company}. Interviewing {name} for {job}.\n"
            f"STYLE: Short questions (1-2 sentences MAX). One at a time. Tough on English.\n"
            f"OUTPUT: JSON with fields: coach_response_text, transcription_user, score_pronunciation, feedback_grammar, better_response_example, next_step_advice.")

SCHEMA = {"type": "OBJECT", "properties": {
    "coach_response_text": {"type": "STRING"}, "transcription_user": {"type": "STRING"},
    "score_pronunciation": {"type": "NUMBER"}, "feedback_grammar": {"type": "STRING"}, 
    "better_response_example": {"type": "STRING"}, "next_step_advice": {"type": "STRING"}},
    "required": ["coach_response_text", "transcription_user", "score_pronunciation", "better_response_example"]}

# --- ROUTES ---
@app.route('/')
def index(): return app.send_static_file('index.html')

@app.route('/health', methods=['GET'])
def health():
    conn = get_db_connection()
    status = "ok" if conn else "disconnected"
    if conn: conn.close()
    return jsonify({"status": "ok", "db": status})

@app.route('/start_chat', methods=['POST'])
def start_chat():
    d = request.json
    sid = d.get('session_id')
    conn = get_db_connection()
    if conn:
        cur = conn.cursor()
        cur.execute("""INSERT INTO sessions (session_id, candidate_name, job_title, company_type, created_at) VALUES (%s, %s, %s, %s, NOW()) ON CONFLICT (session_id) DO UPDATE SET candidate_name = EXCLUDED.candidate_name, job_title = EXCLUDED.job_title, company_type = EXCLUDED.company_type""", (sid, d['candidate_name'], d['job_title'], d['company_type']))
        conn.commit()
        conn.close()

    msg = f"Hi {d['candidate_name']}. I'm Mike. Let's start the interview for {d['job_title']}. Tell me about yourself."
    save_msg(sid, "model", msg)
    
    # Génération audio (maintenant G-TTS)
    audio = generate_ai_voice(msg)
    
    return jsonify({"coach_response_text": msg, "audio_base64": audio, "transcription_user": "", "score_pronunciation": 10, "feedback_grammar": "", "better_response_example": "N/A"})

@app.route('/analyze', methods=['POST'])
def analyze():
    sid = request.form.get('session_id')
    f = request.files.get('audio')
    sess = get_sess(sid)
    if not sess: return jsonify({"error": "Session lost"}), 404

    path, mime = None, "video/webm"
    try:
        fd, t_webm = tempfile.mkstemp(suffix=".webm")
        os.close(fd)
        f.save(t_webm)
        path = t_webm
        if shutil.which("ffmpeg"):
            try:
                fd2, t_mp3 = tempfile.mkstemp(suffix=".mp3")
                os.close(fd2)
                AudioSegment.from_file(t_webm).export(t_mp3, format="mp3")
                path, mime = t_mp3, "audio/mp3"
            except: pass
    except: return jsonify({"error": "File error"}), 500

    try:
        model = genai.GenerativeModel(MODEL_NAME, system_instruction=get_sys_prompt(sess['candidate_name'], sess['job_title'], sess['company_type']))
        chat = model.start_chat(history=get_hist(sid))
        
        u_file = genai.upload_file(path, mime_type=mime)
        retry = 0
        while u_file.state.name == "PROCESSING" and retry < 10: 
            time.sleep(0.5)
            u_file = genai.get_file(u_file.name)
            retry += 1
            
        resp = chat.send_message([u_file, "Analyze."], generation_config=genai.GenerationConfig(response_mime_type="application/json", response_schema=SCHEMA))
        
        try:
            genai.delete_file(u_file.name)
            os.remove(t_webm)
            if mime == "audio/mp3": os.remove(path)
        except: pass

        res = json.loads(clean_json(resp.text))
        save_msg(sid, "user", res.get("transcription_user", "..."))
        save_msg(sid, "model", res.get("coach_response_text", ""))
        
        # Génération audio (maintenant G-TTS)
        res["audio_base64"] = generate_ai_voice(res.get("coach_response_text"))
        
        return jsonify(res)
    except Exception as e:
        print(f"CRITICAL: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)