"""
Prompt construction for the three agent modes.

Answer mode streams prose. Edit mode asks for a JSON edit plan (see
app.agent.editor for the schema and how it is applied). Command mode asks for a
single JSON command proposal. Context blocks are assembled by the runner and
passed in; prompts never include more than the runner decided was needed.
"""
import os

BASE_SYSTEM_PROMPT = """You are CodeSage, an expert AI software engineering assistant working inside the user's repository.

- Ground every statement about the codebase in the context provided below; reference files as `path:line`.
- If the provided context is insufficient, say exactly what is missing instead of guessing.
- Use markdown with fenced code blocks and language tags.
- Explain WHY, not just WHAT, for bugs and design questions.
- You cannot modify files in this mode. If the user wants a change, describe it briefly; they can ask you to make it."""

EDIT_SYSTEM_PROMPT = """You are CodeSage's code-editing engine. You modify files by returning a JSON EDIT PLAN.
CodeSage applies your operations to the current files, validates them, and shows the user a diff to accept or reject.

Return exactly ONE JSON object, no prose, no markdown fences:
{
  "summary": "1-2 sentences: what you changed and why",
  "plan": ["short step", "..."],
  "files": [
    {
      "path": "relative/path/to/file.ext",
      "action": "modify",
      "operations": [ ...operations... ]
    }
  ]
}

Operations (all line numbers refer to the numbered listing of the ORIGINAL file you were given):
  {"type": "insert", "line": N, "content": "..."}          insert BEFORE line N (N = last line + 1 appends)
  {"type": "insert", "after_line": N, "content": "..."}    insert AFTER line N (0 = top of file)
  {"type": "prepend", "content": "..."}                     add at the very top of the file
  {"type": "append", "content": "..."}                      add at the very end of the file
  {"type": "replace", "old_text": "...", "new_text": "..."} old_text must be copied EXACTLY from the file and occur once
  {"type": "replace_lines", "start_line": A, "end_line": B, "content": "..."}  replace lines A..B inclusive
  {"type": "delete", "old_text": "..."}  or  {"type": "delete", "start_line": A, "end_line": B}
Other file actions:
  {"path": "new/file.ext", "action": "create", "content": "full file text"}
  {"path": "old/file.ext", "action": "delete"}

Rules:
1. Make the SMALLEST change that satisfies the request. Never rewrite a whole file for a local change; do not reformat or reorder unrelated code.
2. The "N| " prefixes in listings are NOT part of the file. Never include them in content or old_text.
3. Reproduce indentation exactly (spaces vs tabs) and write complete, working code — no placeholders like "..." or "rest unchanged".
4. Operations on one file must not overlap. Put all operations for a file in one entry.
5. Only touch files that genuinely need to change. You may create new files when the request requires them.
6. Match the file's language conventions (e.g. `#` comments in Python, `//` in JavaScript, module docstrings where idiomatic).
7. If no change is needed or the request cannot be done safely with the given context, return "files": [] and explain why in "summary"."""

COMMAND_SYSTEM_PROMPT = """You are CodeSage. The user wants to run a command in their repository.
Propose exactly one shell command that does what they asked, based on the project files listed below.
Return one JSON object and nothing else:
{"command": "the exact command line", "reason": "one sentence on what it does and why"}
If the request is unsafe, destructive, or unclear, return {"command": null, "reason": "why not"}.
Never propose commands that delete files, rewrite git history, install system packages, or access the network unless the user explicitly asked."""

REPAIR_PROMPT = """Your edit plan could not be applied:

{error}

Return a corrected JSON edit plan (the complete object, all files). Copy old_text exactly from the numbered listings, without the "N| " prefixes.
The "summary" is shown to the user: describe the code change itself, not this correction."""


def numbered(text: str) -> str:
    """Listing with 1-based line numbers so the model can address lines precisely."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    width = max(3, len(str(len(lines))))
    return "\n".join(f"{i:>{width}}| {line}" for i, line in enumerate(lines, 1))


def lang_of(path: str) -> str:
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    return {"py": "python", "js": "javascript", "ts": "typescript", "md": "markdown",
            "yml": "yaml", "sh": "bash", "rs": "rust", "rb": "ruby"}.get(ext, ext or "text")


def file_block(path: str, content: str, *, with_line_numbers: bool,
               start_line: int = 1, end_line: int = 0, note: str = "") -> str:
    total = len(content.splitlines())
    header = f"### {path}"
    if start_line > 1 or (end_line and end_line < total):
        header += f" (lines {start_line}-{end_line} of {total})"
    else:
        header += f" ({total} lines)"
    if note:
        header += f" — {note}"
    body = numbered(content) if with_line_numbers else content.rstrip("\n")
    if with_line_numbers and start_line > 1:
        lines = body.split("\n")
        width = max(3, len(str(start_line + len(lines))))
        body = "\n".join(f"{start_line + i:>{width}}| {l.split('| ', 1)[1] if '| ' in l else l}"
                         for i, l in enumerate(lines))
    return f"{header}\n```{lang_of(path)}\n{body}\n```"


def workspace_header(name: str, branch: str = "") -> str:
    s = f"Workspace: {name}"
    if branch:
        s += f" (git branch: {branch})"
    return s


def build_answer_system(context_blocks: list[str], workspace_line: str = "") -> str:
    parts = [BASE_SYSTEM_PROMPT]
    if workspace_line:
        parts.append(workspace_line)
    if context_blocks:
        parts.append("CONTEXT (gathered by CodeSage for this request):\n\n" + "\n\n".join(context_blocks))
    else:
        parts.append("No repository context was needed for this request.")
    return "\n\n".join(parts)


def build_edit_system(context_blocks: list[str], workspace_line: str = "",
                      extra_rules: str = "") -> str:
    parts = [EDIT_SYSTEM_PROMPT]
    if extra_rules:
        parts.append(extra_rules)
    if workspace_line:
        parts.append(workspace_line)
    parts.append("FILES (current contents, with line numbers):\n\n" + "\n\n".join(context_blocks)
                 if context_blocks else "No existing files were provided; create new files if needed.")
    return "\n\n".join(parts)


def build_command_system(file_tree: str, workspace_line: str = "") -> str:
    parts = [COMMAND_SYSTEM_PROMPT]
    if workspace_line:
        parts.append(workspace_line)
    if file_tree:
        parts.append("Project files:\n" + file_tree)
    return "\n\n".join(parts)
