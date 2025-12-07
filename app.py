import os, sys, tempfile, json, time, shutil, base64
from flask import Flask, request, jsonify
from flask_cors import CORS
from pydub import AudioSegment
import google.generativeai as genai
import psycopg2
from psycopg2.extras import RealDictCursor
import requests 
from urllib.parse import quote 

# --- CONFIG (CLOUD) ---
# Sur Render, les variables sont injectées directement par le dashboard.
API_KEY = os.getenv("GOOGLE_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
MODEL_NAME = 'gemini-1.5-flash' 
COACH_NAME = 'Sarah'

# Vérification silencieuse (pour éviter de faire crasher le build si les vars arrivent après)
if not API_KEY or not DATABASE_URL:
    print("⚠️ WARNING: API Keys not found via os.getenv. Ensure they are set in Render Dashboard.")

try:
    if API_KEY: genai.configure(api_key=API_KEY)
except Exception as e:
    print(f"⚠️ GEMINI CONFIG ERROR: {e}")

app = Flask(__name__, static_folder='static', static_url_path='')
CORS(app)

# --- DATABASE ---
def get_db_connection():
    try:
        return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor, sslmode='require')
    except Exception as e:
        print(f"⚠️ DB ERROR: {e}")
        return None

def init_db():
    conn = get_db_connection()
    if not conn: return
    try:
        cur = conn.cursor()
        cur.execute('''CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, candidate_name TEXT, job_title TEXT, company_type TEXT, cv_content TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);''')
        cur.execute('''CREATE TABLE IF NOT EXISTS history (id SERIAL PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP);''')
        conn.commit()
        conn.close()
    except Exception as e: print(f"DB INIT ERROR: {e}")

init_db()

# --- AUDIO & AI ---
def generate_tts(text):
    try:
        url = f"https://translate.google.com/translate_tts?ie=UTF-8&client=tw-ob&tl=en&q={quote(text[:1000])}"
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5)
        if r.status_code == 200: return base64.b64encode(r.content).decode('utf-8')
    except: pass
    return None

def get_prompt(name, job, company, cv):
    cv_txt = f"\nCV INFO:\n{cv[:4000]}" if cv else ""
    return (f"ROLE: Recruiter {COACH_NAME} at {company}. Interviewing {name} for {job}.{cv_txt}\n"
            "STYLE: 1 short question at a time. Tough. Detect grammar errors.\n"
            "JSON OUTPUT: {coach_response_text, transcription_user, score_pronunciation (0-10), "
            "feedback_grammar, better_response_example, next_step_advice}")

# --- ROUTES ---
@app.route('/')
def index(): return app.send_static_file('index.html')

@app.route('/health')
def health(): return jsonify({"status": "alive"})

@app.route('/start_chat', methods=['POST'])
def start_chat():
    d = request.json
    sid = d.get('session_id')
    # Save session
    conn = get_db_connection()
    if conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO sessions (session_id, candidate_name, job_title, company_type, cv_content) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (session_id) DO NOTHING", 
                   (sid, d.get('candidate_name'), d.get('job_title'), d.get('company_type'), d.get('cv_content','')))
        conn.commit()
        conn.close()
    
    msg = f"Hello {d.get('candidate_name')}. I'm {COACH_NAME}. Tell me about yourself."
    return jsonify({"coach_response_text": msg, "audio_base64": generate_tts(msg), "transcription_user": ""})

@app.route('/analyze', methods=['POST'])
def analyze():
    sid = request.form.get('session_id')
    f = request.files.get('audio')
    if not f or not sid: return jsonify({"error": "No audio"}), 400

    # 1. Save temp file
    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp:
        f.save(tmp.name)
        webm_path = tmp.name
    
    mp3_path = webm_path + ".mp3"
    
    try:
        # 2. Convert to MP3 (FFmpeg via Docker)
        AudioSegment.from_file(webm_path).export(mp3_path, format="mp3")
        
        # 3. Get Context
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM sessions WHERE session_id=%s", (sid,))
        sess = cur.fetchone()
        cur.execute("SELECT role, content FROM history WHERE session_id=%s ORDER BY id DESC LIMIT 10", (sid,))
        hist_rows = cur.fetchall()
        conn.close()

        hist = [{"role": r['role'], "parts": [r['content']]} for r in reversed(hist_rows)]
        
        # 4. Gemini
        model = genai.GenerativeModel(MODEL_NAME, system_instruction=get_prompt(sess['candidate_name'], sess['job_title'], sess['company_type'], sess['cv_content']))
        chat = model.start_chat(history=hist)
        
        uf = genai.upload_file(mp3_path, mime_type="audio/mp3")
        while uf.state.name == "PROCESSING": time.sleep(1); uf = genai.get_file(uf.name)
        
        resp = chat.send_message([uf, "Analyze."], generation_config={"response_mime_type": "application/json"})
        data = json.loads(resp.text)
        
        # 5. Save & TTS
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("INSERT INTO history (session_id, role, content) VALUES (%s, 'user', %s)", (sid, data.get('transcription_user','')))
        cur.execute("INSERT INTO history (session_id, role, content) VALUES (%s, 'model', %s)", (sid, data.get('coach_response_text','')))
        conn.commit()
        conn.close()
        
        data['audio_base64'] = generate_tts(data.get('coach_response_text'))
        return jsonify(data)

    except Exception as e:
        print(f"ERROR: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        if os.path.exists(webm_path): os.remove(webm_path)
        if os.path.exists(mp3_path): os.remove(mp3_path)

if __name__ == '__main__':
    # C'EST ICI QUE LA MAGIE OPÈRE POUR RENDER
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)