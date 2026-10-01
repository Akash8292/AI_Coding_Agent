"""
Applying reviewed changes to the workspace.

apply_change_set() is the only code path that writes agent edits to disk:
  1. Pre-check EVERY file: its current bytes must hash to what the proposal was
     computed from (modify/delete), or it must not exist (create). If the user
     changed a file after the proposal, nothing is written — status "stale".
  2. Optional git checkpoint (non-destructive snapshot) before writing.
  3. Atomic write of each file, preserving newline style and BOM.
  4. If any write fails, already-written files are restored (all-or-nothing).
  5. Verification: re-read each file and confirm it matches the reviewed
     content, then syntax-check languages we can check.

revert_change_set() undoes an applied change the same way, and refuses to
touch a file the user has edited since it was applied.
"""
import os
from typing import Optional

from app.agent.editor import syntax_error
from app.repository import workspace_fs, git_tools


class StaleChangeError(Exception):
    def __init__(self, paths: list[str]):
        super().__init__("Files changed since the proposal was made: " + ", ".join(paths))
        self.paths = paths


def _current(root: str, path: str) -> Optional[bytes]:
    return workspace_fs.read_raw(root, path)


def apply_change_set(root: str, files: list[dict], checkpoint: bool = True,
                     label: str = "before applying change") -> dict:
    """
    files: [{path, action, original, new, original_hash}]
    Returns {ok, checkpoint, verification:[{path, ok, message}], written:[paths]}.
    Raises StaleChangeError (nothing written) or OSError/WorkspacePathError.
    """
    stale = []
    snapshots: dict[str, Optional[bytes]] = {}
    for f in files:
        path = f["path"]
        workspace_fs.resolve(root, path, for_write=True)  # validates path policy
        raw = _current(root, path)
        snapshots[path] = raw
        if f["action"] == "create":
            if raw is not None:
                stale.append(path)
        elif workspace_fs.sha256_bytes(raw) != f.get("original_hash"):
            stale.append(path)
    if stale:
        raise StaleChangeError(stale)

    cp = None
    if checkpoint and git_tools.is_git_repo(root):
        res = git_tools.create_checkpoint(root, label)
        cp = res.get("sha") if res.get("ok") else None

    written: list[str] = []
    try:
        for f in files:
            path = f["path"]
            if f["action"] == "delete":
                workspace_fs.delete_file(root, path)
            else:
                workspace_fs.write_atomic(root, path, workspace_fs.encode_like(f["new"], snapshots[path]))
            written.append(path)
    except Exception:
        _restore(root, written, snapshots)
        raise

    return {"ok": True, "checkpoint": cp, "written": written,
            "verification": verify(root, files, snapshots)}


def verify(root: str, files: list[dict], snapshots: dict) -> list[dict]:
    results = []
    for f in files:
        path = f["path"]
        raw = _current(root, path)
        if f["action"] == "delete":
            ok = raw is None
            results.append({"path": path, "ok": ok, "check": "deleted",
                            "message": "File removed" if ok else "File still exists"})
            continue
        expected = workspace_fs.encode_like(f["new"], snapshots.get(path))
        if raw != expected:
            results.append({"path": path, "ok": False, "check": "content",
                            "message": "File on disk does not match the reviewed content"})
            continue
        err = syntax_error(path, f["new"])
        if err:
            results.append({"path": path, "ok": False, "check": "syntax", "message": f"Syntax error: {err}"})
        else:
            checked = os.path.splitext(path)[1].lower() in (".py", ".json", ".toml", ".js", ".mjs",
                                                             ".cjs", ".yml", ".yaml")
            results.append({"path": path, "ok": True, "check": "syntax" if checked else "content",
                            "message": "Written; syntax OK" if checked else "Written and verified"})
    return results


def _restore(root: str, paths: list[str], snapshots: dict) -> None:
    for path in reversed(paths):
        raw = snapshots.get(path)
        try:
            if raw is None:
                workspace_fs.delete_file(root, path)
            else:
                workspace_fs.write_atomic(root, path, raw)
        except Exception:
            pass


def revert_change_set(root: str, files: list[dict]) -> dict:
    """
    Undo an applied change. Each file must still contain exactly what was
    written; otherwise nothing is reverted (StaleChangeError).
    """
    stale = []
    snapshots = {}
    for f in files:
        path = f["path"]
        raw = _current(root, path)
        snapshots[path] = raw
        if f["action"] == "delete":
            if raw is not None:
                stale.append(path)
        elif raw is None or workspace_fs.to_logical(raw.decode("utf-8-sig", errors="replace")) != f["new"]:
            stale.append(path)
    if stale:
        raise StaleChangeError(stale)

    written = []
    try:
        for f in files:
            path = f["path"]
            if f["action"] == "create":
                workspace_fs.delete_file(root, path)
            else:
                workspace_fs.write_atomic(root, path, workspace_fs.encode_like(f["original"], snapshots[path]))
            written.append(path)
    except Exception:
        _restore(root, written, snapshots)
        raise
    return {"ok": True, "reverted": written}


def apply_file_edit(repo_path: str, path: str, proposed_content: str) -> dict:
    """Single-file write with the same safety rules (legacy helper)."""
    try:
        raw = _current(repo_path, path)
        if raw is None:
            return {"ok": False, "error": f"File not found: {path}"}
        f = {"path": path, "action": "modify", "new": workspace_fs.to_logical(proposed_content),
             "original_hash": workspace_fs.sha256_bytes(raw)}
        res = apply_change_set(repo_path, [f], checkpoint=False)
        return {"ok": True, "path": path, "verification": res["verification"]}
    except workspace_fs.WorkspacePathError as e:
        return {"ok": False, "error": f"Path escapes repository boundary or is protected: {e}"}
    except (OSError, StaleChangeError) as e:
        return {"ok": False, "error": str(e)}


def compute_diff(original: str, proposed: str, path: str) -> list[dict]:
    from app.agent.editor import compute_diff as _cd
    return _cd(original, proposed, path)


def _is_within(path: str, root: str) -> bool:
    return git_tools._is_within(path, root)
