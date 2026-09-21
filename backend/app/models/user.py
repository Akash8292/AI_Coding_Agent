from app.database import db
from datetime import datetime, timezone
import bcrypt


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    name = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc))

    # User preferences stored as JSON
    settings = db.Column(db.JSON, default=dict)

    # Relationships
    conversations = db.relationship("Conversation", back_populates="user", cascade="all, delete-orphan")
    workspaces = db.relationship("Workspace", back_populates="user", cascade="all, delete-orphan")
    usage_records = db.relationship("UsageRecord", back_populates="user", cascade="all, delete-orphan")

    def set_password(self, password: str):
        self.password_hash = bcrypt.hashpw(
            password.encode("utf-8"), bcrypt.gensalt()
        ).decode("utf-8")

    def check_password(self, password: str) -> bool:
        try:
            return bcrypt.checkpw(
                password.encode("utf-8"),
                self.password_hash.encode("utf-8")
            )
        except Exception:
            return False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "email": self.email,
            "name": self.name,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "settings": self.settings or {},
        }
