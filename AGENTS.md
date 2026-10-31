Test command: `uv run --locked ruff check conformance && uv run --locked pytest`
E2E command: `! gofmt -l . | grep . && go vet ./... && go test ./... && go build -o bin/guard-proxy ./cmd/guard-proxy && GUARD_PROXY_CMD="$PWD/bin/guard-proxy --listen 127.0.0.1:{port}" uv run --locked pytest -m "not spans"`

Related repos: `peopleforrester/Unleash_an_Agent_Watch_It_Burn` (the workshop that deploys this proxy),
`peopleforrester/llm-guard-cpu-offline` (the scanner image at `LLM_GUARD_URL`) and
`peopleforrester/llm-guard-upstream-mirror` (private backup of the archived LLM Guard source).
See the README's Related repos section.
