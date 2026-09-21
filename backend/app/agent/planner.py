"""
Agent planner — builds investigation plans and coordinates agent responses.
"""
from app.agent.context import detect_intent

BASE_SYSTEM_PROMPT = """You are CodeSage, an expert AI software engineering assistant.
You help developers understand, debug, and improve their codebases.

When answering questions:
- Be specific and reference actual file paths and line numbers when available
- Use markdown formatting with proper code fences and language tags
- When you find a bug or issue, explain WHY it happens, not just what to change
- Always think step by step for complex problems
- If context is insufficient, say so clearly instead of guessing

When proposing code changes:
- Explain what you're changing and why
- Show the change as a clear diff
- Consider edge cases and side effects
- Prefer minimal, surgical changes over rewrites"""

EDIT_SYSTEM_PROMPT = """You are an expert software engineer making a precise edit to a file.
You will be given the file's full current contents and an instruction.

Respond with ONLY the complete new file contents wrapped EXACTLY like this:
<<<FILE>>>
...entire new file content here...
<<<END>>>

Rules:
- No explanation, no code fences, no commentary outside the markers
- Preserve everything unrelated to the requested change
- Do not reformat, reorder, or rewrite unrelated code
- If the change is not needed, return the original file unchanged"""

DIAGNOSE_SYSTEM_PROMPT = """You are an expert software engineer diagnosing and fixing a bug.
You will be given a problem description and the contents of the most likely responsible file.

First, in 2-4 sentences, explain what's actually wrong and why.
Then output the complete corrected file contents wrapped EXACTLY like this:
<<<FILE>>>
...entire corrected file content here...
<<<END>>>

Preserve everything unrelated to the bug. If the file doesn't contain the bug, say so and return it unchanged."""


def build_investigation_plan(question: str, intent: str) -> list[str]:
    """Build a high-level plan for investigating a question."""
    if intent == "general":
        return ["Answering based on general knowledge"]

    if intent == "repository":
        return [
            "Understanding your question",
            "Searching repository for relevant code",
            "Reading relevant files",
            "Analyzing the code",
            "Preparing answer with file references",
        ]

    if intent == "modification":
        return [
            "Understanding the change requested",
            "Searching for relevant files",
            "Reading current code",
            "Planning the modification",
            "Generating the proposed changes",
            "Creating diff for your review",
        ]

    if intent == "command":
        return [
            "Understanding the command request",
            "Validating the command is safe",
            "Requesting your approval",
            "Executing command (after approval)",
            "Reporting output",
        ]

    return ["Processing your request"]


def parse_file_response(text: str) -> tuple[str, str | None]:
    """
    Extract (explanation, file_content) from a model response.
    Looks for <<<FILE>>>...<<<END>>> markers.
    """
    start_marker = "<<<FILE>>>"
    end_marker = "<<<END>>>"
    start = text.find(start_marker)
    end = text.find(end_marker)

    if start != -1 and end != -1 and end > start:
        explanation = text[:start].strip()
        content = text[start + len(start_marker):end]
        return explanation, content.lstrip("\n").rstrip("\n") + "\n"

    # Fallback: try ``` code fence
    if "```" in text:
        parts = text.split("```")
        if len(parts) >= 3:
            block = parts[1]
            lines = block.split("\n", 1)
            if len(lines) == 2 and len(lines[0].split()) <= 1:
                block = lines[1]
            return "", block.rstrip("\n") + "\n"

    return text, None
