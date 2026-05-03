#!/bin/bash
# Bek Rate Desk — Mac/Linux launcher
# Credentials are loaded automatically from .env

cd "$(dirname "$0")"

# Activate virtual environment if it exists
source venv/bin/activate 2>/dev/null || true

# Open browser after a short delay
sleep 1 && open http://127.0.0.1:5001 &

# Start with Gunicorn (3 workers, production-grade)
# Falls back to plain Flask if Gunicorn is not installed
if command -v gunicorn &>/dev/null; then
    gunicorn -w 3 -b 0.0.0.0:5001 --timeout 120 app:app
else
    echo "Gunicorn not found, falling back to Flask dev server."
    echo "Run: pip3 install gunicorn --break-system-packages"
    python3 app.py
fi
