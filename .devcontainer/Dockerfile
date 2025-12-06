FROM mcr.microsoft.com/vscode/devcontainers/base:ubuntu-22.04
ARG USERNAME=vscode

# Installation des outils système vitaux (FFmpeg pour l'audio, Git, Curl)
RUN apt-get update && apt-get install -y --no-install-recommends     curl     git     ffmpeg     python3     python3-pip     python3-venv     nodejs     npm

USER $USERNAME
WORKDIR /workspace
