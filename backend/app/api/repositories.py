import os
import threading
from flask import Blueprint, request, jsonify, g, current_app
from app.auth.utils import require_auth
from app.database import db
from app.models.workspace import Workspace
from app.repository import start_indexing, get_status, list_files
from app.repository import git_tools
from app.repository.searcher import search

ws_bp = Blueprint("workspaces", __name__, url_prefix="/api/workspaces")


def _on_index_complete(workspace_id: int, total_files: int, total_chunks: int):
    """Callback fired when indexing finishes — updates DB record."""
    from app.database import db
    from app.models.workspace import Workspace
    from datetime import datetime, timezone
    # Need app context since this runs in a background thread
    try:
        from app import create_app
        # Import here to avoid circular imports; use stored app
        with _app_ref.app_context():
            ws = db.session.get(Workspace, workspace_id)
            if ws:
                ws.index_status = "done"
                ws.total_files = total_files
                ws.total_chunks = total_chunks
                from datetime import datetime, timezone
                ws.indexed_at = datetime.now(timezone.utc)
                db.session.commit()
    except Exception:
        pass


_app_ref = None  # set in create_app


@ws_bp.route("", methods=["GET"])
@require_auth
def list_workspaces():
    """GET /api/workspaces — list user's workspaces."""
    user = g.current_user
    workspaces = Workspace.query.filter_by(user_id=user.id).order_by(
        Workspace.updated_at.desc()
    ).all()
    return jsonify({"workspaces": [w.to_dict() for w in workspaces]})


@ws_bp.route("", methods=["POST"])
@require_auth
def create_workspace():
    """POST /api/workspaces — create a workspace."""
    body = request.get_json(force=True, silent=True) or {}
    user = g.current_user

    repo_path = (body.get("repo_path") or "").strip()
    name = (body.get("name") or os.path.basename(repo_path) or "Workspace").strip()

    if not repo_path:
        return jsonify({"error": "repo_path is required"}), 400
    if not os.path.isdir(repo_path):
        return jsonify({"error": f"Directory not found: {repo_path}"}), 400

    ws = Workspace(
        user_id=user.id,
        name=name,
        repo_path=os.path.abspath(repo_path),
        branch=git_tools.git_current_branch(repo_path),
    )
    db.session.add(ws)
    db.session.commit()
    return jsonify(ws.to_dict()), 201


@ws_bp.route("/<int:ws_id>", methods=["GET"])
@require_auth
def get_workspace(ws_id: int):
    user = g.current_user
    ws = Workspace.query.filter_by(id=ws_id, user_id=user.id).first()
    if not ws:
        return jsonify({"error": "Workspace not found"}), 404
    return jsonify(ws.to_dict())


@ws_bp.route("/<int:ws_id>", methods=["DELETE"])
@require_auth
def delete_workspace(ws_id: int):
    user = g.current_user
    ws = Workspace.query.filter_by(id=ws_id, user_id=user.id).first()
    if not ws:
        return jsonify({"error": "Workspace not found"}), 404
    db.session.delete(ws)
    db.session.commit()
    return jsonify({"ok": True})


@ws_bp.route("/<int:ws_id>/index", methods=["POST"])
@require_auth
def index_workspace(ws_id: int):
    """POST /api/workspaces/:id/index — start/restart indexing."""
    user = g.current_user
    ws = Workspace.query.filter_by(id=ws_id, user_id=user.id).first()
    if not ws:
        return jsonify({"error": "Workspace not found"}), 404

    cfg = current_app.config
    ws.index_status = "indexing"
    db.session.commit()

    try:
        result = start_indexing(
            ws.id, ws.repo_path, cfg=dict(cfg),
            on_complete=_on_index_complete,
        )
    except ValueError as e:
        ws.index_status = "error"
        ws.index_error = str(e)
        db.session.commit()
        return jsonify({"error": str(e)}), 400

    return jsonify({"result": result, "workspace": ws.to_dict()})


@ws_bp.route("/<int:ws_id>/index/status", methods=["GET"])
@require_auth
def index_status(ws_id: int):
    """GET /api/workspaces/:id/index/status — poll indexing progress."""
    user = g.current_user
    ws = Workspace.query.filter_by(id=ws_id, user_id=user.id).first()
    if not ws:
        return jsonify({"error": "Workspace not found"}), 404
    status = get_status(ws_id)
    return jsonify(status)


@ws_bp.route("/<int:ws_id>/files", methods=["GET"])
@require_auth
def list_workspace_files(ws_id: int):
    """GET /api/workspaces/:id/files — get file tree."""
    user = g.current_user
    ws = Workspace.query.filter_by(id=ws_id, user_id=user.id).first()
    if not ws:
        return jsonify({"error": "Workspace not found"}), 404
    cfg = current_app.config
    files = list_files(ws_id, ws.repo_path, cfg=dict(cfg))
    return jsonify({"files": files})


@ws_bp.route("/<int:ws_id>/search", methods=["GET"])
@require_auth
def search_workspace(ws_id: int):
    """GET /api/workspaces/:id/search?q=... — search code."""
    user = g.current_user
    ws = Workspace.query.filter_by(id=ws_id, user_id=user.id).first()
    if not ws:
        return jsonify({"error": "Workspace not found"}), 404
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "q is required"}), 400
    cfg = current_app.config
    results = search(ws_id, ws.repo_path, query, top_k=8, cfg=dict(cfg))
    return jsonify({"results": results})


@ws_bp.route("/<int:ws_id>/git/status", methods=["GET"])
@require_auth
def git_status(ws_id: int):
    user = g.current_user
    ws = Workspace.query.filter_by(id=ws_id, user_id=user.id).first()
    if not ws:
        return jsonify({"error": "Workspace not found"}), 404
    result = git_tools.git_status(ws.repo_path)
    return jsonify(result)


@ws_bp.route("/<int:ws_id>/git/diff", methods=["GET"])
@require_auth
def git_diff(ws_id: int):
    user = g.current_user
    ws = Workspace.query.filter_by(id=ws_id, user_id=user.id).first()
    if not ws:
        return jsonify({"error": "Workspace not found"}), 404
    path = request.args.get("path")
    result = git_tools.git_diff(ws.repo_path, path)
    return jsonify(result)


@ws_bp.route("/<int:ws_id>/git/log", methods=["GET"])
@require_auth
def git_log(ws_id: int):
    user = g.current_user
    ws = Workspace.query.filter_by(id=ws_id, user_id=user.id).first()
    if not ws:
        return jsonify({"error": "Workspace not found"}), 404
    result = git_tools.git_log(ws.repo_path)
    return jsonify(result)


@ws_bp.route("/<int:ws_id>/git/checkpoint", methods=["POST"])
@require_auth
def create_checkpoint(ws_id: int):
    """Create a git stash checkpoint before AI changes."""
    user = g.current_user
    ws = Workspace.query.filter_by(id=ws_id, user_id=user.id).first()
    if not ws:
        return jsonify({"error": "Workspace not found"}), 404
    body = request.get_json(force=True, silent=True) or {}
    msg = body.get("message", "AI checkpoint")
    result = git_tools.create_checkpoint(ws.repo_path, msg)
    return jsonify(result)


@ws_bp.route("/<int:ws_id>/git/restore", methods=["POST"])
@require_auth
def restore_checkpoint(ws_id: int):
    """Restore most recent git stash checkpoint."""
    user = g.current_user
    ws = Workspace.query.filter_by(id=ws_id, user_id=user.id).first()
    if not ws:
        return jsonify({"error": "Workspace not found"}), 404
    result = git_tools.restore_checkpoint(ws.repo_path)
    return jsonify(result)
