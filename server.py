from flask import Flask
app = Flask(__name__)

@app.get("/")
def home():
    return "Bot is running!"

def run():
    from waitress import serve
    import os
    port = int(os.environ.get("PORT", 10000))
    serve(app, host="0.0.0.0", port=port)
