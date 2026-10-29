# Project State: guard-proxy-agenticburn

Phase: 3.3 Promote
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
- [ ] #21 One TURN_TIMEOUT deadline per /chat turn: implemented, awaiting promotion (Go reads and validates it; applied when Go /chat lands)
- [ ] #23 /toggle?cost_cap=N sets the cluster-wide cost cap at runtime (KubeCon finale): implemented, awaiting promotion (proxy.py only)

Go rewrite: M0 to M2 done (passthrough and guards conformance subsets). M3 to M7 not started.
Since M2, `/chat`, the topic guard, `/controls`, `/cost`, `/prompts` and `/toggle` have landed
in `proxy.py` only, so the Go build's parity gap is widening.

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
