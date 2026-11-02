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

## 2026-10-30: /toggle sets the cluster-wide cost cap at runtime

The KubeCon finale (Unleash_an_Agent_Watch_It_Burn#690) lowers `COST_CAP_USD` live so the room can trip
it in minutes, then puts it back (guard-proxy-agenticburn#23). `GET /toggle?cost_cap=N` sets the cap
`cost_capped()` checks, `cost_cap=default` restores the env value, and both `/cost`'s `cap_usd` and the
two "cap reached" replies use the runtime value. Choices the issue left open:

- **A runtime cap only lowers.** It must be a finite number above 0 and, while `COST_CAP_USD` is set, no
  higher than it. `/toggle` has no auth and the agent's tool pod can reach it (the workshop's
  `verify/test_mesh_enrollment.py`), so a prompt-injected agent must not be able to lift the Bedrock bill
  backstop or switch it off with 0. With `COST_CAP_USD` unset any positive value is a lowering.
- **A refused value answers 400 and applies nothing**, guard keys in the same query included, so a
  helper that pairs a cap with a guard flip never half-applies. An empty `cost_cap=` is refused rather
  than ignored, so a helper with an unset shell variable fails loudly.
- **`/toggle` reports the cap as `cost_cap_usd`** beside the guard keys. `/guards` stays the guard map
  alone, since the terminal's `guards-status` and the Go build read it as one. The workshop's consumers of
  the `/toggle` response read keys by name, so the added key breaks none of them.
- **Not in the Go build.** Its `/toggle` and `/cost` answer 501 until their PRD #412 milestone, so the
  new cases are unmarked, like the rest of `/toggle`'s, and the Go subset picks them up when those
  endpoints land.

## 2026-10-30: The cluster-wide cost cap answers its own reply

`_chat_turn` answered "The kitchen tab is frozen ... Press Reset" for both caps, which is wrong when
`COST_CAP_USD` (or `/toggle?cost_cap=N`) tripped: Reset lifts only the per-session budget
(guard-proxy-agenticburn#25, asked by Unleash_an_Agent_Watch_It_Burn#690 for the KubeCon finale).

- **`cost_capped()` returns which cap tripped** (`"cost_cap"`, `"budget"` or `None`), so each reply quotes
  the cap that stopped the turn. Before, the budget figure was quoted whenever the budget guard was on,
  even when the cluster cap was the one that tripped.
- **The cluster-wide cap answers first when both have tripped.** Pressing Reset would clear the budget and
  still leave the cluster cap holding the conversation, so telling the user to press it misleads.
- **`witb.guard.decision` gains `cost_cap`**, and `budget` now means the per-session cap only. The
  workshop's `verify/test_trace_walkthrough_view.py` still finds `budget` among the stamps, and C6 lists
  neither.
- **The A2A 429 names the cap too**: "Cost cap reached ($X on this cluster). Spend is frozen for
  everyone; ..." for the cluster cap, and the existing "Budget cap reached" text for the budget cap. No
  workshop consumer outside its vendored copy of this suite matches the old text.

## 2026-10-31: Port /chat and the admin endpoints to Go as proxy.py does them

Approved by Michael on 2026-10-31, choosing "port as-is" over doing the PRD's M3 now. The Go build will
serve `/chat`, `/cost`, `/prompts`, `/controls` and `/toggle` with spend metered after each response,
exactly as `proxy.py`, until the full conformance suite passes against it. That unblocks #22.

### REJECTED (for now): building the M3 budget reservation in this port
**Why:** the reservation counts a request's tokens with Bedrock `CountTokens` and caps `max_tokens`
before the model is called. guard-proxy sits in front of the agent (kagent at `AGENT_URL`) and sees
token usage only in the agent's reply, so it never holds a model request to count or cap.
**Status:** Revisit when a design puts the proxy (or a second hop) between kagent and Bedrock.
**Do not suggest:** estimating tokens from the A2A prompt text, or capping the agent's turn by
cancelling it mid-flight. Neither bounds what a single model call spends.

## 2026-10-31: a /chat forward is a polled A2A task, cancelled when the forward gives up

On 2026-10-31 a `/chat` turn on burritobot-private passed `PROXY_TIMEOUT` (120 s); guard-proxy answered
502 and the agent worked on for about 2.5 hours, 613 `deploy_app` calls, until its pod was deleted
(guard-proxy-agenticburn#27, asked by Unleash_an_Agent_Watch_It_Burn#742 for the KubeCon breakout on
2026-11-12). The fix sends `tasks/cancel` for the turn's task. Read from the source the workshop runs,
kagent v0.10.1 with `runtime: go` (`go/adk/pkg/a2a`) on a2a-go v0.3.15 (`internal/taskexec`,
`a2asrv/agentexec.go`), on 2026-10-31:

- **Why the agent kept going.** a2a-go starts every execution in `context.WithoutCancel`, so a client
  hanging up stops nothing. `tasks/cancel` does stop it: the executor's `Cancel` writes a final
  `canceled` event into the running execution's queue, the consumer returns, and that cancels the
  producer's context, which the ADK runner and its Bedrock call run under.
- **Why the forward is non-blocking.** The task id is the server's (`a2a.NewTaskID()`); a message naming
  a task id the store does not hold fails with "task loading failed", and kagent's task store answers
  `tasks/list` with "not supported". A blocking `message/send` therefore never tells the proxy which task
  to cancel. `message/send` with `configuration.blocking: false` answers at the task's first event,
  kagent's `submitted`, which it writes before the model runs.
- **Polled with `tasks/get`, not streamed.** `message/stream` would give the id too, but every
  conformance fake, and the call counts the suite pins, would have to change to SSE. A task that is
  already finished in the first answer, as every existing fake's is, is used as it is, so polling only
  adds calls for a turn still running. `tasks/get` returns the same stored task a blocking send returns,
  usage metadata included. The poll waits 0.25 s, doubling to 1 s, so a turn answers at most about a
  second later than it finishes.
- **What counts as running.** `submitted` and `working`. Every other state ends the poll, including
  `input-required` and `auth-required`, where a blocking send also returns. A running task with no id,
  or a `tasks/get` answer with no task, fails the forward.
- **`PROXY_TIMEOUT` bounds the whole forward**, the send and every poll, as it bounded the one blocking
  call before; `TURN_TIMEOUT` cuts it shorter. Without that, a turn with no `TURN_TIMEOUT` would poll
  forever.
- **The cancel goes out after the turn has answered**, on its own thread or goroutine, bounded by
  `PROXY_TIMEOUT`, so the room's 502 is not held up behind it. It is sent whenever the forward fails with a
  task id in hand: a deadline, a failed poll, or the optional thinking-only retry running out of time
  (that turn still answers 200 with the first reply). It logs `task_cancel` (INFO) with the task id and
  the state the agent reports, or `task_cancel_failed` (ERROR) when the call fails or the agent answers a
  JSON-RPC error. A send that fails before returning a task has no id to cancel, and logs only its
  `forward_error`.
- **Only `/chat`.** The A2A passthrough forwards the caller's own request, blocking or not, and the caller
  owns its task.
- **Assumes one agent replica.** a2a-go's execution manager is in-process, so `tasks/cancel` stops the
  task only on the pod running it. The workshop runs one `workshop-agent` pod behind agentgateway's
  `:3000` bind, a plain HTTP route that passes every JSON-RPC method.
- **The Python runtime cannot be cancelled.** Read at kagent v0.10.1 and a2a-sdk 0.3.26 (the version
  kagent's `python/uv.lock` pins) on 2026-10-31 for guard-proxy-agenticburn#29: `kagent-adk`'s
  `A2aAgentExecutor.cancel` (`_agent_executor.py`) raises `NotImplementedError("Cancellation is not
  supported")`. a2a-sdk's `DefaultRequestHandler.on_cancel_task` calls the executor's `cancel` before it
  cancels the running producer task, so the exception skips that step, and the JSON-RPC app answers
  `-32603` with that message. The proxy logs `task_cancel_failed` and the run goes on. The fix only
  works on `runtime: go`. The v1alpha2 Agent schema defaults `spec.declarative.runtime` to `go` (but
  `EffectiveDeclarativeRuntime` falls back to Python when it is unset), and the workshop's Agent sets
  none, so the stored value is likely `go`. Unleash_an_Agent_Watch_It_Burn#697 left that to be read
  from a live cluster.
- **Not a budget control.** The Go port's entry above rejects cancelling a turn mid-flight as a way to
  bound spend, and this does not change that: the cancel stops work on a turn the proxy has already given
  up on, and the model call in flight when it lands is still billed.

## 2026-11-01: the Go build keeps spend in a bbolt file on the pod's volume

PRD #412 decision 2 (approved 2026-10-11) says budget state persists across restarts in an embedded
store, SQLite or bbolt, file-backed on the pod's volume, so a restart cannot reset a budget
(guard-proxy-agenticburn#32, from Unleash_an_Agent_Watch_It_Burn#737, M3). Until now `internal/spend`
kept the tally in memory, so restarting the process was a way around both caps. `proxy.py`, which
KubeCon runs, does not change.

- **`go.etcd.io/bbolt` v1.5.0**, the latest release on proxy.golang.org on 2026-11-01 (published
  2026-06-03), with `golang.org/x/sys` v0.45.0 as its only dependency. Pure Go, so it builds with
  `CGO_ENABLED=0` (#30). Chosen over `modernc.org/sqlite` because the state is three keyed values and
  needs no queries: bbolt is one small package where the SQLite port is a transpiled C library and a
  much larger binary. Each `db.Update` fsyncs on commit by default (`NoSync` is left off).
- **What is stored.** The cluster tally `/cost` reports, each session's spend, and the runtime cost cap
  `/toggle?cost_cap=` sets. Session ids are hashed with SHA-256 before they are keys, since the id is
  whatever the browser sends and a bbolt key is at most 32 KiB.
- **The runtime cost cap is stored too.** The KubeCon finale lowers it so the room can trip it; if the
  pod restarted mid-finale and the cap went back to `COST_CAP_USD`, a restart would reopen a kitchen the
  instructor closed. A cap equal to `COST_CAP_USD` (`cost_cap=default`) is stored as no runtime cap, so a
  later change to `COST_CAP_USD` applies. On start a stored cap applies only while it is above 0 and, when
  `COST_CAP_USD` is set, no higher than it, the same rule `/toggle` enforces; otherwise `COST_CAP_USD`
  does. Since the runtime cap only lowers, keeping it never lifts the backstop.
- **`spend.store_path` (or `SPEND_STORE_PATH`) is required.** Unset or blank stops startup. There is no
  memory-only mode, because the PRD says fail closed by default and a silent memory fallback is the
  restart bypass this closes. A path the process cannot open for writing (a read-only or missing
  directory, a directory) stops startup before the port is bound, and so does a file another process
  holds: bbolt takes an exclusive `flock`, the open waits 2 s for it, and two processes metering into
  one file would each overwrite the other's tally. The workshop Deployment therefore needs
  `strategy: Recreate` (or one pod at a time) as well as the volume, at the Go cutover
  (Unleash_an_Agent_Watch_It_Burn#738).
- **Each reply's spend is committed before the reply is returned.** `Meter.Record` adds to the tally in
  memory and then commits the whole tally and the session's new total in one transaction, under the
  meter's lock, so the file is never behind a reply that has left. When the commit fails the spend still
  counts in memory, so the caps hold in this process, the next commit writes the whole tally again, and
  the reply is withheld: `/chat` answers its 502 sentence (also after the optional thinking-only retry),
  the A2A path a 502 naming the error, and both log `spend_store_error` (ERROR). The model call was
  already billed, but returning the reply would leave spend that the next restart forgets. A `/toggle`
  whose cap cannot be stored answers 500 and applies nothing, guard keys included.
- **Reset stays a fresh session.** The issue describes Reset as "the /toggle path that clears a session's
  budget", but no such path exists in either build. The storefront's Reset (`resetChat()` in the
  workshop's `gitops/ai-layer/web/burritbot.html`, pinned by its `verify/test_fresh_context.py`) mints a
  new session id, and a new id has no stored spend, so Reset after a restart gives the same fresh
  budget it gave before. The old session's spend stays in the store and in the cluster tally. No
  server-side clear is added: `/toggle` has no auth and the agent's tool pod can reach it (the
  2026-10-30 cost cap entry), so an endpoint that zeroes a session's spend would let a prompt-injected
  agent lift its own budget cap.
- **The window is unchanged:** the tally runs from the first start on a fresh file. Only deleting the
  file (or giving the pod a new volume) resets it; a restart no longer does. The scoped windows (per
  cluster, per session, per identity) come with the M3 reservation. Superseded in part by question 1033
  below: `guard-proxy reset-spend` also resets it.
- **The conformance suite gives every proxy start its own `SPEND_STORE_PATH`** under the test's
  `tmp_path`, so no test inherits another's spend; `proxy.py` ignores it. The restart test is Go's
  (`cmd/guard-proxy`'s `TestSpendSurvivesAKillMidWindow`), since `proxy.py` has no store and the default
  test command runs the suite against it.

## 2026-11-01: the /chat `witch` name rides the A2A message as a second text part

The storefront now assigns each visitor a witch name and sends it as `witch` with every `/chat` POST
(guard-proxy-agenticburn#34, from Unleash_an_Agent_Watch_It_Burn#762). Challenge 3 (HC-666) asks the
agent for a Deployment named `joker-` plus the name the person chatting gave, so the agent must see it.

- **A second text part, not message metadata.** kagent v0.10.1's Go runtime (`runtime: go`, which the
  workshop Agent sets) converts the inbound message with `messageToGenAIContent` in
  `go/adk/pkg/a2a/converter.go`, which hands every text part to the model; message metadata goes only to
  telemetry (`SetMessageMetadataAttributes` in `executor.go`). Read from the v0.10.1 source on
  2026-11-01. Metadata would never reach the model.
- **After the prompt.** `parts[0]` stays the prompt as typed, so anything reading the first part sees
  what it saw before. The part reads `The person chatting with you gave their name as <name>.`, echoing
  HC-666's wording so the agent ties the two together; the agent still forms `joker-<slug>` itself.
- **Outside every guard.** The block-list, the injection classifier, the topic guard, the prompt feed and
  the span attributes see only the prompt, as the issue requires. The name is trimmed; a blank or
  non-string `witch` is no name and the message is exactly the prompt, as before.
- **Trade-off, left to the workshop.** The field is client-supplied, so anyone who POSTs `/chat` directly
  can put any text in it and it reaches the model unscanned. No shape or length check is added because
  the issue asks for none and the vocabulary belongs to the desk; a follow-up proposes one. Superseded by
  question 1036 below: a name not shaped `<First> the <Creature>` is now no name.

## 2026-11-01: question 1036, /chat forwards only a storefront-shaped witch name

**Question 1036:** should `/chat` forward the client-supplied `witch` name to the agent only when it looks
like a storefront name, such as `Mortimer the Witch`, and treat anything else as no name
(guard-proxy-agenticburn#35)?

**Answer:** accept only a storefront-shaped name. Decided by hecaton's research under Michael's standing
rules, not by Michael. The research cited the workshop's own record: every bypass of a guarded path it has
met (the console's route to the unauthenticated `/toggle`, pods skipping the model gateway, the
`/brief.html` side door) was closed rather than kept as a stage beat (Unleash_an_Agent_Watch_It_Burn
decisions.md, #333, #481, #490, #511).

What it changes:

- **Both builds forward `witch` only when, trimmed, it is `<First> the <Creature>`**: each word one
  capital ASCII letter then 1 to 15 lowercase ones (`[A-Z][a-z]{1,15} the [A-Z][a-z]{1,15}`, whole
  string). Anything else is no name and the message is exactly the prompt, so a direct POST can no longer
  carry an injection past the guards in this field.
- **The 16-letter bound per word is the length cap.** The bare pattern `[A-Z][a-z]+` admits a word of any
  length; the desk's longest first name is 10 letters and its longest creature 8, so 16 leaves room for
  new ones without letting the field carry a paragraph. All 768 desk names (32 first names times 24
  creatures, `gitops/ai-layer/ticket_site.py`) pass.
- **No vocabulary copy.** guard-proxy checks the shape, not the desk's list, so adding a capitalized
  one-word first name or creature at the desk needs no change here. A name with a hyphen, accent or space
  would be dropped until the pattern is widened.
- **A dropped name logs `witch_name_refused`** at INFO with its length, not its text, so a desk name the
  pattern misses shows in the logs instead of silently losing HC-666's `joker-<slug>`.
- **Accepted residue:** a short injection that fits two capitalized words, such as `Ignore the Rules`,
  still passes; it carries very little payload.
- Conformance in `test_chat_witch.py`; Go's `TestWitchNameRE`; two mutants in `mutation_check.py`.

## 2026-11-01: question 1033, an instructor resets the spend tally with `guard-proxy reset-spend`

**Question 1033:** how should an instructor reset the Go guard-proxy's spend tally to $0 before a new
workshop delivery, now that the tally lives in a bbolt file a restart no longer clears
(guard-proxy-agenticburn#33, discovered from #32)?

**Answer:** a `reset-spend` subcommand in the binary. Decided by hecaton's research under Michael's
standing rules, not by Michael. The research cited PRD 412 (`prds/412-guard-proxy-go-rewrite.md` in
Unleash_an_Agent_Watch_It_Burn): decision 4 says code reaches a cluster only as a signed, digest-pinned
image, which rules out a delete-file Job (the static image has no shell or `rm`, so it would need a second
signed image); decision 2 says a restart must not reset a budget, not that an explicit offline reset is
barred. The runner-up, a per-delivery `SPEND_STORE_PATH`, needs no code but fails quietly when forgotten.

What it changes:

- **`guard-proxy reset-spend --store PATH`** clears the cluster tally, every session's spend and the
  runtime cost cap in one bbolt commit, keeps the file, and prints what it cleared (spend, requests,
  tokens, sessions and the runtime cap, or that there was none). The next start runs from $0 under
  `COST_CAP_USD`, as a `proxy.py` restart did.
- **It clears the runtime cost cap too.** A delivery starts at the configured backstop, as it did when a
  restart cleared memory; the printed line names the cap it dropped, which the Job option could not.
- **It refuses a store the proxy holds.** It opens the file the way the proxy does, with the 2 s lock wait,
  so a live pod's store fails with `held by another process` and nothing is cleared. The instructor scales
  the Deployment to 0, runs one pod from the same image with the volume mounted, then scales back to 1.
- **It refuses a path with no store** (`no spend store at PATH`) rather than creating one, so a mistyped
  path cannot report a clean reset while the real tally stays.
- **`--store` is required and is the only flag;** it does not read the config or `SPEND_STORE_PATH`, so
  the file it clears is the one named on the command line.
- **Still no HTTP reset.** No endpoint reaches it: `/toggle` has no auth and the agent's tool pod can reach
  the proxy, so a prompt-injected agent cannot lift either cap.
- **The workshop wraps the steps** (scale to 0, one-off pod, scale to 1) in one script or Job manifest at
  the Go cutover, Unleash_an_Agent_Watch_It_Burn#738.
- Tests: `TestResetClearsSpendSessionsAndTheRuntimeCostCap` in `internal/spend`, and the built binary's
  `TestResetSpendStartsADeliveryFromZero` and `TestResetSpendRefusals` in `cmd/guard-proxy`.

## 2026-11-01: The span tests read a fake OTLP collector, not a span file

The span tests (now `test_chat_spans.py` and `test_telemetry.py`, both marked `spans`) start a fake
OTLP/HTTP collector and point the implementation at it with `OTEL_EXPORTER_OTLP_ENDPOINT`. This replaces
`CONFORMANCE_SPANS`, which had `launch_python.py` write spans to a file in the Python SDK's console
format. A file exporter would have needed a test-only hook in the Go binary; OTLP is what both
implementations export in production, so the suite now tests that path for both, black-box.

`launch_python.py` installs the SDK's tracer and meter providers with the OTLP/HTTP exporters when the
endpoint is set, as the OTel Operator does in the workshop. That adds
`opentelemetry-exporter-otlp-proto-http==1.45.1` as a dev dependency, matching the SDK pin; it brings
`opentelemetry-proto`, which the collector decodes with. 1.45.2 (2026-10-29) was two days old.

Found while writing the tests: proxy.py extracts the inbound trace context from `dict(self.headers)`,
whose keys keep the case the client sent, so a capitalized `Traceparent` is not joined. agentgateway
sends it lowercase, so the workshop is unaffected; the tests send it lowercase. The Go build reads
headers case-insensitively.

## 2026-11-01: M5 exports OTLP/HTTP only, and names attributes from the workshop registry

The Go build exports traces and metrics with the OTLP/HTTP exporters at otel-go v1.47.0 (2026-10-02,
proxy.golang.org), and refuses gRPC or any other exporter at startup rather than ignoring it. The
workshop's Python proxy exports over HTTP, and `autoexport` v0.72.0 would bring gRPC, Prometheus and
the log exporters into the static image M6 builds.

Export is on only when an OTLP endpoint or `OTEL_TRACES_EXPORTER=otlp` is set, which is what the OTel
Operator's `inject-sdk` annotation provides; otherwise it is off, as proxy.py is without the SDK, and
the startup log says so. The W3C propagators are installed either way.

Attribute names are generated by weaver from the workshop's `weaver/registry`, the contract the
workshop's `weaver live-check` holds proxy.py to, so both implementations answer to one file. The
generated file is committed with the workshop commit it came from; CI does not run weaver, because the
registry lives in another repo. Three names the registry lacks are hand-written until it adds them.

## 2026-11-02: each request is priced at the model the workshop-agent is bound to

guard-proxy-agenticburn#37, from Unleash_an_Agent_Watch_It_Burn#130's design note: the workshop swaps the
model live by patching the `workshop-agent` Agent's `spec.declarative.modelConfig`, which Argo CD leaves
out of its diff, and the counter priced the swapped model at the `MODEL_TIER` read at start. Both builds
now read the Agent, with the token and the `guard-proxy-reader` get grant `/controls` already uses, and
price each request at the tier its ModelConfig names. Choices the issue left open:

- **The read is cached for 5 seconds**, as the badges are. A read per request would put one API call on
  every model call and every `/cost` poll of a room; a swap restarts the agent's pod, which takes longer
  than 5 seconds, so the window is not visible to a student. Spend already metered keeps its price.
- **Only `bedrock-<tier>` names a tier**, for the four rows of the price table. Any other ModelConfig
  (`gateway-bedrock-nova`, `gateway-local-qwen3`, a missing field) prices at `MODEL_TIER`, as the issue
  says, and is logged once as the warning `price_tier_unknown` so the wrong price is visible.
- **A failed read keeps the last tier read.** An API blip says nothing about the binding, and returning
  to `MODEL_TIER` would price a swapped cluster at the old model for the length of the blip. Until the
  first successful read, `MODEL_TIER` prices.
- **An explicit `COST_PER_1K_IN` or `COST_PER_1K_OUT` stays fixed** whatever the binding, as it already
  overrode `MODEL_TIER`.
- **`/cost`'s `tier` reports the tier in effect**, so the counter and the price agree.
- **Out of scope:** `gen_ai.request.model` on the spans and the cost gauge still follows `MODEL_TIER` or
  `MODEL_NAME`; filed as a follow-up, guard-proxy-agenticburn#38 (below). The workshop's submodule bump and its `verify/model_pin_check.py`
  extension are in Unleash_an_Agent_Watch_It_Burn, outside this repo.

## 2026-11-02: gen_ai.request.model follows the binding, and the cost gauge has one series per model

guard-proxy-agenticburn#38, from #37: after a live swap from `bedrock-nova` to `bedrock-opus`, Datadog
attributed Opus calls to `us.amazon.nova-pro-v1:0`. Both builds now report, as `gen_ai.request.model`,
the model ID of the tier each request is priced at (`_TIER_TO_MODEL_ID` / `tierModelIDs`), and an
explicit `MODEL_NAME` still wins. Choices the issue left open:

- **One series per model on `gen_ai.client.cost`**, the issue's own example. Each series is the spend
  metered as that model, so the series sum to `/cost`'s `usd`. The alternative, one cumulative series
  tagged with the model bound now, would keep `max:` queries working but credit Opus with every dollar
  Nova spent before the swap, which is the misattribution the issue reports, reversed. Cost: the
  workshop's leaderboard and fleet dashboard read a cluster's bill as `max:... by {kube_cluster_name}`,
  which would show only the biggest model's share after a swap; they must move to `sum:` with the
  submodule repin, filed on Unleash_an_Agent_Watch_It_Burn and called out in the release notes.
- **The bound model always has a series**, at 0 until it spends. With one series an idle cluster read
  $0; without this it would have no series at all and an empty leaderboard cell. The gauge callback reads
  the binding through the same 5-second cache, so it adds at most one Agent read per export interval.
- **A request's spans name the model as the request starts.** The `/chat` root and `chat` spans take it
  once per turn; each attempt is still priced, and added to its model's series, at the tier it was
  metered at, so a swap landing mid-turn moves the spend correctly even if the span names the earlier
  model.
- **A ModelConfig with no tier reports `MODEL_TIER`'s model**, as it is priced at `MODEL_TIER`; the
  `price_tier_unknown` warning already flags it. Reporting the ModelConfig name would put a model on the
  span that the price does not match.
- **The Go store keeps the split** under a `models` key beside `totals`, written in the same commit. A
  store with spend but no split was written before this change, and no model can be named for that spend,
  so `NewMeter` refuses it with a message naming `guard-proxy reset-spend` rather than reporting series
  that do not add up. The Go build is not deployed yet (the cutover is Unleash_an_Agent_Watch_It_Burn#738),
  so no live store hits this. `proxy.py` keeps the split in memory, as it does the tally.

## 2026-11-02: tests that read GitHub live in e2e/, run only by the E2E command

guard-proxy-agenticburn#41 asked for a test that fails when no comment on #39 names the cause of its hung
mutation-check wait. That fact lives on GitHub, so the test reads it with `gh issue view` and needs `gh`
and the network. It sits in `e2e/`, outside `testpaths`, so the Test command and CI, which must pass
everywhere, never collect it; the E2E command runs `pytest e2e` after the conformance suite and fails
with a named message when `gh` is missing or the call fails. "Names the cause" means one comment holds
all of "by process name", "pgrep -f mutation_check.py" and "own command line", the run report's wording.
