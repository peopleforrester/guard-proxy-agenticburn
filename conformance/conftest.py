# ABOUTME: Fixtures that start a guard-proxy implementation as a subprocess against fake upstreams.
# ABOUTME: GUARD_PROXY_CMD selects the implementation under test; the default is the Python reference.
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from fakes import FakeUpstream, Recorded, Reply, a2a_result, free_port, verdict

HERE = Path(__file__).resolve().parent
DEFAULT_CMD = (
    f"{shlex.quote(sys.executable)} {shlex.quote(str(HERE / 'launch_python.py'))} {{port}}"
)
STARTUP_TIMEOUT = 15.0
GUARD_TOKEN = "conformance-token"

# Only these are inherited from the caller. Everything the proxy reads is set explicitly per test, so a
# variable in the developer's shell (an OTEL_* export, a stray COST_CAP_USD) cannot change a verdict.
_INHERITED = ("PATH", "HOME", "LANG", "TMPDIR", "SYSTEMROOT")


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    raw: bytes

    def json(self) -> Any:
        return json.loads(self.raw)


def http(
    method: str,
    url: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> Response:
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return Response(r.status, {k.lower(): v for k, v in r.headers.items()}, r.read())
    except urllib.error.HTTPError as e:
        return Response(e.code, {k.lower(): v for k, v in e.headers.items()}, e.read())


class Proxy:
    """A running implementation under test, plus helpers for its HTTP surface and its log stream."""

    def __init__(self, env: dict[str, str], log_path: Path) -> None:
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.log_path = log_path
        cmd = os.environ.get("GUARD_PROXY_CMD", DEFAULT_CMD).replace("{port}", str(self.port))
        self._log = open(log_path, "wb")  # noqa: SIM115  (outlives __init__; closed in stop())
        self._proc = subprocess.Popen(
            shlex.split(cmd), env=env, stdout=self._log, stderr=subprocess.STDOUT
        )
        self._wait_ready()

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                break
            try:
                if http("GET", self.url + "/guards", timeout=1).status == 200:
                    return
            except OSError:
                time.sleep(0.05)
        self.stop()
        raise RuntimeError(f"proxy did not become ready; output:\n{self.log_path.read_text()}")

    def get(self, path: str, headers: dict[str, str] | None = None) -> Response:
        return http("GET", self.url + path, headers=headers)

    def post(self, path: str, body: Any, headers: dict[str, str] | None = None) -> Response:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        h = {"Content-Type": "application/json", **(headers or {})}
        return http("POST", self.url + path, body=data, headers=h)

    def chat(self, prompt: Any, session: str | None = None) -> Response:
        body: dict[str, Any] = {"prompt": prompt}
        if session is not None:
            body["session"] = session
        return self.post("/chat", body)

    def a2a(self, text: str, path: str = "/", rpc_id: Any = "req-1") -> Response:
        return self.post(path, a2a_request(text, rpc_id))

    def toggle(self, **flags: str) -> dict[str, bool]:
        query = "&".join(f"{k}={v}" for k, v in flags.items())
        return self.get("/toggle?" + query).json()

    def events(self) -> list[str]:
        """The `event` field of every structured JSON log line emitted so far."""
        self._log.flush()
        out = []
        for line in self.log_path.read_text().splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and rec.get("event"):
                out.append(rec["event"])
        return out

    def stop(self) -> None:
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
        self._log.close()


def a2a_request(text: str, rpc_id: Any = "req-1", extra_parts: list | None = None) -> dict:
    parts = [{"kind": "text", "text": text}, *(extra_parts or [])]
    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "method": "message/send",
        "params": {"message": {"role": "user", "messageId": "m1", "parts": parts}},
    }


def _echo_agent(req: Recorded) -> Reply:
    try:
        rpc_id = req.json().get("id")
    except ValueError:
        rpc_id = None
    return Reply(body=a2a_result(rpc_id=rpc_id))


@pytest.fixture
def agent() -> Iterator[FakeUpstream]:
    up = FakeUpstream(_echo_agent)
    yield up
    up.close()


@pytest.fixture
def guard() -> Iterator[FakeUpstream]:
    def allow(req: Recorded) -> Reply:
        return Reply(body=verdict(True, output=req.path == "/analyze/output"))

    up = FakeUpstream(allow)
    yield up
    up.close()


@pytest.fixture
def start_proxy(
    agent: FakeUpstream, guard: FakeUpstream, tmp_path: Path
) -> Iterator[Callable[..., Proxy]]:
    """Start the implementation with AGENT_URL and LLM_GUARD_URL wired to the fakes plus `env`."""
    running: list[Proxy] = []

    def start(**env: str) -> Proxy:
        full = {k: os.environ[k] for k in _INHERITED if k in os.environ}
        full.update(
            {
                "PYTHONDONTWRITEBYTECODE": "1",
                "AGENT_URL": agent.url,
                "LLM_GUARD_URL": guard.url,
                "LLM_GUARD_TOKEN": GUARD_TOKEN,
                "PROXY_TIMEOUT": "5",
            }
        )
        full.update(env)
        p = Proxy(full, tmp_path / f"proxy-{len(running)}.log")
        running.append(p)
        return p

    yield start
    for p in running:
        p.stop()
