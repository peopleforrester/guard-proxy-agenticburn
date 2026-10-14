// ABOUTME: Unit tests for the guards on the A2A path, for what the conformance suite cannot see: the
// ABOUTME: guard_error log, malformed requests, and replies that must survive scrubbing byte for byte.
// SPDX-License-Identifier: Apache-2.0
package server

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/peopleforrester/guard-proxy-agenticburn/internal/config"
	"github.com/peopleforrester/guard-proxy-agenticburn/internal/guard"
	"github.com/peopleforrester/guard-proxy-agenticburn/internal/logging"
)

func guardedProxy(t *testing.T, agentURL, guardURL string, g config.Guards, failClosed bool) (*Server, *bytes.Buffer) {
	t.Helper()
	var logs bytes.Buffer
	cfg := config.Config{
		Listen:     "127.0.0.1:0",
		Agent:      config.Agent{URL: agentURL, Timeout: config.Duration{Duration: 2 * time.Second}},
		LLMGuard:   config.LLMGuard{URL: guardURL, Timeout: config.Duration{Duration: time.Second}},
		Blocklist:  guard.ParseBlocklist("delete,rm -rf"),
		FailClosed: failClosed,
		Guards:     g,
	}
	return New(cfg, logging.New(&logs)), &logs
}

func records(t *testing.T, logs *bytes.Buffer) []map[string]any {
	t.Helper()
	var out []map[string]any
	for _, l := range strings.Split(strings.TrimSpace(logs.String()), "\n") {
		if l == "" {
			continue
		}
		var rec map[string]any
		if err := json.Unmarshal([]byte(l), &rec); err != nil {
			t.Fatalf("log line %q: %v", l, err)
		}
		out = append(out, rec)
	}
	return out
}

func eventsOf(t *testing.T, logs *bytes.Buffer) []string {
	var out []string
	for _, r := range records(t, logs) {
		if e, ok := r["event"].(string); ok {
			out = append(out, e)
		}
	}
	return out
}

func deadURL() string {
	s := httptest.NewServer(http.NotFoundHandler())
	s.Close()
	return s.URL
}

// a2a is a message/send request with one text part. id is raw JSON.
func a2a(id, text string) string {
	b, _ := json.Marshal(text)
	return fmt.Sprintf(`{"jsonrpc":"2.0","id":%s,"method":"message/send","params":{"message":{"parts":[{"kind":"text","text":%s}]}}}`, id, b)
}

func TestBlockEchoesTheRequestIDVerbatim(t *testing.T) {
	a := newAgent(t, replyJSON(`{}`))
	s, _ := guardedProxy(t, a.URL, deadURL(), config.Guards{InputBlocklist: true}, true)
	for _, id := range []string{`12345678901234567890`, `"abc"`, `null`, `{"k":[1]}`} {
		w := do(s, http.MethodPost, "/", a2a(id, "please delete it"), nil)
		want := `{"jsonrpc":"2.0","id":` + id + `,"error":{"code":-32600,"message":"Request blocked by input block-list (matched 'delete'). No model tokens were spent."}}`
		if w.Code != http.StatusForbidden || w.Body.String() != want {
			t.Errorf("id %s: got %d %s", id, w.Code, w.Body.String())
		}
	}
	w := do(s, http.MethodPost, "/", `{"params":{"message":{"parts":[{"kind":"text","text":"rm -rf"}]}}}`, nil)
	if !strings.Contains(w.Body.String(), `"id":null`) {
		t.Errorf("a request without an id: %s", w.Body.String())
	}
	if len(a.seen()) != 0 {
		t.Error("a blocked request reached the agent")
	}
}

// The reference proxy crashes on these shapes. Here they carry no text, so no guard applies.
func TestMalformedMessagesCarryNoText(t *testing.T) {
	a := newAgent(t, replyJSON(`{"jsonrpc":"2.0","id":1,"result":{}}`))
	s, _ := guardedProxy(t, a.URL, deadURL(), config.Guards{InputBlocklist: true, InputClassifier: true}, true)
	for _, body := range []string{
		`[1,2]`, `"delete"`, `{"params":[]}`, `{"params":{"message":"delete"}}`,
		`{"params":{"message":{"parts":{"kind":"text","text":"delete"}}}}`,
		`{"params":{"message":{"parts":["delete",{"kind":"text","text":7}]}}}`,
	} {
		if w := do(s, http.MethodPost, "/", body, nil); w.Code != http.StatusOK {
			t.Errorf("%s: got %d %s", body, w.Code, w.Body.String())
		}
	}
}

func TestClassifierFailureLogsAGuardError(t *testing.T) {
	a := newAgent(t, replyJSON(`{"jsonrpc":"2.0","id":1,"result":{}}`))
	for _, failClosed := range []bool{true, false} {
		s, logs := guardedProxy(t, a.URL, deadURL(), config.Guards{InputClassifier: true}, failClosed)
		w := do(s, http.MethodPost, "/", a2a(`1`, "hello"), nil)
		want := http.StatusOK
		if failClosed {
			want = http.StatusForbidden
		}
		if w.Code != want {
			t.Errorf("failClosed=%v: status %d, want %d", failClosed, w.Code, want)
		}
		recs := records(t, logs)
		if len(recs) == 0 || recs[0]["event"] != "guard_error" || recs[0]["level"] != "WARNING" {
			t.Errorf("failClosed=%v: logs %v", failClosed, recs)
		}
	}
}

func TestOutputGuardLeavesAnUntouchedReplyByteForByte(t *testing.T) {
	// Key order, spacing and a number too large for a float64 all survive when nothing is redacted.
	reply := `{"result": {"status": {"message": {"parts": [{"kind": "text", "text": "fine"}]}}}, "jsonrpc": "2.0", "id": 12345678901234567890}`
	a := newAgent(t, replyJSON(reply))
	g := httptest.NewServer(replyJSON(`{"is_valid": true}`))
	defer g.Close()
	s, logs := guardedProxy(t, a.URL, g.URL, config.Guards{Output: true}, true)
	w := do(s, http.MethodPost, "/", a2a(`1`, "hi"), nil)
	if w.Body.String() != reply {
		t.Errorf("reply changed:\n got %s\nwant %s", w.Body.String(), reply)
	}
	if ev := eventsOf(t, logs); len(ev) != 0 {
		t.Errorf("events = %v", ev)
	}
}

func TestOutputGuardKeepsLargeNumbersWhenItRedacts(t *testing.T) {
	a := newAgent(t, replyJSON(`{"id":12345678901234567890,"result":{"artifacts":[{"parts":[{"kind":"text","text":"a <SECRET> & b"}]}]}}`))
	g := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.WriteString(w, `{"is_valid": false, "sanitized_output": "a <R> & b"}`)
	}))
	defer g.Close()
	s, logs := guardedProxy(t, a.URL, g.URL, config.Guards{Output: true}, true)
	w := do(s, http.MethodPost, "/", a2a(`1`, "hi"), nil)
	want := `{"id":12345678901234567890,"result":{"artifacts":[{"parts":[{"kind":"text","text":"a <R> & b"}]}]}}`
	if w.Body.String() != want {
		t.Errorf("got  %s\nwant %s", w.Body.String(), want)
	}
	if ev := eventsOf(t, logs); len(ev) != 1 || ev[0] != "output_scrub" {
		t.Errorf("events = %v", ev)
	}
}

func TestOutputGuardFailureBlocksEachPartAndLogs(t *testing.T) {
	a := newAgent(t, replyJSON(`{"result":{"artifacts":[{"parts":[{"kind":"text","text":"x"}]},"not an object",{"parts":null}],"history":[null,{"role":"agent","parts":[{"kind":"text","text":"y"}]}],"status":null}}`))
	s, logs := guardedProxy(t, a.URL, deadURL(), config.Guards{Output: true}, true)
	w := do(s, http.MethodPost, "/", a2a(`1`, "hi"), nil)
	if w.Code != http.StatusOK || strings.Count(w.Body.String(), "[BLOCKED BY OUTPUT GUARDRAIL]") != 2 {
		t.Errorf("got %d %s", w.Code, w.Body.String())
	}
	ev := strings.Join(eventsOf(t, logs), ",")
	if ev != "guard_error,output_scrub,guard_error,output_scrub" {
		t.Errorf("events = %s", ev)
	}
}

func TestGuardsOffNeverCallLLMGuard(t *testing.T) {
	a := newAgent(t, replyJSON(`{"result":{"artifacts":[{"parts":[{"kind":"text","text":"delete"}]}]}}`))
	g := newAgent(t, replyJSON(`{"is_valid": false}`))
	s, _ := guardedProxy(t, a.URL, g.URL, config.Guards{}, true)
	if w := do(s, http.MethodPost, "/", a2a(`1`, "delete it"), nil); w.Code != http.StatusOK {
		t.Errorf("status %d", w.Code)
	}
	if n := len(g.seen()); n != 0 {
		t.Errorf("LLM Guard called %d times", n)
	}
}
