from flask import Blueprint, jsonify, current_app
from app.llm.factory import ProviderFactory

health_bp = Blueprint("health", __name__)


@health_bp.route("/health")
def health():
    """Health check endpoint."""
    cfg = current_app.config

    # Check database
    db_ok = True
    try:
        from app.database import db
        db.session.execute(db.text("SELECT 1"))
    except Exception:
        db_ok = False

    # Check at least one provider is available
    providers_info = ProviderFactory.get_all_info(cfg, live_models=False)
    available_providers = [k for k, v in providers_info.items() if v.get("available")]

    return jsonify({
        "status": "healthy" if db_ok else "degraded",
        "version": cfg.get("APP_VERSION", "1.0.0"),
        "database": "connected" if db_ok else "error",
        "providers_available": available_providers,
        "default_provider": cfg.get("DEFAULT_PROVIDER", "openai"),
        "ollama": providers_info.get("ollama", {}).get("status"),
    }), (200 if db_ok else 503)


@health_bp.route("/api/models")
def list_models():
    """Return all provider/model info for the frontend model selector."""
    cfg = current_app.config
    return jsonify(ProviderFactory.get_all_info(cfg))
