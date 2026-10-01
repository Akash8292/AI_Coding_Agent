"""
Workspace filesystem safety layer.

Every read or write of a user's repository goes through here, so the rules
are enforced in one place:
  - paths are resolved relative to the workspace root and must stay inside it
    (no '..', no absolute paths, no symlink escapes)
  - VCS internals and secret files (.git, .env, keys) are never read or written
  - binary and oversized files are refused
  - writes are atomic (temp file + os.replace) and preserve the file's
    original newline style and encoding BOM
"""
import fnmatch
import hashlib
import os
import subprocess
import tempfile
import time
from typing import Optional

IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", "venv", ".venv", "env", ".env",
    "dist", "build", ".next", ".nuxt", "target", ".idea", ".vscode",
    "coverage", ".pytest_cache", ".mypy_cache", ".ruff_cache", "index_cache",
    ".tox", ".eggs", ".gradle", ".terraform", "bower_components", "vendor",
}

# Never sent to an LLM, never modified by the agent.
SECRET_PATTERNS = [
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*",
    "*.keystore", ".npmrc", ".pypirc", ".netrc", "credentials*.json", "secrets.*",
]
SECRET_ALLOWLIST = {".env.example", ".env.sample", ".env.template"}

BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".pdf", ".zip", ".gz",
    ".tar", ".7z", ".rar", ".exe", ".dll", ".so", ".dylib", ".bin", ".pkl", ".pyc",
    ".db", ".sqlite", ".sqlite3", ".woff", ".woff2", ".ttf", ".otf", ".mp3", ".mp4",
    ".mov", ".avi", ".wav", ".jar", ".class", ".o", ".a", ".lib", ".npy", ".npz",
    ".parquet", ".h5", ".onnx", ".pt", ".ckpt", ".safetensors",
}

DEFAULT_MAX_READ_BYTES = 1_000_000


class WorkspacePathError(ValueError):
    """The path is outside the workspace, protected, binary, or too large."""


def is_secret_file(rel_path: str) -> bool:
    name = os.path.basename(rel_path).lower()
    if name in SECRET_ALLOWLIST:
        return False
    return any(fnmatch.fnmatch(name, pat) for pat in SECRET_PATTERNS)


def is_ignored_path(rel_path: str) -> bool:
    parts = rel_path.replace("\\", "/").split("/")
    return any(p in IGNORE_DIRS for p in parts[:-1])


def resolve(root: str, rel_path: str, *, for_write: bool = False) -> str:
    """
    Resolve rel_path inside root, raising WorkspacePathError if it escapes the
    root or targets a protected file. Returns the absolute real path.
    """
    if not rel_path or not isinstance(rel_path, str):
        raise WorkspacePathError("Empty file path")
    cleaned = rel_path.strip().replace("\\", "/")
    if cleaned.startswith("/") or (len(cleaned) > 1 and cleaned[1] == ":"):
        raise WorkspacePathError(f"Absolute paths are not allowed: {rel_path}")
    if "\x00" in cleaned:
        raise WorkspacePathError("Invalid path")
    root_real = os.path.realpath(root)
    abs_path = os.path.realpath(os.path.join(root_real, cleaned))
    try:
        inside = os.path.commonpath([abs_path, root_real]) == root_real
    except ValueError:
        inside = False
    if not inside or abs_path == root_real:
        raise WorkspacePathError(f"Path escapes the workspace: {rel_path}")
    rel = os.path.relpath(abs_path, root_real).replace("\\", "/")
    if rel.split("/")[0] == ".git" or "/.git/" in f"/{rel}/":
        raise WorkspacePathError(f"Refusing to touch git internals: {rel}")
    if is_secret_file(rel):
        raise WorkspacePathError(f"{rel} looks like a secrets file and is protected")
    if for_write and os.path.splitext(rel)[1].lower() in BINARY_EXTS:
        raise WorkspacePathError(f"Refusing to write binary file type: {rel}")
    return abs_path


def normalize_rel(root: str, rel_path: str) -> str:
    """Canonical forward-slash path relative to root (validated)."""
    return os.path.relpath(resolve(root, rel_path), os.path.realpath(root)).replace("\\", "/")


def _looks_binary(sample: bytes) -> bool:
    return b"\x00" in sample


def read_text(root: str, rel_path: str, max_bytes: int = DEFAULT_MAX_READ_BYTES) -> str:
    """Read a text file inside the workspace. Raises WorkspacePathError / FileNotFoundError."""
    abs_path = resolve(root, rel_path)
    if not os.path.isfile(abs_path):
        raise FileNotFoundError(rel_path)
    if os.path.splitext(abs_path)[1].lower() in BINARY_EXTS:
        raise WorkspacePathError(f"{rel_path} is a binary file")
    size = os.path.getsize(abs_path)
    if size > max_bytes:
        raise WorkspacePathError(f"{rel_path} is too large ({size:,} bytes; limit {max_bytes:,})")
    with open(abs_path, "rb") as f:
        data = f.read()
    if _looks_binary(data[:8192]):
        raise WorkspacePathError(f"{rel_path} appears to be binary")
    text = data.decode("utf-8-sig" if data.startswith(b"\xef\xbb\xbf") else "utf-8", errors="replace")
    return text


def read_raw(root: str, rel_path: str) -> Optional[bytes]:
    """Raw bytes of a workspace file, or None if it does not exist."""
    abs_path = resolve(root, rel_path)
    if not os.path.isfile(abs_path):
        return None
    with open(abs_path, "rb") as f:
        return f.read()


def sha256_bytes(data: Optional[bytes]) -> Optional[str]:
    return hashlib.sha256(data).hexdigest() if data is not None else None


def file_hash(root: str, rel_path: str) -> Optional[str]:
    return sha256_bytes(read_raw(root, rel_path))


def to_logical(text: str) -> str:
    """Normalise newlines to \\n for editing/diffing."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def detect_newline(raw: Optional[bytes]) -> str:
    if raw and b"\r\n" in raw:
        return "\r\n"
    return "\n"


def encode_like(text: str, original_raw: Optional[bytes]) -> bytes:
    """Encode logical (\\n) text using the original file's newline style and BOM."""
    nl = detect_newline(original_raw)
    out = text.replace("\n", nl) if nl != "\n" else text
    data = out.encode("utf-8")
    if original_raw and original_raw.startswith(b"\xef\xbb\xbf"):
        data = b"\xef\xbb\xbf" + data
    return data


def write_atomic(root: str, rel_path: str, data: bytes) -> None:
    abs_path = resolve(root, rel_path, for_write=True)
    directory = os.path.dirname(abs_path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".codesage-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        if os.path.exists(abs_path):
            try:
                os.chmod(tmp, os.stat(abs_path).st_mode)
            except OSError:
                pass
        _replace_with_retry(tmp, abs_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _replace_with_retry(src: str, dst: str) -> None:
    # Windows: editors/antivirus can hold a transient lock on the target
    for attempt in range(5):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.1 * (attempt + 1))


def delete_file(root: str, rel_path: str) -> None:
    abs_path = resolve(root, rel_path, for_write=True)
    if os.path.isfile(abs_path):
        os.remove(abs_path)


# ── File listing ──────────────────────────────────────────────────────────────

def list_files(root: str, max_files: int = 20_000) -> list[str]:
    """
    Relative paths of the workspace's files. Uses `git ls-files` when the
    workspace is a git repo (respects .gitignore), otherwise walks the tree
    skipping IGNORE_DIRS. Secret files are excluded.
    """
    files = _git_ls_files(root)
    if files is None:
        files = []
        for dirpath, dirs, names in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d not in IGNORE_DIRS and not d.startswith("."))
            rel_dir = os.path.relpath(dirpath, root)
            for n in sorted(names):
                rel = n if rel_dir == "." else f"{rel_dir}/{n}".replace("\\", "/")
                files.append(rel)
                if len(files) >= max_files:
                    break
            if len(files) >= max_files:
                break
    out = [f for f in files if not is_secret_file(f) and not is_ignored_path(f)]
    return out[:max_files]


def _git_ls_files(root: str) -> Optional[list[str]]:
    if not os.path.isdir(os.path.join(root, ".git")):
        return None
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root, capture_output=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    paths = [p for p in proc.stdout.decode("utf-8", "replace").split("\x00") if p]
    # ls-files lists deleted-but-tracked files too; keep only what exists
    return sorted(p for p in paths if os.path.isfile(os.path.join(root, p)))
