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
)

// Config is the whole of guard-proxy's configuration.
type Config struct {
	// Listen is the address the HTTP server binds, as host:port.
	Listen string `yaml:"listen"`
	Agent  Agent  `yaml:"agent"`
	// Guards is the state each guard starts in. The live toggles change it at runtime.
	Guards Guards `yaml:"guards"`
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
		Listen: ":8080",
		Agent:  Agent{Timeout: Duration{150 * time.Second}},
	}
	if path != "" {
		if err := decodeFile(path, &c); err != nil {
			return Config{}, err
		}
	}
	if err := applyEnv(&c, getenv); err != nil {
		return Config{}, err
	}
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
	if v, ok := getenv("PROXY_TIMEOUT"); ok {
		secs, err := strconv.ParseFloat(strings.TrimSpace(v), 64)
		if err != nil || math.IsNaN(secs) || math.IsInf(secs, 0) {
			return fmt.Errorf("PROXY_TIMEOUT=%q: want a number of seconds", v)
		}
		c.Agent.Timeout = Duration{time.Duration(secs * float64(time.Second))}
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
	u, err := url.Parse(c.Agent.URL)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return fmt.Errorf("config: agent.url %q is not an absolute http or https URL", c.Agent.URL)
	}
	if c.Agent.Timeout.Duration <= 0 {
		return fmt.Errorf("config: agent timeout %v must be positive", c.Agent.Timeout.Duration)
	}
	return nil
}
