# --- AUTH ---
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'index'

oauth = OAuth(app)

# FIX CRITIQUE: Configuration Google Login (Résout l'erreur 'Invalid URL userinfo')
# On utilise userinfo_endpoint pour ne pas dépendre de api_base_url
google = oauth.register(
    name='google',
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    userinfo_endpoint='https://www.googleapis.com/oauth2/v1/userinfo', # URL COMPLÈTE AJOUTÉE ICI
    client_kwargs={'scope': 'openid email profile'},
)