# ABOUTME: Conformance for the read and toggle surface: /guards, /toggle, /cost, /controls, the GET
# ABOUTME: passthrough to the agent, response CORS headers, and the deliberately absent OPTIONS.
from __future__ import annotations

import pytest

from conftest import http
from fakes import FakeUpstream, Recorded, Reply, unreachable_url

# The keys the Go build reports so far. The on-topic guard (`topic`) is in proxy.py only; its seeding
# and toggling are pinned in test_topic_guard.py.
BUILT_KEYS = {"input_blocklist", "input_classifier", "output", "budget"}
GUARD_KEYS = BUILT_KEYS | {"topic"}


# --- /guards: seeded from the environment ---------------------------------------------------------


@pytest.mark.passthrough
def test_guards_default_all_off(start_proxy):
    got = start_proxy().get("/guards").json()
    assert set(got) >= BUILT_KEYS
    assert not any(got.values())


def test_guards_reports_every_key(start_proxy):
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
@pytest.mark.passthrough
def test_guards_seeded_from_env(start_proxy, env, expected_on):
    got = start_proxy(**env).get("/guards").json()
    assert {k for k, v in got.items() if v} == expected_on


@pytest.mark.passthrough
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
    assert not state["output"] and not state["budget"] and not state["topic"]
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

    Absent objects read as not installed, except the two badges whose None means unknown. The
    cluster-backed cases below point K8S_API_URL at a fake API server.
    """
    p = start_proxy(INPUT_BLOCKLIST="on", WIB_PUBLIC_HOST="michael-student.agenticburn.com")
    assert p.get("/controls").json() == {
        "ai": {**dict.fromkeys(GUARD_KEYS, False), "input_blocklist": True},
        "infra": {
            "networkpolicy": False,
            "kyverno": False,
            "kubearmor": False,
            "falco": False,
            "tool_allowlist": None,
            "rbac_scoped": None,
        },
        "host": "michael-student.agenticburn.com",
    }


def test_controls_host_defaults_empty(start_proxy):
    assert start_proxy().get("/controls").json()["host"] == ""


# --- /controls C7: the rogue evil-mcp tools are blocked ---------------------------------------------
# Two controls can block them. On the KubeCon admin cluster it is agentgateway's mcpAuthorization rule
# on the :3003 bind evil-mcp is dialed through; the contract (Unleash_an_Agent_Watch_It_Burn #514) is
# ConfigMap agent/agentgateway-c7-config, key config.yaml, the one line ending `# c7-rule`, where
# 'true' is open and 'mcp.tool.name == "get_weather"' is narrowed. On a workshop cluster that rule
# stays open and the control is the kagent Agent's toolNames for evil-mcp.

SA_TOKEN = "conformance-sa-token"
C7_CONFIGMAP = "/api/v1/namespaces/agent/configmaps/agentgateway-c7-config"
AGENT_CR = "/apis/kagent.dev/v1alpha2/namespaces/agent/agents/workshop-agent"
ALL_EVIL_TOOLS = ["get_weather", "read_internal_config", "apply_optimization"]
RULE_OPEN = "- 'true' # c7-rule"
RULE_NARROWED = """- 'mcp.tool.name == "get_weather"' # c7-rule"""


def c7_config(rule_line: str) -> str:
    """agentgateway-c7-config's config.yaml in the shape #514 commits, with `rule_line` as the rule."""
    return (
        "binds:\n"
        "- port: 3003\n"
        "  listeners:\n"
        "  - protocol: HTTP\n"
        "    routes:\n"
        "    - policies:\n"
        "        mcpAuthorization:\n"
        "          rules:\n"
        f"          {rule_line}\n"
        "      backends:\n"
        "      - mcp:\n"
        "          targets:\n"
        "          - name: evil-mcp\n"
        "            mcp:\n"
        "              host: http://evil-mcp-shim.agent.svc.cluster.local:8000/mcp\n"
    )


def configmap(config_yaml: str) -> dict:
    return {"kind": "ConfigMap", "data": {"config.yaml": config_yaml}}


def workshop_agent(evil_tool_names: list[str]) -> dict:
    return {
        "kind": "Agent",
        "spec": {
            "declarative": {
                "tools": [
                    {"type": "McpServer", "mcpServer": {"name": "workshop-mcp", "toolNames": []}},
                    {
                        "type": "McpServer",
                        "mcpServer": {"name": "evil-mcp", "toolNames": evil_tool_names},
                    },
                ]
            }
        },
    }


@pytest.fixture
def kube_api(tmp_path):
    """A fake Kubernetes API serving `objects` by path, 404 for anything else, and a token file."""
    objects: dict[str, dict] = {}

    def respond(req: Recorded) -> Reply:
        if req.path in objects:
            return Reply(body=objects[req.path])
        return Reply(status=404, body={"kind": "Status", "reason": "NotFound"})

    up = FakeUpstream(respond)
    up.objects = objects
    token = tmp_path / "sa-token"
    token.write_text(SA_TOKEN + "\n")
    up.env = {"K8S_API_URL": up.url, "K8S_TOKEN_PATH": str(token)}
    yield up
    up.close()


@pytest.mark.parametrize(
    "rule_line,evil_tools,expected",
    [
        # Admin cluster: toolNames stays full and the gateway rule is the control.
        (RULE_NARROWED, ALL_EVIL_TOOLS, True),
        (RULE_OPEN, ALL_EVIL_TOOLS, False),
        # Workshop cluster: the gateway rule stays open and toolNames is the control.
        (RULE_OPEN, ["get_weather"], True),
        # An emptied toolNames exposes every tool on the server, so it blocks nothing.
        (RULE_OPEN, [], False),
        (RULE_NARROWED, ["get_weather"], True),
        # The rule's quoting does not change its meaning.
        ('- "mcp.tool.name == \\"get_weather\\"" # c7-rule', ALL_EVIL_TOOLS, True),
        ('- "true" # c7-rule', ALL_EVIL_TOOLS, False),
    ],
)
def test_controls_c7_reads_the_gateway_rule_and_the_allow_list(
    start_proxy, kube_api, rule_line, evil_tools, expected
):
    kube_api.objects[C7_CONFIGMAP] = configmap(c7_config(rule_line))
    kube_api.objects[AGENT_CR] = workshop_agent(evil_tools)
    assert start_proxy(**kube_api.env).get("/controls").json()["infra"]["tool_allowlist"] is expected


@pytest.mark.parametrize(
    "config_yaml",
    [
        # A rule the badge does not recognize could still admit a rogue tool.
        c7_config("""- 'mcp.tool.name != "read_internal_config"' # c7-rule"""),
        # No marked line, or two of them: the contract is broken, so the rule's state is unknown.
        c7_config("- 'true'"),
        c7_config(RULE_NARROWED) + "# a second marked line # c7-rule\n",
    ],
)
def test_controls_c7_unknown_when_the_gateway_rule_cannot_be_read(start_proxy, kube_api, config_yaml):
    kube_api.objects[C7_CONFIGMAP] = configmap(config_yaml)
    kube_api.objects[AGENT_CR] = workshop_agent(ALL_EVIL_TOOLS)
    assert start_proxy(**kube_api.env).get("/controls").json()["infra"]["tool_allowlist"] is None


def test_controls_c7_unknown_without_the_gateway_configmap(start_proxy, kube_api):
    """A missing ConfigMap reads like a denied one: the badge cannot tell, so it claims nothing."""
    kube_api.objects[AGENT_CR] = workshop_agent(ALL_EVIL_TOOLS)
    assert start_proxy(**kube_api.env).get("/controls").json()["infra"]["tool_allowlist"] is None


def test_controls_c7_narrowed_rule_counts_without_the_agent(start_proxy, kube_api):
    kube_api.objects[C7_CONFIGMAP] = configmap(c7_config(RULE_NARROWED))
    assert start_proxy(**kube_api.env).get("/controls").json()["infra"]["tool_allowlist"] is True


def test_controls_reads_the_cluster_with_the_service_account_token(start_proxy, kube_api):
    """The C7 reads are GETs of the two objects guard-proxy-reader is granted by name."""
    kube_api.objects[C7_CONFIGMAP] = configmap(c7_config(RULE_OPEN))
    kube_api.objects[AGENT_CR] = workshop_agent(ALL_EVIL_TOOLS)
    start_proxy(**kube_api.env).get("/controls")
    for path in (C7_CONFIGMAP, AGENT_CR):
        [call] = kube_api.calls(path)
        assert call.method == "GET"
        assert call.headers["Authorization"] == f"Bearer {SA_TOKEN}"


# --- /controls C3 detection: Falco is on this cluster ----------------------------------------------
# The burn profile (the Community cluster) installs no Falco (Unleash_an_Agent_Watch_It_Burn #486), so
# the badge reads the security/falco DaemonSet the Falco chart creates, which guard-proxy-reader's
# ClusterRole grants get on by name. Measured on means the DaemonSet has a ready pod.

FALCO_DS = "/apis/apps/v1/namespaces/security/daemonsets/falco"


def falco_daemonset(status) -> dict:
    return {"apiVersion": "apps/v1", "kind": "DaemonSet", "metadata": {"name": "falco"}, "status": status}


@pytest.mark.parametrize(
    "status,expected",
    [
        ({"desiredNumberScheduled": 3, "numberReady": 3}, True),
        ({"desiredNumberScheduled": 3, "numberReady": 1}, True),
        # Scheduled but not ready, or not yet reported: nothing is watching.
        ({"desiredNumberScheduled": 3, "numberReady": 0}, False),
        ({"desiredNumberScheduled": 3}, False),
        ({}, False),
    ],
)
def test_controls_falco_reads_the_daemonset(start_proxy, kube_api, status, expected):
    kube_api.objects[FALCO_DS] = falco_daemonset(status)
    assert start_proxy(**kube_api.env).get("/controls").json()["infra"]["falco"] is expected


def test_controls_falco_off_where_it_is_not_installed(start_proxy, kube_api):
    """The Community cluster: no Falco DaemonSet, so no detection badge."""
    assert start_proxy(**kube_api.env).get("/controls").json()["infra"]["falco"] is False


def test_controls_falco_off_when_the_read_is_denied(start_proxy, tmp_path):
    """A cluster whose reader role lacks the DaemonSet grant answers 403: the badge claims nothing."""
    denied = FakeUpstream(lambda req: Reply(status=403, body={"kind": "Status", "reason": "Forbidden"}))
    token = tmp_path / "sa-token"
    token.write_text(SA_TOKEN + "\n")
    try:
        p = start_proxy(K8S_API_URL=denied.url, K8S_TOKEN_PATH=str(token))
        assert p.get("/controls").json()["infra"]["falco"] is False
        assert [c.path for c in denied.calls(FALCO_DS)] == [FALCO_DS]
    finally:
        denied.close()


def test_controls_falco_is_a_get_with_the_service_account_token(start_proxy, kube_api):
    kube_api.objects[FALCO_DS] = falco_daemonset({"numberReady": 1})
    start_proxy(**kube_api.env).get("/controls")
    [call] = kube_api.calls(FALCO_DS)
    assert call.method == "GET"
    assert call.headers["Authorization"] == f"Bearer {SA_TOKEN}"


# --- /controls C2: the restrict-image-registries admission wall is denying -------------------------
# The workshop's policy is a Kyverno CEL ValidatingPolicy (Unleash_an_Agent_Watch_It_Burn #206), cluster
# scoped, and guard-proxy-reader's ClusterRole grants get on it by name. Its action is the list
# spec.validationActions: Audit does not block, so the badge is armed only when the list holds Deny.

C2_POLICY = "/apis/policies.kyverno.io/v1/validatingpolicies/restrict-image-registries"
RETIRED_C2_CLUSTERPOLICY = "/apis/kyverno.io/v1/clusterpolicies/restrict-image-registries"


def validating_policy(actions) -> dict:
    return {
        "apiVersion": "policies.kyverno.io/v1",
        "kind": "ValidatingPolicy",
        "metadata": {"name": "restrict-image-registries"},
        "spec": {"validationActions": actions},
    }


@pytest.mark.parametrize(
    "actions,expected",
    [
        (["Deny"], True),
        (["Audit"], False),
        (["Deny", "Audit"], True),
        (["Audit", "Warn"], False),
        ([], False),
        # The field is a list; a bare string is not one, even one spelling Deny.
        ("Deny", False),
    ],
)
def test_controls_c2_reads_the_validating_policy_actions(start_proxy, kube_api, actions, expected):
    kube_api.objects[C2_POLICY] = validating_policy(actions)
    assert start_proxy(**kube_api.env).get("/controls").json()["infra"]["kyverno"] is expected


def test_controls_c2_ignores_the_retired_clusterpolicy(start_proxy, kube_api):
    """An Enforce ClusterPolicy left behind does not arm the badge: admission is the ValidatingPolicy."""
    kube_api.objects[RETIRED_C2_CLUSTERPOLICY] = {
        "kind": "ClusterPolicy",
        "spec": {"rules": [{"validate": {"failureAction": "Enforce"}}]},
    }
    assert start_proxy(**kube_api.env).get("/controls").json()["infra"]["kyverno"] is False
    assert kube_api.calls(RETIRED_C2_CLUSTERPOLICY) == []


@pytest.mark.parametrize("policy", [{"kind": "ValidatingPolicy"}, {"kind": "ValidatingPolicy", "spec": {}}])
def test_controls_c2_unarmed_when_the_policy_has_no_actions(start_proxy, kube_api, policy):
    kube_api.objects[C2_POLICY] = policy
    assert start_proxy(**kube_api.env).get("/controls").json()["infra"]["kyverno"] is False


def test_controls_c2_reads_the_policy_with_the_service_account_token(start_proxy, kube_api):
    """The C2 read is a GET of the one ValidatingPolicy guard-proxy-reader is granted by name."""
    kube_api.objects[C2_POLICY] = validating_policy(["Deny"])
    start_proxy(**kube_api.env).get("/controls")
    [call] = kube_api.calls(C2_POLICY)
    assert call.method == "GET"
    assert call.headers["Authorization"] == f"Bearer {SA_TOKEN}"


# --- GET passthrough to the agent -----------------------------------------------------------------


@pytest.mark.passthrough
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


@pytest.mark.passthrough
def test_get_passthrough_agent_error_status_is_502(start_proxy, agent):
    agent.respond = lambda req: Reply(status=404, body={"error": "nope"})
    r = start_proxy().get("/missing")
    assert r.status == 502
    assert r.json()["error"].startswith("agent GET failed:")


@pytest.mark.passthrough
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
# The platform serves the start page on both domains at once (Unleash_an_Agent_Watch_It_Burn#651).
CONSOLE_ALT = "https://start.burritobot.co"


@pytest.mark.parametrize(
    "origin,host,allowed",
    [
        (CONSOLE, None, True),
        (CONSOLE_ALT, None, True),
        ("https://evil.example", None, False),
        ("https://start.example.com", None, False),
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


def test_cors_console_origin_takes_a_list(start_proxy):
    p = start_proxy(CONSOLE_ORIGIN="https://a.example, https://b.example")
    for origin in ("https://a.example", "https://b.example"):
        r = p.get("/cost", headers={"Origin": origin})
        assert r.headers.get("access-control-allow-origin") == origin
    for origin in (CONSOLE, "https://c.example", "https://a.example, https://b.example"):
        assert "access-control-allow-origin" not in p.get("/cost", headers={"Origin": origin}).headers


def test_cors_no_origin_no_header(start_proxy):
    assert "access-control-allow-origin" not in start_proxy().get("/cost").headers


def test_foreign_origin_post_is_not_rejected(start_proxy):
    """Origin enforcement was removed (it broke every browser behind the apex router); see #151."""
    p = start_proxy()
    r = p.post("/chat", {"prompt": "one burrito"}, headers={"Origin": "https://evil.example"})
    assert r.status == 200
    assert "access-control-allow-origin" not in r.headers


@pytest.mark.passthrough
def test_options_preflight_is_not_implemented(start_proxy):
    """No OPTIONS handler, so a cross-origin preflight fails: a second layer under the Origin echo."""
    p = start_proxy()
    assert http("OPTIONS", p.url + "/chat").status == 501
