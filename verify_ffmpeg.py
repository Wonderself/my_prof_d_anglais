import shutil
import subprocess
import os
import sys

def check_ffmpeg():
    print("--- DIAGNOSTIC FFMPEG ---")
    
    # 1. Recherche dans le PATH système
    # shutil.which permet de voir si la commande est accessible globalement
    ffmpeg_path = shutil.which("ffmpeg")
    
    if ffmpeg_path:
        print(f"✅ FFmpeg trouvé à l'emplacement : {ffmpeg_path}")
        
        # 2. Vérification de la version pour s'assurer que l'exécutable fonctionne
        try:
            result = subprocess.run([ffmpeg_path, "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            head_line = result.stdout.split('\n')[0]
            print(f"✅ Version détectée : {head_line}")
            print("\nCONCLUSION : Tout est parfait. Ton serveur peut désormais convertir l'audio.")
            return True
        except Exception as e:
            print(f"⚠️ FFmpeg est détecté mais ne répond pas correctement : {e}")
            return False
    else:
        print("❌ FFmpeg n'est PAS trouvé dans le PATH système.")
        print("\nANALYSE DU PROBLÈME :")
        print("1. Soit FFmpeg n'est pas installé.")
        print("2. Soit il est installé, mais Windows ne sait pas où il est (Variable d'environnement 'Path' non configurée).")
        print("ACTION : Tu dois suivre le guide d'installation manuelle.")
        return False

if __name__ == "__main__":
    success = check_ffmpeg()
    if not success:
        print("\n👉 Si ce script échoue, l'audio du coach ne fonctionnera pas correctement.")