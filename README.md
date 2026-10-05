# guard-proxy-agenticburn

A platform-injected guard proxy for AI agents. It sits between an agent and its model endpoint and
enforces input scanning, output scanning, a hard USD spend cap and fail-closed behavior, for every
workload on the cluster, whether or not the developer who wrote the agent asked for any of it.

Built for the [Unleash an Agent, Watch It Burn](https://github.com/peopleforrester/Unleash_an_Agent_Watch_It_Burn)
workshop, where it is the control attendees switch on while an agent is being attacked.

## Status: being rewritten in Go

`proxy.py` is the **reference implementation**: 1,076 lines of Python with no third-party
dependencies, tuned against live attacker behavior across two workshop deliveries. It is what runs in
the workshop today.

It is being rewritten in Go, because it is expected to become a product and Go's libraries suit a
network proxy better than Python's `http.server`. The rewrite also folds in what LiteLLM does better.
The main example is LiteLLM's budget reservation, which estimates a request's maximum cost and rejects
it **before** the model is called. `proxy.py` meters cost after the response arrives, so it can only
block the *next* request, and so can agentgateway's budgets. Tracked in
[Unleash_an_Agent_Watch_It_Burn#412](https://github.com/peopleforrester/Unleash_an_Agent_Watch_It_Burn/issues/412).

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
AGENT_URL  BUDGET_CAP_USD  BUDGET_GUARD  CONSOLE_ORIGIN  COST_CAP_USD  INPUT_BLOCKLIST
INPUT_CLASSIFIER  INPUT_GUARD  LLM_GUARD_TOKEN  LLM_GUARD_URL  MODEL_NAME  MODEL_TIER
OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT  OUTPUT_GUARD  PEER_SERVICE  PROFANITY_LIST
PROXY_FAIL_CLOSED  PROXY_TIMEOUT  RATE_LIMIT_RPM  STREAM_PROMPTS  WIB_PUBLIC_HOST
```

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
| `GET /toggle` | Switches a guard live, without a restart |

## How the workshop consumes it

The workshop repository includes this one as a git submodule at `gitops/ai-layer/guard-proxy/`. A
kustomize `configMapGenerator` turns `proxy.py` into a ConfigMap, which is mounted into a stock Python
image. That delivery is what lets a code change reach a live cluster in seconds during the workshop.
The Go rewrite replaces it with a signed, digest-pinned image.

## History

This repository's history was carried over from the workshop repository with `git filter-repo`, so
`git log` explains the decisions that shaped the proxy. `proxy.py` started life at
`agent/gateway/guard-proxy/proxy.py`, which later became a symlink when the file moved into the GitOps
tree. That earlier path appears in history as `history/agent-gateway-proxy.py`, and is removed in the
first standalone commit.

## License

MIT; see `LICENSE`.
