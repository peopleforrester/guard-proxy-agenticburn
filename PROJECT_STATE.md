# Project State: guard-proxy-agenticburn

Phase: 3.1 Stage
Approved: pending

## Lifecycle
- [x] 1.1 Research
- [x] 1.2 Plan
- [x] 1.3 Approve
- [x] 2.1 Test
- [x] 2.2 Implement
- [x] 2.3 Verify
- [x] 3.1 Stage
- [x] 3.2 Confirm CI
- [x] 3.3 Promote

## Contracts
_(none yet. Approved plans land here with ts + sha256.)_

## Current Plan
The last unit of work (`000f57e`, per-conversation `/chat` rate limit) is on `main` with green CI.

Agent-task issues worked 2026-10-29 (all closed):
- [x] #18 Link the sibling repos llm-guard-cpu-offline and llm-guard-upstream-mirror
- [x] #19 Allow the instructor console origin on start.agenticburn.com and start.burritobot.co
- [x] #17 Name the scanner in the classifier refusal
- [x] #20 Redact personal data on the prompt feed; match code-of-conduct terms as whole words
- [x] #21 One TURN_TIMEOUT deadline per /chat turn (on main at 92b0587)
- [x] #23 /toggle?cost_cap=N sets the cluster-wide cost cap at runtime (KubeCon finale) (on main at 92b0587)
- [x] #25 The cluster-wide cost cap answers "the kitchen is closed", not "press Reset"; decision `cost_cap` (on main at 92b0587)

Go rewrite: M0 to M2 done (passthrough and guards conformance subsets).

### Active: port /chat and the admin endpoints to Go, as proxy.py does them (approved 2026-10-31)
Michael chose "port /chat as-is": spend metered after each response, exactly as proxy.py, until the
full conformance suite passes against the Go build. Closes #22. The PRD M3 reservation (CountTokens,
capped max_tokens) is out of scope: it needs the proxy on the model path. Baseline 2026-10-31: 271
conformance tests fail against Go. Units, each committed green:
- [x] U1 spend: cost metering, rate limits (cluster and per session), cost cap and budget cap, /cost
- [x] U2 /toggle and /guards, the runtime cost cap
- [x] U3 POST /chat: validation, A2A wrapping, reply extraction, retries, TURN_TIMEOUT (#22)
- [x] U4 the on-topic guard on both paths
- [x] U5 the prompt feed: GET /prompts, moderate(), probes
- [x] U6 GET /controls badges (Kubernetes API reads)
- [x] U7 logging parity, then the full suite green except test_chat_spans.py
- [ ] U8 decide with Michael: test_chat_spans.py needs an OpenTelemetry SDK in Go (PRD M5); marked `spans`, deselected for Go
- [x] README milestone table, config and conformance sections; docs/gotchas-go.md RE2 section
- [ ] File the PRD #412 amendment on Unleash (M3 needs the model path; M4 done as proxy.py)

## Branch & Tests
- Branch: staging
- Working tree: clean
- Last CI: green; sha 47ac08b (staging and main)

## Phase History
_(append-only. Each phase transition adds one line, oldest first.)_
- 2026-10-29T13:43:39Z init-state initialized at 3.3 (last unit 000f57e already promoted)
- 2026-10-29 #18, #19, #17, #20 implemented and verified locally (2.3); staging push next
- 2026-10-29 3.3 #17 to #20 promoted to main at 47ac08b; mutation check 28/28 caught
- 2026-10-29 #21 implemented and verified locally (2.3) on agent/21-11185
- 2026-10-30 #23 implemented and verified locally (2.3) on agent/23-12049
- 2026-10-30 #25 implemented and verified locally (2.3) on agent/25-12984
- 2026-10-31 #21, #23, #25 promoted to main at 92b0587
- 2026-10-31 1.3 Go /chat port approved by Michael (port as-is); 2.1 starting U1
- 2026-10-31 2.3 Go port verified: 305/323 conformance pass against Go (18 spans deselected), proxy.py 323/323, go test ./... green; 3.1 a00beae pushed
