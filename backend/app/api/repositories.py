"""
Workspaces — a user's repositories.

Isolation: every query is scoped to the authenticated user. When
ALLOW_ANY_WORKSPACE_PATH is false (production default), a workspace must live
inside <WORKSPACE_ROOT>/<user_id>/, so one user can never point CodeSage at
another user's files or at the server's own filesystem. Repos can be cloned
from a git URL into that directory.
"""
import logging
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone

from flask import Blueprint, current_app, g, jsonify, request

from app.agent.context import invalidate_workspace_files
from app.auth.utils import require_auth
from app.database import db
from app.models.workspace import Workspace
from app.repository import get_status, git_tools, list_files, start_indexing
from app.repository.searcher import search

logger = logging.getLogger(__name__)

ws_bp = Blueprint("workspaces", __name__, url_prefix="/api/workspaces")

_app_ref = None  # set in create_app — used by the background indexing callback

GIT_URL_RE = re.compile(r"^(https://[\w.\-]+(:\d+)?/[\w.\-~/]+?(\.git)?/?|git@[\w.\-]+:[\w.\-~/]+?(\.git)?)$")


def _on_index_complete(workspace_id: int, total_files: int, total_chunks: int):
    """Callback fired when indexing finishes — updates DB record."""
    if _app_ref is None:
        return
    try:
        with _app_ref.app_context():
            ws = db.session.get(Workspace, workspace_id)
            if ws:
                ws.index_status = "done"
                ws.index_error = None
                ws.total_files = total_files
                ws.total_chunks = total_chunks
                ws.indexed_at = datetime.now(timezone.utc)
                db.session.commit()
    except Exception:
        logger.exception("[index] failed to record completion for workspace %s", workspace_id)


def _user_root(user_id: int) -> str:
    root = os.path.join(os.path.abspath(current_app.config["WORKSPACE_ROOT"]), str(user_id))
    os.makedirs(root, exist_ok=True)
    return root


def _within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([os.path.realpath(path), os.path.realpath(root)]) == os.path.realpath(root)
    except ValueError:
        return False


def _validate_repo_path(user_id: int, repo_path: str) -> tuple[str | None, str | None]:
    """Returns (absolute_path, error)."""
    cfg = current_app.config
    user_root = _user_root(user_id)
    if not cfg.get("ALLOW_ANY_WORKSPACE_PATH"):
        # relative paths are interpreted inside the user's own root
        candidate = repo_path if os.path.isabs(repo_path) else os.path.join(user_root, repo_path)
        if not _within(candidate, user_root):
            return None, ("Workspaces must be inside your workspace folder on the server "
                          f"({user_root}). Clone a repository by URL instead.")
        repo_path = candidate
    abs_path = os.path.realpath(repo_path)
    if not os.path.isdir(abs_path):
        return None, f"Directory not found: {repo_path}"
    if abs_path in (os.path.realpath(os.sep), os.path.realpath(os.path.expanduser("~"))):
        return None, "Refusing to use a filesystem root or home directory as a workspace"
    return abs_path, None


def _get_ws(ws_id: int):
    return Workspace.query.filter_by(id=ws_id, user_id=g.current_user.id).first()


@ws_bp.route("", methods=["GET"])
@require_auth
def list_workspaces():
    """GET /api/workspaces — list user's workspaces."""
    workspaces = Workspace.query.filter_by(user_id=g.current_user.id) \
        .order_by(Workspace.updated_at.desc()).all()
    cfg = current_app.config
    return jsonify({"workspaces": [w.to_dict() for w in workspaces],
                    "policy": {"allow_any_path": bool(cfg.get("ALLOW_ANY_WORKSPACE_PATH")),
                               "allow_git_clone": bool(cfg.get("ALLOW_GIT_CLONE"))}})


@ws_bp.route("", methods=["POST"])
@require_auth
def create_workspace():
    """
    POST /api/workspaces — {name, repo_path} for an existing directory, or
    {name, git_url} to clone into the user's workspace folder.
    """
    body = request.get_json(force=True, silent=True) or {}
    user = g.current_user
    cfg = current_app.config
    git_url = (body.get("git_url") or "").strip()
    repo_path = (body.get("repo_path") or "").strip()

    if git_url:
        if not cfg.get("ALLOW_GIT_CLONE"):
            return jsonify({"error": "Cloning repositories is disabled on this server"}), 403
        if not GIT_URL_RE.match(git_url):
            return jsonify({"error": "Only https:// or git@ repository URLs are supported"}), 400
        slug = re.sub(r"[^\w.\-]", "_", git_url.rstrip("/").split("/")[-1].removesuffix(".git"))[:60] or "repo"
        target = os.path.join(_user_root(user.id), slug)
        n = 1
        while os.path.exists(target):
            n += 1
            target = os.path.join(_user_root(user.id), f"{slug}-{n}")
        try:
            proc = subprocess.run(["git", "clone", "--depth", "50", "--", git_url, target],
                                  capture_output=True, text=True,
                                  timeout=int(cfg.get("GIT_CLONE_TIMEOUT_SECONDS", 120)),
                                  env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        except subprocess.TimeoutExpired:
            shutil.rmtree(target, ignore_errors=True)
            return jsonify({"error": "git clone timed out"}), 504
        except FileNotFoundError:
            return jsonify({"error": "git is not installed on the server"}), 500
        if proc.returncode != 0:
            shutil.rmtree(target, ignore_errors=True)
            return jsonify({"error": "git clone failed: " + (proc.stderr.strip().splitlines() or ["unknown"])[-1]}), 400
        repo_path = target
    elif not repo_path:
        return jsonify({"error": "repo_path or git_url is required"}), 400

    abs_path, error = _validate_repo_path(user.id, repo_path)
    if error:
        return jsonify({"error": error}), 400
    existing = Workspace.query.filter_by(user_id=user.id, repo_path=abs_path).first()
    if existing:
        return jsonify(existing.to_dict()), 200

    name = (body.get("name") or os.path.basename(abs_path) or "Workspace").strip()[:255]
    ws = Workspace(user_id=user.id, name=name, repo_path=abs_path,
                   branch=git_tools.current_branch(abs_path) or None, index_status="indexing")
    db.session.add(ws)
    db.session.commit()
    try:
        start_indexing(ws.id, ws.repo_path, dict(cfg), on_complete=_on_index_complete)
    except Exception as e:
        ws.index_status, ws.index_error = "error", str(e)
        db.session.commit()
    return jsonify(ws.to_dict()), 201


@ws_bp.route("/<int:ws_id>", methods=["GET"])
@require_auth
def get_workspace(ws_id: int):
    ws = _get_ws(ws_id)
    if not ws:
        return jsonify({"error": "Workspace not found"}), 404
    return jsonify(ws.to_dict())


@ws_bp.route("/<int:ws_id>", methods=["DELETE"])
@require_auth
def delete_workspace(ws_id: int):
    """Removes the workspace from CodeSage. Never deletes the repository files."""
    ws = _get_ws(ws_id)
    if not ws:
        return jsonify({"error": "Workspace not found"}), 404
    from app.models.conversation import Conversation
    Conversation.query.filter_by(workspace_id=ws.id, user_id=g.current_user.id) \
        .update({"workspace_id": None})
    db.session.delete(ws)
    db.session.commit()
    return jsonify({"ok": True})


@ws_bp.route("/<int:ws_id>/index", methods=["POST"])
@require_auth
def index_workspace(ws_id: int):
    """POST /api/workspaces/:id/index — refresh (or with {"force": true} rebuild) the index."""
    ws = _get_ws(ws_id)
    if not ws:
        return jsonify({"error": "Workspace not found"}), 404
    force = bool((request.get_json(force=True, silent=True) or {}).get("force"))
    ws.index_status = "indexing"
    db.session.commit()
    invalidate_workspace_files(ws.repo_path)
    try:
        result = start_indexing(ws.id, ws.repo_path, cfg=dict(current_app.config),
                                on_complete=_on_index_complete, force=force)
    except ValueError as e:
        ws.index_status, ws.index_error = "error", str(e)
        db.session.commit()
        return jsonify({"error": str(e)}), 400
    return jsonify({"result": result, "workspace": ws.to_dict()})


@ws_bp.route("/<int:ws_id>/index/status", methods=["GET"])
@require_auth
def index_status(ws_id: int):
    ws = _get_ws(ws_id)
    if not ws:
        return jsonify({"error": "Workspace not found"}), 404
    status = get_status(ws_id)
    if status.get("status") == "idle" and ws.index_status == "done":
        status = {"status": "done", "total_files": ws.total_files, "total_chunks": ws.total_chunks}
    return jsonify(status)


@ws_bp.route("/<int:ws_id>/files", methods=["GET"])
@require_auth
def list_workspace_files(ws_id: int):
    ws = _get_ws(ws_id)
    if not ws:
        return jsonify({"error": "Workspace not found"}), 404
    return jsonify({"files": list_files(ws_id, ws.repo_path, cfg=dict(current_app.config))})


@ws_bp.route("/<int:ws_id>/search", methods=["GET"])
@require_auth
def search_workspace(ws_id: int):
    ws = _get_ws(ws_id)
    if not ws:
        return jsonify({"error": "Workspace not found"}), 404
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "q is required"}), 400
    return jsonify({"results": search(ws_id, ws.repo_path, query, top_k=8, cfg=dict(current_app.config))})


@ws_bp.route("/<int:ws_id>/git/status", methods=["GET"])
@require_auth
def git_status(ws_id: int):
    ws = _get_ws(ws_id)
    if not ws:
        return jsonify({"error": "Workspace not found"}), 404
    summary = git_tools.status_summary(ws.repo_path)
    if summary.get("is_repo") and summary.get("branch") and summary["branch"] != ws.branch:
        ws.branch = summary["branch"]
        db.session.commit()
    return jsonify(summary)


@ws_bp.route("/<int:ws_id>/git/diff", methods=["GET"])
@require_auth
def git_diff(ws_id: int):
    ws = _get_ws(ws_id)
    if not ws:
        return jsonify({"error": "Workspace not found"}), 404
    return jsonify(git_tools.git_diff(ws.repo_path, request.args.get("path")))


@ws_bp.route("/<int:ws_id>/git/log", methods=["GET"])
@require_auth
def git_log(ws_id: int):
    ws = _get_ws(ws_id)
    if not ws:
        return jsonify({"error": "Workspace not found"}), 404
    return jsonify({"commits": git_tools.log_commits(ws.repo_path, 20)})


@ws_bp.route("/<int:ws_id>/git/checkpoint", methods=["POST"])
@require_auth
def create_checkpoint(ws_id: int):
    """Snapshot current changes (non-destructive: the working tree is not modified)."""
    ws = _get_ws(ws_id)
    if not ws:
        return jsonify({"error": "Workspace not found"}), 404
    msg = (request.get_json(force=True, silent=True) or {}).get("message", "manual checkpoint")
    res = git_tools.create_checkpoint(ws.repo_path, str(msg)[:120])
    return jsonify(res), (200 if res.get("ok") else 400)


@ws_bp.route("/<int:ws_id>/git/checkpoints", methods=["GET"])
@require_auth
def list_checkpoints(ws_id: int):
    ws = _get_ws(ws_id)
    if not ws:
        return jsonify({"error": "Workspace not found"}), 404
    return jsonify({"checkpoints": git_tools.list_checkpoints(ws.repo_path)})


@ws_bp.route("/<int:ws_id>/git/restore", methods=["POST"])
@require_auth
def restore_checkpoint(ws_id: int):
    """Restore tracked files to a checkpoint; the current state is snapshotted first."""
    ws = _get_ws(ws_id)
    if not ws:
        return jsonify({"error": "Workspace not found"}), 404
    sha = (request.get_json(force=True, silent=True) or {}).get("sha")
    res = git_tools.restore_checkpoint(ws.repo_path, sha)
    invalidate_workspace_files(ws.repo_path)
    return jsonify(res), (200 if res.get("ok") else 400)
