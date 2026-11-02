# ABOUTME: Real local HTTP servers standing in for guard-proxy's upstreams: the A2A agent and LLM Guard.
# ABOUTME: Each records every request it receives and answers from a programmable handler.
from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass
class Recorded:
    """One request an upstream received."""

    method: str
    path: str
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)


@dataclass
class Reply:
    """What an upstream sends back. `delay` simulates a slow upstream."""

    status: int = 200
    body: Any = field(default_factory=dict)
    delay: float = 0.0


Responder = Callable[[Recorded], Reply]


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False


class FakeUpstream:
    """A threaded HTTP server on 127.0.0.1 with an ephemeral port.

    `respond` decides each answer. Swap it per test; every request is appended to `requests`.
    """

    def __init__(self, respond: Responder) -> None:
        self.respond = respond
        self.requests: list[Recorded] = []
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length", "0") or "0")
                rec = Recorded(self.command, self.path, dict(self.headers), self.rfile.read(length))
                with outer._lock:
                    outer.requests.append(rec)
                reply = outer.respond(rec)
                if reply.delay:
                    time.sleep(reply.delay)
                data = (
                    reply.body if isinstance(reply.body, bytes) else json.dumps(reply.body).encode()
                )
                try:
                    self.send_response(reply.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the proxy gave up on a deliberately slow reply

            do_GET = _handle
            do_POST = _handle

            def log_message(self, fmt: str, *args: Any) -> None:
                return

        self._server = _Server(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def calls(self, path: str | None = None) -> list[Recorded]:
        with self._lock:
            return [r for r in self.requests if path is None or r.path == path]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


# The sockets behind unreachable_url(), held until the run ends. A port released after it was picked
# could be bound by another process on a busy host (another run's fake, say), and a test expecting a
# refused connection would talk to that process instead.
_unreachable: list[socket.socket] = []


def unreachable_url() -> str:
    """A loopback URL with nothing listening, so a connection is refused immediately.

    Its port stays bound, never listening, for the rest of the run, so no other process can take it."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    _unreachable.append(s)
    return f"http://127.0.0.1:{s.getsockname()[1]}"


def a2a_result(
    text: str = "Here is your burrito.",
    *,
    prompt_tokens: int | None = 100,
    output_tokens: int | None = 50,
    total_tokens: int | None = None,
    usage_key: str = "kagent_usage_metadata",
    usage_in_status: bool = False,
    where: str = "artifacts",
    rpc_id: Any = "chat",
) -> dict[str, Any]:
    """An A2A message/send result in the shape kagent returns.

    `where` places the reply text in artifacts, agent history, or the status message. Usage lands in
    result.metadata, or in status.message.metadata when `usage_in_status` is set.
    """
    usage: dict[str, Any] | None = None
    if prompt_tokens is not None or output_tokens is not None:
        usage = {"promptTokenCount": prompt_tokens or 0, "candidatesTokenCount": output_tokens or 0}
        if total_tokens is not None:
            usage["totalTokenCount"] = total_tokens
    parts = [{"kind": "text", "text": text}]
    result: dict[str, Any] = {"kind": "task", "status": {"state": "completed", "message": {}}}
    if where == "artifacts":
        result["artifacts"] = [{"parts": parts}]
    elif where == "history":
        result["history"] = [
            {"role": "user", "parts": [{"kind": "text", "text": "the user turn"}]},
            {"role": "agent", "parts": parts},
        ]
    elif where == "status":
        result["status"]["message"] = {"role": "agent", "parts": parts}
    else:
        raise ValueError(where)
    if usage is not None:
        if usage_in_status:
            result["status"]["message"].setdefault("metadata", {})[usage_key] = usage
        else:
            result["metadata"] = {usage_key: usage}
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def verdict(
    is_valid: bool,
    sanitized: str | None = None,
    *,
    output: bool = False,
    scanners: dict[str, float] | None = None,
) -> dict[str, Any]:
    """An LLM Guard API verdict envelope for /analyze/prompt or /analyze/output.

    scanners maps each scanner that ran to its risk score, as the API reports them."""
    body: dict[str, Any] = {"is_valid": is_valid, "scanners": scanners or {}}
    if sanitized is not None:
        body["sanitized_output" if output else "sanitized_prompt"] = sanitized
    return body
