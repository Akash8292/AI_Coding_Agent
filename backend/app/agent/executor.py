"""
Agent tool executor.
Handles permission checking, sandboxing, and actual execution.
"""
import difflib
import os
import subprocess
from datetime import datetime, timezone
from typing import Any, Optional

from app.agent.tools import TOOLS, DANGER_SAFE, DANGER_MODERATE, DANGER_DANGEROUS, get_tool
from app.repository import searcher, git_tools
from app.repository.indexer import list_files as index_list_files

# Permission modes
PERM_ASK_ALWAYS = "ask_always"
PERM_ASK_DANGEROUS = "ask_dangerous"   # default: safe/moderate auto-approve
PERM_MANUAL_ONLY = "manual_only"        # everything needs approval


def _is_within(path: str, root: str) -> bool:
    path = os.path.realpath(path)
    root = os.path.realpath(root)
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def needs_approval(tool_name: str, permission_mode: str) -> bool:
    """Return True if this tool requires explicit user approval."""
    tool = get_tool(tool_name)
    if not tool:
        return True  # unknown tools always need approval

    if permission_mode == PERM_MANUAL_ONLY:
        return True
    elif permission_mode == PERM_ASK_ALWAYS:
        return True
    elif permission_mode == PERM_ASK_DANGEROUS:
        # Only moderate and dangerous need approval
        return tool.danger_level in (DANGER_MODERATE, DANGER_DANGEROUS)
    return True


def execute_tool(tool_name: str, args: dict, workspace_id: int,
                 repo_path: str, cfg: dict = None) -> dict:
    """
    Execute a tool and return its result.
    Returns {ok, result, error, activity}
    """
    cfg = cfg or {}

    if tool_name == "list_files":
        files = index_list_files(workspace_id, repo_path, cfg)
        return {
            "ok": True,
            "result": files,
            "activity": f"Listed {len(files)} files in repository",
        }

    elif tool_name == "search_code":
        query = args.get("query", "")
        top_k = args.get("top_k", 6)
        results = searcher.search(workspace_id, repo_path, query, top_k=top_k, cfg=cfg)
        return {
            "ok": True,
            "result": results,
            "activity": f"Found {len(results)} relevant code sections for: {query[:50]}",
        }

    elif tool_name == "read_file":
        path = args.get("path", "")
        abs_path = os.path.join(repo_path, path)
        if not _is_within(abs_path, repo_path):
            return {"ok": False, "error": "Path escapes repository boundary", "activity": f"Blocked: {path}"}

        content = searcher.read_file(repo_path, path)
        if content is None:
            return {"ok": False, "error": f"Cannot read file: {path}", "activity": f"Failed to read: {path}"}

        start = args.get("start_line")
        end = args.get("end_line")
        if start or end:
            lines = content.splitlines()
            s = (start or 1) - 1
            e = end or len(lines)
            content = "\n".join(lines[s:e])

        return {
            "ok": True,
            "result": {"path": path, "content": content},
            "activity": f"Read file: {path}",
        }

    elif tool_name == "git_status":
        result = git_tools.git_status(repo_path)
        return {"ok": result["ok"], "result": result["stdout"], "activity": "Checked git status"}

    elif tool_name == "git_diff":
        result = git_tools.git_diff(repo_path, args.get("path"))
        return {"ok": result["ok"], "result": result["stdout"], "activity": "Checked git diff"}

    elif tool_name == "git_log":
        result = git_tools.git_log(repo_path, args.get("n", 10))
        return {"ok": result["ok"], "result": result["stdout"], "activity": "Read git log"}

    elif tool_name == "edit_file":
        # Returns a pending diff — not applied yet, goes through user review
        path = args.get("path", "")
        instruction = args.get("instruction", "")
        abs_path = os.path.join(repo_path, path)
        if not _is_within(abs_path, repo_path):
            return {"ok": False, "error": "Path escapes repository boundary"}
        if not os.path.isfile(abs_path):
            return {"ok": False, "error": f"File not found: {path}"}
        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                original = f.read()
        except OSError as e:
            return {"ok": False, "error": str(e)}
        return {
            "ok": True,
            "result": {"path": path, "original": original, "instruction": instruction,
                       "status": "pending_edit"},
            "activity": f"Prepared edit for: {path}",
        }

    elif tool_name == "create_file":
        path = args.get("path", "")
        content = args.get("content", "")
        abs_path = os.path.join(repo_path, path)
        if not _is_within(abs_path, repo_path):
            return {"ok": False, "error": "Path escapes repository boundary"}
        try:
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(content)
            return {"ok": True, "result": {"path": path}, "activity": f"Created file: {path}"}
        except OSError as e:
            return {"ok": False, "error": str(e)}

    elif tool_name == "delete_file":
        path = args.get("path", "")
        abs_path = os.path.join(repo_path, path)
        if not _is_within(abs_path, repo_path):
            return {"ok": False, "error": "Path escapes repository boundary"}
        try:
            # Backup before delete
            backup = abs_path + ".bak"
            with open(abs_path, "r", encoding="utf-8") as f:
                with open(backup, "w", encoding="utf-8") as b:
                    b.write(f.read())
            os.remove(abs_path)
            return {"ok": True, "result": {"path": path, "backup": backup},
                    "activity": f"Deleted file: {path} (backup: {backup})"}
        except OSError as e:
            return {"ok": False, "error": str(e)}

    elif tool_name == "run_command":
        command = args.get("command", "").strip()
        timeout = cfg.get("COMMAND_TIMEOUT_SECONDS", 30)
        max_output = cfg.get("COMMAND_MAX_OUTPUT_BYTES", 50_000)

        if not command:
            return {"ok": False, "error": "Empty command"}

        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            out = (proc.stdout + proc.stderr)[:max_output]
            return {
                "ok": proc.returncode == 0,
                "result": {
                    "command": command,
                    "exit_code": proc.returncode,
                    "output": out,
                },
                "activity": f"Ran command: {command[:60]} (exit {proc.returncode})",
            }
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"Command timed out after {timeout}s",
                    "activity": f"Command timed out: {command[:60]}"}
        except OSError as e:
            return {"ok": False, "error": str(e)}

    elif tool_name == "run_tests":
        command = args.get("command", "pytest -q")
        return execute_tool("run_command", {"command": command, "reason": "running tests"},
                            workspace_id, repo_path, cfg)

    return {"ok": False, "error": f"Unknown tool: {tool_name}"}


def apply_file_edit(repo_path: str, path: str, proposed_content: str) -> dict:
    """Write proposed content to disk after user approval. Creates .bak backup."""
    abs_path = os.path.join(repo_path, path)
    if not _is_within(abs_path, repo_path):
        return {"ok": False, "error": "Path escapes repository boundary"}
    if not os.path.isfile(abs_path):
        return {"ok": False, "error": f"File not found: {path}"}
    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            original = f.read()
        with open(abs_path + ".bak", "w", encoding="utf-8") as f:
            f.write(original)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(proposed_content)
        return {"ok": True, "path": path, "backup": path + ".bak"}
    except OSError as e:
        return {"ok": False, "error": str(e)}


def compute_diff(original: str, proposed: str, path: str) -> list[dict]:
    """Return structured diff lines."""
    diff_lines = difflib.unified_diff(
        original.splitlines(keepends=True),
        proposed.splitlines(keepends=True),
        fromfile=f"{path} (current)",
        tofile=f"{path} (proposed)",
        n=3,
    )
    result = []
    for line in diff_lines:
        if line.startswith("+++") or line.startswith("---"):
            kind = "header"
        elif line.startswith("@@"):
            kind = "hunk"
        elif line.startswith("+"):
            kind = "add"
        elif line.startswith("-"):
            kind = "remove"
        else:
            kind = "context"
        result.append({"type": kind, "text": line.rstrip("\n")})
    return result
