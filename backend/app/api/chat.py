"""
Chat API — POST /api/chat streams an agent run as Server-Sent Events.

Event types (JSON in `data:` lines; `: ping` comments are heartbeats):
  start      {request_id, conversation_id}
  plan       {mode, scope, explicit_files, reasons}
  activity   {items: [{id, text, status, kind, ms, detail}]}
  chunk      {content}                        answer text
  progress   {chars}                          edit generation progress
  proposal   {change}                         reviewed-diff proposal (nothing written yet)
  command    {command}                        command awaiting permission
  done       {conversation_id, message_id, metrics}
  cancelled  {message_id}
  error      {message, kind, retryable, message_id}

Cancellation: POST /api/chat/cancel {request_id} (owner only) sets the run's
cancel event; the provider request is aborted and the stream ends with
`cancelled`. A client disconnect cancels the run the same way.
"""
import json
import logging
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Iterator

from flask import Blueprint, Response, current_app, g, jsonify, request, stream_with_context
from sqlalchemy import func

from app.agent.runner import AgentCancelled, AgentError, AgentRun, WorkspaceRef
from app.auth.utils import require_auth
from app.database import db
from app.llm.base import ProviderNotAvailableError
from app.llm.factory import ProviderFactory, record_provider_result
from app.models.conversation import Conversation, Message, PendingApproval, ProposedChange
from app.models.usage import UsageRecord
from app.models.workspace import Workspace

logger = logging.getLogger(__name__)

chat_bp = Blueprint("chat", __name__, url_prefix="/api")


# ── Run registry (cancellation + per-user concurrency) ───────────────────────

class _Run:
    __slots__ = ("user_id", "event", "started")

    def __init__(self, user_id: int):
        self.user_id = user_id
        self.event = threading.Event()
        self.started = time.time()


_runs: dict[str, _Run] = {}
_runs_lock = threading.Lock()


def _register_request(req_id: str, user_id: int = 0) -> threading.Event:
    run = _Run(user_id)
    with _runs_lock:
        _runs[req_id] = run
    return run.event


def _unregister_request(req_id: str) -> None:
    with _runs_lock:
        _runs.pop(req_id, None)


def _is_cancelled(ev: threading.Event) -> bool:
    return ev.is_set()


def _active_runs(user_id: int) -> int:
    with _runs_lock:
        return sum(1 for r in _runs.values() if r.user_id == user_id)


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, default=str)}\n\n"


# ── Cancel ───────────────────────────────────────────────────────────────────

@chat_bp.route("/chat/cancel", methods=["POST"])
@require_auth
def cancel_chat():
    """POST /api/chat/cancel — cancel a running request (only its owner may)."""
    body = request.get_json(force=True, silent=True) or {}
    req_id = (body.get("request_id") or "").strip()
    if not req_id:
        return jsonify({"ok": False, "error": "request_id is required"}), 400
    with _runs_lock:
        run = _runs.get(req_id)
    if run is None or (run.user_id and run.user_id != g.current_user.id):
        return jsonify({"ok": False, "message": "Request not found or already completed"}), 404
    run.event.set()
    logger.info("[cancel] request_id=%s cancelled by user=%s", req_id, g.current_user.id)
    return jsonify({"ok": True, "message": "Cancellation requested"})


# ── Limits ───────────────────────────────────────────────────────────────────

def _check_limits(user_id: int, cfg) -> tuple[str, int] | None:
    now = datetime.now(timezone.utc)
    max_concurrent = int(cfg.get("MAX_CONCURRENT_REQUESTS_PER_USER", 2))
    if _active_runs(user_id) >= max_concurrent:
        return (f"You already have {max_concurrent} request(s) running. Wait for one to finish "
                "or stop it first.", 429)
    per_min = int(cfg.get("RATE_LIMIT_REQUESTS_PER_MINUTE", 30))
    recent = UsageRecord.query.filter(UsageRecord.user_id == user_id,
                                      UsageRecord.created_at >= now - timedelta(minutes=1)).count()
    if recent >= per_min:
        return (f"Rate limit: {per_min} requests per minute. Try again in a moment.", 429)
    per_day = int(cfg.get("RATE_LIMIT_TOKENS_PER_DAY", 2_000_000))
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    used = db.session.query(func.coalesce(func.sum(UsageRecord.input_tokens + UsageRecord.output_tokens), 0)) \
        .filter(UsageRecord.user_id == user_id, UsageRecord.created_at >= start_of_day).scalar() or 0
    if used >= per_day:
        return (f"Daily token limit reached ({used:,} of {per_day:,}). It resets at 00:00 UTC.", 429)
    return None


def _completed_turns(messages: list) -> list[dict]:
    """
    Conversation history for the model: only user turns whose reply completed.
    A question that was cancelled or failed was abandoned by the user, so it
    must not leak into (and get merged with) the next question.
    """
    out: list[dict] = []
    for i, m in enumerate(messages):
        if m.role not in ("user", "assistant") or not m.content:
            continue
        if m.role == "assistant":
            if (m.status or "complete") == "complete":
                out.append({"role": "assistant", "content": m.content})
            continue
        nxt = messages[i + 1] if i + 1 < len(messages) else None
        if nxt is not None and nxt.role == "assistant" and (nxt.status or "complete") != "complete":
            continue
        out.append({"role": "user", "content": m.content})
    return out


# ── Chat ─────────────────────────────────────────────────────────────────────

@chat_bp.route("/chat", methods=["POST"])
@require_auth
def chat():
    """
    Body: {conversation_id, message, provider, model, use_repo, workspace_id,
           mode ("quick"|"deep"), request_id, retry}
    """
    body = request.get_json(force=True, silent=True) or {}
    user = g.current_user
    cfg = current_app.config

    message_text = (body.get("message") or "").strip()
    if not message_text:
        return jsonify({"error": "message is required"}), 400
    if len(message_text) > 100_000:
        return jsonify({"error": "message is too long (100,000 characters max)"}), 400

    req_id = (body.get("request_id") or request.headers.get("X-Request-ID") or str(uuid.uuid4()))[:64]
    with _runs_lock:
        if req_id in _runs:
            return jsonify({"error": "request_id already in use"}), 409

    limit = _check_limits(user.id, cfg)
    if limit:
        return jsonify({"error": limit[0], "kind": "rate_limited"}), limit[1]

    provider_name = (body.get("provider") or cfg.get("DEFAULT_PROVIDER", "gemini")).lower()
    model = body.get("model") or None
    use_repo = bool(body.get("use_repo", True))
    depth = "deep" if body.get("mode") == "deep" else "quick"
    retry = bool(body.get("retry"))

    try:
        provider = ProviderFactory.get(provider_name, model)
    except ProviderNotAvailableError as e:
        return jsonify({"error": str(e), "kind": "provider_unavailable"}), 400

    # Conversation
    conv_id = body.get("conversation_id")
    if conv_id:
        conv = Conversation.query.filter_by(id=conv_id, user_id=user.id).first()
        if not conv:
            return jsonify({"error": "Conversation not found"}), 404
    else:
        conv = Conversation(user_id=user.id, title=message_text[:80], provider=provider_name, model=model)
        db.session.add(conv)
        db.session.flush()

    # Workspace (must belong to this user)
    workspace = None
    ws_id = body.get("workspace_id") or conv.workspace_id
    if ws_id:
        workspace = Workspace.query.filter_by(id=ws_id, user_id=user.id).first()
        if not workspace:
            db.session.rollback()
            return jsonify({"error": "Workspace not found"}), 404
        if not conv.workspace_id:
            conv.workspace_id = workspace.id
    conv.provider, conv.model = provider_name, provider.model

    # History before this turn; on retry, drop the failed reply and reuse the user turn
    prior = list(conv.messages)
    if retry and prior and prior[-1].role == "assistant" and (prior[-1].status or "complete") != "complete":
        db.session.delete(prior[-1])
        prior = prior[:-1]
    if retry and prior and prior[-1].role == "user" and prior[-1].content == message_text:
        prior = prior[:-1]
    else:
        db.session.add(Message(conversation_id=conv.id, role="user", content=message_text))
    history = _completed_turns(prior)
    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.error("[chat] DB commit failed: %s", exc)
        return jsonify({"error": "Database error"}), 500

    ws_ref = None
    if workspace and use_repo:
        ws_ref = WorkspaceRef(workspace.id, workspace.name, workspace.repo_path, workspace.branch or "")
    elif workspace and not use_repo:
        ws_ref = None

    cancel_ev = _register_request(req_id, user.id)
    agent = AgentRun(message=message_text, history=history, provider=provider, workspace=ws_ref,
                     cfg=dict(cfg), cancel_event=cancel_ev, request_id=req_id, depth=depth)
    conv_id, user_id, ws_db_id = conv.id, user.id, workspace.id if workspace else None
    logger.info("[chat] req=%s user=%s conv=%s provider=%s model=%s ws=%s", req_id, user_id, conv_id,
                provider_name, provider.model, ws_db_id)

    def generate() -> Iterator[str]:
        started = time.monotonic()
        text_parts: list[str] = []
        outcome = "error"
        err: AgentError | None = None
        try:
            yield _sse({"type": "start", "request_id": req_id, "conversation_id": conv_id,
                        "provider": provider_name, "model": provider.model})
            for ev in agent.run():
                t = ev.get("type")
                if t == "heartbeat":
                    yield ": ping\n\n"
                    continue
                if t == "chunk":
                    text_parts.append(ev["content"])
                ev["request_id"] = req_id
                yield _sse(ev)
            outcome = "complete"
            record_provider_result(provider_name, None, provider.model)
        except AgentCancelled:
            outcome = "cancelled"
        except AgentError as e:
            err = e
            if e.kind.startswith("provider_"):
                from app.llm.base import ProviderError
                record_provider_result(provider_name, ProviderError(provider_name, e.kind[9:], e.message),
                                       provider.model)
            logger.warning("[chat] req=%s failed kind=%s: %s", req_id, e.kind, e.message)
        except GeneratorExit:
            cancel_ev.set()  # client went away: stop the provider request
            outcome = "cancelled"
            raise
        except Exception as e:  # never leave the client hanging
            logger.exception("[chat] req=%s unexpected error", req_id)
            err = AgentError("internal", f"Unexpected server error: {type(e).__name__}: {e}")
        finally:
            _unregister_request(req_id)
            final = _persist(outcome, err, agent, text_parts, started)
            logger.info("[chat] req=%s outcome=%s total_ms=%s timings=%s", req_id, outcome,
                        final.get("metrics", {}).get("total_ms"), agent.result.timings)

        if outcome == "complete":
            if final.get("change"):
                yield _sse({"type": "proposal", "change": final["change"], "request_id": req_id})
            if final.get("command"):
                yield _sse({"type": "command", "command": final["command"], "request_id": req_id})
            yield _sse({"type": "done", "conversation_id": conv_id, "message_id": final.get("message_id"),
                        "metrics": final.get("metrics"), "request_id": req_id})
        elif outcome == "cancelled":
            yield _sse({"type": "cancelled", "message_id": final.get("message_id"), "request_id": req_id})
        else:
            yield _sse({"type": "error", "message": err.message if err else "Request failed",
                        "kind": err.kind if err else "internal",
                        "retryable": err.retryable if err else True,
                        "message_id": final.get("message_id"), "request_id": req_id})

    def _persist(outcome: str, err, agent: AgentRun, text_parts: list[str], started: float) -> dict:
        """Save the assistant turn, proposal/command, and usage. Never raises."""
        res = agent.result
        total_ms = int((time.monotonic() - started) * 1000)
        metrics = {
            "total_ms": total_ms, "ttft_ms": res.ttft_ms, "timings": res.timings,
            "input_tokens": res.usage.input_tokens, "output_tokens": res.usage.output_tokens,
            "tokens_estimated": res.usage.estimated, "provider": provider_name, "model": provider.model,
            "llm_calls": res.llm_calls,
        }
        out: dict = {"metrics": metrics}
        try:
            if outcome == "complete":
                content = res.content
            elif outcome == "cancelled":
                partial = "".join(text_parts) if res.mode == "answer" else ""
                content = (partial + "\n\n" if partial else "") + "_Stopped by user._"
            else:
                content = f"**Request failed:** {err.message if err else 'unknown error'}"
            from app.models.usage import estimate_cost
            metrics["cost_usd"] = round(estimate_cost(provider_name, provider.model,
                                                      res.usage.input_tokens, res.usage.output_tokens), 6)
            msg = Message(
                conversation_id=conv_id, role="assistant", content=content, status=outcome,
                provider=provider_name, model=provider.model,
                input_tokens=res.usage.input_tokens, output_tokens=res.usage.output_tokens,
                activity_log=agent.activity,
                meta={"mode": res.mode, "scope": res.scope, "files_read": res.files_read,
                      "request_id": req_id, "metrics": metrics,
                      "error": ({"kind": err.kind, "message": err.message, "retryable": err.retryable}
                                if err else None)},
            )
            db.session.add(msg)
            db.session.flush()
            out["message_id"] = msg.id

            if outcome == "complete" and res.changes and ws_db_id:
                change = ProposedChange(
                    user_id=user_id, workspace_id=ws_db_id, conversation_id=conv_id, message_id=msg.id,
                    request_id=req_id, status="pending", summary=res.summary, plan=res.plan_steps,
                    git_branch=agent.workspace.branch if agent.workspace else None,
                    files=[{"path": c.path, "action": c.action, "original": c.original, "new": c.new,
                            "original_hash": res.file_hashes.get(c.path) if c.action != "create" else None,
                            "diff": c.diff, "additions": c.additions, "deletions": c.deletions,
                            "warnings": c.warnings} for c in res.changes],
                )
                db.session.add(change)
                db.session.flush()
                out["change"] = change.to_dict()
            if outcome == "complete" and res.command and ws_db_id:
                appr = PendingApproval(
                    conversation_id=conv_id, user_id=user_id, workspace_id=ws_db_id, message_id=msg.id,
                    tool_name="run_command", tool_args={"command": res.command["command"]},
                    reason=res.command.get("reason"), danger_level=res.command["danger_level"],
                    status="pending",
                )
                db.session.add(appr)
                db.session.flush()
                out["command"] = {**appr.to_dict(), "risk_reason": res.command.get("risk_reason")}

            if res.llm_calls or outcome == "complete":
                UsageRecord.record(user_id=user_id, provider=provider_name, model=provider.model,
                                   input_tokens=res.usage.input_tokens, output_tokens=res.usage.output_tokens,
                                   conversation_id=conv_id, request_id=req_id, duration_ms=total_ms,
                                   status=outcome, estimated=res.usage.estimated)
            c = db.session.get(Conversation, conv_id)
            if c:
                c.updated_at = datetime.now(timezone.utc)
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception("[chat] req=%s failed to persist results", req_id)
        return out

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Request-ID": req_id},
    )
