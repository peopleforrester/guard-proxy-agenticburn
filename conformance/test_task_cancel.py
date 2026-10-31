# ABOUTME: Conformance for the /chat forward as an A2A task: sent non-blocking, polled with tasks/get, and
# ABOUTME: cancelled with tasks/cancel when the forward fails or runs out of time, so the agent stops working.
from __future__ import annotations

import time

from fakes import Reply, a2a_result

KITCHEN_DOWN = {
    "reply": "BurritoBot's kitchen isn't answering. Try again in a moment.",
    "guarded": False,
    "input_tokens": 0,
    "output_tokens": 0,
}
TASK = "task-7"


def _task(state, task_id=TASK, rpc_id="chat"):
    """A JSON-RPC result carrying a task in `state`, with no reply yet, as kagent answers a running turn."""
    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "result": {"kind": "task", "id": task_id, "contextId": "ctx-1", "status": {"state": state}},
    }


def _completed(text, task_id=TASK, **kw):
    body = a2a_result(text, **kw)
    body["result"]["id"] = task_id
    return body


def _method(req):
    return req.json().get("method")


def _methods(agent):
    return [_method(c) for c in agent.calls()]


def _cancels(agent):
    return [c.json() for c in agent.calls() if _method(c) == "tasks/cancel"]


def _wait_for(predicate, timeout=5.0):
    """The cancel goes out after the room has its answer, so a test waits for it to arrive."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _never_done(task_id=TASK, cancel=None):
    """An agent that accepts the turn as a task and keeps working on it until it is cancelled."""

    def respond(req):
        method = _method(req)
        if method == "message/send":
            return Reply(body=_task("submitted", task_id))
        if method == "tasks/get":
            return Reply(body=_task("working", task_id))
        if method == "tasks/cancel":
            return cancel or Reply(body=_task("canceled", task_id, rpc_id=req.json().get("id")))
        return Reply(status=400, body={"error": f"unexpected {method}"})

    return respond


def _timed_chat(p, prompt="hi", session=None):
    start = time.monotonic()
    r = p.chat(prompt, session=session)
    return r, time.monotonic() - start


def test_the_forward_asks_for_the_task_without_blocking(start_proxy, agent):
    start_proxy().chat("hi")
    (first,) = agent.calls()
    assert first.json()["method"] == "message/send"
    assert first.json()["params"]["configuration"] == {"blocking": False}


def test_a_running_task_is_polled_until_it_completes(start_proxy, agent):
    gets = {"n": 0}

    def respond(req):
        if _method(req) == "message/send":
            return Reply(body=_task("submitted"))
        gets["n"] += 1
        if gets["n"] == 1:
            return Reply(body=_task("working"))
        return Reply(body=_completed("Two burritos.", prompt_tokens=30, output_tokens=7))

    agent.respond = respond
    r = start_proxy().chat("hi")
    assert r.status == 200
    body = r.json()
    assert body["reply"] == "Two burritos."
    assert (body["input_tokens"], body["output_tokens"]) == (30, 7)
    assert _methods(agent) == ["message/send", "tasks/get", "tasks/get"]
    for call in agent.calls()[1:]:
        assert call.json()["jsonrpc"] == "2.0"
        assert call.json()["params"] == {"id": TASK}


def test_a_task_that_finishes_is_not_cancelled(start_proxy, agent):
    p = start_proxy()
    assert p.chat("hi").status == 200
    time.sleep(0.3)
    assert _methods(agent) == ["message/send"]
    assert "task_cancel" not in p.events()


def test_a_task_past_proxy_timeout_is_cancelled(start_proxy, agent):
    """The 2026-10-31 runaway: the forward gave up at PROXY_TIMEOUT and the agent worked on for hours."""
    agent.respond = _never_done()
    p = start_proxy(PROXY_TIMEOUT="1")
    r, took = _timed_chat(p)
    assert r.status == 502
    assert r.json() == KITCHEN_DOWN
    assert took < 3, "the poll ran past PROXY_TIMEOUT"
    assert _wait_for(lambda: _cancels(agent)), "no tasks/cancel reached the agent"
    (cancel,) = _cancels(agent)
    assert cancel["jsonrpc"] == "2.0"
    assert cancel["params"] == {"id": TASK}
    assert _wait_for(lambda: "task_cancel" in p.events())
    assert "forward_error" in p.events()


def test_a_task_past_the_turn_deadline_is_cancelled(start_proxy, agent):
    agent.respond = _never_done()
    p = start_proxy(TURN_TIMEOUT="1.5", PROXY_TIMEOUT="5")
    r, took = _timed_chat(p)
    assert r.status == 502
    assert r.json() == KITCHEN_DOWN
    assert took < 3
    assert _wait_for(lambda: _cancels(agent))
    assert _cancels(agent)[0]["params"] == {"id": TASK}
    assert _wait_for(lambda: "task_cancel" in p.events())


def test_a_failed_poll_cancels_the_task(start_proxy, agent):
    def respond(req):
        if _method(req) == "tasks/get":
            return Reply(status=500, body={"error": "boom"})
        return _never_done()(req)

    agent.respond = respond
    p = start_proxy()
    r = p.chat("hi")
    assert r.status == 502
    assert r.json() == KITCHEN_DOWN
    assert _wait_for(lambda: _cancels(agent))
    assert _cancels(agent)[0]["params"] == {"id": TASK}


def test_the_optional_retry_is_cancelled_when_time_runs_out(start_proxy, agent):
    """The thinking-only retry keeps the first reply when time runs out, and its task still stops."""
    sends = {"n": 0}

    def respond(req):
        if _method(req) == "message/send":
            sends["n"] += 1
            if sends["n"] == 1:
                return Reply(body=_completed("<thinking>menu first</thinking>", task_id="task-1"))
        return _never_done("task-2")(req)

    agent.respond = respond
    p = start_proxy(TURN_TIMEOUT="1.5", PROXY_TIMEOUT="5")
    r, took = _timed_chat(p)
    assert r.status == 200
    assert r.json()["reply"] == "menu first"
    assert took < 3
    assert _wait_for(lambda: _cancels(agent))
    assert [c["params"] for c in _cancels(agent)] == [{"id": "task-2"}]


def test_a_refused_cancel_is_logged_and_the_room_still_gets_502(start_proxy, agent):
    refused = Reply(body={"jsonrpc": "2.0", "id": "cancel",
                          "error": {"code": -32002, "message": "Task cannot be canceled"}})
    agent.respond = _never_done(cancel=refused)
    p = start_proxy(PROXY_TIMEOUT="1")
    r = p.chat("hi")
    assert r.status == 502
    assert r.json() == KITCHEN_DOWN
    assert _wait_for(lambda: "task_cancel_failed" in p.events())
    assert "task_cancel" not in p.events()


def test_a_forward_that_never_returns_a_task_sends_no_cancel(start_proxy, agent):
    """Without a task id there is nothing to name in tasks/cancel."""
    agent.respond = lambda req: Reply(body=a2a_result(), delay=4)
    p = start_proxy(TURN_TIMEOUT="1", PROXY_TIMEOUT="5")
    assert p.chat("hi").status == 502
    time.sleep(0.5)
    assert _methods(agent) == ["message/send"]
    assert "task_cancel" not in p.events()
