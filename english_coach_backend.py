import os, sys, tempfile, json, datetime, time, shutil, base64
from dotenv import load_dotenv
from flask import Flask, request, jsonify
from flask_cors import CORS
from pydub import AudioSegment
import google.generativeai as genai
import psycopg2
from psycopg2.extras import RealDictCursor
import requests 
from urllib.parse import quote 

# --- CONFIGURATION INITIALE ---
load_dotenv(override=True)

API_KEY = os.getenv("GOOGLE_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL") 
MODEL_NAME = 'gemini-2.5-flash' 
COACH_NAME = 'Sarah' 

if not API_KEY: sys.exit("❌ CLÉ GEMINI MANQUANTE")
if not DATABASE_URL: sys.exit("❌ DATABASE_URL MANQUANTE")

try:
    genai.configure(api_key=API_KEY.strip())
except Exception as e: sys.exit(f"❌ ERREUR CONFIG GEMINI: {e}")

# Initialisation de l'application Flask
app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app)

# --- GESTION DE LA BASE DE DONNÉES (POSTGRES) ---
def get_db_connection():
    """Crée et retourne une connexion à PostgreSQL."""
    try:
        return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    except Exception as e:
        print(f"⚠️ Erreur DB: {e}")
        return None

def init_db():
    """Initialise les tables si elles n'existent pas."""
    conn = get_db_connection()
    if not conn: 
        print("❌ Impossible d'initialiser la DB, connexion échouée.")
        return
    try:
        cur = conn.cursor()
        # SCHEMA MIS À JOUR : Ajout de cv_content
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
        cur.execute('''CREATE TABLE IF NOT EXISTS history (id SERIAL PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP);''')
        conn.commit()
        conn.close()
        print("✅ DB Initialisée (Schema V4.0)")
    except Exception as e: 
        print(f"❌ Erreur Init DB: {e}")

init_db()

# --- Fonctions de l'Historique ---

def save_msg(sid, role, txt):
    """Enregistre un message dans l'historique."""
    conn = get_db_connection()
    if not conn: return
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO history (session_id, role, content, timestamp) VALUES (%s, %s, %s, NOW())", (sid, role, txt))
        conn.commit()
        conn.close()
    except Exception as e: print(f"Save Error: {e}")

def get_hist(sid):
    """Récupère l'historique pour Gemini (20 dernières entrées)."""
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
    """Récupère les détails de la session."""
    conn = get_db_connection()
    if not conn: return None
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM sessions WHERE session_id = %s", (sid,))
        row = cur.fetchone()
        conn.close()
        return row
    except: return None

# --- AUDIO G-TTS (Robuste) ---
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
        return "" 

# --- LOGIQUE GEMINI (Masterclass) ---

def clean_json(text):
    """Nettoie le bloc de code Markdown autour du JSON de Gemini."""
    t = text.strip()
    if t.startswith("`" * 3):
        lines = t.split('\n')
        if len(lines) > 2: return "\n".join(lines[1:-1]).strip()
    return t

def get_sys_prompt(name, job, company, cv_content=None):
    """Crée l'instruction système pour Gemini avec injection du CV."""
    cv_injection = ""
    if cv_content and cv_content.strip():
        cv_injection = (
            f"CONTEXTE CLÉ: Le CV/Résumé de {name} est fourni ci-dessous. "
            f"Utilisez les expériences, compétences et réalisations listées dans ce CV pour améliorer la qualité et la pertinence de tous les exemples de 'better_response_example' que vous fournirez. "
            f"Assurez-vous que les réponses modèles (masterclass) sont directement liées au CV. \n"
            f"--- CV FOURNI ---\n{cv_content}\n"
            f"--- FIN CV ---\n"
        )
    
    return (
        f"ROLE: Coach {COACH_NAME}, recruiter at {company}. Interviewing {name} for {job}.\n"
        f"STYLE: Short questions (1-2 sentences MAX). One at a time. Tough on English.\n"
        f"{cv_injection}"
        f"OUTPUT: JSON with fields: coach_response_text, transcription_user, score_pronunciation (0-10), feedback_intonation, feedback_grammar, better_response_example, next_step_advice."
    )

SCHEMA = {"type": "OBJECT", "properties": {
    "coach_response_text": {"type": "STRING"}, "transcription_user": {"type": "STRING"},
    "score_pronunciation": {"type": "NUMBER"}, "feedback_intonation": {"type": "STRING"},
    "feedback_grammar": {"type": "STRING"}, "better_response_example": {"type": "STRING"},
    "next_step_advice": {"type": "STRING"}},
    "required": ["coach_response_text", "transcription_user", "score_pronunciation", "better_response_example"]}

# --- ROUTES FLASK ---

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
    
    cv_content = d.get('cv_content', None) 
    
    # FIX CRITIQUE: Retire les caractères NUL (0x00) pour éviter les erreurs PostgreSQL
    if cv_content:
        cv_content = cv_content.replace('\x00', '') 
    
    conn = get_db_connection()
    if conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO sessions (session_id, candidate_name, job_title, company_type, cv_content, created_at)
            VALUES (%s, %s, %s, %s, %s, NOW())
            ON CONFLICT (session_id) DO UPDATE 
            SET candidate_name = EXCLUDED.candidate_name, 
                job_title = EXCLUDED.job_title, 
                company_type = EXCLUDED.company_type,
                cv_content = EXCLUDED.cv_content
        """, (sid, d['candidate_name'], d['job_title'], d['company_type'], cv_content))
        conn.commit()
        conn.close()

    msg = f"Hi {d['candidate_name']}. I'm {COACH_NAME}. Let's start the interview for {d['job_title']}. Tell me about yourself."
    save_msg(sid, "model", msg)
    
    audio = generate_ai_voice(msg)
    
    return jsonify({"coach_response_text": msg, "audio_base64": audio, "transcription_user": "", "score_pronunciation": 10, "feedback_grammar": "", "better_response_example": "N/A"})

@app.route('/analyze', methods=['POST'])
def analyze():
    sid = request.form.get('session_id')
    f = request.files.get('audio')
    
    sess = get_sess(sid)
    if not sess: return jsonify({"error": "Session lost"}), 404

    # Utiliser le CV sauvegardé pour le prompt
    cv_for_prompt = sess['cv_content'] 

    # 1. Traitement audio (inchangé)
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

    # 2. Appel à Gemini
    try:
        # Passation du CV au prompt
        sys_prompt = get_sys_prompt(sess['candidate_name'], sess['job_title'], sess['company_type'], cv_content=cv_for_prompt)
        model = genai.GenerativeModel(MODEL_NAME, system_instruction=sys_prompt)
        
        hist = get_hist(sid)
        chat = model.start_chat(history=hist)
        
        u_file = genai.upload_file(path, mime_type=mime)
        
        retry = 0
        while u_file.state.name == "PROCESSING" and retry < 10: 
            time.sleep(0.5)
            u_file = genai.get_file(u_file.name)
            retry += 1
            
        if u_file.state.name != "ACTIVE": raise Exception("Gemini File Upload Failed")
        
        resp = chat.send_message([u_file, "Analyze."], generation_config=genai.GenerationConfig(response_mime_type="application/json", response_schema=SCHEMA))
        
        try:
            genai.delete_file(u_file.name)
            os.remove(t_webm)
            if mime == "audio/mp3": os.remove(path)
        except: pass

        res = json.loads(clean_json(resp.text))
        save_msg(sid, "user", res.get("transcription_user", "..."))
        save_msg(sid, "model", res.get("coach_response_text", ""))
        
        res["audio_base64"] = generate_ai_voice(res.get("coach_response_text"))
        
        return jsonify(res)
    except Exception as e:
        print(f"CRITICAL: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)