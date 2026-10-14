// ABOUTME: guard-proxy configuration: one YAML file decoded strictly, then the environment variables
// ABOUTME: the workshop manifests set today, then validation. Any error stops the process at start.
// SPDX-License-Identifier: Apache-2.0
package config

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"math"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"

	"go.yaml.in/yaml/v3"

	"github.com/peopleforrester/guard-proxy-agenticburn/internal/guard"
)

// Config is the whole of guard-proxy's configuration.
type Config struct {
	// Listen is the address the HTTP server binds, as host:port.
	Listen   string   `yaml:"listen"`
	Agent    Agent    `yaml:"agent"`
	LLMGuard LLMGuard `yaml:"llm_guard"`
	// Blocklist is the input block-list, normalized to trimmed lowercase terms.
	Blocklist []string `yaml:"blocklist"`
	// FailClosed blocks a request or a reply when LLM Guard cannot give a verdict.
	FailClosed bool `yaml:"fail_closed"`
	// Guards is the state each guard starts in. The live toggles change it at runtime.
	Guards Guards `yaml:"guards"`
}

// LLMGuard is the LLM Guard API that the classifier and the output guard call.
type LLMGuard struct {
	URL string `yaml:"url"`
	// Token is read only from LLM_GUARD_TOKEN, so the config file never holds a secret.
	Token   string   `yaml:"-"`
	Timeout Duration `yaml:"timeout"`
}

// Agent is the upstream every request is proxied to.
type Agent struct {
	URL string `yaml:"url"`
	// Timeout bounds one upstream exchange, from connecting to reading the whole response.
	Timeout Duration `yaml:"timeout"`
}

// Guards is the on/off state of each guard. The field names are the keys /guards reports.
type Guards struct {
	InputBlocklist  bool `yaml:"input_blocklist" json:"input_blocklist"`
	InputClassifier bool `yaml:"input_classifier" json:"input_classifier"`
	Output          bool `yaml:"output" json:"output"`
	Budget          bool `yaml:"budget" json:"budget"`
}

// Duration is a time.Duration written in YAML as a Go duration string, such as "150s".
type Duration struct{ time.Duration }

// UnmarshalYAML parses a duration string.
func (d *Duration) UnmarshalYAML(n *yaml.Node) error {
	var s string
	if err := n.Decode(&s); err != nil {
		return err
	}
	v, err := time.ParseDuration(s)
	if err != nil {
		return fmt.Errorf("line %d: %w", n.Line, err)
	}
	d.Duration = v
	return nil
}

// Load builds the configuration: defaults, then the YAML file at path (skipped when path is empty),
// then the environment read through getenv, then validation.
func Load(path string, getenv func(string) (string, bool)) (Config, error) {
	c := Config{
		Listen:     ":8080",
		Agent:      Agent{Timeout: Duration{150 * time.Second}},
		LLMGuard:   LLMGuard{Timeout: Duration{150 * time.Second}},
		Blocklist:  guard.ParseBlocklist(guard.DefaultBlocklist),
		FailClosed: true,
	}
	if path != "" {
		if err := decodeFile(path, &c); err != nil {
			return Config{}, err
		}
	}
	if err := applyEnv(&c, getenv); err != nil {
		return Config{}, err
	}
	c.Blocklist = guard.Normalize(c.Blocklist)
	if err := c.validate(); err != nil {
		return Config{}, err
	}
	return c, nil
}

func decodeFile(path string, c *Config) error {
	raw, err := os.ReadFile(path)
	if err != nil {
		return fmt.Errorf("config: %w", err)
	}
	dec := yaml.NewDecoder(bytes.NewReader(raw))
	// An unknown key is most often a misspelled guard, and ignoring it would leave that guard off.
	dec.KnownFields(true)
	if err := dec.Decode(c); err != nil && !errors.Is(err, io.EOF) {
		return fmt.Errorf("config %s: %w", path, err)
	}
	var extra yaml.Node
	if err := dec.Decode(&extra); !errors.Is(err, io.EOF) {
		return fmt.Errorf("config %s: holds more than one YAML document", path)
	}
	return nil
}

// applyEnv maps the environment variables the workshop manifests set onto the config. A variable that
// is present overrides the file; one that is absent leaves it alone.
func applyEnv(c *Config, getenv func(string) (string, bool)) error {
	if v, ok := getenv("AGENT_URL"); ok {
		c.Agent.URL = v
	}
	if v, ok := getenv("LLM_GUARD_URL"); ok {
		c.LLMGuard.URL = v
	}
	if v, ok := getenv("LLM_GUARD_TOKEN"); ok {
		c.LLMGuard.Token = v
	}
	if v, ok := getenv("BLOCK_LIST"); ok {
		c.Blocklist = guard.ParseBlocklist(v)
	}
	if v, ok := getenv("PROXY_FAIL_CLOSED"); ok {
		// The reference treats anything but "true" as false, so a typo turns fail-closed off. Here a
		// typo stops startup instead.
		switch strings.ToLower(strings.TrimSpace(v)) {
		case "true":
			c.FailClosed = true
		case "false":
			c.FailClosed = false
		default:
			return fmt.Errorf("PROXY_FAIL_CLOSED=%q: want true or false", v)
		}
	}
	if v, ok := getenv("PROXY_TIMEOUT"); ok {
		secs, err := strconv.ParseFloat(strings.TrimSpace(v), 64)
		if err != nil || math.IsNaN(secs) || math.IsInf(secs, 0) {
			return fmt.Errorf("PROXY_TIMEOUT=%q: want a number of seconds", v)
		}
		// One timeout bounds both upstreams, as in the reference proxy.
		c.Agent.Timeout = Duration{time.Duration(secs * float64(time.Second))}
		c.LLMGuard.Timeout = c.Agent.Timeout
	}
	// A guard switch is on only for the value "on", in any case, matching the reference proxy.
	on := func(name string, dst *bool) {
		if v, ok := getenv(name); ok {
			*dst = strings.ToLower(v) == "on"
		}
	}
	on("INPUT_BLOCKLIST", &c.Guards.InputBlocklist)
	on("INPUT_CLASSIFIER", &c.Guards.InputClassifier)
	on("OUTPUT_GUARD", &c.Guards.Output)
	on("BUDGET_GUARD", &c.Guards.Budget)
	// INPUT_GUARD=on is the older single switch for both input stages, and it wins over either one.
	if v, ok := getenv("INPUT_GUARD"); ok && strings.ToLower(v) == "on" {
		c.Guards.InputBlocklist = true
		c.Guards.InputClassifier = true
	}
	return nil
}

func (c Config) validate() error {
	if c.Listen == "" {
		return errors.New("config: listen is empty")
	}
	if c.Agent.URL == "" {
		return errors.New("config: agent.url (or AGENT_URL) is required")
	}
	if err := checkURL("agent.url", c.Agent.URL); err != nil {
		return err
	}
	if c.LLMGuard.URL == "" {
		return errors.New("config: llm_guard.url (or LLM_GUARD_URL) is required")
	}
	if err := checkURL("llm_guard.url", c.LLMGuard.URL); err != nil {
		return err
	}
	if c.Agent.Timeout.Duration <= 0 {
		return fmt.Errorf("config: agent timeout %v must be positive", c.Agent.Timeout.Duration)
	}
	if c.LLMGuard.Timeout.Duration <= 0 {
		return fmt.Errorf("config: llm_guard timeout %v must be positive", c.LLMGuard.Timeout.Duration)
	}
	return nil
}

func checkURL(key, raw string) error {
	u, err := url.Parse(raw)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return fmt.Errorf("config: %s %q is not an absolute http or https URL", key, raw)
	}
	return nil
}
