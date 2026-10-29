# Decisions

## 2026-10-25: opentelemetry-sdk 1.45.1 as a dev dependency

The conformance suite needs to read the spans proxy.py emits, to pin the `session.id` and
`witb.guard.decision` attributes on the `POST /chat` root span (guard-proxy-agenticburn#8). In the
workshop the OTel Operator installs the SDK before proxy.py runs; without an SDK every span is a no-op
and there is nothing to assert on. `conformance/launch_python.py` therefore installs the real SDK's
tracer provider when `CONFORMANCE_SPANS` names a file, and exports each finished span to it as one JSON
line through the SDK's own `ConsoleSpanExporter`.

Pinned at `==1.45.1`, the current release on PyPI on 2026-10-25 (published 2026-10-06). It is a dev
dependency only: proxy.py stays stdlib-only and still runs with no OTel present. Having
`opentelemetry-api` installed in the dev environment also means the whole suite now runs proxy.py with
its OTel code path active (as no-op spans when no provider is installed), which is the path the
workshop runs.

## 2026-10-29: Lifecycle state tracking adopted

`PROJECT_STATE.md` added on the lifecycle schema, seeded at 3.3 because the last unit of work
(`000f57e`) was already on `main` with green CI. The open agent-task issues are its current plan.

## 2026-10-29: the prompt feed redacts personal data, and its terms match whole words

The workshop projects `GET /prompts` on the prompt wall, and the 2026-10-28 planning call
(Unleash_an_Agent_Watch_It_Burn#646) asked that a prompt carrying a code-of-conduct term or personal data
still reach the bot but show redacted on the wall. `moderate()` now masks, before the 280-character cut:

- email addresses; any run of seven or more digits with spaces, dots, dashes or brackets between them
  (phone, card, Social Security and account numbers, and a dash-written date as a side effect); and a
  street address, a house number with one to four words and a street suffix. No Luhn check narrows the
  card case, because a projected sixteen-digit number is unwanted whether or not it is a valid card.
- `PROFANITY_LIST` terms as whole words with an optional plural `s`, the topic phrases' rule. As
  substrings they masked the menu: "spicy" showed as "[redacted]y" (from "spic"), and "cocktail", "grapes"
  and "Dickens" lost their middles. Inflected forms and compounds (fucking, bullshit) are listed
  explicitly in the default instead, since generated endings turn "spic" into "spices". `BLOCK_LIST` still
  masks as substrings, since it is the same list the input guard blocks on.

Only the feed copy is masked; `/chat` and the A2A path forward the prompt unchanged, which
`test_prompt_feed.py` pins. Names on their own are not caught. The email pattern starts a match only where
a run of address characters starts, because without that a 100 KB body with no `@` took 30 seconds.
