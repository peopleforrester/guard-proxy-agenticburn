# ABOUTME: Conformance for the structured log stream: one JSON object per line with a fixed schema and
# ABOUTME: a stable `event` name per guard decision, which Datadog log facets and monitors key on.
from __future__ import annotations

import json

from fakes import unreachable_url


def _records(p):
    p.events()  # flushes the capture file
    out = []
    for line in p.log_path.read_text().splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def test_guard_decisions_are_json_lines_with_fixed_schema(start_proxy, agent):
    p = start_proxy(INPUT_BLOCKLIST="on")
    p.chat("delete it")
    (rec,) = [r for r in _records(p) if r.get("event") == "input_blocklist_hit"]
    assert set(rec) >= {"timestamp", "level", "logger", "message", "event"}
    assert rec["level"] == "INFO"
    assert rec["logger"] == "guard-proxy"
    assert "delete" in rec["message"]


def test_forward_errors_log_at_error_level(start_proxy):
    p = start_proxy(AGENT_URL=unreachable_url())
    p.chat("hi")
    p.a2a("hi")
    errs = [r for r in _records(p) if r.get("event") == "forward_error"]
    assert len(errs) == 2
    assert {r["level"] for r in errs} == {"ERROR"}


def test_a_clean_request_logs_no_guard_events(start_proxy, agent):
    p = start_proxy(INPUT_GUARD="on", OUTPUT_GUARD="on")
    p.chat("one burrito")
    p.a2a("one burrito")
    assert p.events() == []
