# ABOUTME: Tests for the suite's own harness: the implementation is reached at the port it bound itself,
# ABOUTME: and an unreachable upstream stays refused, so no other server on a busy host answers for either.
from __future__ import annotations

import json
import re
import shlex
import socket
import sys
from urllib.parse import urlparse

import pytest

import conftest
from fakes import unreachable_url


def test_the_proxy_is_reached_at_the_port_it_announced(start_proxy):
    """The harness hands the implementation port 0 and reads the bound port from its own output.

    A port the harness picked and released first could be taken by another process before the
    implementation bound it, and that process would then answer the readiness probe and the test."""
    p = start_proxy()
    p.events()  # flushes the capture file
    announced = []
    for line in p.log_path.read_text().splitlines():
        if line.startswith("{"):
            m = re.match(r"listening on 127\.0\.0\.1:(\d+)\b", json.loads(line).get("message", ""))
            if m:
                announced.append(int(m.group(1)))
    assert announced == [p.port]
    assert p.url == f"http://127.0.0.1:{p.port}"
    assert set(p.get("/guards").json()) >= {"input_blocklist", "input_classifier", "output"}


def test_a_proxy_that_never_announces_its_port_fails_loudly(start_proxy, monkeypatch):
    monkeypatch.setattr(conftest, "STARTUP_TIMEOUT", 1.0)
    monkeypatch.setenv(
        "GUARD_PROXY_CMD", f"{shlex.quote(sys.executable)} -c 'import time; time.sleep(30)'"
    )
    with pytest.raises(RuntimeError, match="announced no listening port"):
        start_proxy()


def test_an_unreachable_url_refuses_and_no_one_else_can_take_its_port():
    port = urlparse(unreachable_url()).port
    with socket.socket() as s, pytest.raises(ConnectionRefusedError):
        s.connect(("127.0.0.1", port))
    with socket.socket() as s, pytest.raises(OSError):
        s.bind(("127.0.0.1", port))
