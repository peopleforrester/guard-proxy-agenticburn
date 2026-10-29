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

Open agent-task issues, worked in order:
- [ ] #18 Link the sibling repos llm-guard-cpu-offline and llm-guard-upstream-mirror
- [ ] #19 Allow the instructor console origin on start.agenticburn.com and start.burritobot.co
- [ ] #17 Name the scanner in the classifier refusal
- [ ] #20 Redact personal data on the prompt feed; match code-of-conduct terms as whole words

Go rewrite: M0 to M2 done (passthrough and guards conformance subsets). M3 to M7 not started.
Since M2, `/chat`, the topic guard, `/controls`, `/cost`, `/prompts` and `/toggle` have landed
in `proxy.py` only, so the Go build's parity gap is widening.

## Branch & Tests
- Branch: staging
- Working tree: clean before this file
- Last CI: green; sha 000f57e (staging and main)

## Phase History
_(append-only. Each phase transition adds one line, oldest first.)_
- 2026-10-29T13:43:39Z init-state initialized at 3.3 (last unit 000f57e already promoted)
