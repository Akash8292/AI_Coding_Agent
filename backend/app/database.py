"""
SQLAlchemy database instance — shared across all modules.
Import `db` from here, never create a new SQLAlchemy() instance.
"""
import logging

from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from sqlalchemy import inspect, text

logger = logging.getLogger(__name__)

db = SQLAlchemy()
migrate = Migrate()

# Columns added after the first release. db.create_all() creates missing
# tables but never alters existing ones, so existing databases get these
# via an idempotent ALTER TABLE on startup (works on SQLite and PostgreSQL).
ADDED_COLUMNS = {
    "messages": [("status", "VARCHAR(20)"), ("meta", "JSON")],
    "usage_records": [("request_id", "VARCHAR(64)"), ("duration_ms", "INTEGER"),
                      ("status", "VARCHAR(20)"), ("estimated", "BOOLEAN")],
    "pending_approvals": [("workspace_id", "INTEGER"), ("message_id", "INTEGER"),
                          ("result", "JSON")],
}


def ensure_columns() -> list[str]:
    """Add any missing columns from ADDED_COLUMNS. Returns what was added."""
    added = []
    insp = inspect(db.engine)
    tables = set(insp.get_table_names())
    for table, cols in ADDED_COLUMNS.items():
        if table not in tables:
            continue
        existing = {c["name"] for c in insp.get_columns(table)}
        for name, ddl in cols:
            if name in existing:
                continue
            col_type = "TEXT" if (ddl == "JSON" and db.engine.dialect.name == "sqlite") else ddl
            db.session.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {col_type}'))
            added.append(f"{table}.{name}")
    if added:
        db.session.commit()
        logger.info("[db] added columns: %s", ", ".join(added))
    return added


def init_db(app):
    """Initialize database extensions with the Flask app."""
    db.init_app(app)
    migrate.init_app(app, db)

    with app.app_context():
        # Import all models so SQLAlchemy knows about them
        from app.models import user, conversation, workspace, usage  # noqa: F401
        db.create_all()
        ensure_columns()

        # Enable WAL mode for SQLite — allows concurrent reads/writes
        # which is essential when running Flask with threaded=True.
        if "sqlite" in app.config.get("SQLALCHEMY_DATABASE_URI", ""):
            try:
                db.session.execute(db.text("PRAGMA journal_mode=WAL"))
                db.session.execute(db.text("PRAGMA busy_timeout=5000"))
                db.session.commit()
            except Exception:
                pass

    return db
