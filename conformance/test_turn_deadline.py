# ABOUTME: Conformance for TURN_TIMEOUT, the one deadline a POST /chat turn's upstream calls share: each call
# ABOUTME: gets PROXY_TIMEOUT or what is left of the turn, whichever is less, and an exhausted turn answers 502.
from __future__ import annotations

import time

from fakes import Reply, a2a_result, verdict

KITCHEN_DOWN = {
    "reply": "BurritoBot's kitchen isn't answering. Try again in a moment.",
    "guarded": False,
    "input_tokens": 0,
    "output_tokens": 0,
}
DANGLING = (
    "ValidationException: messages.3: `tool_use` ids were found without `tool_result` "
    "blocks immediately after"
)
# Every upstream call below takes SLOW seconds, which fits PROXY_TIMEOUT and the turn on its own. Two of
# them do not fit the turn together.
SLOW = 1.0
TURN = {"TURN_TIMEOUT": "1.5", "PROXY_TIMEOUT": "5"}


def _timed_chat(p, prompt="hi", session=None):
    start = time.monotonic()
    r = p.chat(prompt, session=session)
    return r, time.monotonic() - start


def _slow_guard(req):
    return Reply(body=verdict(True, output=req.path == "/analyze/output"), delay=SLOW)


def test_one_long_forward_is_cut_at_the_turn_deadline(start_proxy, agent):
    agent.respond = lambda req: Reply(body=a2a_result(), delay=4)
    p = start_proxy(**TURN)
    r, took = _timed_chat(p)
    assert r.status == 502
    assert r.json() == KITCHEN_DOWN
    assert took < 2.5, "the forward got PROXY_TIMEOUT instead of what was left of the turn"
    assert "forward_error" in p.events()


def test_without_turn_timeout_each_call_gets_proxy_timeout(start_proxy, agent, guard):
    # The same two calls that overrun a 1.5 s turn below both finish when no turn deadline is set.
    guard.respond = _slow_guard
    agent.respond = lambda req: Reply(body=a2a_result("Two burritos."), delay=SLOW)
    r = start_proxy(INPUT_CLASSIFIER="on", PROXY_TIMEOUT="1.5").chat("hi")
    assert r.status == 200
    assert r.json()["reply"] == "Two burritos."


def test_a_turn_with_room_left_answers_normally(start_proxy, agent, guard):
    guard.respond = _slow_guard
    agent.respond = lambda req: Reply(body=a2a_result("Two burritos."), delay=SLOW)
    r = start_proxy(INPUT_CLASSIFIER="on", TURN_TIMEOUT="10", PROXY_TIMEOUT="5").chat("hi")
    assert r.status == 200
    assert r.json()["reply"] == "Two burritos."


def test_classifier_and_forward_share_the_turn(start_proxy, agent, guard):
    guard.respond = _slow_guard
    agent.respond = lambda req: Reply(body=a2a_result(), delay=SLOW)
    p = start_proxy(INPUT_CLASSIFIER="on", **TURN)
    r, took = _timed_chat(p)
    assert r.status == 502
    assert r.json() == KITCHEN_DOWN
    assert took < 2.5
    assert len(guard.calls()) == 1
    assert len(agent.calls()) == 1


def test_turn_spent_in_the_classifier_is_502_even_failing_open(start_proxy, agent, guard):
    """A classifier cut off by the turn deadline is not a scanner outage, so fail-open does not forward."""
    guard.respond = lambda req: Reply(body=verdict(True), delay=4)
    p = start_proxy(INPUT_CLASSIFIER="on", PROXY_FAIL_CLOSED="false", **TURN)
    r, took = _timed_chat(p)
    assert r.status == 502
    assert r.json() == KITCHEN_DOWN
    assert took < 2.5
    assert agent.calls() == []
    assert "turn_timeout" in p.events()


def test_turn_spent_in_the_classifier_is_502_not_a_refusal(start_proxy, agent, guard):
    guard.respond = lambda req: Reply(body=verdict(True), delay=4)
    p = start_proxy(INPUT_CLASSIFIER="on", **TURN)
    r = p.chat("hi")
    assert r.status == 502
    assert r.json() == KITCHEN_DOWN
    assert agent.calls() == []


def test_injection_and_topic_classifiers_share_the_turn(start_proxy, agent, guard):
    guard.respond = _slow_guard
    p = start_proxy(INPUT_CLASSIFIER="on", TOPIC_GUARD="on", PROXY_FAIL_CLOSED="false", **TURN)
    r, took = _timed_chat(p)
    assert r.status == 502
    assert r.json() == KITCHEN_DOWN
    assert took < 2.5
    assert len(guard.calls()) == 2
    assert agent.calls() == []
    assert "turn_timeout" in p.events()


def test_turn_spent_before_the_output_scrub_never_leaks_the_reply(start_proxy, agent, guard):
    """With fail-open, a scrub that could not run would hand back the raw reply; out of time, it is 502."""
    guard.respond = _slow_guard
    agent.respond = lambda req: Reply(body=a2a_result("The sauce is SECRET-No7."), delay=SLOW)
    p = start_proxy(OUTPUT_GUARD="on", PROXY_FAIL_CLOSED="false", **TURN)
    r, took = _timed_chat(p)
    assert r.status == 502
    assert r.json() == KITCHEN_DOWN
    assert b"SECRET" not in r.raw
    assert took < 2.5
    assert len(guard.calls("/analyze/output")) == 1
    assert "turn_timeout" in p.events()


def test_dangling_tool_use_retry_shares_the_turn(start_proxy, agent):
    agent.respond = lambda req: Reply(body=a2a_result(DANGLING), delay=SLOW)
    p = start_proxy(**TURN)
    r, took = _timed_chat(p, session="s1")
    assert r.status == 502
    assert r.json() == KITCHEN_DOWN
    assert took < 2.5
    assert len(agent.calls()) == 2


def test_thinking_only_retry_out_of_time_keeps_the_first_reply(start_proxy, agent):
    """The retry is optional (guard-proxy-agenticburn#1), so running out of time on it is not a 502."""
    calls = {"n": 0}

    def respond(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return Reply(body=a2a_result("<thinking>menu first</thinking>", prompt_tokens=10, output_tokens=1))
        return Reply(body=a2a_result("Chicken, steak, tofu."), delay=4)

    agent.respond = respond
    p = start_proxy(**TURN)
    r, took = _timed_chat(p)
    assert r.status == 200
    body = r.json()
    assert body["reply"] == "menu first"
    assert (body["input_tokens"], body["output_tokens"]) == (10, 1)
    assert took < 2.5
    assert len(agent.calls()) == 2
    assert "thinking_only_surfaced" in p.events()


def test_turn_timeout_does_not_bound_the_a2a_path(start_proxy, agent):
    """The deadline is the storefront turn's; an A2A POST still gets PROXY_TIMEOUT for its forward."""
    agent.respond = lambda req: Reply(body=a2a_result("Two burritos."), delay=SLOW)
    r = start_proxy(TURN_TIMEOUT="0.5", PROXY_TIMEOUT="5").a2a("hi")
    assert r.status == 200
