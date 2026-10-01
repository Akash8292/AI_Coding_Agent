"""
Request understanding and context budgeting.

classify_request() decides, before any LLM call, WHAT the user wants (mode)
and HOW MUCH of the repository is needed to do it (scope):

    mode   answer   explain / question — stream prose, never a diff
           edit     change code — structured edit → validate → diff → review
           command  run something — needs explicit user permission

    scope  explicit_file    the message names one existing file   → read it
           explicit_files   the message names several files        → read them
           repository       needs repo search to find the context  → search
           general          no repository context needed           → none
           command          command request

The cheapest sufficient scope wins: naming a file never triggers a repo-wide
search, while a repository question still gets search + relevant files.
"""
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from app.repository import workspace_fs

SCOPE_EXPLICIT_FILE = "explicit_file"
SCOPE_EXPLICIT_FILES = "explicit_files"
SCOPE_REPOSITORY = "repository"
SCOPE_GENERAL = "general"
SCOPE_COMMAND = "command"

MODE_ANSWER = "answer"
MODE_EDIT = "edit"
MODE_COMMAND = "command"


# ── Token budgeting ───────────────────────────────────────────────────────────

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
    Fit conversation history into max_tokens, dropping the oldest turns first.
    Consecutive same-role messages (e.g. a user turn whose reply failed) are
    merged so every provider receives a valid alternating sequence.
    """
    available = max_tokens - count_tokens_approx(system_prompt) - count_tokens_approx(repo_context) - 1000

    result: list[dict] = []
    used = 0
    for msg in reversed(messages):
        tokens = count_tokens_approx(msg.get("content", ""))
        if used + tokens > available and result:
            break
        result.insert(0, msg)
        used += tokens

    merged: list[dict] = []
    for m in result:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1] = {"role": m["role"], "content": merged[-1]["content"] + "\n\n" + m["content"]}
        else:
            merged.append({"role": m["role"], "content": m["content"]})
    while merged and merged[0]["role"] != "user":
        merged.pop(0)
    return merged


# ── Workspace file index (cached) ───────────────────────────────────────────

_files_lock = threading.Lock()
_files_cache: dict[str, tuple[float, list[str]]] = {}
FILES_TTL = 20.0


def workspace_files(repo_path: str) -> list[str]:
    """Cached file list for explicit-file resolution (cheap for repeated turns)."""
    key = os.path.realpath(repo_path)
    with _files_lock:
        hit = _files_cache.get(key)
        if hit and time.time() - hit[0] < FILES_TTL:
            return hit[1]
    files = workspace_fs.list_files(repo_path) if os.path.isdir(repo_path) else []
    with _files_lock:
        _files_cache[key] = (time.time(), files)
    return files


def invalidate_workspace_files(repo_path: str) -> None:
    with _files_lock:
        _files_cache.pop(os.path.realpath(repo_path), None)


# ── Explicit file detection ──────────────────────────────────────────────────

EXTENSIONLESS_NAMES = {"dockerfile", "makefile", "procfile", "readme", "license", "gemfile",
                       "rakefile", "jenkinsfile", "vagrantfile", "cmakelists.txt"}

_PATHLIKE = re.compile(r"""(?<![\w@])([A-Za-z0-9_.\-/\\]*[A-Za-z0-9_\-]\.[A-Za-z0-9]{1,10}|[A-Za-z0-9_.\-]+(?:[/\\][A-Za-z0-9_.\-]+)+)(?![\w/])""")
_WORD = re.compile(r"[A-Za-z0-9_.\-]+")


def extract_explicit_files(message: str, repo_path: str,
                           files: Optional[list[str]] = None) -> list[str]:
    """Existing workspace files named in the message, in mention order."""
    found, _ = resolve_explicit_files(message, repo_path, files)
    return found


def resolve_explicit_files(message: str, repo_path: str,
                           files: Optional[list[str]] = None) -> tuple[list[str], list[str]]:
    """
    Returns (paths, notes). Candidates are path-like tokens and well-known
    extensionless names; only files that actually exist are returned. A bare
    basename that matches several files resolves to the shallowest one and
    adds a note so the ambiguity is visible to the user.
    """
    if not repo_path or not os.path.isdir(repo_path):
        return [], []
    files = files if files is not None else workspace_files(repo_path)
    if not files:
        return [], []

    by_lower = {f.lower(): f for f in files}
    by_base: dict[str, list[str]] = {}
    for f in files:
        by_base.setdefault(os.path.basename(f).lower(), []).append(f)

    text = re.sub(r"```.*?```", " ", message, flags=re.DOTALL)  # ignore pasted code blocks
    candidates: list[str] = []
    for m in _PATHLIKE.finditer(text):
        candidates.append(m.group(1))
    for m in _WORD.finditer(text):
        if m.group(0).lower() in EXTENSIONLESS_NAMES:
            candidates.append(m.group(0))

    found: list[str] = []
    notes: list[str] = []
    for raw in candidates:
        cand = raw.strip("./\\").replace("\\", "/").rstrip(".,;:!?)")
        if cand.lower().startswith("./"):
            cand = cand[2:]
        low = cand.lower()
        match: Optional[str] = by_lower.get(low)
        if match is None and "/" in low:
            suffix = [f for f in files if f.lower().endswith("/" + low)]
            if suffix:
                match = min(suffix, key=lambda p: (p.count("/"), len(p)))
        if match is None:
            base_hits = by_base.get(os.path.basename(low), [])
            if base_hits:
                match = min(base_hits, key=lambda p: (p.count("/"), len(p)))
                if len(base_hits) > 1:
                    others = ", ".join(sorted(h for h in base_hits if h != match)[:3])
                    notes.append(f"'{cand}' matches {len(base_hits)} files; using {match} (also: {others})")
        if match and match not in found:
            found.append(match)
    return found, notes


# ── Intent / mode detection ──────────────────────────────────────────────────

MODIFICATION_VERBS = {
    "add", "create", "implement", "write", "fix", "refactor", "update", "change",
    "modify", "edit", "delete", "remove", "rename", "move", "improve", "optimize",
    "replace", "insert", "upgrade", "downgrade", "convert", "migrate", "append",
    "prepend", "comment", "document", "annotate", "extract", "inline", "rewrite",
    "clean", "cleanup", "format", "reformat", "simplify", "split", "merge", "bump",
    "patch", "introduce", "wrap", "port", "translate", "make", "set", "use", "switch",
    "generate", "adjust", "correct", "handle", "validate", "sort", "reorder", "drop",
}
# "Write/give/generate an explanation of X" produces prose, not a code change
ANSWER_ARTIFACTS = {"explanation", "explanations", "summary", "summaries", "analysis", "overview",
                    "description", "walkthrough", "breakdown", "review", "comparison", "answer",
                    "tutorial", "essay", "report", "critique", "assessment", "evaluation"}
# Verbs that are only a modification when paired with a code target
WEAK_VERBS = {"make", "set", "use", "switch", "generate", "handle", "validate", "sort",
              "comment", "document", "format", "clean", "port", "translate", "drop", "wrap"}
TARGET_CODE_WORDS = {
    "function", "functions", "class", "classes", "file", "files", "bug", "issue", "method",
    "exception", "test", "tests", "requirment", "requirement", "requirements", "dependency",
    "dependencies", "package", "packages", "version", "config", "setting", "settings",
    "code", "line", "lines", "import", "imports", "variable", "endpoint", "comment",
    "comments", "docstring", "docstrings", "header", "logging", "logger", "type", "types",
    "hints", "route", "routes", "module", "component", "field", "column", "parameter",
    "argument", "return", "error", "errors", "handler", "api", "button", "style", "css",
}
QUESTION_STARTERS = {
    "what", "why", "how", "where", "which", "who", "when", "explain", "describe",
    "summarize", "summarise", "tell", "show", "list", "does", "do", "is", "are",
    "can", "could", "would", "should", "will", "inspect", "review", "analyze",
    "analyse", "understand", "walk", "compare", "find", "search", "look", "check",
    "read", "give", "help", "i", "whats", "what's", "hows",
}
POLITE_PREFIX = re.compile(
    r"^(?:please|pls|kindly|hey|ok|okay|now|also|then|so|and|codesage)[,\s]+", re.I)
REQUEST_PREFIX = re.compile(
    r"^(?:can|could|would|will)\s+you\s+(?:please\s+)?|^i\s+(?:want|need|would like)\s+(?:you\s+)?to\s+|^let'?s\s+|^go\s+ahead\s+and\s+",
    re.I)
# Sentence punctuation only counts when followed by whitespace/end, so the "." in
# "demo.py" never splits a clause.
CLAUSE_SPLIT = re.compile(r"(?:[.;!?]+(?=\s|$)|\n+|,\s+|\s+and\s+(?:then\s+)?|\s+then\s+|\s+also\s+|\s+but\s+)", re.I)

_NOT_A_TARGET = r"(?!(?:the|a|an|it|this|that|sure|them|my|our|all|every|some|me|us)\b)"
COMMAND_PREFIX = re.compile(
    r"^(?:please\s+)?(?:run|execute|exec|launch)\s+\S"
    r"|^(?:pip3?|npm|npx|yarn|pnpm|cargo|mvn|gradle|dotnet|poetry|uv)\s+" + _NOT_A_TARGET + r"[\w\-.]"
    r"|^pytest\b"
    r"|^python3?\s+(?:-m\s+\S|-c\s|\S+\.py\b)"
    r"|^node\s+\S+\.[cm]?js\b"
    r"|^git\s+(?:status|log|diff|show|branch|fetch|pull|stash|add|commit|checkout|switch)\b"
    r"|^docker(?:-compose)?\s+(?:compose|build|run|ps|up|down|logs|exec)\b"
    r"|^make\s+" + _NOT_A_TARGET + r"[\w\-]+\s*$"
    r"|^go\s+(?:run|build|test|vet|fmt|mod|get)\b", re.I)

REPO_PHRASES = [
    "this project", "the project", "this repo", "the repo", "this repository", "the repository",
    "this codebase", "the codebase", "our project", "our repo", "our codebase", "my project",
    "my repo", "my code", "our code", "this code", "this app", "the app", "this application",
    "what this is doing", "what is this doing", "what does this do", "how does this work",
    "project overview", "codebase overview", "architecture", "file structure",
    "folder structure", "directory structure", "entry point", "in this", "in our", "in my",
]
OVERVIEW_PHRASES = [
    "explain this project", "explain the project", "explain the repo", "explain this repo",
    "explain the codebase", "explain this codebase", "project overview", "codebase overview",
    "what does this project", "what is this project", "what this project", "architecture",
    "file structure", "folder structure", "directory structure", "overview", "how is the project",
    "how is this project", "structure of", "tech stack", "summarize the project", "summarize this",
]
REPO_KEYWORDS = {
    "project", "repo", "repository", "codebase", "workspace", "architecture", "overview",
    "endpoint", "endpoints", "route", "routes", "schema", "schemas", "model", "models",
    "database", "pipeline", "module", "modules", "implemented", "implementation", "defined",
    "declared", "used", "called", "configured", "flow",
}
GENERAL_PATTERNS = [
    r"^what(?:'s| is| are)\s+(?:a|an|the)?\s*(?:difference|concept|meaning|purpose of a)\b",
    r"^(?:what|how)\s+(?:is|are|does|do)\s+(?:a|an)\s+\w+",
    r"\bin general\b", r"\bgenerally\b", r"\bbest practices?\b", r"\bdifference between\b",
    r"\bin (?:python|javascript|typescript|java|go|rust|c\+\+|c#|ruby|php)\b\s*\??$",
]
STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "is", "are", "was",
    "be", "it", "this", "that", "how", "what", "why", "where", "does", "do", "work", "works",
    "working", "can", "you", "me", "my", "our", "i", "we", "explain", "show", "tell", "about",
    "please", "which", "who", "when", "there", "here", "from", "into", "code", "file", "files",
    "project", "repo", "use", "used", "using", "get", "set", "have", "has", "all", "any", "some",
    "should", "would", "could", "will", "not", "no", "yes", "its", "them", "they", "their",
}


def _clauses(message: str) -> list[list[str]]:
    msg = re.sub(r"```.*?```", " ", message, flags=re.DOTALL).strip().lower()
    out = []
    for clause in CLAUSE_SPLIT.split(msg):
        clause = clause.strip()
        prev = None
        while prev != clause:  # strip stacked politeness: "ok please can you ..."
            prev = clause
            clause = POLITE_PREFIX.sub("", clause)
            clause = REQUEST_PREFIX.sub("", clause)
        words = re.findall(r"[a-z0-9_']+", clause)
        if words:
            out.append(words)
    return out


def detect_mode(message: str) -> str:
    """answer | edit | command — see module docstring."""
    stripped = message.strip()
    if COMMAND_PREFIX.search(stripped):
        return MODE_COMMAND

    clauses = _clauses(message)
    all_words = [w for c in clauses for w in c]
    if not all_words:
        return MODE_ANSWER

    # 0. "Write/give a (detailed, long) explanation of …" — the verb's object is prose
    if all_words[0] in MODIFICATION_VERBS and any(w in ANSWER_ARTIFACTS for w in all_words[1:10]):
        return MODE_ANSWER

    # 1. An imperative modification verb opening any clause → edit
    for words in clauses:
        first = words[0]
        if first in MODIFICATION_VERBS and any(w in ANSWER_ARTIFACTS for w in words[1:6]):
            continue  # "write a detailed explanation of ..." is a question, not an edit
        if first in MODIFICATION_VERBS:
            if first in WEAK_VERBS and not any(w in TARGET_CODE_WORDS or "." in w for w in words):
                continue
            return MODE_EDIT
        # "in requirements.txt add X", "for demo.py, add X": verb right after a locator phrase
        if first in ("in", "for", "inside", "within", "to") and len(words) > 2:
            for w in words[1:6]:
                if w in MODIFICATION_VERBS and w not in WEAK_VERBS:
                    return MODE_EDIT

    # 2. Questions / explanations stay answers unless a clause is imperative (handled above)
    if all_words[0] in QUESTION_STARTERS:
        return MODE_ANSWER

    # 3. Loose phrasing ("the login is broken, fix please") — verb + code target anywhere
    verbs = [w for w in all_words if w in MODIFICATION_VERBS and w not in WEAK_VERBS]
    if any(w in ANSWER_ARTIFACTS for w in all_words[:8]):
        return MODE_ANSWER
    if verbs and (any(w in TARGET_CODE_WORDS for w in all_words) or len(verbs) >= 2):
        return MODE_EDIT
    return MODE_ANSWER


def _mentions_repo(message: str) -> bool:
    low = message.lower()
    if any(p in low for p in REPO_PHRASES):
        return True
    words = set(re.findall(r"[a-z0-9_]+", low))
    return bool(words & REPO_KEYWORDS)


REPO_SEARCH_PATTERNS = [
    r"\b(find|locate|search|look for|track down|trace)\b",
    r"\b(across|throughout|everywhere|all (?:the )?(?:files|places|usages|callers|references))\b",
    r"\b(every|each) (?:file|place|usage|caller|call site|reference)\b",
    r"\b(where(?:ver)?|which files?)\b.*\b(used|called|defined|referenced|imported)\b",
    r"\bthe \w+ (?:flow|logic|layer|pipeline|system|module)\b",
    r"\b(codebase|whole project|entire project|the repo)\b",
]


def wants_repo_search(message: str) -> bool:
    """True when the request reaches beyond the files it names."""
    low = message.lower()
    return any(re.search(p, low) for p in REPO_SEARCH_PATTERNS)


def wants_overview(message: str) -> bool:
    low = message.lower()
    return any(p in low for p in OVERVIEW_PHRASES)


def _is_general_knowledge(message: str) -> bool:
    low = message.lower().strip()
    return any(re.search(p, low) for p in GENERAL_PATTERNS)


def content_terms(message: str) -> list[str]:
    words = re.findall(r"[a-z][a-z0-9_]{2,}", message.lower())
    return [w for w in words if w not in STOPWORDS]


def terms_match_repo(message: str, files: list[str],
                     vocab: Optional[Callable[[str], int]] = None) -> list[str]:
    """
    Content terms of the message that correspond to something in the repo:
    a path component ("auth" ↔ "authentication") or, when an index vocabulary
    is available, identifiers that occur in the code.
    """
    comps: set[str] = set()
    for f in files:
        for part in re.split(r"[/._\-]", f.lower()):
            if len(part) >= 3:
                comps.add(part)
    hits = []
    for term in content_terms(message):
        matched = term in comps or any(
            (len(c) >= 4 and term.startswith(c)) or (len(term) >= 4 and c.startswith(term))
            for c in comps)
        if not matched and vocab is not None:
            matched = vocab(term) >= 2
        if matched:
            hits.append(term)
    return hits


def detect_intent(message: str) -> str:
    """
    Backwards-compatible coarse intent: general | repository | modification | command.
    """
    mode = detect_mode(message)
    if mode == MODE_EDIT:
        return "modification"
    if mode == MODE_COMMAND:
        return "command"
    if _mentions_repo(message) and not _is_general_knowledge(message):
        return "repository"
    return "general"


@dataclass
class RequestPlan:
    mode: str
    scope: str
    explicit_files: list[str] = field(default_factory=list)
    overview: bool = False
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def intent(self) -> str:  # legacy label used in logs/UI
        return {"edit": "modification", "command": "command"}.get(
            self.mode, "repository" if self.scope != SCOPE_GENERAL else "general")

    def to_dict(self) -> dict:
        return {"mode": self.mode, "scope": self.scope, "explicit_files": self.explicit_files,
                "overview": self.overview, "reasons": self.reasons, "notes": self.notes,
                "intent": self.intent}


def classify_request(message: str, repo_path: str = "",
                     vocab: Optional[Callable[[str], int]] = None) -> RequestPlan:
    """Decide mode and minimal sufficient scope for a request."""
    mode = detect_mode(message)
    has_repo = bool(repo_path) and os.path.isdir(repo_path)

    if mode == MODE_COMMAND:
        return RequestPlan(mode, SCOPE_COMMAND if has_repo else SCOPE_GENERAL,
                           reasons=["message asks to run a command"])

    explicit, notes = ([], [])
    if has_repo:
        explicit, notes = resolve_explicit_files(message, repo_path)

    if explicit:
        if wants_repo_search(message):
            # Named files are pinned, but the request also reaches across the codebase
            return RequestPlan(mode, SCOPE_REPOSITORY, explicit_files=explicit, notes=notes,
                               reasons=[f"names {', '.join(explicit)} but also asks to search the codebase"])
        scope = SCOPE_EXPLICIT_FILE if len(explicit) == 1 else SCOPE_EXPLICIT_FILES
        return RequestPlan(mode, scope, explicit_files=explicit, notes=notes,
                           reasons=[f"message names {', '.join(explicit)}"])

    if not has_repo:
        return RequestPlan(mode, SCOPE_GENERAL, reasons=["no workspace attached"])

    if mode == MODE_EDIT:
        return RequestPlan(mode, SCOPE_REPOSITORY, overview=wants_overview(message),
                           reasons=["code change without a named file — must locate targets"])

    if wants_overview(message):
        return RequestPlan(mode, SCOPE_REPOSITORY, overview=True,
                           reasons=["asks about the project as a whole"])
    if _mentions_repo(message) and not _is_general_knowledge(message):
        return RequestPlan(mode, SCOPE_REPOSITORY, reasons=["refers to this codebase"])
    if not _is_general_knowledge(message):
        hits = terms_match_repo(message, workspace_files(repo_path), vocab)
        if hits:
            return RequestPlan(mode, SCOPE_REPOSITORY,
                               reasons=[f"terms found in the repository: {', '.join(hits[:4])}"])
    return RequestPlan(mode, SCOPE_GENERAL, reasons=["general programming question"])


def detect_scope(message: str, repo_path: str = "") -> dict:
    """Legacy dict form of classify_request()."""
    plan = classify_request(message, repo_path)
    scope = plan.scope
    if scope == SCOPE_EXPLICIT_FILES:
        scope = SCOPE_EXPLICIT_FILE
    return {"scope": scope, "intent": plan.intent, "explicit_files": plan.explicit_files}


# ── Workspace overview ───────────────────────────────────────────────────────

DOC_CANDIDATES = ["README.md", "readme.md", "README.rst", "README.txt", "README",
                  "ARCHITECTURE.md", "architecture.md", "DESIGN.md"]
MANIFESTS = ["pyproject.toml", "package.json", "requirements.txt", "backend/requirements.txt",
             "go.mod", "Cargo.toml", "pom.xml", "build.gradle", "Gemfile", "composer.json"]


def render_file_tree(files: list[str], max_entries: int = 200) -> str:
    """Compact tree: shows files up to max_entries, then summarises the rest by directory."""
    if len(files) <= max_entries:
        return "\n".join(files)
    shown = files[:max_entries]
    rest: dict[str, int] = {}
    for f in files[max_entries:]:
        d = f.rsplit("/", 1)[0] if "/" in f else "."
        rest[d] = rest.get(d, 0) + 1
    tail = "\n".join(f"{d}/ (+{n} more files)" for d, n in sorted(rest.items())[:40])
    return "\n".join(shown) + "\n" + tail


def get_workspace_overview(repo_path: str, max_files: int = 200) -> dict:
    """File tree + primary README + dependency manifests for project-level questions."""
    overview = {"file_tree": "", "docs": "", "doc_name": "", "manifests": {}, "total_files": 0}
    if not repo_path or not os.path.isdir(repo_path):
        return overview
    files = workspace_files(repo_path)
    overview["total_files"] = len(files)
    overview["file_tree"] = render_file_tree(files, max_files)
    for doc in DOC_CANDIDATES:
        if doc in files or os.path.isfile(os.path.join(repo_path, doc)):
            try:
                overview["docs"] = workspace_fs.read_text(repo_path, doc)[:12000]
                overview["doc_name"] = doc
                break
            except (OSError, workspace_fs.WorkspacePathError):
                pass
    for mf in MANIFESTS:
        if mf in files:
            try:
                overview["manifests"][mf] = workspace_fs.read_text(repo_path, mf)[:3000]
            except (OSError, workspace_fs.WorkspacePathError):
                pass
    return overview
