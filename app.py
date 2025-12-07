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
API_KEY = os.getenv("GOOGLE_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
MODEL_NAME = 'gemini-1.5-flash' 
COACH_NAME = 'Sarah'

if not API_KEY or not DATABASE_URL:
    print("⚠️ WARNING: Keys missing. Ensure they are set in Render Dashboard.")

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
        # TTS Google rapide et gratuit (Fallback robuste)
        url = f"https://translate.google.com/translate_tts?ie=UTF-8&client=tw-ob&tl=en&q={quote(text[:500])}"
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5)
        if r.status_code == 200: return base64.b64encode(r.content).decode('utf-8')
    except: pass
    return None

def get_prompt(name, job, company, cv, history_len):
    """
    Prompt Engineering Avancé pour structurer l'entretien et forcer l'usage du CV.
    """
    # Détection approximative de l'étape de l'entretien basée sur la longueur de l'historique
    stage = "INTRODUCTION"
    if history_len > 2: stage = "DEEP DIVE EXPERIENCE (Challenge the CV)"
    if history_len > 6: stage = "HARD SKILLS & TECHNICAL"
    if history_len > 10: stage = "SOFT SKILLS & BEHAVIORAL"
    if history_len > 14: stage = "CLOSING"

    cv_context = f"\n=== CANDIDATE CV (CRITICAL SOURCE) ===\n{cv[:5000]}\n=== END CV ===\n" if cv else ""
    
    return (
        f"ROLE: You are {COACH_NAME}, an expert recruiter at {company}. Interviewing {name} for {job}.\n"
        f"CURRENT STAGE: {stage}.\n"
        f"{cv_context}"
        f"GOAL: Conduct a realistic, structured interview. Be professional but tough.\n"
        f"RULES:\n"
        f"1. Ask ONE short question at a time (max 15 words).\n"
        f"2. Do NOT be repetitive. Follow the flow: Intro -> Exp -> Tech -> Closing.\n"
        f"3. AUDIO ANALYSIS: The user sends audio transcription. Detect grammar mistakes.\n"
        f"4. MASTERCLASS LOGIC (CRITICAL): When generating 'better_response_example', YOU MUST USE FACTS FROM THE CV provided above. "
        f"Do not give generic advice. Rewrite the user's answer to be a 'Perfect Answer' using their specific dates, companies, and achievements found in the CV text.\n"
        f"JSON OUTPUT FORMAT: {{'coach_response_text': 'Your spoken question', 'transcription_user': 'User text', "
        f"'score_pronunciation': 0-10, 'feedback_grammar': 'Correction', "
        f"'better_response_example': 'The MASTERCLASS answer using CV details', "
        f"'next_step_advice': 'Short tip'}}"
    )

# --- ROUTES ---
@app.route('/')
def index(): return app.send_static_file('index.html')

@app.route('/health')
def health(): return jsonify({"status": "alive"})

@app.route('/start_chat', methods=['POST'])
def start_chat():
    d = request.json
    sid = d.get('session_id')
    conn = get_db_connection()
    if conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO sessions (session_id, candidate_name, job_title, company_type, cv_content) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (session_id) DO NOTHING", 
                   (sid, d.get('candidate_name'), d.get('job_title'), d.get('company_type'), d.get('cv_content','')))
        conn.commit()
        conn.close()
    
    msg = f"Hello {d.get('candidate_name')}. I'm {COACH_NAME}. I've reviewed your CV. Let's start. Briefly introduce yourself."
    return jsonify({"coach_response_text": msg, "audio_base64": generate_tts(msg), "transcription_user": ""})

@app.route('/analyze', methods=['POST'])
def analyze():
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
        cur.execute("SELECT role, content FROM history WHERE session_id=%s ORDER BY id ASC", (sid,)) # On prend tout pour le contexte
        hist_rows = cur.fetchall()
        conn.close()

        # On garde les 10 derniers échanges pour le contexte immédiat
        hist = [{"role": r['role'], "parts": [r['content']]} for r in hist_rows[-10:]]
        history_len = len(hist_rows) # On utilise la longueur totale pour savoir où on en est dans l'ITV
        
        sys_prompt = get_prompt(sess['candidate_name'], sess['job_title'], sess['company_type'], sess['cv_content'], history_len)
        
        model = genai.GenerativeModel(MODEL_NAME, system_instruction=sys_prompt)
        chat = model.start_chat(history=hist)
        
        uf = genai.upload_file(mp3_path, mime_type="audio/mp3")
        while uf.state.name == "PROCESSING": time.sleep(0.5); uf = genai.get_file(uf.name)
        
        resp = chat.send_message([uf, "Analyze this response."], generation_config={"response_mime_type": "application/json"})
        
        try:
            data = json.loads(resp.text)
        except:
            # Fallback nettoyage markdown
            clean = resp.text.replace('```json', '').replace('```', '').strip()
            data = json.loads(clean)
        
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
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)