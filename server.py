import os
from flask import Flask


app = Flask(__name__)

@app.get("/")
def home():
    return "Bot is running!", 200

def run():
    # Render dynamically assigns the port
    port = int(os.getenv("PORT", 5000))
    print(f"🌐 Starting keep-alive server on port {port}...")
    app.run(host="0.0.0.0", port=port)
  
