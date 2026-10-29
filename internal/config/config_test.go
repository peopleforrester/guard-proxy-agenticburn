// ABOUTME: Tests for config loading: strict YAML, the environment variables the workshop sets today,
// ABOUTME: and the validation that makes a bad config fail at start instead of disabling a guard.
// SPDX-License-Identifier: Apache-2.0
package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func envFrom(m map[string]string) func(string) (string, bool) {
	return func(k string) (string, bool) {
		v, ok := m[k]
		return v, ok
	}
}

func writeFile(t *testing.T, body string) string {
	t.Helper()
	p := filepath.Join(t.TempDir(), "guard-proxy.yaml")
	if err := os.WriteFile(p, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	return p
}

func mustLoad(t *testing.T, path string, env map[string]string) Config {
	t.Helper()
	c, err := Load(path, envFrom(env))
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	return c
}

func loadErr(t *testing.T, path string, env map[string]string) string {
	t.Helper()
	_, err := Load(path, envFrom(env))
	if err == nil {
		t.Fatal("Load succeeded; want an error")
	}
	return err.Error()
}

// agentOnly is the smallest environment that loads: the two upstreams.
var agentOnly = map[string]string{"AGENT_URL": "http://agent:8080", "LLM_GUARD_URL": "http://llm-guard:8000"}

// withBase returns env plus the two required upstream URLs, unless env sets them itself.
func withBase(env map[string]string) map[string]string {
	out := map[string]string{"AGENT_URL": "http://a", "LLM_GUARD_URL": "http://g"}
	for k, v := range env {
		out[k] = v
	}
	return out
}

func TestDefaults(t *testing.T) {
	c := mustLoad(t, "", agentOnly)
	if c.Listen != ":8080" {
		t.Errorf("Listen = %q, want :8080", c.Listen)
	}
	if c.Agent.Timeout.Duration != 150*time.Second {
		t.Errorf("Timeout = %v, want 150s", c.Agent.Timeout.Duration)
	}
	if c.Guards != (Guards{}) {
		t.Errorf("Guards = %+v, want all off", c.Guards)
	}
}

func TestYAMLFile(t *testing.T) {
	p := writeFile(t, `
listen: 127.0.0.1:9000
agent:
  url: http://agent.example:3000
  timeout: 2.5s
llm_guard:
  url: http://llm-guard.example:8000
  timeout: 4s
blocklist: [Guacamole, " salsa "]
fail_closed: false
guards:
  output: true
  budget: true
`)
	c := mustLoad(t, p, nil)
	if c.Listen != "127.0.0.1:9000" || c.Agent.URL != "http://agent.example:3000" {
		t.Errorf("got %+v", c)
	}
	if c.Agent.Timeout.Duration != 2500*time.Millisecond {
		t.Errorf("Timeout = %v", c.Agent.Timeout.Duration)
	}
	if c.Guards != (Guards{Output: true, Budget: true}) {
		t.Errorf("Guards = %+v", c.Guards)
	}
	if c.LLMGuard.URL != "http://llm-guard.example:8000" || c.LLMGuard.Timeout.Duration != 4*time.Second {
		t.Errorf("LLMGuard = %+v", c.LLMGuard)
	}
	// A list in the file replaces the default one, and its terms are normalized.
	if strings.Join(c.Blocklist, "|") != "guacamole|salsa" {
		t.Errorf("Blocklist = %q", c.Blocklist)
	}
	if c.FailClosed {
		t.Error("fail_closed: false was ignored")
	}
}

func TestUnknownKeyFailsStartup(t *testing.T) {
	for name, body := range map[string]string{
		"top level": "agent: {url: http://a}\nllm_guard: {url: http://g}\noutput_guard: true\n",
		"nested":    "agent: {url: http://a}\nllm_guard: {url: http://g}\nguards: {outptu: true}\n",
	} {
		t.Run(name, func(t *testing.T) {
			msg := loadErr(t, writeFile(t, body), nil)
			if !strings.Contains(msg, "not found") {
				t.Errorf("error %q does not name the unknown field", msg)
			}
		})
	}
}

func TestSecondYAMLDocumentIsRejected(t *testing.T) {
	loadErr(t, writeFile(t, "agent: {url: http://a}\n---\nguards: {output: true}\n"), nil)
}

func TestMissingFileFails(t *testing.T) {
	loadErr(t, filepath.Join(t.TempDir(), "absent.yaml"), agentOnly)
}

func TestEnvironmentOverridesFile(t *testing.T) {
	p := writeFile(t, "agent: {url: http://from-file, timeout: 9s}\nguards: {output: true}\n")
	c := mustLoad(t, p, map[string]string{
		"AGENT_URL":     "http://from-env",
		"LLM_GUARD_URL": "http://g",
		"PROXY_TIMEOUT": "0.5",
		"OUTPUT_GUARD":  "off",
	})
	if c.Agent.URL != "http://from-env" {
		t.Errorf("URL = %q", c.Agent.URL)
	}
	if c.Agent.Timeout.Duration != 500*time.Millisecond {
		t.Errorf("Timeout = %v", c.Agent.Timeout.Duration)
	}
	if c.Guards.Output {
		t.Error("OUTPUT_GUARD=off did not override the file")
	}
}

// The seeding rules proxy.py applies, which the workshop manifests depend on.
func TestGuardEnvironmentSemantics(t *testing.T) {
	cases := []struct {
		env  map[string]string
		want Guards
	}{
		{map[string]string{"INPUT_BLOCKLIST": "on"}, Guards{InputBlocklist: true}},
		{map[string]string{"INPUT_CLASSIFIER": "ON"}, Guards{InputClassifier: true}},
		{map[string]string{"BUDGET_GUARD": "On"}, Guards{Budget: true}},
		{map[string]string{"INPUT_GUARD": "on"}, Guards{InputBlocklist: true, InputClassifier: true}},
		// The legacy switch wins over a per-stage "off".
		{map[string]string{"INPUT_GUARD": "on", "INPUT_BLOCKLIST": "off"}, Guards{InputBlocklist: true, InputClassifier: true}},
		// Anything other than "on" is off.
		{map[string]string{"OUTPUT_GUARD": "true"}, Guards{}},
		{map[string]string{"OUTPUT_GUARD": "1"}, Guards{}},
		{map[string]string{"INPUT_GUARD": "yes"}, Guards{}},
	}
	for _, tc := range cases {
		if got := mustLoad(t, "", withBase(tc.env)).Guards; got != tc.want {
			t.Errorf("%v: got %+v, want %+v", tc.env, got, tc.want)
		}
	}
}

func TestValidation(t *testing.T) {
	cases := map[string]map[string]string{
		"agent url missing":     {"LLM_GUARD_URL": "http://g"},
		"agent url relative":    withBase(map[string]string{"AGENT_URL": "agent:8080"}),
		"agent url not http":    withBase(map[string]string{"AGENT_URL": "ftp://agent"}),
		"llm guard url missing": {"AGENT_URL": "http://a"},
		"llm guard url bad":     withBase(map[string]string{"LLM_GUARD_URL": "llm-guard:8000"}),
		"timeout not a number":  withBase(map[string]string{"PROXY_TIMEOUT": "soon"}),
		"timeout zero":          withBase(map[string]string{"PROXY_TIMEOUT": "0"}),
		"timeout negative":      withBase(map[string]string{"PROXY_TIMEOUT": "-1"}),
		"timeout not finite":    withBase(map[string]string{"PROXY_TIMEOUT": "NaN"}),
		"turn timeout bad":      withBase(map[string]string{"TURN_TIMEOUT": "soon"}),
		"turn timeout negative": withBase(map[string]string{"TURN_TIMEOUT": "-1"}),
		"turn timeout infinite": withBase(map[string]string{"TURN_TIMEOUT": "Inf"}),
		// A typo must not turn fail-closed off, so only true and false are accepted.
		"fail closed typo": withBase(map[string]string{"PROXY_FAIL_CLOSED": "ture"}),
	}
	for name, env := range cases {
		t.Run(name, func(t *testing.T) { loadErr(t, "", env) })
	}
}

func TestEmptyListenIsRejected(t *testing.T) {
	loadErr(t, writeFile(t, "listen: \"\"\nagent: {url: http://a}\nllm_guard: {url: http://g}\n"), nil)
}

func TestGuardDefaults(t *testing.T) {
	c := mustLoad(t, "", agentOnly)
	if !c.FailClosed {
		t.Error("FailClosed defaults to false; want true")
	}
	if c.LLMGuard.Timeout.Duration != 150*time.Second {
		t.Errorf("LLMGuard timeout = %v, want 150s", c.LLMGuard.Timeout.Duration)
	}
	if len(c.Blocklist) != 15 || c.Blocklist[0] != "delete" ||
		c.Blocklist[13] != "witch-hazel-ghost-pepper-bat-spit-no7" {
		t.Errorf("Blocklist = %q", c.Blocklist)
	}
}

func TestGuardEnvironment(t *testing.T) {
	c := mustLoad(t, "", withBase(map[string]string{
		"LLM_GUARD_URL":     "http://llm-guard:8000",
		"LLM_GUARD_TOKEN":   "secret",
		"PROXY_TIMEOUT":     "0.5",
		"PROXY_FAIL_CLOSED": "FALSE",
		"BLOCK_LIST":        " Guacamole , ",
	}))
	if c.LLMGuard.URL != "http://llm-guard:8000" || c.LLMGuard.Token != "secret" {
		t.Errorf("LLMGuard = %+v", c.LLMGuard)
	}
	// One timeout bounds both upstreams, as in the reference proxy.
	if c.LLMGuard.Timeout.Duration != 500*time.Millisecond {
		t.Errorf("LLMGuard timeout = %v", c.LLMGuard.Timeout.Duration)
	}
	if c.FailClosed {
		t.Error("PROXY_FAIL_CLOSED=FALSE left fail-closed on")
	}
	if strings.Join(c.Blocklist, "|") != "guacamole" {
		t.Errorf("Blocklist = %q", c.Blocklist)
	}
	if c := mustLoad(t, "", withBase(map[string]string{"BLOCK_LIST": ""})); len(c.Blocklist) != 0 {
		t.Errorf("an empty BLOCK_LIST left %q", c.Blocklist)
	}
}

// TURN_TIMEOUT is one deadline for a whole /chat turn (guard-proxy-agenticburn#21). 0, the default,
// sets none, so each upstream call keeps its own timeout.
func TestTurnTimeout(t *testing.T) {
	if got := mustLoad(t, "", agentOnly).Chat.TurnTimeout.Duration; got != 0 {
		t.Errorf("default TurnTimeout = %v, want 0", got)
	}
	c := mustLoad(t, writeFile(t, "agent: {url: http://a}\nllm_guard: {url: http://g}\nchat: {turn_timeout: 270s}\n"), nil)
	if c.Chat.TurnTimeout.Duration != 270*time.Second {
		t.Errorf("file TurnTimeout = %v, want 270s", c.Chat.TurnTimeout.Duration)
	}
	for v, want := range map[string]time.Duration{"270": 270 * time.Second, " 1.5 ": 1500 * time.Millisecond, "0": 0} {
		if got := mustLoad(t, "", withBase(map[string]string{"TURN_TIMEOUT": v})).Chat.TurnTimeout.Duration; got != want {
			t.Errorf("TURN_TIMEOUT=%q: got %v, want %v", v, got, want)
		}
	}
	loadErr(t, writeFile(t, "agent: {url: http://a}\nllm_guard: {url: http://g}\nchat: {turn_timeout: -1s}\n"), nil)
}

func TestTokenIsNotReadFromTheFile(t *testing.T) {
	loadErr(t, writeFile(t, "agent: {url: http://a}\nllm_guard: {url: http://g, token: x}\n"), nil)
}
