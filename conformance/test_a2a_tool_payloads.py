# ABOUTME: Conformance for an A2A reply carrying a tool's call and response in data parts: the always-on
# ABOUTME: redaction reads every string in them, the output guard none of them (guard-proxy-agenticburn#75).
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from fakes import Reply, unreachable_url, verdict

pytestmark = pytest.mark.guards

# The tasks/get answer recorded on watch-it-burn-community on 2026-11-08 (see test_chat_answer_then_tool.py).
# The first model step called apply_optimization, and the tool's response, a Deployment manifest, sits in
# the next agent message's function_response data part, which no text part repeats word for word.
RECORDED = json.loads((Path(__file__).parent / "recorded" / "answer_then_tool_call.json").read_text())
CALL = RECORDED["result"]["history"][2]["parts"][1]
RESPONSE = RECORDED["result"]["history"][3]["parts"][0]
SECRET = "name: clownfile-optimizer, namespace: apps"


def _agent_texts(task: dict = RECORDED) -> list[str]:
    """Every agent-authored text part of the task: the artifacts and the agent's turns in the history."""
    result = task["result"]
    messages = [h for h in result["history"] if h["role"] == "agent"] + result["artifacts"]
    return [p["text"] for m in messages for p in m["parts"] if p["kind"] == "text"]


def _with_result(text: str, task: dict = RECORDED) -> dict:
    """The recorded task with its tool response's result set to `text`, the rest byte for byte."""
    out = copy.deepcopy(task)
    out["result"]["history"][3]["parts"][0]["data"]["response"]["output"]["result"] = text
    return out


def test_the_recorded_task_holds_a_tool_call_and_its_response_in_data_parts():
    assert CALL["metadata"]["adk_type"] == "function_call"
    assert RESPONSE["metadata"]["adk_type"] == "function_response"
    assert CALL["data"]["id"] == RESPONSE["data"]["id"]
    assert CALL["data"]["name"] == RESPONSE["data"]["name"] == "apply_optimization"
    assert SECRET in RESPONSE["data"]["response"]["output"]["result"]
    assert all(SECRET not in t for t in _agent_texts()), "only the tool response holds it"


def test_a_tool_response_gets_the_always_on_redaction(start_proxy, agent, guard):
    agent.respond = lambda req: Reply(body=RECORDED)
    p = start_proxy(ALWAYS_REDACT=SECRET)
    assert p.get("/guards").json()["output"] is False
    r = p.a2a("reverse a linked list")
    assert r.status == 200
    # Only the matched text changes: the call's id and name, and every part's metadata, come back as sent.
    result = RESPONSE["data"]["response"]["output"]["result"]
    assert r.json() == _with_result(result.replace(SECRET, "[REDACTED]"))
    assert guard.calls() == [], "the always-on redaction calls no LLM Guard"
    assert "always_redact" in p.events()


def test_another_tools_arguments_response_and_confirmation_get_the_always_on_redaction(start_proxy, agent):
    """Every string in an agent-authored data part is redacted, at any depth and inside lists, whichever tool
    wrote it: here the recorded ask_user task with its tool renamed, whose call, response and confirmation
    carry the text as arguments, as a response and as a hint."""
    task = json.loads((Path(__file__).parent / "recorded" / "ask_user_input_required.json").read_text())
    history = task["result"]["history"]
    history[2]["parts"][1]["data"]["name"] = "order_burrito"
    history[3]["parts"][0]["data"]["name"] = "order_burrito"
    task["result"]["status"]["message"]["parts"][0]["data"]["args"]["originalFunctionCall"]["name"] = "order_burrito"
    history[2]["parts"][1]["data"]["args"]["sides"] = ["chips", {"note": "extra flour tortilla", "count": 2}]
    agent.respond = lambda req: Reply(body=task)
    p = start_proxy(ALWAYS_REDACT="flour")
    assert "flour" in json.dumps(task["result"]["status"]) and "flour" in json.dumps(history[3])
    # The guest's own two turns name flour too, and are left as sent.
    expected = copy.deepcopy(task)
    for h in expected["result"]["history"][2:]:
        h["parts"] = json.loads(json.dumps(h["parts"]).replace("flour", "[REDACTED]"))
    status = expected["result"]["status"]
    status["message"] = json.loads(json.dumps(status["message"]).replace("flour", "[REDACTED]"))
    assert p.a2a("which tortilla?").json() == expected
    assert "always_redact" in p.events()


def test_the_guests_own_data_parts_are_not_redacted(start_proxy, agent):
    task = copy.deepcopy(RECORDED)
    task["result"]["history"][0]["parts"].append({"kind": "data", "data": {"note": SECRET}})
    task["result"]["history"][1]["parts"].append({"kind": "data", "data": {"note": SECRET}})
    agent.respond = lambda req: Reply(body=task)
    r = start_proxy(ALWAYS_REDACT=SECRET).a2a("reverse a linked list")
    result = RESPONSE["data"]["response"]["output"]["result"]
    assert r.json() == _with_result(result.replace(SECRET, "[REDACTED]"), task)


def test_the_output_guard_reads_no_tool_payload(start_proxy, agent, guard):
    agent.respond = lambda req: Reply(body=RECORDED)
    guard.respond = lambda req: Reply(body=verdict(True, output=True))
    p = start_proxy(OUTPUT_GUARD="on")
    assert p.a2a("reverse a linked list").json() == RECORDED
    outputs = [c.json()["output"] for c in guard.calls("/analyze/output")]
    assert sorted(outputs) == sorted(_agent_texts())


def test_the_output_guard_down_leaves_the_tool_payloads_as_sent(start_proxy, agent):
    """Fail-closed blocks every text the output guard reads, and it reads no tool call or response, so their
    ids and names stay intact for the client."""
    agent.respond = lambda req: Reply(body=RECORDED)
    p = start_proxy(OUTPUT_GUARD="on", LLM_GUARD_URL=unreachable_url())
    expected = copy.deepcopy(RECORDED)
    result = expected["result"]
    for m in [h for h in result["history"] if h["role"] == "agent"] + result["artifacts"]:
        for part in m["parts"]:
            if part["kind"] == "text":
                part["text"] = "[BLOCKED BY OUTPUT GUARDRAIL]"
    assert p.a2a("reverse a linked list").json() == expected


def test_redaction_then_the_output_guard_leave_the_tool_response_redacted(start_proxy, agent, guard):
    agent.respond = lambda req: Reply(body=RECORDED)
    guard.respond = lambda req: Reply(body=verdict(True, output=True))
    p = start_proxy(ALWAYS_REDACT=SECRET, OUTPUT_GUARD="on")
    result = RESPONSE["data"]["response"]["output"]["result"]
    assert p.a2a("reverse a linked list").json() == _with_result(result.replace(SECRET, "[REDACTED]"))
    assert sorted(c.json()["output"] for c in guard.calls("/analyze/output")) == sorted(_agent_texts())
