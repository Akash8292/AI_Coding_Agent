"""
A tiny threaded HTTP server that imitates LLM streaming endpoints, so provider
tests exercise real sockets: blocked reads, slow first tokens, mid-stream
stalls, HTTP errors, and cancellation.

Behaviour is chosen per test via FakeLLMServer.script — a list of steps:
    ("status", 429, '{"error": {"message": "..."}}')   respond with an error
    ("sleep", seconds)                                   stall before/between chunks
    ("sse", "data: {...}")                               send one SSE line
    ("raw", "line")                                      send one raw line (NDJSON)
"""
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence
        pass

    def do_POST(self):
        server: "FakeLLMServer" = self.server.owner  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        server.requests.append({"path": self.path, "body": body.decode("utf-8", "replace"),
                                "headers": dict(self.headers)})
        script = server.next_script()
        started_stream = False
        try:
            for step in script:
                kind = step[0]
                if kind == "status":
                    payload = step[2].encode()
                    self.send_response(step[1])
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if kind == "sleep":
                    if not started_stream and len(step) > 2 and step[2] == "before_headers":
                        time.sleep(step[1])
                        continue
                    if not started_stream:
                        self._start_stream()
                        started_stream = True
                    time.sleep(step[1])
                    continue
                if not started_stream:
                    self._start_stream()
                    started_stream = True
                line = step[1] + "\n\n" if kind == "sse" else step[1] + "\n"
                self._chunk(line.encode())
            if not started_stream:
                self._start_stream()
            self._chunk(b"")  # terminating chunk
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            server.client_disconnects += 1

    def _start_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _chunk(self, data: bytes):
        self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()


class FakeLLMServer:
    def __init__(self):
        self.scripts: list[list] = []
        self.requests: list[dict] = []
        self.client_disconnects = 0
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.daemon_threads = True
        self._httpd.owner = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def next_script(self) -> list:
        if len(self.scripts) > 1:
            return self.scripts.pop(0)
        return self.scripts[0] if self.scripts else []

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._httpd.shutdown()
        self._httpd.server_close()
