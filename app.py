import os
import json
import tempfile
import traceback
from flask import Flask, render_template, request, jsonify
from gemini_coach import GeminiCoach

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, 'templates')
STATIC_DIR = os.path.join(BASE_DIR, 'static')

app = Flask(__name__, template_folder=TEMPLATE_DIR, static_folder=STATIC_DIR)

global_coach = None

def get_coach():
    global global_coach
    if global_coach is None:
        print("⚡ (Re)Démarrage du moteur GeminiCoach...")
        try:
            global_coach = GeminiCoach()
            print("✅ Coach Mike est prêt !")
        except Exception as e:
            print(f"❌ Erreur critique init coach: {e}")
            raise e
    return global_coach

def reset_coach():
    global global_coach
    print("♻️ RESET: On tue l'instance du coach pour nettoyer la mémoire.")
    global_coach = None

@app.after_request
def add_security_headers(response):
    csp_policy = (
        "default-src * 'self' blob: data: gap:; "
        "style-src * 'self' 'unsafe-inline' blob: data: gap:; "
        "script-src * 'self' 'unsafe-eval' 'unsafe-inline' blob: data: gap:; "
        "object-src * 'self' blob: data: gap:; "
        "img-src * 'self' 'unsafe-inline' blob: data: gap:; "
        "connect-src * 'self' 'unsafe-inline' blob: data: gap: ws: wss:; "
        "media-src * 'self' blob: data: gap:; "
        "font-src * 'self' data:;"
    )
    response.headers['Content-Security-Policy'] = csp_policy
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response

@app.route('/')
def home():
    try:
        return render_template('index.html')
    except Exception as e:
        return f"<h1>Erreur Template</h1><p>{e}</p>", 500

@app.route('/health')
def health_check():
    # Utilisé par le script warm-up du HTML pour réveiller le serveur
    return "OK", 200

@app.route('/analyze', methods=['POST'])
def analyze():
    temp_path = None
    try:
        coach = get_coach()
        if 'audio' not in request.files:
            return jsonify({"error": "No audio file"}), 400
        audio_file = request.files['audio']
        with tempfile.NamedTemporaryFile(delete=False, suffix='.webm') as temp:
            audio_file.save(temp.name)
            temp_path = temp.name
            
        print(f"📞 Audio reçu : {temp_path}")
        result_json_str = coach.analyze_audio_interview(temp_path)
        
        try:
            return jsonify(json.loads(result_json_str)), 200
        except json.JSONDecodeError:
            reset_coach() 
            return jsonify({
                "score": 0, 
                "transcription": "Error parsing response", 
                "coach_response": "I'm having a little glitch. Try again, I'm reset now.",
                "next_step": "Retry"
            }), 200

    except Exception as e:
        reset_coach()
        print(f"❌ ERREUR 500 : {traceback.format_exc()}")
        return jsonify({"error": str(e), "details": "Instance Reset"}), 500
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
                mp3_path = temp_path + ".mp3"
                if os.path.exists(mp3_path):
                    os.remove(mp3_path)
            except:
                pass

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)