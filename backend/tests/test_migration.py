"""
Existing databases (created before messages.status/meta etc. existed) are
upgraded in place on startup without losing data.
"""
import sqlite3

from app import create_app
from app.database import db


def test_old_schema_is_upgraded_in_place(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR(255) NOT NULL UNIQUE,
            password_hash VARCHAR(255) NOT NULL, name VARCHAR(255), created_at DATETIME,
            updated_at DATETIME, settings JSON);
        CREATE TABLE conversations (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, workspace_id INTEGER,
            title VARCHAR(500) NOT NULL, created_at DATETIME, updated_at DATETIME, archived BOOLEAN,
            provider VARCHAR(50), model VARCHAR(100));
        CREATE TABLE messages (id INTEGER PRIMARY KEY, conversation_id INTEGER NOT NULL, role VARCHAR(20) NOT NULL,
            content TEXT NOT NULL, created_at DATETIME, tool_calls JSON, tool_results JSON, provider VARCHAR(50),
            model VARCHAR(100), input_tokens INTEGER, output_tokens INTEGER, file_changes JSON, activity_log JSON);
        CREATE TABLE usage_records (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, conversation_id INTEGER,
            provider VARCHAR(50) NOT NULL, model VARCHAR(100) NOT NULL, input_tokens INTEGER, output_tokens INTEGER,
            cost_usd FLOAT, created_at DATETIME);
        CREATE TABLE pending_approvals (id INTEGER PRIMARY KEY, conversation_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            tool_name VARCHAR(100) NOT NULL, tool_args JSON NOT NULL, reason TEXT, danger_level VARCHAR(20),
            status VARCHAR(20), created_at DATETIME, resolved_at DATETIME);
        INSERT INTO users (id, email, password_hash) VALUES (1, 'old@example.com', 'x');
        INSERT INTO conversations (id, user_id, title) VALUES (1, 1, 'Old chat');
        INSERT INTO messages (id, conversation_id, role, content) VALUES (1, 1, 'user', 'hello from before');
    """)
    con.commit()
    con.close()

    class Cfg:
        TESTING = True
        SECRET_KEY = "k"
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{path}"
        SQLALCHEMY_TRACK_MODIFICATIONS = False
        CORS_ORIGINS = []

    app = create_app(Cfg)
    with app.app_context():
        from app.models import Conversation, ProposedChange
        conv = db.session.get(Conversation, 1)
        data = conv.to_dict(include_messages=True)
        assert data["messages"][0]["content"] == "hello from before"
        assert data["messages"][0]["status"] == "complete"      # NULL status reads as complete
        assert ProposedChange.query.count() == 0                  # new table created
        db.session.remove()
        db.engine.dispose()

    cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(messages)")}
    assert {"status", "meta"} <= cols
    cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(pending_approvals)")}
    assert {"workspace_id", "message_id", "result"} <= cols
