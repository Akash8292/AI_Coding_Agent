"""
Review workflow for agent proposals.

  GET  /api/changes/<id>            proposal with diffs (?content=1 adds full file texts)
  POST /api/changes/<id>/apply      write the reviewed change (stale-safe, all-or-nothing)
  POST /api/changes/<id>/reject     discard — the workspace is never touched
  POST /api/changes/<id>/revert     undo an applied change (only if files are untouched since)
  GET  /api/conversations/<id>/changes

  POST /api/commands/<id>/approve   run an approved command (sandboxed, time-limited)
  POST /api/commands/<id>/reject
"""
import logging
from datetime import datetime, timezone

from flask import Blueprint, current_app, g, jsonify, request

from app.agent import executor
from app.agent.context import invalidate_workspace_files
from app.agent.permissions import assess_command, run_command
from app.auth.utils import require_auth
from app.database import db
from app.models.conversation import PendingApproval, ProposedChange
from app.models.workspace import Workspace
from app.repository import workspace_fs

logger = logging.getLogger(__name__)

changes_bp = Blueprint("changes", __name__, url_prefix="/api")


def _load_change(change_id: str):
    change = ProposedChange.query.filter_by(id=change_id, user_id=g.current_user.id).first()
    if not change:
        return None, None, (jsonify({"error": "Change not found"}), 404)
    ws = Workspace.query.filter_by(id=change.workspace_id, user_id=g.current_user.id).first()
    if not ws:
        return change, None, (jsonify({"error": "Workspace for this change no longer exists"}), 404)
    return change, ws, None


@changes_bp.route("/changes/<change_id>", methods=["GET"])
@require_auth
def get_change(change_id):
    change, _, err = _load_change(change_id)
    if err:
        return err
    return jsonify(change.to_dict(include_content=request.args.get("content") == "1"))


@changes_bp.route("/conversations/<int:conv_id>/changes", methods=["GET"])
@require_auth
def list_changes(conv_id):
    items = ProposedChange.query.filter_by(conversation_id=conv_id, user_id=g.current_user.id) \
        .order_by(ProposedChange.created_at.desc()).all()
    return jsonify({"changes": [c.to_dict() for c in items]})


@changes_bp.route("/changes/<change_id>/apply", methods=["POST"])
@require_auth
def apply_change(change_id):
    change, ws, err = _load_change(change_id)
    if err:
        return err
    if change.status != "pending":
        return jsonify({"error": f"This change is {change.status}; only pending changes can be applied.",
                        "change": change.to_dict()}), 409
    try:
        result = executor.apply_change_set(ws.repo_path, change.files, checkpoint=True,
                                           label=f"before change {change.id[:8]}")
    except executor.StaleChangeError as e:
        change.status = "stale"
        change.error = str(e)
        change.resolved_at = datetime.now(timezone.utc)
        db.session.commit()
        logger.info("[changes] %s stale: %s", change.id, e.paths)
        return jsonify({"error": f"Not applied: {', '.join(e.paths)} changed after this diff was created. "
                                 "Nothing was written. Ask again to get a fresh diff.",
                        "kind": "stale", "change": change.to_dict()}), 409
    except (OSError, workspace_fs.WorkspacePathError) as e:
        change.status = "failed"
        change.error = str(e)
        db.session.commit()
        logger.exception("[changes] %s apply failed", change.id)
        return jsonify({"error": f"Writing files failed and was rolled back: {e}", "kind": "write_failed",
                        "change": change.to_dict()}), 500

    change.status = "applied"
    change.checkpoint = result.get("checkpoint")
    change.verification = result["verification"]
    change.resolved_at = datetime.now(timezone.utc)
    change.error = None
    db.session.commit()
    invalidate_workspace_files(ws.repo_path)
    logger.info("[changes] %s applied to %s", change.id, result["written"])
    return jsonify({"ok": True, "change": change.to_dict()})


@changes_bp.route("/changes/<change_id>/reject", methods=["POST"])
@require_auth
def reject_change(change_id):
    change, _, err = _load_change(change_id)
    if err:
        return err
    if change.status != "pending":
        return jsonify({"error": f"This change is already {change.status}.", "change": change.to_dict()}), 409
    change.status = "rejected"
    change.resolved_at = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify({"ok": True, "change": change.to_dict()})


@changes_bp.route("/changes/<change_id>/revert", methods=["POST"])
@require_auth
def revert_change(change_id):
    change, ws, err = _load_change(change_id)
    if err:
        return err
    if change.status != "applied":
        return jsonify({"error": "Only applied changes can be reverted.", "change": change.to_dict()}), 409
    try:
        executor.revert_change_set(ws.repo_path, change.files)
    except executor.StaleChangeError as e:
        return jsonify({"error": f"Not reverted: {', '.join(e.paths)} was edited after the change was "
                                 "applied, so reverting could lose your work.", "kind": "stale",
                        "change": change.to_dict()}), 409
    except (OSError, workspace_fs.WorkspacePathError) as e:
        return jsonify({"error": f"Revert failed and was rolled back: {e}"}), 500
    change.status = "reverted"
    change.resolved_at = datetime.now(timezone.utc)
    db.session.commit()
    invalidate_workspace_files(ws.repo_path)
    return jsonify({"ok": True, "change": change.to_dict()})


# ── Commands ────────────────────────────────────────────────────────────────

def _load_command(cmd_id: int):
    appr = PendingApproval.query.filter_by(id=cmd_id, user_id=g.current_user.id).first()
    if not appr:
        return None, None, (jsonify({"error": "Command not found"}), 404)
    ws = Workspace.query.filter_by(id=appr.workspace_id, user_id=g.current_user.id).first() \
        if appr.workspace_id else None
    if not ws:
        return appr, None, (jsonify({"error": "Workspace for this command no longer exists"}), 404)
    return appr, ws, None


@changes_bp.route("/commands/<int:cmd_id>/approve", methods=["POST"])
@require_auth
def approve_command(cmd_id):
    appr, ws, err = _load_command(cmd_id)
    if err:
        return err
    if appr.status != "pending":
        return jsonify({"error": f"This command is already {appr.status}.", "command": appr.to_dict()}), 409
    command = (appr.tool_args or {}).get("command", "")
    level, why = assess_command(command)  # re-check server-side; never trust stored level alone
    if level == "blocked":
        appr.status = "blocked"
        appr.result = {"error": f"Blocked: {why}"}
        db.session.commit()
        return jsonify({"error": f"Blocked by safety policy: {why}", "command": appr.to_dict()}), 403
    cfg = current_app.config
    appr.status = "running"
    db.session.commit()
    result = run_command(ws.repo_path, command,
                         timeout=int(cfg.get("COMMAND_TIMEOUT_SECONDS", 60)),
                         max_output=int(cfg.get("COMMAND_MAX_OUTPUT_BYTES", 50_000)))
    appr.status = "completed" if result["ok"] else ("timeout" if result["timed_out"] else "failed")
    appr.result = result
    appr.resolved_at = datetime.now(timezone.utc)
    db.session.commit()
    invalidate_workspace_files(ws.repo_path)
    logger.info("[commands] %s ran %r exit=%s", appr.id, command, result.get("exit_code"))
    return jsonify({"ok": result["ok"], "command": appr.to_dict()})


@changes_bp.route("/commands/<int:cmd_id>/reject", methods=["POST"])
@require_auth
def reject_command(cmd_id):
    appr, _, err = _load_command(cmd_id)
    if err:
        return err
    if appr.status != "pending":
        return jsonify({"error": f"This command is already {appr.status}."}), 409
    appr.status = "rejected"
    appr.resolved_at = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify({"ok": True, "command": appr.to_dict()})
