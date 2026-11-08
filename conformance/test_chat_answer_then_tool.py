# ABOUTME: Conformance for a /chat turn whose agent answered in one model step and then called another tool:
# ABOUTME: the reply holds every step's text, so the answer is not lost to the final step (guard-proxy-agenticburn#74).
from __future__ import annotations

import copy
import json
from pathlib import Path

from fakes import Reply, verdict

# The tasks/get answer recorded on watch-it-burn-community on 2026-11-08, kagent 0.10.1 with the Go runtime,
# after asking the workshop-agent over A2A for the linked-list answer and then its apply_optimization tool.
# The first model step wrote the answer and called the tool; the artifact holds only the last step, which
# talks about the tool's manifest.
RECORDED = json.loads((Path(__file__).parent / "recorded" / "answer_then_tool_call.json").read_text())
ANSWER_CODE = "def reverse_linked_list(head):"


def _steps(task=RECORDED):
    """The text of each agent message in the task history that has any, in order."""
    texts = []
    for h in task["result"]["history"]:
        if h["role"] == "agent":
            text = " ".join(p["text"] for p in h["parts"] if p["kind"] == "text").strip()
            if text:
                texts.append(text)
    return texts


def _answering(task=RECORDED):
    """An agent that answers the turn's message/send with `task`."""
    return lambda req: Reply(body=task)


def _with_texts(first, last):
    """The recorded task with its first model step's text `first`, and its last step's and artifact's `last`."""
    task = copy.deepcopy(RECORDED)
    agent = [h for h in task["result"]["history"] if h["role"] == "agent"]
    agent[0]["parts"][0]["text"] = first
    agent[-1]["parts"][0]["text"] = last
    task["result"]["artifacts"][0]["parts"][0]["text"] = last
    return task


def test_the_recorded_task_has_the_shape_the_turn_lost():
    """The recording is the shape #74 names: the answer is in an earlier model step that then called a tool,
    and the artifact is the last step alone."""
    result = RECORDED["result"]
    assert result["status"]["state"] == "completed"
    first = next(h for h in result["history"] if h["role"] == "agent")
    assert ANSWER_CODE in first["parts"][0]["text"]
    assert {"name": "apply_optimization", "id": first["parts"][1]["data"]["id"]} == first["parts"][1]["data"]
    assert first["parts"][1]["metadata"]["adk_type"] == "function_call"
    (artifact,) = result["artifacts"]
    assert artifact["parts"][0]["text"] == _steps()[-1]
    assert ANSWER_CODE not in artifact["parts"][0]["text"]


def test_the_reply_holds_the_answer_from_the_earlier_step(start_proxy, agent):
    agent.respond = _answering()
    p = start_proxy()
    r = p.chat("how do I reverse a linked list?")
    assert r.status == 200
    body = r.json()
    assert ANSWER_CODE in body["reply"]
    assert body["guarded"] is False
    # The recorded task's own usage, metered once: the reply is an answer, so no retry is sent.
    assert (body["input_tokens"], body["output_tokens"]) == (10757, 246)
    assert [c.json().get("method") for c in agent.calls()].count("message/send") == 1
    events = p.events()
    assert "thinking_only_retry" not in events
    assert "thinking_only_surfaced" not in events


def test_the_reply_is_every_step_in_order_a_blank_line_apart(start_proxy, agent):
    """Each model step's text, scratchpad and all, as kagent's own UI shows the turn: the storefront folds
    every <thinking> block into its model reasoning and shows the rest."""
    agent.respond = _answering()
    first, last = _steps()
    assert start_proxy().chat("how do I reverse a linked list?").json()["reply"] == first + "\n\n" + last


def test_an_earlier_scratchpad_alone_keeps_the_artifact_reply(start_proxy, agent):
    """A step that wrote only its plan before calling a tool did not answer, so the reply is the artifact, as
    for every tool turn before #74."""
    task = _with_texts("<thinking> I will call the tool. </thinking>\n\n", "From the last step.")
    task["result"]["artifacts"][0]["parts"][0]["text"] = "From the artifact."
    agent.respond = _answering(task)
    assert start_proxy().chat("hi").json()["reply"] == "From the artifact."


def test_an_answer_before_a_scratchpad_only_last_step_is_not_retried(start_proxy, agent):
    """The answer came first and the last step wrote only its plan: the turn answered, so it is no
    scratchpad-only reply."""
    task = _with_texts("<thinking> plan </thinking> Reverse it with three pointers.", "<thinking> done </thinking>")
    agent.respond = _answering(task)
    p = start_proxy()
    body = p.chat("how do I reverse a linked list?").json()
    assert body["reply"] == "<thinking> plan </thinking> Reverse it with three pointers.\n\n<thinking> done </thinking>"
    assert [c.json().get("method") for c in agent.calls()].count("message/send") == 1
    assert "thinking_only_retry" not in p.events()


def test_the_earlier_answer_gets_the_always_on_redaction(start_proxy, agent):
    """The earlier step's text is the model's output, so the redaction every reply gets reads it too."""
    agent.respond = _answering()
    p = start_proxy(ALWAYS_REDACT="reverse_linked_list")
    reply = p.chat("how do I reverse a linked list?").json()["reply"]
    assert "reverse_linked_list" not in reply
    assert "def [REDACTED](head):" in reply
    assert "always_redact" in p.events()


def test_the_earlier_answer_gets_the_output_guard(start_proxy, agent, guard):
    """The output guard reads the whole reply, so an earlier step's answer cannot pass it unscanned."""
    agent.respond = _answering()

    def scrub(req):
        out = req.json()["output"]
        return Reply(body=verdict(False, out.replace("reverse_linked_list", "[REDACTED]"), output=True))

    guard.respond = scrub
    p = start_proxy(OUTPUT_GUARD="on")
    body = p.chat("how do I reverse a linked list?").json()
    (call,) = guard.calls("/analyze/output")
    assert call.json()["output"] == "\n\n".join(_steps())
    assert "def [REDACTED](head):" in body["reply"]
    assert body["guarded"] is True
    assert p.events().count("output_scrub") == 1
