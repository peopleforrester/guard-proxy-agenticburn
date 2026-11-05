# ABOUTME: A fake OTLP/HTTP collector for the span and metric tests: it decodes what an implementation
# ABOUTME: exports over the standard OTEL_EXPORTER_OTLP_ENDPOINT, so neither needs a test-only exporter.
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue
from opentelemetry.proto.trace.v1.trace_pb2 import Span, Status

from fakes import FakeUpstream, Recorded, Reply

WAIT = 5.0

# The environment that points an implementation at a collector. The short delays make a finished span
# or a gauge reading reach it within the tests' wait instead of after the SDK defaults of 5 and 60 s.
def otlp_env(url: str) -> dict[str, str]:
    return {
        "OTEL_EXPORTER_OTLP_ENDPOINT": url,
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_SERVICE_NAME": "guard-proxy",
        "OTEL_BSP_SCHEDULE_DELAY": "50",
        "OTEL_METRIC_EXPORT_INTERVAL": "200",
    }


def _value(v: AnyValue) -> Any:
    kind = v.WhichOneof("value")
    if kind == "array_value":
        return [_value(x) for x in v.array_value.values]
    return getattr(v, kind) if kind else None


def _attributes(kvs) -> dict[str, Any]:
    return {kv.key: _value(kv.value) for kv in kvs}


class Collector:
    """Receives OTLP/HTTP traces and metrics and answers each export with an empty success."""

    def __init__(self) -> None:
        # An empty body is a valid, empty ExportTraceServiceResponse in protobuf.
        self._up = FakeUpstream(lambda req: Reply(body=b""))
        self.url = self._up.url
        self.env = otlp_env(self.url)

    def close(self) -> None:
        self._up.close()

    def _requests(self, path: str) -> list[Recorded]:
        return self._up.calls(path)

    def spans(self) -> list[dict[str, Any]]:
        """Every span received so far, as name, kind, ids, attributes, status and its description."""
        out = []
        for rec in self._requests("/v1/traces"):
            req = ExportTraceServiceRequest()
            req.ParseFromString(rec.body)
            for rs in req.resource_spans:
                resource = _attributes(rs.resource.attributes)
                for ss in rs.scope_spans:
                    for s in ss.spans:
                        out.append(
                            {
                                "name": s.name,
                                "kind": Span.SpanKind.Name(s.kind).removeprefix("SPAN_KIND_"),
                                "trace_id": s.trace_id.hex(),
                                "span_id": s.span_id.hex(),
                                "parent_span_id": s.parent_span_id.hex(),
                                "attributes": _attributes(s.attributes),
                                "status": Status.StatusCode.Name(s.status.code).removeprefix(
                                    "STATUS_CODE_"
                                ),
                                "status_message": s.status.message,
                                "resource": resource,
                            }
                        )
        return out

    def wait_for(self, check: Callable[[list[dict[str, Any]]], Any], what: str) -> Any:
        """Poll the received spans until `check` returns something truthy, and return it."""
        deadline = time.monotonic() + WAIT
        while True:
            spans = self.spans()
            got = check(spans)
            if got:
                return got
            if time.monotonic() > deadline:
                raise AssertionError(f"{what} within {WAIT}s; got {[s['name'] for s in spans]}")
            time.sleep(0.05)

    def gauge(self, name: str) -> list[tuple[float, dict[str, Any]]]:
        """The data points of the latest export of gauge `name`, as (value, attributes)."""
        latest: list[tuple[float, dict[str, Any]]] = []
        for rec in self._requests("/v1/metrics"):
            req = ExportMetricsServiceRequest()
            req.ParseFromString(rec.body)
            for rm in req.resource_metrics:
                for sm in rm.scope_metrics:
                    for m in sm.metrics:
                        if m.name == name and m.HasField("gauge"):
                            latest = [
                                (
                                    p.as_double if p.HasField("as_double") else float(p.as_int),
                                    _attributes(p.attributes),
                                )
                                for p in m.gauge.data_points
                            ]
        return latest
