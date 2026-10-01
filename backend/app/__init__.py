"""
Flask application factory.
Import and call create_app() to get the configured Flask app.
"""
import logging
import os

from flask import Flask, send_from_directory
from flask_cors import CORS

from app.config import get_config
from app.database import init_db

# The SPA loads scripts/styles from these CDNs only; everything else is same-origin.
CSP = ("default-src 'self'; "
       "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
       "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdnjs.cloudflare.com; "
       "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


def create_app(config=None) -> Flask:
    app = Flask(__name__, static_folder=None)

    cfg = config or get_config()
    app.config.from_object(cfg)

    if not logging.getLogger().handlers:
        logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                            format="%(asctime)s %(levelname)s %(name)s %(message)s")

    # CORS — the frontend is served by this app (same origin), so cross-origin
    # access is opt-in via CORS_ORIGINS. Auth uses bearer tokens, not cookies.
    origins = [o for o in (app.config.get("CORS_ORIGINS") or []) if o]
    if origins:
        CORS(app, resources={r"/api/*": {"origins": origins}, r"/auth/*": {"origins": origins}},
             supports_credentials=False)

    init_db(app)

    from app.api import repositories as repo_api
    repo_api._app_ref = app

    from app.auth import auth_bp
    from app.api.health import health_bp
    from app.api.chat import chat_bp
    from app.api.conversations import conv_bp
    from app.api.repositories import ws_bp
    from app.api.usage import usage_bp
    from app.api.changes import changes_bp

    for bp in (auth_bp, health_bp, chat_bp, conv_bp, ws_bp, usage_bp, changes_bp):
        app.register_blueprint(bp)

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        if resp.mimetype == "text/html":
            resp.headers.setdefault("Content-Security-Policy", CSP)
        return resp

    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    frontend_dir = os.path.join(os.path.dirname(backend_dir), "frontend")

    # Serve the frontend SPA for all non-API routes
    @app.route("/", defaults={"path": ""})
    @app.route("/<path:path>")
    def serve_frontend(path):
        if path.startswith(("api/", "auth/")):
            return {"error": "Not found"}, 404
        if path and os.path.isfile(os.path.join(frontend_dir, path)):
            return send_from_directory(frontend_dir, path)
        return send_from_directory(frontend_dir, "index.html")

    return app
