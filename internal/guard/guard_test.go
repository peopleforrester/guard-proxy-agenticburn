// ABOUTME: Tests for the guards: the deterministic block-list and the LLM Guard API client, including
// ABOUTME: how each kind of scanner failure is reported so the caller can fail closed.
// SPDX-License-Identifier: Apache-2.0
package guard

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestBlocklistMatchesFirstTermInListOrder(t *testing.T) {
	b := Blocklist{"wipe", "nuke", "rm -rf"}
	for text, want := range map[string]string{
		"nuke it, then wipe it": "wipe",
		"NUKE it":               "nuke",
		"run rm -rf /":          "rm -rf",
	} {
		got, ok := b.Match(text)
		if !ok || got != want {
			t.Errorf("Match(%q) = %q, %v; want %q", text, got, ok, want)
		}
	}
	if got, ok := b.Match("one burrito"); ok {
		t.Errorf("clean text matched %q", got)
	}
	if _, ok := (Blocklist{}).Match("delete"); ok {
		t.Error("an empty list matched")
	}
}

func TestParseBlocklistTrimsLowercasesAndDropsEmpties(t *testing.T) {
	got := ParseBlocklist(" Guacamole , ,RM -rf,")
	want := Blocklist{"guacamole", "rm -rf"}
	if len(got) != len(want) || got[0] != want[0] || got[1] != want[1] {
		t.Errorf("ParseBlocklist = %q, want %q", got, want)
	}
}

type call struct {
	path, auth, contentType string
	body                    map[string]any
}

func scanner(t *testing.T, status int, reply string, delay time.Duration) (*httptest.Server, *[]call) {
	t.Helper()
	var calls []call
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		_ = json.Unmarshal(raw, &body)
		calls = append(calls, call{r.URL.Path, r.Header.Get("Authorization"), r.Header.Get("Content-Type"), body})
		time.Sleep(delay)
		w.WriteHeader(status)
		_, _ = io.WriteString(w, reply)
	}))
	t.Cleanup(s.Close)
	return s, &calls
}

func TestScanPromptSendsThePromptWithTheToken(t *testing.T) {
	s, calls := scanner(t, 200, `{"is_valid": true, "scanners": {}}`, 0)
	g := NewLLMGuard(s.URL, "tok", time.Second)
	ok, err := g.ScanPrompt(context.Background(), "one burrito")
	if err != nil || !ok {
		t.Fatalf("ScanPrompt = %v, %v", ok, err)
	}
	c := (*calls)[0]
	if c.path != "/analyze/prompt" || c.auth != "Bearer tok" || c.contentType != "application/json" {
		t.Errorf("call = %+v", c)
	}
	suppress, _ := c.body["scanners_suppress"].([]any)
	if len(c.body) != 2 || c.body["prompt"] != "one burrito" || len(suppress) != 1 || suppress[0] != "BanTopics" {
		t.Errorf("body = %v", c.body)
	}
}

func TestScanPromptVerdicts(t *testing.T) {
	for reply, want := range map[string]bool{
		`{"is_valid": false}`: false,
		`{"is_valid": true}`:  true,
		// No verdict field means the scanner found nothing to object to, as in the reference.
		`{"scanners": {}}`: true,
		// A null or empty verdict is falsy in the reference, so it blocks.
		`{"is_valid": null}`: false,
		`{"is_valid": 0}`:    false,
		`{"is_valid": "no"}`: true,
	} {
		s, _ := scanner(t, 200, reply, 0)
		ok, err := NewLLMGuard(s.URL, "", time.Second).ScanPrompt(context.Background(), "x")
		if err != nil || ok != want {
			t.Errorf("%s: ScanPrompt = %v, %v; want %v", reply, ok, err, want)
		}
	}
}

func TestOmitsAuthorizationWithoutAToken(t *testing.T) {
	s, calls := scanner(t, 200, `{"is_valid": true}`, 0)
	if _, err := NewLLMGuard(s.URL, "", time.Second).ScanPrompt(context.Background(), "x"); err != nil {
		t.Fatal(err)
	}
	if a := (*calls)[0].auth; a != "" {
		t.Errorf("Authorization = %q, want none", a)
	}
}

func TestScanFailuresAreErrors(t *testing.T) {
	unreachable := httptest.NewServer(http.NotFoundHandler())
	unreachable.Close()
	cases := map[string]func() string{
		"unreachable": func() string { return unreachable.URL },
		"http 500":    func() string { s, _ := scanner(t, 500, `{"detail":"boom"}`, 0); return s.URL },
		"bad json":    func() string { s, _ := scanner(t, 200, `<html>`, 0); return s.URL },
		"not object":  func() string { s, _ := scanner(t, 200, `[true]`, 0); return s.URL },
		"timeout": func() string {
			s, _ := scanner(t, 200, `{"is_valid": true}`, 300*time.Millisecond)
			return s.URL
		},
	}
	for name, url := range cases {
		g := NewLLMGuard(url(), "", 100*time.Millisecond)
		if _, err := g.ScanPrompt(context.Background(), "x"); err == nil {
			t.Errorf("%s: ScanPrompt returned no error", name)
		}
		if _, err := g.ScanOutput(context.Background(), "x"); err == nil {
			t.Errorf("%s: ScanOutput returned no error", name)
		}
	}
}

func TestScanOutput(t *testing.T) {
	for reply, want := range map[string]string{
		`{"is_valid": true, "sanitized_output": "ignored"}`:    "agent SECRET",
		`{"is_valid": false, "sanitized_output": "agent [R]"}`: "agent [R]",
		// Invalid without replacement text redacts the whole part.
		`{"is_valid": false}`:                         "[REDACTED]",
		`{"is_valid": false, "sanitized_output": ""}`: "[REDACTED]",
	} {
		s, calls := scanner(t, 200, reply, 0)
		got, err := NewLLMGuard(s.URL, "", time.Second).ScanOutput(context.Background(), "agent SECRET")
		if err != nil || got != want {
			t.Errorf("%s: ScanOutput = %q, %v; want %q", reply, got, err, want)
		}
		c := (*calls)[0]
		if c.path != "/analyze/output" || c.body["prompt"] != "" || c.body["output"] != "agent SECRET" {
			t.Errorf("call = %+v", c)
		}
	}
}
