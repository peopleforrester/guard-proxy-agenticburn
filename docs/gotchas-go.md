# Go gotchas for guard-proxy

Things that are easy to get wrong when working on the Go implementation, each with where it was
checked. Read this before adding a dependency or changing how the proxy talks to the agent.

## Toolchain

- **`go.mod` says `go 1.27.2`, a patch release, on purpose.** 1.27.2 (2026-10-08) carries security
  fixes to `net/http`, `net/textproto` and `crypto/tls`, per the
  [release history](https://go.dev/doc/devel/release) read on 2026-10-14. A machine with 1.27.1
  installed downloads 1.27.2 on first build (`GOTOOLCHAIN=auto`, the default), and `go version`
  then reports go1.27.2. That was observed on 2026-10-14 when 1.27.1 was the local install.

## YAML

- **The import is `go.yaml.in/yaml/v3`, not `gopkg.in/yaml.v3`.** The old `go-yaml/yaml` repository
  is archived. As of 2026-10-14 proxy.golang.org lists `go.yaml.in/yaml/v3` v3.0.5 (2026-07-26) as
  latest.
- **Unknown keys are silently ignored unless `Decoder.KnownFields(true)` is set.** For a guard proxy
  that is the dangerous default: `guards: {ouput: true}` would decode cleanly and leave the output
  guard off. `internal/config` sets it, and a test proves a misspelled nested key fails.
- **One `Decode` reads one document.** A file holding `---` and a second document decodes the first
  and ignores the rest. `internal/config` decodes a second time and refuses anything but `io.EOF`.

## The upstream hop

- **guard-proxy does not use `httputil.ReverseProxy`.** The reference proxy rebuilds the upstream
  request from the body alone and sends only `Content-Type: application/json`, so client
  credentials stop at the proxy. A reverse proxy copies headers by default. If a later milestone
  does adopt it, use `Rewrite`. `Director` is documented as insecure, because hop-by-hop removal
  runs after it and inbound `X-Forwarded-*` headers survive
  ([pkg.go.dev](https://pkg.go.dev/net/http/httputil#ReverseProxy), read 2026-10-14).
- **`http.Client.Timeout` is a total deadline.** It covers connecting, redirects and reading the whole
  body (`go doc net/http Client.Timeout`). Python's `urlopen(timeout=)` sets a socket timeout that
  applies to each blocking operation, so a slow trickle of bytes never trips it. `PROXY_TIMEOUT`
  therefore bounds the whole exchange in Go. That is stricter, and it is the intended reading.
- **The client follows redirects**, up to 10 (`go doc net/http Client.CheckRedirect`). urllib
  follows them too, so behavior matches.
- **gzip is transparent only when Go asked for it.** The transport adds `Accept-Encoding: gzip` and
  decodes the reply itself. Setting that header by hand turns the decoding off
  (`go doc net/http Transport.DisableCompression`).

## Logging

- **`log/slog`'s JSON handler writes `time`, `msg` and `WARN`.** The reference proxy writes
  `timestamp`, `message` and `WARNING`, and Datadog facets key on those. `internal/logging` renames
  them in `ReplaceAttr`. Timestamps are RFC 3339 in UTC, whereas the reference uses Python's
  local-time `formatTime`. Nothing parses the value positionally.

## Telemetry (for M5)

- **otel-go has no `gen_ai.*` semantic-convention constants.** Checked in PRD #412 against otel-go
  v1.47.0. The attribute names come from `weaver/registry/guard-proxy-spans.yaml` in the workshop
  repository, generated into Go rather than typed by hand.

## JSON

- **`encoding/json` decodes every number into `float64` by default**, which corrupts an integer past
  2^53, such as a large JSON-RPC id. The output guard decodes the agent's reply with
  `Decoder.UseNumber()`, and the request id is echoed as `json.RawMessage`, so neither is reparsed.
- **`json.Marshal` escapes `<`, `>` and `&` as `<` and friends.** That is valid JSON but
  changes text the agent wrote, so the output guard re-encodes with `Encoder.SetEscapeHTML(false)`.
  It also re-sorts object keys, which is why a reply the guard leaves alone is returned unchanged
  rather than re-encoded.
