"""
LLM Provider abstraction — base class and shared types.

Every provider implements `stream()` as a generator of text fragments and
reports problems by RAISING `ProviderError` (never by yielding error text).
After a stream finishes, `provider.last_usage` holds token usage for it.

Shared infrastructure lives here so providers stay small:
  `LLMProvider._stream_http()` performs the HTTP request on a reader thread
  and consumes it through a queue, so cancellation and the first-token /
  idle / total deadlines take effect within ~100 ms on every OS, even while
  the socket read is blocked. Retries happen only before any output and only
  for failures a retry can fix (rate limit, 5xx, network).
"""
import logging
import queue
import socket
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Iterator, Optional

import requests

logger = logging.getLogger(__name__)

# Defaults — overridable per provider instance via `timeouts=`
DEFAULT_CONNECT_TIMEOUT = 10      # TCP/TLS connect
DEFAULT_FIRST_TOKEN_TIMEOUT = 90  # request sent → first byte of output
DEFAULT_IDLE_TIMEOUT = 60         # max silence between chunks once streaming
DEFAULT_TOTAL_TIMEOUT = 300       # whole request, hard ceiling
DEFAULT_MAX_ATTEMPTS = 2


@dataclass
class ModelInfo:
    provider: str
    model: str
    label: str
    context_window: int
    max_output_tokens: int
    is_local: bool = False
    available: bool = True
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "label": self.label,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "is_local": self.is_local,
            "available": self.available,
            "error": self.error,
        }


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    estimated: bool = True  # False when the provider reported real counts

    def to_dict(self) -> dict:
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "estimated": self.estimated}


@dataclass
class GenerationResult:
    content: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    provider: str = ""
    finish_reason: str = "stop"


@dataclass
class Timeouts:
    connect: float = DEFAULT_CONNECT_TIMEOUT
    first_token: float = DEFAULT_FIRST_TOKEN_TIMEOUT
    idle: float = DEFAULT_IDLE_TIMEOUT
    total: float = DEFAULT_TOTAL_TIMEOUT
    max_attempts: int = DEFAULT_MAX_ATTEMPTS


class ProviderNotAvailableError(Exception):
    """Raised when a provider is not configured or unavailable."""
    pass


class ProviderError(Exception):
    """
    A failed LLM request. `kind` is one of:
      auth, quota, rate_limit, timeout, network, bad_request, server,
      not_found, cancelled, empty, unavailable
    `retry_after` (seconds) is set when the provider said when to retry.
    """

    RETRYABLE_KINDS = {"rate_limit", "server", "network"}

    def __init__(self, provider: str, kind: str, message: str,
                 status: Optional[int] = None, model: str = "",
                 retry_after: Optional[float] = None):
        super().__init__(message)
        self.provider = provider
        self.kind = kind
        self.message = message
        self.status = status
        self.model = model
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.kind in self.RETRYABLE_KINDS

    def to_dict(self) -> dict:
        return {"provider": self.provider, "kind": self.kind, "message": self.message,
                "status": self.status, "model": self.model, "retry_after": self.retry_after}


class ProviderCancelled(ProviderError):
    def __init__(self, provider: str, model: str = ""):
        super().__init__(provider, "cancelled", "Request cancelled", model=model)


def _parse_error_body(body_text: str) -> dict:
    """{message, code, type, quota_ids, retry_after} from a JSON error body (any provider)."""
    import json
    out = {"message": "", "code": "", "type": "", "quota_ids": [], "retry_after": None}
    try:
        data = json.loads(body_text or "")
    except Exception:
        return out
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        return out
    err = data.get("error", data)
    if isinstance(err, str):
        out["message"] = err[:300]
        return out
    if not isinstance(err, dict):
        return out
    out["message"] = str(err.get("message") or err.get("type") or "")[:300]
    out["code"] = str(err.get("code") or "")
    out["type"] = str(err.get("type") or err.get("status") or "")
    for det in err.get("details") or []:            # Google RPC error details
        if not isinstance(det, dict):
            continue
        t = det.get("@type", "")
        if t.endswith("QuotaFailure"):
            out["quota_ids"] += [v.get("quotaId", "") for v in det.get("violations") or []]
        elif t.endswith("RetryInfo"):
            delay = str(det.get("retryDelay") or "").rstrip("s")
            try:
                out["retry_after"] = float(delay)
            except ValueError:
                pass
    return out


def classify_http_error(provider: str, label: str, status: int, body_text: str,
                        model: str = "", retry_after_header: Optional[str] = None) -> ProviderError:
    """Map an HTTP error response to a typed ProviderError with a useful message."""
    info = _parse_error_body(body_text)
    detail = info["message"] or (body_text or "").strip()[:200] or f"HTTP {status}"
    retry_after = info["retry_after"]
    if retry_after is None and retry_after_header:
        try:
            retry_after = float(retry_after_header)
        except ValueError:
            pass

    def err(kind, msg):
        return ProviderError(provider, kind, msg, status, model, retry_after)

    if status in (401, 403):
        return err("auth", f"{label} rejected the API key ({status}): {detail}")
    if status == 429:
        code = (info["code"] + " " + info["type"]).lower()
        quota_ids = " ".join(info["quota_ids"]).lower()
        daily = "perday" in quota_ids or "per_day" in quota_ids
        per_minute = "perminute" in quota_ids or "per_minute" in quota_ids
        # Structured quota ids (Google) and error codes (OpenAI) beat message text
        billing = "insufficient_quota" in code or (
            not info["quota_ids"] and any(k in detail.lower() for k in ("no credits", "credit balance")))
        if per_minute:
            wait = f" Retry in {retry_after:.0f}s." if retry_after else ""
            return err("rate_limit", f"{label} per-minute rate limit reached for {model}.{wait}")
        if billing:
            return err("quota", f"{label} account has no remaining credits/quota: {detail}")
        if daily:
            return err("quota", f"{label} daily quota exhausted for {model}. It resets tomorrow — "
                                f"switch models or providers meanwhile.")
        wait = f" Retry in {retry_after:.0f}s." if retry_after else ""
        return err("rate_limit", f"{label} rate limit reached for {model}.{wait} ({detail[:160]})")
    if status == 404:
        return err("not_found", f"{label} model or endpoint not found ({model}): {detail}")
    if status in (400, 413, 422):
        return err("bad_request", f"{label} rejected the request ({status}): {detail}")
    if status >= 500:
        return err("server", f"{label} server error {status}: {detail}")
    return err("bad_request", f"{label} error {status}: {detail}")


def force_close(response: Optional[requests.Response]) -> None:
    """
    Best-effort abort of a streaming response from another thread. On Linux
    this unblocks a pending read at once; on Windows the reader thread exits
    at its next socket timeout. Callers never wait on it either way.
    """
    if response is None:
        return
    try:
        sock = _find_socket(response)
        if sock is not None:
            sock.shutdown(socket.SHUT_RDWR)
    except Exception:
        pass
    try:
        response.close()
    except Exception:
        pass


def _find_socket(response: requests.Response):
    raw = getattr(response, "raw", None)
    conn = getattr(raw, "_connection", None)
    sock = getattr(conn, "sock", None)
    if sock is not None:
        return sock
    fp = getattr(raw, "_fp", None)                       # http.client.HTTPResponse
    fp2 = getattr(fp, "fp", None)                        # BufferedReader
    rawio = getattr(fp2, "raw", None)                    # SocketIO
    return getattr(rawio, "_sock", None)


class _HTTPReader:
    """
    Performs one streaming POST on a daemon thread and hands results to the
    caller through a queue:
        ("status", code, body_text, headers)   non-200 response
        ("line", str)                          one decoded response line
        ("exc", exception)                     transport failure
        ("end",)                               stream finished normally
    The caller polls the queue with a short timeout, so cancellation and
    deadlines are enforced without depending on the blocked read returning.
    """

    def __init__(self, url: str, headers: dict, payload: dict, params: Optional[dict],
                 timeout: tuple):
        self.q: "queue.Queue[tuple]" = queue.Queue()
        self.abandoned = threading.Event()
        self._response: Optional[requests.Response] = None
        self._args = (url, headers, payload, params, timeout)
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        url, headers, payload, params, timeout = self._args
        try:
            resp = requests.post(url, headers=headers, params=params, json=payload,
                                 stream=True, timeout=timeout)
            self._response = resp
            if self.abandoned.is_set():
                return
            if resp.status_code != 200:
                try:
                    body = resp.text
                except Exception:
                    body = ""
                self.q.put(("status", resp.status_code, body, dict(resp.headers)))
                return
            for raw_line in resp.iter_lines(decode_unicode=False):
                if self.abandoned.is_set():
                    return
                self.q.put(("line", raw_line.decode("utf-8", errors="replace") if raw_line else ""))
            self.q.put(("end",))
        except BaseException as e:  # delivered to the caller's thread
            if not self.abandoned.is_set():
                self.q.put(("exc", e))
        finally:
            if self._response is not None:
                try:
                    self._response.close()
                except Exception:
                    pass

    def abandon(self) -> None:
        self.abandoned.set()
        resp = self._response
        if resp is not None:
            # close() can block on the reader's buffer lock until its recv
            # returns (Windows), so never do it on the caller's thread.
            threading.Thread(target=force_close, args=(resp,), daemon=True).start()


class LLMProvider(ABC):
    """Base class for all LLM providers."""

    label: str = "LLM"

    def __init__(self, timeouts: Optional[Timeouts] = None):
        self.timeouts = timeouts or Timeouts()
        self.last_usage: Usage = Usage()
        self.last_ttft: Optional[float] = None  # seconds to first output token
        # optional callback(err, delay_seconds, attempt) so callers can show retries
        self.on_retry: Optional[Callable[[ProviderError, float, int], None]] = None

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Short identifier e.g. 'openai', 'anthropic'."""
        ...

    @property
    def model(self) -> str:
        return getattr(self, "_model", "")

    @abstractmethod
    def stream(self, messages: list[dict], system_prompt: str = "",
               cancel_event: Optional[threading.Event] = None,
               json_mode: bool = False, max_tokens: Optional[int] = None,
               **kwargs) -> Iterator[str]:
        """
        Stream text chunks. Raises ProviderError on failure (including
        ProviderCancelled when cancel_event is set). Sets self.last_usage.

        messages: list of {role: 'user'|'assistant', content: str}
        json_mode: ask the provider for a JSON object response where supported
        """
        ...

    @abstractmethod
    def get_model_info(self) -> ModelInfo:
        ...

    def count_tokens(self, text: str) -> int:
        """Rough token estimate (4 chars ≈ 1 token). Override for accuracy."""
        return max(1, len(text) // 4)

    def estimate_usage(self, messages: list[dict], system_prompt: str, output: str) -> Usage:
        input_text = system_prompt + " ".join(m.get("content", "") for m in messages)
        return Usage(self.count_tokens(input_text), self.count_tokens(output) if output else 0, True)

    def generate(self, messages: list[dict], system_prompt: str = "", **kwargs) -> GenerationResult:
        """Blocking generation — collects the stream into one result."""
        full = "".join(self.stream(messages, system_prompt=system_prompt, **kwargs))
        u = self.last_usage
        return GenerationResult(content=full, input_tokens=u.input_tokens,
                                output_tokens=u.output_tokens, model=self.model,
                                provider=self.provider_name)

    # ── Shared streaming machinery ───────────────────────────────────────────

    def _stream_http(self, url: str, *, headers: dict, payload: dict,
                     params: Optional[dict], cancel_event: Optional[threading.Event],
                     parse_line: Callable[[str], Iterator[str]],
                     line_prefix: Optional[str] = "data: ") -> Iterator[str]:
        """
        POST `payload` and stream the response through `parse_line`, which
        yields text pieces for each line (after stripping `line_prefix`).

        Enforced here, identically for every provider:
          - cancellation (checked every 100 ms, even while the socket is blocked)
          - first-token, idle and total deadlines
          - retries only before any output and only for retryable failures
          - a typed ProviderError for every failure mode
        """
        t = self.timeouts
        label = self.label
        started = time.monotonic()
        self.last_ttft = None
        attempt = 0
        yielded_any = False
        # Per-read socket timeout: a backstop that bounds orphaned reader threads
        read_timeout = max(t.first_token, t.idle) + 5

        while True:
            attempt += 1
            self._check_cancel(cancel_event)
            reader = _HTTPReader(url, headers, payload, params, (t.connect, read_timeout))
            last_activity = time.monotonic()
            retry = False
            try:
                while True:
                    try:
                        item = reader.q.get(timeout=0.1)
                    except queue.Empty:
                        now = time.monotonic()
                        self._check_cancel(cancel_event)
                        if now - started > t.total:
                            raise self._timeout_error("total")
                        if not yielded_any and now - started > t.first_token:
                            raise self._timeout_error("first_token")
                        if yielded_any and now - last_activity > t.idle:
                            raise self._timeout_error("idle")
                        continue

                    kind = item[0]
                    if kind == "status":
                        _, code, body, resp_headers = item
                        err = classify_http_error(self.provider_name, label, code, body, self.model,
                                                  resp_headers.get("retry-after") or resp_headers.get("Retry-After"))
                        if self._should_retry(err, attempt, yielded_any, cancel_event, err.retry_after):
                            retry = True
                            break
                        raise err
                    if kind == "exc":
                        err = self._transport_error(item[1])
                        if self._should_retry(err, attempt, yielded_any, cancel_event):
                            retry = True
                            break
                        raise err
                    if kind == "end":
                        return

                    # kind == "line"
                    self._check_cancel(cancel_event)
                    last_activity = time.monotonic()
                    line = item[1]
                    if not line:
                        continue
                    if line_prefix:
                        if not line.startswith(line_prefix):
                            continue
                        line = line[len(line_prefix):]
                    for piece in parse_line(line):
                        if piece:
                            if not yielded_any:
                                self.last_ttft = time.monotonic() - started
                                yielded_any = True
                            yield piece
            finally:
                # Normal end, error, cancel, or the consumer stopped iterating
                reader.abandon()
            if not retry:
                return

    def _check_cancel(self, cancel_event: Optional[threading.Event]) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise ProviderCancelled(self.provider_name, self.model)

    def _transport_error(self, exc: BaseException) -> ProviderError:
        label, t = self.label, self.timeouts
        if isinstance(exc, requests.exceptions.ConnectTimeout):
            return ProviderError(self.provider_name, "network",
                                 f"Could not connect to {label} within {t.connect:.0f}s.", model=self.model)
        if isinstance(exc, requests.exceptions.ReadTimeout):
            return ProviderError(self.provider_name, "timeout",
                                 f"{label} stopped responding (socket read timeout).", model=self.model)
        if isinstance(exc, requests.exceptions.ConnectionError):
            return ProviderError(self.provider_name, "network",
                                 f"Network error contacting {label}: could not connect.", model=self.model)
        return ProviderError(self.provider_name, "network",
                             f"{label} stream failed: {type(exc).__name__}.", model=self.model)

    def _timeout_error(self, which: str) -> ProviderError:
        t, label = self.timeouts, self.label
        if which == "first_token":
            msg = (f"{label} request timed out after {t.first_token:.0f} seconds "
                   f"without producing output ({self.model}).")
        elif which == "idle":
            msg = f"{label} stream stalled for {t.idle:.0f} seconds ({self.model})."
        else:
            msg = f"{label} request exceeded the {t.total:.0f}-second limit ({self.model})."
        return ProviderError(self.provider_name, "timeout", msg, model=self.model)

    MAX_RETRY_WAIT = 30.0  # never silently wait longer than this for a retry

    def _should_retry(self, err: ProviderError, attempt: int, yielded_any: bool,
                      cancel_event: Optional[threading.Event], retry_after=None) -> bool:
        if yielded_any or not err.retryable or attempt >= self.timeouts.max_attempts:
            return False
        delay = 1.5 * attempt
        if retry_after:
            try:
                delay = float(retry_after) + 0.5
            except (TypeError, ValueError):
                pass
        if delay > self.MAX_RETRY_WAIT:
            return False  # surface the error with its retry hint instead of hanging
        if self.on_retry:
            try:
                self.on_retry(err, delay, attempt)
            except Exception:
                pass
        logger.warning("[llm] %s attempt %d failed (%s): %s — retrying in %.1fs",
                       self.provider_name, attempt, err.kind, err.message, delay)
        if cancel_event is not None:
            if cancel_event.wait(delay):
                raise ProviderCancelled(self.provider_name, self.model)
        else:
            time.sleep(delay)
        return True
