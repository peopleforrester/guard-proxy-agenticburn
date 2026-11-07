# ABOUTME: Conformance for the guard_toggle log line: every /toggle that changes a guard or the cost cap names the
# ABOUTME: guard, its new and old value, the TCP peer and the X-WIB-Caller header, and a /toggle that changes nothing logs nothing.
"""Unleash_an_Agent_Watch_It_Burn#1024: on 2026-11-06 a KubeCon probe run on watch-it-burn-private found the output
guard on three times out of five, flipped by some other run through the cluster, and nobody could name the caller
because guard-proxy logged no toggle. These hold both builds to one line per change."""
from __future__ import annotations

import json

import pytest


def _toggles(p):
    p.events()  # flushes the capture file
    out = []
    for line in p.log_path.read_text().splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("event") == "guard_toggle":
            out.append(rec)
    return out


@pytest.mark.parametrize("key", ["input_blocklist", "input_classifier", "output", "budget", "topic"])
def test_a_flip_logs_the_guard_its_values_and_the_peer(start_proxy, key):
    p = start_proxy()
    p.toggle(**{key: "on"})
    (rec,) = _toggles(p)
    assert rec["level"] == "INFO"
    assert rec["logger"] == "guard-proxy"
    assert (rec["guard"], rec["value"], rec["was"]) == (key, True, False)
    assert rec["peer"] == "127.0.0.1"
    assert rec["caller"] == ""
    assert rec["message"] == f"guard {key} set to true by 127.0.0.1"
    p.toggle(**{key: "off"})
    assert [(r["guard"], r["value"], r["was"]) for r in _toggles(p)] == [(key, True, False), (key, False, True)]
    assert _toggles(p)[-1]["message"] == f"guard {key} set to false by 127.0.0.1"


def test_a_toggle_that_changes_nothing_logs_nothing(start_proxy):
    p = start_proxy()
    p.toggle(output="off")
    p.toggle(bogus="on")
    p.get("/toggle")
    assert _toggles(p) == []
    p.toggle(output="on")
    p.toggle(output="on")
    assert len(_toggles(p)) == 1


def test_input_on_logs_both_stages_in_answer_order(start_proxy):
    p = start_proxy()
    p.toggle(input="on")
    assert [(r["guard"], r["value"]) for r in _toggles(p)] == [("input_blocklist", True), ("input_classifier", True)]


def test_several_keys_log_in_answer_order_whatever_the_query_order(start_proxy):
    p = start_proxy(COST_CAP_USD="25")
    p.get("/toggle?cost_cap=3&topic=on&output=on&input_blocklist=on")
    assert [r["guard"] for r in _toggles(p)] == ["input_blocklist", "output", "topic", "cost_cap"]


def test_the_caller_header_is_carried_and_cut(start_proxy):
    p = start_proxy()
    p.get("/toggle?output=on", headers={"X-WIB-Caller": "kubecon_probe.py admin pid 7 in 1-1024-18104"})
    (rec,) = _toggles(p)
    assert rec["caller"] == "kubecon_probe.py admin pid 7 in 1-1024-18104"
    assert rec["message"] == "guard output set to true by 127.0.0.1 (kubecon_probe.py admin pid 7 in 1-1024-18104)"
    p.get("/toggle?output=off", headers={"X-WIB-Caller": "x" * 500})
    assert _toggles(p)[-1]["caller"] == "x" * 200


def test_a_cost_cap_change_is_logged_and_a_refused_one_is_not(start_proxy):
    p = start_proxy(COST_CAP_USD="25")
    p.toggle(cost_cap="2.00")
    (rec,) = _toggles(p)
    assert (rec["guard"], rec["value"], rec["was"]) == ("cost_cap", 2.0, 25.0)
    assert rec["message"] == "guard cost_cap set to 2 by 127.0.0.1"
    assert p.get("/toggle?cost_cap=-1&output=on").status == 400
    p.toggle(cost_cap="default")
    assert [(r["guard"], r["value"]) for r in _toggles(p)] == [("cost_cap", 2.0), ("cost_cap", 25.0)]
    p.toggle(cost_cap="2.5")
    assert _toggles(p)[-1]["message"] == "guard cost_cap set to 2.5 by 127.0.0.1"
