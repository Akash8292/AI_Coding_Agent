"""WSGI entry point. Production: gunicorn (see gunicorn.conf.py). Development: python backend/wsgi.py"""
import os
import sys

# Add the backend directory to the Python path
sys.path.insert(0, os.path.dirname(__file__))

from app import create_app

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    # Localhost only by default: the Werkzeug debugger (DEBUG=true) executes
    # arbitrary code, so it must never be reachable from the network.
    host = os.environ.get("HOST", "127.0.0.1")
    debug = os.environ.get("DEBUG", "false").lower() in ("1", "true", "yes")
    # threaded=True: concurrent chats and streaming responses don't block each other
    app.run(host=host, port=port, debug=debug, threaded=True, use_reloader=debug)
