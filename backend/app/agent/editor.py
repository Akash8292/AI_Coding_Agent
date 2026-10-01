"""
CodeSage structured edit engine.

The LLM never writes the final file or the diff. It returns an EDIT PLAN
(JSON, schema below) describing *operations*; this module deterministically
applies those operations to the ORIGINAL file contents, validates the result,
and computes the unified diff itself — so the diff shown to the user is
exactly the change that will be written.

Edit plan schema
----------------
{
  "summary": "one or two sentences describing the change",
  "plan":    ["optional", "ordered", "steps"],
  "files": [
    {
      "path": "relative/path.py",
      "action": "modify" | "create" | "delete",
      "operations": [                      # action == "modify"
        {"type": "insert",  "line": 1, "content": "text\\n"},          # before line N (N = last+1 appends)
        {"type": "insert",  "after_line": 12, "content": "text\\n"},   # after line N (0 = top of file)
        {"type": "prepend", "content": "text\\n"},
        {"type": "append",  "content": "text\\n"},
        {"type": "replace", "old_text": "exact existing text", "new_text": "replacement"},
        {"type": "replace_lines", "start_line": 3, "end_line": 5, "content": "new lines\\n"},
        {"type": "delete",  "old_text": "exact existing text"},
        {"type": "delete",  "start_line": 3, "end_line": 5},
        {"type": "rewrite", "content": "entire new file"}
      ],
      "content": "full text"               # action == "create"
    }
  ]
}

Rules enforced (each violation raises EditError with a message precise enough
to send back to the model for one repair attempt):
  - `old_text` must occur exactly once (exact match; if absent, a unique match
    ignoring trailing whitespace / indentation-only differences is accepted)
  - line numbers must be in range; operations may not overlap
  - modify/delete targets must exist; create targets must not
  - the result must differ from the original and pass a syntax check for
    languages we can check (only if the original passed it too)
"""
import difflib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Optional

try:
    import tomllib  # py3.11+
except ImportError:  # pragma: no cover
    tomllib = None

MAX_FILES_PER_EDIT = 20
MAX_OPS_PER_FILE = 60


class EditError(ValueError):
    """The model's edit plan is malformed or cannot be applied safely."""

    def __init__(self, message: str, path: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.path = path


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class FileEdit:
    path: str
    action: str                          # modify | create | delete
    operations: list[dict] = field(default_factory=list)
    content: Optional[str] = None


@dataclass
class EditPlan:
    summary: str
    files: list[FileEdit]
    plan: list[str] = field(default_factory=list)


@dataclass
class FileChange:
    path: str
    action: str
    original: Optional[str]              # logical (\n) text, None for create
    new: Optional[str]                   # logical (\n) text, None for delete
    diff: str
    additions: int
    deletions: int
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"path": self.path, "action": self.action, "diff": self.diff,
                "additions": self.additions, "deletions": self.deletions,
                "warnings": self.warnings}


# ── Parsing ──────────────────────────────────────────────────────────────────

_OP_ALIASES = {
    "insert_before": "insert", "insert_after": "insert", "add": "insert",
    "search_replace": "replace", "find_replace": "replace", "substitute": "replace",
    "replace_range": "replace_lines", "remove": "delete", "delete_lines": "delete",
    "overwrite": "rewrite", "full_rewrite": "rewrite",
}
_ACTION_ALIASES = {"edit": "modify", "update": "modify", "change": "modify",
                   "add": "create", "new": "create", "remove": "delete"}


_INVALID_ESCAPE = re.compile(r'\\(?!["\\/bfnrtu])')


def extract_json(text: str, keys: tuple = ("files", "summary", "file")) -> dict:
    """
    Parse the model's JSON object. Accepts a bare object or one wrapped in a
    ```json fence / surrounded by stray prose. Never guesses at non-JSON text.
    """
    if not text or not text.strip():
        raise EditError("The model returned an empty response instead of a JSON edit plan.")
    s = text.strip()
    fence = re.search(r"```(?:json|JSON)?\s*\n(.*?)\n\s*```", s, re.DOTALL)
    candidates = [fence.group(1)] if fence else []
    candidates.append(s)
    decoder = json.JSONDecoder()
    last_err = None
    for cand in candidates:
        # try each '{' in turn (stray prose before the object may contain braces)
        starts = [m.start() for m in re.finditer(r"\{", cand)][:20]
        for start in starts:
            try:
                obj, _ = decoder.raw_decode(cand[start:])
            except json.JSONDecodeError as e:
                if "escape" not in e.msg:
                    last_err = last_err or e
                    continue
                # Invalid escapes (e.g. ' or \d) have one valid reading: a literal
                # backslash. Repair lexically; the content is not reinterpreted.
                try:
                    obj, _ = decoder.raw_decode(_INVALID_ESCAPE.sub(r"\\\\", cand[start:]))
                except json.JSONDecodeError as e2:
                    last_err = last_err or e2
                    continue
            if isinstance(obj, dict) and any(k in obj for k in keys):
                return obj
    detail = f" ({last_err.msg} at line {last_err.lineno} column {last_err.colno})" if last_err else ""
    raise EditError("The response is not valid JSON" + detail +
                    ". Return exactly one JSON object matching the edit schema.")


def parse_edit_plan(data: dict) -> EditPlan:
    """Validate the JSON shape and normalise tolerated aliases."""
    if not isinstance(data, dict):
        raise EditError("The edit plan must be a JSON object.")
    summary = str(data.get("summary") or data.get("explanation") or "").strip()
    plan_steps = data.get("plan") or []
    if not isinstance(plan_steps, list):
        plan_steps = [str(plan_steps)]

    raw_files = data.get("files")
    if raw_files is None and (data.get("file") or data.get("path")):
        # single-file shorthand: {"file": "x.py", "changes": [...]}
        raw_files = [{"path": data.get("file") or data.get("path"),
                      "action": data.get("action", "modify"),
                      "operations": data.get("operations") or data.get("changes") or [],
                      "content": data.get("content")}]
    if raw_files is None:
        raise EditError('The edit plan has no "files" array.')
    if not isinstance(raw_files, list):
        raise EditError('"files" must be an array.')
    if len(raw_files) > MAX_FILES_PER_EDIT:
        raise EditError(f"Too many files in one edit ({len(raw_files)}; max {MAX_FILES_PER_EDIT}).")

    files: list[FileEdit] = []
    seen = set()
    for i, rf in enumerate(raw_files):
        if not isinstance(rf, dict):
            raise EditError(f"files[{i}] must be an object.")
        path = str(rf.get("path") or rf.get("file") or "").strip().replace("\\", "/")
        if not path:
            raise EditError(f'files[{i}] is missing "path".')
        if path in seen:
            raise EditError(f"{path} appears more than once in files[]; merge its operations into one entry.", path)
        seen.add(path)
        action = str(rf.get("action") or "modify").lower().strip()
        action = _ACTION_ALIASES.get(action, action)
        if action not in ("modify", "create", "delete"):
            raise EditError(f'files[{i}] ({path}) has unknown action "{action}".', path)
        ops = rf.get("operations")
        if ops is None:
            ops = rf.get("changes") or rf.get("edits") or []
        if not isinstance(ops, list):
            raise EditError(f"{path}: operations must be an array.", path)
        if len(ops) > MAX_OPS_PER_FILE:
            raise EditError(f"{path}: too many operations ({len(ops)}).", path)
        content = rf.get("content")
        if action == "create" and not isinstance(content, str):
            raise EditError(f'{path}: action "create" requires a string "content".', path)
        if action == "modify" and not ops:
            if isinstance(content, str):
                ops = [{"type": "rewrite", "content": content}]
            else:
                raise EditError(f"{path}: modify requires at least one operation.", path)
        norm_ops = []
        for j, op in enumerate(ops):
            if not isinstance(op, dict):
                raise EditError(f"{path}: operation #{j + 1} must be an object.", path)
            op = dict(op)
            t = str(op.get("type") or op.get("op") or "").lower().strip()
            t = _OP_ALIASES.get(t, t)
            if t == "insert" and str(op.get("type", "")).lower() == "insert_after" and "line" in op:
                op["after_line"] = op.pop("line")
            op["type"] = t
            norm_ops.append(op)
        files.append(FileEdit(path=path, action=action, operations=norm_ops,
                              content=content if isinstance(content, str) else None))
    return EditPlan(summary=summary, files=files, plan=[str(s) for s in plan_steps][:20])


# ── Applying operations ──────────────────────────────────────────────────────

def _as_lines_block(text: str) -> str:
    """Line-oriented content always ends with a newline (empty stays empty)."""
    text = text.replace("\r\n", "\n")
    return text if (not text or text.endswith("\n")) else text + "\n"


def _line_starts(text: str) -> list[int]:
    """Offset of the start of each line, plus len(text) as the end sentinel."""
    starts = [0]
    for m in re.finditer("\n", text):
        starts.append(m.end())
    if starts[-1] != len(text):
        starts.append(len(text))
    return starts


def _num_lines(text: str) -> int:
    return len(text.splitlines())


def _int(op: dict, key: str, path: str, idx: int) -> int:
    v = op.get(key)
    try:
        return int(v)
    except (TypeError, ValueError):
        raise EditError(f"{path}: operation #{idx} ({op.get('type')}) needs integer '{key}', got {v!r}.", path)


def _find_unique(text: str, needle: str, path: str, idx: int, what: str) -> tuple[int, int]:
    """Locate needle exactly once. Falls back to a whitespace-tolerant line match."""
    needle = needle.replace("\r\n", "\n")
    if not needle:
        raise EditError(f"{path}: operation #{idx} has an empty '{what}'.", path)
    count = text.count(needle)
    if count == 1:
        start = text.index(needle)
        return start, start + len(needle)
    if count > 1:
        raise EditError(f"{path}: operation #{idx}: '{what}' matches {count} places. "
                        "Include more surrounding lines so it is unique.", path)

    # Tolerant: compare line-by-line ignoring trailing whitespace and leading
    # indentation differences, but still require exactly one match.
    n_lines = needle.strip("\n").split("\n")
    key = [l.strip() for l in n_lines]
    if not any(key):
        raise EditError(f"{path}: operation #{idx}: '{what}' is only whitespace.", path)
    hay = text.split("\n")
    starts = _line_starts(text)
    hits = []
    for i in range(0, len(hay) - len(key) + 1):
        if [h.strip() for h in hay[i:i + len(key)]] == key:
            hits.append(i)
    if len(hits) == 1:
        i = hits[0]
        start = starts[i]
        end_line = i + len(key)
        end = starts[end_line] if end_line < len(starts) else len(text)
        # keep the trailing newline outside the span unless the needle had one
        if not needle.endswith("\n") and end > start and text[end - 1:end] == "\n":
            end -= 1
        return start, end
    if len(hits) > 1:
        raise EditError(f"{path}: operation #{idx}: '{what}' matches {len(hits)} places "
                        "(ignoring whitespace). Include more surrounding lines.", path)
    preview = needle.strip().split("\n")[0][:80]
    raise EditError(f"{path}: operation #{idx}: '{what}' was not found in the file "
                    f"(first line: {preview!r}). Copy the text exactly from the current file.", path)


def apply_operations(original: str, operations: list[dict], path: str = "file") -> str:
    """
    Apply operations to `original` (logical \\n text). All positions refer to
    the ORIGINAL text; operations are resolved to spans, checked for overlap,
    and applied bottom-up so the result is deterministic.
    """
    text = original.replace("\r\n", "\n")
    n = _num_lines(text)
    starts = _line_starts(text)
    ends_with_nl = text.endswith("\n") or text == ""

    def line_start(line_no: int) -> int:  # 1-based; n+1 → end of text
        return starts[line_no - 1] if line_no - 1 < len(starts) else len(text)

    spans: list[tuple[int, int, str, int]] = []  # (start, end, replacement, op_index)
    for idx, op in enumerate(operations, 1):
        t = op.get("type")
        if t == "rewrite":
            if len(operations) != 1:
                raise EditError(f"{path}: 'rewrite' cannot be combined with other operations.", path)
            content = op.get("content")
            if not isinstance(content, str):
                raise EditError(f"{path}: rewrite needs string 'content'.", path)
            return content.replace("\r\n", "\n")

        if t in ("insert", "prepend", "append"):
            content = op.get("content")
            if not isinstance(content, str) or content == "":
                raise EditError(f"{path}: operation #{idx} ({t}) needs non-empty 'content'.", path)
            block = _as_lines_block(content)
            if t == "prepend":
                line = 1
            elif t == "append":
                line = n + 1
            elif "after_line" in op:
                line = _int(op, "after_line", path, idx) + 1
            elif "before_line" in op:
                line = _int(op, "before_line", path, idx)
            else:
                line = _int(op, "line", path, idx)
            if not 1 <= line <= n + 1:
                raise EditError(f"{path}: operation #{idx} inserts at line {line}, but the file has "
                                f"{n} lines (valid: 1..{n + 1}).", path)
            pos = line_start(line)
            if pos == len(text) and text and not ends_with_nl:
                block = "\n" + block  # file lacked a final newline
            spans.append((pos, pos, block, idx))

        elif t == "replace":
            old = op.get("old_text", op.get("search", op.get("find")))
            new = op.get("new_text", op.get("replace", op.get("replacement")))
            if old is None and "start_line" in op:
                op = {**op, "type": "replace_lines", "content": new if new is not None else op.get("content", "")}
                t = "replace_lines"
            else:
                if not isinstance(old, str):
                    raise EditError(f"{path}: operation #{idx} (replace) needs string 'old_text'.", path)
                if not isinstance(new, str):
                    raise EditError(f"{path}: operation #{idx} (replace) needs string 'new_text'.", path)
                s, e = _find_unique(text, old, path, idx, "old_text")
                spans.append((s, e, new.replace("\r\n", "\n"), idx))
                continue

        if t == "replace_lines" or (t == "delete" and "start_line" in op):
            s_line = _int(op, "start_line", path, idx)
            e_line = _int(op, "end_line", path, idx) if op.get("end_line") is not None else s_line
            if not (1 <= s_line <= e_line <= n):
                raise EditError(f"{path}: operation #{idx} ({t}) line range {s_line}-{e_line} is invalid "
                                f"for a file with {n} lines.", path)
            s, e = line_start(s_line), line_start(e_line + 1)
            if t == "delete":
                spans.append((s, e, "", idx))
            else:
                content = op.get("content", "")
                if not isinstance(content, str):
                    raise EditError(f"{path}: operation #{idx} (replace_lines) needs string 'content'.", path)
                block = _as_lines_block(content)
                if e == len(text) and text and not ends_with_nl and block.endswith("\n"):
                    block = block[:-1]
                spans.append((s, e, block, idx))
        elif t == "delete":
            old = op.get("old_text", op.get("text", op.get("search")))
            if not isinstance(old, str):
                raise EditError(f"{path}: operation #{idx} (delete) needs 'old_text' or a line range.", path)
            s, e = _find_unique(text, old, path, idx, "old_text")
            # deleting whole lines: also remove the line break
            if (s == 0 or text[s - 1] == "\n") and e < len(text) and text[e] == "\n":
                e += 1
            spans.append((s, e, "", idx))
        elif t not in ("insert", "prepend", "append", "replace", "replace_lines"):
            raise EditError(f"{path}: operation #{idx} has unknown type {t!r}. Allowed: insert, prepend, "
                            "append, replace, replace_lines, delete, rewrite.", path)

    # Pure insertions sort before a range starting at the same offset, so
    # "insert at X" + "replace from X" means: new text, then the replacement.
    order = lambda s: (s[0], s[1] > s[0], s[3])  # noqa: E731
    ordered = sorted(spans, key=order)
    for a, b in zip(ordered, ordered[1:]):
        if a[1] > b[0]:
            raise EditError(f"{path}: operations #{a[3]} and #{b[3]} overlap. "
                            "Combine them into a single operation.", path)

    out = text
    for s, e, rep, _ in sorted(spans, key=order, reverse=True):
        out = out[:s] + rep + out[e:]
    return out


# ── Validation ───────────────────────────────────────────────────────────────

def syntax_error(path: str, text: str) -> Optional[str]:
    """Return a syntax error message for languages we can check, else None."""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext in (".py", ".pyw"):
            compile(text, path, "exec", dont_inherit=True)
        elif ext == ".json":
            json.loads(text) if text.strip() else None
        elif ext == ".toml" and tomllib is not None:
            tomllib.loads(text)
        elif ext in (".yml", ".yaml"):
            try:
                import yaml  # optional dependency
            except ImportError:
                return None
            list(yaml.safe_load_all(text))
        elif ext in (".js", ".mjs", ".cjs"):
            return _node_check(text, ext)
    except SyntaxError as e:
        return f"line {e.lineno}: {e.msg}"
    except Exception as e:  # json/toml/yaml decode errors
        return str(e).split("\n")[0][:200]
    return None


def _node_check(text: str, ext: str) -> Optional[str]:
    node = shutil.which("node")
    if not node:
        return None
    fd, tmp = tempfile.mkstemp(suffix=ext)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        proc = subprocess.run([node, "--check", tmp], capture_output=True, text=True, timeout=10)
        if proc.returncode != 0:
            lines = [l for l in proc.stderr.splitlines() if "SyntaxError" in l]
            return lines[0].strip() if lines else "JavaScript syntax error"
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return None


# ── Diffs ────────────────────────────────────────────────────────────────────

def unified_diff(original: Optional[str], new: Optional[str], path: str) -> str:
    a = (original or "").splitlines(keepends=True)
    b = (new or "").splitlines(keepends=True)
    for lines in (a, b):
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n\\ No newline at end of file\n"
    diff = difflib.unified_diff(a, b,
                                fromfile="/dev/null" if original is None else f"a/{path}",
                                tofile="/dev/null" if new is None else f"b/{path}", n=3)
    return "".join(diff)


def diff_stats(diff: str) -> tuple[int, int]:
    adds = dels = 0
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            adds += 1
        elif line.startswith("-") and not line.startswith("---"):
            dels += 1
    return adds, dels


def compute_diff(original: str, proposed: str, path: str) -> list[dict]:
    """Structured diff lines [{type, text}] (kept for API compatibility)."""
    out = []
    for line in unified_diff(original, proposed, path).splitlines():
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
        out.append({"type": kind, "text": line})
    return out


# ── End-to-end: plan → validated file changes ───────────────────────────────

def build_changes(plan: EditPlan, read_file, file_exists) -> list[FileChange]:
    """
    Resolve an EditPlan against the current workspace.
      read_file(path) -> logical text (raises FileNotFoundError / ValueError)
      file_exists(path) -> bool
    Returns validated FileChange objects; raises EditError on any problem.
    """
    changes: list[FileChange] = []
    for fe in plan.files:
        warnings: list[str] = []
        if fe.action == "create":
            if file_exists(fe.path):
                raise EditError(f"{fe.path} already exists; use action \"modify\" with operations.", fe.path)
            original, new = None, fe.content.replace("\r\n", "\n")
            if new and not new.endswith("\n"):
                new += "\n"
        elif fe.action == "delete":
            if not file_exists(fe.path):
                raise EditError(f"Cannot delete {fe.path}: it does not exist.", fe.path)
            original, new = read_file(fe.path), None
        else:
            try:
                original = read_file(fe.path)
            except FileNotFoundError:
                raise EditError(f"Cannot modify {fe.path}: file does not exist. "
                                "Use action \"create\" to add a new file.", fe.path)
            new = apply_operations(original, fe.operations, fe.path)
            if new == original:
                raise EditError(f"The operations for {fe.path} do not change the file.", fe.path)

        if new is not None:
            err = syntax_error(fe.path, new)
            if err:
                prior = syntax_error(fe.path, original) if original is not None else None
                if prior is None:
                    raise EditError(f"{fe.path}: the edited file has a syntax error ({err}).", fe.path)
                warnings.append(f"File already had a syntax error before this edit ({prior}).")

        diff = unified_diff(original, new, fe.path)
        adds, dels = diff_stats(diff)
        changes.append(FileChange(fe.path, fe.action, original, new, diff, adds, dels, warnings))
    return changes
