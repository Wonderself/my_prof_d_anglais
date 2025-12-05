import os
import json
import time
import google.generativeai as genai
from dotenv import load_dotenv
from pydub import AudioSegment

load_dotenv()

class GeminiCoach:
    def __init__(self):
        self.api_key = os.getenv("GOOGLE_API_KEY")
        if not self.api_key:
            print("CRITICAL ERROR: API Key missing.")
            self.model = None
            return
        
        genai.configure(api_key=self.api_key)
        self.model_name = 'models/gemini-2.5-flash' # Priorité au modèle rapide
        self.model = genai.GenerativeModel(self.model_name)
        
        # Gestion de la mémoire locale (Attention: Volatile sur Cloud Run)
        self.history_file = "history.json"
        self.conversation_history = self._load_history()

    def _load_history(self):
        """Charge l'historique depuis le JSON local."""
        if os.path.exists(self.history_file):
            try:
                with open(self.history_file, 'r') as f:
                    return json.load(f)
            except:
                return []
        return []

    def _save_history(self):
        """Sauvegarde l'historique dans le JSON local."""
        try:
            # On ne garde que les 8 derniers échanges pour ne pas saturer le prompt
            short_history = self.conversation_history[-8:]
            with open(self.history_file, 'w') as f:
                json.dump(short_history, f)
        except Exception as e:
            print(f"Erreur sauvegarde historique: {e}")

    def analyze_audio_interview(self, audio_path):
        if not self.model:
            return json.dumps({"error": "AI Coach not initialized."})

        # 1. Conversion MP3
        clean_audio_path = audio_path + ".mp3"
        try:
            print(f"Converting {audio_path} -> MP3...")
            audio = AudioSegment.from_file(audio_path)
            # Standardisation Mono 24k pour la voix
            audio = audio.set_channels(1).set_frame_rate(24000)
            audio.export(clean_audio_path, format="mp3", bitrate="64k")
            file_to_upload = clean_audio_path
            mime_type = "audio/mp3"
        except Exception as e:
            print(f"⚠️ FFmpeg Error: {e}. Using raw file.")
            file_to_upload = audio_path
            mime_type = "audio/webm"

        # 2. Upload Google
        try:
            audio_file = genai.upload_file(path=file_to_upload, mime_type=mime_type)
        except Exception as e:
            return json.dumps({"error": f"Upload Failed: {str(e)}"})

        # 3. Polling
        try:
            while audio_file.state.name == "PROCESSING":
                time.sleep(1)
                audio_file = genai.get_file(audio_file.name)
            if audio_file.state.name != "ACTIVE":
                raise Exception("Audio file rejected by Google AI.")
        except Exception as e:
            return json.dumps({"error": f"Processing Error: {str(e)}"})

        # 4. Construction du contexte mémoire
        context_str = ""
        if self.conversation_history:
            context_str = "PREVIOUS CONVERSATION CONTEXT:\n"
            for exchange in self.conversation_history[-3:]: # On injecte les 3 derniers
                context_str += f"- User: {exchange['user']}\n- Coach: {exchange['coach']}\n"

        # 5. Prompt "Coach Mike" (American Style)
        prompt = f"""
        ROLE: You are 'Coach Mike', a top-tier American Accent & Communication Coach based in California.
        TONE: Energetic, positive, direct, and encouraging (Apple/Silicon Valley style).
        LANGUAGE: SPEAK ONLY IN ENGLISH (American Standard), YOU ALWAYS BEGIN BY REPEATING THE AUDIO WITH THE GOOD ACCENT.
        
        {context_str}

        MISSION:
        1. Listen to the audio.
        2. Transcribe it perfectly.
        3. Analyze the pronunciation in details (focus on American 'R', 'T', and vowel sounds).
        4. Check grammar and vocabulary.
        
        STRICT JSON RESPONSE FORMAT (No markdown):
        {{
            "score": [Integer 0-100],
            "transcription": "[Exact transcription]",
            "pronunciation_feedback": "[Specific feedback on sounds, e.g., 'You rolled your R too much']",
            "coach_response": "[Your conversational response. Be natural, use idioms. If there is context, refer to it.]",
            "next_step": "[A specific sentence to repeat or a question to answer]"
        }}
        """

        try:
            response = self.model.generate_content([prompt, audio_file])
            json_str = response.text.replace("```json", "").replace("```", "").strip()
            
            # Sauvegarde en mémoire après succès
            try:
                data = json.loads(json_str)
                self.conversation_history.append({
                    "user": data.get("transcription", ""),
                    "coach": data.get("coach_response", "")
                })
                self._save_history()
            except:
                pass # Si le JSON est mal formé, on ne sauvegarde pas

            return json_str
            
        except Exception as e:
            print(f"❌ AI Error: {e}")
            return json.dumps({
                "score": 0, 
                "transcription": "Error", 
                "pronunciation_feedback": "System Error.", 
                "coach_response": "I couldn't hear you clearly. Could you try again?", 
                "next_step": "Try again."
            })