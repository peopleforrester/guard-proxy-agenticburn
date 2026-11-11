# ABOUTME: Conformance for the listen backlog: the kernel holds at least 1,024 pending connections for the
# ABOUTME: proxy's socket, so a burst of phones queues instead of overflowing (guard-proxy-agenticburn#85).
from __future__ import annotations

import ast
import subprocess
from pathlib import Path

PROXY_PY = Path(__file__).resolve().parent.parent / "proxy.py"
# The stdlib's ThreadingHTTPServer listens with 5; a 500-phone burst overflowed that 3,227 times.
MIN_BACKLOG = 1024


def _listen_backlog(port: int) -> int:
    """The backlog the kernel reports for the socket listening on `port`: ss's Send-Q for a LISTEN row."""
    out = subprocess.run(
        ["ss", "-ltnH", f"sport = :{port}"], capture_output=True, text=True, check=True
    ).stdout
    rows = [line.split() for line in out.splitlines() if line.strip()]
    assert len(rows) == 1, f"expected one listening socket on port {port}, ss printed:\n{out}"
    return int(rows[0][2])


def test_listen_backlog_holds_a_burst(start_proxy):
    p = start_proxy()
    somaxconn = int(Path("/proc/sys/net/core/somaxconn").read_text())
    assert somaxconn >= MIN_BACKLOG, (
        f"net.core.somaxconn is {somaxconn}, which caps any backlog below {MIN_BACKLOG}"
    )
    assert _listen_backlog(p.port) >= MIN_BACKLOG


def test_proxy_py_main_serves_with_its_server():
    """proxy.py's __main__ builds the same Server the suite launches, not a stock ThreadingHTTPServer."""
    tree = ast.parse(PROXY_PY.read_text())
    main = next(
        node
        for node in tree.body
        if isinstance(node, ast.If) and ast.unparse(node.test) == "__name__ == '__main__'"
    )
    built = {
        n.func.id for n in ast.walk(main) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert built == {"Server"}
