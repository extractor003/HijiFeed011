"""
Flask app to keep Render web service awake, and run Telegram bot in a background thread.
Gunicorn will serve this Flask app on $PORT. The bot runs alongside in a thread.
"""

import threading
import asyncio
import os
from flask import Flask, jsonify
from bot import run_bot_polling

_bot_started = False
_lock = threading.Lock()


def _start_bot_once():
    global _bot_started
    with _lock:
        if not _bot_started:
            # Run the Telegram bot inside its own asyncio event loop
            def runner():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(run_bot_polling())

            t = threading.Thread(target=runner, name="tg-bot-thread", daemon=True)
            t.start()
            _bot_started = True


def create_app():
    _start_bot_once()
    app = Flask(__name__)

    @app.get("/")
    def root():
        return "OK - Telegram Feedback Bot running", 200

    @app.get("/health")
    def health():
        return jsonify(status="ok"), 200

    return app


# ✅ Expose a global app object for Gunicorn
app = create_app()

# ✅ Support running locally without Gunicorn
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))  # Render injects PORT at runtime
    app.run(host="0.0.0.0", port=port)
