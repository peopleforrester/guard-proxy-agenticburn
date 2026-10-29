# Decisions

## 2026-10-25: opentelemetry-sdk 1.45.1 as a dev dependency

The conformance suite needs to read the spans proxy.py emits, to pin the `session.id` and
`witb.guard.decision` attributes on the `POST /chat` root span (guard-proxy-agenticburn#8). In the
workshop the OTel Operator installs the SDK before proxy.py runs; without an SDK every span is a no-op
and there is nothing to assert on. `conformance/launch_python.py` therefore installs the real SDK's
tracer provider when `CONFORMANCE_SPANS` names a file, and exports each finished span to it as one JSON
line through the SDK's own `ConsoleSpanExporter`.

Pinned at `==1.45.1`, the current release on PyPI on 2026-10-25 (published 2026-10-06). It is a dev
dependency only: proxy.py stays stdlib-only and still runs with no OTel present. Having
`opentelemetry-api` installed in the dev environment also means the whole suite now runs proxy.py with
its OTel code path active (as no-op spans when no provider is installed), which is the path the
workshop runs.

## 2026-10-29: Lifecycle state tracking adopted

`PROJECT_STATE.md` added on the lifecycle schema, seeded at 3.3 because the last unit of work
(`000f57e`) was already on `main` with green CI. The open agent-task issues are its current plan.
