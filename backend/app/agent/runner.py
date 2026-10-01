"""
AgentRun — the CodeSage request pipeline.

    classify (mode + scope)
      → gather the MINIMUM context the scope needs
          explicit_file(s): read just those files
          repository:       search the index, read the relevant sections
          general:          nothing
      → LLM
          answer:  stream prose
          edit:    JSON edit plan → parse → validate/apply in memory → diff
                   (one automatic repair round-trip if the plan is invalid)
          command: JSON command proposal → permission assessment
      → events for the API layer to persist and stream

The runner is framework-agnostic: it never touches the database or Flask. It
yields event dicts; app.api.chat persists proposals/messages and streams them.

Every "activity" event describes an operation that actually happened, with
its measured duration. Nothing is announced before it is done.
"""
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Iterator, Optional

from app.agent import context as ctx
from app.agent import planner
from app.agent.editor import EditError, FileChange, build_changes, extract_json, parse_edit_plan
from app.agent.permissions import assess_command
from app.llm.base import LLMProvider, ProviderCancelled, ProviderError, Usage
from app.repository import git_tools, searcher, workspace_fs

logger = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 1.0
MAX_EDIT_ATTEMPTS = 2


class AgentCancelled(Exception):
    pass


class AgentError(Exception):
    def __init__(self, kind: str, message: str, retryable: bool = True):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retryable = retryable


@dataclass
class WorkspaceRef:
    id: int
    name: str
    repo_path: str
    branch: str = ""


@dataclass
class RunResult:
    content: str = ""
    mode: str = "answer"
    scope: str = "general"
    changes: list[FileChange] = field(default_factory=list)
    file_hashes: dict = field(default_factory=dict)
    summary: str = ""
    plan_steps: list[str] = field(default_factory=list)
    command: Optional[dict] = None
    files_read: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    llm_calls: int = 0
    timings: dict = field(default_factory=dict)
    ttft_ms: Optional[int] = None


class AgentRun:
    def __init__(self, *, message: str, history: list[dict], provider: LLMProvider,
                 workspace: Optional[WorkspaceRef], cfg, cancel_event: threading.Event,
                 request_id: str, depth: str = "quick"):
        self.message = message
        self.history = history
        self.provider = provider
        self.workspace = workspace
        self.cfg = cfg
        self.cancel = cancel_event
        self.request_id = request_id
        self.depth = depth
        self.result = RunResult()
        self._llm_text = ""
        self._blocks: list[str] = []
        self._file_texts: dict[str, str] = {}
        self._matches: list[dict] = []
        self._activity: list[dict] = []
        self._t0 = time.monotonic()

    # ── Activity helpers ────────────────────────────────────────────────────

    def _snapshot(self) -> dict:
        return {"type": "activity", "items": [dict(a) for a in self._activity]}

    def _start(self, text: str, kind: str = "step") -> dict:
        item = {"id": len(self._activity) + 1, "text": text, "status": "running",
                "kind": kind, "started": time.monotonic()}
        self._activity.append(item)
        return item

    def _finish(self, item: dict, text: Optional[str] = None, status: str = "done",
                detail: Optional[str] = None) -> dict:
        item["status"] = status
        if text:
            item["text"] = text
        if detail:
            item["detail"] = detail
        item["ms"] = int((time.monotonic() - item.pop("started", time.monotonic())) * 1000)
        return self._snapshot()

    def _done(self, text: str, kind: str = "step", detail: Optional[str] = None) -> dict:
        item = self._start(text, kind)
        return self._finish(item, detail=detail)

    @property
    def activity(self) -> list[dict]:
        return [{k: v for k, v in a.items() if k != "started"} for a in self._activity]

    def _check_cancel(self):
        if self.cancel.is_set():
            raise AgentCancelled()

    def _time(self, stage: str, started: float):
        self.result.timings[stage] = int((time.monotonic() - started) * 1000) + self.result.timings.get(stage, 0)

    # ── Main entry ──────────────────────────────────────────────────────────

    def run(self) -> Iterator[dict]:
        t = time.monotonic()
        repo = self.workspace.repo_path if self.workspace else ""
        vocab = None
        if self.workspace:
            vocab = searcher.vocabulary_count(self.workspace.id, repo, self.cfg)
        plan = ctx.classify_request(self.message, repo, vocab=vocab)
        self._time("classify", t)
        self.result.mode, self.result.scope = plan.mode, plan.scope

        label = {"answer": "Question", "edit": "Code change", "command": "Command"}[plan.mode]
        scope_label = {
            ctx.SCOPE_EXPLICIT_FILE: f"target file: {', '.join(plan.explicit_files)}",
            ctx.SCOPE_EXPLICIT_FILES: f"target files: {', '.join(plan.explicit_files)}",
            ctx.SCOPE_REPOSITORY: "needs repository search",
            ctx.SCOPE_GENERAL: "no repository context needed",
            ctx.SCOPE_COMMAND: "command request",
        }[plan.scope]
        yield self._done(f"Understood request · {label} · {scope_label}", "classify",
                         detail="; ".join(plan.reasons + plan.notes) or None)
        yield {"type": "plan", **plan.to_dict()}
        self._check_cancel()

        if plan.mode == ctx.MODE_EDIT and not self.workspace:
            raise AgentError("no_workspace", "Code changes need a workspace. Add or select a "
                             "workspace (sidebar → WORKSPACE) and try again.", retryable=False)

        if plan.mode == ctx.MODE_COMMAND and self.workspace:
            yield from self._run_command_mode()
        elif plan.mode == ctx.MODE_EDIT:
            yield from self._run_edit_mode(plan)
        else:
            yield from self._run_answer_mode(plan)
        self.result.timings["total"] = int((time.monotonic() - self._t0) * 1000)

    # ── Context gathering ───────────────────────────────────────────────────

    def _budget(self) -> int:
        return int(self.cfg.get("CONTEXT_MAX_TOKENS", 60_000))

    def _read_explicit(self, paths: list[str], for_edit: bool) -> Iterator[dict]:
        """Read named files. Yields activity; returns list of context blocks via self._blocks."""
        repo = self.workspace.repo_path
        max_bytes = int(self.cfg.get("MAX_EDIT_FILE_BYTES", 300_000)) if for_edit else 1_000_000
        budget = self._budget()
        used = 0
        for path in paths:
            self._check_cancel()
            item = self._start(f"Reading {path}", "read")
            t = time.monotonic()
            try:
                raw = workspace_fs.read_raw(repo, path)
                text = workspace_fs.read_text(repo, path, max_bytes)
            except FileNotFoundError:
                self._time("read_files", t)
                yield self._finish(item, f"{path} not found", "error")
                raise AgentError("file_not_found", f"{path} does not exist in the workspace.", False)
            except workspace_fs.WorkspacePathError as e:
                self._time("read_files", t)
                yield self._finish(item, f"Cannot read {path}", "error", detail=str(e))
                raise AgentError("file_unreadable", str(e), False)
            self._time("read_files", t)
            logical = workspace_fs.to_logical(text)
            tokens = ctx.count_tokens_approx(logical)
            if used + tokens > budget:
                if for_edit:
                    yield self._finish(item, f"{path} is too large to edit in one request", "error")
                    raise AgentError("too_large", f"{path} is ~{tokens:,} tokens, above the context "
                                     f"budget ({budget:,}). Narrow the request to a smaller file or section.",
                                     False)
                keep_chars = max(0, (budget - used) * 4)
                logical = logical[:keep_chars]
                note = "truncated to fit the context budget"
            else:
                note = ""
            used += ctx.count_tokens_approx(logical)
            self._blocks.append(planner.file_block(path, logical, with_line_numbers=True, note=note))
            self._file_texts[path] = logical
            self.result.file_hashes[path] = workspace_fs.sha256_bytes(raw)
            self.result.files_read.append(path)
            lines = len(logical.splitlines())
            yield self._finish(item, f"Read {path} ({lines} lines)" + (" — truncated" if note else ""))

    def _search(self, top_k: int) -> Iterator[dict]:
        """Repository search. Stores matches in self._matches."""
        item = self._start("Searching repository index", "search")
        t = time.monotonic()
        box: dict = {}

        def work():
            try:
                box["r"] = searcher.search(self.workspace.id, self.workspace.repo_path,
                                           self.message, top_k=top_k, cfg=self.cfg)
            except Exception as e:  # surfaced below
                box["e"] = e

        th = threading.Thread(target=work, daemon=True)
        th.start()
        deadline = time.monotonic() + float(self.cfg.get("SEARCH_TIMEOUT_SECONDS", 30))
        while th.is_alive():
            th.join(0.2)
            if self.cancel.is_set():
                raise AgentCancelled()
            if time.monotonic() > deadline:
                self._time("search", t)
                yield self._finish(item, "Repository search timed out", "error")
                raise AgentError("search_timeout", "Repository search took longer than "
                                 f"{self.cfg.get('SEARCH_TIMEOUT_SECONDS', 30)}s. The index may still be "
                                 "building — try again shortly.")
        self._time("search", t)
        if "e" in box:
            logger.warning("[agent] req=%s search failed: %s", self.request_id, box["e"])
            yield self._finish(item, "Repository search failed", "error", detail=str(box["e"])[:200])
            self._matches = []
            return
        self._matches = box.get("r") or []
        files = []
        for m in self._matches:
            if m["file"] not in files:
                files.append(m["file"])
        if self._matches:
            yield self._finish(item, f"Found {len(self._matches)} relevant sections in {len(files)} file(s)",
                               detail=", ".join(files[:8]))
        else:
            yield self._finish(item, "No matching code found in the index")

    def _overview_blocks(self) -> Iterator[dict]:
        item = self._start("Reading project structure and README", "read")
        t = time.monotonic()
        ov = ctx.get_workspace_overview(self.workspace.repo_path)
        self._time("read_files", t)
        if ov["file_tree"]:
            self._blocks.append(f"### Project file tree ({ov['total_files']} files)\n```\n{ov['file_tree']}\n```")
        read = []
        if ov["docs"]:
            self._blocks.append(planner.file_block(ov["doc_name"], ov["docs"], with_line_numbers=False))
            read.append(ov["doc_name"])
        for name, text in ov["manifests"].items():
            self._blocks.append(planner.file_block(name, text, with_line_numbers=False))
            read.append(name)
        self.result.files_read.extend(read)
        yield self._finish(item, f"Read project structure ({ov['total_files']} files)"
                           + (f" and {', '.join(read)}" if read else ""))

    ENTRY_NAMES = ("main", "app", "server", "index", "cli", "wsgi", "manage", "__main__", "api",
                   "routes", "views", "models", "config", "settings")
    CODE_EXTS = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".rb", ".php", ".cs",
                 ".kt", ".swift", ".vue", ".svelte", ".c", ".cpp", ".h"}

    def _representative_files(self) -> Iterator[dict]:
        """For whole-project questions: entry points and core modules, within ~40% of the budget."""
        repo = self.workspace.repo_path
        files = [f for f in ctx.workspace_files(repo)
                 if os.path.splitext(f)[1].lower() in self.CODE_EXTS
                 and not any(p in ("tests", "test", "__tests__", "migrations", "vendor", "static")
                             for p in f.split("/")[:-1])
                 and not os.path.basename(f).startswith("test_")]

        def rank(f):
            stem = os.path.splitext(os.path.basename(f))[0].lower()
            return (0 if stem in self.ENTRY_NAMES else 1, f.count("/"), f)
        budget = int(self._budget() * 0.4)
        item = self._start("Reading core source files", "read")
        t = time.monotonic()
        used, read = 0, []
        for f in sorted(files, key=rank)[:40]:
            if len(read) >= 12:
                break
            try:
                text = workspace_fs.to_logical(workspace_fs.read_text(repo, f, 200_000))
            except (OSError, workspace_fs.WorkspacePathError):
                continue
            tokens = ctx.count_tokens_approx(text)
            if not text.strip() or used + tokens > budget:
                continue
            self._blocks.append(planner.file_block(f, text, with_line_numbers=True))
            used += tokens
            read.append(f)
        self._time("read_files", t)
        self.result.files_read.extend(read)
        if read:
            yield self._finish(item, f"Read {len(read)} core source file(s)", detail=", ".join(read))
        else:
            yield self._finish(item, "No source files fit in the context budget", "skipped")

    def _read_sections(self, for_edit: bool) -> Iterator[dict]:
        """Turn search matches into context: sections for answers, whole files for edits."""
        repo = self.workspace.repo_path
        budget = self._budget() - sum(ctx.count_tokens_approx(b) for b in self._blocks)
        by_file: dict[str, list[dict]] = {}
        for m in self._matches:
            by_file.setdefault(m["file"], []).append(m)
        ranked = sorted(by_file.items(), key=lambda kv: -max(m["score"] for m in kv[1]))
        max_files = 5 if for_edit else (8 if self.depth == "deep" else 6)

        item = self._start("Reading relevant files", "read")
        t = time.monotonic()
        read = []
        used = 0
        ranked = [(p, m) for p, m in ranked if p not in self._file_texts]
        for path, matches in ranked[:max_files]:
            self._check_cancel()
            try:
                raw = workspace_fs.read_raw(repo, path)
                text = workspace_fs.to_logical(workspace_fs.read_text(repo, path))
            except (OSError, workspace_fs.WorkspacePathError):
                continue
            lines = text.split("\n")
            whole_tokens = ctx.count_tokens_approx(text)
            if for_edit or whole_tokens <= 2500:
                if used + whole_tokens > budget:
                    continue
                self._blocks.append(planner.file_block(path, text, with_line_numbers=True))
                self._file_texts[path] = text
                self.result.file_hashes[path] = workspace_fs.sha256_bytes(raw)
                used += whole_tokens
                read.append(path)
                continue
            # Large file, answer mode: include matched ranges (+ small margin), merged
            ranges = sorted((max(1, m["start_line"] - 5), min(len(lines), m["end_line"] + 5)) for m in matches)
            merged: list[list[int]] = []
            for s, e in ranges:
                if merged and s <= merged[-1][1] + 1:
                    merged[-1][1] = max(merged[-1][1], e)
                else:
                    merged.append([s, e])
            for s, e in merged:
                section = "\n".join(lines[s - 1:e])
                tokens = ctx.count_tokens_approx(section)
                if used + tokens > budget:
                    break
                self._blocks.append(planner.file_block(path, section, with_line_numbers=True,
                                                       start_line=s, end_line=e))
                used += tokens
            read.append(f"{path} ({', '.join(f'L{s}-{e}' for s, e in merged)})")
        self._time("read_files", t)
        self.result.files_read.extend(r.split(" (")[0] for r in read)
        if read:
            yield self._finish(item, f"Read {len(read)} file(s) for context", detail=", ".join(read))
        else:
            yield self._finish(item, "No relevant file content fit in the context", "skipped")

    def _gather(self, plan: ctx.RequestPlan, for_edit: bool) -> Iterator[dict]:
        self._blocks: list[str] = []
        self._file_texts: dict[str, str] = {}
        self._matches: list[dict] = []
        if plan.scope in (ctx.SCOPE_EXPLICIT_FILE, ctx.SCOPE_EXPLICIT_FILES):
            yield from self._read_explicit(plan.explicit_files, for_edit)
        elif plan.scope == ctx.SCOPE_REPOSITORY and self.workspace:
            if plan.explicit_files:  # named files are always in context, search adds the rest
                yield from self._read_explicit(plan.explicit_files, for_edit)
            if plan.overview:
                yield from self._overview_blocks()
            top_k = 16 if (self.depth == "deep" or for_edit) else 10
            if plan.overview and not for_edit:
                # whole-project question: keyword search adds little; read the core modules
                yield from self._representative_files()
            else:
                yield from self._search(top_k)
                if self._matches:
                    yield from self._read_sections(for_edit)
            if for_edit:
                files = ctx.workspace_files(self.workspace.repo_path)
                tree = ctx.render_file_tree(files, 150)
                self._blocks.append(f"### Project file tree ({len(files)} files; for choosing paths of new files)\n```\n{tree}\n```")

    def _history(self) -> list[dict]:
        return ctx.build_context(self.history, "", max_tokens=int(self.cfg.get("HISTORY_MAX_TOKENS", 20_000)))

    def _workspace_line(self) -> str:
        if not self.workspace:
            return ""
        return planner.workspace_header(self.workspace.name, self.workspace.branch)

    # ── LLM plumbing ────────────────────────────────────────────────────────

    def _llm(self, messages: list[dict], system: str, *, json_mode: bool, max_tokens: int,
             stream_text: bool, label: str, done_label: str, effort: Optional[str] = None) -> Iterator[dict]:
        """
        Run the provider on a worker thread. Yields:
          {"type": "chunk"} (answer mode), {"type": "progress"} (edit mode),
          {"type": "heartbeat"} while waiting. Final text in self._llm_text.
        """
        q: "queue.Queue[tuple]" = queue.Queue()
        provider = self.provider

        def work():
            try:
                for piece in provider.stream(messages, system_prompt=system, cancel_event=self.cancel,
                                             json_mode=json_mode, max_tokens=max_tokens, effort=effort):
                    q.put(("piece", piece))
                q.put(("done", None))
            except BaseException as e:
                q.put(("error", e))

        provider.on_retry = lambda err, delay, attempt: q.put(("retry", (err, delay, attempt)))
        item = self._start(label, "llm")
        yield self._snapshot()
        started = time.monotonic()
        threading.Thread(target=work, daemon=True, name=f"llm-{self.request_id}").start()
        parts: list[str] = []
        first = None
        last_progress = 0.0
        while True:
            try:
                kind, val = q.get(timeout=HEARTBEAT_SECONDS)
            except queue.Empty:
                if self.cancel.is_set():
                    self._finish(item, "Stopped by user", "stopped")
                    raise AgentCancelled()
                yield {"type": "heartbeat", "elapsed_ms": int((time.monotonic() - started) * 1000)}
                continue
            if kind == "retry":
                rerr, delay, attempt = val
                why = {"rate_limit": "rate limited", "server": "temporarily unavailable",
                       "network": "network error"}.get(rerr.kind, rerr.kind)
                # close this attempt's item, open a new one: the timeline stays chronological
                yield self._finish(item, f"{provider.label} {why} — retrying in {delay:.0f}s", "error",
                                   detail=rerr.message[:200])
                item = self._start(f"{label} (attempt {attempt + 1})", "llm")
                yield self._snapshot()
                continue
            if kind == "piece":
                if first is None:
                    first = time.monotonic()
                    if self.result.ttft_ms is None:
                        self.result.ttft_ms = int((first - started) * 1000)
                parts.append(val)
                if stream_text:
                    yield {"type": "chunk", "content": val}
                elif time.monotonic() - last_progress > 0.5:
                    last_progress = time.monotonic()
                    yield {"type": "progress", "chars": sum(len(p) for p in parts)}
                continue
            if kind == "error":
                err = val
                self.result.timings["llm"] = self.result.timings.get("llm", 0) + int((time.monotonic() - started) * 1000)
                if isinstance(err, ProviderCancelled) or self.cancel.is_set():
                    self._finish(item, "Stopped by user", "stopped")
                    raise AgentCancelled()
                if isinstance(err, ProviderError):
                    self._finish(item, f"{provider.label} request failed", "error", detail=err.message)
                    yield self._snapshot()
                    raise AgentError(f"provider_{err.kind}", err.message,
                                     retryable=err.kind in ("timeout", "rate_limit", "server", "network", "empty"))
                self._finish(item, f"{provider.label} request failed", "error", detail=str(err)[:200])
                yield self._snapshot()
                raise AgentError("provider_error", f"{provider.label} request failed: {err}")
            break  # done

        elapsed = time.monotonic() - started
        self.result.timings["llm"] = self.result.timings.get("llm", 0) + int(elapsed * 1000)
        self.result.llm_calls += 1
        u = provider.last_usage
        prev = self.result.usage
        estimated = u.estimated if self.result.llm_calls == 1 else (prev.estimated or u.estimated)
        self.result.usage = Usage(prev.input_tokens + u.input_tokens,
                                  prev.output_tokens + u.output_tokens, estimated)
        text = "".join(parts)
        self._llm_text = text
        if not text.strip():
            yield self._finish(item, f"{provider.label} returned an empty response", "error")
            raise AgentError("provider_empty", f"{provider.label} ({provider.model}) returned an empty "
                             "response. Try again or choose another model.")
        yield self._finish(item, f"{done_label} ({elapsed:.1f}s, {u.output_tokens:,} output tokens)")

    # ── Modes ───────────────────────────────────────────────────────────────

    def _run_answer_mode(self, plan: ctx.RequestPlan) -> Iterator[dict]:
        yield from self._gather(plan, for_edit=False)
        self._check_cancel()
        system = planner.build_answer_system(self._blocks, self._workspace_line())
        messages = self._history() + [{"role": "user", "content": self.message}]
        yield from self._llm(messages, system, json_mode=False,
                             max_tokens=int(self.cfg.get("ANSWER_MAX_OUTPUT_TOKENS", 8192)),
                             stream_text=True, label=f"Asking {self.provider.label} ({self.provider.model})…",
                             done_label=f"{self.provider.label} answered")
        self.result.content = self._llm_text

    def _run_edit_mode(self, plan: ctx.RequestPlan) -> Iterator[dict]:
        yield from self._gather(plan, for_edit=True)
        self._check_cancel()
        if plan.scope == ctx.SCOPE_REPOSITORY and not self._file_texts:
            yield self._done("No existing file matched; the model may propose new files", "search")
        extra = ""
        if plan.scope in (ctx.SCOPE_EXPLICIT_FILE, ctx.SCOPE_EXPLICIT_FILES):
            extra = ("The user named the target file(s): " + ", ".join(plan.explicit_files) +
                     ". Change only those files unless the request clearly requires another file.")
        system = planner.build_edit_system(self._blocks, self._workspace_line(), extra)
        messages = self._history() + [{"role": "user", "content": self.message}]
        repo = self.workspace.repo_path

        def read_file(path: str) -> str:
            norm = workspace_fs.normalize_rel(repo, path)
            if norm in self._file_texts:
                return self._file_texts[norm]
            raw = workspace_fs.read_raw(repo, norm)
            if raw is None:
                raise FileNotFoundError(norm)
            text = workspace_fs.to_logical(workspace_fs.read_text(
                repo, norm, int(self.cfg.get("MAX_EDIT_FILE_BYTES", 300_000))))
            self._file_texts[norm] = text
            self.result.file_hashes[norm] = workspace_fs.sha256_bytes(raw)
            return text

        def exists(path: str) -> bool:
            return os.path.isfile(workspace_fs.resolve(repo, path))

        for attempt in range(1, MAX_EDIT_ATTEMPTS + 1):
            label = (f"Generating edit with {self.provider.label} ({self.provider.model})…" if attempt == 1
                     else f"Asking {self.provider.label} to correct the edit (attempt {attempt}/{MAX_EDIT_ATTEMPTS})…")
            yield from self._llm(messages, system, json_mode=True,
                                 max_tokens=int(self.cfg.get("EDIT_MAX_OUTPUT_TOKENS", 32768)),
                                 stream_text=False, label=label,
                                 done_label=f"{self.provider.label} returned an edit plan",
                                 effort="low" if plan.scope == ctx.SCOPE_EXPLICIT_FILE else None)
            raw_text = self._llm_text
            self._check_cancel()
            item = self._start("Validating edit plan", "validate")
            t = time.monotonic()
            try:
                edit_plan = parse_edit_plan(extract_json(raw_text))
                for fe in edit_plan.files:  # path policy before touching anything
                    try:
                        fe.path = workspace_fs.normalize_rel(repo, fe.path)
                    except workspace_fs.WorkspacePathError as e:
                        raise EditError(f"{fe.path}: {e}", fe.path)
                changes = build_changes(edit_plan, read_file, exists)
            except EditError as e:
                self._time("validate", t)
                logger.info("[agent] req=%s edit attempt %d invalid: %s", self.request_id, attempt, e.message)
                if attempt < MAX_EDIT_ATTEMPTS:
                    yield self._finish(item, "Edit plan rejected by validation — requesting a fix", "error",
                                       detail=e.message)
                    messages = messages + [
                        {"role": "assistant", "content": raw_text[:20000]},
                        {"role": "user", "content": planner.REPAIR_PROMPT.format(error=e.message)},
                    ]
                    continue
                yield self._finish(item, "Edit plan failed validation", "error", detail=e.message)
                raise AgentError("edit_invalid", "The model's edit could not be applied safely after "
                                 f"{MAX_EDIT_ATTEMPTS} attempts: {e.message}")
            self._time("validate", t)
            break

        self.result.summary = edit_plan.summary
        self.result.plan_steps = edit_plan.plan
        if not changes:
            yield self._finish(item, "No changes proposed")
            self.result.content = edit_plan.summary or "No changes were needed."
            return

        adds = sum(c.additions for c in changes)
        dels = sum(c.deletions for c in changes)
        yield self._finish(item, f"Edit validated · {len(changes)} file(s) · +{adds} −{dels}",
                           detail=", ".join(f"{c.path} ({c.action})" for c in changes))

        # git awareness: warn when target files already have uncommitted user changes
        t = time.monotonic()
        dirty = git_tools.dirty_paths(repo, [c.path for c in changes if c.action != "create"])
        for c in changes:
            if c.path in dirty:
                c.warnings.append("This file has uncommitted changes of yours. They are kept — "
                                  "the diff applies on top of the current file.")
        self._time("git", t)
        yield self._done("Diff ready for review — nothing has been written yet", "diff")

        self.result.changes = changes
        lines = [edit_plan.summary or "Proposed changes are ready for review."]
        if edit_plan.plan and len(edit_plan.plan) > 1:
            lines.append("\n".join(f"{i}. {s}" for i, s in enumerate(edit_plan.plan, 1)))
        self.result.content = "\n\n".join(lines)

    def _run_command_mode(self) -> Iterator[dict]:
        item = self._start("Reading project layout", "read")
        files = ctx.workspace_files(self.workspace.repo_path)
        tree = ctx.render_file_tree(files, 120)
        yield self._finish(item, f"Read project layout ({len(files)} files)")
        system = planner.build_command_system(tree, self._workspace_line())
        messages = [{"role": "user", "content": self.message}]
        yield from self._llm(messages, system, json_mode=True, max_tokens=1024, stream_text=False,
                             label=f"Asking {self.provider.label} which command to run…",
                             done_label=f"{self.provider.label} proposed a command", effort="low")
        try:
            data = extract_json(self._llm_text, keys=("command",)) if "{" in self._llm_text else {}
        except EditError:
            data = {}
        command = (data.get("command") or "").strip() if isinstance(data.get("command"), str) else ""
        reason = str(data.get("reason") or "").strip()
        if not command:
            yield self._done("No command proposed", "command")
            self.result.content = reason or "I couldn't determine a safe command for that request."
            return
        level, why = assess_command(command)
        if level == "blocked":
            yield self._done(f"Command blocked by safety policy: {why}", "command")
            self.result.content = (f"The command I would run is blocked by CodeSage's safety policy "
                                   f"({why}):\n\n```\n{command}\n```\n\nRun it yourself if you are sure.")
            return
        yield self._done("Command needs your approval — nothing has run yet", "command",
                         detail=f"{command} · risk: {level} ({why})")
        self.result.command = {"command": command, "reason": reason, "danger_level": level,
                               "risk_reason": why}
        self.result.content = reason or f"Proposed command: `{command}`"
