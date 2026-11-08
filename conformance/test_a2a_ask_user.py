# ABOUTME: Conformance for an A2A reply carrying kagent's ask_user question in data parts: the always-on
# ABOUTME: redaction and the output guard read every copy of it (guard-proxy-agenticburn#73).
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from fakes import Reply, unreachable_url, verdict

pytestmark = pytest.mark.guards

# The tasks/get answer recorded on watch-it-burn-community on 2026-11-08 (see test_chat_ask_user.py). The
# question is written four times, all in data parts: the ask_user call and its pending response in the
# agent history, and the status message's adk_request_confirmation, in its originalFunctionCall and as
# its toolConfirmation hint.
RECORDED = json.loads((Path(__file__).parent / "recorded" / "ask_user_input_required.json").read_text())
QUESTION = "Which tortilla would you like for your burrito: flour or corn?"
THINKING = RECORDED["result"]["history"][2]["parts"][0]["text"]


def _slots(task: dict) -> list[tuple[dict, str]]:
    """The four places the recorded task holds the question, as (container, key)."""
    history = task["result"]["history"]
    confirmation = task["result"]["status"]["message"]["parts"][0]["data"]["args"]
    return [
        (history[2]["parts"][1]["data"]["args"]["questions"][0], "question"),
        (history[3]["parts"][0]["data"]["response"]["questions"][0], "question"),
        (confirmation["originalFunctionCall"]["args"]["questions"][0], "question"),
        (confirmation["toolConfirmation"], "hint"),
    ]


def _with_question(text: str, task: dict = RECORDED) -> dict:
    """The recorded task with every copy of its question set to `text`, the rest byte for byte."""
    out = copy.deepcopy(task)
    for obj, key in _slots(out):
        obj[key] = text
    return out


def test_the_recorded_task_holds_the_question_in_four_data_parts():
    assert [obj[key] for obj, key in _slots(RECORDED)] == [QUESTION] * 4
    agent_text = [p["text"] for h in RECORDED["result"]["history"] if h["role"] == "agent"
                  for p in h["parts"] if p["kind"] == "text"]
    assert all("flour" not in t for t in agent_text), "the question is in no agent text part"


def test_every_copy_of_the_question_gets_the_always_on_redaction(start_proxy, agent, guard):
    agent.respond = lambda req: Reply(body=RECORDED)
    p = start_proxy(ALWAYS_REDACT="flour")
    assert p.get("/guards").json()["output"] is False
    r = p.a2a("which tortilla?")
    assert r.status == 200
    # Only the question changes: the ids, names and states a client reads come back as the agent sent them.
    assert r.json() == _with_question(QUESTION.replace("flour", "[REDACTED]"))
    assert guard.calls() == [], "the always-on redaction calls no LLM Guard"
    assert "always_redact" in p.events()


def test_the_output_guard_scans_every_copy_of_the_question(start_proxy, agent, guard):
    agent.respond = lambda req: Reply(body=RECORDED)

    def scrub(req):
        out = req.json()["output"]
        return Reply(body=verdict(False, out.replace("flour", "[R]"), output=True)) if "flour" in out \
            else Reply(body=verdict(True, output=True))

    guard.respond = scrub
    p = start_proxy(OUTPUT_GUARD="on")
    assert p.a2a("which tortilla?").json() == _with_question(QUESTION.replace("flour", "[R]"))
    calls = guard.calls("/analyze/output")
    assert sorted(c.json()["output"] for c in calls) == sorted([THINKING] + [QUESTION] * 4)
    assert "output_scrub" in p.events()


def test_the_question_is_redacted_before_the_output_guard_reads_it(start_proxy, agent, guard):
    agent.respond = lambda req: Reply(body=RECORDED)
    p = start_proxy(ALWAYS_REDACT="flour", OUTPUT_GUARD="on")
    assert p.a2a("which tortilla?").json() == _with_question(QUESTION.replace("flour", "[REDACTED]"))
    outputs = [c.json()["output"] for c in guard.calls("/analyze/output")]
    assert outputs.count(QUESTION.replace("flour", "[REDACTED]")) == 4


def test_the_output_guard_down_blocks_every_copy_of_the_question(start_proxy, agent):
    agent.respond = lambda req: Reply(body=RECORDED)
    p = start_proxy(OUTPUT_GUARD="on", LLM_GUARD_URL=unreachable_url())
    expected = _with_question("[BLOCKED BY OUTPUT GUARDRAIL]")
    expected["result"]["history"][2]["parts"][0]["text"] = "[BLOCKED BY OUTPUT GUARDRAIL]"
    assert p.a2a("which tortilla?").json() == expected


def test_another_tools_call_and_confirmation_pass_verbatim(start_proxy, agent, guard):
    """Only the ask_user question is read from a data part: another tool's arguments, and a confirmation
    of another tool's call, are left as the agent sent them."""
    task = copy.deepcopy(RECORDED)
    history = task["result"]["history"]
    history[2]["parts"][1]["data"]["name"] = "order_burrito"
    history[3]["parts"][0]["data"]["name"] = "order_burrito"
    task["result"]["status"]["message"]["parts"][0]["data"]["args"]["originalFunctionCall"]["name"] = "order_burrito"
    agent.respond = lambda req: Reply(body=task)
    p = start_proxy(ALWAYS_REDACT="flour")
    assert p.a2a("which tortilla?").json() == task
    assert "always_redact" not in p.events()


@pytest.mark.parametrize(
    "parts",
    [
        [{"kind": "data"}, {"kind": "data", "data": "flour"}, {"kind": "data", "data": {"name": "ask_user"}}],
        [{"kind": "data", "data": {"name": "ask_user", "args": "flour", "response": ["flour"]}}],
        [{"kind": "data", "data": {"name": "ask_user", "args": {"questions": "flour"}}}],
        [{"kind": "data", "data": {"name": "ask_user", "args": {"questions": [None, "flour", {"question": 3}]}}}],
        [{"kind": "data", "data": {"name": "adk_request_confirmation", "args": ["flour"]}}],
        [{"kind": "data", "data": {"name": "adk_request_confirmation", "args": {
            "originalFunctionCall": {"name": "ask_user", "args": {"questions": [{}]}},
            "toolConfirmation": "flour"}}}],
    ],
)
def test_odd_data_parts_pass_through_with_every_guard_off(start_proxy, agent, parts):
    body = {"jsonrpc": "2.0", "id": "req-1", "result": {
        "history": [{"role": "agent", "parts": parts}], "status": {"message": {"parts": parts}}}}
    agent.respond = lambda req: Reply(body=body)
    r = start_proxy(ALWAYS_REDACT="flour").a2a("hi")
    assert r.status == 200
    assert r.json() == body
