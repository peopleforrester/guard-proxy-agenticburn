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

var agentOnly = map[string]string{"AGENT_URL": "http://agent:8080"}

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
}

func TestUnknownKeyFailsStartup(t *testing.T) {
	for name, body := range map[string]string{
		"top level": "agent: {url: http://a}\noutput_guard: true\n",
		"nested":    "agent: {url: http://a}\nguards: {outptu: true}\n",
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
		env := map[string]string{"AGENT_URL": "http://a"}
		for k, v := range tc.env {
			env[k] = v
		}
		if got := mustLoad(t, "", env).Guards; got != tc.want {
			t.Errorf("%v: got %+v, want %+v", tc.env, got, tc.want)
		}
	}
}

func TestValidation(t *testing.T) {
	cases := map[string]map[string]string{
		"agent url missing":    {},
		"agent url relative":   {"AGENT_URL": "agent:8080"},
		"agent url not http":   {"AGENT_URL": "ftp://agent"},
		"timeout not a number": {"AGENT_URL": "http://a", "PROXY_TIMEOUT": "soon"},
		"timeout zero":         {"AGENT_URL": "http://a", "PROXY_TIMEOUT": "0"},
		"timeout negative":     {"AGENT_URL": "http://a", "PROXY_TIMEOUT": "-1"},
		"timeout not finite":   {"AGENT_URL": "http://a", "PROXY_TIMEOUT": "NaN"},
	}
	for name, env := range cases {
		t.Run(name, func(t *testing.T) { loadErr(t, "", env) })
	}
}

func TestEmptyListenIsRejected(t *testing.T) {
	loadErr(t, writeFile(t, "listen: \"\"\nagent: {url: http://a}\n"), nil)
}
