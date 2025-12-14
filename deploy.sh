#!/bin/bash
cd /home/botuser/what-to-cook-bot
git pull
source venv/bin/activate
pip install -r requirements.txt --quiet
sudo systemctl restart what-to-cook-bot
echo "✅ Бот обновлён и перезапущен!"