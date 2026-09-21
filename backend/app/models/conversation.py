from app.database import db
from datetime import datetime, timezone


class Conversation(db.Model):
    __tablename__ = "conversations"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspaces.id"), nullable=True)
    title = db.Column(db.String(500), nullable=False, default="New Chat")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc))
    archived = db.Column(db.Boolean, default=False)

    # Active model/provider for this conversation
    provider = db.Column(db.String(50), nullable=True)
    model = db.Column(db.String(100), nullable=True)

    # Relationships
    user = db.relationship("User", back_populates="conversations")
    workspace = db.relationship("Workspace", back_populates="conversations")
    messages = db.relationship("Message", back_populates="conversation",
                               cascade="all, delete-orphan", order_by="Message.created_at")

    def to_dict(self, include_messages: bool = False) -> dict:
        d = {
            "id": self.id,
            "user_id": self.user_id,
            "workspace_id": self.workspace_id,
            "title": self.title,
            "provider": self.provider,
            "model": self.model,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "archived": self.archived,
            "message_count": len(self.messages),
        }
        if include_messages:
            d["messages"] = [m.to_dict() for m in self.messages]
        return d


class Message(db.Model):
    __tablename__ = "messages"

    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey("conversations.id"),
                                nullable=False, index=True)
    role = db.Column(db.String(20), nullable=False)  # user, assistant, system, tool
    content = db.Column(db.Text, nullable=False, default="")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    # Tool calls and results (stored as JSON)
    tool_calls = db.Column(db.JSON, nullable=True)
    tool_results = db.Column(db.JSON, nullable=True)

    # Metadata
    provider = db.Column(db.String(50), nullable=True)
    model = db.Column(db.String(100), nullable=True)
    input_tokens = db.Column(db.Integer, default=0)
    output_tokens = db.Column(db.Integer, default=0)

    # File changes attached to this message
    file_changes = db.Column(db.JSON, nullable=True)  # list of {path, diff, status}

    # Agent activity log
    activity_log = db.Column(db.JSON, nullable=True)  # list of activity items

    # Relationship
    conversation = db.relationship("Conversation", back_populates="messages")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "role": self.role,
            "content": self.content,
            "tool_calls": self.tool_calls,
            "tool_results": self.tool_results,
            "provider": self.provider,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "file_changes": self.file_changes,
            "activity_log": self.activity_log,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class PendingApproval(db.Model):
    """Stores tool executions waiting for user approval."""
    __tablename__ = "pending_approvals"

    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey("conversations.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    tool_name = db.Column(db.String(100), nullable=False)
    tool_args = db.Column(db.JSON, nullable=False)
    reason = db.Column(db.Text, nullable=True)
    danger_level = db.Column(db.String(20), default="safe")  # safe, moderate, dangerous
    status = db.Column(db.String(20), default="pending")  # pending, approved, rejected
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    resolved_at = db.Column(db.DateTime, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "tool_name": self.tool_name,
            "tool_args": self.tool_args,
            "reason": self.reason,
            "danger_level": self.danger_level,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
