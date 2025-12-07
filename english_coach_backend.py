import os, sys, tempfile, json, datetime, time, shutil, base64, asyncio
from dotenv import load_dotenv
from flask import Flask, request, jsonify
from flask_cors import CORS
from pydub import AudioSegment
import google.generativeai as genai
import psycopg2
from psycopg2.extras import RealDictCursor
import edge_tts

# --- CONFIGURATION INITIALE ---
load_dotenv(override=True)

# Définition des variables clés
API_KEY = os.getenv("GOOGLE_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL") 
MODEL_NAME = 'gemini-2.5-flash' # Modèle le plus rapide pour la production

if not API_KEY: sys.exit("❌ CLÉ GEMINI MANQUANTE. Définissez GOOGLE_API_KEY.")
if not DATABASE_URL: sys.exit("❌ DATABASE_URL MANQUANTE. Définissez la chaîne de connexion Neon.")

try:
    genai.configure(api_key=API_KEY.strip())
except Exception as e: sys.exit(f"❌ ERREUR CONFIG GEMINI: {e}")

# Initialisation de l'application Flask
app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app) # Active CORS pour toutes les origines

# --- GESTION DE LA BASE DE DONNÉES (POSTGRES) ---
def get_db_connection():
    """Crée et retourne une connexion à PostgreSQL."""
    try:
        # RealDictCursor permet d'accéder aux colonnes par nom, comme les Row de SQLite
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        return conn
    except Exception as e:
        print(f"⚠️ Erreur DB (Connexion): {e}")
        return None

def init_db():
    """Initialise les tables si elles n'existent pas."""
    conn = get_db_connection()
    if not conn: 
        print("❌ Impossible d'initialiser la DB, connexion échouée.")
        return
    try:
        cur = conn.cursor()
        # Table des sessions
        cur.execute('''
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                candidate_name TEXT,
                job_title TEXT,
                company_type TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')
        # Table de l'historique des messages
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
        cur.close()
        conn.close()
        print("✅ DB Initialisée (Postgres)")
    except Exception as e:
        print(f"❌ Erreur Init DB: {e}")

# Exécuter l'initialisation au démarrage du module
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
        cur.close()
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
        cur.close()
        conn.close()
        # Format Gemini : list of dicts with 'role' and 'parts'
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
        cur.close()
        conn.close()
        return row
    except: return None

# --- GÉNÉRATION VOCALE (EDGE TTS) ---
async def _gen_audio(text, path):
    """Fonction asynchrone pour générer la voix via edge-tts."""
    # Voix masculine professionnelle
    await edge_tts.Communicate(text, "en-US-ChristopherNeural").save(path)

def generate_ai_voice(text):
    """Génère un fichier MP3, l'encode en Base64 et le supprime."""
    try:
        fd, out = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        try:
            # Gère l'exécution asynchrone dans un contexte synchrone (Gunicorn)
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        
        loop.run_until_complete(_gen_audio(text, out))
        
        with open(out, "rb") as f: 
            b64 = base64.b64encode(f.read()).decode('utf-8')
        
        os.remove(out)
        return b64
    except Exception as e:
        # CORRECTION BUG SILENCE : Retourne une chaîne vide en cas d'erreur TTS
        print(f"❌ CRITIQUE: Échec de la génération Edge TTS: {e}")
        return "" 

# --- LOGIQUE GEMINI ---
def clean_json(text):
    """Nettoie le bloc de code Markdown autour du JSON de Gemini."""
    t = text.strip()
    if t.startswith("`" * 3):
        lines = t.split('\n')
        if len(lines) > 2: return "\n".join(lines[1:-1]).strip()
    return t

def get_sys_prompt(name, job, company):
    """Crée l'instruction système pour Gemini."""
    return (f"ROLE: Coach Mike, recruiter at {company}. Interviewing {name} for {job}.\n"
            f"STYLE: Short questions (1-2 sentences MAX). One at a time. Tough on English.\n"
            f"OUTPUT: JSON with fields: coach_response_text, transcription_user, score_pronunciation (0-10), feedback_intonation, feedback_grammar, better_response_example, next_step_advice.")

# Schéma de sortie pour garantir la structure JSON
SCHEMA = {"type": "OBJECT", "properties": {
    "coach_response_text": {"type": "STRING"}, "transcription_user": {"type": "STRING"},
    "score_pronunciation": {"type": "NUMBER"}, "feedback_intonation": {"type": "STRING"},
    "feedback_grammar": {"type": "STRING"}, "better_response_example": {"type": "STRING"},
    "next_step_advice": {"type": "STRING"}},
    "required": ["coach_response_text", "transcription_user", "score_pronunciation", "better_response_example"]}

# --- ROUTES FLASK ---

@app.route('/')
def index():
    """Sert le fichier HTML principal."""
    return app.send_static_file('index.html')

@app.route('/health', methods=['GET'])
def health():
    """Endpoint de santé utilisé par Render et le Keep-Alive Cron Job."""
    conn = get_db_connection()
    db_status = "ok"
    if conn:
        conn.close()
    else:
        db_status = "disconnected"
    return jsonify({"status": "ok", "db": db_status})

@app.route('/start_chat', methods=['POST'])
def start_chat():
    """Démarre une nouvelle session et retourne la première question."""
    d = request.json
    sid = d.get('session_id')
    
    # Enregistre ou met à jour les détails de la session
    conn = get_db_connection()
    if conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO sessions (session_id, candidate_name, job_title, company_type, created_at)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (session_id) DO UPDATE 
            SET candidate_name = EXCLUDED.candidate_name, 
                job_title = EXCLUDED.job_title, 
                company_type = EXCLUDED.company_type
        """, (sid, d['candidate_name'], d['job_title'], d['company_type']))
        conn.commit()
        cur.close()
        conn.close()

    msg = f"Hi {d['candidate_name']}. I'm Mike. Let's start the interview for {d['job_title']}. Tell me about yourself."
    save_msg(sid, "model", msg)
    
    audio_data = generate_ai_voice(msg)
    
    return jsonify({"coach_response_text": msg, "audio_base64": audio_data, "transcription_user": "", "score_pronunciation": 10, "feedback_grammar": "", "better_response_example": "N/A"})

@app.route('/analyze', methods=['POST'])
def analyze():
    """Reçoit l'audio de l'utilisateur, l'envoie à Gemini et génère la réponse."""
    sid = request.form.get('session_id')
    f = request.files.get('audio')
    
    sess = get_sess(sid)
    if not sess: return jsonify({"error": "Session introuvable"}), 404

    # 1. Traitement audio (WebM -> MP3)
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
    except: return jsonify({"error": "Erreur fichier audio"}), 500

    # 2. Appel à Gemini
    try:
        # Utilisation de la variable MODEL_NAME pour la cohérence
        model = genai.GenerativeModel(MODEL_NAME, system_instruction=get_sys_prompt(sess['candidate_name'], sess['job_title'], sess['company_type']))
        
        hist = get_hist(sid)
        chat = model.start_chat(history=hist)
        
        u_file = genai.upload_file(path, mime_type=mime)
        
        # Attendre la fin du traitement du fichier
        retry = 0
        while u_file.state.name == "PROCESSING" and retry < 10: 
            time.sleep(0.5)
            u_file = genai.get_file(u_file.name)
            retry += 1
            
        if u_file.state.name != "ACTIVE": raise Exception("Gemini File Upload Failed")
        
        resp = chat.send_message([u_file, "Analyze audio."], generation_config=genai.GenerationConfig(response_mime_type="application/json", response_schema=SCHEMA))
        
        # Nettoyage des fichiers temporaires
        try:
            genai.delete_file(u_file.name)
            os.remove(t_webm)
            if mime == "audio/mp3": os.remove(path)
        except: pass

        # 3. Traitement de la réponse
        res = json.loads(clean_json(resp.text))
        
        save_msg(sid, "user", res.get("transcription_user", "..."))
        save_msg(sid, "model", res.get("coach_response_text", ""))
        
        # 4. Génération vocale
        audio_data = generate_ai_voice(res.get("coach_response_text"))
        res["audio_base64"] = audio_data
        
        return jsonify(res)
        
    except Exception as e:
        print(f"❌ CRITICAL ERROR: {str(e)}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    # Mode dev uniquement
    app.run(host='0.0.0.0', port=5000, debug=True)