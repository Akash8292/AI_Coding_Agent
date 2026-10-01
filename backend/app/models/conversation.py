import uuid

from app.database import db
from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat() if dt else None


class Conversation(db.Model):
    __tablename__ = "conversations"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspaces.id"), nullable=True)
    title = db.Column(db.String(500), nullable=False, default="New Chat")
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)
    archived = db.Column(db.Boolean, default=False)

    # Active model/provider for this conversation
    provider = db.Column(db.String(50), nullable=True)
    model = db.Column(db.String(100), nullable=True)

    # Relationships
    user = db.relationship("User", back_populates="conversations")
    workspace = db.relationship("Workspace", back_populates="conversations")
    messages = db.relationship("Message", back_populates="conversation",
                               cascade="all, delete-orphan", order_by="Message.id")

    def to_dict(self, include_messages: bool = False) -> dict:
        d = {
            "id": self.id,
            "user_id": self.user_id,
            "workspace_id": self.workspace_id,
            "title": self.title,
            "provider": self.provider,
            "model": self.model,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "archived": self.archived,
            "message_count": len(self.messages),
        }
        if include_messages:
            changes = {c.message_id: c for c in
                       ProposedChange.query.filter_by(conversation_id=self.id).all() if c.message_id}
            commands = {a.message_id: a for a in
                        PendingApproval.query.filter_by(conversation_id=self.id).all() if a.message_id}
            d["messages"] = [m.to_dict(change=changes.get(m.id), command=commands.get(m.id))
                             for m in self.messages]
        return d


class Message(db.Model):
    __tablename__ = "messages"

    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey("conversations.id"),
                                nullable=False, index=True)
    role = db.Column(db.String(20), nullable=False)  # user, assistant, system, tool
    content = db.Column(db.Text, nullable=False, default="")
    created_at = db.Column(db.DateTime, default=_now)

    # complete | error | cancelled — failed/cancelled turns never lock a conversation
    status = db.Column(db.String(20), nullable=True, default="complete")

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

    # Request metadata: mode, scope, files read, stage timings, usage, error
    meta = db.Column(db.JSON, nullable=True)

    # Relationship
    conversation = db.relationship("Conversation", back_populates="messages")

    def to_dict(self, change: "ProposedChange" = None, command: "PendingApproval" = None) -> dict:
        d = {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "role": self.role,
            "content": self.content,
            "status": self.status or "complete",
            "tool_calls": self.tool_calls,
            "tool_results": self.tool_results,
            "provider": self.provider,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "file_changes": self.file_changes,
            "activity_log": self.activity_log,
            "meta": self.meta or {},
            "created_at": _iso(self.created_at),
        }
        if change is not None:
            d["change"] = change.to_dict()
        if command is not None:
            d["command"] = command.to_dict()
        return d


class ProposedChange(db.Model):
    """
    An edit proposed by the agent and awaiting (or past) user review.

    Holds the exact original and proposed contents of every file, plus the
    sha256 of each original as read from disk. Applying re-checks those
    hashes, so a file the user changed after the proposal is never overwritten.
    """
    __tablename__ = "proposed_changes"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspaces.id"), nullable=False)
    conversation_id = db.Column(db.Integer, db.ForeignKey("conversations.id"), nullable=True, index=True)
    message_id = db.Column(db.Integer, db.ForeignKey("messages.id"), nullable=True)
    request_id = db.Column(db.String(64), nullable=True)

    # pending | applied | rejected | reverted | stale | failed
    status = db.Column(db.String(20), nullable=False, default="pending")
    summary = db.Column(db.Text, nullable=True)
    plan = db.Column(db.JSON, nullable=True)
    # [{path, action, original, new, original_hash, diff, additions, deletions, warnings}]
    files = db.Column(db.JSON, nullable=False)
    git_branch = db.Column(db.String(255), nullable=True)
    checkpoint = db.Column(db.String(64), nullable=True)   # git snapshot sha taken before apply
    verification = db.Column(db.JSON, nullable=True)       # [{path, ok, message}]
    error = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=_now)
    resolved_at = db.Column(db.DateTime, nullable=True)

    def to_dict(self, include_content: bool = False) -> dict:
        files = []
        for f in self.files or []:
            item = {k: f.get(k) for k in ("path", "action", "diff", "additions", "deletions",
                                          "warnings", "user_modified")}
            if include_content:
                item["original"] = f.get("original")
                item["new"] = f.get("new")
            files.append(item)
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "conversation_id": self.conversation_id,
            "message_id": self.message_id,
            "status": self.status,
            "summary": self.summary,
            "plan": self.plan or [],
            "files": files,
            "additions": sum(f.get("additions") or 0 for f in self.files or []),
            "deletions": sum(f.get("deletions") or 0 for f in self.files or []),
            "git_branch": self.git_branch,
            "checkpoint": self.checkpoint,
            "verification": self.verification,
            "error": self.error,
            "created_at": _iso(self.created_at),
            "resolved_at": _iso(self.resolved_at),
        }


class PendingApproval(db.Model):
    """
    An action that needs explicit user approval before it runs — currently
    shell commands proposed by the agent. Stores the outcome once resolved.
    """
    __tablename__ = "pending_approvals"

    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey("conversations.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    tool_name = db.Column(db.String(100), nullable=False)
    tool_args = db.Column(db.JSON, nullable=False)
    reason = db.Column(db.Text, nullable=True)
    danger_level = db.Column(db.String(20), default="safe")  # safe, moderate, dangerous, blocked
    # pending, approved, rejected, running, completed, failed, timeout, blocked
    status = db.Column(db.String(20), default="pending")
    created_at = db.Column(db.DateTime, default=_now)
    resolved_at = db.Column(db.DateTime, nullable=True)

    workspace_id = db.Column(db.Integer, db.ForeignKey("workspaces.id"), nullable=True)
    message_id = db.Column(db.Integer, db.ForeignKey("messages.id"), nullable=True)
    result = db.Column(db.JSON, nullable=True)  # {exit_code, output, duration_ms, truncated}

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "workspace_id": self.workspace_id,
            "message_id": self.message_id,
            "tool_name": self.tool_name,
            "tool_args": self.tool_args,
            "command": (self.tool_args or {}).get("command"),
            "reason": self.reason,
            "danger_level": self.danger_level,
            "status": self.status,
            "result": self.result,
            "created_at": _iso(self.created_at),
            "resolved_at": _iso(self.resolved_at),
        }
