"""
Provider behaviour over real sockets: streaming, usage, error taxonomy,
retry policy, deadlines, and cancellation of blocked reads.
"""
import json
import threading
import time

import pytest

from app.llm.base import ProviderCancelled, ProviderError, Timeouts
from app.llm.openai_provider import OpenAIProvider
from app.llm.gemini_provider import GeminiProvider
from app.llm.anthropic_provider import AnthropicProvider
from app.llm.ollama_provider import OllamaProvider
from app.llm.factory import is_real_key
from tests.fake_llm_server import FakeLLMServer

KEY = "test-key-0123456789abcdef"
FAST = Timeouts(connect=2, first_token=2, idle=2, total=6, max_attempts=2)


def oai_chunk(text):
    return ("sse", "data: " + json.dumps({"choices": [{"delta": {"content": text}}]}))


def gem_chunk(text, usage=None):
    d = {"candidates": [{"content": {"parts": [{"text": text}]}}]}
    if usage:
        d["usageMetadata"] = usage
    return ("sse", "data: " + json.dumps(d))


def test_openai_streams_and_reports_real_usage():
    with FakeLLMServer() as srv:
        srv.scripts = [[oai_chunk("Hel"), oai_chunk("lo"),
                        ("sse", "data: " + json.dumps({"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 2}})),
                        ("sse", "data: [DONE]")]]
        p = OpenAIProvider(KEY, model="gpt-4o", base_url=srv.url, timeouts=FAST)
        out = "".join(p.stream([{"role": "user", "content": "hi"}], system_prompt="sys", json_mode=True))
        assert out == "Hello"
        assert (p.last_usage.input_tokens, p.last_usage.output_tokens, p.last_usage.estimated) == (11, 2, False)
        body = json.loads(srv.requests[0]["body"])
        assert body["response_format"] == {"type": "json_object"}
        assert body["messages"][0] == {"role": "system", "content": "sys"}
        assert p.last_ttft is not None


def test_quota_error_is_not_retried_and_raises_typed_error():
    with FakeLLMServer() as srv:
        srv.scripts = [[("status", 429, json.dumps({"error": {"message": "You have no credits remaining"}}))]]
        p = OpenAIProvider(KEY, base_url=srv.url, timeouts=FAST)
        with pytest.raises(ProviderError) as ei:
            list(p.stream([{"role": "user", "content": "hi"}]))
        assert ei.value.kind == "quota"
        assert "no credits" in ei.value.message
        assert len(srv.requests) == 1  # no pointless retry


def test_auth_error_is_typed():
    with FakeLLMServer() as srv:
        srv.scripts = [[("status", 401, json.dumps({"error": {"message": "API key is invalid."}}))]]
        p = AnthropicProvider(KEY, base_url=srv.url, timeouts=FAST)
        with pytest.raises(ProviderError) as ei:
            list(p.stream([{"role": "user", "content": "hi"}]))
        assert ei.value.kind == "auth"
        assert ei.value.retryable is False


def test_server_error_is_retried_once_then_succeeds():
    with FakeLLMServer() as srv:
        srv.scripts = [[("status", 503, '{"error":{"message":"overloaded"}}')],
                       [oai_chunk("ok"), ("sse", "data: [DONE]")]]
        p = OpenAIProvider(KEY, base_url=srv.url, timeouts=FAST)
        assert "".join(p.stream([{"role": "user", "content": "hi"}])) == "ok"
        assert len(srv.requests) == 2


def test_first_token_timeout_fires_on_silent_stream():
    with FakeLLMServer() as srv:
        srv.scripts = [[("sleep", 10), gem_chunk("late")]]
        p = GeminiProvider(KEY, base_url=srv.url,
                           timeouts=Timeouts(connect=2, first_token=1, idle=5, total=10, max_attempts=1))
        t0 = time.monotonic()
        with pytest.raises(ProviderError) as ei:
            list(p.stream([{"role": "user", "content": "hi"}]))
        elapsed = time.monotonic() - t0
        assert ei.value.kind == "timeout"
        assert "timed out after 1 seconds" in ei.value.message
        assert elapsed < 4, f"timeout took {elapsed:.1f}s"


def test_idle_timeout_fires_mid_stream():
    with FakeLLMServer() as srv:
        srv.scripts = [[gem_chunk("partial"), ("sleep", 10), gem_chunk("never")]]
        p = GeminiProvider(KEY, base_url=srv.url,
                           timeouts=Timeouts(connect=2, first_token=3, idle=1, total=10, max_attempts=1))
        got = []
        with pytest.raises(ProviderError) as ei:
            for piece in p.stream([{"role": "user", "content": "hi"}]):
                got.append(piece)
        assert got == ["partial"]
        assert ei.value.kind == "timeout" and "stalled" in ei.value.message


def test_cancel_aborts_blocked_read_immediately():
    with FakeLLMServer() as srv:
        srv.scripts = [[gem_chunk("first"), ("sleep", 20), gem_chunk("never")]]
        p = GeminiProvider(KEY, base_url=srv.url,
                           timeouts=Timeouts(connect=2, first_token=30, idle=30, total=60, max_attempts=1))
        cancel = threading.Event()
        got = []

        def cancel_soon():
            time.sleep(0.5)
            cancel.set()

        threading.Thread(target=cancel_soon, daemon=True).start()
        t0 = time.monotonic()
        with pytest.raises(ProviderCancelled):
            for piece in p.stream([{"role": "user", "content": "hi"}], cancel_event=cancel):
                got.append(piece)
        elapsed = time.monotonic() - t0
        assert got == ["first"]
        assert elapsed < 2.0, f"cancel took {elapsed:.1f}s — read was not aborted"


def test_cancel_while_waiting_for_headers():
    with FakeLLMServer() as srv:
        srv.scripts = [[("sleep", 20, "before_headers"), oai_chunk("never")]]
        p = OpenAIProvider(KEY, base_url=srv.url,
                           timeouts=Timeouts(connect=2, first_token=30, idle=30, total=60, max_attempts=1))
        cancel = threading.Event()
        threading.Timer(0.4, cancel.set).start()
        t0 = time.monotonic()
        with pytest.raises(ProviderCancelled):
            list(p.stream([{"role": "user", "content": "hi"}], cancel_event=cancel))
        assert time.monotonic() - t0 < 1.5


def test_gemini_usage_includes_thoughts_and_skips_thought_parts():
    with FakeLLMServer() as srv:
        thought = ("sse", "data: " + json.dumps(
            {"candidates": [{"content": {"parts": [{"text": "thinking...", "thought": True}]}}]}))
        srv.scripts = [[thought, gem_chunk("answer", {"promptTokenCount": 7, "candidatesTokenCount": 3,
                                                       "thoughtsTokenCount": 5})]]
        p = GeminiProvider(KEY, base_url=srv.url, timeouts=FAST)
        assert "".join(p.stream([{"role": "user", "content": "q"}], json_mode=True)) == "answer"
        assert (p.last_usage.input_tokens, p.last_usage.output_tokens) == (7, 8)
        body = json.loads(srv.requests[0]["body"])
        assert body["generationConfig"]["responseMimeType"] == "application/json"


def test_gemini_empty_output_due_to_safety_is_an_error():
    with FakeLLMServer() as srv:
        srv.scripts = [[("sse", "data: " + json.dumps({"candidates": [{"finishReason": "SAFETY"}]}))]]
        p = GeminiProvider(KEY, base_url=srv.url, timeouts=FAST)
        with pytest.raises(ProviderError) as ei:
            list(p.stream([{"role": "user", "content": "q"}]))
        assert "SAFETY" in ei.value.message


def test_anthropic_stream_usage_and_error_event():
    with FakeLLMServer() as srv:
        srv.scripts = [[
            ("sse", "data: " + json.dumps({"type": "message_start", "message": {"usage": {"input_tokens": 20, "output_tokens": 1}}})),
            ("sse", "data: " + json.dumps({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi"}})),
            ("sse", "data: " + json.dumps({"type": "message_delta", "usage": {"output_tokens": 4}})),
            ("sse", "data: " + json.dumps({"type": "message_stop"})),
        ]]
        p = AnthropicProvider(KEY, base_url=srv.url, timeouts=FAST)
        assert "".join(p.stream([{"role": "user", "content": "q"}])) == "Hi"
        assert (p.last_usage.input_tokens, p.last_usage.output_tokens) == (20, 4)


def test_ollama_unreachable_is_fast_and_typed():
    # Nothing listens on this port
    p = OllamaProvider(base_url="http://127.0.0.1:9", timeouts=Timeouts(connect=1, first_token=2,
                                                                        idle=2, total=3, max_attempts=1))
    t0 = time.monotonic()
    assert p.is_available() is False
    with pytest.raises(ProviderError) as ei:
        list(p.stream([{"role": "user", "content": "q"}]))
    assert ei.value.kind == "unavailable"
    assert time.monotonic() - t0 < 8


def test_placeholder_keys_are_not_real():
    assert not is_real_key("sk-...")
    assert not is_real_key("AIzaSy...")
    assert not is_real_key("")
    assert not is_real_key(None)
    assert not is_real_key("your-openai-key-goes-here")
    assert is_real_key("sk-proj-abcdefghijklmnopqrstuvwxyz0123")


def test_rate_limit_with_retry_delay_waits_then_succeeds_and_reports():
    body = json.dumps({"error": {"code": 429, "message": "You exceeded your current quota",
                                 "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                                              "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]},
                                             {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "1s"}]}})
    with FakeLLMServer() as srv:
        srv.scripts = [[("status", 429, body)], [gem_chunk("ok")]]
        p = GeminiProvider(KEY, base_url=srv.url, timeouts=FAST)
        seen = []
        p.on_retry = lambda err, delay, attempt: seen.append((err.kind, round(delay, 1), attempt))
        assert "".join(p.stream([{"role": "user", "content": "q"}])) == "ok"
        assert seen == [("rate_limit", 1.5, 1)]


def test_long_retry_delay_is_surfaced_not_waited():
    body = json.dumps({"error": {"code": 429, "message": "quota",
                                 "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                                              "violations": [{"quotaId": "GenerateRequestsPerMinute"}]},
                                             {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "55s"}]}})
    with FakeLLMServer() as srv:
        srv.scripts = [[("status", 429, body)]]
        p = GeminiProvider(KEY, base_url=srv.url, timeouts=FAST)
        t0 = time.monotonic()
        with pytest.raises(ProviderError) as ei:
            list(p.stream([{"role": "user", "content": "q"}]))
        assert ei.value.kind == "rate_limit" and "Retry in 55s" in ei.value.message
        assert time.monotonic() - t0 < 2


def test_gemini_effort_hint_maps_to_thinking_level():
    p = GeminiProvider(KEY, model="gemini-3.6-flash")
    body = p.build_payload([{"role": "user", "content": "x"}], "", True, None, effort="low")
    assert body["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}
    body = p.build_payload([{"role": "user", "content": "x"}], "", True, None)
    assert "thinkingConfig" not in body["generationConfig"]
