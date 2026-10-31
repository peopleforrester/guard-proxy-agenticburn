# guard-proxy-agenticburn

A platform-injected guard proxy for AI agents. It sits between an agent and its model endpoint and
enforces input scanning, output scanning, a hard USD spend cap and fail-closed behavior, for every
workload on the cluster, whether or not the developer who wrote the agent asked for any of it.

Built for the [Unleash an Agent, Watch It Burn](https://github.com/peopleforrester/Unleash_an_Agent_Watch_It_Burn)
workshop, where it is the control attendees switch on while an agent is being attacked.

## Status: being rewritten in Go

`proxy.py` is the **reference implementation**: 1,295 lines of Python with no third-party
dependencies, tuned against live attacker behavior across two workshop deliveries. It is what runs in
the workshop today.

It is being rewritten in Go, because it is expected to become a product and Go's libraries suit a
network proxy better than Python's `http.server`. The rewrite also folds in what LiteLLM does better.
The main example is LiteLLM's budget reservation, which estimates a request's maximum cost and rejects
it **before** the model is called. `proxy.py` meters cost after the response arrives, so it can only
block the *next* request, and so can agentgateway's budgets. Tracked in
[Unleash_an_Agent_Watch_It_Burn#412](https://github.com/peopleforrester/Unleash_an_Agent_Watch_It_Burn/issues/412).

### The Go implementation so far

| Milestone | State |
|---|---|
| M0 conformance suite | done |
| M1 skeleton: listener, agent forwarding, config validation, health | done: passes the `passthrough` conformance subset |
| M2 guards on the A2A path: block-list, LLM Guard classifier, output guard, fail closed | done: passes the `guards` conformance subset |
| M3 budget reservation: count tokens and cap `max_tokens` before the model is called | not started; needs a design that puts the proxy on the model path (below) |
| M4 rate limits, routing, toggles, the prompt feed | done as `proxy.py` does them: passes the whole suite except its span tests |
| M5 telemetry: OpenTelemetry spans | not started; the `spans` conformance tests are deselected for Go |
| M6 signed image, M7 live validation | not started |

M3 as the PRD writes it is not a port. guard-proxy sits in front of the agent (`AGENT_URL`, kagent),
not in front of Bedrock, and reads a turn's token usage from the agent's reply after the fact, so it
never holds a model request it could count or cap. The Go build therefore meters spend after each
reply, exactly as `proxy.py` does, and the reservation waits on a design for that hop.

```
go test ./...                                   # unit tests, plus an end-to-end test of the binary
go build -o bin/guard-proxy ./cmd/guard-proxy
AGENT_URL=http://localhost:3000 LLM_GUARD_URL=http://localhost:8000 bin/guard-proxy --listen 127.0.0.1:8080 [--config guard-proxy.yaml]
```

It serves every endpoint `proxy.py` does, with the same guards, limits, caps, toggles and prompt feed
on both paths, and passes the conformance suite except the span tests. It emits no OpenTelemetry spans
yet (M5), which the workshop's Datadog views need, so `proxy.py` stays what the workshop deploys until
the cutover after KubeCon (2026-11-12).

Where it differs from `proxy.py` on purpose:

- A scanner failure logs a `guard_error` event at WARNING before the request is refused (or, with
  fail-closed off, allowed). `proxy.py` fails silently.
- `PROXY_FAIL_CLOSED` accepts only `true` or `false`; anything else stops startup. `proxy.py` reads
  any value but `true` as false, so a typo turns fail-closed off.
- With no `LLM_GUARD_TOKEN`, no `Authorization` header is sent, rather than an empty bearer token.
- A request body of an unexpected shape carries no text, so no input guard applies to it, and an
  unexpected reply shape is passed through. `proxy.py` raises on both and drops the connection.
- A reply the output guard leaves alone is returned byte for byte. One it changes is re-encoded, with
  keys in sorted order.
- A scanner failure on the topic classifier or on `/chat` logs `guard_error` too.
- A spend setting `proxy.py` would crash on or silently switch off, such as `COST_CAP_USD=-1`,
  `RATE_LIMIT_RPM=1.5` or `COST_PER_1K_IN=` (empty), stops startup. An empty cap or limit still means
  its default, as in `proxy.py`.
- Whole-word matching in the profanity list and the topic phrases uses RE2's ASCII `\b`, where Python's
  is Unicode-aware. A term next to a non-ASCII letter ("fuckñ") is masked in Go and not in `proxy.py`.

Config is one YAML file, decoded strictly: an unknown key stops the process at start.

```yaml
listen: ":8080"            # default; --listen overrides it
agent:
  url: http://agentgateway.agent.svc.cluster.local:3000   # required
  timeout: 150s            # default; bounds the whole upstream exchange
llm_guard:
  url: http://llm-guard.agent.svc.cluster.local:8000      # required
  timeout: 150s            # default; the token comes only from LLM_GUARD_TOKEN
chat:
  turn_timeout: 0s         # default, no turn deadline; bounds every upstream call of a /chat turn
blocklist: [delete, "rm -rf"]   # replaces the default list; terms are trimmed and lowercased
fail_closed: true          # default; refuse when LLM Guard gives no verdict
guards:                    # the state each guard starts in
  input_blocklist: false
  input_classifier: false
  output: false
  budget: false
  topic: false
spend:
  cost_cap_usd: 0          # default, off; the cluster-wide backstop
  budget_cap_usd: 0.03     # default; the per-session cap the budget guard enforces
  rate_limit_rpm: 0        # default, off; model-bound requests per minute, both paths
  rate_limit_session_rpm: 0   # default, off; per /chat conversation, checked first
  model_tier: haiku        # default; nova, haiku, sonnet or opus prices the tally
  # model_name, cost_per_1k_in and cost_per_1k_out default from the tier
feed:
  enabled: false           # default; GET /prompts records nothing until this is on
  profanity: [cilantro]    # replaces the default list; whole words, plural included
topic:
  off: [karaoke, hot dog]  # refuses without a model call
  on: [menu, burrito]      # passes without a model call; anything else goes to BanTopics
  refusal: "BurritoBot only talks burritos: the menu, our hours, and our locations."
cors:
  console_origins: [https://start.agenticburn.com, https://start.burritobot.co]   # default
controls:                  # the in-cluster defaults; override to point at another API server
  api_url: https://kubernetes.default.svc
  token_path: /var/run/secrets/kubernetes.io/serviceaccount/token
  ca_path: /var/run/secrets/kubernetes.io/serviceaccount/ca.crt
  public_host: ""          # reported by /controls as "host"
```

Every environment variable `proxy.py` reads (listed under [Configuration](#configuration)) overrides
the file, with the same meaning, except the two OpenTelemetry ones, `PEER_SERVICE` and
`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`, which wait for M5. The guard switches and
`STREAM_PROMPTS` are on only for the value `on`, as in `proxy.py`. Read [docs/gotchas-go.md](docs/gotchas-go.md)
before changing the upstream hop or adding a dependency.

## What it does

| Function | How |
|---|---|
| Input scanning | Calls an LLM Guard API server running a prompt-injection classifier, plus a local blocklist |
| Output scanning | Regex over the completion for planted secret patterns; blocks and redacts |
| Spend cap | Per-session and per-cluster USD caps, metered from token usage |
| Rate limiting | Requests per minute |
| Model tier | Routes to a cheaper or more expensive model by config |
| Fail closed | If the scanner is unreachable, refuse rather than pass through |
| Telemetry | OpenTelemetry spans with gen_ai attributes, including message content when enabled |
| Live prompt feed | Streams prompts to a feed the room watches during the workshop |

The last row is workshop apparatus rather than a product feature, and nothing else in the ecosystem
provides it.

## Configuration

Everything is set through environment variables:

```
AGENT_URL  BLOCK_LIST  BUDGET_CAP_USD  BUDGET_GUARD  CONSOLE_ORIGIN  COST_CAP_USD  COST_PER_1K_IN
COST_PER_1K_OUT  INPUT_BLOCKLIST  INPUT_CLASSIFIER  INPUT_GUARD  K8S_API_URL  K8S_TOKEN_PATH
LLM_GUARD_TOKEN  LLM_GUARD_URL  MODEL_NAME  MODEL_TIER
OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT  OUTPUT_GUARD  PEER_SERVICE  PROFANITY_LIST
PROXY_FAIL_CLOSED  PROXY_TIMEOUT  RATE_LIMIT_RPM  RATE_LIMIT_SESSION_RPM  STREAM_PROMPTS
TOPIC_GUARD  TOPIC_OFF_LIST  TOPIC_ON_LIST  TOPIC_REFUSAL  TURN_TIMEOUT  WIB_PUBLIC_HOST
```

`PROXY_TIMEOUT` bounds each upstream call. A `POST /chat` turn can make up to six in a row: the
injection classifier, the topic classifier, the forward, the two self-heal retries and the output
scrub. `TURN_TIMEOUT` (seconds, default `0` for none) is one deadline for the whole turn: each call
gets `PROXY_TIMEOUT` or what is left of the turn, whichever is less, and a turn that runs out
answers the same 502 as a failed forward. A guard call the deadline cuts off ends the turn with
that 502 whatever `PROXY_FAIL_CLOSED` says, so fail-open never forwards an unscanned prompt or
returns an unscrubbed reply. The thinking-only retry is optional, so running out of time on it keeps
the first reply, as its failure does. The A2A path is not bounded by it.

The `/chat` forward is an A2A task (#27). It is sent as a non-blocking `message/send`, which answers
with the task's id, and polled with `tasks/get` until the task leaves `submitted` and `working`.
`PROXY_TIMEOUT` bounds the whole forward, every poll included. When the forward fails or runs out of
time with a task still running, the turn answers its 502 and guard-proxy then sends the agent
`tasks/cancel` for that task, logging `task_cancel`, or `task_cancel_failed` when the agent refuses or
cannot be reached. kagent runs a turn detached from the request, so without the cancel a turn nobody
is waiting for keeps calling tools and the model.

`COST_CAP_USD` (default `0` for none) is the cluster-wide backstop on spend. `GET
/toggle?cost_cap=2.00` lowers it at runtime, keeping the tally an env change would reset, and
`/toggle?cost_cap=default` restores the env value. A runtime cap must be above 0 and no higher than
`COST_CAP_USD` while that is set; anything else answers 400 and changes nothing. `/toggle` reports
the cap in effect as `cost_cap_usd`, and `/cost`'s `cap_usd` counts it. When it trips, `/chat` says the
whole kitchen is closed and stamps `witb.guard.decision` as `cost_cap`, since Reset cannot lift it; the
per-session budget cap's reply offers Reset and stamps `budget`. The cluster-wide cap answers first when
both have tripped.

Several overlap (`BUDGET_CAP_USD` and `COST_CAP_USD`, `INPUT_GUARD` and `INPUT_CLASSIFIER`). Some of
that is history and some is a live toggle the workshop flips. Rationalizing them is part of the
rewrite.

## Endpoints

| Path | Purpose |
|---|---|
| `POST /chat` | The proxied agent call, with every guard applied |
| `GET /cost` | Current spend against the active cap |
| `GET /prompts` | The live prompt feed |
| `GET /guards` | Which guards are on |
| `GET /controls` | The state of each workshop challenge's control |
| `GET /toggle` | Switches a guard, or sets the cost cap, live without a restart |

## Conformance suite

`conformance/` is a black-box HTTP test suite. It is the contract the Go rewrite has to meet. It
starts the implementation as a subprocess and points it at real local servers that stand in for the
agent and the LLM Guard API. It then checks only what a client could observe: status codes, response
bodies, headers, what reached each upstream, and the structured log events. No test imports the
code under test.

```
uv run pytest                                          # against proxy.py
GUARD_PROXY_CMD="$PWD/bin/guard-proxy --listen 127.0.0.1:{port}" uv run pytest -m "not spans"   # against the Go build
uv run python conformance/mutation_check.py           # negative control, see below
```

`GUARD_PROXY_CMD` is the only adapter. `{port}` is replaced with a free loopback port, and the suite
waits for `GET /guards` to answer.

`-m "not spans"` leaves out the span tests (`test_chat_spans.py`), because the Go build emits no
OpenTelemetry spans until M5. It runs everything else. The `passthrough` and `guards` markers still
select the M1 and M2 subsets. `proxy.py` always runs all of it.

The suite covers:

- every endpoint;
- both input stages, the output guard, and fail-closed against an unreachable, erroring, malformed or
  slow scanner;
- the rate limit, the cluster-wide cost cap and the per-session budget cap, and the order they run in;
- token metering and tier pricing;
- the self-heal retries;
- the `/chat` task poll and its `tasks/cancel` on a failed or timed-out forward;
- the moderated prompt feed;
- response CORS.

`mutation_check.py` runs the suite against deliberately broken copies of `proxy.py`, and fails
unless every one is caught. That is how we know the suite pins real behavior and does not just
pass.

**Not covered yet.**

- **Telemetry (spans and the cost metric).** It needs the OpenTelemetry SDK that the cluster
  injects, and it arrives with the Go rewrite's telemetry milestone, against a recorded baseline from
  `proxy.py`.
- **`/controls` against a cluster API, for C1, C3 KubeArmor and C8.** `K8S_API_URL` and
  `K8S_TOKEN_PATH` point `proxy.py` at a fake API server, and the C2 Kyverno ValidatingPolicy, C3
  Falco DaemonSet and C7 badges are tested that way. The C1 NetworkPolicy, C3 KubeArmor and C8
  `rbac_scoped` badges are tested only in the no-credentials case.

**Known defects are pinned, not hidden.** A test whose name ends in `_today` asserts current behavior
that is wrong, and names the issue that tracks it (#1 through #4). Fixing one changes `proxy.py` and
that test together.

## How the workshop consumes it

The workshop repository includes this one as a git submodule at `gitops/ai-layer/guard-proxy/`. A
kustomize `configMapGenerator` turns `proxy.py` into a ConfigMap, which is mounted into a stock Python
image. That delivery is what lets a code change reach a live cluster in seconds during the workshop.
[docs/release-notes.md](docs/release-notes.md) names the commit to repin the submodule to for each change.
The Go rewrite replaces it with a signed, digest-pinned image.

## Related repos

| Repo | What it provides |
|---|---|
| [Unleash_an_Agent_Watch_It_Burn](https://github.com/peopleforrester/Unleash_an_Agent_Watch_It_Burn) | The workshop. It deploys this proxy from its submodule and is where most requests for it start. |
| [llm-guard-cpu-offline](https://github.com/peopleforrester/llm-guard-cpu-offline) | The LLM Guard scanner image the proxy calls at `LLM_GUARD_URL`, `ghcr.io/peopleforrester/watch-it-burn:llm-guard-0.3.16-cpu.3`. Its classifier is baked in, so a fail-closed proxy behind default-deny egress does not refuse every request. |
| `peopleforrester/llm-guard-upstream-mirror` (private) | A backup of the archived `protectai/llm-guard` source the scanner image is built from. |

## History

This repository's history was carried over from the workshop repository with `git filter-repo`, so
`git log` explains the decisions that shaped the proxy. `proxy.py` started life at
`agent/gateway/guard-proxy/proxy.py`, which later became a symlink when the file moved into the GitOps
tree. That earlier path appears in history as `history/agent-gateway-proxy.py`, and is removed in the
first standalone commit.

## License

The Go implementation is Apache-2.0; see `LICENSE`. `proxy.py` and the conformance suite in
`conformance/` remain under the MIT license they were written under; see `LICENSE-MIT`.
