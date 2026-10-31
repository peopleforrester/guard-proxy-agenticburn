# ABOUTME: Conformance for the on-topic guard (`topic`): its seeding and toggle, the three steps that
# ABOUTME: decide (off phrase, on phrase, BanTopics classifier), its place in the stage order, on both paths.
from __future__ import annotations

import json

import pytest

from conftest import a2a_request
from fakes import Reply, unreachable_url, verdict

# A slice of the workshop's lists (Unleash_an_Agent_Watch_It_Burn gitops/ai-layer/resources.yaml),
# written the way that file folds them: spaces after the commas.
OFF = "dance, dancing, dance move, karaoke, hot dog, linked list, song, cha-cha"
ON = "menu, burrito, salsa, rice, hour, open, Dragon's Blood"
REFUSAL = "I'm BurritoBot, and I only talk burritos!"
TOPIC_ENV = {
    "TOPIC_GUARD": "on",
    "TOPIC_OFF_LIST": OFF,
    "TOPIC_ON_LIST": ON,
    "TOPIC_REFUSAL": REFUSAL,
}
REFUSED = {"reply": REFUSAL, "guarded": True, "input_tokens": 0, "output_tokens": 0}
TOPIC_SCAN = {"scanners_suppress": ["PromptInjection"]}
INJECTION_SCAN = {"scanners_suppress": ["BanTopics"]}


def _records(p, event):
    p.events()  # flushes the capture file
    out = []
    for line in p.log_path.read_text().splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("event") == event:
            out.append(rec)
    return out


def _bans_topics(req):
    """LLM Guard with BanTopics armed: the topic scan refuses, the injection scan allows."""
    body = req.json()
    return Reply(body=verdict(body.get("scanners_suppress") != ["PromptInjection"]))


# --- Seeding and the toggle -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "env,on",
    [
        ({}, False),
        ({"TOPIC_GUARD": "on"}, True),
        ({"TOPIC_GUARD": "ON"}, True),
        ({"TOPIC_GUARD": "true"}, False),
        # The legacy input switch seeds the two input stages, never this one.
        ({"INPUT_GUARD": "on"}, False),
    ],
)
def test_topic_seeded_from_env(start_proxy, env, on):
    p = start_proxy(**env)
    assert p.get("/guards").json()["topic"] is on
    assert p.get("/controls").json()["ai"]["topic"] is on


def test_live_toggle_arms_the_topic_guard_without_restart(start_proxy, agent):
    p = start_proxy(**{**TOPIC_ENV, "TOPIC_GUARD": "off"})
    assert p.chat("teach me some salsa dance moves").json()["guarded"] is False
    assert p.toggle(topic="on")["topic"] is True
    assert p.chat("teach me some salsa dance moves").json() == REFUSED
    assert p.toggle(topic="off")["topic"] is False
    assert p.chat("teach me some salsa dance moves").json()["guarded"] is False
    assert len(agent.calls()) == 2


# --- Step 1: an off phrase refuses ----------------------------------------------------------------


@pytest.mark.parametrize(
    "prompt",
    [
        "how do I throw a hot dog party",
        # Off is checked before on: "salsa" is on the menu, "dance move" is not.
        "teach me some salsa dance moves",
        "recommend KARAOKE songs for a wedding",
        "how do I reverse a linked list",
    ],
)
def test_off_phrase_refuses_without_a_model_call(start_proxy, agent, guard, prompt):
    p = start_proxy(**TOPIC_ENV)
    assert p.chat(prompt).json() == REFUSED
    assert agent.calls() == []
    assert guard.calls() == [], "a phrase decides; the classifier is not asked"
    (rec,) = _records(p, "topic_guard_block")
    assert rec["step"] == "off_phrase"
    assert rec["level"] == "INFO"
    assert "off_phrase" in rec["message"]


def test_refusal_spends_nothing(start_proxy, agent):
    p = start_proxy(**TOPIC_ENV)
    p.chat("how do I throw a hot dog party")
    p.a2a("how do I throw a hot dog party")
    cost = p.get("/cost").json()
    assert (cost["requests"], cost["total_tokens"], cost["usd"]) == (0, 0, 0.0)


# --- Step 2: an on phrase passes ------------------------------------------------------------------


@pytest.mark.parametrize(
    "prompt", ["what are your hours", "do you have mild salsa", "how spicy is DRAGON'S BLOOD"]
)
def test_on_phrase_passes_without_a_classifier_call(start_proxy, agent, guard, prompt):
    guard.respond = _bans_topics
    p = start_proxy(**TOPIC_ENV)
    body = p.chat(prompt).json()
    assert body["reply"] == "Here is your burrito."
    assert body["guarded"] is False
    assert guard.calls() == []
    assert len(agent.calls()) == 1
    assert "topic_guard_block" not in p.events()


# --- Step 3: anything else goes to BanTopics ------------------------------------------------------


def test_classifier_refuses_an_off_topic_paraphrase(start_proxy, agent, guard):
    guard.respond = _bans_topics
    p = start_proxy(**TOPIC_ENV)
    assert p.chat("what's the capital of France").json() == REFUSED
    (call,) = guard.calls()
    assert call.path == "/analyze/prompt"
    # Only BanTopics speaks here; the injection classifier is its own stage with its own toggle.
    assert call.json() == {"prompt": "what's the capital of France", **TOPIC_SCAN}
    assert agent.calls() == []
    (rec,) = _records(p, "topic_guard_block")
    assert rec["step"] == "classifier"


def test_classifier_passes_a_vague_menu_ask(start_proxy, agent, guard):
    p = start_proxy(**TOPIC_ENV)
    assert p.chat("what do you recommend").json()["guarded"] is False
    assert [c.json() for c in guard.calls()] == [{"prompt": "what do you recommend", **TOPIC_SCAN}]
    assert len(agent.calls()) == 1


def test_classifier_verdict_without_is_valid_allows(start_proxy, agent, guard):
    guard.respond = lambda req: Reply(body={"scanners": {}})
    assert start_proxy(**TOPIC_ENV).chat("what do you recommend").json()["guarded"] is False


@pytest.mark.parametrize("failure", ["unreachable", "http_500", "bad_json"])
def test_classifier_failure_fails_closed_by_default(start_proxy, agent, guard, failure):
    env = dict(TOPIC_ENV)
    if failure == "unreachable":
        env["LLM_GUARD_URL"] = unreachable_url()
    elif failure == "http_500":
        guard.respond = lambda req: Reply(status=500, body={"detail": "boom"})
    else:
        guard.respond = lambda req: Reply(body=b"<html>")
    p = start_proxy(**env)
    assert p.chat("what do you recommend").json() == REFUSED
    assert agent.calls() == []
    assert [r["step"] for r in _records(p, "topic_guard_block")] == ["classifier"]


def test_classifier_failure_fails_open_when_configured(start_proxy, agent):
    p = start_proxy(**TOPIC_ENV, PROXY_FAIL_CLOSED="false", LLM_GUARD_URL=unreachable_url())
    assert p.chat("what do you recommend").json()["guarded"] is False
    assert len(agent.calls()) == 1


# --- The phrase rule: whole words, any case, optional plural "s" ----------------------------------


@pytest.mark.parametrize(
    "off,prompt,matched",
    [
        ("hot dog", "which Hot Dogs should I grill", True),
        ("song", "rank the top songs", True),
        ("rice", "what's the price", False),
        ("dance", "a dancer walks in", False),
        ("cha-cha", "show me the cha-cha", True),
        ("linked list", "reverse a linked-list", False),
        # Terms are stripped, empty ones dropped; an empty list matches nothing.
        (" karaoke ,, ", "karaoke night", True),
        ("", "karaoke night", False),
        (" , ", "karaoke night", False),
    ],
)
def test_phrase_rule(start_proxy, agent, guard, off, prompt, matched):
    p = start_proxy(TOPIC_GUARD="on", TOPIC_OFF_LIST=off, TOPIC_ON_LIST="", TOPIC_REFUSAL=REFUSAL)
    body = p.chat(prompt).json()
    if matched:
        assert body == REFUSED
        assert guard.calls() == []
    else:
        # Not an off phrase, and the on list is empty, so the (allowing) classifier decides.
        assert body["guarded"] is False
        assert len(guard.calls()) == 1


def test_unset_lists_leave_every_prompt_to_the_classifier(start_proxy, agent, guard):
    p = start_proxy(TOPIC_GUARD="on")
    p.chat("teach me some salsa dance moves")
    p.chat("what are your hours")
    assert len(guard.calls()) == 2


def test_refusal_text_has_a_default(start_proxy, agent, guard):
    guard.respond = lambda req: Reply(body=verdict(False))
    body = start_proxy(TOPIC_GUARD="on").chat("what's the capital of France").json()
    assert body == {
        **REFUSED,
        "reply": "BurritoBot only talks burritos: the menu, our hours, and our locations.",
    }


# --- Stage order: after both input stages, before the rate limit and cost cap ---------------------


def test_blocklist_runs_before_the_topic_guard(start_proxy, agent):
    p = start_proxy(**TOPIC_ENV, INPUT_BLOCKLIST="on", BLOCK_LIST="nuke")
    assert "blocked: 'nuke'" in p.chat("nuke the hot dog party").json()["reply"]
    assert "topic_guard_block" not in p.events()


def test_injection_classifier_runs_before_the_topic_guard(start_proxy, agent, guard):
    guard.respond = lambda req: Reply(body=verdict(False))
    p = start_proxy(**TOPIC_ENV, INPUT_CLASSIFIER="on")
    body = p.chat("what's the capital of France").json()
    assert body["reply"] == "BurritoBot can't help with that (blocked by the input guardrail)."
    assert [c.json() for c in guard.calls()] == [
        {"prompt": "what's the capital of France", **INJECTION_SCAN}
    ]


def test_both_classifiers_run_with_their_own_suppress_list(start_proxy, agent, guard):
    guard.respond = _bans_topics
    p = start_proxy(**TOPIC_ENV, INPUT_CLASSIFIER="on")
    assert p.chat("what's the capital of France").json() == REFUSED
    assert [c.json() for c in guard.calls()] == [
        {"prompt": "what's the capital of France", **INJECTION_SCAN},
        {"prompt": "what's the capital of France", **TOPIC_SCAN},
    ]


def test_topic_refusal_takes_no_rate_limit_slot(start_proxy, agent):
    p = start_proxy(**TOPIC_ENV, RATE_LIMIT_RPM="1")
    assert p.chat("how do I throw a hot dog party").json() == REFUSED
    assert p.a2a("how do I throw a hot dog party").status == 403
    assert p.chat("one burrito").json()["guarded"] is False


def test_topic_refusal_precedes_the_cost_cap(start_proxy, agent):
    p = start_proxy(**TOPIC_ENV, COST_CAP_USD="0.0001")
    p.chat("one burrito")  # spends past the cap
    assert p.chat("how do I throw a hot dog party").json() == REFUSED
    assert p.chat("one burrito").json()["reply"].startswith("The kitchen is closed:")


# --- The A2A path ---------------------------------------------------------------------------------


def test_a2a_off_phrase_is_403(start_proxy, agent, guard):
    p = start_proxy(**TOPIC_ENV)
    r = p.a2a("how do I throw a hot dog party", rpc_id="t1")
    assert r.status == 403
    assert r.json() == {"jsonrpc": "2.0", "id": "t1", "error": {"code": -32600, "message": REFUSAL}}
    assert agent.calls() == [] and guard.calls() == []
    assert [r["step"] for r in _records(p, "topic_guard_block")] == ["off_phrase"]


def test_a2a_classifier_refusal_is_403(start_proxy, agent, guard):
    guard.respond = _bans_topics
    p = start_proxy(**TOPIC_ENV)
    r = p.a2a("what's the capital of France", rpc_id=4)
    assert r.status == 403
    assert r.json()["error"] == {"code": -32600, "message": REFUSAL}
    assert [c.json() for c in guard.calls()] == [
        {"prompt": "what's the capital of France", **TOPIC_SCAN}
    ]
    assert [r["step"] for r in _records(p, "topic_guard_block")] == ["classifier"]


def test_a2a_on_phrase_is_forwarded(start_proxy, agent, guard):
    guard.respond = _bans_topics
    p = start_proxy(**TOPIC_ENV)
    assert p.a2a("can I see the menu").status == 200
    assert len(agent.calls()) == 1 and guard.calls() == []


def test_a2a_request_with_no_text_skips_the_topic_guard(start_proxy, agent, guard):
    p = start_proxy(**TOPIC_ENV)
    body = a2a_request("x")
    body["params"]["message"]["parts"] = [{"kind": "data", "data": {}}]
    assert p.post("/", body).status == 200
    assert guard.calls() == []
