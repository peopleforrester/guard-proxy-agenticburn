# ABOUTME: Conformance for the read and toggle surface: /guards, /toggle, /cost, /controls, the GET
# ABOUTME: passthrough to the agent, response CORS headers, and the deliberately absent OPTIONS.
from __future__ import annotations

import pytest

from conftest import http
from fakes import Reply, unreachable_url

GUARD_KEYS = {"input_blocklist", "input_classifier", "output", "budget"}


# --- /guards: seeded from the environment ---------------------------------------------------------


def test_guards_default_all_off(start_proxy):
    assert start_proxy().get("/guards").json() == dict.fromkeys(GUARD_KEYS, False)


@pytest.mark.parametrize(
    "env,expected_on",
    [
        ({"INPUT_BLOCKLIST": "on"}, {"input_blocklist"}),
        ({"INPUT_CLASSIFIER": "ON"}, {"input_classifier"}),
        ({"OUTPUT_GUARD": "on"}, {"output"}),
        ({"BUDGET_GUARD": "on"}, {"budget"}),
        # The legacy switch seeds both input stages at once.
        ({"INPUT_GUARD": "on"}, {"input_blocklist", "input_classifier"}),
        ({"INPUT_GUARD": "on", "INPUT_BLOCKLIST": "off"}, {"input_blocklist", "input_classifier"}),
        # Anything other than "on" is off.
        ({"OUTPUT_GUARD": "true"}, set()),
        ({"OUTPUT_GUARD": "1"}, set()),
    ],
)
def test_guards_seeded_from_env(start_proxy, env, expected_on):
    got = start_proxy(**env).get("/guards").json()
    assert {k for k, v in got.items() if v} == expected_on


def test_guards_matches_by_prefix(start_proxy):
    assert start_proxy(OUTPUT_GUARD="on").get("/guards?x=1").json()["output"] is True


# --- /toggle: runtime flips, no restart -----------------------------------------------------------


@pytest.mark.parametrize("key", sorted(GUARD_KEYS))
def test_toggle_each_guard_on_then_off(start_proxy, key):
    p = start_proxy()
    assert p.toggle(**{key: "on"})[key] is True
    assert p.get("/guards").json()[key] is True
    assert p.toggle(**{key: "off"})[key] is False
    assert p.get("/guards").json()[key] is False


def test_toggle_input_flips_both_input_stages(start_proxy):
    p = start_proxy()
    state = p.toggle(input="on")
    assert state["input_blocklist"] and state["input_classifier"]
    assert not state["output"] and not state["budget"]
    state = p.toggle(input="off")
    assert not state["input_blocklist"] and not state["input_classifier"]


def test_toggle_specific_key_overrides_input_convenience(start_proxy):
    state = start_proxy().toggle(input="on", input_classifier="off")
    assert state["input_blocklist"] is True
    assert state["input_classifier"] is False


def test_toggle_value_is_case_insensitive_and_non_on_means_off(start_proxy):
    p = start_proxy()
    assert p.toggle(output="ON")["output"] is True
    assert p.toggle(output="yes")["output"] is False


def test_toggle_ignores_unknown_keys_and_returns_state(start_proxy):
    p = start_proxy(OUTPUT_GUARD="on")
    r = p.get("/toggle?bogus=on")
    assert r.status == 200
    assert r.json() == {**dict.fromkeys(GUARD_KEYS, False), "output": True}


# --- /cost ----------------------------------------------------------------------------------------


def test_cost_initial_shape(start_proxy):
    assert start_proxy(MODEL_TIER="sonnet").get("/cost").json() == {
        "tier": "sonnet",
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "usd": 0.0,
        "cap_usd": 0,
        "budget_guard": False,
    }


def test_cost_tier_defaults_to_haiku(start_proxy):
    assert start_proxy().get("/cost").json()["tier"] == "haiku"


@pytest.mark.parametrize(
    "env,cap",
    [
        ({}, 0),
        ({"COST_CAP_USD": "25"}, 25.0),
        # The budget cap only counts while the budget guard is on; then the lower cap is reported.
        ({"BUDGET_CAP_USD": "0.05"}, 0),
        ({"BUDGET_GUARD": "on"}, 0.03),
        ({"BUDGET_GUARD": "on", "BUDGET_CAP_USD": "0.05", "COST_CAP_USD": "25"}, 0.05),
        ({"BUDGET_GUARD": "on", "BUDGET_CAP_USD": "30", "COST_CAP_USD": "25"}, 25.0),
    ],
)
def test_cost_reports_the_cap_that_bites_first(start_proxy, env, cap):
    body = start_proxy(**env).get("/cost").json()
    assert body["cap_usd"] == pytest.approx(cap)
    assert body["budget_guard"] is (env.get("BUDGET_GUARD") == "on")


def test_cost_budget_guard_follows_toggle(start_proxy):
    p = start_proxy(BUDGET_CAP_USD="0.07")
    p.toggle(budget="on")
    body = p.get("/cost").json()
    assert body["budget_guard"] is True
    assert body["cap_usd"] == pytest.approx(0.07)


# --- /controls ------------------------------------------------------------------------------------


def test_controls_without_cluster_credentials(start_proxy):
    """Outside a pod there is no ServiceAccount token, so every cluster read is 'could not read'.

    Absent objects read as not installed, except the two badges whose None means unknown, and Falco,
    which is always on. A cluster-backed case needs an API endpoint override that proxy.py does not
    have; the Go implementation adds one and extends this file.
    """
    p = start_proxy(INPUT_BLOCKLIST="on", WIB_PUBLIC_HOST="michael-student.agenticburn.com")
    assert p.get("/controls").json() == {
        "ai": {**dict.fromkeys(GUARD_KEYS, False), "input_blocklist": True},
        "infra": {
            "networkpolicy": False,
            "kyverno": False,
            "kubearmor": False,
            "falco": True,
            "tool_allowlist": None,
            "rbac_scoped": None,
        },
        "host": "michael-student.agenticburn.com",
    }


def test_controls_host_defaults_empty(start_proxy):
    assert start_proxy().get("/controls").json()["host"] == ""


# --- GET passthrough to the agent -----------------------------------------------------------------


def test_get_passthrough_forwards_path_and_body(start_proxy, agent):
    card = {"name": "workshop-agent", "skills": []}
    agent.respond = lambda req: Reply(body=card)
    p = start_proxy()
    r = p.get("/.well-known/agent-card.json?v=1")
    assert r.status == 200
    assert r.headers["content-type"] == "application/json"
    assert r.json() == card
    assert [c.path for c in agent.calls()] == ["/.well-known/agent-card.json?v=1"]
    assert agent.calls()[0].method == "GET"


def test_get_passthrough_agent_error_status_is_502(start_proxy, agent):
    agent.respond = lambda req: Reply(status=404, body={"error": "nope"})
    r = start_proxy().get("/missing")
    assert r.status == 502
    assert r.json()["error"].startswith("agent GET failed:")


def test_get_passthrough_agent_unreachable_is_502(start_proxy):
    r = start_proxy(AGENT_URL=unreachable_url()).get("/anything")
    assert r.status == 502
    assert r.json()["error"].startswith("agent GET failed:")


def test_admin_paths_never_reach_the_agent(start_proxy, agent):
    p = start_proxy()
    for path in ("/cost", "/prompts", "/guards", "/controls", "/toggle"):
        assert p.get(path).status == 200
    assert agent.calls() == []


# --- Response CORS (read access for the instructor console) ---------------------------------------

CONSOLE = "https://start.agenticburn.com"


@pytest.mark.parametrize(
    "origin,host,allowed",
    [
        (CONSOLE, None, True),
        ("https://evil.example", None, False),
        # The attendee's own page: Origin hostname equals the request Host (port ignored).
        ("https://michael-student.agenticburn.com", "michael-student.agenticburn.com:443", True),
        ("https://other-student.agenticburn.com", "michael-student.agenticburn.com", False),
    ],
)
def test_cors_echoes_only_allowed_origins(start_proxy, origin, host, allowed):
    headers = {"Origin": origin}
    if host:
        headers["Host"] = host
    r = start_proxy().get("/cost", headers=headers)
    assert r.status == 200
    if allowed:
        assert r.headers.get("access-control-allow-origin") == origin
        assert r.headers.get("vary") == "Origin"
    else:
        assert "access-control-allow-origin" not in r.headers


def test_cors_console_origin_is_configurable(start_proxy):
    p = start_proxy(CONSOLE_ORIGIN="https://console.example")
    assert (
        p.get("/cost", headers={"Origin": "https://console.example"}).headers.get(
            "access-control-allow-origin"
        )
        == "https://console.example"
    )
    assert "access-control-allow-origin" not in p.get("/cost", headers={"Origin": CONSOLE}).headers


def test_cors_no_origin_no_header(start_proxy):
    assert "access-control-allow-origin" not in start_proxy().get("/cost").headers


def test_foreign_origin_post_is_not_rejected(start_proxy):
    """Origin enforcement was removed (it broke every browser behind the apex router); see #151."""
    p = start_proxy()
    r = p.post("/chat", {"prompt": "one burrito"}, headers={"Origin": "https://evil.example"})
    assert r.status == 200
    assert "access-control-allow-origin" not in r.headers


def test_options_preflight_is_not_implemented(start_proxy):
    """No OPTIONS handler, so a cross-origin preflight fails: a second layer under the Origin echo."""
    p = start_proxy()
    assert http("OPTIONS", p.url + "/chat").status == 501
