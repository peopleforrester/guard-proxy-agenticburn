Test command: `uv run --locked ruff check conformance && uv run --locked pytest`
E2E command: `! gofmt -l . | grep . && go vet ./... && go test ./... && go build -o bin/guard-proxy ./cmd/guard-proxy && GUARD_PROXY_CMD="$PWD/bin/guard-proxy --listen 127.0.0.1:{port}" uv run --locked pytest`

Mutation check: `uv run --locked python conformance/mutation_check.py`. It runs the whole suite once
per mutant, four at a time (`MUTATION_JOBS`), and prints `[n/total] caught|ESCAPED name (~Ns left)` as
each finishes, then `all N mutants caught` and exit 0. The Test command alone can pass 10 minutes, so
both outlast a 600 s foreground limit. Start a long run in the background with its output to a log,
`mkdir -p .mutants && { uv run --locked python conformance/mutation_check.py > .mutants/run.log 2>&1; echo "exit $?" >> .mutants/run.log; }`,
and wait for the `exit` line in that log, or on the PID with `wait` or `kill -0`. Never wait by
process name: `pgrep -f mutation_check.py` and `ps aux | grep mutation_check` match the waiting
loop's own command line, and the agent's own process when its prompt names the script, so the loop
never ends. A background-task monitor may be denied; the log's `exit` line works without one.

Related repos: `peopleforrester/Unleash_an_Agent_Watch_It_Burn` (the workshop that deploys this proxy),
`peopleforrester/llm-guard-cpu-offline` (the scanner image at `LLM_GUARD_URL`) and
`peopleforrester/llm-guard-upstream-mirror` (private backup of the archived LLM Guard source).
See the README's Related repos section.
