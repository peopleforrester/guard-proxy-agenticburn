Test command: `uv run --locked ruff check conformance && uv run --locked pytest`
E2E command: `! gofmt -l . | grep . && go vet ./... && go test ./... && go build -o bin/guard-proxy ./cmd/guard-proxy && GUARD_PROXY_CMD="$PWD/bin/guard-proxy --listen 127.0.0.1:{port}" uv run --locked pytest -m "passthrough or guards"`
