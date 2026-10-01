"""
Git tools — safe subprocess wrappers for git operations.

All operations are timeout-bounded, run with an argument list (no shell), and
NEVER discard user work: checkpoints are created with `git stash create` +
`git stash store`, which snapshot tracked changes without touching the
working tree, and restoring a checkpoint first snapshots the current state.
"""
import os
import re
import subprocess
from typing import Optional

GIT_TIMEOUT = 15
_REF_RE = re.compile(r"^[A-Za-z0-9/_.\-~^:@{}]+$")


def _is_within(path: str, root: str) -> bool:
    path = os.path.realpath(path)
    root = os.path.realpath(root)
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def _run_git(args: list[str], repo_path: str, timeout: int = GIT_TIMEOUT) -> dict:
    """Run a git command in repo_path. Returns {ok, stdout, stderr, exit_code}."""
    if not os.path.isdir(repo_path):
        return {"ok": False, "stdout": "", "stderr": "Repo path not found", "exit_code": -1}
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
    try:
        result = subprocess.run(
            ["git", "-c", "core.quotepath=off"] + args,
            cwd=repo_path, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, env=env,
        )
        return {
            "ok": result.returncode == 0,
            "stdout": result.stdout[:200_000],
            "stderr": result.stderr[:5_000],
            "exit_code": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "stdout": "", "stderr": f"git timed out after {timeout}s", "exit_code": -1}
    except FileNotFoundError:
        return {"ok": False, "stdout": "", "stderr": "git not found in PATH", "exit_code": -1}
    except OSError as e:
        return {"ok": False, "stdout": "", "stderr": str(e), "exit_code": -1}


def is_git_repo(repo_path: str) -> bool:
    result = _run_git(["rev-parse", "--is-inside-work-tree"], repo_path, timeout=5)
    return result["ok"] and result["stdout"].strip() == "true"


def git_status(repo_path: str) -> dict:
    """Raw porcelain status (kept for API compatibility)."""
    return _run_git(["status", "--short", "--branch"], repo_path)


def status_summary(repo_path: str) -> dict:
    """
    Parsed status: {is_repo, branch, head, ahead, behind, clean, changes:[{path, code, label}]}.
    """
    if not is_git_repo(repo_path):
        return {"is_repo": False}
    res = _run_git(["status", "--porcelain=v1", "--branch", "-z"], repo_path)
    if not res["ok"]:
        return {"is_repo": True, "error": res["stderr"].strip()}
    branch, ahead, behind = "", 0, 0
    changes = []
    entries = res["stdout"].split("\x00")
    i = 0
    while i < len(entries):
        e = entries[i]
        i += 1
        if not e:
            continue
        if e.startswith("## "):
            head = e[3:]
            m = re.match(r"(?:No commits yet on )?([^.\s]+)", head)
            branch = m.group(1) if m else head
            a = re.search(r"ahead (\d+)", head)
            b = re.search(r"behind (\d+)", head)
            ahead, behind = int(a.group(1)) if a else 0, int(b.group(1)) if b else 0
            continue
        code, path = e[:2], e[3:]
        if code[0] in "RC":  # rename/copy: next entry is the source path
            i += 1
        changes.append({"path": path, "code": code, "label": _status_label(code)})
    head_sha = _run_git(["rev-parse", "--short", "HEAD"], repo_path, timeout=5)
    return {
        "is_repo": True,
        "branch": branch or current_branch(repo_path),
        "head": head_sha["stdout"].strip() if head_sha["ok"] else None,
        "ahead": ahead, "behind": behind,
        "clean": not changes,
        "changes": changes,
    }


def _status_label(code: str) -> str:
    if code == "??":
        return "untracked"
    c = code.strip()[:1]
    return {"M": "modified", "A": "added", "D": "deleted", "R": "renamed",
            "C": "copied", "U": "conflict", "T": "type change"}.get(c, "changed")


def dirty_paths(repo_path: str, paths: list[str]) -> set[str]:
    """Which of `paths` have uncommitted changes (so we can warn before editing)."""
    if not paths or not is_git_repo(repo_path):
        return set()
    res = _run_git(["status", "--porcelain=v1", "-z", "--"] + paths, repo_path)
    if not res["ok"]:
        return set()
    out = set()
    for e in res["stdout"].split("\x00"):
        if len(e) > 3:
            out.add(e[3:])
    return out


def git_diff(repo_path: str, path: Optional[str] = None) -> dict:
    args = ["diff", "--stat", "-p"]
    if path:
        args += ["--", path]
    return _run_git(args, repo_path, timeout=20)


def git_log(repo_path: str, n: int = 20) -> dict:
    return _run_git(["log", f"-{int(n)}", "--oneline", "--decorate"], repo_path)


def log_commits(repo_path: str, n: int = 20) -> list[dict]:
    sep = "\x1f"
    res = _run_git(["log", f"-{int(n)}", f"--pretty=format:%h{sep}%s{sep}%an{sep}%ar"], repo_path)
    if not res["ok"]:
        return []
    commits = []
    for line in res["stdout"].splitlines():
        parts = line.split(sep)
        if len(parts) == 4:
            commits.append({"hash": parts[0], "subject": parts[1], "author": parts[2], "date": parts[3]})
    return commits


def git_show(repo_path: str, ref: str = "HEAD") -> dict:
    if not _REF_RE.match(ref) or ref.startswith("-"):
        return {"ok": False, "stdout": "", "stderr": "Invalid git ref", "exit_code": -1}
    return _run_git(["show", "--stat", ref], repo_path, timeout=20)


def git_current_branch(repo_path: str) -> str:
    return current_branch(repo_path) or "unknown"


def current_branch(repo_path: str) -> str:
    result = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo_path, timeout=5)
    return result["stdout"].strip() if result["ok"] else ""


# ── Checkpoints (non-destructive) ───────────────────────────────────────────

def create_checkpoint(repo_path: str, message: str = "AI checkpoint") -> dict:
    """
    Snapshot the current tracked changes WITHOUT modifying the working tree.
    Returns {ok, sha, message}. A clean tree has nothing to snapshot (sha=HEAD).
    """
    if not is_git_repo(repo_path):
        return {"ok": False, "error": "Not a git repository"}
    label = f"codesage: {message}"[:200]
    created = _run_git(["stash", "create", label], repo_path)
    if not created["ok"]:
        return {"ok": False, "error": created["stderr"].strip() or "git stash create failed"}
    sha = created["stdout"].strip()
    if not sha:
        head = _run_git(["rev-parse", "HEAD"], repo_path, timeout=5)
        return {"ok": True, "sha": head["stdout"].strip() if head["ok"] else None,
                "message": "Working tree clean — HEAD is the checkpoint", "clean": True}
    stored = _run_git(["stash", "store", "-m", label, sha], repo_path)
    if not stored["ok"]:
        return {"ok": False, "error": stored["stderr"].strip() or "git stash store failed"}
    return {"ok": True, "sha": sha, "message": f"Checkpoint saved ({sha[:8]})", "clean": False}


def list_checkpoints(repo_path: str) -> list[dict]:
    res = _run_git(["stash", "list", "--format=%H%x1f%gs%x1f%cr"], repo_path)
    if not res["ok"]:
        return []
    out = []
    for i, line in enumerate(res["stdout"].splitlines()):
        parts = line.split("\x1f")
        if len(parts) == 3 and "codesage:" in parts[1]:
            out.append({"ref": f"stash@{{{i}}}", "sha": parts[0], "message": parts[1], "date": parts[2]})
    return out


def list_stashes(repo_path: str) -> dict:
    return _run_git(["stash", "list"], repo_path)


def restore_checkpoint(repo_path: str, sha: Optional[str] = None) -> dict:
    """
    Restore tracked files to a checkpoint. The current state is snapshotted
    first, so the restore itself can be undone. Untracked files are untouched.
    """
    cps = list_checkpoints(repo_path)
    if sha is None:
        if not cps:
            return {"ok": False, "error": "No CodeSage checkpoints found"}
        sha = cps[0]["sha"]
    if not _REF_RE.match(sha):
        return {"ok": False, "error": "Invalid checkpoint id"}
    backup = create_checkpoint(repo_path, "auto-backup before restore")
    if not backup.get("ok"):
        return {"ok": False, "error": "Could not snapshot current state: " + backup.get("error", "")}
    res = _run_git(["checkout", sha, "--", "."], repo_path, timeout=30)
    if not res["ok"]:
        return {"ok": False, "error": res["stderr"].strip()}
    return {"ok": True, "restored": sha, "backup": backup.get("sha"),
            "message": f"Restored checkpoint {sha[:8]}; previous state saved as {str(backup.get('sha'))[:8]}"}
