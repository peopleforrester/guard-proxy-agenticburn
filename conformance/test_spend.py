# ABOUTME: Conformance for spend control: token metering into /cost, tier pricing, the per-minute rate
# ABOUTME: limit, the cluster-wide cost cap, the per-session budget cap (C4), and their ordering.
from __future__ import annotations

import pytest

from conftest import a2a_request
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


def test_blocked_requests_cost_nothing(start_proxy, agent):
    p = start_proxy(INPUT_BLOCKLIST="on")
    p.chat("delete everything")
    p.a2a("delete everything")
    assert p.get("/cost").json()["usd"] == 0.0


def test_retry_tokens_are_not_metered_today(start_proxy, agent):
    """Known defect, guard-proxy-agenticburn#3: only the final reply of a self-heal retry is metered.

    The first attempt was billed by the model provider, so /cost and the budget cap undercount.
    """
    first = a2a_result("<thinking>hmm</thinking>", prompt_tokens=1000, output_tokens=1000)
    second = a2a_result("Done.", prompt_tokens=1000, output_tokens=1000)
    queue = [first, second]
    agent.respond = lambda req: Reply(body=queue.pop(0))
    p = start_proxy()
    p.chat("hi")
    assert len(agent.calls()) == 2
    body = p.get("/cost").json()
    assert (body["requests"], body["input_tokens"]) == (1, 1000)


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
            "message": "Budget cap reached ($0.01 on this cluster). Spend is frozen; a blocked request "
            "costs nothing.",
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
        "reply": "The kitchen tab is frozen. This conversation hit its $0.01 spend budget, so I'm not "
        "sending anything else to the model. A blocked request costs nothing. Press Reset for "
        "a fresh conversation; the budget guard stays on.",
        "guarded": True,
        "input_tokens": 0,
        "output_tokens": 0,
    }
    assert len(agent.calls()) == 2


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
    assert capped["guarded"] is True and "$0.01 spend budget" in capped["reply"]
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
    assert "$0.01" in r.json()["error"]["message"]


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


def test_cap_message_names_budget_cap_whenever_budget_guard_is_on(start_proxy, agent):
    """With the budget guard on, the message quotes BUDGET_CAP_USD even if COST_CAP_USD tripped."""
    agent.respond = _reply_costing()
    p = start_proxy(BUDGET_GUARD="on", BUDGET_CAP_USD="1", COST_CAP_USD="0.01")
    p.chat("1", session="a")
    p.chat("2", session="b")
    body = p.chat("3", session="c").json()
    assert body["guarded"] is True
    assert "$1.00 spend budget" in body["reply"]


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
