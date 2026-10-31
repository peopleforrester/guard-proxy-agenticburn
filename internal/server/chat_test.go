// ABOUTME: Unit tests for the /chat helpers the conformance suite reaches only through whole turns: reply
// ABOUTME: extraction, the scratchpad rules, the dangling tool_use check, context generations and task cancel.
// SPDX-License-Identifier: Apache-2.0
package server

import (
	"bytes"
	"encoding/json"
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/peopleforrester/guard-proxy-agenticburn/internal/config"
	"github.com/peopleforrester/guard-proxy-agenticburn/internal/logging"
)

func decode(t *testing.T, s string) any {
	t.Helper()
	var v any
	if err := json.Unmarshal([]byte(s), &v); err != nil {
		t.Fatal(err)
	}
	return v
}

func TestReplyTextTakesOneSource(t *testing.T) {
	for body, want := range map[string]string{
		`{"result": {"artifacts": [{"parts": [{"kind": "text", "text": "art"}]}], "history": [{"role": "agent", "parts": [{"kind": "text", "text": "hist"}]}]}}`:                                       "art",
		`{"result": {"history": [{"role": "user", "parts": [{"kind": "text", "text": "u"}]}, {"role": "agent", "parts": [{"kind": "text", "text": "hist"}]}]}}`:                                        "hist",
		`{"result": {"status": {"message": {"parts": [{"kind": "text", "text": " status "}]}}}}`:                                                                                                       "status",
		`{"result": {"artifacts": [{"parts": [{"kind": "text", "text": "one"}, {"kind": "data", "text": "skip"}, {"kind": "text", "text": "two"}]}, {"parts": [{"kind": "text", "text": "three"}]}]}}`: "one two three",
		`{"result": {}}`: "",
		`{"result": []}`: "",
		`[]`:             "",
		`null`:           "",
	} {
		if got := replyText(decode(t, body)); got != want {
			t.Errorf("%s: replyText = %q, want %q", body, got, want)
		}
	}
}

func TestThinkingOnly(t *testing.T) {
	for reply, want := range map[string]bool{
		"<thinking>I should list the proteins.</thinking>":    true,
		"< Thinking >I should list the proteins.</thinking >": true,
		"<thinking>x</thinking> .":                            true,
		"<thinking>unclosed to the end":                       true,
		"< Thinking > unclosed with a newline\n":              true,
		"<thinking>easy</thinking>Two burritos coming up.":    false,
		"Two burritos coming up. <thinking>maybe chips":       false,
		"":   false,
		"ok": false,
	} {
		if got := isThinkingOnly(reply); got != want {
			t.Errorf("isThinkingOnly(%q) = %v, want %v", reply, got, want)
		}
	}
}

func TestScratchpad(t *testing.T) {
	for reply, want := range map[string]string{
		"<thinking> I need the menu. </thinking>":        "I need the menu.",
		"<thinking> I need the menu. ":                   "I need the menu.",
		"<thinking>a</thinking><THINKING> b </thinking>": "a b",
		"<thinking></thinking>":                          "",
	} {
		if got := scratchpad(reply); got != want {
			t.Errorf("scratchpad(%q) = %q, want %q", reply, got, want)
		}
	}
}

func TestDanglingToolUse(t *testing.T) {
	if !isDanglingToolUse("ValidationException: `tool_use` ids were found without `tool_result` blocks") {
		t.Error("the Bedrock error was not recognized")
	}
	if isDanglingToolUse("I used a tool_use and got a tool_result") {
		t.Error("a reply mentioning the words is not the error")
	}
}

func TestContextGenerations(t *testing.T) {
	s := New(config.Config{Agent: config.Agent{URL: "http://unused.invalid"}}, nil)
	if got := s.contextID("s1"); got != "s1" {
		t.Errorf("contextID = %q", got)
	}
	s.rotateContext("s1")
	if got, other := s.contextID("s1"), s.contextID("s2"); got != "s1-g1" || other != "s2" {
		t.Errorf("contextID = %q, %q", got, other)
	}
	if got := s.contextID(""); got != "" {
		t.Errorf("no session: %q", got)
	}
}

func TestChatRequest(t *testing.T) {
	for body, want := range map[string][2]any{
		`{"prompt": "hi", "session": "s"}`: {"hi", "s"},
		`{"prompt": "hi", "session": 42}`:  {"hi", ""},
		`{"prompt": 42}`:                   {42.0, ""},
		`{}`:                               {"", ""},
		`null`:                             {"", ""},
		`["prompt"]`:                       {"", ""},
		`not json`:                         {"", ""},
	} {
		p, s := chatRequest([]byte(body))
		if p != want[0] || s != want[1] {
			t.Errorf("%s: chatRequest = %v, %q", body, p, s)
		}
	}
}

func TestRunningTaskID(t *testing.T) {
	for body, want := range map[string]string{
		`{"result": {"kind": "task", "id": "t1", "status": {"state": "submitted"}}}`:      "t1",
		`{"result": {"kind": "task", "id": "t1", "status": {"state": "working"}}}`:        "t1",
		`{"result": {"kind": "task", "id": "t1", "status": {"state": "completed"}}}`:      "",
		`{"result": {"kind": "task", "id": "t1", "status": {"state": "input-required"}}}`: "",
		`{"result": {"artifacts": []}}`:                                                   "",
		`{"error": {"code": -32000}}`:                                                     "",
		`null`:                                                                            "",
	} {
		got, err := runningTaskID(decode(t, body))
		if err != nil || got != want {
			t.Errorf("%s: runningTaskID = %q, %v, want %q", body, got, err, want)
		}
	}
	if _, err := runningTaskID(decode(t, `{"result": {"status": {"state": "working"}}}`)); err == nil {
		t.Error("a running task with no id was accepted")
	}
}

// lockedBuffer is a log sink the test reads while the cancel goroutine may still write to it.
type lockedBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *lockedBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *lockedBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

func rpcMethod(t *testing.T, body []byte) (method string, params map[string]any) {
	t.Helper()
	var rpc struct {
		Method string         `json:"method"`
		Params map[string]any `json:"params"`
	}
	if err := json.Unmarshal(body, &rpc); err != nil {
		t.Errorf("agent got %q: %v", body, err) // not Fatalf: the fake agent calls this off the test goroutine
	}
	return rpc.Method, rpc.Params
}

func TestChatCancelsATaskPastTheForwardDeadline(t *testing.T) {
	// The agent never finishes the task, until it is cancelled.
	var a *fakeAgent
	a = newAgent(t, func(w http.ResponseWriter, _ *http.Request) {
		calls := a.seen()
		state := "working"
		if m, _ := rpcMethod(t, calls[len(calls)-1].body); m == "tasks/cancel" {
			state = "canceled"
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"jsonrpc": "2.0", "id": "chat",
			"result": map[string]any{"kind": "task", "id": "t7", "status": map[string]any{"state": state}}})
	})
	var logs lockedBuffer
	s := New(config.Config{Agent: config.Agent{URL: a.URL, Timeout: config.Duration{Duration: 400 * time.Millisecond}}}, logging.New(&logs))
	start := time.Now()
	w := do(s, http.MethodPost, "/chat", `{"prompt": "hi"}`, nil)
	if w.Code != http.StatusBadGateway || !strings.Contains(w.Body.String(), kitchenDown) {
		t.Fatalf("chat = %d %s", w.Code, w.Body)
	}
	if took := time.Since(start); took > 2*time.Second {
		t.Errorf("the poll ran %v, past the 400ms forward deadline", took)
	}
	deadline := time.Now().Add(3 * time.Second)
	for !strings.Contains(logs.String(), `"task_cancel"`) && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
	}
	if !strings.Contains(logs.String(), `"task_cancel"`) {
		t.Fatalf("no task_cancel log: %s", logs.String())
	}
	calls := a.seen()
	first, params := rpcMethod(t, calls[0].body)
	if first != "message/send" || field(params["configuration"], "blocking") != false {
		t.Errorf("first call %s %v, want a non-blocking message/send", first, params)
	}
	last, params := rpcMethod(t, calls[len(calls)-1].body)
	if last != "tasks/cancel" || params["id"] != "t7" {
		t.Errorf("last call %s %v, want tasks/cancel for t7", last, params)
	}
	for _, c := range calls[1 : len(calls)-1] {
		if m, p := rpcMethod(t, c.body); m != "tasks/get" || p["id"] != "t7" {
			t.Errorf("poll %s %v, want tasks/get for t7", m, p)
		}
	}
}
