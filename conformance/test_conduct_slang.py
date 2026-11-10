# ABOUTME: Conformance for guard-proxy-agenticburn#84: the default list refuses the issue's slang sample in a /chat
# ABOUTME: prompt, and passes the ordinary text its short and numeric spellings collide with, in both builds.
from __future__ import annotations

import codecs
from pathlib import Path

import pytest

pytestmark = pytest.mark.guards

REFUSAL = "Let's keep it kind: BurritoBot only answers within the event's code of conduct."
FIXTURE = Path(__file__).resolve().parent / "coc_floor" / "slang-set.rot13.tsv"


def _slang_set() -> dict[str, list[str]]:
    """The fixture's texts by kind, refused or passes, decoded."""
    out: dict[str, list[str]] = {"refused": [], "passes": []}
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            kind, text = line.split("\t")
            out[kind].append(codecs.decode(text, "rot13"))
    return out


SLANG = _slang_set()


def _forwarded_prompts(agent) -> list[str]:
    return [c.json()["params"]["message"]["parts"][0]["text"] for c in agent.calls()]


def test_the_slang_sample_is_refused_before_the_agent(start_proxy, agent):
    p = start_proxy()
    # By index, so a failure prints no term.
    wrong = [i for i, text in enumerate(SLANG["refused"]) if p.chat(text).json()["reply"] != REFUSAL]
    assert wrong == []
    assert agent.calls() == []
    assert p.events().count("conduct_refused") == len(SLANG["refused"]) == 70


def test_the_slang_is_masked_on_the_wall(start_proxy, agent):
    # The wall reads the same list: a refused prompt goes up with nothing of it but the mask and the words around
    # it, and none of the fixture's texts carries a term the mask leaves.
    p = start_proxy(STREAM_PROMPTS="on", BLOCK_LIST="")
    for text in SLANG["refused"][:40]:
        p.chat(text)
    shown = p.get("/prompts").json()["prompts"]
    assert len(shown) == 40
    assert all("[redacted]" in s for s in shown)


def test_the_text_a_short_or_numeric_spelling_collides_with_reaches_the_agent(start_proxy, agent):
    p = start_proxy()
    refused = [t for t in SLANG["passes"] if p.chat(t).json()["reply"] == REFUSAL]
    assert refused == []
    assert _forwarded_prompts(agent) == SLANG["passes"]
    assert "conduct_refused" not in p.events()
