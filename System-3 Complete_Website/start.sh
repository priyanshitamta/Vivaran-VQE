#!/bin/zsh
cd "$(dirname "$0")"
source ~/Downloads/Antah-Ai-Platform-main/venv/bin/activate
export ANTAHAI_SECRET="antahai-sih-2026"
export ANTAHAI_ADMIN_USER="admin"
export ANTAHAI_ADMIN_PASSWORD="Admin@2026"
export ANTAHAI_TRAINER_USER="trainer"
export ANTAHAI_TRAINER_PASSWORD="Trainer@2026"
export ANTAHAI_S1_PYTHON="$(which python)"
python run_antahai.py
