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

# In the workshop the OTel Operator installs the SDK before proxy.py runs, exporting over OTLP to the
# endpoint in OTEL_EXPORTER_OTLP_ENDPOINT. With that set, this installs the same: the SDK's tracer and
# meter providers with the OTLP/HTTP exporters, which read the rest of their settings from OTEL_*.
if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
    from opentelemetry import metrics, trace
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    _provider = TracerProvider()
    _provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(_provider)
    metrics.set_meter_provider(
        MeterProvider(metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter())])
    )
    # The Operator's auto-instrumentation also wraps urllib, giving each outbound request its own CLIENT
    # span and traceparent. A test that needs those, as the tasks/get polls' does, turns it on here; the
    # rest leave it off, so the traceparent the agent sees names proxy.py's own agent.forward span.
    if os.environ.get("CONFORMANCE_URLLIB_INSTRUMENTATION") == "on":
        from opentelemetry.instrumentation.urllib import URLLibInstrumentor

        URLLibInstrumentor().instrument()

import proxy  # noqa: E402  (environment is read at import, so the path must be set first)


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), proxy.Handler)
    # The suite passes port 0 and reads the bound port back from this line, as the Go build logs it.
    host, port = server.server_address[:2]
    proxy.log.info("listening on %s:%d, agent %s", host, port, proxy.AGENT_URL)
    server.serve_forever()


if __name__ == "__main__":
    main()
