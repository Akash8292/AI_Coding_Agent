from flask import Blueprint, request, jsonify, g, Response, stream_with_context, current_app
from app.auth.utils import require_auth
from app.database import db
from app.models.conversation import Conversation, Message
from app.models.workspace import Workspace
from app.models.usage import UsageRecord
from app.llm.factory import ProviderFactory
from app.llm.base import ProviderNotAvailableError
from app.agent.context import detect_intent, build_context, build_system_prompt
from app.agent.planner import BASE_SYSTEM_PROMPT, parse_file_response
from app.repository import searcher
import json

chat_bp = Blueprint("chat", __name__, url_prefix="/api")


@chat_bp.route("/chat", methods=["POST"])
@require_auth
def chat():
    """
    POST /api/chat — stream a chat response.
    Body: {conversation_id, message, provider, model, use_repo, mode}
    """
    body = request.get_json(force=True, silent=True) or {}
    user = g.current_user
    cfg = current_app.config

    message_text = (body.get("message") or "").strip()
    if not message_text:
        return jsonify({"error": "message is required"}), 400

    conv_id = body.get("conversation_id")
    provider_name = (body.get("provider") or cfg.get("DEFAULT_PROVIDER", "openai")).lower()
    model = body.get("model") or None
    use_repo = bool(body.get("use_repo", True))
    mode = body.get("mode", "quick")  # quick | deep

    # Load or create conversation
    if conv_id:
        conv = Conversation.query.filter_by(id=conv_id, user_id=user.id).first()
        if not conv:
            return jsonify({"error": "Conversation not found"}), 404
    else:
        conv = Conversation(
            user_id=user.id,
            title=message_text[:80],
            provider=provider_name,
            model=model,
        )
        db.session.add(conv)
        db.session.flush()  # get ID

    # Load workspace if conv has one
    workspace = None
    repo_path = None
    workspace_id = body.get("workspace_id") or (conv.workspace_id if conv else None)
    if workspace_id:
        workspace = Workspace.query.filter_by(id=workspace_id, user_id=user.id).first()
        if workspace:
            repo_path = workspace.repo_path
            if not conv.workspace_id:
                conv.workspace_id = workspace.id

    # Persist user message
    user_msg = Message(
        conversation_id=conv.id,
        role="user",
        content=message_text,
    )
    db.session.add(user_msg)

    # Detect intent + gather repo context
    intent = detect_intent(message_text)
    repo_context = ""
    if use_repo and workspace and repo_path and intent in ("repository", "modification"):
        try:
            matches = searcher.search(workspace_id, repo_path, message_text, top_k=6, cfg=cfg)
            if matches:
                blocks = [
                    f"### {m['file']} (lines {m['start_line']}-{m['end_line']}, "
                    f"relevance {m['score']:.2f})\n```\n{m['text']}\n```"
                    for m in matches
                ]
                repo_context = "\n\n".join(blocks)
        except Exception:
            pass

    # Build messages from history
    history = []
    if conv.messages:
        for msg in conv.messages[:-1]:  # exclude the just-added user message
            if msg.role in ("user", "assistant") and msg.content:
                history.append({"role": msg.role, "content": msg.content})

    history.append({"role": "user", "content": message_text})

    system_prompt = build_system_prompt(
        BASE_SYSTEM_PROMPT,
        repo_context=repo_context,
        workspace_name=workspace.name if workspace else "",
        intent=intent,
    )

    history = build_context(history, system_prompt, repo_context, max_tokens=100_000)

    # Get LLM provider
    try:
        provider = ProviderFactory.get(provider_name, model)
    except ProviderNotAvailableError as e:
        db.session.rollback()
        return jsonify({
            "error": str(e),
            "hint": f"Configure your {provider_name.upper()}_API_KEY in settings or choose another provider.",
        }), 400

    model_info = provider.get_model_info()

    def generate():
        full_response = []
        activity = [
            {"status": "done", "text": "Understanding your request"},
        ]

        if repo_context:
            activity.append({"status": "done", "text": f"Found relevant code in your repository"})

        # Yield activity log first as a JSON event
        yield f"data: {json.dumps({'type': 'activity', 'items': activity})}\n\n"

        # Stream the actual response
        yield f"data: {json.dumps({'type': 'start'})}\n\n"

        for chunk in provider.stream(history, system_prompt=system_prompt):
            full_response.append(chunk)
            yield f"data: {json.dumps({'type': 'chunk', 'content': chunk})}\n\n"

        full_text = "".join(full_response)
        input_tokens = sum(len(m.get("content", "")) // 4 for m in history)
        output_tokens = len(full_text) // 4

        # Persist assistant message
        assistant_msg = Message(
            conversation_id=conv.id,
            role="assistant",
            content=full_text,
            provider=provider_name,
            model=model_info.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            activity_log=activity,
        )
        db.session.add(assistant_msg)

        # Track usage
        UsageRecord.record(
            user_id=user.id,
            provider=provider_name,
            model=model_info.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            conversation_id=conv.id,
        )

        db.session.commit()

        yield f"data: {json.dumps({'type': 'done', 'conversation_id': conv.id, 'message_id': assistant_msg.id})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )


@chat_bp.route("/propose_edit", methods=["POST"])
@require_auth
def propose_edit():
    """
    POST /api/propose_edit — ask AI to propose a file edit.
    Body: {workspace_id, path, instruction, provider, model}
    """
    body = request.get_json(force=True, silent=True) or {}
    user = g.current_user
    cfg = current_app.config

    workspace_id = body.get("workspace_id")
    path = (body.get("path") or "").strip()
    instruction = (body.get("instruction") or "").strip()
    provider_name = (body.get("provider") or cfg.get("DEFAULT_PROVIDER", "openai")).lower()
    model = body.get("model") or None

    if not workspace_id or not path or not instruction:
        return jsonify({"error": "workspace_id, path and instruction are required"}), 400

    workspace = Workspace.query.filter_by(id=workspace_id, user_id=user.id).first()
    if not workspace:
        return jsonify({"error": "Workspace not found"}), 404

    content = searcher.read_file(workspace.repo_path, path)
    if content is None:
        return jsonify({"error": f"Cannot read file: {path}"}), 400

    from app.agent.planner import EDIT_SYSTEM_PROMPT
    from app.agent.executor import compute_diff

    messages = [{"role": "user", "content":
                 f"File path: {path}\n\nCurrent file content:\n{content}\n\n"
                 f"Instruction: {instruction}"}]

    try:
        provider = ProviderFactory.get(provider_name, model)
    except ProviderNotAvailableError as e:
        return jsonify({"error": str(e)}), 400

    raw_response = "".join(provider.stream(messages, system_prompt=EDIT_SYSTEM_PROMPT))
    explanation, proposed = parse_file_response(raw_response)

    if proposed is None:
        return jsonify({"error": "AI could not generate a valid edit",
                        "raw_response": raw_response[:500]}), 502

    # Normalize trailing newline
    if content.endswith("\n") and not proposed.endswith("\n"):
        proposed += "\n"
    elif not content.endswith("\n") and proposed.endswith("\n"):
        proposed = proposed.rstrip("\n")

    diff = compute_diff(content, proposed, path)
    return jsonify({
        "path": path,
        "original_content": content,
        "proposed_content": proposed,
        "explanation": explanation,
        "diff": diff,
        "unchanged": proposed == content,
    })


@chat_bp.route("/apply_edit", methods=["POST"])
@require_auth
def apply_edit():
    """POST /api/apply_edit — apply an approved file edit."""
    body = request.get_json(force=True, silent=True) or {}
    user = g.current_user

    workspace_id = body.get("workspace_id")
    path = (body.get("path") or "").strip()
    content = body.get("content")

    if not workspace_id or not path or content is None:
        return jsonify({"error": "workspace_id, path, content are required"}), 400

    workspace = Workspace.query.filter_by(id=workspace_id, user_id=user.id).first()
    if not workspace:
        return jsonify({"error": "Workspace not found"}), 404

    from app.agent.executor import apply_file_edit
    result = apply_file_edit(workspace.repo_path, path, content)
    if not result["ok"]:
        return jsonify({"error": result["error"]}), 500
    return jsonify(result)


@chat_bp.route("/diagnose", methods=["POST"])
@require_auth
def diagnose():
    """POST /api/diagnose — AI bug diagnosis and fix."""
    body = request.get_json(force=True, silent=True) or {}
    user = g.current_user
    cfg = current_app.config

    workspace_id = body.get("workspace_id")
    problem = (body.get("problem") or "").strip()
    provider_name = (body.get("provider") or cfg.get("DEFAULT_PROVIDER", "openai")).lower()
    model = body.get("model") or None

    if not workspace_id or not problem:
        return jsonify({"error": "workspace_id and problem are required"}), 400

    workspace = Workspace.query.filter_by(id=workspace_id, user_id=user.id).first()
    if not workspace:
        return jsonify({"error": "Workspace not found"}), 404

    matches = searcher.search(workspace_id, workspace.repo_path, problem, top_k=8, cfg=cfg)
    if not matches:
        return jsonify({"error": "No relevant code found in the indexed repository"}), 404

    primary_file = matches[0]["file"]
    content = searcher.read_file(workspace.repo_path, primary_file)
    if content is None:
        return jsonify({"error": f"Cannot read primary suspect file: {primary_file}"}), 500

    context_blocks = []
    seen = set()
    for m in matches[1:]:
        if m["file"] not in seen:
            seen.add(m["file"])
            context_blocks.append(
                f"### {m['file']} (lines {m['start_line']}-{m['end_line']})\n```\n{m['text']}\n```"
            )
        if len(seen) >= 3:
            break

    user_content = (
        f"Problem description:\n{problem}\n\n"
        f"Most likely responsible file: {primary_file}\n\n"
        f"Full current content of {primary_file}:\n{content}\n"
    )
    if context_blocks:
        user_content += "\n\nOther related code:\n\n" + "\n\n".join(context_blocks)

    from app.agent.planner import DIAGNOSE_SYSTEM_PROMPT
    from app.agent.executor import compute_diff

    try:
        provider = ProviderFactory.get(provider_name, model)
    except ProviderNotAvailableError as e:
        return jsonify({"error": str(e)}), 400

    raw = "".join(provider.stream([{"role": "user", "content": user_content}],
                                   system_prompt=DIAGNOSE_SYSTEM_PROMPT))
    explanation, proposed = parse_file_response(raw)
    if proposed is None:
        return jsonify({"error": "AI could not generate a fix", "raw_response": raw[:500]}), 502

    if content.endswith("\n") and not proposed.endswith("\n"):
        proposed += "\n"

    diff = compute_diff(content, proposed, primary_file)
    return jsonify({
        "path": primary_file,
        "explanation": explanation,
        "original_content": content,
        "proposed_content": proposed,
        "diff": diff,
        "unchanged": proposed == content,
        "other_candidates": [m["file"] for m in matches[1:] if m["file"] != primary_file][:3],
    })
