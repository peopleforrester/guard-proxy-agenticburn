# ABOUTME: proxy.py's code-of-conduct pattern matches as the spike's single alternation does (#84), in time linear in
# ABOUTME: the text (#83): each repeated leetspeak run is timed at 1,000 and 4,000 characters, as the workshop's is.
"""In-process, standard library only: proxy.py is loaded as a module, as launch_python.py loads it, and its
conduct_matches(), the one matcher the /chat refusal and the reply mask both read, is timed directly. Timing it
over HTTP would time the server as well.

Unleash_an_Agent_Watch_It_Burn#1300 found that with each letter of a term its whole class repeated (`[s5$]+`), a
run of `$`, `5$`, `(` or `4@` let a term start after every symbol and each start read to the run's end, and a run two
neighbouring letters can both take was split every way there is: Python's backtracking `re` took seconds on 4,000
characters. The cases and the verdict follow that repo's verify/coc_linear_cases.py:

    pair_cases()     a run of each pair of characters one letter's class holds (the letter, its leetspeak stand-ins,
                     `*` for a vowel), and of each pair of separator characters, a pair of one character included
    in_term_cases()  for each pair of neighbouring letters (or a letter and a phrase's separator) whose classes share
                     a character, the first term with them, up to them, then a run of each pair of what they share

At 4,000 characters a case takes under 50 ms, and at most eight times its 1,000-character cost wherever it takes
1 ms or more (linear time takes four times as long on four times the text; time in the square of the length takes
sixteen). Each time is the best of three, and a case outside a bound is timed again over nine, whose verdict stands:
a burst of load on a shared host can slow one length's runs, while a quadratic case is outside on every run.
"""

from __future__ import annotations

import codecs
import importlib.util
import itertools
import json
import os
import random
import re
import time
from pathlib import Path
from unittest import mock

import pytest

PROXY_PY = Path(__file__).resolve().parent.parent / "proxy.py"
SEPARATORS = " ._*-"  # proxy._CONDUCT_SEP's characters, its whitespace as the space
SHORT, LONG = 1_000, 4_000
LIMIT_MS, GROWTH, NOISE_MS = 50.0, 8.0, 1.0
RUNS, CONFIRM_RUNS = 3, 9

FIXTURES = Path(__file__).resolve().parent / "coc_floor"


def _load_proxy():
    # proxy.py reads its environment at import. Only the variables the subprocess fixtures pass are kept, so a
    # PROFANITY_LIST in the developer's shell cannot change the pattern under test.
    keep = {
        k: os.environ[k]
        for k in ("PATH", "HOME", "LANG", "TMPDIR", "SYSTEMROOT")
        if k in os.environ
    }
    spec = importlib.util.spec_from_file_location("proxy_conduct_linear", PROXY_PY)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(os.environ, keep, clear=True):
        spec.loader.exec_module(module)
    return module


proxy = _load_proxy()
DEFAULT_TERMS = [t.strip().lower() for t in proxy._DEFAULT_PROFANITY.split(",") if t.strip()]


def within(short_ms: float, long_ms: float) -> bool:
    return long_ms < LIMIT_MS and (long_ms < NOISE_MS or long_ms <= GROWTH * short_ms)


def chars(letter: str) -> str:
    """The characters proxy.py's class for the letter matches, or the separator's for " "."""
    if letter == " ":
        return SEPARATORS
    return (
        letter
        + proxy._CONDUCT_LEET.get(letter, "")
        + ("*" if letter in proxy._CONDUCT_VOWELS else "")
    )


def _units(alphabet: str) -> list[str]:
    return [a + b if a != b else a for a, b in itertools.combinations_with_replacement(alphabet, 2)]


def pair_cases() -> list[tuple[str, str, str]]:
    seen, out = set(), []
    for letter in [*sorted(proxy._CONDUCT_LEET), " "]:
        for unit in _units(chars(letter)):
            if unit not in seen:
                seen.add(unit)
                out.append((f"run of {unit!r}", "", unit))
    return out


def in_term_cases() -> list[tuple[str, str, str]]:
    seen, out = set(), []
    for term in DEFAULT_TERMS:
        text = " ".join(proxy.conduct_fold(term)[0].split())
        for i in range(len(text) - 1):
            x, y = text[i], text[i + 1]
            shared = "".join(c for c in chars(x) if c in chars(y))
            if not shared or (x, y) in seen:
                continue
            seen.add((x, y))
            prefix = text[:i]
            for unit in _units(shared):
                # The prefix is a term's start, so the id shows it ROT13-encoded, as proxy.py keeps the terms.
                out.append((f"{codecs.encode(prefix, 'rot13')!r} then {unit!r}", prefix, unit))
    return out


def text(prefix: str, unit: str, length: int) -> str:
    return prefix + (unit * length)[:length]


def best_ms(fn, texts: tuple[str, ...], runs: int) -> list[float]:
    """The fastest of `runs` timings of fn on each text, in ms. The texts take turns, so a burst of load on the
    host falls on both lengths rather than on one."""
    best = [float("inf")] * len(texts)
    for _ in range(runs):
        for i, t in enumerate(texts):
            t0 = time.perf_counter()
            fn(t)
            best[i] = min(best[i], time.perf_counter() - t0)
    return [b * 1000 for b in best]


def judge(
    fn, prefix: str, unit: str, attempts: tuple[int, ...] = (RUNS, CONFIRM_RUNS)
) -> tuple[float, float, bool]:
    texts = (text(prefix, unit, SHORT), text(prefix, unit, LONG))
    for runs in attempts:
        short, long = best_ms(fn, texts, runs)
        ok = within(short, long)
        if ok:
            break
    return short, long, ok


CASES = pair_cases() + in_term_cases()


def _single_alternation(terms: list[str]) -> re.Pattern:
    """The terms' alternatives in one alternation, longest term first, as the spike's floor.py joins them."""
    alts = [a for t in sorted(terms, key=len, reverse=True) for a in proxy._conduct_term(t)]
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(alts) + r")s?(?![a-z0-9])")


def test_the_grouped_pattern_matches_as_the_single_alternation():
    # Grouped by first character (#84), a word start tries the alternatives its first character can begin, in the
    # order the single alternation tries them, so every span is the same: on the abuse set, the stage's texts, each
    # term plain, spaced, dotted and capitalised, and random strings of the letters, stand-ins and separators.
    assert proxy._PROFANITY_RE.pattern == proxy._profanity_pattern(DEFAULT_TERMS).pattern
    single = _single_alternation(DEFAULT_TERMS)
    texts = [
        codecs.decode(line.split("\t")[1], "rot13")
        for line in (FIXTURES / "abuse-set.rot13.tsv").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    stage = json.loads((FIXTURES / "stage-texts.json").read_text(encoding="utf-8"))["texts"]
    texts += [t for group in stage.values() for t in group]
    for term in DEFAULT_TERMS:
        texts += [term, term + "s!", " ".join(term), ".".join(term), term.upper()]
    rng = random.Random(84)
    alphabet = "fuckshitbaonrgdlewy 1|!*$5@4(3.-_+7089\u200b\uff46"
    texts += ["".join(rng.choice(alphabet) for _ in range(rng.randint(1, 40))) for _ in range(20_000)]
    folded = [proxy.conduct_fold(t)[0] for t in texts]
    differ = sum(
        [m.span() for m in proxy._PROFANITY_RE.finditer(f)] != [m.span() for m in single.finditer(f)]
        for f in folded
    )
    # Counted, not listed, so a failure prints no term.
    assert differ == 0, f"the grouped pattern's spans differ on {differ} of {len(texts)} texts"


def test_a_letter_is_one_character_then_a_run_of_the_letter():
    # floor.py's _term for the same words, its `[k]+` written `[k][k]*`: the doubled s of "ass" is one s and then a
    # run, the 1 of "kil1" an l.
    assert proxy._conduct_term("ass") == [
        r"[a4@\*][a4]*[s5\$][s5\$][s5]*",
        r"[a4@\*][\s._*\-][s5\$][\s._*\-][s5\$]",
    ]
    assert proxy._conduct_term("kill") == [
        r"[k][k]*[i1!\|\*][i]*[l1\|][l1\|][l1]*",
        r"[k][\s._*\-][i1!\|\*][\s._*\-][l1\|][\s._*\-][l1\|]",
    ]
    assert proxy._conduct_term("f u") == [r"[f][f]*[\s._*\-]+[u\*][u]*"]


def test_the_cases_reach_the_stand_ins_and_the_terms():
    # Every character a letter with a stand-in matches runs alone, and the terms give the in-term cases: in the
    # workshop's list over 20 pairs of neighbouring letters share a character.
    assert {u for _, _, u in pair_cases()} >= {c for ch in proxy._CONDUCT_LEET for c in chars(ch)}
    assert len({p for _, p, _ in in_term_cases()}) > 20


@pytest.mark.parametrize(
    ("prefix", "unit"), [(p, u) for _, p, u in CASES], ids=[label for label, _, _ in CASES]
)
def test_each_leetspeak_run_matches_in_linear_time(prefix, unit):
    short, long, ok = judge(proxy.conduct_matches, prefix, unit)
    assert ok, (
        f"{short:.1f} ms at {SHORT:,} characters, {long:.1f} ms at {LONG:,}: over {LIMIT_MS:.0f} ms, "
        f"or more than {GROWTH:.0f} times as long on four times the text"
    )


def test_the_timing_fails_the_pattern_as_it_was_before_1300():
    # Built the old way, each letter its whole class repeated, a run of "$" takes seconds and fails the verdict.
    with mock.patch.object(
        proxy,
        "_conduct_run",
        lambda ch, after: proxy._conduct_letter(ch) + proxy._conduct_letter(ch) + "*",
    ):
        before = proxy._profanity_pattern(DEFAULT_TERMS)
    assert before.pattern != proxy._PROFANITY_RE.pattern
    short, long, ok = judge(lambda t: list(before.finditer(t)), "", "$", attempts=(1,))
    assert not ok, f"the old form passed: {short:.0f} ms at {SHORT:,}, {long:.0f} ms at {LONG:,}"
