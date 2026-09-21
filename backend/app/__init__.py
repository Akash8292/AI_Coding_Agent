"""
Flask application factory.
Import and call create_app() to get the configured Flask app.
"""
from flask import Flask
from flask_cors import CORS

from app.config import get_config
from app.database import init_db


def create_app(config=None) -> Flask:
    app = Flask(__name__, static_folder="../../frontend", static_url_path="")

    # Load config
    cfg = config or get_config()
    app.config.from_object(cfg)

    # CORS
    CORS(app, resources={r"/api/*": {"origins": cfg.CORS_ORIGINS},
                          r"/auth/*": {"origins": cfg.CORS_ORIGINS}},
         supports_credentials=True)

    # Database
    init_db(app)

    # Store app reference for background thread callbacks
    from app.api import repositories as repo_api
    repo_api._app_ref = app

    # Register blueprints
    from app.auth import auth_bp
    from app.api.health import health_bp
    from app.api.chat import chat_bp
    from app.api.conversations import conv_bp
    from app.api.repositories import ws_bp
    from app.api.usage import usage_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(health_bp)
    app.register_blueprint(chat_bp)
    app.register_blueprint(conv_bp)
    app.register_blueprint(ws_bp)
    app.register_blueprint(usage_bp)

    # Serve frontend SPA for all non-API routes
    @app.route("/", defaults={"path": ""})
    @app.route("/<path:path>")
    def serve_frontend(path):
        import os
        from flask import send_from_directory
        backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        root_dir = os.path.dirname(backend_dir)
        frontend_dir = os.path.join(root_dir, "frontend")
        if not os.path.isdir(frontend_dir):
            frontend_dir = os.path.join(backend_dir, "frontend")

        if path and os.path.exists(os.path.join(frontend_dir, path)):
            return send_from_directory(frontend_dir, path)
        return send_from_directory(frontend_dir, "index.html")

    return app
