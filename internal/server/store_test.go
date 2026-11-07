// ABOUTME: Tests that spend the store did not commit fails closed: the reply is withheld on both paths, its
// ABOUTME: server span is an error, a thinking-only retry cannot slip it through, and a refused cost cap is not applied.
// SPDX-License-Identifier: Apache-2.0
package server

import (
	"bytes"
	"net/http"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/peopleforrester/guard-proxy-agenticburn/internal/config"
	"github.com/peopleforrester/guard-proxy-agenticburn/internal/logging"
	"github.com/peopleforrester/guard-proxy-agenticburn/internal/spend"
)

const meteredReply = `{"jsonrpc": "2.0", "id": "x", "result": {"kind": "task", "id": "t1",
	"status": {"state": "completed", "message": {"role": "agent", "parts": [{"kind": "text", "text": "SECRET-REPLY"}]}},
	"metadata": {"kagent_usage_metadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}}}}`

// brokenStoreProxy is a Server whose store is closed once it is built, so every commit fails.
func brokenStoreProxy(t *testing.T, agentURL string) (*Server, *bytes.Buffer) {
	t.Helper()
	st, err := spend.OpenStore(filepath.Join(t.TempDir(), "spend.db"))
	if err != nil {
		t.Fatal(err)
	}
	var logs bytes.Buffer
	s, err := New(config.Config{Agent: config.Agent{URL: agentURL, Timeout: config.Duration{Duration: 2 * time.Second}},
		Spend: config.Spend{ModelTier: "opus"}}, logging.New(&logs), st)
	if err != nil {
		t.Fatal(err)
	}
	if err := st.Close(); err != nil {
		t.Fatal(err)
	}
	return s, &logs
}

func assertWithheld(t *testing.T, w interface {
	Result() *http.Response
}, body string, logs *bytes.Buffer) {
	t.Helper()
	if code := w.Result().StatusCode; code != http.StatusBadGateway {
		t.Errorf("status = %d, want 502", code)
	}
	if strings.Contains(body, "SECRET-REPLY") {
		t.Errorf("the reply left although its spend was not stored: %s", body)
	}
	if !strings.Contains(logs.String(), `"event":"spend_store_error"`) {
		t.Errorf("no spend_store_error logged: %s", logs)
	}
}

func TestA2AReplyIsWithheldWhenItsSpendIsNotStored(t *testing.T) {
	a := newAgent(t, replyJSON(meteredReply))
	s, logs := brokenStoreProxy(t, a.URL)
	w := do(s, http.MethodPost, "/", `{"jsonrpc": "2.0", "id": "x", "method": "message/send", "params": {}}`, nil)
	assertWithheld(t, w, w.Body.String(), logs)
	if !strings.Contains(w.Body.String(), "spend not recorded: spend store: ") {
		t.Errorf("body = %s", w.Body)
	}
}

func TestChatReplyIsWithheldWhenItsSpendIsNotStored(t *testing.T) {
	a := newAgent(t, replyJSON(meteredReply))
	s, logs := brokenStoreProxy(t, a.URL)
	w := do(s, http.MethodPost, "/chat", `{"prompt": "hi", "session": "a"}`, nil)
	assertWithheld(t, w, w.Body.String(), logs)
	if !strings.Contains(w.Body.String(), kitchenDown) {
		t.Errorf("body = %s", w.Body)
	}
	// The spend still holds in this process, though it was not stored.
	if got, want := s.meter.SessionUSD("a"), (spend.Prices{In: 0.005, Out: 0.025}).Cost(10, 5); got != want {
		t.Errorf("SessionUSD = %v", got)
	}
}

func TestChatThinkingOnlyRetryCannotReturnAnUnstoredReply(t *testing.T) {
	// The first reply has no usage, so it meters nothing and needs no commit; the retry's does.
	var n atomic.Int32
	a := newAgent(t, func(w http.ResponseWriter, r *http.Request) {
		if n.Add(1) == 1 {
			replyJSON(`{"result": {"kind": "task", "id": "t0", "status": {"state": "completed",
				"message": {"role": "agent", "parts": [{"kind": "text", "text": "<thinking>SECRET-REPLY draft</thinking>"}]}}}}`)(w, r)
			return
		}
		replyJSON(meteredReply)(w, r)
	})
	s, logs := brokenStoreProxy(t, a.URL)
	w := do(s, http.MethodPost, "/chat", `{"prompt": "hi", "session": "a"}`, nil)
	assertWithheld(t, w, w.Body.String(), logs)
	if got := n.Load(); got != 2 {
		t.Errorf("agent calls = %d, want the reply and its retry", got)
	}
}

func TestToggleCostCapTheStoreRefusedChangesNothing(t *testing.T) {
	s, logs := brokenStoreProxy(t, "http://unused.invalid")
	w := do(s, http.MethodGet, "/toggle?cost_cap=2&output=on", "", nil)
	if w.Code != http.StatusInternalServerError || !strings.Contains(w.Body.String(), "cost cap not set: spend store: ") {
		t.Errorf("toggle = %d %s", w.Code, w.Body)
	}
	if s.meter.CostCap() != 0 || s.guardState().Output {
		t.Errorf("cost cap %v, output %v: a refused toggle applied", s.meter.CostCap(), s.guardState().Output)
	}
	if !strings.Contains(logs.String(), `"event":"spend_store_error"`) {
		t.Errorf("no spend_store_error logged: %s", logs)
	}
	if strings.Contains(logs.String(), `"event":"guard_toggle"`) {
		t.Errorf("a refused toggle logged a guard_toggle: %s", logs)
	}
}

// recordSpans sends s's spans to a recorder instead of the global provider.
func recordSpans(s *Server) *tracetest.SpanRecorder {
	rec := tracetest.NewSpanRecorder()
	s.tracer = sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(rec)).Tracer(instrumentation)
	return rec
}

func TestAnUnstoredSpendMarksTheServerSpan(t *testing.T) {
	// guard-proxy-agenticburn#54: a reply withheld because its spend was not stored answers 502, so the
	// POST server span is an error that names the store's failure, on both paths.
	for _, path := range []string{"/", "/chat"} {
		a := newAgent(t, replyJSON(meteredReply))
		s, _ := brokenStoreProxy(t, a.URL)
		rec := recordSpans(s)
		body := `{"jsonrpc": "2.0", "id": "x", "method": "message/send", "params": {}}`
		if path == "/chat" {
			body = `{"prompt": "hi", "session": "a"}`
		}
		if w := do(s, http.MethodPost, path, body, nil); w.Code != http.StatusBadGateway {
			t.Fatalf("%s: status = %d, want 502", path, w.Code)
		}
		var root sdktrace.ReadOnlySpan
		for _, sp := range rec.Ended() {
			if sp.Name() == "POST "+path {
				root = sp
			}
		}
		if root == nil {
			t.Fatalf("%s: no POST %s span among %d", path, path, len(rec.Ended()))
		}
		st := root.Status()
		if st.Code != codes.Error || !strings.Contains(st.Description, "spend store: ") {
			t.Errorf("%s: status = %v %q, want Error naming the store", path, st.Code, st.Description)
		}
	}
}
