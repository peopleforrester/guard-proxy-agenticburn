# ABOUTME: Conformance for GET /prompts, the moderated side-screen feed: off by default, captures both
# ABOUTME: paths before any guard, masks terms, skips probes, bounded, and names each /chat sender's witch.
from __future__ import annotations

from conftest import a2a_request

PROBE = "[[wib-probe]]"


def feed(p):
    return p.get("/prompts").json()


def test_feed_is_off_by_default_and_records_nothing(start_proxy, agent):
    p = start_proxy()
    p.chat("one burrito")
    p.a2a("one burrito")
    assert feed(p) == {"enabled": False, "prompts": [], "witches": []}


def test_feed_records_both_paths_in_order(start_proxy, agent):
    p = start_proxy(STREAM_PROMPTS="on")
    p.chat("first")
    p.a2a("second")
    assert feed(p) == {"enabled": True, "prompts": ["first", "second"], "witches": ["", ""]}


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


def test_feed_masks_terms_in_any_case(start_proxy, agent):
    """guard-proxy-agenticburn#4: a capitalized or mixed-case term must not reach the projected screen."""
    p = start_proxy(STREAM_PROMPTS="on")
    p.chat("Delete it, DELETE it, delete it, dElEtE it")
    p.chat("Fuck, FUCK, fuck, FuCk")
    assert feed(p)["prompts"] == [
        "[redacted] it, [redacted] it, [redacted] it, [redacted] it",
        "[redacted], [redacted], [redacted], [redacted]",
    ]


def test_feed_masks_custom_profanity_in_any_case(start_proxy, agent):
    p = start_proxy(STREAM_PROMPTS="on", PROFANITY_LIST="Cilantro")
    p.chat("no Cilantro, no CILANTRO, no cilantro")
    assert feed(p)["prompts"] == ["no [redacted], no [redacted], no [redacted]"]


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


# --- Personal data and whole-word terms (Unleash_an_Agent_Watch_It_Burn#646) ----------------------


def forwarded_text(agent, i=-1):
    return agent.calls()[i].json()["params"]["message"]["parts"][0]["text"]


def test_feed_redacts_personal_data_but_the_bot_gets_the_prompt_unchanged(start_proxy, agent):
    p = start_proxy(STREAM_PROMPTS="on")
    prompt = "email jane.doe+burrito@example.com or call (503) 555-0100, deliver to 1600 Pennsylvania Avenue"
    assert p.chat(prompt).json()["reply"] == "Here is your burrito."
    assert forwarded_text(agent) == prompt
    assert feed(p)["prompts"] == ["email [redacted] or call [redacted], deliver to [redacted]"]


def test_feed_redacts_numbers_in_every_written_form(start_proxy, agent):
    p = start_proxy(STREAM_PROMPTS="on")
    p.chat("card 4111 1111 1111 1111, or 4111-1111-1111-1111, or 4111111111111111")
    p.chat("ssn 123-45-6789, uk +44 20 7946 0958, local 555.0100")
    p.chat("send it to 42 elm st and ring twice")
    assert feed(p)["prompts"] == [
        "card [redacted], or [redacted], or [redacted]",
        "ssn [redacted], uk [redacted], local [redacted]",
        "send it to [redacted] and ring twice",
    ]


def test_feed_redacts_personal_data_on_the_a2a_path_too(start_proxy, agent):
    p = start_proxy(STREAM_PROMPTS="on")
    assert p.a2a("my email is pat@example.org").status == 200
    assert forwarded_text(agent) == "my email is pat@example.org"
    assert feed(p)["prompts"] == ["my email is [redacted]"]


def test_feed_leaves_an_ordinary_order_alone(start_proxy, agent):
    """Quantities, times and short order numbers are not personal data, and no term hides inside a word."""
    order = (
        "2 burritos and 3 tacos for the road, all the way, at 12:30, order 12345: spicy, spiced, "
        "spices, a cocktail, grapes, a peacock and Dickens"
    )
    p = start_proxy(STREAM_PROMPTS="on")
    p.chat(order)
    assert feed(p)["prompts"] == [order]


def test_feed_masks_inflected_and_compound_terms(start_proxy, agent):
    p = start_proxy(STREAM_PROMPTS="on")
    p.chat("this fucking bullshit, these bitches, two shits")
    assert feed(p)["prompts"] == ["this [redacted] [redacted], these [redacted], two [redacted]"]


def test_feed_masks_a_term_the_bot_never_gets(start_proxy, agent):
    # The code-of-conduct check (#78) refuses the prompt after the feed has read it, so the wall holds it
    # masked and the agent nothing.
    p = start_proxy(STREAM_PROMPTS="on")
    p.chat("this shit is good")
    assert agent.calls() == []
    assert feed(p)["prompts"] == ["this [redacted] is good"]


def test_a_chat_probe_reaches_the_agent_without_its_marker(start_proxy, agent):
    """guard-proxy-agenticburn#70: the marker is the feed's alone. Forwarded, the model read it as a ticket
    id and called the turn a probe, so a live probe tested a message no guest sends."""
    p = start_proxy(STREAM_PROMPTS="on")
    assert p.chat(f"one burrito {PROBE}").json()["reply"] == "Here is your burrito."
    assert p.post("/chat", {"prompt": f"{PROBE} two tacos", "witch": "Agatha the Banshee"}).status == 200
    assert [forwarded_text(agent, i) for i in (0, 1)] == ["one burrito", "two tacos"]
    assert all(PROBE not in call.body.decode() for call in agent.calls())
    assert feed(p)["prompts"] == []


def test_a_chat_prompt_that_is_only_the_probe_marker_is_empty(start_proxy, agent):
    p = start_proxy(STREAM_PROMPTS="on")
    resp = p.chat(f"  {PROBE} ")
    assert (resp.status, resp.json()["reply"]) == (400, "Tell BurritoBot what you'd like.")
    assert agent.calls() == []
    assert feed(p)["prompts"] == []


def test_feed_masks_before_truncating_so_no_partial_number_shows(start_proxy, agent):
    p = start_proxy(STREAM_PROMPTS="on")
    p.chat("a" * 269 + " 4111 1111 1111 1111")
    assert feed(p)["prompts"] == ["a" * 269 + " [redacted]"]


# --- Each prompt's witch name (Unleash_an_Agent_Watch_It_Burn#921) -----------------------------------
# `prompts` stays the bare texts the prompt wall, lab-platform's relay and display.js parse as strings.
# `witches` holds each prompt's sender's witch name at the same index, "" when there is none.


def chat_as(p, prompt, witch=None):
    body = {"prompt": prompt}
    if witch is not None:
        body["witch"] = witch
    return p.post("/chat", body)


def test_witches_carry_each_prompts_name_at_its_index_and_prompts_stay_texts(start_proxy, agent):
    p = start_proxy(STREAM_PROMPTS="on")
    chat_as(p, "one burrito", "Agatha the Banshee")
    chat_as(p, "two tacos", "Hazel the Ghoul")
    assert feed(p) == {
        "enabled": True,
        "prompts": ["one burrito", "two tacos"],
        "witches": ["Agatha the Banshee", "Hazel the Ghoul"],
    }


def test_a_prompt_with_no_witch_name_carries_an_empty_one(start_proxy, agent):
    p = start_proxy(STREAM_PROMPTS="on")
    chat_as(p, "no field")
    chat_as(p, "blank", "   ")
    chat_as(p, "not a string", ["Agatha the Banshee"])
    chat_as(p, "not shaped", "agatha")
    p.a2a("from the terminal")
    body = feed(p)
    assert body["prompts"] == [
        "no field",
        "blank",
        "not a string",
        "not shaped",
        "from the terminal",
    ]
    assert body["witches"] == ["", "", "", "", ""]


def test_a_witch_name_moderation_would_mask_is_dropped_whole(start_proxy, agent):
    p = start_proxy(STREAM_PROMPTS="on")
    chat_as(p, "one burrito", "Shit the Ghoul")
    chat_as(p, "two burritos", "Delete the Witch")
    assert feed(p)["witches"] == ["", ""]


def test_witches_keep_the_latest_50_in_step_with_prompts(start_proxy, agent):
    p = start_proxy(STREAM_PROMPTS="on")
    for i in range(55):
        chat_as(p, f"order {i}", "Agatha the Banshee" if i % 2 else "")
    body = feed(p)
    assert body["prompts"] == [f"order {i}" for i in range(5, 55)]
    assert body["witches"] == ["Agatha the Banshee" if i % 2 else "" for i in range(5, 55)]


def test_witches_follow_moderation_and_probes_as_prompts_do(start_proxy, agent):
    p = start_proxy(STREAM_PROMPTS="on")
    chat_as(p, "call (503) 555-0100", "Agatha the Banshee")
    chat_as(p, f"smoke {PROBE}", "Hazel the Ghoul")
    assert feed(p) == {
        "enabled": True,
        "prompts": ["call [redacted]"],
        "witches": ["Agatha the Banshee"],
    }


def test_the_feed_off_records_no_witch_name(start_proxy, agent):
    p = start_proxy()
    chat_as(p, "one burrito", "Agatha the Banshee")
    assert feed(p) == {"enabled": False, "prompts": [], "witches": []}


def test_a_full_feed_of_named_emoji_prompts_fits_lab_platforms_read(start_proxy, agent):
    """lab-platform's promptfeed refuses a /prompts body over 256 KiB whole, so the cluster would read
    unavailable. Fifty 280-character emoji prompts, each with the longest shaped name, must fit."""
    p = start_proxy(STREAM_PROMPTS="on")
    longest = "Abcdefghijklmnop the Abcdefghijklmnop"
    for _ in range(50):
        chat_as(p, "\U0001f32f" * 400, longest)
    resp = p.get("/prompts")
    body = resp.json()
    assert body["witches"] == [longest] * 50
    assert len(body["prompts"]) == 50
    assert len(resp.raw) <= 256 << 10
