# ABOUTME: Conformance for spend control: token metering into /cost, pricing at the bound tier, the rate
# ABOUTME: limit, the cluster-wide cost cap, the per-session budget cap (C4), and their ordering.
from __future__ import annotations

import time

import pytest

from conftest import SA_TOKEN, a2a_request
from fakes import Reply, a2a_result

# Haiku list price: $0.001/1K in, $0.005/1K out. The default agent reply is 100 in / 50 out.
HAIKU_DEFAULT_REPLY_USD = 0.1 * 0.001 + 0.05 * 0.005


def _reply_costing(prompt_tokens=1000, output_tokens=1000):
    return lambda req: Reply(
        body=a2a_result(prompt_tokens=prompt_tokens, output_tokens=output_tokens)
    )


# --- Metering -------------------------------------------------------------------------------------


def test_cost_accumulates_from_both_paths(start_proxy, agent):
    p = start_proxy()
    p.chat("hi")
    p.a2a("hi")
    body = p.get("/cost").json()
    assert body["requests"] == 2
    assert (body["input_tokens"], body["output_tokens"], body["total_tokens"]) == (200, 100, 300)
    assert body["usd"] == pytest.approx(2 * HAIKU_DEFAULT_REPLY_USD)


def test_total_tokens_prefers_reported_total(start_proxy, agent):
    agent.respond = lambda req: Reply(
        body=a2a_result(prompt_tokens=10, output_tokens=5, total_tokens=40)
    )
    p = start_proxy()
    p.chat("hi")
    assert p.get("/cost").json()["total_tokens"] == 40


def test_reply_without_usage_is_not_counted(start_proxy, agent):
    agent.respond = lambda req: Reply(body=a2a_result(prompt_tokens=None, output_tokens=None))
    p = start_proxy()
    p.chat("hi")
    p.a2a("hi")
    assert p.get("/cost").json()["requests"] == 0


@pytest.mark.parametrize("usage_key", ["kagent_usage_metadata", "adk_usage_metadata"])
@pytest.mark.parametrize("in_status", [False, True])
def test_metering_reads_either_key_and_location(start_proxy, agent, usage_key, in_status):
    agent.respond = lambda req: Reply(
        body=a2a_result(
            prompt_tokens=1000, output_tokens=0, usage_key=usage_key, usage_in_status=in_status
        )
    )
    p = start_proxy()
    p.a2a("hi")
    assert p.get("/cost").json()["input_tokens"] == 1000


@pytest.mark.parametrize(
    "tier,usd",
    [
        ("nova", 0.0008 + 0.0032),
        ("haiku", 0.001 + 0.005),
        ("sonnet", 0.003 + 0.015),
        ("opus", 0.005 + 0.025),
        ("SONNET", 0.003 + 0.015),
        ("unknown-tier", 0.001 + 0.005),  # unknown tiers price as haiku
    ],
)
def test_tier_pricing_per_1k_tokens(start_proxy, agent, tier, usd):
    agent.respond = _reply_costing(1000, 1000)
    p = start_proxy(MODEL_TIER=tier)
    assert p.chat("hi").json()["cost_usd"] == pytest.approx(usd)
    body = p.get("/cost").json()
    assert body["usd"] == pytest.approx(usd)
    assert body["tier"] == tier.lower()


def test_explicit_price_overrides_tier(start_proxy, agent):
    agent.respond = _reply_costing(1000, 1000)
    p = start_proxy(MODEL_TIER="opus", COST_PER_1K_IN="1", COST_PER_1K_OUT="2")
    assert p.chat("hi").json()["cost_usd"] == pytest.approx(3.0)


# --- Pricing at the model the workshop-agent is bound to (guard-proxy-agenticburn#37) ---------------
# The workshop swaps the model live by patching the Agent's spec.declarative.modelConfig to
# bedrock-<tier>, which Argo CD leaves out of its diff. MODEL_TIER is read once at start, so each request
# is priced at the tier the Agent names, read through the API with the badges' service account token.
# The read is cached as briefly as the badges' (5 s), so a swap is priced within that window.

AGENT_CR = "/apis/kagent.dev/v1alpha2/namespaces/agent/agents/workshop-agent"
BINDING_TTL = 5.0
# USD for the 1,000 in and 1,000 out tokens _reply_costing answers with by default, per tier.
TIER_USD = {
    "nova": 0.0008 + 0.0032,
    "haiku": 0.001 + 0.005,
    "sonnet": 0.003 + 0.015,
    "opus": 0.005 + 0.025,
}


def bound_agent(model_config: str | None) -> dict:
    """The workshop-agent Agent, bound to `model_config`, or with no modelConfig at all for None."""
    declarative: dict = {"tools": []}
    if model_config is not None:
        declarative["modelConfig"] = model_config
    return {"kind": "Agent", "spec": {"declarative": declarative}}


@pytest.mark.parametrize("tier", ["nova", "haiku", "sonnet", "opus"])
def test_requests_are_priced_at_the_bound_model_not_model_tier(start_proxy, agent, kube_api, tier):
    agent.respond = _reply_costing(1000, 1000)
    kube_api.objects[AGENT_CR] = bound_agent(f"bedrock-{tier}")
    start_tier = "opus" if tier != "opus" else "nova"
    p = start_proxy(**kube_api.env, MODEL_TIER=start_tier)
    assert p.chat("hi").json()["cost_usd"] == pytest.approx(TIER_USD[tier])
    p.a2a("hi")
    body = p.get("/cost").json()
    assert body["usd"] == pytest.approx(2 * TIER_USD[tier])
    assert body["tier"] == tier


def test_cost_reports_the_bound_tier_before_any_request(start_proxy, kube_api):
    kube_api.objects[AGENT_CR] = bound_agent("bedrock-opus")
    assert start_proxy(**kube_api.env, MODEL_TIER="nova").get("/cost").json()["tier"] == "opus"


def test_a_live_swap_is_priced_at_the_new_model(start_proxy, agent, kube_api):
    """The swap Unleash_an_Agent_Watch_It_Burn#130 teaches: Nova Pro to Opus on a running cluster."""
    agent.respond = _reply_costing(1000, 1000)
    kube_api.objects[AGENT_CR] = bound_agent("bedrock-nova")
    p = start_proxy(**kube_api.env, MODEL_TIER="nova")
    assert p.chat("hi").json()["cost_usd"] == pytest.approx(TIER_USD["nova"])
    kube_api.objects[AGENT_CR] = bound_agent("bedrock-opus")
    time.sleep(BINDING_TTL + 0.5)
    assert p.chat("hi").json()["cost_usd"] == pytest.approx(TIER_USD["opus"])
    body = p.get("/cost").json()
    # Spend already metered keeps the price it was metered at.
    assert body["usd"] == pytest.approx(TIER_USD["nova"] + TIER_USD["opus"])
    assert body["tier"] == "opus"
    assert p.events().count("price_tier") == 2


def test_an_unreadable_agent_keeps_the_last_bound_tier(start_proxy, agent, kube_api):
    """A failed read says nothing about the binding, so it does not put a swapped cluster back on
    MODEL_TIER."""
    agent.respond = _reply_costing(1000, 1000)
    kube_api.objects[AGENT_CR] = bound_agent("bedrock-opus")
    p = start_proxy(**kube_api.env, MODEL_TIER="nova")
    p.chat("hi")
    del kube_api.objects[AGENT_CR]
    time.sleep(BINDING_TTL + 0.5)
    assert p.chat("hi").json()["cost_usd"] == pytest.approx(TIER_USD["opus"])
    assert len(kube_api.calls(AGENT_CR)) == 2
    assert p.get("/cost").json()["tier"] == "opus"


def test_without_a_cluster_requests_are_priced_at_model_tier(start_proxy, agent, tmp_path):
    agent.respond = _reply_costing(1000, 1000)
    p = start_proxy(MODEL_TIER="sonnet", K8S_TOKEN_PATH=str(tmp_path / "no-token"))
    assert p.chat("hi").json()["cost_usd"] == pytest.approx(TIER_USD["sonnet"])
    assert p.get("/cost").json()["tier"] == "sonnet"
    assert "price_tier" not in p.events()


@pytest.mark.parametrize(
    "model_config",
    [
        "gateway-local-qwen3",  # a ModelConfig with no row in the price table
        "bedrock-mystery",
        "BEDROCK-OPUS",  # Kubernetes names are lowercase, so this is no ModelConfig of ours
        None,  # the Agent names no ModelConfig
    ],
)
def test_a_model_config_with_no_tier_is_priced_at_model_tier(
    start_proxy, agent, kube_api, model_config
):
    agent.respond = _reply_costing(1000, 1000)
    kube_api.objects[AGENT_CR] = bound_agent(model_config)
    p = start_proxy(**kube_api.env, MODEL_TIER="sonnet")
    assert p.chat("hi").json()["cost_usd"] == pytest.approx(TIER_USD["sonnet"])
    p.chat("hi")
    assert p.get("/cost").json()["tier"] == "sonnet"
    # Said once, at the read that found it, not on every request.
    assert p.events().count("price_tier_unknown") == 1
    assert "price_tier" not in p.events()


def test_the_binding_is_read_once_per_cache_window(start_proxy, agent, kube_api):
    """One GET of the Agent by name, with the service account token, serves every request and /cost
    poll in the window, so a busy room does not load the API server."""
    kube_api.objects[AGENT_CR] = bound_agent("bedrock-haiku")
    p = start_proxy(**kube_api.env, MODEL_TIER="nova")
    p.chat("hi")
    p.a2a("hi")
    p.chat("hi", session="s1")
    p.get("/cost")
    [call] = kube_api.calls(AGENT_CR)
    assert call.method == "GET"
    assert call.headers["Authorization"] == f"Bearer {SA_TOKEN}"
    assert p.events().count("price_tier") == 1


def test_an_explicit_price_still_wins_over_the_bound_tier(start_proxy, agent, kube_api):
    """COST_PER_1K_IN and COST_PER_1K_OUT each fix their own price; the other follows the bound tier."""
    agent.respond = _reply_costing(1000, 1000)
    kube_api.objects[AGENT_CR] = bound_agent("bedrock-opus")
    p = start_proxy(**kube_api.env, MODEL_TIER="nova", COST_PER_1K_IN="1")
    assert p.chat("hi").json()["cost_usd"] == pytest.approx(1 + 0.025)


def test_a_retried_turn_reports_what_each_attempt_cost(start_proxy, agent, kube_api):
    agent.respond = _queue(
        Reply(body=a2a_result("<thinking>hmm</thinking>", prompt_tokens=1000, output_tokens=0)),
        Reply(body=a2a_result("Done.", prompt_tokens=1000, output_tokens=0)),
    )
    kube_api.objects[AGENT_CR] = bound_agent("bedrock-sonnet")
    p = start_proxy(**kube_api.env, MODEL_TIER="nova")
    assert p.chat("hi", session="s1").json()["cost_usd"] == pytest.approx(2 * 0.003)
    assert p.get("/cost").json()["usd"] == pytest.approx(2 * 0.003)


def test_blocked_requests_cost_nothing(start_proxy, agent):
    p = start_proxy(INPUT_BLOCKLIST="on")
    p.chat("delete everything")
    p.a2a("delete everything")
    assert p.get("/cost").json()["usd"] == 0.0


# Every model call is billed by the provider, so a self-heal retry meters both attempts
# (guard-proxy-agenticburn#3). /cost counts model calls, and the turn reports what it spent in all.

DANGLING = (
    "ValidationException: messages.3: `tool_use` ids were found without `tool_result` "
    "blocks immediately after"
)


def _queue(*replies):
    queue = list(replies)
    return lambda req: queue.pop(0)


@pytest.mark.parametrize(
    "first,second",
    [
        ("<thinking>hmm</thinking>", "Done."),  # the retry recovers
        ("<thinking>hmm</thinking>", "<thinking>still</thinking>"),  # both reasoning-only
        (DANGLING, "Fresh start."),  # the context rotation
    ],
)
def test_retry_meters_every_attempt(start_proxy, agent, first, second):
    agent.respond = _queue(
        Reply(body=a2a_result(first, prompt_tokens=1000, output_tokens=200)),
        Reply(body=a2a_result(second, prompt_tokens=3000, output_tokens=400)),
    )
    p = start_proxy(MODEL_TIER="haiku")
    turn = p.chat("hi", session="s1").json()
    assert len(agent.calls()) == 2
    body = p.get("/cost").json()
    assert (body["requests"], body["input_tokens"], body["output_tokens"]) == (2, 4000, 600)
    usd = 4.0 * 0.001 + 0.6 * 0.005
    assert body["usd"] == pytest.approx(usd)
    assert (turn["input_tokens"], turn["output_tokens"]) == (4000, 600)
    assert turn["cost_usd"] == pytest.approx(usd)


def test_failed_retry_still_meters_the_first_attempt(start_proxy, agent):
    agent.respond = _queue(
        Reply(body=a2a_result(DANGLING, prompt_tokens=1000, output_tokens=200)),
        Reply(status=500, body={}),
    )
    p = start_proxy()
    assert p.chat("hi", session="s1").status == 502
    body = p.get("/cost").json()
    assert (body["requests"], body["input_tokens"]) == (1, 1000)


def test_retry_spend_counts_toward_the_session_budget(start_proxy, agent):
    # Two attempts at $0.003 each make $0.006, past a $0.005 cap that one attempt would not reach.
    agent.respond = _queue(
        Reply(body=a2a_result("<thinking>hmm</thinking>", prompt_tokens=0, output_tokens=600)),
        Reply(body=a2a_result("Done.", prompt_tokens=0, output_tokens=600)),
    )
    p = start_proxy(BUDGET_GUARD="on", BUDGET_CAP_USD="0.005")
    p.chat("1", session="a")
    assert p.chat("2", session="a").json()["guarded"] is True
    assert len(agent.calls()) == 2


# --- Rate limit (requests per minute, shared by both paths) ---------------------------------------


def test_rate_limit_a2a(start_proxy, agent):
    p = start_proxy(RATE_LIMIT_RPM="2")
    assert p.a2a("1").status == 200
    assert p.a2a("2").status == 200
    r = p.a2a("3", rpc_id=33)
    assert r.status == 429
    assert r.json() == {
        "jsonrpc": "2.0",
        "id": 33,
        "error": {
            "code": -32000,
            "message": "Rate limit reached (2/min on this cluster). Slow down; the cost demo will not "
            "run away.",
        },
    }
    assert len(agent.calls()) == 2


def test_rate_limit_chat_is_200_guarded(start_proxy, agent):
    p = start_proxy(RATE_LIMIT_RPM="1")
    p.chat("1")
    assert p.chat("2").json() == {
        "reply": "Slow down, hungry traveler. (1/min cap on this cluster.)",
        "guarded": True,
        "input_tokens": 0,
        "output_tokens": 0,
    }


# --- Rate limit per conversation (a ballroom of phones on one bot) --------------------------------
# One phone must not spend the room's shared window. Each /chat session gets its own 60s window, checked
# first, so a refused prompt does not take a slot from everyone else (Unleash_an_Agent_Watch_It_Burn#450).


def test_session_rate_limit_refuses_that_session_only(start_proxy, agent):
    p = start_proxy(RATE_LIMIT_SESSION_RPM="2")
    assert p.chat("1", session="a").json()["guarded"] is False
    assert p.chat("2", session="a").json()["guarded"] is False
    assert p.chat("3", session="a").json() == {
        "reply": "Slow down, hungry traveler. (2/min per conversation.)",
        "guarded": True,
        "input_tokens": 0,
        "output_tokens": 0,
    }
    assert p.chat("4", session="b").json()["guarded"] is False
    assert len(agent.calls()) == 3


def test_session_rate_limit_spares_the_cluster_window(start_proxy, agent):
    p = start_proxy(RATE_LIMIT_SESSION_RPM="1", RATE_LIMIT_RPM="2")
    p.chat("1", session="a")
    for i in range(3):
        assert p.chat(f"again {i}", session="a").json()["guarded"] is True
    # a's refusals took no slot: b still has the cluster's second one.
    assert p.chat("2", session="b").json()["guarded"] is False
    assert "on this cluster" in p.chat("3", session="c").json()["reply"]


def test_session_rate_limit_skips_requests_without_a_session(start_proxy, agent):
    p = start_proxy(RATE_LIMIT_SESSION_RPM="1")
    for i in range(3):
        assert p.chat(str(i)).json()["guarded"] is False


def test_session_rate_limit_is_off_by_default(start_proxy, agent):
    p = start_proxy()
    for i in range(5):
        assert p.chat(str(i), session="a").json()["guarded"] is False


def test_rate_limit_is_shared_across_paths(start_proxy, agent):
    p = start_proxy(RATE_LIMIT_RPM="2")
    p.chat("1")
    p.a2a("2")
    assert p.chat("3").json()["guarded"] is True
    assert p.a2a("4").status == 429


def test_rate_limit_counts_only_requests_that_pass_input_guards(start_proxy, agent):
    p = start_proxy(RATE_LIMIT_RPM="1", INPUT_BLOCKLIST="on")
    for _ in range(3):
        assert p.chat("nuke it").json()["guarded"] is True
    p.post("/chat", {"prompt": ""})
    for path in ("/cost", "/guards", "/prompts"):
        p.get(path)
    assert p.chat("one burrito").json()["guarded"] is False
    assert p.chat("another").json()["reply"].startswith("Slow down")


def test_rate_limit_zero_means_unlimited(start_proxy, agent):
    p = start_proxy(RATE_LIMIT_RPM="0")
    assert all(p.a2a(str(i)).status == 200 for i in range(5))


def test_a2a_without_json_body_gets_null_id_on_429(start_proxy, agent):
    p = start_proxy(RATE_LIMIT_RPM="1")
    p.post("/", b"raw")
    r = p.post("/", b"raw")
    assert r.status == 429
    assert r.json()["id"] is None


# --- Cluster-wide cost cap (the infra backstop) ---------------------------------------------------


def test_cost_cap_a2a(start_proxy, agent):
    agent.respond = _reply_costing()  # $0.006 per reply at haiku
    p = start_proxy(COST_CAP_USD="0.01")
    assert p.a2a("1").status == 200
    assert p.a2a("2").status == 200
    r = p.a2a("3", rpc_id="x")
    assert r.status == 429
    assert r.json() == {
        "jsonrpc": "2.0",
        "id": "x",
        "error": {
            "code": -32000,
            "message": "Cost cap reached ($0.01 on this cluster). Spend is frozen for everyone; a "
            "blocked request costs nothing.",
        },
    }
    assert len(agent.calls()) == 2


def test_cost_cap_chat_is_cluster_wide_across_sessions(start_proxy, agent):
    agent.respond = _reply_costing()
    p = start_proxy(COST_CAP_USD="0.01")
    p.chat("1", session="a")
    p.chat("2", session="b")
    body = p.chat("3", session="c").json()
    assert body == {
        "reply": "The kitchen is closed: this cluster hit its $0.01 spend cap, so BurritoBot isn't "
        "sending anything else to the model for anyone. A blocked request costs nothing.",
        "guarded": True,
        "input_tokens": 0,
        "output_tokens": 0,
    }
    assert len(agent.calls()) == 2


def test_runtime_cost_cap_trips_both_paths_and_default_releases_them(start_proxy, agent):
    """The KubeCon finale: the presenters lower the cap live, the room trips it, then it is restored."""
    agent.respond = _reply_costing()
    p = start_proxy(COST_CAP_USD="25")
    assert p.chat("1", session="a").json()["guarded"] is False
    assert p.chat("2", session="b").json()["guarded"] is False
    p.toggle(cost_cap="0.01")
    body = p.chat("3", session="c").json()
    assert body["guarded"] is True
    assert body["reply"].startswith("The kitchen is closed: this cluster hit its $0.01 spend cap")
    r = p.a2a("4")
    assert r.status == 429
    assert r.json()["error"]["message"].startswith("Cost cap reached ($0.01 on this cluster)")
    assert len(agent.calls()) == 2
    # The tally survives the change: the cap moved, the counter did not reset.
    assert p.get("/cost").json()["usd"] == pytest.approx(0.012)
    p.toggle(cost_cap="default")
    assert p.chat("5", session="c").json()["guarded"] is False
    assert len(agent.calls()) == 3


def test_runtime_cost_cap_trips_at_exactly_the_cap(start_proxy, agent):
    agent.respond = _reply_costing(1000, 0)
    p = start_proxy(COST_CAP_USD="25", **EXACT)
    p.toggle(cost_cap="2")
    assert p.a2a("1").status == 200
    assert p.a2a("2").status == 200
    assert p.a2a("3").status == 429


def test_rate_limit_is_checked_before_cost_cap(start_proxy, agent):
    agent.respond = _reply_costing()
    p = start_proxy(COST_CAP_USD="0.001", RATE_LIMIT_RPM="1")
    p.a2a("1")
    assert "Rate limit" in p.a2a("2").json()["error"]["message"]


# --- Per-session budget cap (Challenge 4) ---------------------------------------------------------


def test_budget_cap_is_per_session(start_proxy, agent):
    agent.respond = _reply_costing()
    p = start_proxy(BUDGET_GUARD="on", BUDGET_CAP_USD="0.01")
    p.chat("1", session="a")
    p.chat("2", session="a")
    capped = p.chat("3", session="a").json()
    assert capped == {
        "reply": "The kitchen tab is frozen. This conversation hit its $0.01 spend budget, so I'm not "
        "sending anything else to the model. A blocked request costs nothing. Press Reset for "
        "a fresh conversation; the budget guard stays on.",
        "guarded": True,
        "input_tokens": 0,
        "output_tokens": 0,
    }
    # A fresh session (Reset in the storefront) gets a fresh budget with the guard still armed.
    assert p.chat("4", session="b").json()["guarded"] is False
    assert p.get("/cost").json()["usd"] == pytest.approx(0.018)


def test_budget_cap_default_is_three_cents(start_proxy, agent):
    agent.respond = _reply_costing()
    p = start_proxy(BUDGET_GUARD="on")
    for i in range(5):  # $0.030 after five replies
        assert p.chat(str(i), session="a").json()["guarded"] is False
    assert "$0.03 spend budget" in p.chat("6", session="a").json()["reply"]


def test_budget_cap_without_session_uses_cluster_total(start_proxy, agent):
    agent.respond = _reply_costing()
    p = start_proxy(BUDGET_GUARD="on", BUDGET_CAP_USD="0.01")
    p.chat("1", session="a")
    p.chat("2", session="b")
    assert p.chat("3").json()["guarded"] is True
    r = p.a2a("4")
    assert r.status == 429
    assert r.json()["error"]["message"] == (
        "Budget cap reached ($0.01 on this cluster). Spend is frozen; a blocked request costs nothing."
    )


@pytest.mark.parametrize("session", [{"id": "a"}, ["a"]])
def test_budget_cap_with_a_non_string_session_uses_cluster_total(start_proxy, agent, session):
    agent.respond = _reply_costing()
    p = start_proxy(BUDGET_GUARD="on", BUDGET_CAP_USD="0.01")
    p.chat("1", session="a")
    p.chat("2", session="b")
    assert p.post("/chat", {"prompt": "3", "session": session}).json()["guarded"] is True


def test_budget_cap_off_does_not_bind(start_proxy, agent):
    agent.respond = _reply_costing()
    p = start_proxy(BUDGET_CAP_USD="0.001")
    for i in range(3):
        assert p.chat(str(i), session="a").json()["guarded"] is False


def test_session_spend_accrues_while_budget_guard_is_off(start_proxy, agent):
    agent.respond = _reply_costing()
    p = start_proxy(BUDGET_CAP_USD="0.01")
    p.chat("1", session="a")
    p.chat("2", session="a")
    p.toggle(budget="on")
    assert p.chat("3", session="a").json()["guarded"] is True
    p.toggle(budget="off")
    assert p.chat("4", session="a").json()["guarded"] is False


def test_cap_message_names_the_cluster_cap_when_it_tripped_with_the_budget_guard_on(start_proxy, agent):
    """Reset cannot lift the cluster-wide cap, so the reply must not tell the room to press it."""
    agent.respond = _reply_costing()
    p = start_proxy(BUDGET_GUARD="on", BUDGET_CAP_USD="1", COST_CAP_USD="0.01")
    p.chat("1", session="a")
    p.chat("2", session="b")
    body = p.chat("3", session="c").json()
    assert body["guarded"] is True
    assert body["reply"].startswith("The kitchen is closed: this cluster hit its $0.01 spend cap")
    assert "Reset" not in body["reply"]
    assert p.a2a("4").json()["error"]["message"].startswith("Cost cap reached ($0.01 on this cluster)")


def test_cluster_cap_reply_wins_when_both_caps_tripped(start_proxy, agent):
    agent.respond = _reply_costing()
    p = start_proxy(BUDGET_GUARD="on", BUDGET_CAP_USD="0.01", COST_CAP_USD="0.016")
    p.chat("1", session="a")
    p.chat("2", session="a")
    assert p.chat("3", session="a").json()["reply"].startswith("The kitchen tab is frozen.")
    p.chat("4", session="b")  # $0.018 on the cluster: now both caps hold session a
    body = p.chat("5", session="a").json()
    assert body["reply"].startswith("The kitchen is closed: this cluster hit its $0.02 spend cap")


def test_a2a_blocked_input_spends_nothing_and_skips_caps(start_proxy, agent):
    p = start_proxy(INPUT_BLOCKLIST="on", COST_CAP_USD="0.000001")
    agent.respond = _reply_costing()
    p.a2a("ok")
    r = p.post("/", a2a_request("wipe it"))
    assert r.status == 403, "input guards run before the caps"


# --- Cap boundaries: reaching a cap exactly is enough to trip it ------------------------------------
# Prices chosen so each reply costs exactly $1.00 in binary floating point, which keeps the boundary
# itself under test instead of rounding noise around it.
EXACT = {"COST_PER_1K_IN": "1", "COST_PER_1K_OUT": "0"}


def test_cost_cap_trips_at_exactly_the_cap(start_proxy, agent):
    agent.respond = _reply_costing(1000, 0)
    p = start_proxy(COST_CAP_USD="2", **EXACT)
    assert p.a2a("1").status == 200
    assert p.a2a("2").status == 200
    assert p.get("/cost").json()["usd"] == 2.0
    assert p.a2a("3").status == 429


def test_budget_cap_trips_at_exactly_the_cap(start_proxy, agent):
    agent.respond = _reply_costing(1000, 0)
    p = start_proxy(BUDGET_GUARD="on", BUDGET_CAP_USD="1", **EXACT)
    assert p.chat("1", session="a").json()["guarded"] is False
    assert p.chat("2", session="a").json()["guarded"] is True


def test_rate_limit_allows_exactly_the_limit(start_proxy, agent):
    p = start_proxy(RATE_LIMIT_RPM="3")
    assert [p.a2a(str(i)).status for i in range(4)] == [200, 200, 200, 429]
