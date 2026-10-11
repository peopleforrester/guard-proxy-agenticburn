# ABOUTME: Runs the reference proxy.py on a chosen loopback port, for the conformance suite.
# ABOUTME: proxy.py hardcodes 0.0.0.0:8080 in __main__; this serves the same Handler elsewhere.
from __future__ import annotations

import sys
from http.server import ThreadingHTTPServer
from pathlib import Path

# An optional second argument points at another directory holding a proxy.py, which is how the
# mutation check runs the suite against deliberately broken copies.
_SOURCE = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SOURCE))

import proxy  # noqa: E402  (environment is read at import, so the path must be set first)


def main() -> None:
    port = int(sys.argv[1])
    ThreadingHTTPServer(("127.0.0.1", port), proxy.Handler).serve_forever()


if __name__ == "__main__":
    main()
