import os, sys, tempfile, json, time, shutil, base64
from dotenv import load_dotenv
from flask import Flask, request, jsonify
from flask_cors import CORS
from pydub import AudioSegment
import google.generativeai as genai
import psycopg2
from psycopg2.extras import RealDictCursor
import requests 
from urllib.parse import quote 

# --- CONFIGURATION ---
load_dotenv(override=True)

API_KEY = os.getenv("GOOGLE_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL") 
MODEL_NAME = 'gemini-2.0-flash-exp' # Or 'gemini-1.5-flash' depending on availability
COACH_NAME = 'Sarah' 

if not API_KEY: sys.exit("❌ MISSING GOOGLE_API_KEY")
if not DATABASE_URL: sys.exit("❌ MISSING DATABASE_URL")

try:
    genai.configure(api_key=API_KEY.strip())
except Exception as e: sys.exit(f"❌ GEMINI CONFIG ERROR: {e}")

app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app)

# --- DATABASE ---
def get_db_connection():
    try:
        return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    except Exception as e:
        print(f"⚠️ DB Connection Error: {e}")
        return None

def init_db():
    conn = get_db_connection()
    if not conn: return
    try:
        cur = conn.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                candidate_name TEXT,
                job_title TEXT,
                company_type TEXT,
                cv_content TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS history (
                id SERIAL PRIMARY KEY, 
                session_id TEXT, 
                role TEXT, 
                content TEXT, 
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')
        conn.commit()
        conn.close()
        print("✅ DB Initialized (V4.1)")
    except Exception as e: 
        print(f"❌ Init DB Error: {e}")

init_db()

# --- HELPERS ---
def save_msg(sid, role, txt):
    conn = get_db_connection()
    if not conn: return
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO history (session_id, role, content) VALUES (%s, %s, %s)", (sid, role, txt))
        conn.commit()
        conn.close()
    except Exception as e: print(f"Save Error: {e}")

def get_hist(sid):
    conn = get_db_connection()
    if not conn: return []
    try:
        cur = conn.cursor()
        cur.execute("SELECT role, content FROM history WHERE session_id = %s ORDER BY id ASC", (sid,))
        rows = cur.fetchall()
        conn.close()
        # Ensure we only send valid Gemini roles (user/model)
        valid_hist = []
        for r in rows[-20:]: # Limit context window
            gemini_role = "user" if r['role'] == "user" else "model"
            valid_hist.append({"role": gemini_role, "parts": [r['content']]})
        return valid_hist
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

def generate_ai_voice(text):
    """Google Translate TTS as fallback"""
    try:
        clean_text = text.replace('\n', ' ').strip()
        encoded_text = quote(clean_text)
        url = f"https://translate.google.com/translate_tts?ie=UTF-8&client=tw-ob&tl=en&q={encoded_text}"
        response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'})
        response.raise_for_status()
        return base64.b64encode(response.content).decode('utf-8')
    except Exception as e:
        print(f"TTS Error: {e}")
        return "" 

def clean_json(text):
    t = text.strip()
    if t.startswith("`" * 3):
        lines = t.split('\n')
        if len(lines) > 2: return "\n".join(lines[1:-1]).strip()
    return t

# --- PROMPT LOGIC ---
def get_sys_prompt(name, job, company, cv_content=None):
    
    cv_context = ""
    if cv_content and len(cv_content) > 10:
        cv_context = (
            f"IMPORTANT CONTEXT: The candidate's Resume/CV is provided below. "
            f"Use the specific details (skills, past companies, achievements) from this CV to ask relevant questions "
            f"and to generate the 'better_response_example'.\n"
            f"--- START CV ---\n{cv_content}\n--- END CV ---\n"
        )
    
    return (
        f"You are {COACH_NAME}, a professional recruiter at {company}. "
        f"You are interviewing {name} for the position of {job}.\n\n"
        f"{cv_context}\n"
        f"RULES:\n"
        f"1. Ask ONE concise question at a time (max 2 sentences).\n"
        f"2. Be professional but challenging.\n"
        f"3. Language: ENGLISH ONLY.\n"
        f"4. OUTPUT FORMAT: JSON Only.\n\n"
        f"JSON SCHEMA:\n"
        f"{{"
        f"  'coach_response_text': 'Your next question or reaction',"
        f"  'transcription_user': 'What the user said (transcribed)',"
        f"  'score_pronunciation': Number (0-10),"
        f"  'feedback_intonation': 'Brief comment on tone',"
        f"  'feedback_grammar': 'Fix English mistakes if any, else empty string',"
        f"  'better_response_example': 'An ideal, professional answer the candidate COULD have given based on their CV/Context',"
        f"  'next_step_advice': 'A quick tip for the next answer'"
        f"}}"
    )

SCHEMA = {
    "type": "OBJECT", 
    "properties": {
        "coach_response_text": {"type": "STRING"}, 
        "transcription_user": {"type": "STRING"},
        "score_pronunciation": {"type": "NUMBER"}, 
        "feedback_grammar": {"type": "STRING"}, 
        "better_response_example": {"type": "STRING"}, 
        "next_step_advice": {"type": "STRING"}
    },
    "required": ["coach_response_text", "score_pronunciation", "better_response_example"]
}

# --- ROUTES ---

@app.route('/')
def index(): return app.send_static_file('index.html')

@app.route('/health')
def health():
    conn = get_db_connection()
    status = "ok" if conn else "error"
    if conn: conn.close()
    return jsonify({"status": status})

@app.route('/start_chat', methods=['POST'])
def start_chat():
    d = request.json
    sid = d.get('session_id')
    
    # sanitize CV Content to remove Null Bytes which crash Postgres
    raw_cv = d.get('cv_content', '')
    if raw_cv:
        raw_cv = raw_cv.replace('\x00', '').strip()

    conn = get_db_connection()
    if conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO sessions (session_id, candidate_name, job_title, company_type, cv_content)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (session_id) DO UPDATE 
            SET candidate_name = EXCLUDED.candidate_name, 
                job_title = EXCLUDED.job_title, 
                company_type = EXCLUDED.company_type,
                cv_content = EXCLUDED.cv_content
        """, (sid, d['candidate_name'], d['job_title'], d['company_type'], raw_cv))
        conn.commit()
        conn.close()

    first_msg = f"Hello {d['candidate_name']}. Thank you for joining. I've reviewed your application for the {d['job_title']} role. Tell me, can you briefly introduce yourself?"
    
    # If CV provided, make the intro more specific
    if raw_cv:
        first_msg = f"Hello {d['candidate_name']}. I have your CV in front of me. To start, could you walk me through your relevant experience for this {d['job_title']} position?"

    save_msg(sid, "model", first_msg)
    audio = generate_ai_voice(first_msg)
    
    return jsonify({
        "coach_response_text": first_msg, 
        "audio_base64": audio
    })

@app.route('/analyze', methods=['POST'])
def analyze():
    sid = request.form.get('session_id')
    f = request.files.get('audio')
    
    sess = get_sess(sid)
    if not sess: return jsonify({"error": "Session not found"}), 404

    # 1. Process Audio
    path, mime = None, "video/webm"
    try:
        fd, t_webm = tempfile.mkstemp(suffix=".webm")
        os.close(fd)
        f.save(t_webm)
        path = t_webm
        
        # Convert if ffmpeg available (Gemini prefers mp3/wav sometimes)
        if shutil.which("ffmpeg"):
            try:
                fd2, t_mp3 = tempfile.mkstemp(suffix=".mp3")
                os.close(fd2)
                AudioSegment.from_file(t_webm).export(t_mp3, format="mp3")
                path, mime = t_mp3, "audio/mp3"
            except: pass
    except: return jsonify({"error": "Audio processing failed"}), 500

    # 2. Gemini
    try:
        sys_prompt = get_sys_prompt(sess['candidate_name'], sess['job_title'], sess['company_type'], cv_content=sess['cv_content'])
        model = genai.GenerativeModel(MODEL_NAME, system_instruction=sys_prompt)
        
        hist = get_hist(sid)
        chat = model.start_chat(history=hist)
        
        u_file = genai.upload_file(path, mime_type=mime)
        
        # Wait for processing
        for _ in range(10):
            if u_file.state.name == "ACTIVE": break
            time.sleep(0.5)
            u_file = genai.get_file(u_file.name)
            
        resp = chat.send_message([u_file, "Listen to the answer and respond in JSON."], generation_config=genai.GenerationConfig(response_mime_type="application/json", response_schema=SCHEMA))
        
        # Cleanup
        try:
            genai.delete_file(u_file.name)
            os.remove(t_webm)
            if mime == "audio/mp3": os.remove(path)
        except: pass

        res = json.loads(clean_json(resp.text))
        
        # Save to DB
        save_msg(sid, "user", res.get("transcription_user", "(Audio)"))
        save_msg(sid, "model", res.get("coach_response_text", ""))
        
        res["audio_base64"] = generate_ai_voice(res.get("coach_response_text"))
        
        return jsonify(res)
        
    except Exception as e:
        print(f"CRITICAL GEMINI ERROR: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)