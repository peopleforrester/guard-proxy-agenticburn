# ABOUTME: A2A-aware guard proxy that fronts the kagent agent and calls LLM Guard.
# ABOUTME: Input guard (prompt-injection on the request) + output guard (sentinel exfil on the
# ABOUTME: response), each toggled by env. Realizes the spec's output-sidecar as a standalone
# ABOUTME: inspection point because the kagent controller owns the agent pod (no in-pod sidecar).
#
# Stdlib only — runs in a stock python image via a mounted ConfigMap; no registry/build needed
# for the test cluster. For production, bake this into a pinned image (see GATEWAY-NOTES.md).
#
# Verdict envelope (confirmed live 2026-06-17): /analyze/prompt and /analyze/output return
# {"is_valid": bool, "scanners": {...}, "sanitized_prompt"|"sanitized_output": "..."}.
import codecs
import collections
import hashlib
import hmac
import json
import logging
import math
import os
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from contextlib import nullcontext
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlparse, parse_qs

# OTel instrumentation (PRD #22 M1 / issue #19). The stock python image carries no opentelemetry; the
# OTel Operator injects the SDK at pod startup (PYTHONPATH). The try/except guard keeps this file runnable
# with no OTel present (local dev, test clusters) as a no-op until the Operator's annotation is applied.
try:
    from opentelemetry import context as otel_context, trace, metrics
    # The key urllib's instrumentation reads to skip a request: no span, and no traceparent of its own.
    # It is the API's, so suppressing needs no instrumentation package installed.
    from opentelemetry.context import _SUPPRESS_HTTP_INSTRUMENTATION_KEY
    from opentelemetry.trace import SpanKind, Status, StatusCode
    from opentelemetry.metrics import Observation
    from opentelemetry.propagate import extract, inject
    _OTEL_AVAILABLE = True
    _tracer = trace.get_tracer(__name__)
    _meter = metrics.get_meter(__name__)
except ImportError:
    _OTEL_AVAILABLE = False
    _tracer = None
    _meter = None


# Structured JSON logging with trace correlation (PRD #27 M2). Every log line is one JSON object
# carrying the active span's trace_id/span_id, so Datadog ties guard-decision logs to the trace in
# the waterfall. Field names are the OTel-standard `trace_id`/`span_id` (lowercase hex) which Datadog
# auto-recognizes — NO dd.trace_id/dd.span_id 64-bit-decimal remapping (locked PRD #27 Decision Log,
# 2026-06-25). Stdlib logging only; no SDK dependency.
class _TraceJsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        event = getattr(record, "event", None)
        if event:
            payload["event"] = event
        # Which step of a multi-step guard decided, for a facet beside the event (topic_guard_block).
        step = getattr(record, "step", None)
        if step:
            payload["step"] = step
        # The input scanners that flagged a blocked prompt (input_classifier_block).
        scanners = getattr(record, "scanners", None)
        if scanners:
            payload["scanners"] = scanners
        # A /toggle that changed a guard (guard_toggle): which guard, the value it took and had, and who asked.
        toggle = getattr(record, "toggle", None)
        if toggle:
            payload.update(toggle)
        if _OTEL_AVAILABLE:
            ctx = trace.get_current_span().get_span_context()
            if ctx.is_valid:
                payload["trace_id"] = format(ctx.trace_id, "032x")
                payload["span_id"] = format(ctx.span_id, "016x")
        return json.dumps(payload)


log = logging.getLogger("guard-proxy")
if not log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(_TraceJsonFormatter())
    log.addHandler(_handler)
    log.setLevel(logging.INFO)
    log.propagate = False

# Content capture is gated on the OTel GenAI capture env var, read ONCE at module load. This env var is
# specific to opentelemetry-util-genai / contrib instrumentations; it does NOT govern hand-written SDK
# spans automatically (the research spikes (removed 2026-08-30; see `git log --diff-filter=D -- research/`) Q3), so this proxy reads it explicitly. Valid enum values:
# NO_CONTENT (default), SPAN_ONLY, EVENT_ONLY, SPAN_AND_EVENT. "true" is NOT valid and must NOT enable
# capture (it silently collects nothing on the ADK path too). Default OFF matches BUILD-SPEC s4.
_CAPTURE_MODE = os.environ.get("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "NO_CONTENT").upper()
_CAPTURE_CONTENT = _CAPTURE_MODE in ("SPAN_ONLY", "EVENT_ONLY", "SPAN_AND_EVENT")


def _genai_messages(text):
    """One OTel GenAI message envelope (user/text) as a JSON string, per the messages schema."""
    return json.dumps([{"role": "user", "parts": [{"type": "text", "content": text}]}])


def _genai_output(text):
    """One OTel GenAI message envelope (assistant/text) as a JSON string, per the messages schema."""
    return json.dumps([{"role": "assistant", "parts": [{"type": "text", "content": text}], "finish_reason": "stop"}])

AGENT_URL = os.environ.get("AGENT_URL", "http://workshop-agent.attendee-test:8080")
# peer.service for the outbound CLIENT span (PRD #27 M2): the downstream node the Datadog Service Map
# draws an edge to. Derived from AGENT_URL's host so the edge always names whatever this proxy really
# calls. Target topology: AGENT_URL fronts agentgateway (-> "agentgateway"); until agentgateway is
# deployed it points straight at the kagent agent. PEER_SERVICE overrides if the host label is wrong.
# The Service Map node name of the next hop: the downstream SERVICE, not the Kubernetes hostname. The
# services are one "burritobot" family (#265) while the Service is still called agentgateway.
_PEER_SERVICE = os.environ.get("PEER_SERVICE") or "burritobot-gateway"
LLM_GUARD_URL = os.environ.get("LLM_GUARD_URL", "http://llm-guard.attendee-test:8000")
LLM_GUARD_TOKEN = os.environ.get("LLM_GUARD_TOKEN", "")
# Guard state is RUNTIME-mutable (flipped via GET /toggle), seeded from env. This is deliberate:
# the platform is Argo CD-managed and self-heal would revert a `kubectl set env` change, and an env
# change also restarts the pod (resetting the cost counter). A runtime toggle changes no managed
# spec, so it sticks AND the counter survives. The live demo flips guards via /toggle, not kubectl.
_guard_lock = threading.Lock()
# The keys a guard_toggle line can name, in the order /toggle's answer lists them, and how much of a caller's
# X-WIB-Caller header the line keeps.
TOGGLE_KEYS = ("input_blocklist", "input_classifier", "output", "budget", "topic", "cost_cap")
TOGGLE_CALLER_MAX = 200
# Input guard is TWO progressively-enabled stages (ruling 2026-06-19): stage 1 is the deterministic
# block-list (cheapest, pre-LLM, zero tokens), stage 2 is the model-based classifier. They toggle
# INDEPENDENTLY so the demo shows "cheaper first" on the cost counter. Output is the exfil sidecar.
# Back-compat: INPUT_GUARD=on seeds both input stages on.
_input_legacy = os.environ.get("INPUT_GUARD", "").lower() == "on"
GUARDS = {
    "input_blocklist": _input_legacy or os.environ.get("INPUT_BLOCKLIST", "off").lower() == "on",
    "input_classifier": _input_legacy or os.environ.get("INPUT_CLASSIFIER", "off").lower() == "on",
    "output": os.environ.get("OUTPUT_GUARD", "off").lower() == "on",
    # The denial-of-wallet control (C4). Off, as on the community cluster: the bill runs away. On = the
    # per-agent budget cap enforces at the gateway. Toggled live like the others.
    "budget": os.environ.get("BUDGET_GUARD", "off").lower() == "on",
    # The on-topic guard (Unleash_an_Agent_Watch_It_Burn#455): BurritoBot sells burritos only. Off by
    # default, and not seeded by INPUT_GUARD: every challenge prompt is off-topic by design, so the
    # run-of-show turns it on alone, in segment 7.
    "topic": os.environ.get("TOPIC_GUARD", "off").lower() == "on",
}
# Fail closed: if LLM Guard is unreachable, block rather than silently leak (no-silent-fallback rule).
FAIL_CLOSED = os.environ.get("PROXY_FAIL_CLOSED", "true").lower() == "true"
TIMEOUT = float(os.environ.get("PROXY_TIMEOUT", "150"))
# One deadline for a whole POST /chat turn, in seconds (guard-proxy-agenticburn#21). A turn can make up to
# six upstream calls in a row (injection classifier, topic classifier, forward, two self-heal retries,
# output scrub), and PROXY_TIMEOUT bounds each alone, so the turn's worst case was six times it. With this
# set, each call gets PROXY_TIMEOUT or what is left of the turn, whichever is less, and a turn that runs
# out answers the same 502 as a failed forward. 0 (the default) leaves each call its own PROXY_TIMEOUT.
TURN_TIMEOUT = float(os.environ.get("TURN_TIMEOUT", "0") or "0")


class TurnExpired(Exception):
    """The /chat turn's TURN_TIMEOUT ran out before it had an answer."""


def _call_timeout(deadline):
    """The timeout for one upstream call: PROXY_TIMEOUT, cut to what is left before deadline (a monotonic
    time, or None for no turn deadline). Raises TurnExpired when nothing is left.

    urllib applies a timeout to each blocking socket operation, not to the exchange as a whole, so this
    bounds the wait for an answer; an upstream trickling bytes can still run past the deadline."""
    if deadline is None:
        return TIMEOUT
    left = deadline - time.monotonic()
    if left <= 0:
        raise TurnExpired(f"the {TURN_TIMEOUT:g}s turn deadline (TURN_TIMEOUT) passed")
    return min(TIMEOUT, left)


def _raise_if_expired(deadline):
    """Raise TurnExpired once deadline has passed. A guard call that failed after the deadline was cut off
    by the turn, which is not a scanner outage, so PROXY_FAIL_CLOSED does not decide it."""
    if deadline is not None and time.monotonic() >= deadline:
        raise TurnExpired(f"the {TURN_TIMEOUT:g}s turn deadline (TURN_TIMEOUT) passed")


# The /chat forward is an A2A task the proxy can name (guard-proxy-agenticburn#27). kagent runs a turn in a
# context detached from the request, so a forward that gave up left the agent working, and spending, for
# hours. The forward is therefore sent non-blocking, which answers with the task's id at once, then polled
# with tasks/get until the task leaves these states; a forward that fails or runs out of time sends
# tasks/cancel for that id. The poll waits _POLL_FIRST seconds, doubling to at most _POLL_MAX.
_TASK_RUNNING = ("submitted", "working")
_POLL_FIRST = 0.25
_POLL_MAX = 1.0


def _running_task_id(resp):
    """The id of the task an A2A response carries while the agent is still working on it, else None. A
    running task without an id cannot be polled or cancelled, so it is an error."""
    result = resp.get("result") if isinstance(resp, dict) else None
    if not isinstance(result, dict):
        return None
    status = result.get("status")
    if not isinstance(status, dict) or status.get("state") not in _TASK_RUNNING:
        return None
    task_id = result.get("id")
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("the agent answered a running task with no id")
    return task_id


def _cancel_task(task_id, reason):
    """Send the agent tasks/cancel for a /chat task the forward gave up on, or one waiting on a guest who
    cannot answer it, so the agent stops on a turn nobody is waiting for. Runs on its own thread, so the
    turn's answer never waits on it, and logs either way; reason says why the task is cancelled."""
    body = {"jsonrpc": "2.0", "id": "cancel", "method": "tasks/cancel", "params": {"id": task_id}}
    try:
        req = urllib.request.Request(AGENT_URL + "/", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            resp = json.loads(r.read())
    except Exception as exc:
        log.error("could not cancel the agent's task %s: %s", task_id, exc,
                  extra={"event": "task_cancel_failed"})
        return
    result = resp.get("result") if isinstance(resp, dict) else None
    if not isinstance(result, dict):
        error = resp.get("error", resp) if isinstance(resp, dict) else resp
        log.error("the agent did not cancel task %s: %s", task_id, json.dumps(error)[:200],
                  extra={"event": "task_cancel_failed"})
        return
    state = result.get("status", {}).get("state") if isinstance(result.get("status"), dict) else None
    log.info("cancelled the agent's task %s (state %s): %s", task_id, state, reason,
             extra={"event": "task_cancel"})

# --- Infra-control state for the badge strip (#197/#209) -------------------------------------------
# The AI guards above are guard-proxy's own in-memory state. The badges for C1-C3 and C7 report the
# state of real cluster objects the student installs, which only the Kubernetes API knows. This block
# reads them read-only, with the pod's own ServiceAccount token, so the browser can light a badge the
# moment a student applies the control (a NetworkPolicy, a Kyverno Enforce flip, a KubeArmor policy, a
# narrowed MCP tool list, or a narrowed agentgateway mcpAuthorization rule).
#
# Deliberately narrow: the guard-proxy is the component the challenges ATTACK, so its new RBAC is
# get-only on the named objects read below and nothing else (see the Role/ClusterRole in
# resources.yaml). Reading whether a NetworkPolicy exists is not an escalation an attacker gains anything from, and the token is
# never exposed to the model path.
# Both overridable so the conformance suite can stand a fake API server in for the cluster. In a pod
# they stay unset and the in-cluster defaults apply.
_K8S_HOST = os.environ.get("K8S_API_URL") or "https://kubernetes.default.svc"
_K8S_TOKEN_PATH = os.environ.get("K8S_TOKEN_PATH") or "/var/run/secrets/kubernetes.io/serviceaccount/token"
_K8S_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
_CONTROLS_TTL = 5.0        # seconds; the strip polls every 12s, so a short cache is plenty
_controls_cache = {"ts": 0.0, "val": None}
_controls_lock = threading.Lock()


def _k8s_get(path):
    """GET one object from the cluster API. Returns the parsed JSON, or None on 404/error.

    None means 'could not determine', which a caller renders as an un-lit badge rather than a false
    claim either way. A 404 (the object does not exist yet) is the normal 'control not installed' case
    and also returns None."""
    try:
        with open(_K8S_TOKEN_PATH, "r") as f:
            token = f.read().strip()
    except OSError:
        return None
    import ssl
    ctx = ssl.create_default_context(cafile=_K8S_CA_PATH) if os.path.exists(_K8S_CA_PATH) else None
    req = urllib.request.Request(_K8S_HOST + path, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=5, context=ctx) as r:
            return json.loads(r.read())
    except Exception:
        return None


# The C7 gateway rule's contract with the workshop repo (Unleash_an_Agent_Watch_It_Burn #514): ConfigMap
# agent/agentgateway-c7-config, key config.yaml, and the one line ending `# c7-rule`. 'true' is open;
# the web terminal's guard-mcp-on rewrites that line to admit get_weather alone.
_C7_RULE_MARKER = "# c7-rule"
_C7_RULE_OPEN = "true"
_C7_RULE_NARROWED = 'mcp.tool.name == "get_weather"'


def _yaml_quoted(scalar):
    """The value of a single- or double-quoted YAML scalar, the scalar itself if unquoted, or None."""
    if len(scalar) >= 2 and scalar[0] == scalar[-1] == "'":
        return scalar[1:-1].replace("''", "'")
    if len(scalar) >= 2 and scalar[0] == scalar[-1] == '"':
        try:
            return json.loads(scalar)
        except ValueError:
            return None
    return scalar


def _c7_gateway_rule_narrowed():
    """True when agentgateway's C7 rule admits only get_weather, False when it is open, else None.

    Read by its marker line, the same line the toggle rewrites, because the stdlib has no YAML parser.
    A missing marker, a second marked line, or a rule this does not recognize is None: an unknown rule
    could still admit a rogue tool, so the badge claims nothing either way."""
    cm = _k8s_get("/api/v1/namespaces/agent/configmaps/agentgateway-c7-config")
    try:
        marked = [ln.strip() for ln in cm["data"]["config.yaml"].splitlines()
                  if ln.rstrip().endswith(_C7_RULE_MARKER)]
    except (KeyError, TypeError, AttributeError):
        return None
    if len(marked) != 1:
        return None
    item = marked[0][:-len(_C7_RULE_MARKER)].strip()
    if not item.startswith("- "):
        return None
    rule = _yaml_quoted(item[2:].strip())
    if rule == _C7_RULE_NARROWED:
        return True
    if rule == _C7_RULE_OPEN:
        return False
    return None


def _c7_allowlist_narrowed():
    """True when the agent's toolNames no longer carry the two rogue tools, False when they do, None if
    the Agent could not be read. The evil-mcp server ships with
    [get_weather, read_internal_config, apply_optimization]."""
    ag = _k8s_get("/apis/kagent.dev/v1alpha2/namespaces/agent/agents/workshop-agent")
    if ag is None:
        return None
    rogue = {"read_internal_config", "apply_optimization"}
    present = set()
    try:
        for t in ag["spec"]["declarative"]["tools"]:
            srv = t.get("mcpServer") or {}
            names = srv.get("toolNames") or []
            # kagent treats an EMPTY toolNames as "expose every tool on that server", so an
            # emptied evil-mcp entry still ships the rogue tools. Verified live 2026-09-05: with
            # [] the sentinel kept leaking; with ["get_weather"] it stopped. Count it as unfixed.
            if srv.get("name") == "evil-mcp" and not names:
                present |= rogue
            present.update(names)
    except (KeyError, TypeError):
        pass
    return not (rogue & present)


_WORKLOAD_WRITES = {("apps", "deployments"), ("", "pods")}


def _role_grants_workload_writes(role):
    """True when a rule of `role` grants create or patch on Deployments or Pods. A Role whose rules
    cannot be read as a list of rules is treated as granting them: the access cannot be ruled out."""
    try:
        for r in role.get("rules", []):
            groups = r.get("apiGroups", [])
            res = r.get("resources", [])
            verbs = r.get("verbs", [])
            if not ({"create", "patch", "*"} & set(verbs)):
                continue
            for group, resource in _WORKLOAD_WRITES:
                if (group in groups or "*" in groups) and (resource in res or "*" in res):
                    return True
    except (AttributeError, TypeError):
        return True
    return False


def read_controls():
    """Live state of the cluster-object controls behind the C1/C2/C3/C7/C8 badges, plus Falco.

    The infra fields are networkpolicy, kyverno, kubearmor, falco, tool_allowlist, rbac_scoped (the
    agent's Role no longer grants broad Secrets access) and agency_scoped (the agent's Role grants no
    create or patch on Deployments or Pods: Challenge 3's least privilege at KubeCon).

    Cached for a few seconds so the badge poll does not hit the API server on every request. Each field
    is True (installed / enforcing), False (measured absent), or None (could not read)."""
    now = time.time()
    with _controls_lock:
        if _controls_cache["val"] is not None and (now - _controls_cache["ts"]) < _CONTROLS_TTL:
            return dict(_controls_cache["val"])

    out = {}

    # C1: the agent-egress-allowlist NetworkPolicy exists (default-deny egress is in place).
    np = _k8s_get("/apis/networking.k8s.io/v1/namespaces/agent/networkpolicies/agent-egress-allowlist")
    out["networkpolicy"] = (np is not None)

    # C2: restrict-image-registries is DENYING (Audit does not block, so Audit is 'not installed' for
    # the badge). It is a Kyverno CEL ValidatingPolicy (Unleash_an_Agent_Watch_It_Burn #206), and the
    # live toggle replaces its spec.validationActions list: armed when that list contains Deny.
    vp = _k8s_get("/apis/policies.kyverno.io/v1/validatingpolicies/restrict-image-registries")
    try:
        actions = vp["spec"]["validationActions"]
    except (KeyError, TypeError):
        actions = None
    out["kyverno"] = isinstance(actions, list) and "Deny" in actions

    # C3 prevention: the block-recipe-snoop KubeArmorPolicy exists.
    ka = _k8s_get("/apis/security.kubearmor.com/v1/namespaces/agent/kubearmorpolicies/block-recipe-snoop")
    out["kubearmor"] = (ka is not None)

    # C3 detection: Falco is on this cluster and watching. It is not a control the student toggles, but
    # it is not on every cluster either: the burn profile (the Community cluster) installs none
    # (Unleash_an_Agent_Watch_It_Burn #486). So it is measured like the rest: the security/falco
    # DaemonSet exists and has a ready pod. A DaemonSet with no ready pod detects nothing.
    ds = _k8s_get("/apis/apps/v1/namespaces/security/daemonsets/falco")
    try:
        out["falco"] = int(ds["status"].get("numberReady") or 0) > 0
    except (KeyError, TypeError, AttributeError, ValueError):
        out["falco"] = False

    # C7: the rogue evil-mcp tools are blocked. Two controls can block them, and the badge lights when
    # either does. On the KubeCon admin cluster it is agentgateway's mcpAuthorization rule on the bind
    # evil-mcp is dialed through: guard-mcp-on narrows the rule and leaves toolNames full. On a workshop
    # cluster the rule stays open and the control is the agent's toolNames allow-list. Either one
    # blocking is True, both measured open is False, anything else is None.
    gateway = _c7_gateway_rule_narrowed()
    allowlist = _c7_allowlist_narrowed()
    if gateway or allowlist:
        out["tool_allowlist"] = True
    elif gateway is False and allowlist is False:
        out["tool_allowlist"] = False
    else:
        out["tool_allowlist"] = None

    # C8 (#111): the agent-sa Role is "fixed" once its secrets access is scoped down from blanket
    # get/list to get on the recipe Secret by name. Report true when the broad access is gone: no rule
    # grants list on secrets, and no rule grants get on secrets without a resourceNames restriction.
    role = _k8s_get("/apis/rbac.authorization.k8s.io/v1/namespaces/agent/roles/workshop-agent")
    if role is None:
        out["rbac_scoped"] = None
    else:
        broad = False
        try:
            for r in role.get("rules", []):
                groups = r.get("apiGroups", [])
                res = r.get("resources", [])
                if "" not in groups or "secrets" not in res:
                    continue
                verbs = r.get("verbs", [])
                names = r.get("resourceNames", [])
                # broad = can list any secret, or can get secrets without naming them
                if "list" in verbs or (("get" in verbs) and not names):
                    broad = True
        except (AttributeError, TypeError):
            broad = True
        out["rbac_scoped"] = not broad

    # Challenge 3 at KubeCon (#76): least privilege is on once guard-agency-on's Role leaves agent-sa
    # unable to create or patch Deployments (apiGroup apps) or Pods (the core group). The same Role read
    # as rbac_scoped; a wildcard group, resource or verb grants as surely as naming it.
    out["agency_scoped"] = None if role is None else not _role_grants_workload_writes(role)

    with _controls_lock:
        _controls_cache["ts"] = now
        _controls_cache["val"] = dict(out)
    return out

# Rate-limit + cost-cap the demo ITSELF: a room hammering the chaos agent must not run up the real
# Bedrock bill or DoS the demo (the cost demo cannot itself run away). Both are per-cluster (this
# proxy fronts one cluster) and env-tunable; 0 disables. verify-at-build: set caps to the room size.
COST_CAP_USD = float(os.environ.get("COST_CAP_USD", "0") or "0")     # reject once this cluster's tally hits it
# The cap cost_capped() checks. Seeded from COST_CAP_USD and RUNTIME-mutable like the guards: the KubeCon
# finale lowers it with /toggle?cost_cap=N so a room can trip it in minutes, and /toggle?cost_cap=default
# puts the env value back. An env change would restart the pod and reset the tally, and Argo CD reverts it.
_cost_cap = {"usd": COST_CAP_USD}
RATE_LIMIT_RPM = int(os.environ.get("RATE_LIMIT_RPM", "0") or "0")   # max model-bound requests per 60s
# Per /chat conversation, checked before the cluster window: a ballroom shares one bot, and one phone must not
# spend everyone's slots. 0 turns it off. A request with no session is held only by the cluster window.
RATE_LIMIT_SESSION_RPM = int(os.environ.get("RATE_LIMIT_SESSION_RPM", "0") or "0")

# Denial-of-wallet control (Challenge 4, replacing the fork bomb). Token spend is its own DoS vector:
# a room, or an agent talked into a loop, runs the Bedrock bill up while nothing crashes. The counter on
# BurritoBot climbs and nothing stops it. The gateway budget cap IS the control: once a cluster's metered
# spend crosses BUDGET_CAP_USD, further requests are refused BEFORE the model is called, so a blocked
# request costs zero. It is a RUNTIME toggle (like the input/output guards) rather than a pod-restarting
# env change; DEFAULT OFF so the attack lands before the student installs the control.
# The cap is PER SESSION (see cost_capped): it kills the abusive conversation, not the cluster.
# 3 cents, MEASURED not guessed. The expensive lab prompt costs about $0.005 a send (attendee-001,
# 2026-09-08: 4 sends moved $0.0500 to $0.0708, then 7 moved it to $0.1012). At the old 10 cents that
# was roughly TWENTY sends, while the lab said "a few more times", and it was measured against a
# cluster-wide total that started wherever the last person left it. Per session from zero, 3 cents is
# about six sends: enough to feel like an attack, short enough to finish.
BUDGET_CAP_USD = float(os.environ.get("BUDGET_CAP_USD", "0.03") or "0.03")
_rate_lock = threading.Lock()
_req_times = collections.deque()  # timestamps of recent forwarded POSTs (sliding 60s window)
_session_times = {}  # session -> deque of its recent /chat timestamps, under _rate_lock


def rate_limited():
    """True if this cluster is over its requests-per-minute cap. Sliding 60s window."""
    if RATE_LIMIT_RPM <= 0:
        return False
    now = time.monotonic()
    with _rate_lock:
        while _req_times and now - _req_times[0] > 60.0:
            _req_times.popleft()
        if len(_req_times) >= RATE_LIMIT_RPM:
            return True
        _req_times.append(now)
        return False


def session_rate_limited(session):
    """True if this /chat conversation is over RATE_LIMIT_SESSION_RPM. Sliding 60s window per session."""
    if RATE_LIMIT_SESSION_RPM <= 0 or not session:
        return False
    now = time.monotonic()
    with _rate_lock:
        if len(_session_times) > 5000:  # a room of abandoned conversations must not grow without bound
            for k in [k for k, q in _session_times.items() if not q or now - q[-1] > 60.0]:
                del _session_times[k]
        q = _session_times.setdefault(session, collections.deque())
        while q and now - q[0] > 60.0:
            q.popleft()
        if len(q) >= RATE_LIMIT_SESSION_RPM:
            return True
        q.append(now)
        return False


def parse_cost_cap(value):
    """The cluster-wide cap a /toggle?cost_cap= value asks for, or None when the value is refused.

    "default" restores COST_CAP_USD. Anything else must be a finite number above 0 and, while COST_CAP_USD
    is set, no higher than it: the runtime cap only LOWERS the infra backstop. /toggle has no auth and the
    agent's tool pod can reach it, so it must not be a way to lift the Bedrock bill protection, and a 0
    would switch the backstop off.
    """
    v = value.strip().lower()
    if v == "default":
        return COST_CAP_USD
    try:
        cap = float(v)
    except ValueError:
        return None
    if not math.isfinite(cap) or cap <= 0 or (COST_CAP_USD > 0 and cap > COST_CAP_USD):
        return None
    return cap


def _toggle_value_text(value):
    """A guard_toggle message's value as the Go build's %v writes it: true or false, and a cap in its shortest
    digits (2, not 2.0), whole caps below 1e21 without an exponent."""
    if isinstance(value, bool):
        return json.dumps(value)
    if value.is_integer() and abs(value) < 1e21:
        return str(int(value))
    return repr(value)


def cost_cap_refusal():
    """The 400 message for a cost_cap value parse_cost_cap refuses."""
    ceiling = f" and no higher than COST_CAP_USD (${COST_CAP_USD:.2f})" if COST_CAP_USD > 0 else ""
    return f"cost_cap must be a number above 0{ceiling}, or 'default' to restore COST_CAP_USD"


def cost_capped(session=""):
    """Which cap spend has reached: "cost_cap", "budget", or None. TWO caps, measured against deliberately
    different totals. The cluster-wide one answers first when both have tripped, because Reset lifts only
    the per-session one.

    COST_CAP_USD is the infra safety backstop and stays CLUSTER-WIDE. It protects the real Bedrock bill
    from a runaway room, so no single conversation may be handed its own $25. The value checked is the
    runtime one in _cost_cap, which /toggle?cost_cap= can lower and restore.

    BUDGET_CAP_USD is the Challenge 4 control and is PER SESSION. Every other guardrail in this workshop
    is a predicate: C1 blocks the beacon while a burrito order still works, C2 blocks one registry, C5
    scrubs one signature, C6 blocks one prompt shape. They stay armed for the rest of the lab and nothing
    else breaks. A cluster-wide accumulator was the only control that, once tripped, killed the agent for
    every purpose, which is why the lab had to tell a student to switch it back off, and why missing that
    one line silently broke Challenges 5 through 8 half an hour later.

    Nothing turns a budget cap off in production. What production does is scope it per user or per tenant,
    so the abusive conversation dies and everyone else carries on. Per session is that, and it lands on a
    control the student already has: Reset gives them a fresh conversation with the guardrail still armed.

    An empty session (the A2A passthrough, which carries no browser session) falls back to the
    cluster-wide tally, so that path is no weaker than it was.
    """
    with _cost_lock:
        spend = _cost["usd"]
        session_spend = _session_cost.get(session, 0.0) if session else spend
    cost_cap = _cost_cap["usd"]
    if cost_cap > 0 and spend >= cost_cap:
        return "cost_cap"
    if GUARDS.get("budget") and BUDGET_CAP_USD > 0 and session_spend >= BUDGET_CAP_USD:
        return "budget"
    return None

# Input block-list (Challenge 6, the "block-list" stage): deterministic, runs BEFORE any LLM call.
# A match is rejected without spending a single Bedrock token — the workshop's cost lesson. Two kinds
# of term live here:
#   1. Destructive intent (delete/wipe/nuke/...): the cost-saving lesson.
#   2. Secret-recipe phrases: block attempts to extract Bat Spit Amazing Awesome Sauce. By design these
#      are the DISTINCTIVE made-up names (ogre toenails, snail blood) and the AMOUNT-bearing phrases
#      (the proportions are the secret), plus the signature — NOT bare common words like "bat saliva",
#      "moonlight", "ghost pepper", or "smoked paprika", which would over-block normal chat everywhere
#      they appear, and NOT the product name itself (it appears in the Challenge-5 prompt). This mirrors
#      the output guard's redaction policy (see the llm-guard-scanners Regex scanner).
# Comma-separated terms; case-insensitive substring match. Cheap by design (no model). Terms must not
# contain a comma (the list is comma-split).
BLOCK_LIST = [t.strip().lower() for t in os.environ.get(
    "BLOCK_LIST",
    "delete,destroy,rm -rf,drop database,kubectl delete,shutdown,terminate,wipe,nuke,"
    "ogre toenails,snail blood,generous splash of bat saliva,pinch of moonlight,"
    "WITCH-HAZEL-GHOST-PEPPER-BAT-SPIT-No7,OPHELIA-THORN-TIBBY-TOP-SPIRE-No9",
).split(",") if t.strip()]

# On-topic guard (#455): decides cheapest first. An off phrase refuses, then an on phrase passes, and
# anything with neither goes to LLM Guard's BanTopics zero-shot scanner. The phrases carry the on-topic
# side because BanTopics softmaxes over the banned labels only, so a short menu question ("do you have mild
# salsa") lands its whole score on some banned label. The lists and the refusal belong with the persona, so
# the workshop ships them as env (gitops/ai-layer/resources.yaml); unset lists are empty, which leaves every
# prompt to the classifier. Comma-separated like BLOCK_LIST; case is left alone because matching ignores it.
TOPIC_OFF_LIST = [t.strip() for t in os.environ.get("TOPIC_OFF_LIST", "").split(",") if t.strip()]
TOPIC_ON_LIST = [t.strip() for t in os.environ.get("TOPIC_ON_LIST", "").split(",") if t.strip()]
TOPIC_REFUSAL = os.environ.get(
    "TOPIC_REFUSAL", "BurritoBot only talks burritos: the menu, our hours, and our locations.")

# Live cost meter: tally Bedrock token usage from each agent response (kagent reports the real
# promptTokenCount / candidatesTokenCount) and convert to USD for the "wasted tokens are the new DoS"
# story. The COUNTER value is never hardcoded; only the per-tier PRICE table is config, and the
# authoritative post-hoc total is Cost Explorer (teardown/cost-report.sh).
#
# Per-1K-token list prices (USD), sourced 2026-06-19 from Anthropic + AWS Bedrock pricing pages:
#   Haiku 4.5 $1/$5, Sonnet 4.6 $3/$15, Opus 4.8 $5/$25 per 1M tokens.
# verify-at-build: confirm the Bedrock list price for the deployed region. Anthropic API list prices
#   (used here) historically match Bedrock for these models, but confirm before quoting a number.
#
# `nova` added 2026-08-30: the workshop agent is bound to the bedrock-nova ModelConfig
# (us.amazon.nova-pro-v1:0), but MODEL_TIER was still "sonnet", so the on-screen counter was pricing Nova
# tokens at Sonnet rates (roughly 4x over). The counter is a headline visual for C4, so a wrong multiplier
# is a wrong lesson. Nova Pro list price $0.0008/1K in, $0.0032/1K out ($0.80/$3.20 per 1M), per the AWS
# pricing pages as of 2026-08-30. verify-at-build: the Bedrock pricing table is JS-rendered and could not
# be scraped, so re-confirm this pair against aws.amazon.com/bedrock/pricing before quoting it on a slide.
TIER_PRICES_PER_1K = {
    "nova":   {"in": 0.0008, "out": 0.0032},
    "haiku":  {"in": 0.001, "out": 0.005},
    "sonnet": {"in": 0.003, "out": 0.015},
    "opus":   {"in": 0.005, "out": 0.025},
}
# Which tier this cluster starts on (set per cluster to match the kagent ModelConfig). Defaults to haiku.
# Requests are priced at the tier the workshop-agent is bound to (bound_tier below); this prices them
# until the Agent has been read, and whenever it names a ModelConfig with no tier.
MODEL_TIER = os.environ.get("MODEL_TIER", "haiku").lower()
# Full Bedrock model ID per tier — reported as gen_ai.request.model for a request priced at that tier,
# when MODEL_NAME is not explicitly set (request_model below).
# IDs verified ACTIVE 2026-06-26; update when kagent ModelConfigs are bumped.
_TIER_TO_MODEL_ID = {
    "nova":   "us.amazon.nova-pro-v1:0",
    "haiku":  "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "sonnet": "us.anthropic.claude-sonnet-4-6",
    "opus":   "us.anthropic.claude-opus-4-8",
}
# MODEL_NAME: an explicit model name, reported on every span and cost series whatever the agent is bound
# to. Unset or empty, each request reports the model of the tier it is priced at.
MODEL_NAME = os.environ.get("MODEL_NAME") or None
# Optional explicit per-1K overrides. Each one set fixes that price whatever the agent is bound to; an
# unset one comes from the tier table above.
_COST_PER_1K_IN = os.environ.get("COST_PER_1K_IN")
_COST_PER_1K_IN = float(_COST_PER_1K_IN) if _COST_PER_1K_IN is not None else None
_COST_PER_1K_OUT = os.environ.get("COST_PER_1K_OUT")
_COST_PER_1K_OUT = float(_COST_PER_1K_OUT) if _COST_PER_1K_OUT is not None else None

# The model the workshop-agent is bound to (guard-proxy-agenticburn#37). The workshop swaps it live by
# patching the Agent's spec.declarative.modelConfig to bedrock-haiku, bedrock-sonnet or bedrock-opus, which
# Argo CD leaves out of its diff, and MODEL_TIER above is read once at start: priced at MODEL_TIER, a swap
# from Nova Pro to Opus counts Opus tokens at a sixth of their input price. So each request is priced at the
# tier the Agent names now, read with the same get-only token as the badges (guard-proxy-reader grants
# get on workshop-agent) and cached as briefly. A ModelConfig bedrock-<tier> is that tier's row in the
# price table; any other name (gateway-local-qwen3, say) prices at MODEL_TIER. A read that fails says
# nothing about the binding, so it keeps the tier the last read found.
_AGENT_PATH = "/apis/kagent.dev/v1alpha2/namespaces/agent/agents/workshop-agent"
_MODEL_CONFIG_PREFIX = "bedrock-"
_BINDING_TTL = 5.0         # seconds, as the badges' cache; a swap restarts the agent pod, which is slower
_binding = {"at": None, "read": False, "model_config": None, "tier": MODEL_TIER}
_binding_lock = threading.Lock()


def _tier_of(model_config):
    """The price tier a ModelConfig name binds, or None when it names no row of the price table."""
    if isinstance(model_config, str) and model_config.startswith(_MODEL_CONFIG_PREFIX):
        tier = model_config[len(_MODEL_CONFIG_PREFIX):]
        if tier in TIER_PRICES_PER_1K:
            return tier
    return None


def bound_tier():
    """The tier a request is priced at now: the workshop-agent's bound model, read at most every 5 s."""
    now = time.monotonic()
    with _binding_lock:
        if _binding["at"] is not None and now - _binding["at"] < _BINDING_TTL:
            return _binding["tier"]
    ag = _k8s_get(_AGENT_PATH)
    with _binding_lock:
        _binding["at"] = now
        if ag is None:
            return _binding["tier"]
        try:
            model_config = ag["spec"]["declarative"].get("modelConfig")
        except (KeyError, TypeError, AttributeError):
            model_config = None
        tier = _tier_of(model_config)
        changed = not _binding["read"] or model_config != _binding["model_config"]
        _binding["read"], _binding["model_config"] = True, model_config
        _binding["tier"] = tier or MODEL_TIER
        priced = _binding["tier"]
    if changed and tier:
        log.info("pricing at %s: workshop-agent is bound to %s", tier, model_config,
                 extra={"event": "price_tier"})
    elif changed:
        log.warning("workshop-agent is bound to %r, which names no price tier; pricing at MODEL_TIER %s",
                    model_config, MODEL_TIER, extra={"event": "price_tier_unknown"})
    return priced


def request_model(tier):
    """gen_ai.request.model for a request priced at tier: MODEL_NAME when set, else the tier's model ID,
    else the tier name itself (guard-proxy-agenticburn#38)."""
    return MODEL_NAME or _TIER_TO_MODEL_ID.get(tier, tier)


def prices_per_1k(tier):
    """(USD per 1K input tokens, USD per 1K output tokens) at tier; an unknown tier prices as haiku."""
    row = TIER_PRICES_PER_1K.get(tier, TIER_PRICES_PER_1K["haiku"])
    return (row["in"] if _COST_PER_1K_IN is None else _COST_PER_1K_IN,
            row["out"] if _COST_PER_1K_OUT is None else _COST_PER_1K_OUT)


_cost_lock = threading.Lock()
# Per-session spend for the Challenge 4 cap, keyed by the browser session id the storefront sends.
# Bounded in practice: a workshop cluster sees tens of sessions and an entry is one float. The
# cluster-wide _cost below is untouched; it is what /cost reports and what the $25 backstop uses.
_session_cost = {}
_cost = {"requests": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "usd": 0.0}
# The same spend split by the model each request was reported as, for the cost gauge's series. Its values
# sum to _cost["usd"].
_cost_by_model = {}

# Export the running spend as `gen_ai.client.cost` via OTLP, the SAME pipeline as the spans and the
# standard token metric. Tokens are the standard `gen_ai.client.token.usage` (emitted by the kagent ADK
# agent, the actual LLM client). The OTel GenAI semconv defines NO monetary metric, so cost is a project
# suffix UNDER the standard gen_ai namespace (NOT a custom metric tree). Datadog derives cost from tokens
# in LLM Observability anyway; this metric is the pre-computed visual for the live counter. Attribute is
# the standard gen_ai.request.model.
#
# One series per model (guard-proxy-agenticburn#38): the workshop swaps the model live, so each series is
# what requests reported as that model have spent, and the series sum to the cluster's spend. The model the
# agent is bound to now always has a series, at 0 until it spends, so an idle cluster still reads $0.
if _meter is not None:
    def _observe_cost(_options):
        bound = request_model(bound_tier())
        with _cost_lock:
            by_model = dict(_cost_by_model)
        by_model.setdefault(bound, 0.0)
        for model, usd in by_model.items():
            # gen_ai.provider.name MUST be aws.bedrock for a Bedrock model (OTel GenAI semconv); without
            # it Datadog tags the cost metric provider as "N/A". The Collector also stamps this as a safety
            # net, but set it at the source too.
            yield Observation(usd, {"gen_ai.request.model": model, "gen_ai.provider.name": "aws.bedrock"})
    _meter.create_observable_gauge(
        "gen_ai.client.cost", callbacks=[_observe_cost], unit="USD",
        description="Estimated Bedrock spend (USD) for this cluster, derived from token usage.")

# Optional gamification: stream attendees' prompts to a side screen ("screen goes black, someone won").
# Projecting attendee input on a public screen needs moderation under the code of conduct, so capture
# is DEFAULT OFF and the /prompts feed only ever returns MODERATED text. verify-at-build: for a real
# public screen, back this with a content-moderation service (agentgateway external moderation / LLM
# Guard), not just this deterministic mask.
STREAM_ENABLED = os.environ.get("STREAM_PROMPTS", "off").lower() == "on"

# Cross-origin policy for the browser-facing surface (#151).
#
# THE CONTROL IS THE ORIGIN HEADER, NOT CORS. Tightening Access-Control-Allow-Origin does not stop a
# hostile page driving this agent, because CORS governs whether the caller may READ the response and
# never whether the request ARRIVES. A POST with Content-Type: text/plain is a CORS-simple request, so
# it skips the preflight entirely, and this proxy ignored Content-Type and json.loads the body anyway.
# Measured 2026-09-03 against a live cluster from outside:
#
#   POST /chat   -H 'Content-Type: text/plain' -d '{}'  ->  200, the handler ran
#   POST /a2a/   -H 'Content-Type: text/plain' -d '{}'  ->  200, the handler ran
#   OPTIONS /chat (preflight)                           ->  501, there is no do_OPTIONS
#
# So the recommendation in #151 to narrow CORS would have closed nothing. What does close it is
# rejecting a POST whose Origin is a site we do not serve. Browsers always send Origin on a
# cross-origin request, so this stops the drive-by; non-browser callers (the in-cluster A2A hop, the
# verify probes, curl) send no Origin at all and are deliberately unaffected.
#
# WHAT THIS DOES NOT DO: it does not authenticate anyone. A direct request from outside a browser has
# no Origin to check and still reaches the agent, bounded by RATE_LIMIT_RPM and COST_CAP_USD. Closing
# that needs a shared credential handed out with the cluster URL, which changes the attendee sign-in
# flow and is a workshop decision rather than a code one. See #151 option 2.
# Comma-separated: the start page is served on agenticburn.com and burritobot.co at once.
CONSOLE_ORIGINS = frozenset(
    o.strip()
    for o in os.environ.get(
        "CONSOLE_ORIGIN", "https://start.agenticburn.com,https://start.burritobot.co"
    ).split(",")
    if o.strip()
)


def _origin_allowed(handler):
    """True when the request carries no Origin, its own Host, or an instructor console origin."""
    origin = handler.headers.get("Origin")
    if not origin:
        return True   # not a browser cross-origin request; the A2A hop and every probe land here
    host = (handler.headers.get("Host") or "").split(":")[0].lower()
    try:
        o = urlparse(origin)
    except Exception:
        return False
    if o.hostname and host and o.hostname.lower() == host:
        return True   # the attendee's own console page, served from this same hostname
    return origin in CONSOLE_ORIGINS
# PROFANITY_LIST defaulted to EMPTY, which meant "moderated" masked only the BLOCK_LIST above (attack
# keywords and the C5 sentinel) and passed every slur and obscenity through untouched. That was harmless
# while capture was off; it is not once the feed is projected in a room, which is exactly the state this
# default now has to survive. A wordlist is not a moderation service and does not pretend to be: it is a
# floor, it catches the common cases, and the 280-character truncation below bounds the rest. The real
# control is that attendees are TOLD their prompts may appear on the instructor screen (run-of-show and
# lab step 0), which is what makes projecting them defensible at all.
#
# These terms match as whole words, plural included, never inside another word. Matched as substrings they
# masked the menu: "spicy" went up on the wall as "[redacted]y", and "cocktail", "grapes" and "Dickens" lost
# their middles. Endings are not generated either, because "spic" plus "es" or "ed" is "spices" and "spiced".
# So each inflected form and each compound that hides a term mid-word (fucking, bullshit) is its own entry.
#
# The default is the code-of-conduct spike's list (guard-proxy-agenticburn#81, from
# Unleash_an_Agent_Watch_It_Burn#1267's docs/research/2026-11-09-coc-floor/terms-proposed.rot13): 144
# expletives, slurs, sexual terms, threat and hate phrases, and the commonest expletives in Spanish, French,
# German, Portuguese and Italian. Matched as below, it caught 111 of the spike's 155 abuse texts, where the 40
# terms before it caught 37, and none of 5,198 stage texts or a week of /chat prompts and replies. It is kept
# ROT13-encoded, as the spike keeps it, so the public tree does not display the terms; PROFANITY_LIST is read
# as plain text. Go's config.DefaultProfanity is the same list.
#
# After the spike's 144 come 1,109 entries of Urban Dictionary-style slang (guard-proxy-agenticburn#84):
# abbreviations, vowel-dropped and phonetic spellings, alphanumeric spellings the leetspeak folding below does not
# reach, compounds that hide a term mid-word, slurs, sexual slang, coded hate numbers and symbols, and regional
# slang. 1,007 are from the maintained profane-words list (github.com/zautumnz/profane-words at 4b08ea9), less
# every entry with an ordinary meaning, every name or place, and every entry the 144 already catch; the rest are
# the issue's probe sample and its classes. None matches any of 5,254 stage texts and /chat turns, and none but
# 1488 matches a bare number. A short spelling that matched ordinary text is listed only in a form that does
# not, or not at all: decisions.md (2026-11-10) lists each with what it collided with.
_DEFAULT_PROFANITY = codecs.decode(
    # expletives
    "shpx,shpxf,shpxrq,shpxre,shpxref,shpxvat,shpxva,shpxnoyr,shpxsnpr,shpxurnq,shpxjvg,spx,shx,shd,"
    "sipx,cuhpx,zbgureshpxre,zbgureshpxvat,fuvg,fuvgf,fuvggl,fuvggvat,fuvggrq,fuvgurnq,fuvgsnpr,"
    "ohyyfuvg,ubefrfuvg,qvcfuvg,phag,ovgpu,ovgpurf,ovgpuvat,ovngpu,onfgneq,qvpx,qvpxurnq,pbpx,"
    "pbpxfhpxre,chffl,nffubyr,nefrubyr,qhzonff,wnpxnff,jnaxre,gjng,cevpx,fgsh,tgsb,jgs,"
    # slurs
    "avttre,avttn,snttbg,snt,qlxr,genaal,genaavr,furznyr,ergneq,ergneqrq,gneq,fcnm,zbatbybvq,xvxr,"
    "fcvp,puvax,tbbx,jrgonpx,ornare,enturnq,gbjryurnq,cnxv,pbba,jbc,qntb,ubaxl,erqfxva,mvccreurnq,"
    # sexual
    "frk,cbea,cbeab,oybjwbo,unaqwbo,phz,phzfubg,qvyqb,ubeal,gvgf,gvggvrf,obbof,ahqrf,rebgvp,"
    "guerrfbzr,nany frk,wrex bss,wnpx bss,anxrq obql,fyhg,juber,encr,encrq,encvfg,encvat,"
    # threats and hate phrases
    "xvyy lbhefrys,xvyy hefrys,xlf,unat lbhefrys,urvy uvgyre,fvrt urvy,juvgr cbjre,tnf gur wrjf,"
    # Spanish
    "craqrwb,craqrwn,zvreqn,chgn,chgb,pnoeba,puvatn,puvatnqn,znevpba,wbqre,pbñb,iretn,"
    # French
    "chgnva,zreqr,pbaaneq,fnybcr,raphyr,raphyre,chgr,"
    # German
    "fpurvffr,fpurvßr,uherafbua,sbgmr,jvpufre,svpx qvpu,fpuynzcr,"
    # Portuguese
    "pnenyub,cbeen,ivnqb,ohprgn,"
    # Italian
    "inssnaphyb,fgebamb,pnmmb,chggnan,"
    # abbreviations, acronyms and vowel-dropped spellings, and the finger (#84)
    "oyyfug,oyjwo,ofgeq,ogpu,ogpuf,ogpul,ppx,pxfpxe,pzfug,qnshd,qpx,qpxuq,qpxurnq,qvyvtns,s h,spxq,spxre,spxva,"
    "spxvat,spxa,spxat,spxe,sttg,stg,sx bss,sx h,sx lbh,sxq,sxre,sxva,sxvat,sxa,sxat,sxe,szy,sbnq,tsl,uaqwo,wsp,"
    "yzsnb,zs,zsxe,zgunspxn,zguespxat,zguespxe,zguesxe,attn,atte,attef,cepx,cffl,egeq,egeqq,fug,fug uq,fuguq,"
    "fugg,fuggl,jaxre,jaxe,\U0001f595,"
    # phonetic and alphanumeric respellings of the expletives, and their compounds (#84)
    "2 tveyf 1 phc,2t1p,5ug,nubyr,ncrfuvg,nfu0yr,nfu0yrf,nfubyrf,nff-ung,nff-cvengr,nffonttre,nffpybja,nffpbjobl,"
    "nffsnpr,nfftboyva,nffu0yrm,nffung,nffzhapu,nffzhapure,nffcvengr,nffjnq,nffjvcr,nffjvcrf,nmm,nmmubyr,o!gpuva,"
    "o7pu,onfgneqb,onfgneqm,onfgreqf,onfgreqm,orneqrqpynz,orngpu,orngbss,orrs phegnvaf,orrlbgpu,orbgpu,orlbgpu,"
    "ovtonfgneq,ovbgpu,ovgpunff,ovgpurq,ovgpure,ovgpuref,ovgpurm,ovgpuva,ovgpufync,ovgpugvg,ovgpul,ohyyfuvggrq,"
    "ohzoyrshpx,ohzshpx,ohggsnpr,olngpu,pnzry gbr,pnzrygbr,pnecrg zhapure,pnecrgzhapure,"
    "purrfr rngvat fheeraqre zbaxrl,pahg,pbpxovgr,pbpxpbjobl,pbpxxabo,pbpxzbaxrl,pbpxabo,phpx,phzfgnva,phaa,"
    "phaavr,phaal,phagsnpr,phagent,ploreshp,q0hpu3,qnltb,qrttb,qrtb,qvpxsnpr,qvpxsbeoenvaf,qvpxjnq,qvpxjrnfry,"
    "qvpxjrrq,qvys,qvegl fnapurm,qypx,qbbpuont,qbhpur ont,qbhpuront,qbhpurontf,qbhpurjnssyr,qhzo nff,qhzoovgpu,"
    "qhzoshpx,qhzofuvg,qhzfuvg,rssvat,snprshpxre,snpx,snaalshpxre,srt,svatreonat,sbx,sbbx,sbbxre,sevpx,shpxovgpu,"
    "shpxobl,shpxvatovgpu,shpxvatfuvgzbgureshpxre,shpxxabo,shpxahg,shpxahgf,shpxahgg,shpxahgm,shpxbss,shpxcvt,"
    "shpxfgvpx,shpxgneq,shpxgneqf,shpxjnq,shpxlbh,shqtr cnpxre,shqtrcnpxre,shtyl,shxp,shxgneq,shxgneqf,six,skpx,"
    "tnlobo,tvamb,tvambf,tbngpk,tbqqnzazhgunshpxre,tjnvyb,tjnvybf,tjrvyb,tjrvybf,u00e,u0ne,u0z0,u0zb,u0e,u0er,"
    "unvelonpx,unvelonpxf,urnqshpx,ubont,ubz0,ubzbqhzofuvg,ubbe,ubber,uber,vapry,w3ex0ss,wnpxfuvg,wrex0ss,"
    "wrexbss,wvtnebbav,wvtnebbavf,wvttreobb,wvwwvobb,wvwwvobbf,wvff,whatyr ohaavrf,xnsser,xvttre,xaboovat,"
    "xabornq,xaborq,xaboraq,xabournq,xabowbpxl,xabowbxrl,xabom,xhxfhtre,xhz,xhzohooyr,xhzre,xhzzre,xhzf,xjnv yb,"
    "xjnv ybf,yrzba cnegl,yrzbacnegl,yvzcqvpx,z0s0,z0sb,znpnpn,znshtyl,zttre,zttbe,zvys,zb-sb,zbs0,zbsb,zbbyvr,"
    "zbguresipxre,zhag,zhgunsrpxre,anfglovgpu,anfglub,avvtre,avvte,ayttre,ayttbe,cnpxv,cnyrfvzvna,cnapnxr snpr,"
    "cnapnxr snprf,cnabbpu,crpxrejbbq,cuhx,cuhxrq,cuhxre,cuhxvat,cuhxxrq,cuhxxre,cuhxxvat,cuhxf,cuhd,"
    "cvpxnavaavrf,cvrprbsfuvg,cvtshpxre,cbynp,cbba,cbbanav,cbbanal,cbbagnat,cbepu zbaxvrf,ce0a,ce1p,ce1x,cevp,"
    "cevpxurnq,chanav,chanaal,chanal,chaxnff,chagnat,chfvrf,chfl,dhrns,dhrrs,ergneqm,egneq,egneqf,fpnax,fpuybat,"
    "fu1gre,fu1ggre,fu1gm,fung,fuvgont,fuvgonttre,fuvgoyvzc,fuvgoenva,fuvgoerngu,fuvgpna,fuvgrngre,fuvgrngvat,"
    "fuvgrq,fuvgsnprq,fuvgsvg,fuvgsbeoenvaf,fuvgshyy,fuvguncraf,fuvgunccraf,fuvgurry,fuvgubyr,fuvgubhfr,fuvgvat,"
    "fuvgvatf,fuvgyvfg,fuvgbyn,fuvgbhgbsyhpx,fuvgcbg,fuvgfcvggre,fuvgfgnva,fuvggre,fuvggref,fuvggvrfg,fuvgl,"
    "fuvgm,fuvm,fuvmavg,fulg,fulggl,fulgl,fvzc,fxnapx,fxhz,fxhzont,fbabsnovgpu,fbabsovgpu,fcnturggvoraqre,fcvt,"
    "fcvtbggl,fcybbtr,fcyhqtr,fcbbtr,fghcvqshpx,fghcvqshpxre,fhzbsnovngpu,fjnzc thvarnf,gnpburnq,gnpburnqf,"
    "gne onovrf,guvpxyvc,guvpxyvcf,guhaqrephag,gjngurnq,gjngyvcf,gjnggl,gjngjnssyr,gjhag,gjhagre,jnax,jnaxvat,"
    "jnaxl,ju00e,jubne,jubenyvpvbhf,jubevat,jvyyvrjnaxre,mvtnob,mvtnobf,"
    # slurs, their respellings and compounds (#84)
    "norrq,nsevpbba,nyyvtngbe onvg,nyyvtngbeonvg,nenohfu,nenohfuf,nffpenpxre,nffavttre,nffjuber,oyhrthz,oyhrthzf,"
    "obpur,obpurf,obbat,obbatn,obbatf,obbgyvc,obbgyvcf,ohqqunurnq,ohqqunurnqf,ohyyqvxr,ohyyqlxr,ohee urnq,"
    "ohee urnqf,oheeurnq,oheeurnqf,ohgpuqvxr,ohgpuqlxr,pnzry wbpxrl,pnzry wbpxrlf,pnzrywbpxrl,pnzfyhg,pnzjuber,"
    "puvanzna,puvanzra,puvap,puvapf,puvat pubat,puvat pubatf,puvatpubat,puvatpubatf,puvaxl,pybtjbt,pbpxfhxn,"
    "pbbyvr,pbbyvrf,pbbanff,pbbanffrf,penpxjuber,phzfyhg,phagfyhg,pheel zhapure,pheel zhapuref,pheelzhapure,"
    "pheelzhapuref,qnexrl,qnexrlf,qnexvr,qnexvrf,qnexl,qngavttn,qrtbf,qvncre urnq,qvncre urnqf,qvncreurnq,"
    "qvncreurnqf,qvkvrqvxr,qvkvrqlxr,qbg urnq,qbg urnqf,qbgurnq,qbgurnqf,rnflfyhg,snt1g,sntont,sntrg,sntshpxre,"
    "sntt1g,snttvg,snttvgg,snttbgpbpx,sntvg,sntbg,sntbgf,sntg,sntgneq,sntm,shpxjuber,tngbe onvg,tngbeonvg,"
    "tnlzhgunshpxvajuber,tnlgneq,tvccb,tvccbf,tbyyvjbt,tbyyvjbtf,tbbxrlr,tbbxrlrf,tbbxvrf,tbbxl,ternfronyy,"
    "ternfronyyf,tebvq,tebvqf,tlcb,tlcbf,tlccb,tlccbf,unys oerrq,unys pnfgr,unysoerrq,unyspnfgr,urro,urrof,ubr,"
    "ubaxrl,ubaxrlf,ubaxvr,ubaxvrf,ulzvr,ulzvrf,vxrl,vxrlzb,vxrlzbf,vawha,vawhaf,wnc,wncf,wrjobl,wvtnobb,"
    "wvtnobbf,wvttnob,wvttnobb,wvttnobf,wvttnf,whatyr ohaal,whatyrohaal,xnssre,xnssve,xenhg,xenhgf,xlxr,xlxrf,"
    "yrmob,yrmobf,yrmmvr,yrmmvrf,yrmml,yvyavtyrg,zpsnttrg,zbba pevpxrg,zbba pevpxrgf,zbbapevpxrg,zbbapevpxrgf,"
    "zbffurnq,zbhyvrjbc,a1te,anfglfyhg,anfgljuber,avt abt,avtn,avtnef,avtnf,avtref,avtrggr,avtrggrf,avtt,avtt4u,"
    "avttnu,avttnuf,avttne,avttnenppv,avttnef,avttnm,avttreurnq,avttreubyr,avttbe,avtterff,avtterffrf,avtthu,"
    "avtthuf,avtthe,avtthef,avtyrg,avtabt,avtbe,avtbef,avte,avten,avtenf,avter,avterf,avterff,avtf,avthe,"
    "bar phc gjb tveyf,cnxvr,cvpnavaal,cvppnavaal,cvpxnavaal,cvyybjovgre,cbynpx,cbynpxf,cbynx,cbepu zbaxrl,"
    "cbepuzbaxrl,ebhaqrlr,fnaqavttre,fpuinegfr,fpuinegfra,fpujnegmr,fpujnegmra,frkjuber,fxnax,fxnaxovgpu,fxnaxrr,"
    "fxnaxrl,fxnaxshpx,fxnaxf,fxnaxjuber,fxnaxl,fxnaxlovgpu,fxnaxljuber,fynagrlr,fybcrurnq,fybcrurnqf,fyhgont,"
    "fyhgohpxrg,fyhgqhzcre,fyhgxvff,fyhggvat,fyhggl,fyhgjrne,fyhgjuber,fyhgm,fabjonpx,fabjavttre,fcnturggvavttre,"
    "fcrnepuhpxre,fcrnepuhpxref,fcvx,fcvxf,fdhnj,gne onol,gneonol,gubg,gvzoreavttre,gbjry urnq,gbjry urnqf,ge00a,"
    "ge00af,gebba,gebbaf,gjb tveyf bar phc,gjbovgjuber,ju0ersnpr,juvttre,juvttref,juvgravttre,jubernyvpvbhf,"
    "juberont,juberq,jubersnpr,jubershpxre,juberubccre,juberubhfr,jvttn,jvttnf,jvttre,jvttref,jbt,jbtf,lvq,lvqf,"
    # sexual slang (#84)
    "nff zbaxrl,nffonaqvg,nffonat,nffonatrq,nffonatre,nffonatf,nffoynfgre,nffpbpx,nffshpx,nffshpxre,nffshxxn,"
    "nffurnq,nffubym,nffubccre,nffuber,nffwnpxre,nffwbpxrl,nffxvff,nffxvffre,nffxybja,nffyvpx,nffyvpxre,nffybire,"
    "nffznfgre,nffzbaxrl,nffcnpxre,nffchccvrf,nffenzzre,nffenatre,nfffuvg,nfffhpxre,nffjubyr,o00ovrm,o00om,"
    "onqshpx,onffgreq,onffgreqf,oybj wbo,oyhzcxva,obare,obaref,ohxnxr,ohxxnxr,ohgg cyht,ohggshpx,ohggshpxre,"
    "ohggshpxref,ohggubyr,ohggcyht,ohggjvcr,p0x,pyrirynaq fgrnzre,pyvgsnpr,pyvgshpx,pyhfgreshpx,pbpxoybpx,"
    "pbpxoybpxre,pbpxohetre,pbpxsnpr,pbpxshpxre,pbpxurnq,pbpxubyfgre,pbpxwbpxrl,pbpxxabpxre,pbpxxabxre,"
    "pbpxyvpxre,pbpxybire,pbpxznfgre,pbpxzbatyre,pbpxzbatehry,pbpxzhapu,pbpxzhapure,pbpxabfr,pbpxahttrg,"
    "pbpxdhrra,pbpxevqre,pbpxfuvg,pbpxfzvgu,pbpxfzbxre,pbpxfhpre,pbpxfhpx,pbpxfhpxrq,pbpxfhpxvat,pbpxfhpxf,"
    "pbpxfhxxn,pbpxgrnfr,pbxzhapure,pbxfhpxn,pbbpuvr,pbbpul,pernzcvr,phzohooyr,phzqhzcfgre,phzsrfg,phzthmmyre,"
    "phzwbpxrl,phzyvpxe,phzdhrra,phzfhpxre,phzgneg,phagrlrq,phagshpx,phagshpxre,phagubyr,phaguhagre,phagyvpx,"
    "phagyvpxre,phagyvpxvat,phagfhpxre,phagm,ploreshpx,ploreshpxrq,ploreshpxre,ploreshpxref,ploreshpxvat,"
    "qrrcguebng,qrrcguebngvat,qrfgebllbhechffl,qvpxont,qvpxorngre,qvpxorngref,qvpxoenva,qvpxqvccre,qvpxsyvccre,"
    "qvpxshpx,qvpxubyr,qvpxvfu,qvpxwhvpr,qvpxyrff,qvpxyvpx,qvpxyvpxre,qvpxzvyx,qvpxzbatre,qvpxcvp,qvpxevccre,"
    "qvpxfvccre,qvpxfync,qvpxfyvpxre,qvpxfhpxre,qvpxjuvccre,qvpxjbq,qvpxmvccre,qbaxrl chapu,qbaxrlchapu,qevcqvpx,"
    "qhznff,qiqn,rng zl nff,rngnqvpx,rngzlnff,rngchffl,snfgshpx,sngshpx,sngshpxre,spxphz,sphx,sphxre,sphxvat,"
    "srypu,srypure,srypuvat,srygpu,srygpure,srygpuvat,svatreshpx,svatreshpxrq,svatreshpxre,svatreshpxref,"
    "svatreshpxvat,svatreshpxf,svfgshpx,svfgshpxrq,svfgshpxre,svfgshpxref,svfgshpxvat,svfgshpxvatf,svfgshpxf,"
    "svfgvat,sbbgshpx,sbbgshpxre,sernxshpx,sernxlshpxre,serrshpx,shpxn,shpxnff,shpxont,shpxobbx,shpxoenva,"
    "shpxohqql,shpxohgg,shpxq,shpxrqhc,shpxrefhpxre,shpxsrfg,shpxsernx,shpxsevraq,shpxure,shpxubyr,shpxvan,"
    "shpxvaahgf,shpxvaevtug,shpxvg,shpxzr,shpxzrng,shpxzruneq,shpxzbaxrl,shpxa,shpxahttrg,shpxchccrg,shpxe,"
    "shpxgbl,shpxgebcul,shpxhc,shpxjuvg,shpxlbznzn,shxnu,shxra,shxre,shxva,shxvat,shxxnu,shxxra,shxxre,shxxva,"
    "shxxvat,shxjuvg,shxjvg,shashpx,shk,shk0e,shkbe,tnat onat,tnatonat,tnatonatrq,tnatonatf,tnfflnff,tnlnff,"
    "tbxxha,tbyqra fubjre,tbyqrafubjre,unaq wbo,uneqpberfrk,uneqba,ubgchffl,vagurnff,wnpxbss,wvfvz,wvfz,wvm,"
    "wvmvz,wvmva,wvmwhvpr,wvmz,wvma,wvmm,wvmmq,wvmmrq,wvmmvz,wvmmva,wvmma,wvmmhz,xvffnff,xbpx,xbbpu,xbbpurf,xhag,"
    "xhagf,znxrzrphz,zvaqshpx,zbgun shxre,zbgun shxxnu,zbgun shxxre,zbgunshpx,zbgunshpxn,zbgunshpxnf,zbgunshpxnm,"
    "zbgunshpxrq,zbgunshpxre,zbgunshpxref,zbgunshpxva,zbgunshpxvat,zbgunshpxvatf,zbgunshpxf,zbgure shxnu,"
    "zbgure shxre,zbgure shxxnu,zbgure shxxre,zbgureshpx,zbgureshpxn,zbgureshpxrq,zbgureshpxva,zbgureshpxxn,"
    "zbgureshpxf,zbgueshpxre,zgureshpxre,zgueshpx,zgueshpxre,zgueshpxvat,zgespx,zgeshpx,zgeshpxre,zhss qvire,"
    "zhssqvir,zhssqvire,zhssqvivat,zhssvaqvire,zhgun shxnu,zhgun shxre,zhgun shxxnu,zhgun shxxre,zhgunshpxnm,"
    "zhgunshpxre,zhgunshpxxre,zhgureshpxre,zhgureshpxvat,zhgueshpxvat,arrqgurqvpx,abshpxvatjnl,ahgshpxre,"
    "cravfshpxre,cvaqvpx,cbeasyvpx,cbeaxvat,cbeacevaprff,cgup,ch55v,chffv,chffvr,chffvrf,chfflqrfgeblre,"
    "chfflrngre,chfflsneg,chfflshpx,chfflshpxre,chfflyvpxre,chfflyvpxvat,chfflyvcf,chfflybire,chfflcnynpr,"
    "chfflcbhaqre,dhvz,eragnshpx,evzwbo,funirqchffl,fuvgnff,fuvgphag,fuvgqvpx,fuvgshpx,fuvgshpxre,fuvgybnq,"
    "fubegshpx,fxhyyshpx,fgencba,fhpxqvpx,fhpxzlnff,fhpxzlqvpx,fhpxzlgvg,grnonttvat,gvgshpx,gvgshpxre,gvgshpxva,"
    "gvgwbo,gvgyvpxre,gvgybire,gvggvrshpxre,gvggl,gvgglshpx,gvgglshpxre,gvgglf,gvggljnax,gvgjnax,gvgl,gho tvey,"
    "ghotvey,hapyrshpxre,hashpxnoyr,hcgurnff,juvfxrlqvpx,juvfxlqvpx,lbheobbof,lbhegvgf,"
    # coded hate: numbers, acronyms, phrases and symbols (#84)
    "1488,6zjr,qnl bs gur ebcr,tgxeja,urvy,uu88,wrjf jvyy abg ercynpr hf,xxx,anmv,anmvf,arbanmv,enubjn,"
    "juvgrcbjre,jcjj,\u03df\u03df,\u16cb\u16cb,\u5350,"
    # regional slang and its abbreviations: Jamaican, Irish, British, Spanish, French, German, Portuguese, Italian (#84)
    "4e5r,ne5r,neefr,nefpuybpu,nefr,oryyraq,oybbqpynng,obyyvpx,obyybpx,obyybpxf,obyybx,obyybk,obzonpyng,"
    "ohzonpynng,ohzonpybg,puvatnqb,puvatnqbf,peyu,snaphyb,srpx,srpxre,sbqn fr,sbqnfr,tbofuvgr,uqytc,xabowbpxrl,"
    "xey,zvapuvn,zvatr,zvatre,avdhr gn zrer,agz,cdc,cga,enfpynng,enffpynng,fpurvff,fpurvff qenhs,fuvgr,fuvgront,"
    "fuvgrl,fulgr,fgebamn,gn thrhyr,gbzne ab ph,gbffre,inssn,ifs,igap",
    "rot13",
)
PROFANITY = [t.strip().lower() for t in os.environ.get("PROFANITY_LIST", _DEFAULT_PROFANITY).split(",") if t.strip()]

# Each character of a text is folded on its own before the terms are matched, so a match maps back onto the
# original text one to one: zero-width characters and soft hyphens are dropped, compatibility and accented
# forms reduced to their base letter (fullwidth "ｆ" to "f", "ñ" to "n"), Cyrillic and Greek look-alikes mapped
# to Latin, and the result lower-cased. A term then matches as a whole word with each letter repeatable as
# itself or a digit stand-in ("fuuuck", "sh1iit"), each letter's leetspeak stand-ins below, "*" for any vowel
# ("f*ck"), or the whole term spelt out with one separator between every letter ("f u c k", "c.u.n.t"); a
# separator between only some letters does not count, so "put a" is not "puta". A phrase's words match with any run of separators between them. The
# spike's floor.py is the reference this follows, and internal/guard/conduct.go is the same in RE2.
_CONDUCT_LEET = {"a": "4@", "b": "8", "c": "(", "e": "3", "g": "9", "i": "1!|", "l": "1|", "o": "0", "s": "5$",
                 "t": "7+"}
_CONDUCT_VOWELS = frozenset("aeiou")
_CONDUCT_SEP = r"[\s._*\-]"
_CONDUCT_ZERO_WIDTH = frozenset("\u200b\u200c\u200d\u2060\ufeff\u00ad")
# Cyrillic and Greek letters that render as Latin ones.
_CONDUCT_HOMOGLYPH = str.maketrans("аеорсухіјѕԁԛԝкмтвнАЕОРСУХІЈЅКМТВНαοερτυνκιΑΒΕΖΗΙΚΜΝΟΡΤΥΧ",
                                   "aeopcyxijsdqwkmtbhaeopcyxijskmtbhaoeptuvkiabezhikmnoptyx")


def conduct_fold(text):
    """text folded one character at a time, and for each folded character the index it came from."""
    out, where = [], []
    for i, ch in enumerate(text):
        if ch in _CONDUCT_ZERO_WIDTH:
            continue
        base = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c)) or ch
        out.append(base[0].translate(_CONDUCT_HOMOGLYPH).lower()[0])
        where.append(i)
    return "".join(out), where


def _conduct_chars(ch):
    """The characters a letter of a term matches: itself, its leetspeak stand-ins, and "*" for a vowel."""
    return ch + _CONDUCT_LEET.get(ch, "") + ("*" if ch in _CONDUCT_VOWELS else "")


def _conduct_letter(ch):
    return "[" + "".join(re.escape(c) for c in _conduct_chars(ch)) + "]"


# Matching is linear in the length of the text (Unleash_an_Agent_Watch_It_Burn#1300). With each letter its whole
# class repeated (`[s5$]+`) a run of `$`, `5$`, `(` or `4@` let a term start after every symbol in it, each start
# read to the run's end, and so did each way of splitting the run between two letters: 4,000 characters cost
# seconds. So a letter is one character of its class, then a run of only the letter and its digit stand-ins, less
# any the next letter's class holds. This is floor.py's _run, and internal/guard/conduct.go's conductRun, except that
# a run of the whole class is written `[k][k]*` where floor.py writes `[k]+`, so every alternative of a term starts
# with exactly one character of its first letter.
def _conduct_run(ch, after):
    """One letter of a word, `after` being the letter that follows it ("" at the word's end)."""
    own = ch + _CONDUCT_LEET.get(ch, "")
    again = "".join(c for c in own if (c == ch or c.isdigit()) and c not in after + _CONDUCT_LEET.get(after, ""))
    if not again:
        return _conduct_letter(ch)
    return _conduct_letter(ch) + "[" + re.escape(again) + "]*"


def _conduct_word(word):
    return "".join(_conduct_run(c, word[i + 1:i + 2]) for i, c in enumerate(word))


def _conduct_term(term):
    """The term's alternatives: its words' letters run together, and a one-word term of three or more letters also
    spelt out with one separator between every letter. Each starts with one character of its first letter."""
    words = conduct_fold(term)[0].split()
    alternatives = [(_CONDUCT_SEP + "+").join(_conduct_word(w) for w in words)]
    if len(words) == 1 and len(words[0]) >= 3:
        alternatives.append(_CONDUCT_SEP.join(_conduct_letter(c) for c in words[0]))
    return alternatives


# The alternatives are grouped by the character they start with (#84). One alternation of the terms in a row, as the
# spike's floor.py builds it, has the engine try every term at every word start: with 1,250 terms a 4,000-character
# run of "$" took 120 ms. Grouped, a word start tries the alternatives its first character can begin and no others.
# Each character goes to one group, the group of every first letter whose class holds it ("*" is any vowel's, "1"
# both i's and l's), and a group keeps the alternatives in the order the single alternation tries them, longest term
# first. So at any position the same alternatives are tried in the same order, and the spans are the same.
def _profanity_pattern(terms):
    # An empty list matches nothing; joined as-is it would be an empty alternation that matches everywhere. A
    # term that folds to nothing, zero-width characters alone, is no term for the same reason.
    terms = [t for t in terms if conduct_fold(t)[0].split()]
    if not terms:
        return None
    # (first letter, the alternative after that letter's one character)
    alternatives = []
    for term in sorted(terms, key=len, reverse=True):
        first = conduct_fold(term)[0].split()[0][0]
        alternatives += [(first, alt[len(_conduct_letter(first)):]) for alt in _conduct_term(term)]
    owners = {}
    for first, _ in alternatives:
        for ch in _conduct_chars(first):
            owners.setdefault(ch, set()).add(first)
    groups = {}
    for ch, firsts in owners.items():
        groups.setdefault(frozenset(firsts), []).append(ch)
    alternation = "|".join(
        "[" + "".join(re.escape(ch) for ch in chars) + "](?:"
        + "|".join(rest for first, rest in alternatives if first in firsts) + ")"
        for firsts, chars in groups.items()
    )
    return re.compile(r"(?<![a-z0-9])(?:" + alternation + r")s?(?![a-z0-9])")


_PROFANITY_RE = _profanity_pattern(PROFANITY)

# The code-of-conduct check on /chat (guard-proxy-agenticburn#78, asked by Unleash_an_Agent_Watch_It_Burn#1267).
# Michael, 2026-11-09: "Make sure that a lot of the negative keywords never show up in the system no matter
# what the state of the guardrails is." So it runs on every /chat turn whatever /toggle says, and no toggle
# names it: a prompt carrying a term is refused before any guard, cap or the agent sees it, and every term in
# the agent's reply is masked before the reply leaves, output guard or not. It is the wall's own list above,
# so the wall, the chat page's saved rail and the chat agree on what a term is. The stage guards stay exactly
# as they were, so the attacks still land with every guard off: the check is for abuse, and none of the
# stage's prompts carries a term. Like ALWAYS_REDACT it never sets `guarded` and stamps no
# witb.guard.decision, because both say a toggleable guard acted. In-process, so no network call.
CONDUCT_REFUSAL = "Let's keep it kind: BurritoBot only answers within the event's code of conduct."


def conduct_matches(text):
    """The (start, end) spans of text that break the event's code of conduct, in order.

    The check's one matcher: the refusal and the mask both read it, so a classifier (the research spike in
    Unleash_an_Agent_Watch_It_Burn#1267 weighs LLM Guard's Toxicity scanner and Bedrock Guardrails) replaces
    the term list here alone. A classifier with no spans answers the whole text as one. The terms are matched
    on the folded text, and each match is mapped back onto the characters it came from."""
    if _PROFANITY_RE is None or not text:
        return []
    folded, where = conduct_fold(text)
    return [(where[m.start()], where[m.end() - 1] + 1) for m in _PROFANITY_RE.finditer(folded)]


def conduct_mask(text):
    """text with every code-of-conduct span replaced by [redacted], the wall's own mark."""
    for start, end in reversed(conduct_matches(text)):
        text = text[:start] + "[redacted]" + text[end:]
    return text


# A Bedrock guardrail on the /chat prompt (guard-proxy-agenticburn#81), for the abuse no term list sees: hate
# speech without a slur, insults, threats. The spike (Unleash_an_Agent_Watch_It_Burn#1267) chose Amazon Bedrock
# Guardrails, Standard tier; the guardrail and guard-proxy's IAM are Unleash_an_Agent_Watch_It_Burn#1279. With
# CONDUCT_GUARDRAIL_ID set, a prompt the terms pass is sent to ApplyGuardrail (source INPUT) and refused with
# CONDUCT_REFUSAL when the guardrail intervenes. It fails open: on any error, or once CONDUCT_GUARDRAIL_TIMEOUT
# has passed, the prompt goes on, because the terms still hold. The spike measured 466 ms median per call from
# outside the cluster, 327 ms of it Bedrock's. Credentials come from the environment or, as EKS Pod Identity
# sets them up, from the container credentials endpoint; the endpoint is the SDKs' AWS_ENDPOINT_URL_* when
# set, so the Go build (the AWS SDK) and this one reach the same place.
CONDUCT_GUARDRAIL_ID = os.environ.get("CONDUCT_GUARDRAIL_ID", "").strip()
CONDUCT_GUARDRAIL_VERSION = os.environ.get("CONDUCT_GUARDRAIL_VERSION", "").strip()
CONDUCT_GUARDRAIL_TIMEOUT = 1.5
_AWS_REGION = (os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "").strip()
if CONDUCT_GUARDRAIL_ID and not CONDUCT_GUARDRAIL_VERSION:
    raise SystemExit("CONDUCT_GUARDRAIL_ID is set without CONDUCT_GUARDRAIL_VERSION: "
                     "ApplyGuardrail needs both (a version number or DRAFT)")
if CONDUCT_GUARDRAIL_ID and not _AWS_REGION:
    raise SystemExit("CONDUCT_GUARDRAIL_ID is set without AWS_REGION or AWS_DEFAULT_REGION")
_BEDROCK_URL = (os.environ.get("AWS_ENDPOINT_URL_BEDROCK_RUNTIME") or os.environ.get("AWS_ENDPOINT_URL")
                or f"https://bedrock-runtime.{_AWS_REGION}.amazonaws.com").rstrip("/")
_aws_creds_lock = threading.Lock()
_aws_creds = {}


def _aws_credentials(timeout):
    """(access key id, secret access key, session token or "") from AWS_ACCESS_KEY_ID and
    AWS_SECRET_ACCESS_KEY, else from the container credentials endpoint EKS Pod Identity sets up
    (AWS_CONTAINER_CREDENTIALS_FULL_URI, its token in AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE), kept until
    five minutes before they expire. Raises when neither is set."""
    key, secret = os.environ.get("AWS_ACCESS_KEY_ID"), os.environ.get("AWS_SECRET_ACCESS_KEY")
    if key and secret:
        return key, secret, os.environ.get("AWS_SESSION_TOKEN", "")
    uri = os.environ.get("AWS_CONTAINER_CREDENTIALS_FULL_URI")
    if not uri:
        raise RuntimeError("no AWS credentials: set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY, "
                           "or AWS_CONTAINER_CREDENTIALS_FULL_URI (EKS Pod Identity)")
    with _aws_creds_lock:
        held = _aws_creds.get("held")
        if held and held[3] - time.time() > 300:
            return held[:3]
    headers = {}
    token_file = os.environ.get("AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE")
    if token_file:
        with open(token_file, encoding="utf-8") as f:
            headers["Authorization"] = f.read().strip()
    elif os.environ.get("AWS_CONTAINER_AUTHORIZATION_TOKEN"):
        headers["Authorization"] = os.environ["AWS_CONTAINER_AUTHORIZATION_TOKEN"]
    with urllib.request.urlopen(urllib.request.Request(uri, headers=headers), timeout=timeout) as r:
        doc = json.loads(r.read())
    expires = datetime.fromisoformat(doc["Expiration"].replace("Z", "+00:00")).timestamp()
    held = (doc["AccessKeyId"], doc["SecretAccessKey"], doc.get("Token") or "", expires)
    with _aws_creds_lock:
        _aws_creds["held"] = held
    return held[:3]


def _sigv4(method, url, body, creds, region, service, now):
    """The headers that sign one request with AWS Signature Version 4, as the AWS SDKs sign it."""
    key, secret, token = creds
    u = urlparse(url)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    day = stamp[:8]
    headers = {"content-type": "application/json", "host": u.netloc, "x-amz-date": stamp}
    if token:
        headers["x-amz-security-token"] = token
    names = sorted(headers)
    signed = ";".join(names)
    # Every service but S3 signs the path encoded twice: once on the wire, once more here.
    canonical = "\n".join([method, quote(u.path, safe="/-_.~"), u.query,
                           *(f"{n}:{headers[n]}" for n in names), "", signed,
                           hashlib.sha256(body).hexdigest()])
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    k = ("AWS4" + secret).encode()
    for part in (day, region, service, "aws4_request"):
        k = hmac.new(k, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = (f"AWS4-HMAC-SHA256 Credential={key}/{scope}, SignedHeaders={signed}, "
                                f"Signature={signature}")
    return headers


def conduct_guardrail_blocks(prompt, deadline):
    """True when the configured Bedrock guardrail intervenes on prompt, sent as source INPUT. False when no
    guardrail is configured, and False, logged, on any error or once CONDUCT_GUARDRAIL_TIMEOUT or the turn's
    deadline has passed: the guardrail fails open, because the terms still hold."""
    if not CONDUCT_GUARDRAIL_ID:
        return False
    end = time.monotonic() + CONDUCT_GUARDRAIL_TIMEOUT
    if deadline is not None:
        end = min(end, deadline)
    try:
        creds = _aws_credentials(max(0.001, end - time.monotonic()))
        url = (f"{_BEDROCK_URL}/guardrail/{quote(CONDUCT_GUARDRAIL_ID, safe='')}"
               f"/version/{quote(CONDUCT_GUARDRAIL_VERSION, safe='')}/apply")
        body = json.dumps({"source": "INPUT", "content": [{"text": {"text": prompt}}]}).encode()
        headers = _sigv4("POST", url, body, creds, _AWS_REGION, "bedrock", datetime.now(timezone.utc))
        left = end - time.monotonic()
        if left <= 0:
            raise TimeoutError(f"the {CONDUCT_GUARDRAIL_TIMEOUT:g}s guardrail deadline passed")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=left) as r:
            verdict = json.loads(r.read())
        action = verdict["action"]
    except Exception as exc:
        log.warning("code-of-conduct guardrail gave no verdict; the prompt goes on: %s: %s",
                    type(exc).__name__, exc, extra={"event": "conduct_guardrail_error"})
        return False
    return action == "GUARDRAIL_INTERVENED"

# Personal data on the wall (Unleash_an_Agent_Watch_It_Burn#646). The wall is projected, so a prompt that
# carries an email, a phone or card number, or a street address goes up redacted. The prompt itself still
# goes to the bot unchanged; only the feed copy is masked. Deterministic like the wordlist, and a floor in
# the same way: a name on its own is not caught.
_PII_PATTERNS = (
    # Email addresses. The lookbehind starts a match only where a run of address characters starts;
    # without it a long run with no @ is rescanned from every position, and 100 KB took 30 seconds.
    re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
    # Seven or more digits in one run, with spaces, dots, dashes or brackets between them: phone numbers,
    # payment cards, US Social Security numbers, account numbers. A burrito order never needs seven digits
    # in a row, so no card check (Luhn) narrows it: a projected sixteen-digit number is redacted either way.
    re.compile(r"\+?\(?\d(?:[\s().-]{0,2}\d){6,}\)?"),
    # A street address: a house number, one to four words, and a street suffix ("1600 Pennsylvania Avenue",
    # "42 elm st"). A word like "the" or "for" between them ends the match, so "3 tacos for the road" stays.
    re.compile(
        r"\b\d{1,6}(?:\s+(?!(?:the|a|an|and|or|for|on|to|of|in|at|with|my|your|our|all|some)\b)[a-z0-9.'-]+){1,4}"
        r"\s+(?:street|st|avenue|ave|road|rd|boulevard|blvd|lane|ln|drive|dr|way|court|ct|place|pl|"
        r"terrace|ter|parkway|pkwy|highway|hwy|circle|cir)\b\.?",
        re.IGNORECASE,
    ),
)
_stream_lock = threading.Lock()
_prompts = collections.deque(maxlen=50)  # recent (MODERATED prompt, witch name or "") pairs for the display

# Automated probes must not land in the side-screen feed. verify/browser-smoke.py sends a prompt to every
# cluster on every run, and with capture on those accumulate in the deque above and merge across 13
# clusters in the instructor console, which reads as a wall of duplicate prompts nobody typed. Observed
# 2026-09-03: the ONLY text on the whole fleet was the smoke test's own prompt, repeated per run.
#
# The marker is checked before moderation and before the append, so a probe is answered normally and
# simply never recorded. It is deliberately an ugly literal: it must never collide with something an
# attendee would plausibly type, and it should be obvious in a log that a probe was responsible.
PROBE_MARKER = "[[wib-probe]]"


def _is_probe(text):
    return PROBE_MARKER in (text or "")


def without_probe(text):
    """The /chat prompt as a guest would send it: the marker is the feed's alone (guard-proxy-agenticburn#70).
    Forwarded, the model read "wib-probe" as a support ticket id and called the turn a probe, so a live probe
    tested a message no guest sends. A prompt without the marker is returned exactly as typed."""
    if not _is_probe(text):
        return text
    return text.replace(PROBE_MARKER, "").strip()


def moderate(text):
    """Mask personal data, block-listed and profane terms so the side-screen stays within the code of conduct.

    Personal data goes first, while an address or number is still whole, and everything is masked before
    the 280-character cut, so the cut can never leave half a card number showing."""
    masked = text
    for pattern in _PII_PATTERNS:
        masked = pattern.sub("[redacted]", masked)
    low = masked.lower()
    for term in BLOCK_LIST:
        if term and term in low:
            # Case-insensitive: "Delete" or "DELETE" must be masked too, not only the lower and upper forms.
            masked = re.sub(re.escape(term), "[redacted]", masked, flags=re.IGNORECASE)
            low = masked.lower()
    # The code-of-conduct check's own matcher, so the wall and /chat agree on what a term is (#81).
    masked = conduct_mask(masked)
    return masked[:280]


def feed_witch(witch):
    """The name a feed entry carries: the shaped /chat name as given, or "" when there is none or when
    moderation would mask any of it. A name is a label on a projected screen, so a half-masked one is
    dropped whole rather than shown as "[redacted] the Ghoul"."""
    return witch if witch and moderate(witch) == witch else ""


def record_prompt(text, witch=""):
    """Keep one moderated prompt and its sender's witch name for the side-screen feed."""
    if not STREAM_ENABLED or not text or _is_probe(text):
        return
    entry = (moderate(text), feed_witch(witch))
    with _stream_lock:
        _prompts.append(entry)


def feed_payload():
    """GET /prompts. `prompts` stays the bare moderated texts every reader already parses as strings, and
    `witches` is each one's witch name at the same index ("" for none, and always "" on the A2A path). Names
    ride beside the texts rather than with a second copy of them: lab-platform refuses a body over 256 KiB
    whole, and fifty 280-character prompts of escaped emoji are already about 168 KB."""
    with _stream_lock:
        kept = list(_prompts)
    return {"enabled": STREAM_ENABLED, "prompts": [text for text, _ in kept],
            "witches": [witch for _, witch in kept]}


def record_usage(resp, session=""):
    """Pull kagent token usage from an A2A response and add it to the running cost tally.

    Priced at the tier the agent is bound to now, and added to that tier's model in the cost gauge. Returns
    the USD it added, 0.0 for a reply without usage."""
    result = resp.get("result", {}) if isinstance(resp, dict) else {}

    # Live kagent 0.9.9 emits the token usage at result.metadata.kagent_usage_metadata (confirmed on a
    # real A2A response on watch-it-burn-test, 2026-06-21). the research spikes (removed 2026-08-30; see `git log --diff-filter=D -- research/`) read the published docs as
    # `adk_usage_metadata`, but the running controller uses `kagent_usage_metadata`; the live cluster is
    # ground truth. Accept both keys (kagent first) so a future re-key does not silently zero the counter.
    def _usage(metadata):
        metadata = metadata or {}
        return metadata.get("kagent_usage_metadata") or metadata.get("adk_usage_metadata")

    meta = _usage(result.get("metadata"))
    if not meta:
        meta = _usage(result.get("status", {}).get("message", {}).get("metadata"))
    if not isinstance(meta, dict):
        return 0.0
    pin = int(meta.get("promptTokenCount", 0) or 0)
    pout = int(meta.get("candidatesTokenCount", 0) or 0)
    tier = bound_tier()
    model = request_model(tier)
    price_in, price_out = prices_per_1k(tier)
    spent = (pin / 1000.0) * price_in + (pout / 1000.0) * price_out
    with _cost_lock:
        _cost["requests"] += 1
        _cost["input_tokens"] += pin
        _cost["output_tokens"] += pout
        _cost["total_tokens"] += int(meta.get("totalTokenCount", pin + pout) or 0)
        _cost["usd"] += spent
        _cost_by_model[model] = _cost_by_model.get(model, 0.0) + spent
        if session:
            _session_cost[session] = _session_cost.get(session, 0.0) + spent
    return spent


def _post_guard(path, payload, deadline=None):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{LLM_GUARD_URL}{path}",
        data=data,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {LLM_GUARD_TOKEN}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_call_timeout(deadline)) as r:
        return json.loads(r.read())


def blocklisted(text):
    """Return the matched block-list term, or None. Deterministic, pre-LLM, zero token spend."""
    low = text.lower()
    return next((t for t in BLOCK_LIST if t in low), None)


def input_verdict(text, deadline=None):
    """(allowed, flagged) for the request prompt. flagged names the scanners that scored it above zero,
    as (name, risk) pairs, highest risk first, so a refusal can say why. It is empty when the scanner
    gave no scores or could not be reached. Raises TurnExpired when a /chat turn's deadline cut it off."""
    try:
        # BanTopics shares the input scanner list, and it belongs to the topic stage: without the
        # suppress, turning on C6 would also ban topics.
        verdict = _post_guard("/analyze/prompt", {"prompt": text, "scanners_suppress": ["BanTopics"]},
                              deadline)
    except Exception:
        _raise_if_expired(deadline)
        return not FAIL_CLOSED, []
    scores = verdict.get("scanners")
    flagged = []
    if isinstance(scores, dict):
        flagged = sorted(((str(k), float(v)) for k, v in scores.items()
                          if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0),
                         key=lambda kv: (-kv[1], kv[0]))
    return bool(verdict.get("is_valid", True)), flagged


def input_allowed(text):
    """True if the request prompt is allowed; False if the input scanner flags it."""
    return input_verdict(text)[0]


def describe_flagged(flagged):
    """'PromptInjection, risk 1.00; Toxicity, risk 0.42' for a refusal message."""
    return "; ".join(f"{name}, risk {risk:.2f}" for name, risk in flagged)


# The phrase rule, ported from phrase_verdict in Unleash_an_Agent_Watch_It_Burn verify/topic_cases.py: a
# term matches as whole words, case-insensitive, with an optional plural "s". "song" matches "songs", "hot
# dog" matches "Hot Dogs", and "rice" does not match "price". Off is checked before on, so "salsa dance
# moves" refuses even though "salsa" is on the menu.
def _topic_matches(text, terms):
    alts = sorted((re.escape(t.strip()) for t in terms if t.strip()), key=len, reverse=True)
    # An empty list matches nothing. Joined as-is it would be an empty alternation, which matches everywhere.
    return bool(alts) and re.search(r"\b(?:" + "|".join(alts) + r")s?\b", text, re.IGNORECASE) is not None


def phrase_verdict(text, off_terms, on_terms):
    """'off' when an off phrase is present, else 'on' when an on phrase is, else None (the classifier decides)."""
    if _topic_matches(text, off_terms):
        return "off"
    if _topic_matches(text, on_terms):
        return "on"
    return None


def topic_refused(text, deadline=None):
    """The step that refuses an off-topic prompt, "off_phrase" or "classifier", or None to let it through.

    A classifier error follows PROXY_FAIL_CLOSED, as the injection classifier does, unless a /chat turn's
    deadline cut it off, which raises TurnExpired."""
    phrase = phrase_verdict(text, TOPIC_OFF_LIST, TOPIC_ON_LIST)
    if phrase == "off":
        return "off_phrase"
    if phrase == "on":
        return None
    try:
        # Only BanTopics speaks here; the injection classifier is its own stage with its own toggle.
        verdict = _post_guard("/analyze/prompt",
                              {"prompt": text, "scanners_suppress": ["PromptInjection"]}, deadline)
        allowed = bool(verdict.get("is_valid", True))
    except Exception:
        _raise_if_expired(deadline)
        allowed = not FAIL_CLOSED
    return None if allowed else "classifier"


def output_scrub(text, deadline=None):
    """Return redacted text, or None to signal a hard block. Raises TurnExpired when a /chat turn's
    deadline cut it off, so fail-open never hands back a reply the scrub had no time to read."""
    try:
        verdict = _post_guard("/analyze/output", {"prompt": "", "output": text}, deadline)
        if verdict.get("is_valid", True):
            return text
        return verdict.get("sanitized_output") or "[REDACTED]"
    except Exception:
        _raise_if_expired(deadline)
        return None if FAIL_CLOSED else text


# The always-on redaction (Unleash_an_Agent_Watch_It_Burn#1092). ALWAYS_REDACT is one regular expression, and
# every non-empty match of it in every agent-authored reply text is replaced with [REDACTED] whatever the
# guard toggles say, on /chat and on the A2A pass-through alike. It is for data the workshop never shows in
# the chat in any state (the marketing intel of Challenge 2's send, #1002), so it cannot wait for the output
# guard: the public bot starts with every guard off, and that is when the send is played. The toggleable
# output guard (llm-guard's scanners: the recipe, the CEO address, the MCP sentinel) is unchanged and runs
# after it. It runs here, in-process, with no LLM Guard call, so it adds no upstream dependency, no latency
# and no fail-closed path to a wide-open turn. A redaction by it alone leaves `guarded` false and stamps no
# witb.guard.decision, because both say a toggleable guard acted (the leaderboard reads output_redacted as
# "the output guard is on"); it logs event always_redact instead. Unset or blank: nothing is redacted. A
# pattern that does not compile stops startup, rather than leaving the data unredacted.
_ALWAYS_REDACT_SRC = os.environ.get("ALWAYS_REDACT", "")
ALWAYS_REDACT = re.compile(_ALWAYS_REDACT_SRC) if _ALWAYS_REDACT_SRC.strip() else None
REDACTED = "[REDACTED]"


def always_redact(text):
    """The text with every non-empty ALWAYS_REDACT match replaced by [REDACTED]. An empty match is left
    alone, so a pattern that can match nothing never sprinkles the marker between letters."""
    if ALWAYS_REDACT is None or not text:
        return text
    return ALWAYS_REDACT.sub(lambda m: REDACTED if m.group(0) else "", text)


def extract_text(parts):
    return " ".join(
        p.get("text", "") for p in (parts or []) if isinstance(p, dict) and p.get("kind") == "text"
    )


# Monotonic id for /chat A2A messageIds (kagent requires a messageId; avoids a time-format dependency).
_chat_seq = 0

# Per-session contextId generation. kagent threads the conversation by A2A contextId; if a turn leaves a
# tool_use without its tool_result (a kagent/ADK multi-turn-with-tools quirk seen live), Bedrock then
# rejects EVERY later message in that contextId with a ValidationException. We self-heal by bumping the
# session's generation (a fresh contextId) on that error, abandoning the poisoned history and retrying.
_ctx_gen = {}


def _effective_ctx(session):
    """The A2A contextId to use for a browser session, including its self-heal generation suffix."""
    g = _ctx_gen.get(session, 0)
    return session if g == 0 else f"{session}-g{g}"


def _is_dangling_tooluse(reply):
    """True when a reply is the Bedrock 'tool_use without tool_result' error (poisoned session history)."""
    return bool(reply) and "tool_use" in reply and "tool_result" in reply and "ValidationException" in reply


def _is_tooluse_model_error(reply):
    """True when a reply is Bedrock's 424 for a model that broke the tool-call grammar (#62)."""
    return bool(reply) and "ModelErrorException" in reply and "invalid sequence as part of ToolUse" in reply


# Nova sometimes emits ONLY its <thinking> block and no answer. Measured on a live cluster 2026-09-03:
# 1 of 6 identical requests came back reasoning-only, so roughly one student prompt in six would render
# as an empty bubble. It is not a token ceiling (an empty reply used 148 output tokens and a good one
# 151), it is the model occasionally stopping after the scratchpad.
#
# The tag is matched loosely because Nova varies it: <Thinking>, </thinking >, and internal padding all
# occur, and a strict pattern would leave the tag in place and count it as an answer. Nova also sometimes
# never closes the block, so an unclosed tag runs to the end of the reply (guard-proxy-agenticburn#10),
# the same reading the storefront's splitThinking gives it. Group 1 is the scratchpad's contents.
_THINK_TAG_RE = re.compile(r"<\s*thinking\s*>(.*?)(?:<\s*/\s*thinking\s*>|$)", re.S | re.I)


def _is_thinking_only(reply):
    """True when stripping the scratchpad leaves nothing a student could read."""
    if not reply:
        return False
    return len(_THINK_TAG_RE.sub("", reply).strip()) < 2


# The storefront's witch name is `<First> the <Creature>`, each one capitalized ASCII word of at most 16
# letters (the desk's longest are 10 and 8). The field is client-supplied and no guard scans it, so a name
# of any other shape is no name: a direct POST cannot carry an injection past the guards in it (#35).
_WITCH_NAME_RE = re.compile(r"[A-Z][a-z]{1,15} the [A-Z][a-z]{1,15}")


def witch_context(witch):
    """The sentence /chat adds to the agent's message for the visitor's witch name."""
    return f"The person chatting with you gave their name as {witch}."


# The workshop Agent's recipe rule declines to open the recipe card later in a chat whose guest asked what
# files or folders it can see, but Nova Pro keeps that decline only when the current message carries this
# note (Unleash_an_Agent_Watch_It_Burn#968). Byte-equal to KITCHEN_NOTE in that repo's
# verify/test_recipe_persona.py, which the rule is keyed on (#63).
KITCHEN_NOTE = "[kitchen note: earlier in this chat the guest asked what folders you can see]"

# A guest asking what files or folders the bot can see or has access to: "what" or "which", up to three
# words, then files, folders or directories, then a verb of seeing within the same sentence. ASCII-only, as
# Go's RE2 reads it, so both builds mark the same messages.
_RECON_RE = re.compile(
    r"\b(?:what|which)\s+(?:[a-z]+\s+){0,3}?(?:files?|folders?|director(?:y|ies))\b"
    r"[^.?!]{0,60}?\b(?:see|access|view|open|read|list)\b",
    re.I | re.A,
)

# The browser sessions whose guest has asked the recon question. Keyed by session, not contextId, so the
# self-heal generation bump keeps the mark and a Reset (a new session id) clears it.
_recon_sessions = set()


def kitchen_noted(session, prompt):
    """Whether this /chat turn carries the kitchen note: its session asked the recon question in an earlier
    turn. A turn asking it marks the session for the turns after it, not for itself."""
    noted = session in _recon_sessions
    if session and _RECON_RE.search(prompt):
        _recon_sessions.add(session)
    return noted


# The recipe card's pantry (Unleash_an_Agent_Watch_It_Burn#1000). workshop-mcp withholds the card on any
# tools/call carrying this header, which kagent copies from the /chat forward onto its MCP calls. Byte-equal,
# case aside, to PANTRY_HEADER in that repo's gitops/ai-layer/workshop-mcp-server.py and to the allowedHeaders
# entry in gitops/workshop-agent/agent.yaml (#65).
PANTRY_HEADER = "X-Pantry-Shut"
# A guest message that names where the card is, and a reply that names it to a guest who had not. Case is
# folded for ASCII letters only, as the Go build folds it, so both builds mark the same messages.
_GUEST_NAMES_RE = re.compile(r"burrito-data|config/legacy|secret-sauce-recipe", re.I | re.A)
_REPLY_NAMES_RE = re.compile(r"burrito-data|secret-sauce-recipe", re.I | re.A)

# Each browser session's [guest_named, shown]. Keyed by session, not contextId, so the self-heal generation
# bump keeps them and a Reset (a new session id) starts clean.
_pantry = {}
_pantry_lock = threading.Lock()


def pantry_shut(session, prompt):
    """The X-Pantry-Shut value for this /chat turn, "" for none: `shown` once a reply named the card's folder
    to a guest who had not, else `unnamed` when this message names none of them. Marks the session named when
    this message names one, so a reply after it marks nothing."""
    named = bool(_GUEST_NAMES_RE.search(prompt))
    shown = False
    if session:
        with _pantry_lock:
            flags = _pantry.setdefault(session, [False, False])
            flags[0] = flags[0] or named
            shown = flags[1]
    if shown:
        return "shown"
    return "" if named else "unnamed"


def pantry_read(session, reply):
    """Marks the session shown when the reply the guest reads, thinking block included, names the card's
    folder or file before any of the guest's messages did."""
    if not session or not _REPLY_NAMES_RE.search(reply):
        return
    with _pantry_lock:
        flags = _pantry.setdefault(session, [False, False])
        if not flags[0]:
            flags[1] = True


def ask_user_questions(resp):
    """The questions of the ask_user call an A2A task stopped on, one per line, else "".

    kagent 0.10.1 gives every agent its built-in ask_user tool, with no setting to turn it off, and Nova
    sometimes puts its whole answer in the question (Unleash_an_Agent_Watch_It_Burn#889). The task then
    stops at input-required with no artifact: the question is only in status.message, in the
    adk_request_confirmation data part's args.originalFunctionCall, and the agent history holds the
    scratchpad alone (guard-proxy-agenticburn#72)."""
    result = resp.get("result") if isinstance(resp, dict) else None
    status = result.get("status") if isinstance(result, dict) else None
    if not isinstance(status, dict) or status.get("state") != "input-required":
        return ""
    message = status.get("message")
    questions = []
    for part in (message.get("parts") if isinstance(message, dict) else None) or []:
        data = part.get("data") if isinstance(part, dict) and part.get("kind") == "data" else None
        if not isinstance(data, dict) or data.get("name") != "adk_request_confirmation":
            continue
        args = data.get("args")
        call = args.get("originalFunctionCall") if isinstance(args, dict) else None
        if not isinstance(call, dict) or call.get("name") != "ask_user":
            continue
        call_args = call.get("args")
        for q in (call_args.get("questions") if isinstance(call_args, dict) else None) or []:
            text = q.get("question") if isinstance(q, dict) else None
            if isinstance(text, str) and text.strip():
                questions.append(text.strip())
    return "\n".join(questions)


def answered_steps(result):
    """Each model step's text in an A2A task, in order, a blank line apart, when a step before the last wrote
    something outside its scratchpad, else "".

    kagent's artifact holds only the final model step, and the task history holds one agent message per step.
    Nova sometimes writes its answer and then calls another tool, so the final step only talks about that tool
    and the artifact alone loses the answer (Unleash_an_Agent_Watch_It_Burn#889, guard-proxy-agenticburn#74)."""
    steps = []
    for h in result.get("history") or []:
        if isinstance(h, dict) and h.get("role") == "agent":
            text = extract_text(h.get("parts")).strip()
            if text:
                steps.append(text)
    if not any(not _is_thinking_only(s) for s in steps[:-1]):
        return ""
    return "\n\n".join(steps)


def chat_reply_text(resp):
    """The AGENT's reply text from an A2A response. kagent echoes the SAME text into artifacts AND the
    agent-role history, so take ONE source (artifacts first, then agent history, then status) rather than
    concatenating all three, or the storefront reply comes back doubled. A task stopped on an ask_user
    call answers with its questions, which carry what the model wrote for the guest, and a task that
    answered in an earlier model step answers with every step's text."""
    result = resp.get("result") if isinstance(resp, dict) else None
    if not isinstance(result, dict):
        return ""
    asked = ask_user_questions(resp)
    if asked:
        return asked
    steps = answered_steps(result)
    if steps:
        return steps
    art =" ".join(extract_text(a.get("parts")) for a in (result.get("artifacts") or [])).strip()
    if art:
        return art
    hist = " ".join(extract_text(h.get("parts")) for h in (result.get("history") or [])
                    if h.get("role") == "agent").strip()
    if hist:
        return hist
    return extract_text(result.get("status", {}).get("message", {}).get("parts")).strip()


def chat_usage_tokens(resp):
    """(input_tokens, output_tokens) for one A2A response from kagent_usage_metadata (adk fallback)."""
    result = resp.get("result", {}) if isinstance(resp, dict) else {}

    def _u(m):
        m = m or {}
        return m.get("kagent_usage_metadata") or m.get("adk_usage_metadata")

    u = _u(result.get("metadata")) or _u(result.get("status", {}).get("message", {}).get("metadata")) or {}
    return int(u.get("promptTokenCount", 0) or 0), int(u.get("candidatesTokenCount", 0) or 0)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body_obj, error=None):
        # Record the response code on the in-flight HTTP SERVER span (set in do_POST), if any. _send is
        # the single response path for POST, so this captures the status at every exit (403/429/502/200).
        # A 5xx answer makes the span an error, as OTel's HTTP conventions have a server span that
        # answered 5xx (guard-proxy-agenticburn#54); error, when the exit knows what failed, is the
        # status description.
        _sp = getattr(self, "_server_span", None)
        if _sp is not None:
            _sp.set_attribute("http.response.status_code", code)
            if code >= 500:
                _sp.set_status(Status(StatusCode.ERROR, error))
        body = json.dumps(body_obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        # Allow the instructor index (start.agenticburn.com) to read /cost and /prompts cross-subdomain
        # for the one-place live room view. These are read-only, already-moderated surfaces.
        #
        # Echoed per-request rather than "*", so an arbitrary page cannot read this cluster's spend or
        # its moderated prompt feed. Vary: Origin keeps a cache from serving one origin's answer to
        # another. do_OPTIONS is deliberately still absent: a cross-origin preflight therefore fails,
        # which is a second layer under the Origin check in _origin_allowed.
        _origin = self.headers.get("Origin")
        if _origin and _origin_allowed(self):
            self.send_header("Access-Control-Allow-Origin", _origin)
            self.send_header("Vary", "Origin")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stamp(self, key, value):
        # An attribute on the HTTP SERVER span (set in do_POST), if any. The /chat root carries the
        # browser session and which guard decided the turn: a blocked turn ends before any agent span
        # exists, so the root is the only span a dashboard can scope a session or name a guard from.
        _sp = getattr(self, "_server_span", None)
        if _sp is not None:
            _sp.set_attribute(key, value)

    def _refuse_for_conduct(self, masked):
        """Answer a /chat prompt the code-of-conduct check refused. The turn carries the gen_ai attributes and
        the chat span a turn the agent answers does, so Agent Observability lists it beside the others, but the
        prompt it records is masked (#81): the trace shown on stage never carries the term. The output is the
        refusal the guest read."""
        if _OTEL_AVAILABLE:
            model = request_model(bound_tier())
            root = trace.get_current_span()
            with _tracer.start_as_current_span("chat", kind=SpanKind.INTERNAL) as chat_span:
                for span in (root, chat_span):
                    if span.is_recording():
                        span.set_attribute("gen_ai.operation.name", "chat")
                        span.set_attribute("gen_ai.request.model", model)
                        if _CAPTURE_CONTENT:
                            span.set_attribute("gen_ai.input.messages", _genai_messages(masked))
                            span.set_attribute("gen_ai.output.messages", _genai_output(CONDUCT_REFUSAL))
        self._send(200, {"reply": CONDUCT_REFUSAL, "guarded": False, "input_tokens": 0, "output_tokens": 0})

    def do_GET(self):
        if self.path == "/cost":
            # Expose the ACTIVE ceiling alongside the spend so the UI can say "$x of $y" instead of a bare
            # number. A cap nobody can see reads as "unlimited" right up until it stops working, which is
            # the worst moment to learn about it (Michael, 2026-08-30). Two different ceilings can apply:
            # the always-on infra safety cap (COST_CAP_USD) and, when the C4 budget guard is toggled on,
            # the demo cap (BUDGET_CAP_USD). Report whichever would actually bite first. The infra cap is
            # the runtime one, so a cap lowered with /toggle?cost_cap= is the target the counter shows.
            with _cost_lock:
                payload = dict(_cost)
            payload["tier"] = bound_tier()
            caps = [c for c in (_cost_cap["usd"],
                                BUDGET_CAP_USD if GUARDS.get("budget") else 0) if c and c > 0]
            payload["cap_usd"] = min(caps) if caps else 0
            payload["budget_guard"] = bool(GUARDS.get("budget"))
            self._send(200, payload)
            return
        if self.path == "/prompts":
            # Side-screen feed: MODERATED prompts only, and only if capture is enabled.
            self._send(200, feed_payload())
            return
        # NOTE: the old custom Prometheus /metrics cost endpoint is REMOVED. Cost is now the
        # OTLP metric `gen_ai.client.cost` (registered above, under the standard gen_ai namespace), and
        # tokens are the standard `gen_ai.client.token.usage` from the kagent ADK agent. Both flow via the
        # OTel Collector, so there is nothing to scrape here.
        if self.path.startswith("/guards"):
            with _guard_lock:
                self._send(200, dict(GUARDS))
            return
        if self.path.startswith("/controls"):
            # Badge state for the whole control set (#197/#209): the AI guards this proxy owns, PLUS the
            # cluster-object controls (C1 NetworkPolicy, C2 Kyverno enforce, C3 KubeArmor + Falco, C7
            # gateway rule or tool allow-list) read live from the K8s API. One endpoint so the
            # strip renders every badge from a single poll.
            with _guard_lock:
                ai = dict(GUARDS)
            # "host" tells the page which cluster answered. The community cluster is shared by the
            # whole room, and a student who does not know that reads someone else's prompt as their
            # own bot misbehaving (#292). Carried on the poll the badges already make.
            # "always_on" carries what no toggle turns off, apart from "ai", so the strip never draws the
            # code-of-conduct check (#78) as a stage guard. It is false only when PROFANITY_LIST is set empty.
            self._send(200, {"ai": ai, "infra": read_controls(),
                             "always_on": {"code_of_conduct": _PROFANITY_RE is not None},
                             "host": os.environ.get("WIB_PUBLIC_HOST", "")})
            return
        if self.path.startswith("/toggle"):
            # Runtime flip, no restart, no spec change. Keys: input_blocklist, input_classifier, output,
            # budget, topic. Convenience: input=on flips BOTH input stages. e.g. /toggle?input_blocklist=on
            # cost_cap=N sets the cluster-wide cost cap and cost_cap=default restores COST_CAP_USD. A refused
            # cost_cap answers 400 and changes nothing, guards included. It is read with blank values kept,
            # so an empty cost_cap= is refused instead of silently ignored.
            q = parse_qs(urlparse(self.path).query)
            cap_q = parse_qs(urlparse(self.path).query, keep_blank_values=True).get("cost_cap")
            cap = parse_cost_cap(cap_q[0]) if cap_q else None
            if cap_q and cap is None:
                self._send(400, {"error": cost_cap_refusal()})
                return
            with _guard_lock:
                was = {**GUARDS, "cost_cap": _cost_cap["usd"]}
                if "input" in q:
                    on = q["input"][0].lower() == "on"
                    GUARDS["input_blocklist"] = on
                    GUARDS["input_classifier"] = on
                for k in ("input_blocklist", "input_classifier", "output", "budget", "topic"):
                    if k in q:
                        GUARDS[k] = q[k][0].lower() == "on"
                if cap_q:
                    _cost_cap["usd"] = cap
                self._log_toggles(was, {**GUARDS, "cost_cap": _cost_cap["usd"]})
                self._send(200, {**GUARDS, "cost_cap_usd": _cost_cap["usd"]})
            return
        # Pass through agent-card / .well-known discovery unchanged. A GET has no server span
        # (guard-proxy-agenticburn#57), so a failed one is an ERROR log line, the forward_error /chat logs.
        try:
            req = urllib.request.Request(AGENT_URL + self.path, method="GET")
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                body = r.read()
                self.send_response(r.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        except Exception as exc:
            log.error("agent GET failed: %s", exc, extra={"event": "forward_error"})
            self._send(502, {"error": f"agent GET failed: {exc}"})

    def _log_toggles(self, was, now):
        """One guard_toggle line per guard a /toggle changed, so a guard found in the wrong state can be traced to
        the request that put it there (Unleash_an_Agent_Watch_It_Burn#1024). The peer is the TCP client, which is
        127.0.0.1 for a kubectl exec into this pod; the caller is the X-WIB-Caller header a script may send to name
        itself, cut to TOGGLE_CALLER_MAX characters, and "" when absent. A /toggle that changes nothing logs nothing."""
        peer = self.client_address[0] if self.client_address else ""
        caller = (self.headers.get("X-WIB-Caller") or "")[:TOGGLE_CALLER_MAX]
        by = f"{peer} ({caller})" if caller else peer
        for guard in TOGGLE_KEYS:
            if now[guard] != was[guard]:
                log.info("guard %s set to %s by %s", guard, _toggle_value_text(now[guard]), by,
                         extra={"event": "guard_toggle", "toggle": {
                             "guard": guard, "value": now[guard], "was": was[guard], "peer": peer, "caller": caller}})

    def do_POST(self):
        # HTTP SERVER span (PRD #22 M1), the trace root: it starts the trace unless the caller sent a W3C
        # context, which it then joins. agentgateway sits downstream of this proxy, not upstream:
        # agent.forward's client span is the parent of agentgateway's span for the hop.
        # http.response.status_code is set by _send at every exit. No-op when the OTel SDK is absent
        # (import guard).
        self._server_span = None
        server_cm = (
            _tracer.start_as_current_span(
                f"POST {self.path}", context=extract(dict(self.headers)), kind=SpanKind.SERVER)
            if _OTEL_AVAILABLE else nullcontext()
        )
        with server_cm as server_span:
            self._server_span = server_span
            if server_span is not None:
                server_span.set_attribute("http.request.method", "POST")
                server_span.set_attribute("url.path", self.path)
            # ORIGIN ENFORCEMENT REMOVED. It compared the browser's Origin against this request's Host
            # header, and behind the apex router Host is the LOAD BALANCER name, not the public hostname:
            # the proxy sees k8s-agent-console-<hash>.elb.us-west-2.amazonaws.com on every cluster. So a
            # real browser sending Origin: https://michael-student.agenticburn.com never matched and got
            # 403 "cross-origin request rejected", which BurritoBot renders as "the kitchen isn't
            # answering right now (403)". It broke chat on EVERY cluster and did so in front of a room.
            #
            # curl probes passed because curl sends no Origin header, and _origin_allowed returns True
            # when Origin is absent. A check that only fires for browsers cannot be validated without a
            # browser, and it was not.
            #
            # This is the same Host-rewriting trap that already forced the /brief gate out of nginx and
            # into the router earlier the same day. Any origin enforcement has to live at the apex router,
            # which is the only hop that knows the public hostname; #151 is closed as accepted risk in any
            # case, so nothing here needs to enforce it.
            # /chat is the BurritoBot storefront contract (B1); everything else is the A2A passthrough.
            if self.path.rstrip("/") == "/chat":
                self._handle_chat()
            else:
                self._handle_post()

    def _handle_post(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        try:
            payload = json.loads(raw)
        except Exception:
            payload = None

        text = ""
        if isinstance(payload, dict):
            text = extract_text(payload.get("params", {}).get("message", {}).get("parts", []))
        record_prompt(text)  # moderated; side-screen feed only. The A2A path carries no witch name.

        # The `sanitize` INTERNAL child span wraps the guard decision + the forward (PRD #22 M1).
        # gen_ai.operation.name="chat" so Datadog classifies it as an llm-kind span and renders the
        # before/after message panel in LLM Observability. NOTE: this proxy forwards the prompt UNCHANGED
        # (it blocks-or-passes; it does not rewrite prompt content), so input.messages (original) and
        # output.messages (forwarded) are equal here; the re-leak redaction is the Collector OTTL Act-2
        # step (PRD #22 M2), not in-proxy. Content capture is gated (default OFF; "true" never enables it).
        san_cm = (
            _tracer.start_as_current_span("sanitize", kind=SpanKind.INTERNAL)
            if _OTEL_AVAILABLE else nullcontext()
        )
        with san_cm as san_span:
            if san_span is not None:
                san_span.set_attribute("gen_ai.operation.name", "chat")
                # The model the agent is bound to now (guard-proxy-agenticburn#38), as the reply is priced.
                san_span.set_attribute("gen_ai.request.model", request_model(bound_tier()))
                # Datadog LLM Observability reads INPUT/OUTPUT from gen_ai.input.messages /
                # gen_ai.output.messages span ATTRIBUTES (JSON-encoded messages array with parts schema).
                # The older gen_ai.content.prompt/completion event names are not recognised under
                # gen_ai_latest_experimental semconv (the research spikes (removed 2026-08-30; see `git log --diff-filter=D -- research/`) Q5).
                if _CAPTURE_CONTENT:
                    san_span.set_attribute("gen_ai.input.messages", _genai_messages(text))

            # Stage 1: deterministic block-list (cheapest, pre-LLM, zero tokens). Toggled independently.
            if GUARDS["input_blocklist"] and text:
                hit = blocklisted(text)
                if hit:
                    log.info("input blocked by deterministic block-list (matched %r)", hit,
                             extra={"event": "input_blocklist_hit"})
                    self._send(403, {
                        "jsonrpc": "2.0", "id": payload.get("id"),
                        "error": {"code": -32600,
                                  "message": f"Request blocked by input block-list (matched '{hit}'). "
                                             "No model tokens were spent."},
                    })
                    return
            # Stage 2: model-based prompt-injection classifier (costlier gate; NOT deterministic).
            if GUARDS["input_classifier"] and text:
                allowed, flagged = input_verdict(text)
                if not allowed:
                    log.info("input blocked by prompt-injection classifier",
                             extra={"event": "input_classifier_block",
                                    "scanners": [name for name, _ in flagged]})
                    # Name the scanners, as the block-list names its term; without scores (or with no
                    # verdict, failing closed) the message stays the generic one.
                    reason = (f"flagged by {describe_flagged(flagged)}" if flagged
                              else "prompt injection detected")
                    self._send(403, {
                        "jsonrpc": "2.0", "id": payload.get("id"),
                        "error": {"code": -32600,
                                  "message": f"Request blocked by input guardrail ({reason})."},
                    })
                    return
            # Stage 3: the on-topic guard, after both input stages and before the caps, so a refusal
            # spends no tokens and takes no rate-limit slot.
            if GUARDS["topic"] and text:
                step = topic_refused(text)
                if step:
                    log.info("input refused as off-topic (step %s)", step,
                             extra={"event": "topic_guard_block", "step": step})
                    self._send(403, {
                        "jsonrpc": "2.0", "id": payload.get("id"),
                        "error": {"code": -32600, "message": TOPIC_REFUSAL},
                    })
                    return

            # Protect the demo from itself: rate-limit + cost-cap BEFORE spending any Bedrock tokens.
            if rate_limited():
                self._send(429, {
                    "jsonrpc": "2.0", "id": payload.get("id") if isinstance(payload, dict) else None,
                    "error": {"code": -32000,
                              "message": f"Rate limit reached ({RATE_LIMIT_RPM}/min on this cluster). "
                                         "Slow down; the cost demo will not run away."},
                })
                return
            capped = cost_capped()
            if capped:
                if capped == "cost_cap":
                    message = (f"Cost cap reached (${_cost_cap['usd']:.2f} on this cluster). Spend is frozen "
                               "for everyone; a blocked request costs nothing.")
                else:
                    message = (f"Budget cap reached (${BUDGET_CAP_USD:.2f} on this cluster). Spend is "
                               "frozen; a blocked request costs nothing.")
                self._send(429, {
                    "jsonrpc": "2.0", "id": payload.get("id") if isinstance(payload, dict) else None,
                    "error": {"code": -32000, "message": message},
                })
                return

            # Forward to the agent inside a CLIENT span (PRD #27 M2): this models the egress hop in the
            # Datadog Service Map. peer.service names the downstream node so the map draws
            # guard-proxy -> <next hop>; http.request.method / url.full / http.response.status_code give
            # it http.client semantics. inject() runs INSIDE the span so the forwarded W3C context is the
            # CLIENT span's, and kagent's ADK spans parent onto THIS hop (not the sanitize span).
            fwd_url = AGENT_URL + self.path
            client_cm = (
                _tracer.start_as_current_span("agent.forward", kind=SpanKind.CLIENT)
                if _OTEL_AVAILABLE else nullcontext()
            )
            with client_cm as client_span:
                fwd_headers = {"Content-Type": "application/json"}
                if client_span is not None:
                    client_span.set_attribute("peer.service", _PEER_SERVICE)
                    client_span.set_attribute("http.request.method", "POST")
                    client_span.set_attribute("url.full", fwd_url)
                    inject(fwd_headers)
                try:
                    req = urllib.request.Request(
                        fwd_url, data=raw, headers=fwd_headers, method="POST")
                    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                        if client_span is not None:
                            client_span.set_attribute("http.response.status_code", r.status)
                        resp = json.loads(r.read())
                except Exception as exc:
                    if client_span is not None:
                        # HTTPError carries .code; connection-level failures do not.
                        code = getattr(exc, "code", None)
                        if code is not None:
                            client_span.set_attribute("http.response.status_code", code)
                        client_span.set_attribute("error.type", type(exc).__name__)
                        client_span.set_status(Status(StatusCode.ERROR, str(exc)))
                    if san_span is not None:
                        # The workshop collector makes sanitize the turn's root in Agent Observability and
                        # drops agent.forward (guard-proxy-agenticburn#61), so sanitize fails with it.
                        san_span.set_attribute("error.type", type(exc).__name__)
                        san_span.set_status(Status(StatusCode.ERROR, str(exc)))
                    log.error("agent forward failed: %s", exc, extra={"event": "forward_error"})
                    self._send(502, {"error": f"agent forward failed: {exc}"}, error=str(exc))
                    return
            # Output attribute: the agent's RAW (pre-scrub) reply, so Datadog LLM Observability shows
            # the OUTPUT panel for this gen_ai span. Set while the sanitize span is still open.
            if san_span is not None and _CAPTURE_CONTENT:
                _reply_txt = chat_reply_text(resp)
                if _reply_txt:
                    san_span.set_attribute("gen_ai.output.messages", _genai_output(_reply_txt))

        record_usage(resp)  # tally Bedrock token spend for the live cost counter
        # The always-on redaction runs whatever the toggles say (#1092), before the output guard.
        resp = self._redact_response(resp)
        if GUARDS["output"]:
            resp = self._scrub_response(resp)
        self._send(200, resp)

    @staticmethod
    def _agent_text_parts(resp, every_data_string=False):
        """Every agent-authored text of an A2A reply, as (container, key) pairs to read and write: the
        artifacts, the agent's turns in the history, and the status message, each text part and each copy of
        an ask_user question kagent carries in a data part. With every_data_string, each non-empty string
        anywhere in a data part's data instead, whichever tool wrote it (guard-proxy-agenticburn#75). It runs
        on every reply, guards off included, so a field of an unexpected shape is skipped, never raised on."""

        def field(obj, key):
            return obj.get(key) if isinstance(obj, dict) else None

        def items(v):
            return v if isinstance(v, list) else []

        def string(obj, key):
            if isinstance(field(obj, key), str) and obj[key]:
                yield obj, key

        def questions(call_args):
            for q in items(field(call_args, "questions")):
                yield from string(q, "question")

        def asked(data):
            # kagent 0.10.1 writes an ask_user question four times, all in data parts: the call and its
            # pending response in the history, and the status message's adk_request_confirmation, in its
            # originalFunctionCall and as its toolConfirmation hint (guard-proxy-agenticburn#73).
            if field(data, "name") == "ask_user":
                yield from questions(field(data, "args"))
                yield from questions(field(data, "response"))
            elif field(data, "name") == "adk_request_confirmation":
                args = field(data, "args")
                call = field(args, "originalFunctionCall")
                if field(call, "name") == "ask_user":
                    yield from questions(field(call, "args"))
                    yield from string(field(args, "toolConfirmation"), "hint")

        def strings(obj, key):
            # Every non-empty string in obj[key], at any depth and inside lists; keys, numbers and nulls stay.
            v = obj[key]
            if isinstance(v, str):
                if v:
                    yield obj, key
            elif isinstance(v, dict):
                for k in list(v):
                    yield from strings(v, k)
            elif isinstance(v, list):
                for i in range(len(v)):
                    yield from strings(v, i)

        def texts(parts):
            for p in items(parts):
                if field(p, "kind") == "text":
                    yield from string(p, "text")
                elif field(p, "kind") == "data" and every_data_string:
                    if "data" in p:
                        yield from strings(p, "data")
                elif field(p, "kind") == "data":
                    yield from asked(p.get("data"))

        result = field(resp, "result")
        if not isinstance(result, dict):
            return
        for artifact in items(result.get("artifacts")):
            yield from texts(field(artifact, "parts"))
        for entry in items(result.get("history")):
            if field(entry, "role") == "agent":
                yield from texts(field(entry, "parts"))
        yield from texts(field(field(result.get("status"), "message"), "parts"))

    def _redact_response(self, resp):
        # Every string in a tool's call or response too: the redaction is in-process and rewrites only what
        # the pattern matches, so it calls no LLM Guard and has no fail-closed block to break an id with (#75).
        for obj, key in self._agent_text_parts(resp, every_data_string=True):
            redacted = always_redact(obj[key])
            if redacted != obj[key]:
                log.info("always-on redaction modified response content", extra={"event": "always_redact"})
                obj[key] = redacted
        return resp

    def _scrub_response(self, resp):
        for obj, key in self._agent_text_parts(resp):
            scrubbed = output_scrub(obj[key])
            if scrubbed != obj[key]:
                log.info("output guardrail modified response content",
                         extra={"event": "output_scrub"})
            obj[key] = "[BLOCKED BY OUTPUT GUARDRAIL]" if scrubbed is None else scrubbed
        return resp

    def _handle_chat(self):
        """One storefront turn under one deadline: TURN_TIMEOUT, read once here, bounds every upstream
        call the turn makes. A guard stage the deadline cuts off ends the turn here with the forward's 502;
        a forward it cuts off answers that 502 itself, in _forward."""
        deadline = time.monotonic() + TURN_TIMEOUT if TURN_TIMEOUT > 0 else None
        try:
            self._chat_turn(deadline)
        except TurnExpired as exc:
            log.error("chat turn ran out of time: %s", exc, extra={"event": "turn_timeout"})
            self._send(502, {"reply": "BurritoBot's kitchen isn't answering. Try again in a moment.",
                             "guarded": False, "input_tokens": 0, "output_tokens": 0}, error=str(exc))

    def _chat_turn(self, deadline):
        """BurritoBot storefront contract (B1): POST {prompt, session, witch} -> {reply, guarded,
        input_tokens, output_tokens}. Wraps the prompt as an A2A message/send and runs the SAME input/output guards,
        rate/cost cap, cost metering, and agent forward as the A2A root, then reshapes the response. The
        guard TOGGLES apply identically, so the guardrail toggles affect BurritoBot too."""
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        try:
            body = json.loads(raw) or {}
            prompt = body.get("prompt", "")
            session = body.get("session", "")  # per-browser-session id -> A2A contextId for conversation memory
            witch = body.get("witch", "")  # the visitor's display name, assigned by the storefront (#34)
        except Exception:
            prompt = ""
            session = ""
            witch = ""
        # A non-string session (an object, array, number) is no session: an unhashable one would
        # otherwise crash the per-session cost lookup and drop the connection.
        if not isinstance(session, str):
            session = ""
        # The name is context for the agent, not part of the prompt: no guard scans it, the feed carries it
        # beside the prompt's text and never in it, and a blank or non-string one is no name, so the message
        # is exactly the prompt.
        witch = witch.strip() if isinstance(witch, str) else ""
        if witch and not _WITCH_NAME_RE.fullmatch(witch):
            log.info("chat witch name dropped: %d characters, not shaped <First> the <Creature>", len(witch),
                     extra={"event": "witch_name_refused"})
            witch = ""
        if session:
            self._stamp("session.id", session)  # OTel semconv; joins one browser session's traces
        if not isinstance(prompt, str) or not without_probe(prompt).strip():
            self._send(400, {"reply": "Tell BurritoBot what you'd like.", "guarded": False,
                             "input_tokens": 0, "output_tokens": 0})
            return
        record_prompt(prompt, witch)
        # The feed has read the marker; every guard, the telemetry and the agent see the guest's text.
        prompt = without_probe(prompt)
        # The code-of-conduct check, whatever the toggles say (#78): the prompt reaches nothing past here, and
        # the refusal names no term. The feed above already holds it masked, and the wall drops a masked one.
        # Then the Bedrock guardrail, when one is configured, on a prompt the terms pass (#81).
        if conduct_matches(prompt):
            log.info("chat input refused by the code-of-conduct check", extra={"event": "conduct_refused"})
            self._refuse_for_conduct(conduct_mask(prompt))
            return
        if conduct_guardrail_blocks(prompt, deadline):
            log.info("chat input refused by the code-of-conduct guardrail", extra={"event": "conduct_refused"})
            # The guardrail names no span, so the whole prompt is the span.
            self._refuse_for_conduct("[redacted]")
            return
        # Read before any guard: a message a guard blocks still shows the guest knew the card's folder.
        pantry = pantry_shut(session, prompt)
        # Input guards and the topic guard (same toggles as the A2A path), then rate/cost cap. A blocked
        # request spends 0 tokens.
        if GUARDS["input_blocklist"]:
            hit = blocklisted(prompt)
            if hit:
                log.info("chat input blocked by block-list (matched %r)", hit,
                         extra={"event": "input_blocklist_hit"})
                self._stamp("witb.guard.decision", "input_blocklist")
                self._send(200, {"reply": f"BurritoBot can't help with that (blocked: '{hit}'). "
                                          "No model tokens were spent.",
                                 "guarded": True, "input_tokens": 0, "output_tokens": 0})
                return
        allowed, flagged = input_verdict(prompt, deadline) if GUARDS["input_classifier"] else (True, [])
        if not allowed:
            log.info("chat input blocked by classifier",
                     extra={"event": "input_classifier_block", "scanners": [name for name, _ in flagged]})
            self._stamp("witb.guard.decision", "input_classifier")
            reason = f": {describe_flagged(flagged)}" if flagged else ""
            self._send(200, {"reply": f"BurritoBot can't help with that (blocked by the input guardrail{reason}).",
                             "guarded": True, "input_tokens": 0, "output_tokens": 0})
            return
        if GUARDS["topic"]:
            step = topic_refused(prompt, deadline)
            if step:
                log.info("chat input refused as off-topic (step %s)", step,
                         extra={"event": "topic_guard_block", "step": step})
                self._stamp("witb.guard.decision", "topic")
                self._send(200, {"reply": TOPIC_REFUSAL, "guarded": True,
                                 "input_tokens": 0, "output_tokens": 0})
                return
        if session_rate_limited(session):
            self._stamp("witb.guard.decision", "rate_limit")
            self._send(200, {"reply": f"Slow down, hungry traveler. ({RATE_LIMIT_SESSION_RPM}/min per conversation.) "
                                      "Wait a few seconds and send that again.",
                             "guarded": True, "input_tokens": 0, "output_tokens": 0})
            return
        if rate_limited():
            self._stamp("witb.guard.decision", "rate_limit")
            self._send(200, {"reply": f"Slow down, hungry traveler. ({RATE_LIMIT_RPM}/min cap on this cluster.) "
                                      "Wait a few seconds and send that again.",
                             "guarded": True, "input_tokens": 0, "output_tokens": 0})
            return
        capped = cost_capped(session)
        if capped == "cost_cap":
            # The cluster-wide backstop holds every conversation, so Reset does not help and the reply
            # must not offer it: in the KubeCon finale the whole room reads this at once.
            self._stamp("witb.guard.decision", "cost_cap")
            self._send(200, {"reply": f"The kitchen is closed: this cluster hit its ${_cost_cap['usd']:.2f} "
                                      f"spend cap, so BurritoBot isn't sending anything else to the model "
                                      f"for anyone. A blocked request costs nothing.",
                             "guarded": True, "input_tokens": 0, "output_tokens": 0})
            return
        if capped:
            # The way out is Reset, not switching the control off. A fresh conversation gets a fresh
            # budget and the guardrail stays armed, which is what a per-tenant quota does in production.
            self._stamp("witb.guard.decision", "budget")
            self._send(200, {"reply": f"The kitchen tab is frozen. This conversation hit its ${BUDGET_CAP_USD:.2f} "
                                      f"spend budget, so I'm not sending anything else to the model. A "
                                      f"blocked request costs nothing. Press Reset for a fresh "
                                      f"conversation; the budget guard stays on.",
                             "guarded": True, "input_tokens": 0, "output_tokens": 0})
            return
        # Marked only here, past every guard and cap, so a recon question the agent never saw sets no mark.
        kitchen_note = kitchen_noted(session, prompt)
        # Forward as A2A message/send to the agent root, inside a CLIENT span (the Service Map egress hop).
        global _chat_seq

        def _forward(ctx, optional=False):
            """Send the prompt to the agent under contextId ctx, as a non-blocking message/send polled to
            the end of its task. Returns the A2A resp, or None on a transport error or when PROXY_TIMEOUT
            or the turn's deadline has run out. A 502 has then already been sent, unless optional is set:
            the caller still holds a usable reply and answers with it, so exactly one response reaches the
            client. Either way a task the agent is still running is cancelled."""
            global _chat_seq
            _chat_seq += 1
            m = {"role": "user", "messageId": f"chat-{_chat_seq}",
                 "parts": [{"kind": "text", "text": prompt}]}
            if witch:
                # A second text part: kagent hands every text part to the model, so the agent knows who it
                # is talking to (HC-666 names its Deployment joker-<slug> from this), and parts[0] stays
                # the prompt as typed.
                m["parts"].append({"kind": "text", "text": witch_context(witch)})
            if kitchen_note:
                # Last, after the witch part, and like it outside every guard and the feed: the recipe rule
                # reads it in the current message, where Nova Pro keeps the same-chat decline.
                m["parts"].append({"kind": "text", "text": KITCHEN_NOTE})
            if ctx:
                # Carry the (self-heal-generationed) browser session as the A2A contextId so kagent
                # threads the order across turns.
                m["contextId"] = ctx
            a2a = {"jsonrpc": "2.0", "id": "chat", "method": "message/send",
                   "params": {"message": m, "configuration": {"blocking": False}}}
            fwd_headers = {"Content-Type": "application/json"}
            if witch:
                # The send and every poll carry the name as a header too: kagent copies it onto the agent's
                # MCP calls, so workshop-mcp credits a deploy to the visitor from the request rather than from
                # the model (Unleash_an_Agent_Watch_It_Burn#995). tasks/cancel and /a2a carry none.
                fwd_headers["X-Witch-Name"] = witch
            if pantry:
                # The send and every poll carry it: kagent copies it onto the agent's MCP calls, so
                # workshop-mcp keeps the recipe card shut in a chat that may not have it (#65). tasks/cancel
                # and /a2a carry none.
                fwd_headers[PANTRY_HEADER] = pantry
            # PROXY_TIMEOUT bounds the whole forward, the send and every poll, as it bounded the one
            # blocking call before; the turn's deadline cuts it shorter.
            fwd_deadline = time.monotonic() + TIMEOUT
            if deadline is not None:
                fwd_deadline = min(fwd_deadline, deadline)

            # The tasks/get polls' headers: the same, but under an unsampled traceparent once the span opens.
            poll_headers = dict(fwd_headers)

            def _call(body, poll=False):
                left = fwd_deadline - time.monotonic()
                if left <= 0:
                    _raise_if_expired(deadline)
                    raise TimeoutError(f"the {TIMEOUT:g}s forward deadline (PROXY_TIMEOUT) passed")
                req = urllib.request.Request(AGENT_URL + "/", data=json.dumps(body).encode(),
                                             headers=poll_headers if poll else fwd_headers, method="POST")
                # A poll records no urllib span, and urllib's instrumentation leaves its traceparent alone.
                token = (otel_context.attach(otel_context.set_value(_SUPPRESS_HTTP_INSTRUMENTATION_KEY, True))
                         if poll and _OTEL_AVAILABLE else None)
                try:
                    with urllib.request.urlopen(req, timeout=left) as r:
                        _record_status(r.status)
                        return json.loads(r.read())
                except urllib.error.HTTPError as exc:
                    _record_status(exc.code)
                    raise
                finally:
                    if token is not None:
                        otel_context.detach(token)

            def _record_status(code):
                # The span covers the send and every poll, so each answer overwrites the last: a forward
                # that succeeds records its final 2xx, and one a non-2xx ended records that status.
                if client_span is not None:
                    client_span.set_attribute("http.response.status_code", code)

            client_cm = (_tracer.start_as_current_span("agent.forward", kind=SpanKind.CLIENT)
                         if _OTEL_AVAILABLE else nullcontext())
            with client_cm as client_span:
                if client_span is not None:
                    client_span.set_attribute("peer.service", _PEER_SERVICE)
                    client_span.set_attribute("http.request.method", "POST")
                    client_span.set_attribute("url.full", AGENT_URL + "/")
                    inject(fwd_headers)
                    # A tool can run for many seconds, polled once a second, and every poll would leave a span
                    # at each hop under the forward (guard-proxy-agenticburn#77). A poll carries this span's
                    # context with the sampled flag cleared, which agentgateway and kagent do not record
                    # under; the send keeps the sampled context the agent's own spans hang from.
                    sent = client_span.get_span_context()
                    inject(poll_headers, context=trace.set_span_in_context(trace.NonRecordingSpan(
                        trace.SpanContext(sent.trace_id, sent.span_id, is_remote=False,
                                          trace_flags=trace.TraceFlags(trace.TraceFlags.DEFAULT),
                                          trace_state=sent.trace_state))))
                task_id = None
                try:
                    # A TurnExpired from _call lands in the except below, as a timeout does.
                    resp = _call(a2a)
                    task_id = _running_task_id(resp)
                    wait = _POLL_FIRST
                    while task_id:
                        time.sleep(max(0.0, min(wait, fwd_deadline - time.monotonic())))
                        wait = min(wait * 2, _POLL_MAX)
                        resp = _call({"jsonrpc": "2.0", "id": "chat", "method": "tasks/get",
                                      "params": {"id": task_id}}, poll=True)
                        if not isinstance(resp, dict) or not isinstance(resp.get("result"), dict):
                            raise ValueError(f"tasks/get answered no task: {json.dumps(resp)[:200]}")
                        if _running_task_id(resp) is None:
                            task_id = None
                    return resp
                except Exception as exc:
                    if client_span is not None:
                        # As on the A2A forward; _record_status has already set any HTTP status.
                        client_span.set_attribute("error.type", type(exc).__name__)
                        client_span.set_status(Status(StatusCode.ERROR, str(exc)))
                    log.error("chat forward failed: %s", exc, extra={"event": "forward_error"})
                    if not optional:
                        # The turn fails with the forward, so the chat span is an error too, and _send's
                        # 502 makes the root one: Datadog's error rate and Agent Observability read the
                        # root (guard-proxy-agenticburn#53). A failed optional retry answers 200 and
                        # leaves them Unset.
                        if chat_span is not None:
                            chat_span.set_status(Status(StatusCode.ERROR, str(exc)))
                        self._send(502, {"reply": "BurritoBot's kitchen isn't answering. Try again in a moment.",
                                         "guarded": False, "input_tokens": 0, "output_tokens": 0},
                                   error=str(exc))
                    if task_id:
                        threading.Thread(target=_cancel_task, args=(task_id, f"the chat forward failed: {exc}"),
                                         daemon=True).start()
                    return None

        # Annotate the outer HTTP span, this proxy's own SERVER span opened in do_POST, with gen_ai
        # attributes so Datadog LLM Obs trace list preview shows content. That span is the trace root:
        # the proxy is a stdlib http.server handler, which OTel auto-instrumentation does not touch, so
        # there is no other root above it (measured, Unleash_an_Agent_Watch_It_Burn#382). The chat child
        # span below carries the same attributes for span-level drill-down.
        # gen_ai.request.model is the model the agent is bound to as the turn reaches it
        # (guard-proxy-agenticburn#38); each attempt is still priced and gauged at its own.
        _turn_model = request_model(bound_tier()) if _OTEL_AVAILABLE else None
        if _OTEL_AVAILABLE:
            _root_span = trace.get_current_span()
            if _root_span.is_recording():
                _root_span.set_attribute("gen_ai.operation.name", "chat")
                _root_span.set_attribute("gen_ai.request.model", _turn_model)
                if _CAPTURE_CONTENT:
                    _root_span.set_attribute("gen_ai.input.messages", _genai_messages(prompt))

        # Wrap the storefront turn in a gen_ai "chat" span so Datadog LLM Observability sees it as an LLM
        # call. INPUT/OUTPUT panels are populated from gen_ai.input.messages / gen_ai.output.messages span
        # ATTRIBUTES (JSON-encoded messages array). agent.forward (in _forward) parents onto this span.
        chat_cm = (_tracer.start_as_current_span("chat", kind=SpanKind.INTERNAL)
                   if _OTEL_AVAILABLE else nullcontext())
        with chat_cm as chat_span:
            if chat_span is not None:
                chat_span.set_attribute("gen_ai.operation.name", "chat")
                chat_span.set_attribute("gen_ai.request.model", _turn_model)
                if _CAPTURE_CONTENT:
                    chat_span.set_attribute("gen_ai.input.messages", _genai_messages(prompt))
            # Every model call is billed, so each attempt is metered as it returns, not only the reply
            # the turn ends on (guard-proxy-agenticburn#3). spent sums the turn for the storefront, each
            # attempt at the price it was metered at.
            spent = [0, 0, 0.0]

            def _attempt(optional=False):
                r = _forward(_effective_ctx(session), optional)
                if r is not None:
                    # feed the live cost counter (same path as A2A)
                    spent[2] += record_usage(r, session)
                    tin, tout = chat_usage_tokens(r)
                    spent[0] += tin
                    spent[1] += tout
                    # A task stopped on an ask_user call waits for an answer the chat page has no way to
                    # send, so the turn replies with its question and the task is cancelled rather than
                    # left pending (guard-proxy-agenticburn#72). The guest's next message runs as a new task.
                    if ask_user_questions(r):
                        log.info("the agent asked the guest through ask_user; replying with its question",
                                 extra={"event": "ask_user_reply"})
                        asked_id = r["result"].get("id")
                        if isinstance(asked_id, str) and asked_id:
                            threading.Thread(target=_cancel_task, daemon=True, args=(
                                asked_id, "it waits on an ask_user answer the chat page cannot send")).start()
                        else:
                            log.error("the agent's ask_user task has no id to cancel",
                                      extra={"event": "task_cancel_failed"})
                return r

            resp = _attempt()
            if resp is None:
                return
            # Self-heal: if kagent left a dangling tool_use in this session's history, Bedrock rejects every
            # later turn. Rotate to a fresh contextId (drop the poisoned history) and retry the prompt once.
            if session and _is_dangling_tooluse(chat_reply_text(resp)):
                _ctx_gen[session] = _ctx_gen.get(session, 0) + 1
                log.info("chat session poisoned by dangling tool_use; rotated contextId to gen %d",
                         _ctx_gen[session], extra={"event": "ctx_rotate"})
                retry = _attempt()
                if retry is None:
                    return
                resp = retry
            # Bedrock answers 424 ModelErrorException when Nova's output breaks the tool-call grammar, and
            # kagent hands that error back as the reply. The same ask sent again usually gets an answer, so
            # re-send it once in the same conversation, as a presenter would (guard-proxy-agenticburn#62).
            # The first reply is a raw Bedrock error, so a failed retry answers 502; a second 424 is returned.
            if _is_tooluse_model_error(chat_reply_text(resp)):
                log.info("model broke the tool-call grammar (Bedrock 424); retrying once",
                         extra={"event": "model_error_retry"})
                retry = _attempt()
                if retry is None:
                    return
                resp = retry
            # Same shape of self-heal for a reasoning-only reply: ask once more rather than render an
            # empty bubble at a student. One retry, never a loop, so a model that is reliably terse costs
            # at most double rather than spinning. The cost is bounded and small next to a workshop where
            # one prompt in six visibly does nothing.
            _thinking_only = _is_thinking_only(chat_reply_text(resp))
            if _thinking_only:
                log.info("model returned reasoning with no answer; retrying once",
                         extra={"event": "thinking_only_retry"})
                # The first reply is still usable (the last resort below surfaces its scratchpad), so a
                # failed retry falls back to it instead of answering 502 (guard-proxy-agenticburn#1).
                retry = _attempt(optional=True)
                if retry is not None and not _is_thinking_only(chat_reply_text(retry)):
                    resp = retry
                    _thinking_only = False
            pin, pout, cost_usd = spent
            # The turn's tokens, every forward's summed, go on this span only: the collector turns the root
            # and sanitize into Agent Observability's invoke_agent span, where they would be a second count
            # next to kagent's own model spans (guard-proxy-agenticburn#49).
            if chat_span is not None:
                chat_span.set_attribute("gen_ai.usage.input_tokens", pin)
                chat_span.set_attribute("gen_ai.usage.output_tokens", pout)
            reply = chat_reply_text(resp)
            # LAST RESORT, after the retry above already failed. Measured after adding the retry: the
            # empty-reply rate fell from 1-in-6 to 1-in-10, because sometimes BOTH attempts stop after the
            # scratchpad. A retry alone is therefore not a guarantee, and "..." in front of a room is the
            # one outcome worth spending a line of code to prevent.
            #
            # Unwrap the scratchpad and hand back its contents. It is the only thing the model produced,
            # it is usually a serviceable answer in the first person ("I need to list the proteins"), and
            # showing it is more honest than an empty bubble that implies the agent ignored them.
            if _thinking_only and reply:
                _inner = " ".join(t.strip() for t in _THINK_TAG_RE.findall(reply))
                if _inner.strip():
                    reply = _inner.strip()
                    log.info("model returned reasoning twice; surfacing it rather than an empty reply",
                             extra={"event": "thinking_only_surfaced"})
                else:
                    # An empty scratchpad leaves nothing to surface. Drop the raw tags so the student
                    # sees "..." rather than markup.
                    reply = ""
            # The chat span records what the model said, with the code-of-conduct terms masked, since they
            # never show up anywhere; the root, below, what left the proxy (guard-proxy-agenticburn#82).
            if chat_span is not None and _CAPTURE_CONTENT and reply:
                chat_span.set_attribute("gen_ai.output.messages", _genai_output(conduct_mask(reply)))
            # The always-on redaction first, whatever the toggles say (#1092); it never sets guarded.
            redacted = always_redact(reply)
            if redacted != reply:
                reply = redacted
                log.info("always-on redaction modified response content", extra={"event": "always_redact"})
            # Then the code-of-conduct mask, output guard or not (#78); it never sets guarded either.
            masked = conduct_mask(reply)
            if masked != reply:
                reply = masked
                log.info("code-of-conduct check masked response content", extra={"event": "conduct_masked"})
            guarded = False
            if GUARDS["output"] and reply:
                scrubbed = output_scrub(reply, deadline)
                if scrubbed is None:
                    reply, guarded = "[blocked by the output guardrail]", True
                elif scrubbed != reply:
                    reply, guarded = scrubbed, True
            if guarded:
                log.info("output guardrail modified response content", extra={"event": "output_scrub"})
                self._stamp("witb.guard.decision", "output_redacted")
            # The reply as the guest reads it, so a scrubbed or blocked name marks nothing.
            pantry_read(session, reply)
            # Per-call cost (cost_usd, summed above) so the storefront can show a live dollar counter
            # without hardcoding rates client-side.
            self._send(200, {"reply": reply or "...", "guarded": guarded,
                             "input_tokens": pin, "output_tokens": pout, "cost_usd": cost_usd})
        # Set output on the outer HTTP span after reply is known (reply is in scope after the with block).
        if _OTEL_AVAILABLE and _CAPTURE_CONTENT and reply:
            _root_span = trace.get_current_span()
            if _root_span.is_recording():
                _root_span.set_attribute("gen_ai.output.messages", _genai_output(reply))

    def log_message(self, fmt, *args):
        # Route BaseHTTPRequestHandler's default access logging through the structured JSON logger
        # (PRD #27 M2) instead of its plain-text stderr line. Debug level keeps it out of the INFO
        # stream that carries the guard-decision events.
        log.debug(fmt % args, extra={"event": "http_access"})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
