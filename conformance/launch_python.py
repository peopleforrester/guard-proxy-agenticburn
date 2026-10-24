# ABOUTME: Runs the reference proxy.py on a chosen loopback port, for the conformance suite.
# ABOUTME: proxy.py hardcodes 0.0.0.0:8080 in __main__; this serves the same Handler elsewhere.
from __future__ import annotations

import os
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path

# An optional second argument points at another directory holding a proxy.py, which is how the
# mutation check runs the suite against deliberately broken copies.
_SOURCE = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SOURCE))

# In the workshop the OTel Operator installs the SDK's tracer provider before proxy.py runs. With
# CONFORMANCE_SPANS set, this does the same and writes each finished span as one JSON line to that file.
_SPANS = os.environ.get("CONFORMANCE_SPANS")
if _SPANS:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

    _provider = TracerProvider()
    _provider.add_span_processor(
        SimpleSpanProcessor(
            ConsoleSpanExporter(
                out=open(_SPANS, "a"),  # noqa: SIM115  (lives as long as the process)
                formatter=lambda span: span.to_json(indent=None) + "\n",
            )
        )
    )
    trace.set_tracer_provider(_provider)

import proxy  # noqa: E402  (environment is read at import, so the path must be set first)


def main() -> None:
    port = int(sys.argv[1])
    ThreadingHTTPServer(("127.0.0.1", port), proxy.Handler).serve_forever()


if __name__ == "__main__":
    main()
