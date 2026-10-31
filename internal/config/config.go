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
	Chat     Chat     `yaml:"chat"`
	Spend    Spend    `yaml:"spend"`
	Feed     Feed     `yaml:"feed"`
	Topic    Topic    `yaml:"topic"`
	CORS     CORS     `yaml:"cors"`
	Controls Controls `yaml:"controls"`
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

// Chat is the storefront route, POST /chat.
type Chat struct {
	// TurnTimeout is one deadline for a whole turn, which can make up to six upstream calls in a row
	// (guard-proxy-agenticburn#21). Each call gets its own timeout or what is left of the turn,
	// whichever is less. Zero sets no turn deadline. The A2A path is not bound by it.
	TurnTimeout Duration `yaml:"turn_timeout"`
}

// Spend is the cost meter and the limits that protect the demo from itself. Zero turns a cap or a
// limit off.
type Spend struct {
	// CostCapUSD is the cluster-wide backstop: once the cluster's metered spend reaches it, every
	// model-bound request is refused. /toggle?cost_cap= lowers it at runtime.
	CostCapUSD float64 `yaml:"cost_cap_usd"`
	// BudgetCapUSD is the per-session cap the budget guard enforces (Challenge 4).
	BudgetCapUSD float64 `yaml:"budget_cap_usd"`
	// RateLimitRPM bounds model-bound requests per sliding minute, across both paths.
	RateLimitRPM int `yaml:"rate_limit_rpm"`
	// RateLimitSessionRPM bounds one /chat conversation per sliding minute, checked first.
	RateLimitSessionRPM int `yaml:"rate_limit_session_rpm"`
	// ModelTier picks the price row; an unknown tier prices as haiku.
	ModelTier string `yaml:"model_tier"`
	// ModelName is the model id reported to telemetry; it defaults from the tier.
	ModelName string `yaml:"model_name"`
	// CostPer1KIn and CostPer1KOut are USD per 1,000 tokens; they default from the tier.
	CostPer1KIn  float64 `yaml:"cost_per_1k_in"`
	CostPer1KOut float64 `yaml:"cost_per_1k_out"`

	inSet, outSet bool
}

// Feed is the moderated prompt feed behind GET /prompts.
type Feed struct {
	// Enabled captures prompts; it is off unless STREAM_PROMPTS is on.
	Enabled bool `yaml:"enabled"`
	// Profanity is masked as whole words, plural included, normalized to trimmed lowercase terms.
	Profanity []string `yaml:"profanity"`
}

// Topic is the on-topic guard's phrase lists and refusal. Its on/off state is Guards.Topic.
type Topic struct {
	Off     []string `yaml:"off"`
	On      []string `yaml:"on"`
	Refusal string   `yaml:"refusal"`
}

// CORS is the response CORS policy for the instructor console.
type CORS struct {
	// ConsoleOrigins are the cross-site origins whose reads are allowed, besides a request's own host.
	ConsoleOrigins []string `yaml:"console_origins"`
}

// Controls is how GET /controls reads cluster objects: the API server and the pod's credentials.
type Controls struct {
	APIURL    string `yaml:"api_url"`
	TokenPath string `yaml:"token_path"`
	CAPath    string `yaml:"ca_path"`
	// PublicHost is reported as "host", so the page can say which cluster answered.
	PublicHost string `yaml:"public_host"`
}

// TierPrice is the list price of one model tier, in USD per 1,000 tokens.
type TierPrice struct{ In, Out float64 }

// TierPrices is the reference proxy's table: Haiku 4.5, Sonnet 4.6 and Opus 4.8 list prices sourced
// 2026-06-19, Nova Pro 2026-08-30. Confirm a price against the Bedrock pricing page before quoting it.
var TierPrices = map[string]TierPrice{
	"nova":   {0.0008, 0.0032},
	"haiku":  {0.001, 0.005},
	"sonnet": {0.003, 0.015},
	"opus":   {0.005, 0.025},
}

// tierModelIDs is the Bedrock model id per tier, reported when ModelName is not set.
var tierModelIDs = map[string]string{
	"nova":   "us.amazon.nova-pro-v1:0",
	"haiku":  "us.anthropic.claude-haiku-4-5-20251001-v1:0",
	"sonnet": "us.anthropic.claude-sonnet-4-6",
	"opus":   "us.anthropic.claude-opus-4-8",
}

// DefaultProfanity is the feed's default wordlist. Each inflected form and each compound that hides a
// term mid-word is its own entry, because endings are not generated: "spic" plus "es" is "spices".
const DefaultProfanity = "fuck,fucks,fucked,fucker,fucking,fuckin,motherfucker,motherfucking," +
	"shit,shitty,shitting,bullshit,shithead,cunt,bitch,bitches,bitching,bastard," +
	"dick,dickhead,cock,pussy,asshole,dumbass,jackass,wanker,twat," +
	"nigger,faggot,retard,retarded,tranny,slut,whore,rape,raped,rapist,kike,spic,chink"

// UnmarshalYAML records which prices the file set, so an unset one still defaults from the tier.
func (s *Spend) UnmarshalYAML(n *yaml.Node) error {
	type plain Spend
	p := plain(*s)
	if err := n.Decode(&p); err != nil {
		return err
	}
	for i := 0; i+1 < len(n.Content); i += 2 {
		switch n.Content[i].Value {
		case "cost_per_1k_in":
			p.inSet = true
		case "cost_per_1k_out":
			p.outSet = true
		}
	}
	*s = Spend(p)
	return nil
}

// Guards is the on/off state of each guard. The field names are the keys /guards reports.
type Guards struct {
	InputBlocklist  bool `yaml:"input_blocklist" json:"input_blocklist"`
	InputClassifier bool `yaml:"input_classifier" json:"input_classifier"`
	Output          bool `yaml:"output" json:"output"`
	Budget          bool `yaml:"budget" json:"budget"`
	Topic           bool `yaml:"topic" json:"topic"`
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
		Spend:      Spend{BudgetCapUSD: 0.03, ModelTier: "haiku"},
		Feed:       Feed{Profanity: SplitList(DefaultProfanity)},
		Topic:      Topic{Refusal: "BurritoBot only talks burritos: the menu, our hours, and our locations."},
		CORS:       CORS{ConsoleOrigins: []string{"https://start.agenticburn.com", "https://start.burritobot.co"}},
		Controls: Controls{
			APIURL:    "https://kubernetes.default.svc",
			TokenPath: "/var/run/secrets/kubernetes.io/serviceaccount/token",
			CAPath:    "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
		},
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
	c.Feed.Profanity = []string(guard.Normalize(c.Feed.Profanity))
	c.Topic.Off, c.Topic.On = trimList(c.Topic.Off), trimList(c.Topic.On)
	c.CORS.ConsoleOrigins = trimList(c.CORS.ConsoleOrigins)
	c.Spend.resolve()
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
	if v, ok := getenv("TURN_TIMEOUT"); ok {
		secs, err := strconv.ParseFloat(strings.TrimSpace(v), 64)
		if err != nil || math.IsNaN(secs) || math.IsInf(secs, 0) {
			return fmt.Errorf("TURN_TIMEOUT=%q: want a number of seconds", v)
		}
		c.Chat.TurnTimeout = Duration{time.Duration(secs * float64(time.Second))}
	}
	if err := applySpendEnv(&c.Spend, getenv); err != nil {
		return err
	}
	if v, ok := getenv("PROFANITY_LIST"); ok {
		c.Feed.Profanity = SplitList(v)
	}
	if v, ok := getenv("TOPIC_OFF_LIST"); ok {
		c.Topic.Off = SplitList(v)
	}
	if v, ok := getenv("TOPIC_ON_LIST"); ok {
		c.Topic.On = SplitList(v)
	}
	if v, ok := getenv("TOPIC_REFUSAL"); ok {
		c.Topic.Refusal = v
	}
	if v, ok := getenv("CONSOLE_ORIGIN"); ok {
		c.CORS.ConsoleOrigins = SplitList(v)
	}
	// An empty value keeps the in-cluster default, as in the reference.
	if v, ok := getenv("K8S_API_URL"); ok && v != "" {
		c.Controls.APIURL = v
	}
	if v, ok := getenv("K8S_TOKEN_PATH"); ok && v != "" {
		c.Controls.TokenPath = v
	}
	if v, ok := getenv("WIB_PUBLIC_HOST"); ok {
		c.Controls.PublicHost = v
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
	// The on-topic guard is not seeded by INPUT_GUARD: every challenge prompt is off-topic by design.
	on("TOPIC_GUARD", &c.Guards.Topic)
	on("STREAM_PROMPTS", &c.Feed.Enabled)
	// INPUT_GUARD=on is the older single switch for both input stages, and it wins over either one.
	if v, ok := getenv("INPUT_GUARD"); ok && strings.ToLower(v) == "on" {
		c.Guards.InputBlocklist = true
		c.Guards.InputClassifier = true
	}
	return nil
}

// applySpendEnv reads the spend variables. The reference reads an empty cap or limit as its default; a
// value it cannot parse stops startup here, where the reference would crash or switch the cap off.
func applySpendEnv(s *Spend, getenv func(string) (string, bool)) error {
	usd := func(name string, dst *float64, empty float64) error {
		v, ok := getenv(name)
		if !ok {
			return nil
		}
		if strings.TrimSpace(v) == "" {
			*dst = empty
			return nil
		}
		f, err := strconv.ParseFloat(strings.TrimSpace(v), 64)
		if err != nil || math.IsNaN(f) || math.IsInf(f, 0) || f < 0 {
			return fmt.Errorf("%s=%q: want a non-negative number of US dollars", name, v)
		}
		*dst = f
		return nil
	}
	rpm := func(name string, dst *int) error {
		v, ok := getenv(name)
		if !ok {
			return nil
		}
		if strings.TrimSpace(v) == "" {
			*dst = 0
			return nil
		}
		n, err := strconv.Atoi(strings.TrimSpace(v))
		if err != nil || n < 0 {
			return fmt.Errorf("%s=%q: want a non-negative whole number per minute", name, v)
		}
		*dst = n
		return nil
	}
	price := func(name string, dst *float64, set *bool) error {
		v, ok := getenv(name)
		if !ok {
			return nil
		}
		f, err := strconv.ParseFloat(strings.TrimSpace(v), 64)
		if err != nil || math.IsNaN(f) || math.IsInf(f, 0) || f < 0 {
			return fmt.Errorf("%s=%q: want a non-negative price in US dollars per 1,000 tokens", name, v)
		}
		*dst, *set = f, true
		return nil
	}
	for _, err := range []error{
		usd("COST_CAP_USD", &s.CostCapUSD, 0),
		usd("BUDGET_CAP_USD", &s.BudgetCapUSD, 0.03),
		rpm("RATE_LIMIT_RPM", &s.RateLimitRPM),
		rpm("RATE_LIMIT_SESSION_RPM", &s.RateLimitSessionRPM),
		price("COST_PER_1K_IN", &s.CostPer1KIn, &s.inSet),
		price("COST_PER_1K_OUT", &s.CostPer1KOut, &s.outSet),
	} {
		if err != nil {
			return err
		}
	}
	if v, ok := getenv("MODEL_TIER"); ok {
		s.ModelTier = v
	}
	if v, ok := getenv("MODEL_NAME"); ok {
		s.ModelName = v
	}
	return nil
}

// resolve lowercases the tier and fills the prices and model name it implies, where none was set.
func (s *Spend) resolve() {
	s.ModelTier = strings.ToLower(s.ModelTier)
	p, ok := TierPrices[s.ModelTier]
	if !ok {
		p = TierPrices["haiku"]
	}
	if !s.inSet {
		s.CostPer1KIn = p.In
	}
	if !s.outSet {
		s.CostPer1KOut = p.Out
	}
	if s.ModelName == "" {
		s.ModelName = tierModelIDs[s.ModelTier]
		if s.ModelName == "" {
			s.ModelName = s.ModelTier
		}
	}
	s.inSet, s.outSet = false, false
}

// SplitList splits a comma-separated list, trimming each term and dropping empty ones. Case is kept.
func SplitList(v string) []string {
	return trimList(strings.Split(v, ","))
}

func trimList(in []string) []string {
	out := []string{}
	for _, t := range in {
		if t = strings.TrimSpace(t); t != "" {
			out = append(out, t)
		}
	}
	return out
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
	for name, v := range map[string]float64{
		"spend.cost_cap_usd": c.Spend.CostCapUSD, "spend.budget_cap_usd": c.Spend.BudgetCapUSD,
		"spend.cost_per_1k_in": c.Spend.CostPer1KIn, "spend.cost_per_1k_out": c.Spend.CostPer1KOut,
	} {
		if v < 0 || math.IsNaN(v) || math.IsInf(v, 0) {
			return fmt.Errorf("config: %s %v must be a non-negative number", name, v)
		}
	}
	if c.Spend.RateLimitRPM < 0 || c.Spend.RateLimitSessionRPM < 0 {
		return errors.New("config: spend rate limits must not be negative")
	}
	if c.Chat.TurnTimeout.Duration < 0 {
		return fmt.Errorf("config: chat turn_timeout %v must not be negative", c.Chat.TurnTimeout.Duration)
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
