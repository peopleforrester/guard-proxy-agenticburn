# ABOUTME: Conformance for a /chat turn whose A2A task stops at input-required on kagent's ask_user call:
# ABOUTME: the reply is the question, not the scratchpad, and the task is cancelled (guard-proxy-agenticburn#72).
from __future__ import annotations

import copy
import json
import time
from pathlib import Path

from fakes import Reply, a2a_result

# The tasks/get answer recorded on watch-it-burn-community on 2026-11-08, kagent 0.10.1 with the Go
# runtime, after asking the workshop-agent over A2A to use its ask_user tool. The history's agent text is
# the scratchpad alone, there is no artifact, and the question is only in status.message.
RECORDED = json.loads((Path(__file__).parent / "recorded" / "ask_user_input_required.json").read_text())
TASK = RECORDED["result"]["id"]
QUESTION = "Which tortilla would you like for your burrito: flour or corn?"
# How long a cancel sent after the room has its answer may take to arrive on a loaded host.
WAIT = 10.0


def _method(req):
    return req.json().get("method")


def _cancels(agent):
    return [c.json() for c in agent.calls() if _method(c) == "tasks/cancel"]


def _wait_for(predicate, timeout=WAIT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _asking(task=RECORDED):
    """An agent that accepts the turn as a running task, ends it as `task` on the first poll, and accepts
    a cancel of it, as kagent's Go runtime did live."""
    task_id = task["result"]["id"]

    def respond(req):
        method = _method(req)
        if method == "message/send":
            return Reply(body={"jsonrpc": "2.0", "id": "chat",
                               "result": {"kind": "task", "id": task_id, "status": {"state": "submitted"}}})
        if method == "tasks/get":
            return Reply(body=task)
        if method == "tasks/cancel":
            return Reply(body={"jsonrpc": "2.0", "id": "cancel",
                               "result": {"kind": "task", "id": task_id, "status": {"state": "canceled"}}})
        return Reply(status=400, body={"error": f"unexpected {method}"})

    return respond


def _questions(*texts):
    """The recorded task with its ask_user call asking `texts`."""
    task = copy.deepcopy(RECORDED)
    call = task["result"]["status"]["message"]["parts"][0]["data"]["args"]["originalFunctionCall"]
    call["args"]["questions"] = [{"question": t} for t in texts]
    return task


def test_the_recorded_task_has_the_shape_the_turn_lost():
    """The recording is the shape #72 names: input-required, no artifact, and a scratchpad-only history."""
    result = RECORDED["result"]
    assert result["status"]["state"] == "input-required"
    assert "artifacts" not in result
    agent_text = " ".join(p["text"] for h in result["history"] if h["role"] == "agent"
                          for p in h["parts"] if p["kind"] == "text")
    assert agent_text.strip().startswith("<thinking>") and agent_text.strip().endswith("</thinking>")


def test_an_ask_user_turn_replies_with_the_question(start_proxy, agent):
    agent.respond = _asking()
    p = start_proxy()
    r = p.chat("which tortilla?")
    assert r.status == 200
    body = r.json()
    assert body["reply"] == QUESTION
    assert body["guarded"] is False
    # The recorded task's own usage, metered once: the question is an answer, so no retry is sent.
    assert (body["input_tokens"], body["output_tokens"]) == (10193, 81)
    assert [_method(c) for c in agent.calls()].count("message/send") == 1
    events = p.events()
    assert events.count("ask_user_reply") == 1
    assert "thinking_only_retry" not in events
    assert "thinking_only_surfaced" not in events


def test_the_ask_user_task_is_cancelled_after_the_reply(start_proxy, agent):
    agent.respond = _asking()
    p = start_proxy()
    assert p.chat("which tortilla?").json()["reply"] == QUESTION
    assert _wait_for(lambda: _cancels(agent)), "the input-required task was left pending"
    (cancel,) = _cancels(agent)
    assert cancel["jsonrpc"] == "2.0"
    assert cancel["params"] == {"id": TASK}
    assert _wait_for(lambda: "task_cancel" in p.events())
    assert "task_cancel_failed" not in p.events()


def test_several_questions_reply_one_per_line(start_proxy, agent):
    agent.respond = _asking(_questions("Flour or corn?", "  Beans or no beans?  ", " "))
    p = start_proxy()
    assert p.chat("build me a burrito").json()["reply"] == "Flour or corn?\nBeans or no beans?"


def test_a_question_holding_the_answer_reaches_the_guest_whole(start_proxy, agent):
    """The linked-list shape from Unleash_an_Agent_Watch_It_Burn#889: Nova's whole answer, code and all,
    was the ask_user question."""
    answer = ("Here is how to reverse a linked list:\n```python\nprev = None\nwhile head:\n"
              "    head.next, prev, head = prev, head, head.next\n```\nShall we get back to your order?")
    agent.respond = _asking(_questions(answer))
    assert start_proxy().chat("how do I reverse a linked list?").json()["reply"] == answer


def test_the_question_gets_the_always_on_redaction(start_proxy, agent):
    """The question is the model's output, so the redaction every reply gets reads it too."""
    agent.respond = _asking()
    p = start_proxy(ALWAYS_REDACT="flour")
    assert p.chat("which tortilla?").json()["reply"] == QUESTION.replace("flour", "[REDACTED]")
    assert "always_redact" in p.events()


def test_a_completed_task_is_not_read_as_a_question(start_proxy, agent):
    """Only a task stopped at input-required waits on an answer: a completed one answers from its artifact
    and is not cancelled."""
    done = copy.deepcopy(RECORDED)
    done["result"]["status"]["state"] = "completed"
    done["result"]["artifacts"] = a2a_result("Two burritos.")["result"]["artifacts"]
    agent.respond = _asking(done)
    p = start_proxy()
    assert p.chat("hi").json()["reply"] == "Two burritos."
    time.sleep(0.3)
    assert _cancels(agent) == []
    assert "ask_user_reply" not in p.events()
