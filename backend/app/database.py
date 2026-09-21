"""
SQLAlchemy database instance — shared across all modules.
Import `db` from here, never create a new SQLAlchemy() instance.
"""
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate

db = SQLAlchemy()
migrate = Migrate()


def init_db(app):
    """Initialize database extensions with the Flask app."""
    db.init_app(app)
    migrate.init_app(app, db)

    with app.app_context():
        # Import all models so SQLAlchemy knows about them
        from app.models import user, conversation, workspace, usage  # noqa: F401
        db.create_all()

    return db
