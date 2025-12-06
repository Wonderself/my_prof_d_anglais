#!/bin/bash
set -e

echo "🔵 [1/3] CRÉATION DU DEVCONTAINER (Pour VS Code & GitHub Codespaces)..."

mkdir -p .devcontainer

# 1. Le Dockerfile de développement (L'image système)
cat > .devcontainer/Dockerfile <<EOF
FROM mcr.microsoft.com/vscode/devcontainers/base:ubuntu-22.04
ARG USERNAME=vscode

# Installation des outils système vitaux (FFmpeg pour l'audio, Git, Curl)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    ffmpeg \
    python3 \
    python3-pip \
    python3-venv \
    nodejs \
    npm

USER \$USERNAME
WORKDIR /workspace
EOF

# 2. La configuration VS Code (Extensions & Ports)
cat > .devcontainer/devcontainer.json <<EOF
{
  "name": "Gemini Coach Dev",
  "build": { "dockerfile": "Dockerfile" },
  "features": {
    "ghcr.io/devcontainers/features/docker-in-docker:2": {}
  },
  "customizations": {
    "vscode": {
      "extensions": [
        "ms-python.python",
        "ms-python.pylance",
        "esbenp.prettier-vscode",
        "googlecloudtools.cloudcode"
      ]
    }
  },
  "forwardPorts": [8080],
  "postCreateCommand": "bash scripts/bootstrap-dev.sh",
  "remoteUser": "vscode"
}
EOF

echo "✅ Devcontainer configuré."

echo "🔵 [2/3] CONFIGURATION PROJECT IDX (Pour le Cloud Google)..."

mkdir -p .idx

# 3. La config IDX (Similaire à VS Code mais pour le Cloud)
cat > .idx/dev.nix <<EOF
{ pkgs, ... }: {
  channel = "stable-23.11";
  packages = [
    pkgs.python311
    pkgs.python311Packages.pip
    pkgs.nodejs_20
    pkgs.ffmpeg
    pkgs.gnumake
  ];
  idx = {
    extensions = [
      "ms-python.python"
      "googlecloudtools.cloudcode"
    ];
    workspace = {
      onCreate = {
        setup-env = "python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt";
      };
    };
    previews = {
      enable = true;
      previews = {
        web = {
          command = ["./scripts/run_preview.sh"];
          manager = "web";
        };
      };
    };
  };
}
EOF

# Script de lancement pour la preview IDX
cat > scripts/run_preview.sh <<EOF
#!/bin/bash
source venv/bin/activate
export PORT=\$PORT
python3 app.py
EOF
chmod +x scripts/run_preview.sh

echo "✅ Project IDX configuré."

echo "🔵 [3/3] CRÉATION DU BOOTSTRAP (Installation auto des libs)..."

# 4. Le script qui installe tout quand on ouvre le projet
cat > scripts/bootstrap-dev.sh <<EOF
#!/usr/bin/env bash
set -e
echo "🔧 Installation des dépendances..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi
source venv/bin/activate
pip install --upgrade pip
if [ -f requirements.txt ]; then 
    pip install -r requirements.txt
else
    echo "⚠️ Pas de requirements.txt trouvé !"
fi
echo "✅ Environnement prêt."
EOF
chmod +x scripts/bootstrap-dev.sh

echo "🟢 PHASE 2 TERMINÉE : Environnement standardisé."