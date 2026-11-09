# ABOUTME: Conformance for the telemetry beyond the /chat root's attributes: the span tree on both paths,
# ABOUTME: W3C propagation in and out, opt-in message content, the cost gauge, and trace ids on log lines.
from __future__ import annotations

import http.client
import json
import time
from urllib.parse import urlparse

import pytest

from conftest import AGENT_CR, BINDING_TTL, bound_agent
from fakes import Reply, a2a_result, unreachable_url, verdict

pytestmark = pytest.mark.spans

MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
NOVA = "us.amazon.nova-pro-v1:0"
OPUS = "us.anthropic.claude-opus-4-8"
UPSTREAM_TRACE = "0af7651916cd43dd8448eb211c80319c"
UPSTREAM_SPAN = "b7ad6b7169203331"
CAPTURE = {"OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "SPAN_ONLY"}
DANGLING = (
    "ValidationException: messages.3: `tool_use` ids were found without `tool_result` "
    "blocks immediately after"
)
MODEL_ERROR = (
    "LLM error: API_ERROR operation error Bedrock Runtime: Converse, https response error StatusCode: 424, "
    "RequestID: a1, ModelErrorException: Model produced invalid sequence as part of ToolUse."
)


def _by_name(collector, names: set[str]) -> dict[str, dict]:
    """Wait until one span of each name has arrived, and return them by name."""

    def found(spans):
        got = {s["name"]: s for s in spans if s["name"] in names}
        return got if set(got) == names else None

    return collector.wait_for(found, f"spans {sorted(names)}")


def _two_chats(collector) -> list[dict]:
    """Wait until both turns' chat spans have arrived, and return them in arrival order."""

    def found(spans):
        chats = [s for s in spans if s["name"] == "chat"]
        return chats if len(chats) == 2 else None

    return collector.wait_for(found, "two chat spans")


def _header(rec, name: str) -> str | None:
    return next((v for k, v in rec.headers.items() if k.lower() == name), None)


def _post_with_traceparent(p, path: str, body: dict) -> int:
    """POST with a lowercase traceparent, as agentgateway sends it; urllib would capitalize the name."""
    u = urlparse(p.url)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=30)
    conn.putrequest("POST", path)
    data = json.dumps(body).encode()
    conn.putheader("content-type", "application/json")
    conn.putheader("content-length", str(len(data)))
    conn.putheader("traceparent", f"00-{UPSTREAM_TRACE}-{UPSTREAM_SPAN}-01")
    conn.endheaders(data)
    status = conn.getresponse().status
    conn.close()
    return status


def _envelope(role: str, text: str) -> str:
    if role == "user":
        return json.dumps([{"role": "user", "parts": [{"type": "text", "content": text}]}])
    return json.dumps(
        [{"role": "assistant", "parts": [{"type": "text", "content": text}], "finish_reason": "stop"}]
    )


# --- POST /chat -----------------------------------------------------------------------------------


def test_chat_span_tree(traced, agent):
    p, collector = traced()
    assert p.chat("one burrito").status == 200
    spans = _by_name(collector, {"POST /chat", "chat", "agent.forward"})
    root, chat, fwd = spans["POST /chat"], spans["chat"], spans["agent.forward"]
    assert (root["kind"], chat["kind"], fwd["kind"]) == ("SERVER", "INTERNAL", "CLIENT")
    assert chat["parent_span_id"] == root["span_id"]
    assert fwd["parent_span_id"] == chat["span_id"]
    assert {root["trace_id"], chat["trace_id"], fwd["trace_id"]} == {root["trace_id"]}
    assert root["attributes"] == {
        "http.request.method": "POST",
        "url.path": "/chat",
        "http.response.status_code": 200,
        "gen_ai.operation.name": "chat",
        "gen_ai.request.model": MODEL,
    }
    # The token counts are on the chat span alone: the collector turns the root into Agent Observability's
    # invoke_agent span, where they would count the turn a second time next to kagent's own model spans.
    assert chat["attributes"] == {
        "gen_ai.operation.name": "chat",
        "gen_ai.request.model": MODEL,
        "gen_ai.usage.input_tokens": 100,
        "gen_ai.usage.output_tokens": 50,
    }
    # url.full and http.response.status_code are required by the workshop registry's egress contract
    # (span.witb.guard_proxy.egress).
    assert fwd["attributes"] == {
        "peer.service": "burritobot-gateway",
        "http.request.method": "POST",
        "url.full": agent.url + "/",
        "http.response.status_code": 200,
    }
    assert root["resource"]["service.name"] == "guard-proxy"


@pytest.mark.parametrize(
    "first,second",
    [
        (DANGLING, "Fresh start."),  # the context rotation
        (MODEL_ERROR, "Done."),  # the 424 tool-call grammar retry
        ("<thinking>hmm</thinking>", "Done."),  # the reasoning-only retry
    ],
    ids=["context_rotation", "model_error_retry", "thinking_only_retry"],
)
def test_chat_span_counts_every_forward_of_the_turn(traced, agent, first, second):
    # A self-heal retry is a second billed model call, so the chat span sums the tokens of both forwards,
    # the same totals the storefront is sent.
    queue = [
        Reply(body=a2a_result(first, prompt_tokens=1000, output_tokens=200)),
        Reply(body=a2a_result(second, prompt_tokens=3000, output_tokens=400)),
    ]
    agent.respond = lambda req: queue.pop(0)
    p, collector = traced()
    turn = p.chat("hi", session="s1").json()
    assert len(agent.calls()) == 2
    assert (turn["input_tokens"], turn["output_tokens"]) == (4000, 600)
    spans = _by_name(collector, {"POST /chat", "chat"})
    chat = spans["chat"]["attributes"]
    assert (chat["gen_ai.usage.input_tokens"], chat["gen_ai.usage.output_tokens"]) == (4000, 600)
    def two_forwards(got):
        found = [s for s in got if s["name"] == "agent.forward"]
        return found if len(found) == 2 else None

    forwards = collector.wait_for(two_forwards, "two agent.forward spans")
    for other in [spans["POST /chat"], *forwards]:
        assert not {"gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens"} & set(other["attributes"])


def test_chat_span_counts_zero_when_the_agent_reports_no_usage(traced, agent):
    agent.respond = lambda req: Reply(body=a2a_result(prompt_tokens=None, output_tokens=None))
    p, collector = traced()
    assert p.chat("one burrito").status == 200
    chat = _by_name(collector, {"chat"})["chat"]["attributes"]
    assert (chat["gen_ai.usage.input_tokens"], chat["gen_ai.usage.output_tokens"]) == (0, 0)


def _polled(final: Reply):
    """An agent that accepts the turn as a running task and answers the first tasks/get with final."""

    def respond(req):
        if req.json().get("method") == "message/send":
            return Reply(body={"jsonrpc": "2.0", "id": "chat", "result": {
                "kind": "task", "id": "task-7", "contextId": "ctx-1", "status": {"state": "submitted"}}})
        return final

    return respond


def test_chat_forward_records_the_last_response_status(traced, agent):
    # One span covers the send and every poll; it records the status of the last response, the one the
    # reply came in. A 202 tells it apart from the send's 200.
    done = a2a_result("Here is your burrito.")
    done["result"]["id"] = "task-7"
    agent.respond = _polled(Reply(status=202, body=done))
    p, collector = traced()
    assert p.chat("one burrito").status == 200
    fwd = _by_name(collector, {"agent.forward"})["agent.forward"]
    assert [c.json()["method"] for c in agent.calls()] == ["message/send", "tasks/get"]
    assert fwd["attributes"]["http.response.status_code"] == 202
    assert fwd["status"] == "UNSET"


def test_chat_forward_records_the_status_that_ended_it(traced, agent):
    # A non-2xx answer ends the forward, so the last response is also the first non-2xx one.
    agent.respond = _polled(Reply(status=500, body={}))
    p, collector = traced()
    assert p.chat("one burrito").status == 502
    fwd = _by_name(collector, {"agent.forward"})["agent.forward"]
    assert fwd["attributes"]["http.response.status_code"] == 500


def test_chat_forward_failure_marks_the_client_span(traced, agent):
    # A failed /chat forward is a failed span, as on the A2A forward: ERROR status and an error.type.
    agent.respond = lambda req: Reply(status=500, body={})
    p, collector = traced()
    assert p.chat("one burrito").status == 502
    fwd = _by_name(collector, {"agent.forward"})["agent.forward"]
    assert fwd["status"] == "ERROR"
    assert fwd["attributes"]["http.response.status_code"] == 500
    assert fwd["attributes"]["error.type"]


def test_chat_forward_transport_failure_marks_the_client_span(traced):
    # A refused connection has no status code, but the span still fails and names the error.
    p, collector = traced(AGENT_URL=unreachable_url())
    assert p.chat("one burrito").status == 502
    fwd = _by_name(collector, {"agent.forward"})["agent.forward"]
    assert fwd["status"] == "ERROR"
    assert "http.response.status_code" not in fwd["attributes"]
    assert fwd["attributes"]["error.type"]


def _never_done(req):
    """An agent that accepts the turn as a running task and never finishes it."""
    state = "submitted" if req.json().get("method") == "message/send" else "working"
    return Reply(body={"jsonrpc": "2.0", "id": "chat", "result": {
        "kind": "task", "id": "task-7", "contextId": "ctx-1", "status": {"state": state}}})


@pytest.mark.parametrize("failure", ["http_500", "unreachable", "proxy_timeout"])
def test_a_failed_chat_forward_marks_the_root_and_chat_spans(traced, agent, failure):
    # guard-proxy-agenticburn#53: a turn answered 502 because its forward failed is a failed turn on the
    # root, the span Datadog's error rate and Agent Observability key on, and on the chat span, not only
    # on agent.forward. The HTTP conventions make a server span that answered 5xx an error.
    env = {}
    if failure == "http_500":
        agent.respond = lambda req: Reply(status=500, body={})
    elif failure == "unreachable":
        env["AGENT_URL"] = unreachable_url()
    else:
        agent.respond = _never_done
        env["PROXY_TIMEOUT"] = "1"
    p, collector = traced(**env)
    assert p.chat("one burrito").status == 502
    spans = _by_name(collector, {"POST /chat", "chat", "agent.forward"})
    assert spans["agent.forward"]["status"] == "ERROR"
    assert spans["POST /chat"]["attributes"]["http.response.status_code"] == 502
    assert spans["POST /chat"]["status"] == "ERROR"
    assert spans["chat"]["status"] == "ERROR"
    # The root names what failed, as the chat span does, not only the 502 (guard-proxy-agenticburn#54).
    assert spans["POST /chat"]["status_message"]
    assert spans["POST /chat"]["status_message"] == spans["chat"]["status_message"]


def test_a_chat_turn_whose_guard_stage_ran_out_of_time_marks_the_root(traced, agent, guard):
    # guard-proxy-agenticburn#54: a classifier the turn deadline cut off ends the turn with a 502 before any
    # chat span or forward exists. The root is then the only span that can say the turn failed.
    guard.respond = lambda req: Reply(body=verdict(True), delay=4)
    p, collector = traced(INPUT_CLASSIFIER="on", TURN_TIMEOUT="1", PROXY_TIMEOUT="5")
    assert p.chat("hi").status == 502
    root = _by_name(collector, {"POST /chat"})["POST /chat"]
    assert root["attributes"]["http.response.status_code"] == 502
    assert root["status"] == "ERROR"
    assert "TURN_TIMEOUT" in root["status_message"]
    assert not agent.calls()


def test_a_guarded_chat_turn_leaves_the_root_unset(traced, agent):
    # Only a 5xx answer is a failed turn: a refusal the input guard answered is the proxy doing its job.
    p, collector = traced(INPUT_BLOCKLIST="on")
    r = p.chat("nuke it")
    assert r.status == 200 and r.json()["guarded"]
    root = _by_name(collector, {"POST /chat"})["POST /chat"]
    assert root["status"] == "UNSET"


def test_a_failed_self_heal_retry_marks_the_root_and_chat_spans(traced, agent):
    # The dangling tool_use retry is not optional: its failure answers 502, as the first forward's does.
    calls = {"n": 0}

    def respond(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return Reply(body=a2a_result(DANGLING))
        return Reply(status=500, body={})

    agent.respond = respond
    p, collector = traced()
    assert p.chat("hi", session="s1").status == 502
    spans = _by_name(collector, {"POST /chat", "chat"})
    assert spans["POST /chat"]["status"] == "ERROR"
    assert spans["chat"]["status"] == "ERROR"


def test_a_failed_optional_retry_leaves_the_root_and_chat_spans_unset(traced, agent):
    # The reasoning-only retry is optional: when it fails the turn still answers 200 with the first reply
    # (guard-proxy-agenticburn#1). The caller got an answer, so the root and the chat span stay Unset, as
    # the HTTP conventions leave a 2xx server span; the failed forward is its own agent.forward error.
    calls = {"n": 0}

    def respond(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return Reply(body=a2a_result("<thinking>menu first</thinking>"))
        return Reply(status=500, body={})

    agent.respond = respond
    p, collector = traced()
    assert p.chat("hi").status == 200
    spans = _by_name(collector, {"POST /chat", "chat"})

    def two_forwards(got):
        found = [s for s in got if s["name"] == "agent.forward"]
        return found if len(found) == 2 else None

    forwards = collector.wait_for(two_forwards, "two agent.forward spans")
    assert sorted(f["status"] for f in forwards) == ["ERROR", "UNSET"]
    assert spans["POST /chat"]["status"] == "UNSET"
    assert spans["chat"]["status"] == "UNSET"


def test_a_successful_chat_turn_leaves_the_root_and_chat_spans_unset(traced, agent):
    p, collector = traced()
    assert p.chat("one burrito").status == 200
    spans = _by_name(collector, {"POST /chat", "chat"})
    assert spans["POST /chat"]["status"] == "UNSET"
    assert spans["chat"]["status"] == "UNSET"


def test_chat_joins_the_upstream_trace_and_passes_it_on(traced, agent):
    p, collector = traced()
    assert _post_with_traceparent(p, "/chat", {"prompt": "one burrito"}) == 200
    spans = _by_name(collector, {"POST /chat", "agent.forward"})
    root, fwd = spans["POST /chat"], spans["agent.forward"]
    assert root["trace_id"] == UPSTREAM_TRACE
    assert root["parent_span_id"] == UPSTREAM_SPAN
    # The agent's spans parent onto the forward hop.
    assert _header(agent.calls()[0], "traceparent") == f"00-{UPSTREAM_TRACE}-{fwd['span_id']}-01"


def _polled_twice():
    """An agent that accepts the turn as a running task, still runs it at the first tasks/get, and has
    finished it at the second."""
    running = {"jsonrpc": "2.0", "id": "chat", "result": {
        "kind": "task", "id": "task-7", "contextId": "ctx-1", "status": {"state": "working"}}}
    done = a2a_result("Here is your burrito.")
    done["result"]["id"] = "task-7"
    answers = [running, running, done]

    def respond(req):
        return Reply(body=answers.pop(0))

    return respond


def test_chat_polls_go_out_unsampled_and_leave_no_spans(traced, agent):
    # While a tool runs the forward polls once a second, and each poll left a span at every hop of the
    # turn's trace (guard-proxy-agenticburn#77). A poll goes out under an unsampled traceparent, which
    # agentgateway and kagent do not record under, with urllib's own instrumentation suppressed; the
    # send keeps the turn's sampled context, so the agent's work still hangs from the forward.
    agent.respond = _polled_twice()
    p, collector = traced(CONFORMANCE_URLLIB_INSTRUMENTATION="on")
    assert _post_with_traceparent(p, "/chat", {"prompt": "one burrito"}) == 200
    spans = _by_name(collector, {"POST /chat", "chat", "agent.forward"})
    fwd = spans["agent.forward"]
    send, *polls = agent.calls()
    assert [c.json()["method"] for c in (send, *polls)] == ["message/send", "tasks/get", "tasks/get"]
    for poll in polls:
        assert _header(poll, "traceparent") == f"00-{UPSTREAM_TRACE}-{fwd['span_id']}-00"
    version, trace_id, send_parent, flags = _header(send, "traceparent").split("-")
    assert (version, trace_id, flags) == ("00", UPSTREAM_TRACE, "01")
    # Between agent.forward and the agent, only the send's own HTTP client span, where the implementation
    # instruments its client, and no span for either poll.
    to_agent = [
        s for s in collector.spans()
        if s["name"] != "agent.forward" and agent.url + "/" in s["attributes"].values()
    ]
    if send_parent == fwd["span_id"]:
        assert to_agent == []
    else:
        (sent,) = to_agent
        assert (sent["span_id"], sent["parent_span_id"]) == (send_parent, fwd["span_id"])


def test_chat_message_content_is_opt_in(traced, agent):
    p, collector = traced(**CAPTURE)
    p.chat("one burrito")
    spans = _by_name(collector, {"POST /chat", "chat"})
    for name in ("POST /chat", "chat"):
        attrs = spans[name]["attributes"]
        assert attrs["gen_ai.input.messages"] == _envelope("user", "one burrito")
        assert attrs["gen_ai.output.messages"] == _envelope("assistant", "Here is your burrito.")


@pytest.mark.parametrize("mode", ["NO_CONTENT", "true", "span_only_typo"])
def test_chat_message_content_stays_off_for_other_values(traced, agent, mode):
    p, collector = traced(OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=mode)
    p.chat("one burrito")
    spans = _by_name(collector, {"POST /chat", "chat"})
    for s in spans.values():
        assert "gen_ai.input.messages" not in s["attributes"]
        assert "gen_ai.output.messages" not in s["attributes"]


def test_chat_root_output_is_the_reply_the_client_got(traced, agent, guard):
    agent.respond = lambda req: Reply(body=a2a_result("the code is OPHELIA, you wanker"))
    guard.respond = lambda req: Reply(
        body={"is_valid": False, "sanitized_output": req.json()["output"].replace("OPHELIA", "[REDACTED]")}
    )
    p, collector = traced(OUTPUT_GUARD="on", **CAPTURE)
    assert p.chat("hi").json()["reply"] == "the code is [REDACTED], you [redacted]"
    spans = _by_name(collector, {"POST /chat", "chat"})
    # The chat span records what the model said, with the code-of-conduct terms masked, since they never show
    # up anywhere (guard-proxy-agenticburn#82); the root, what left the proxy.
    assert spans["chat"]["attributes"]["gen_ai.output.messages"] == _envelope(
        "assistant", "the code is OPHELIA, you [redacted]"
    )
    assert spans["POST /chat"]["attributes"]["gen_ai.output.messages"] == _envelope(
        "assistant", "the code is [REDACTED], you [redacted]"
    )


def test_an_answered_turn_s_spans_carry_no_term_with_every_guard_off(traced, agent):
    agent.respond = lambda req: Reply(body=a2a_result("Here is your burrito, you Dickhead. Fuck off."))
    p, collector = traced(**CAPTURE)
    assert p.chat("one burrito").json()["reply"] == "Here is your burrito, you [redacted]. [redacted] off."
    spans = _by_name(collector, {"POST /chat", "chat"})
    for name in ("POST /chat", "chat"):
        assert spans[name]["attributes"]["gen_ai.output.messages"] == _envelope(
            "assistant", "Here is your burrito, you [redacted]. [redacted] off."
        )
    assert all("Dickhead" not in json.dumps(s) and "Fuck" not in json.dumps(s) for s in collector.spans())


def test_the_chat_span_masks_terms_but_keeps_what_always_redact_removed(traced, agent):
    # ALWAYS_REDACT keeps its own decision (2026-11-07): the chat span shows what the model leaked, the root
    # what the proxy sent. Only the code-of-conduct terms are masked on both.
    agent.respond = lambda req: Reply(body=a2a_result("Operation Midnight Munchies, you wanker."))
    p, collector = traced(ALWAYS_REDACT="Operation Midnight Munchies", **CAPTURE)
    assert p.chat("hi").json()["reply"] == "[REDACTED], you [redacted]."
    spans = _by_name(collector, {"POST /chat", "chat"})
    assert spans["chat"]["attributes"]["gen_ai.output.messages"] == _envelope(
        "assistant", "Operation Midnight Munchies, you [redacted]."
    )
    assert spans["POST /chat"]["attributes"]["gen_ai.output.messages"] == _envelope(
        "assistant", "[REDACTED], you [redacted]."
    )


def test_a_blocked_chat_turn_has_no_chat_span(traced, agent):
    p, collector = traced(INPUT_BLOCKLIST="on")
    p.chat("nuke the kitchen")
    (root,) = collector.wait_for(
        lambda spans: [s for s in spans if s["name"] == "POST /chat"], "the POST /chat span"
    )
    assert root["attributes"]["http.response.status_code"] == 200
    assert "gen_ai.operation.name" not in root["attributes"]
    assert [s["name"] for s in collector.spans()] == ["POST /chat"]


def test_peer_service_is_configurable(traced, agent):
    p, collector = traced(PEER_SERVICE="agentgateway")
    p.chat("one burrito")
    fwd = _by_name(collector, {"agent.forward"})["agent.forward"]
    assert fwd["attributes"]["peer.service"] == "agentgateway"


# --- The A2A path ---------------------------------------------------------------------------------


def test_a2a_span_tree(traced, agent):
    p, collector = traced()
    assert p.a2a("two burritos").status == 200
    spans = _by_name(collector, {"POST /", "sanitize", "agent.forward"})
    root, san, fwd = spans["POST /"], spans["sanitize"], spans["agent.forward"]
    assert (root["kind"], san["kind"], fwd["kind"]) == ("SERVER", "INTERNAL", "CLIENT")
    assert san["parent_span_id"] == root["span_id"]
    assert fwd["parent_span_id"] == san["span_id"]
    assert root["attributes"] == {
        "http.request.method": "POST",
        "url.path": "/",
        "http.response.status_code": 200,
    }
    assert san["attributes"] == {"gen_ai.operation.name": "chat", "gen_ai.request.model": MODEL}
    assert fwd["attributes"] == {
        "peer.service": "burritobot-gateway",
        "http.request.method": "POST",
        "url.full": agent.url + "/",
        "http.response.status_code": 200,
    }
    assert fwd["status"] == "UNSET"


def test_a2a_joins_the_upstream_trace_and_passes_it_on(traced, agent):
    p, collector = traced()
    body = {
        "jsonrpc": "2.0",
        "id": "r1",
        "method": "message/send",
        "params": {"message": {"role": "user", "messageId": "m", "parts": [{"kind": "text", "text": "hi"}]}},
    }
    assert _post_with_traceparent(p, "/", body) == 200
    spans = _by_name(collector, {"POST /", "agent.forward"})
    assert spans["POST /"]["trace_id"] == UPSTREAM_TRACE
    assert spans["POST /"]["parent_span_id"] == UPSTREAM_SPAN
    fwd = spans["agent.forward"]
    assert _header(agent.calls()[0], "traceparent") == f"00-{UPSTREAM_TRACE}-{fwd['span_id']}-01"


def test_a2a_message_content_is_opt_in(traced, agent):
    p, collector = traced(**CAPTURE)
    p.a2a("two burritos")
    san = _by_name(collector, {"sanitize"})["sanitize"]
    assert san["attributes"]["gen_ai.input.messages"] == _envelope("user", "two burritos")
    assert san["attributes"]["gen_ai.output.messages"] == _envelope("assistant", "Here is your burrito.")


def test_a2a_forward_failure_marks_the_client_span(traced, agent):
    agent.respond = lambda req: Reply(status=500, body={})
    p, collector = traced()
    assert p.a2a("hi").status == 502
    spans = _by_name(collector, {"POST /", "agent.forward"})
    fwd = spans["agent.forward"]
    assert fwd["status"] == "ERROR"
    assert fwd["attributes"]["http.response.status_code"] == 500
    assert fwd["attributes"]["error.type"]
    assert spans["POST /"]["attributes"]["http.response.status_code"] == 502


@pytest.mark.parametrize("failure", ["http_500", "unreachable"])
def test_a2a_forward_failure_marks_the_root(traced, agent, failure):
    # guard-proxy-agenticburn#54: the passthrough answered 502, so its server span is an error under the HTTP
    # conventions, not only agent.forward, and it names what failed.
    env = {}
    if failure == "http_500":
        agent.respond = lambda req: Reply(status=500, body={})
    else:
        env["AGENT_URL"] = unreachable_url()
    p, collector = traced(**env)
    assert p.a2a("hi").status == 502
    root = _by_name(collector, {"POST /"})["POST /"]
    assert root["attributes"]["http.response.status_code"] == 502
    assert root["status"] == "ERROR"
    assert root["status_message"]


@pytest.mark.parametrize("failure", ["http_500", "unreachable"])
def test_a2a_forward_failure_marks_the_sanitize_span(traced, agent, failure):
    # guard-proxy-agenticburn#61: the workshop collector makes sanitize the turn's root in Agent Observability
    # and drops agent.forward, so sanitize itself must read as failed, naming the same error.type. It carries
    # no status code of its own: the agent's status stays on agent.forward.
    env = {}
    if failure == "http_500":
        agent.respond = lambda req: Reply(status=500, body={})
    else:
        env["AGENT_URL"] = unreachable_url()
    p, collector = traced(**env)
    assert p.a2a("hi").status == 502
    spans = _by_name(collector, {"sanitize", "agent.forward"})
    san, fwd = spans["sanitize"], spans["agent.forward"]
    assert san["status"] == "ERROR"
    assert san["status_message"]
    assert san["attributes"]["error.type"] == fwd["attributes"]["error.type"]
    assert "http.response.status_code" not in san["attributes"]


def test_a2a_answered_request_leaves_the_root_unset(traced, agent):
    p, collector = traced()
    assert p.a2a("two burritos").status == 200
    spans = _by_name(collector, {"POST /", "sanitize"})
    assert spans["POST /"]["status"] == "UNSET"
    assert spans["sanitize"]["status"] == "UNSET"


def test_a2a_blocked_request_has_no_forward_span(traced, agent):
    p, collector = traced(INPUT_BLOCKLIST="on")
    assert p.a2a("nuke it").status == 403
    spans = _by_name(collector, {"POST /", "sanitize"})
    assert spans["POST /"]["attributes"]["http.response.status_code"] == 403
    # A 4xx answer leaves a server span Unset under the HTTP conventions (guard-proxy-agenticburn#54).
    assert spans["POST /"]["status"] == "UNSET"
    assert "agent.forward" not in {s["name"] for s in collector.spans()}


# --- GET requests ---------------------------------------------------------------------------------


def test_get_requests_open_no_span(traced, agent):
    # guard-proxy-agenticburn#57: a GET stays unspanned, a failed one included. The console polls would be a
    # stream of roots, and the workshop collector drops the agent-card fetch by name anyway; a 5xx on a GET is
    # an ERROR log line instead (test_logging.py).
    agent.respond = lambda req: Reply(status=500, body={})
    p, collector = traced()
    for path in ("/cost", "/prompts", "/guards", "/controls", "/toggle?budget=on"):
        assert p.get(path).status == 200
    assert p.get("/.well-known/agent-card.json").status == 502
    # The POST after them is the marker: its span ends last, so any GET span would have arrived with or before it.
    agent.respond = lambda req: Reply(body=a2a_result("ok"))
    assert p.a2a("hi").status == 200
    _by_name(collector, {"POST /"})
    assert {s["name"] for s in collector.spans()} == {"POST /", "sanitize", "agent.forward"}


# --- The cost gauge and log correlation -----------------------------------------------------------


def test_cost_gauge_reports_the_cluster_spend(traced, agent):
    p, collector = traced()
    p.chat("one burrito")
    p.a2a("two burritos")
    usd = p.get("/cost").json()["usd"]

    def latest(_spans):
        points = collector.gauge("gen_ai.client.cost")
        return points if points and points[0][0] == pytest.approx(usd) else None

    (value, attrs), *_ = collector.wait_for(latest, "a gen_ai.client.cost reading of the spend")
    assert value == pytest.approx(usd)
    assert attrs == {"gen_ai.request.model": MODEL, "gen_ai.provider.name": "aws.bedrock"}


# --- The model the workshop-agent is bound to (guard-proxy-agenticburn#38) ------------------------
# gen_ai.request.model follows the model each request is priced at: the bound ModelConfig's tier, read as
# the pricing reads it, unless MODEL_NAME names a model. The cost gauge has one series per model, each
# that model's spend, so a live swap moves new spend onto the new model's series.


def _models(points) -> dict[str, float]:
    """The gauge's points as USD by gen_ai.request.model, checking each carries the provider."""
    assert all(a["gen_ai.provider.name"] == "aws.bedrock" for _, a in points)
    return {a["gen_ai.request.model"]: v for v, a in points}


def _gauge_matching(collector, usd_by_model: dict[str, float]):
    def latest(_spans):
        got = _models(collector.gauge("gen_ai.client.cost"))
        if set(got) == set(usd_by_model) and all(
            got[m] == pytest.approx(usd) for m, usd in usd_by_model.items()
        ):
            return got
        return None

    return collector.wait_for(latest, f"a gen_ai.client.cost reading of {usd_by_model}")


def test_spans_report_the_bound_model_not_model_tier(traced, agent, kube_api):
    kube_api.objects[AGENT_CR] = bound_agent("bedrock-opus")
    p, collector = traced(**kube_api.env, MODEL_TIER="nova")
    assert p.chat("one burrito").status == 200
    assert p.a2a("two burritos").status == 200
    spans = _by_name(collector, {"POST /chat", "chat", "sanitize"})
    for name in ("POST /chat", "chat", "sanitize"):
        assert spans[name]["attributes"]["gen_ai.request.model"] == OPUS, name


def test_a_live_swap_moves_the_spans_and_the_new_spend_to_the_new_model(traced, agent, kube_api):
    """The swap Unleash_an_Agent_Watch_It_Burn#130 teaches: Nova Pro to Opus on a running cluster."""
    kube_api.objects[AGENT_CR] = bound_agent("bedrock-nova")
    p, collector = traced(**kube_api.env, MODEL_TIER="nova")
    nova_usd = p.chat("one burrito").json()["cost_usd"]
    _gauge_matching(collector, {NOVA: nova_usd})
    kube_api.objects[AGENT_CR] = bound_agent("bedrock-opus")
    time.sleep(BINDING_TTL + 0.5)
    opus_usd = p.chat("two burritos").json()["cost_usd"]
    p.a2a("three burritos")
    total = p.get("/cost").json()["usd"]
    got = _gauge_matching(collector, {NOVA: nova_usd, OPUS: total - nova_usd})
    # The series add up to the cluster's spend, which is what /cost and the console's counter show.
    assert sum(got.values()) == pytest.approx(total)
    assert opus_usd > nova_usd

    # Spans and metrics export on separate schedules, so the second chat span can land after the gauge.
    chats = _two_chats(collector)
    assert [s["attributes"]["gen_ai.request.model"] for s in chats] == [NOVA, OPUS]
    sanitize = _by_name(collector, {"sanitize"})["sanitize"]
    assert sanitize["attributes"]["gen_ai.request.model"] == OPUS


def test_a_model_config_with_no_tier_reports_model_tiers_model(traced, agent, kube_api):
    """It is priced at MODEL_TIER, so it is reported as MODEL_TIER's model, as before the binding."""
    kube_api.objects[AGENT_CR] = bound_agent("gateway-local-qwen3")
    p, collector = traced(**kube_api.env, MODEL_TIER="opus")
    usd = p.chat("one burrito").json()["cost_usd"]
    chat = _by_name(collector, {"chat"})["chat"]
    assert chat["attributes"]["gen_ai.request.model"] == OPUS
    _gauge_matching(collector, {OPUS: usd})


def test_an_explicit_model_name_wins_over_the_bound_model(traced, agent, kube_api):
    kube_api.objects[AGENT_CR] = bound_agent("bedrock-nova")
    p, collector = traced(**kube_api.env, MODEL_TIER="nova", MODEL_NAME="burrito-model")
    first = p.chat("one burrito").json()["cost_usd"]
    kube_api.objects[AGENT_CR] = bound_agent("bedrock-opus")
    time.sleep(BINDING_TTL + 0.5)
    second = p.chat("two burritos").json()["cost_usd"]
    chats = _two_chats(collector)
    assert [s["attributes"]["gen_ai.request.model"] for s in chats] == ["burrito-model", "burrito-model"]
    # Still priced at the bound model; only the name reported is fixed.
    assert second > first
    _gauge_matching(collector, {"burrito-model": first + second})


def test_the_bound_model_has_a_series_before_it_spends(traced, agent, kube_api):
    """An idle cluster reads $0, as it did with one series, and a swap shows the new model at $0 until
    it answers, beside the old model's spend."""
    kube_api.objects[AGENT_CR] = bound_agent("bedrock-nova")
    p, collector = traced(**kube_api.env, MODEL_TIER="haiku")
    _gauge_matching(collector, {NOVA: 0.0})
    usd = p.chat("one burrito").json()["cost_usd"]
    _gauge_matching(collector, {NOVA: usd})
    kube_api.objects[AGENT_CR] = bound_agent("bedrock-opus")
    time.sleep(BINDING_TTL + 0.5)
    _gauge_matching(collector, {NOVA: usd, OPUS: 0.0})


def _records(p, event):
    p.events()  # flushes the capture file
    return [
        json.loads(line)
        for line in p.log_path.read_text().splitlines()
        if line.startswith("{") and json.loads(line).get("event") == event
    ]


def test_guard_logs_carry_the_trace(traced, agent):
    p, collector = traced(INPUT_BLOCKLIST="on")
    p.chat("nuke the kitchen")
    root = _by_name(collector, {"POST /chat"})["POST /chat"]
    (rec,) = _records(p, "input_blocklist_hit")
    assert rec["trace_id"] == root["trace_id"]
    assert rec["span_id"] == root["span_id"]


def test_a2a_guard_logs_carry_the_sanitize_span(traced, agent):
    p, collector = traced(INPUT_BLOCKLIST="on")
    p.a2a("nuke it")
    san = _by_name(collector, {"sanitize"})["sanitize"]
    (rec,) = _records(p, "input_blocklist_hit")
    assert (rec["trace_id"], rec["span_id"]) == (san["trace_id"], san["span_id"])


def test_logs_carry_no_trace_without_telemetry(start_proxy, agent):
    p = start_proxy(INPUT_BLOCKLIST="on")
    p.chat("nuke the kitchen")
    (rec,) = _records(p, "input_blocklist_hit")
    assert "trace_id" not in rec and "span_id" not in rec
