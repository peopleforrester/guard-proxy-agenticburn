# ABOUTME: Conformance for GET /prompts, the moderated side-screen feed: off by default, captures both
# ABOUTME: paths before any guard runs, masks block-listed and profane terms, skips probes, bounded.
from __future__ import annotations

from conftest import a2a_request

PROBE = "[[wib-probe]]"


def feed(p):
    return p.get("/prompts").json()


def test_feed_is_off_by_default_and_records_nothing(start_proxy, agent):
    p = start_proxy()
    p.chat("one burrito")
    p.a2a("one burrito")
    assert feed(p) == {"enabled": False, "prompts": []}


def test_feed_records_both_paths_in_order(start_proxy, agent):
    p = start_proxy(STREAM_PROMPTS="on")
    p.chat("first")
    p.a2a("second")
    assert feed(p) == {"enabled": True, "prompts": ["first", "second"]}


def test_feed_records_before_guards_so_blocked_prompts_appear_masked(start_proxy, agent):
    p = start_proxy(STREAM_PROMPTS="on", INPUT_BLOCKLIST="on")
    assert p.chat("please delete the menu").json()["guarded"] is True
    assert p.a2a("NUKE it").status == 403
    assert feed(p)["prompts"] == ["please [redacted] the menu", "[redacted] it"]


def test_feed_masks_default_profanity(start_proxy, agent):
    p = start_proxy(STREAM_PROMPTS="on")
    p.chat("this shit is good")
    assert feed(p)["prompts"] == ["this [redacted] is good"]


def test_feed_profanity_list_is_configurable(start_proxy, agent):
    p = start_proxy(STREAM_PROMPTS="on", PROFANITY_LIST="cilantro")
    p.chat("no cilantro, this shit is good")
    assert feed(p)["prompts"] == ["no [redacted], this shit is good"]


def test_feed_masks_only_lowercase_and_uppercase_forms_today(start_proxy, agent):
    """Known defect, guard-proxy-agenticburn#4: a capitalized term reaches the projected screen."""
    p = start_proxy(STREAM_PROMPTS="on")
    p.chat("Delete it, DELETE it, delete it")
    assert feed(p)["prompts"] == ["Delete it, [redacted] it, [redacted] it"]


def test_feed_truncates_to_280_characters(start_proxy, agent):
    p = start_proxy(STREAM_PROMPTS="on")
    p.chat("a" * 400)
    assert feed(p)["prompts"] == ["a" * 280]


def test_feed_keeps_the_latest_50(start_proxy, agent):
    p = start_proxy(STREAM_PROMPTS="on")
    for i in range(55):
        p.chat(f"order {i}")
    assert feed(p)["prompts"] == [f"order {i}" for i in range(5, 55)]


def test_feed_skips_probes_but_answers_them(start_proxy, agent):
    p = start_proxy(STREAM_PROMPTS="on")
    assert p.chat(f"smoke {PROBE}").json()["reply"] == "Here is your burrito."
    assert p.a2a(f"smoke {PROBE}").status == 200
    assert feed(p)["prompts"] == []


def test_feed_skips_empty_and_textless_requests(start_proxy, agent):
    p = start_proxy(STREAM_PROMPTS="on")
    p.post("/chat", {"prompt": "  "})
    body = a2a_request("x")
    body["params"]["message"]["parts"] = []
    p.post("/", body)
    p.post("/", b"not json")
    assert feed(p)["prompts"] == []
