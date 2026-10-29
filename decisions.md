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

## 2026-10-29: one TURN_TIMEOUT deadline per /chat turn

A `/chat` turn makes up to six upstream calls in a row, and `PROXY_TIMEOUT` bounded each alone, so the
workshop had to set `PROXY_TIMEOUT=45` to keep six of them under Railway's 300 s limit
(Unleash_an_Agent_Watch_It_Burn#450). `TURN_TIMEOUT` is now one deadline for the turn, read when
`_handle_chat` starts (guard-proxy-agenticburn#21). Choices the issue left open:

- **Default `0`, no turn deadline.** Unset, every call keeps its own `PROXY_TIMEOUT`, as before. The
  workshop sets the value, since only it knows nginx's `/chat` timeout.
- **A guard call cut off by the deadline is a 502, not a fail-policy decision.** A classifier that ran
  out of turn is not a scanner outage, and failing open there would forward a prompt with no time left
  to answer it, or return a reply the output scrub never read. Failing closed would show a guardrail
  refusal for what was a timeout. Both answer the forward's existing 502 instead, logged as
  `turn_timeout`.
- **The thinking-only retry keeps the first reply when time runs out**, as it does when the retry
  fails (guard-proxy-agenticburn#1). The dangling `tool_use` retry is not optional, so it answers 502.
- **Only `/chat`.** The A2A passthrough keeps `PROXY_TIMEOUT` per call; the issue is about the turn.
- **urllib's timeout is per socket operation**, so the deadline bounds the wait for an answer, and an
  upstream trickling bytes can still run past it. Go's `http.Client.Timeout` and a context deadline
  are total, so the Go route will be stricter, as `PROXY_TIMEOUT` already is there.
- **The Go build reads and validates `TURN_TIMEOUT`** (and `chat.turn_timeout` in YAML) but has no
  `/chat` to apply it to until PRD #412 M3. The conformance cases are in `test_turn_deadline.py`,
  unmarked like the rest of the `/chat` cases, so the Go subset picks them up when `/chat` lands.
