// ABOUTME: Unit tests for the /chat helpers the conformance suite reaches only through whole turns: reply
// ABOUTME: extraction, the scratchpad rules, the dangling tool_use check and the context generations.
// SPDX-License-Identifier: Apache-2.0
package server

import (
	"encoding/json"
	"testing"

	"github.com/peopleforrester/guard-proxy-agenticburn/internal/config"
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
