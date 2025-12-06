# server.py
import os
import threading
import time
from flask import Flask
import requests

app = Flask(__name__)

@app.get("/")
def home():
    return "Bot server running!", 200


def keep_pinging():
    """Prevents Render from idling by pinging itself every 2 minutes."""
    url = os.getenv("RENDER_EXTERNAL_URL")
    if not url:
        print("⚠️ No RENDER_EXTERNAL_URL found. Self-ping disabled.")
        return

    print("🔄 Self-ping thread started.")

    while True:
        try:
            requests.get(url, timeout=10)
            print("✅ Self-ping OK:", url)
        except Exception as e:
            print("❌ Self-ping error:", e)

        time.sleep(120)   # ping every 2 minutes


def run():
    """Starts Flask server AND starts the keep-alive pinger."""
    port = int(os.getenv("PORT", 10000))

    # Start self-ping in another thread
    threading.Thread(target=keep_pinging, daemon=True).start()

    print(f"🚀 Starting Flask keep-alive server on port {port}")
    app.run(host="0.0.0.0", port=port)
