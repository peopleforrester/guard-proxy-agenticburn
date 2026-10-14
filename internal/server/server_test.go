// ABOUTME: Unit tests for the HTTP surface: routing, health, the upstream hop and its failure log. The
// ABOUTME: conformance suite covers the same surface black-box; these pin what it cannot see directly.
// SPDX-License-Identifier: Apache-2.0
package server

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/peopleforrester/guard-proxy-agenticburn/internal/config"
	"github.com/peopleforrester/guard-proxy-agenticburn/internal/logging"
)

type seen struct {
	method, uri string
	header      http.Header
	body        []byte
}

type fakeAgent struct {
	*httptest.Server
	mu    sync.Mutex
	calls []seen
}

func newAgent(t *testing.T, h http.HandlerFunc) *fakeAgent {
	t.Helper()
	a := &fakeAgent{}
	a.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		a.mu.Lock()
		a.calls = append(a.calls, seen{r.Method, r.RequestURI, r.Header.Clone(), b})
		a.mu.Unlock()
		h(w, r)
	}))
	t.Cleanup(a.Close)
	return a
}

func (a *fakeAgent) seen() []seen {
	a.mu.Lock()
	defer a.mu.Unlock()
	return append([]seen(nil), a.calls...)
}

func replyJSON(body string) http.HandlerFunc {
	return func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, body)
	}
}

func newProxy(t *testing.T, agentURL string, g config.Guards) (*Server, *bytes.Buffer) {
	t.Helper()
	var logs bytes.Buffer
	cfg := config.Config{
		Listen: "127.0.0.1:0",
		Agent:  config.Agent{URL: agentURL, Timeout: config.Duration{Duration: 2 * time.Second}},
		Guards: g,
	}
	return New(cfg, logging.New(&logs)), &logs
}

func do(s *Server, method, target string, body string, h map[string]string) *httptest.ResponseRecorder {
	r := httptest.NewRequest(method, target, strings.NewReader(body))
	for k, v := range h {
		r.Header.Set(k, v)
	}
	w := httptest.NewRecorder()
	s.ServeHTTP(w, r)
	return w
}

func TestHealthzAnswersWithoutTheAgent(t *testing.T) {
	a := newAgent(t, replyJSON(`{}`))
	s, _ := newProxy(t, a.URL, config.Guards{})
	w := do(s, http.MethodGet, "/healthz", "", nil)
	if w.Code != http.StatusOK || strings.TrimSpace(w.Body.String()) != `{"status":"ok"}` {
		t.Errorf("got %d %q", w.Code, w.Body.String())
	}
	if n := len(a.seen()); n != 0 {
		t.Errorf("health check reached the agent %d times", n)
	}
}

func TestGuardsReportsTheConfiguredState(t *testing.T) {
	s, _ := newProxy(t, "http://unused.invalid", config.Guards{Output: true})
	w := do(s, http.MethodGet, "/guards", "", nil)
	var got map[string]bool
	if err := json.Unmarshal(w.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	want := map[string]bool{"input_blocklist": false, "input_classifier": false, "output": true, "budget": false}
	if len(got) != len(want) {
		t.Fatalf("got %v", got)
	}
	for k, v := range want {
		if got[k] != v {
			t.Errorf("%s = %v, want %v", k, got[k], v)
		}
	}
}

func TestUnsupportedMethodsAre501(t *testing.T) {
	s, _ := newProxy(t, "http://unused.invalid", config.Guards{})
	for _, m := range []string{http.MethodHead, http.MethodPut, http.MethodDelete, http.MethodPatch, http.MethodOptions} {
		if w := do(s, m, "/chat", "", nil); w.Code != http.StatusNotImplemented {
			t.Errorf("%s = %d, want 501", m, w.Code)
		}
	}
}

// Routes that later milestones implement answer 501 until then, rather than being forwarded to the
// agent as if they were A2A traffic.
func TestUnbuiltRoutesNeverReachTheAgent(t *testing.T) {
	a := newAgent(t, replyJSON(`{}`))
	s, _ := newProxy(t, a.URL, config.Guards{})
	for _, c := range []struct{ method, path string }{
		{"GET", "/cost"}, {"GET", "/prompts"}, {"GET", "/controls"}, {"GET", "/toggle?output=on"},
		{"POST", "/chat"}, {"POST", "/chat/"},
	} {
		if w := do(s, c.method, c.path, `{"prompt":"hi"}`, nil); w.Code != http.StatusNotImplemented {
			t.Errorf("%s %s = %d, want 501", c.method, c.path, w.Code)
		}
	}
	if n := len(a.seen()); n != 0 {
		t.Errorf("agent was called %d times", n)
	}
}

func TestPostForwardsTheBodyAndOnlyAJSONContentType(t *testing.T) {
	a := newAgent(t, replyJSON(`{"jsonrpc":"2.0","id":1,"result":{}}`))
	s, _ := newProxy(t, a.URL, config.Guards{})
	w := do(s, http.MethodPost, "/api/a2a/agent/?q=1", `{"x":1}`, map[string]string{
		"Content-Type": "text/plain", "Authorization": "Bearer c", "Cookie": "sid=1", "X-Custom": "v",
	})
	if w.Code != http.StatusOK || w.Header().Get("Content-Type") != "application/json" {
		t.Fatalf("got %d %q", w.Code, w.Header().Get("Content-Type"))
	}
	calls := a.seen()
	if len(calls) != 1 {
		t.Fatalf("agent calls = %d", len(calls))
	}
	c := calls[0]
	if c.method != "POST" || c.uri != "/api/a2a/agent/?q=1" || string(c.body) != `{"x":1}` {
		t.Errorf("forwarded %s %s %q", c.method, c.uri, c.body)
	}
	if c.header.Get("Content-Type") != "application/json" {
		t.Errorf("Content-Type = %q", c.header.Get("Content-Type"))
	}
	for _, h := range []string{"Authorization", "Cookie", "X-Custom"} {
		if c.header.Get(h) != "" {
			t.Errorf("client header %s reached the agent", h)
		}
	}
}

func TestGetPassthroughAlwaysAnswersJSON(t *testing.T) {
	a := newAgent(t, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/html")
		_, _ = io.WriteString(w, `{"name":"card"}`)
	})
	s, _ := newProxy(t, a.URL, config.Guards{})
	w := do(s, http.MethodGet, "/.well-known/agent-card.json", "", map[string]string{"Authorization": "Bearer c"})
	if w.Code != http.StatusOK || w.Header().Get("Content-Type") != "application/json" || w.Body.String() != `{"name":"card"}` {
		t.Errorf("got %d %q %q", w.Code, w.Header().Get("Content-Type"), w.Body.String())
	}
	if a.seen()[0].header.Get("Authorization") != "" {
		t.Error("client Authorization reached the agent on GET")
	}
}

func TestForwardFailureLogsAnErrorEvent(t *testing.T) {
	a := newAgent(t, func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusInternalServerError) })
	s, logs := newProxy(t, a.URL, config.Guards{})
	w := do(s, http.MethodPost, "/", `{}`, nil)
	if w.Code != http.StatusBadGateway {
		t.Fatalf("status = %d", w.Code)
	}
	var rec map[string]any
	if err := json.Unmarshal(bytes.TrimSpace(logs.Bytes()), &rec); err != nil {
		t.Fatalf("log %q: %v", logs.String(), err)
	}
	if rec["event"] != "forward_error" || rec["level"] != "ERROR" || !strings.Contains(rec["message"].(string), "500") {
		t.Errorf("log record = %v", rec)
	}
}
