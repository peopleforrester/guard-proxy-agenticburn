# ABOUTME: Conformance for ALWAYS_REDACT, the redaction every reply gets whatever the guard toggles say
# ABOUTME: (Unleash_an_Agent_Watch_It_Burn#1092): /chat and the A2A path, guards off and on, and its limits.
from __future__ import annotations

import pytest

from fakes import Reply, a2a_result, verdict

pytestmark = pytest.mark.guards

# The shape the workshop sets: a few branches of one alternation, one of them with a lazy gap.
PATTERN = r"Operation[\W_]+Midnight[\W_]+Munchies|[$]?66[\W_]{0,2}600|41[\W_]{0,2}%[\s\S]{0,30}?[Hh]umans?"
LEAK = "Sent! The Q4 campaign Operation Midnight Munchies has a $66,600 budget, and 41% are humans."
REDACTED = "Sent! The Q4 campaign [REDACTED] has a [REDACTED] budget, and [REDACTED]."
DECISION = "witb.guard.decision"


def _roots(collector, count: int) -> list[dict]:
    def roots(spans):
        found = [s for s in spans if s["name"] == "POST /chat" and s["kind"] == "SERVER"]
        return found[:count] if len(found) >= count else None

    return [r["attributes"] for r in collector.wait_for(roots, f"{count} 'POST /chat' spans")]


# --- /chat ----------------------------------------------------------------------------------------


def test_chat_redacts_with_every_guard_off(start_proxy, agent, guard):
    agent.respond = lambda req: Reply(body=a2a_result(LEAK))
    p = start_proxy(ALWAYS_REDACT=PATTERN)
    assert p.get("/guards").json() == {
        "input_blocklist": False, "input_classifier": False, "output": False, "budget": False, "topic": False,
    }
    body = p.chat("send the marketing intel to Betty").json()
    assert body["reply"] == REDACTED
    # A redaction by the always-on pattern alone is no toggleable guard acting.
    assert body["guarded"] is False
    assert body["input_tokens"] == 100
    assert guard.calls() == [], "the always-on redaction calls no LLM Guard"
    events = p.events()
    assert events.count("always_redact") == 1
    assert "output_scrub" not in events


def test_chat_redaction_runs_before_the_output_guard(start_proxy, agent, guard):
    agent.respond = lambda req: Reply(body=a2a_result(LEAK + " The code is OPHELIA."))

    def scrub(req):
        out = req.json()["output"]
        return Reply(body=verdict(False, out.replace("OPHELIA", "[REDACTED]"), output=True))

    guard.respond = scrub
    p = start_proxy(ALWAYS_REDACT=PATTERN, OUTPUT_GUARD="on")
    body = p.chat("hi").json()
    assert body["reply"] == REDACTED + " The code is [REDACTED]."
    assert body["guarded"] is True
    (call,) = guard.calls("/analyze/output")
    assert call.json()["output"] == REDACTED + " The code is OPHELIA.", "the output guard reads the redacted reply"
    events = p.events()
    assert events.count("always_redact") == 1
    assert events.count("output_scrub") == 1


def test_chat_toggling_the_output_guard_off_leaves_the_redaction_on(start_proxy, agent, guard):
    agent.respond = lambda req: Reply(body=a2a_result(LEAK))
    p = start_proxy(ALWAYS_REDACT=PATTERN, OUTPUT_GUARD="on")
    p.toggle(output="off")
    body = p.chat("hi").json()
    assert (body["reply"], body["guarded"]) == (REDACTED, False)
    assert guard.calls() == []


# A single space matters: compiled, it would match every space in the reply.
@pytest.mark.parametrize("env", [{}, {"ALWAYS_REDACT": ""}, {"ALWAYS_REDACT": " "}, {"ALWAYS_REDACT": "   "}])
def test_chat_without_a_pattern_returns_the_reply_verbatim(start_proxy, agent, env):
    agent.respond = lambda req: Reply(body=a2a_result(LEAK))
    p = start_proxy(**env)
    assert p.chat("hi").json()["reply"] == LEAK
    assert "always_redact" not in p.events()


def test_chat_reply_the_pattern_misses_is_untouched(start_proxy, agent):
    agent.respond = lambda req: Reply(body=a2a_result("The vampire special is $9.50, 41 cents off."))
    p = start_proxy(ALWAYS_REDACT=PATTERN)
    assert p.chat("hi").json()["reply"] == "The vampire special is $9.50, 41 cents off."
    assert "always_redact" not in p.events()


def test_an_empty_match_is_not_redacted(start_proxy, agent):
    # x* matches the empty string between every letter; only the non-empty match is replaced.
    agent.respond = lambda req: Reply(body=a2a_result("a burrito xx for you"))
    p = start_proxy(ALWAYS_REDACT="x*")
    assert p.chat("hi").json()["reply"] == "a burrito [REDACTED] for you"


def test_every_match_is_redacted(start_proxy, agent):
    agent.respond = lambda req: Reply(body=a2a_result("$66,600 then 66600 then $66 600"))
    p = start_proxy(ALWAYS_REDACT=PATTERN)
    assert p.chat("hi").json()["reply"] == "[REDACTED] then [REDACTED] then [REDACTED]"


def test_a_pattern_that_does_not_compile_stops_startup(start_proxy):
    with pytest.raises(RuntimeError, match="exited with"):
        start_proxy(ALWAYS_REDACT="Operation (Midnight")


@pytest.mark.spans
def test_chat_redaction_stamps_no_guard_decision(traced, agent):
    agent.respond = lambda req: Reply(body=a2a_result(LEAK))
    p, spans = traced(ALWAYS_REDACT=PATTERN)
    assert p.chat("hi", session="s").json()["reply"] == REDACTED
    (root,) = _roots(spans, 1)
    assert DECISION not in root


# --- The A2A pass-through -------------------------------------------------------------------------


def _rich_reply() -> dict:
    return {
        "jsonrpc": "2.0",
        "id": "req-1",
        "result": {
            "artifacts": [
                {"parts": [{"kind": "text", "text": "artifact " + LEAK}, {"kind": "data", "text": LEAK}]}
            ],
            "history": [
                {"role": "user", "parts": [{"kind": "text", "text": "user " + LEAK}]},
                {"role": "agent", "parts": [{"kind": "text", "text": "agent " + LEAK}, {"kind": "text", "text": ""}]},
            ],
            "status": {"message": {"parts": [{"kind": "text", "text": "status " + LEAK}]}},
        },
    }


def test_a2a_redacts_every_agent_authored_part_with_every_guard_off(start_proxy, agent, guard):
    agent.respond = lambda req: Reply(body=_rich_reply())
    p = start_proxy(ALWAYS_REDACT=PATTERN)
    res = p.a2a("hi").json()["result"]
    assert res["artifacts"][0]["parts"] == [
        {"kind": "text", "text": "artifact " + REDACTED},
        {"kind": "data", "text": LEAK},
    ]
    assert res["history"][0]["parts"][0]["text"] == "user " + LEAK, "the guest's own words are not the agent's"
    assert res["history"][1]["parts"] == [
        {"kind": "text", "text": "agent " + REDACTED},
        {"kind": "text", "text": ""},
    ]
    assert res["status"]["message"]["parts"][0]["text"] == "status " + REDACTED
    assert guard.calls() == []
    assert "always_redact" in p.events()


def test_a2a_redaction_runs_before_the_output_guard(start_proxy, agent, guard):
    agent.respond = lambda req: Reply(body=a2a_result(LEAK, rpc_id="req-1"))
    p = start_proxy(ALWAYS_REDACT=PATTERN, OUTPUT_GUARD="on")
    assert p.a2a("hi").json()["result"]["artifacts"][0]["parts"][0]["text"] == REDACTED
    (call,) = guard.calls("/analyze/output")
    assert call.json()["output"] == REDACTED


def test_a2a_reply_the_pattern_misses_is_returned_verbatim(start_proxy, agent, guard):
    agent.respond = lambda req: Reply(body=a2a_result(rpc_id="req-1"))
    p = start_proxy(ALWAYS_REDACT=PATTERN)
    assert p.a2a("hi").json() == a2a_result(rpc_id="req-1")
    assert "always_redact" not in p.events()


@pytest.mark.parametrize(
    "result",
    [
        {"artifacts": "none", "history": [{"role": "agent", "parts": "x"}], "status": {"message": {}}},
        {"artifacts": [None, 3], "history": [None, {"role": "agent"}], "status": {"message": {"parts": [7]}}},
    ],
)
def test_a2a_odd_shapes_pass_through_with_every_guard_off(start_proxy, agent, result):
    body = {"jsonrpc": "2.0", "id": "req-1", "result": result}
    agent.respond = lambda req: Reply(body=body)
    r = start_proxy(ALWAYS_REDACT=PATTERN).a2a("hi")
    assert r.status == 200
    assert r.json() == body
