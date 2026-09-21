from app.database import db
from datetime import datetime, timezone


class Workspace(db.Model):
    __tablename__ = "workspaces"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    name = db.Column(db.String(255), nullable=False)
    repo_path = db.Column(db.String(2000), nullable=False)
    branch = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc))

    # Index status
    index_status = db.Column(db.String(20), default="idle")  # idle, indexing, done, error
    index_error = db.Column(db.Text, nullable=True)
    indexed_at = db.Column(db.DateTime, nullable=True)
    total_files = db.Column(db.Integer, default=0)
    total_chunks = db.Column(db.Integer, default=0)

    # Relationships
    user = db.relationship("User", back_populates="workspaces")
    conversations = db.relationship("Conversation", back_populates="workspace")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "name": self.name,
            "repo_path": self.repo_path,
            "branch": self.branch,
            "index_status": self.index_status,
            "index_error": self.index_error,
            "indexed_at": self.indexed_at.isoformat() if self.indexed_at else None,
            "total_files": self.total_files,
            "total_chunks": self.total_chunks,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
