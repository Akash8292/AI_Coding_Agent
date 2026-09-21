from flask import Blueprint, request, jsonify, g
from app.auth.utils import require_auth
from app.database import db
from app.models.conversation import Conversation, Message

conv_bp = Blueprint("conversations", __name__, url_prefix="/api/conversations")


@conv_bp.route("", methods=["GET"])
@require_auth
def list_conversations():
    """GET /api/conversations — list user's conversations."""
    user = g.current_user
    archived = request.args.get("archived", "false").lower() == "true"
    search = request.args.get("q", "").strip()

    query = Conversation.query.filter_by(user_id=user.id, archived=archived)
    if search:
        query = query.filter(Conversation.title.ilike(f"%{search}%"))
    query = query.order_by(Conversation.updated_at.desc()).limit(100)

    return jsonify({"conversations": [c.to_dict() for c in query.all()]})


@conv_bp.route("", methods=["POST"])
@require_auth
def create_conversation():
    """POST /api/conversations — create a new conversation."""
    body = request.get_json(force=True, silent=True) or {}
    user = g.current_user

    conv = Conversation(
        user_id=user.id,
        title=body.get("title", "New Chat"),
        workspace_id=body.get("workspace_id"),
        provider=body.get("provider"),
        model=body.get("model"),
    )
    db.session.add(conv)
    db.session.commit()
    return jsonify(conv.to_dict()), 201


@conv_bp.route("/<int:conv_id>", methods=["GET"])
@require_auth
def get_conversation(conv_id: int):
    """GET /api/conversations/:id — get conversation with messages."""
    user = g.current_user
    conv = Conversation.query.filter_by(id=conv_id, user_id=user.id).first()
    if not conv:
        return jsonify({"error": "Conversation not found"}), 404
    return jsonify(conv.to_dict(include_messages=True))


@conv_bp.route("/<int:conv_id>", methods=["PUT"])
@require_auth
def update_conversation(conv_id: int):
    """PUT /api/conversations/:id — rename, archive, change model."""
    body = request.get_json(force=True, silent=True) or {}
    user = g.current_user
    conv = Conversation.query.filter_by(id=conv_id, user_id=user.id).first()
    if not conv:
        return jsonify({"error": "Conversation not found"}), 404

    if "title" in body:
        conv.title = (body["title"] or "").strip() or "Untitled"
    if "archived" in body:
        conv.archived = bool(body["archived"])
    if "provider" in body:
        conv.provider = body["provider"]
    if "model" in body:
        conv.model = body["model"]

    db.session.commit()
    return jsonify(conv.to_dict())


@conv_bp.route("/<int:conv_id>", methods=["DELETE"])
@require_auth
def delete_conversation(conv_id: int):
    """DELETE /api/conversations/:id — delete conversation and its messages."""
    user = g.current_user
    conv = Conversation.query.filter_by(id=conv_id, user_id=user.id).first()
    if not conv:
        return jsonify({"error": "Conversation not found"}), 404
    db.session.delete(conv)
    db.session.commit()
    return jsonify({"ok": True})
