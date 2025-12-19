# 📋 INFINITY COACH 6.0 - Setup Checklist & Questions

## 🖼️ Images requises (à placer dans `/static/`)

| Fichier | Dimensions | Description |
|---------|------------|-------------|
| `fav.png` | 512x512 | Favicon / App icon (icône infini sur fond violet/indigo) |
| `og-image.jpg` | 1200x630 | Image Open Graph pour partage réseaux sociaux |
| `cover_job.jpg` | 800x500 | Couverture coach Emma (interview) |
| `cover_dating.jpg` | 800x500 | Couverture coach Hitch (dating) |
| `cover_sales.jpg` | 800x500 | Couverture coach Sarah (sales) |
| `cover_lang.jpg` | 800x500 | Couverture coach Poly (langues) |
| `cover_life.jpg` | 800x500 | Couverture coach Serena (life coach) |
| `cover_debate.jpg` | 800x500 | Couverture coach Marcia (debate) |
| `cover_child.jpg` | 800x500 | Couverture coach Luna (enfants) |
| `cover_study.jpg` | 800x500 | Couverture coach Athena (tutor) |
| `cover_history.jpg` | 800x500 | Couverture coach Cleo (histoire) |
| `cover_faith.jpg` | 800x500 | Couverture coach Faith (religion) |
| `cover_default.jpg` | 800x500 | Image par défaut si manquante |

### Vidéos avatars (optionnel mais recommandé)
| Fichier | Format | Description |
|---------|--------|-------------|
| `emma_idle.mp4` | 150x150, loop | Avatar Emma au repos |
| `emma_talk.mp4` | 150x150, loop | Avatar Emma qui parle |
| *(idem pour chaque coach)* | | |

---

## ❓ Questions pour finaliser le site

### 1. Informations légales
- [ ] **Numéro d'entreprise israélienne** (ח.פ) à ajouter dans Legal Mentions ?
- [ ] **Adresse physique** de l'entreprise à afficher ?
- [ ] **TVA** : Êtes-vous assujetti à la TVA ? Si oui, numéro à afficher ?

### 2. Réseaux sociaux
Confirmer les URLs exactes :
- [ ] Facebook : `https://facebook.com/infinitycoach` ✓ ou autre ?
- [ ] Instagram : `https://instagram.com/infinitycoach` ✓ ou autre ?
- [ ] LinkedIn : `https://linkedin.com/company/infinitycoach` ✓ ou autre ?
- [ ] Twitter/X : Ajouter aussi ?
- [ ] TikTok : Ajouter aussi ?

### 3. Emails
Confirmer les adresses :
- [ ] Support : `support@infinitycoach.ai` ✓ ?
- [ ] Privacy : `privacy@infinitycoach.ai` ✓ ?
- [ ] Legal : `legal@infinitycoach.ai` ✓ ?
- [ ] Contact général : `contact@infinitycoach.ai` ✓ ?

### 4. PayPal
- [ ] **Client ID PayPal production** (actuellement en sandbox `sb`)
- [ ] Compte PayPal Business configuré ?

### 5. Domaine
- [ ] Nom de domaine final : `infinitycoach.ai` ? Autre ?
- [ ] SSL configuré ?

### 6. Design
- [ ] Couleur principale OK ? (Indigo/violet `#6366f1`)
- [ ] Police OK ? (Inter)
- [ ] Style des images coaches : photo réaliste ? illustration ? avatar stylisé ?

### 7. Contenu
- [ ] Descriptions des coaches OK ou à personnaliser ?
- [ ] Texte de la Privacy Policy OK ou modifications ?
- [ ] Texte des Terms OK ou modifications ?
- [ ] Prix OK ? (€7/mois, €49/an)

### 8. Fonctionnalités
- [ ] Analytics à intégrer ? (Google Analytics, Mixpanel, etc.)
- [ ] Chat support en direct ? (Intercom, Crisp, etc.)
- [ ] Newsletter signup ?
- [ ] Referral program ?

---

## 🚀 Prochaines étapes

1. **Créer les images** selon le tableau ci-dessus
2. **Répondre aux questions** pour finaliser le contenu légal
3. **Configurer PayPal** en mode production
4. **Déployer sur Render** avec le domaine final
5. **Configurer Google OAuth** avec le domaine final
6. **Tester le paiement** en production
7. **Lancer !** 🎉

---

## 📁 Structure finale du projet

```
infinity-coach/
├── app.py                 # Backend Flask
├── requirements.txt       # Dépendances Python
├── Dockerfile            # Config Docker
├── .env                  # Variables d'environnement (NE PAS COMMIT)
├── static/
│   ├── index.html        # Frontend SPA
│   ├── fav.png           # Favicon
│   ├── og-image.jpg      # Open Graph
│   ├── cover_*.jpg       # Images coaches (10)
│   ├── cover_default.jpg # Image par défaut
│   └── *_idle.mp4        # Vidéos avatars (optionnel)
└── README.md             # Documentation
```

---

## 🔧 Commandes utiles

```bash
# Développement local
docker build --no-cache -t gemini-teacher .
docker run -p 10000:10000 --env-file .env gemini-teacher

# Voir les logs
docker logs -f $(docker ps -lq)

# Arrêter
docker stop $(docker ps -q)
```