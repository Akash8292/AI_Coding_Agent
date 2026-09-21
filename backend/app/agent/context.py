"""
Context manager — handles context window limits and message summarization.
"""
import re
from typing import Optional


def count_tokens_approx(text: str) -> int:
    """Rough token estimator: ~4 chars per token."""
    return max(1, len(text) // 4)


def count_messages_tokens(messages: list[dict]) -> int:
    return sum(count_tokens_approx(m.get("content", "")) for m in messages)


def build_context(
    messages: list[dict],
    system_prompt: str,
    repo_context: str = "",
    max_tokens: int = 100_000,
) -> list[dict]:
    """
    Build a context-limited message list that fits within max_tokens.
    Trims oldest messages first (keeping the system context intact).
    """
    system_tokens = count_tokens_approx(system_prompt)
    repo_tokens = count_tokens_approx(repo_context)
    available = max_tokens - system_tokens - repo_tokens - 1000  # buffer

    # Work backwards: always keep most recent messages
    result = []
    used = 0
    for msg in reversed(messages):
        tokens = count_tokens_approx(msg.get("content", ""))
        if used + tokens > available and result:
            break
        result.insert(0, msg)
        used += tokens

    return result


def detect_intent(message: str) -> str:
    """
    Detect whether a message is:
    - 'general': general programming question
    - 'repository': question about the repo
    - 'modification': request to change code
    - 'command': request to run something
    """
    msg_lower = message.lower().strip()
    words = re.findall(r"\b[a-z0-9_]+\b", msg_lower)
    if not words:
        return "general"

    first_word = words[0]

    command_signals = {
        "run", "execute", "install", "test", "build", "deploy", "start",
        "npm", "pip", "python", "node", "docker", "make",
    }
    if first_word in command_signals:
        return "command"

    # General conceptual questions
    general_patterns = [
        r"^what is\s+(?:a|an|the)?\s*[a-z]",
        r"^how does\s+[a-z]+ work",
        r"explain\s+(?:the\s+concept\s+of\s+)?[a-z]",
        r"what's the difference",
        r"best practices?\b",
        r"what are\s+(?:the\s+)?[a-z]",
    ]
    if any(re.search(p, msg_lower) for p in general_patterns):
        # Unless it specifically references this repository
        if not any(w in words for w in ["repo", "repository", "codebase", "this project"]):
            return "general"

    modification_signals = {
        "add", "create", "implement", "write", "fix", "refactor", "update",
        "change", "modify", "edit", "delete", "remove", "rename", "move",
        "improve", "optimize", "replace", "insert",
    }

    if first_word in modification_signals:
        return "modification"

    mod_count = sum(1 for w in words if w in modification_signals)
    if mod_count >= 1 and any(w in words for w in ["function", "class", "file", "bug", "issue", "method", "exception"]):
        return "modification"
    if mod_count >= 2:
        return "modification"

    repo_signals = {
        "project", "repo", "repository", "codebase", "file", "function", "class", "method",
        "module", "api", "endpoint", "service", "component", "this", "my", "our",
        "where", "how", "why", "explain", "understand", "find", "search", "show",
    }
    repo_count = sum(1 for w in words if w in repo_signals)
    if repo_count >= 1:
        return "repository"

    return "general"


def build_system_prompt(base_prompt: str, repo_context: str = "",
                        workspace_name: str = "", intent: str = "general") -> str:
    """Assemble the full system prompt."""
    prompt = base_prompt

    if workspace_name:
        prompt += f"\n\nCurrent workspace: {workspace_name}"

    if repo_context and intent in ("repository", "modification"):
        prompt += (
            "\n\nYou have access to relevant code from the user's repository, "
            "retrieved based on their question. Use this context to give accurate, "
            "specific answers. Reference file paths and line numbers when relevant.\n\n"
            "Repository context:\n" + repo_context
        )

    return prompt
