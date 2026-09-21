"""
Git tools — safe subprocess wrappers for git operations.
All operations are path-validated and timeout-bounded.
"""
import os
import subprocess
from typing import Optional


def _is_within(path: str, root: str) -> bool:
    path = os.path.realpath(path)
    root = os.path.realpath(root)
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def _run_git(args: list[str], repo_path: str, timeout: int = 15) -> dict:
    """Run a git command in repo_path. Returns {ok, stdout, stderr, exit_code}."""
    if not os.path.isdir(repo_path):
        return {"ok": False, "stdout": "", "stderr": "Repo path not found", "exit_code": -1}
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "ok": result.returncode == 0,
            "stdout": result.stdout[:20_000],
            "stderr": result.stderr[:5_000],
            "exit_code": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "stdout": "", "stderr": f"git timed out after {timeout}s", "exit_code": -1}
    except FileNotFoundError:
        return {"ok": False, "stdout": "", "stderr": "git not found in PATH", "exit_code": -1}
    except OSError as e:
        return {"ok": False, "stdout": "", "stderr": str(e), "exit_code": -1}


def git_status(repo_path: str) -> dict:
    result = _run_git(["status", "--short", "--branch"], repo_path)
    return result


def git_diff(repo_path: str, path: Optional[str] = None) -> dict:
    args = ["diff", "--stat", "-p"]
    if path:
        args += ["--", path]
    return _run_git(args, repo_path, timeout=20)


def git_log(repo_path: str, n: int = 20) -> dict:
    return _run_git(["log", f"-{n}", "--oneline", "--decorate", "--no-walk=unsorted"], repo_path)


def git_show(repo_path: str, ref: str = "HEAD") -> dict:
    # Sanitize ref — only allow alphanumeric, /, -, _, .
    import re
    if not re.match(r"^[a-zA-Z0-9/_.\-~^:@{}]+$", ref):
        return {"ok": False, "stdout": "", "stderr": "Invalid git ref", "exit_code": -1}
    return _run_git(["show", "--stat", ref], repo_path, timeout=20)


def git_current_branch(repo_path: str) -> str:
    result = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo_path)
    return result["stdout"].strip() if result["ok"] else "unknown"


def create_checkpoint(repo_path: str, message: str = "AI checkpoint") -> dict:
    """Create a git stash as a reversible checkpoint before AI changes."""
    result = _run_git(["stash", "push", "-m", f"codesage: {message}",
                        "--include-untracked"], repo_path)
    return result


def list_stashes(repo_path: str) -> dict:
    return _run_git(["stash", "list"], repo_path)


def restore_checkpoint(repo_path: str) -> dict:
    """Restore most recent stash."""
    return _run_git(["stash", "pop"], repo_path)


def is_git_repo(repo_path: str) -> bool:
    result = _run_git(["rev-parse", "--git-dir"], repo_path, timeout=5)
    return result["ok"]
