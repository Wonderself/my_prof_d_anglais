# --- PROMO CODE ROUTE ---
@app.route('/api/promo_code', methods=['POST'])
@login_required
def promo_code():
    d = request.json
    code = d.get('code', '').upper()
    
    if code == "ZEROMONEY":
        # Code ZEROMONEY (Accès gratuit total)
        days = 3650 # 10 ans d'accès
        new_expiry = datetime.datetime.now() + datetime.timedelta(days=days)
        
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE users SET sub_expires = %s WHERE id = %s", (new_expiry, current_user.id))
        conn.commit()
        conn.close()
        return jsonify({"status": "free_access_granted", "message": "Accès de 10 ans débloqué. Bienvenue !"}), 200
        
    elif code == "FIFTYFIFTY":
        # Code FIFTYFIFTY (50% de réduction)
        # NOTE: La modification du prix du bouton PayPal est complexe en JS.
        # Pour l'instant, on se limite à afficher le message.
        return jsonify({"status": "discount_applied", "message": "Félicitations ! Votre code 50% est activé."}), 200

    return jsonify({"status": "invalid", "message": "Code promo invalide."}), 400