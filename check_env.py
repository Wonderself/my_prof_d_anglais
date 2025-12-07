import os
import sys

# Charger le module 'dotenv' pour la lecture du fichier .env
from dotenv import load_dotenv

print("--- 1. VÉRIFICATION DES BIBLIOTHÈQUES ---")
try:
    # Tenter l'importation pour s'assurer que pip install a fonctionné
    import google.generativeai as genai
    print("✅ Succès : Les bibliothèques sont bien installées.")
except ImportError as e:
    print(f"❌ ÉCHEC : Une bibliothèque manque à l'appel. Erreur : {e}")
    print("Conseil : Exécute 'pip install -r requirements.txt'")
    sys.exit(1)

# --- FIN DU BLOC D'IMPORTATION ---

# On tente de charger les variables d'environnement
print("\n--- 2. VÉRIFICATION DE LA SÉCURITÉ (.ENV) ---")
# load_dotenv() va chercher le fichier nommé EXACTEMENT .env dans le répertoire courant
loaded = load_dotenv()

if not loaded:
    print("⚠️  ATTENTION : Le fichier .env n'a pas été trouvé ou est vide.")
    print("ACTION REQUISE : Crée le fichier '.env' (sans extension) à côté de ce script.")
else:
    print("✅ Succès : Fichier .env détecté.")

# On vérifie la présence spécifique de la clé
api_key = os.getenv("GOOGLE_API_KEY")

if not api_key:
    print("❌ ÉCHEC : La variable 'GOOGLE_API_KEY' est introuvable.")
    print("ACTION REQUISE : Vérifie que le fichier .env contient la ligne exacte : GOOGLE_API_KEY=TaCleIci")
elif api_key.startswith("AIza"):
    # On ne doit jamais afficher la clé complète pour des raisons de sécurité, même en local
    print("✅ Succès : La clé API 'GOOGLE_API_KEY' a été lue avec succès.")
    
    print("\n==================================")
    print("🚀 DIAGNOSTIC FINAL : TOUT EST VERT.")
    print("Ton environnement est prêt à se connecter à l'API Gemini.")
    print("==================================")
else:
    print("⚠️  ATTENTION : Une clé a été trouvée, mais elle ne commence pas par 'AIza'.")
    print("Vérifie que tu as copié la clé complète et non pas un autre secret.")