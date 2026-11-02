// ABOUTME: Tests for the config sections the /chat port reads: spend, the prompt feed, the on-topic
// ABOUTME: guard, CORS and the cluster reads behind /controls, from defaults, YAML and the environment.
// SPDX-License-Identifier: Apache-2.0
package config

import (
	"reflect"
	"strings"
	"testing"
)

func TestSpendDefaults(t *testing.T) {
	c := mustLoad(t, "", agentOnly)
	want := Spend{BudgetCapUSD: 0.03, ModelTier: "haiku", CostPer1KIn: 0.001, CostPer1KOut: 0.005, StorePath: testStore}
	if c.Spend != want {
		t.Errorf("Spend = %+v, want %+v", c.Spend, want)
	}
}

func TestSpendEnvironment(t *testing.T) {
	c := mustLoad(t, "", withBase(map[string]string{
		"COST_CAP_USD": "25", "BUDGET_CAP_USD": "0.05", "RATE_LIMIT_RPM": "60", "RATE_LIMIT_SESSION_RPM": "6",
		"MODEL_TIER": "SONNET",
	}))
	s := c.Spend
	if s.CostCapUSD != 25 || s.BudgetCapUSD != 0.05 || s.RateLimitRPM != 60 || s.RateLimitSessionRPM != 6 {
		t.Errorf("Spend = %+v", s)
	}
	// The tier is lowercased, and an unset price comes from it.
	if s.ModelTier != "sonnet" || s.CostPer1KIn != 0.003 || s.CostPer1KOut != 0.015 || s.ModelAt(s.ModelTier) != "us.anthropic.claude-sonnet-4-6" {
		t.Errorf("Spend = %+v", s)
	}
}

func TestSpendUnknownTierPricesAsHaikuAndNamesItself(t *testing.T) {
	s := mustLoad(t, "", withBase(map[string]string{"MODEL_TIER": "mystery"})).Spend
	if s.ModelTier != "mystery" || s.CostPer1KIn != 0.001 || s.CostPer1KOut != 0.005 || s.ModelAt(s.ModelTier) != "mystery" {
		t.Errorf("Spend = %+v", s)
	}
}

func TestSpendExplicitPricesWinOverTheTier(t *testing.T) {
	s := mustLoad(t, "", withBase(map[string]string{
		"MODEL_TIER": "opus", "COST_PER_1K_IN": "1", "COST_PER_1K_OUT": "0", "MODEL_NAME": "m",
	})).Spend
	if s.CostPer1KIn != 1 || s.CostPer1KOut != 0 || s.ModelAt("opus") != "m" {
		t.Errorf("Spend = %+v", s)
	}
}

// The model a request reports is its bound tier's model, unless MODEL_NAME names one
// (guard-proxy-agenticburn#38).
func TestSpendModelAtTheBoundTier(t *testing.T) {
	s := mustLoad(t, "", withBase(map[string]string{"MODEL_TIER": "nova"})).Spend
	for tier, want := range map[string]string{
		"nova":    "us.amazon.nova-pro-v1:0",
		"haiku":   "us.anthropic.claude-haiku-4-5-20251001-v1:0",
		"sonnet":  "us.anthropic.claude-sonnet-4-6",
		"opus":    "us.anthropic.claude-opus-4-8",
		"mystery": "mystery", // a tier with no model id names itself, as MODEL_TIER does
	} {
		if got := s.ModelAt(tier); got != want {
			t.Errorf("ModelAt(%q) = %q, want %q", tier, got, want)
		}
	}
	for _, env := range []map[string]string{{"MODEL_NAME": "burrito-model"}, {"MODEL_NAME": "burrito-model", "MODEL_TIER": "opus"}} {
		named := mustLoad(t, "", withBase(env)).Spend
		for _, tier := range []string{"nova", "opus", "mystery"} {
			if got := named.ModelAt(tier); got != "burrito-model" {
				t.Errorf("with MODEL_NAME, ModelAt(%q) = %q", tier, got)
			}
		}
	}
	if got := mustLoad(t, "", withBase(map[string]string{"MODEL_NAME": ""})).Spend.ModelAt("opus"); got != "us.anthropic.claude-opus-4-8" {
		t.Errorf("an empty MODEL_NAME reports %q, want the tier's model", got)
	}
}

func TestSpendPricesAtTheBoundTier(t *testing.T) {
	s := mustLoad(t, "", withBase(map[string]string{"MODEL_TIER": "nova"})).Spend
	for tier, want := range map[string]TierPrice{
		"nova": {0.0008, 0.0032}, "haiku": {0.001, 0.005}, "sonnet": {0.003, 0.015}, "opus": {0.005, 0.025},
		"mystery": {0.001, 0.005}, // an unknown tier prices as haiku, as MODEL_TIER does
	} {
		if got := s.PricesAt(tier); got != want {
			t.Errorf("PricesAt(%q) = %+v, want %+v", tier, got, want)
		}
	}
}

func TestSpendExplicitPricesWinOverTheBoundTier(t *testing.T) {
	// Each explicit price is fixed on its own; the other still follows the tier.
	s := mustLoad(t, "", withBase(map[string]string{"MODEL_TIER": "nova", "COST_PER_1K_IN": "1"})).Spend
	if got := s.PricesAt("opus"); got != (TierPrice{1, 0.025}) {
		t.Errorf("PricesAt(opus) = %+v", got)
	}
	path := writeFile(t, "agent: {url: http://a}\nllm_guard: {url: http://g}\nspend: {cost_per_1k_out: 2}\n")
	s = mustLoad(t, path, nil).Spend
	if got := s.PricesAt("sonnet"); got != (TierPrice{0.003, 2}) {
		t.Errorf("PricesAt(sonnet) = %+v", got)
	}
}

func TestTierOfAModelConfig(t *testing.T) {
	for name, want := range map[string]string{
		"bedrock-nova": "nova", "bedrock-haiku": "haiku", "bedrock-sonnet": "sonnet", "bedrock-opus": "opus",
		"gateway-local-qwen3": "", "bedrock-mystery": "", "BEDROCK-OPUS": "", "bedrock-": "", "": "",
	} {
		got, ok := TierOf(name)
		if got != want || ok != (want != "") {
			t.Errorf("TierOf(%q) = %q, %v; want %q", name, got, ok, want)
		}
	}
}

func TestSpendEmptyValuesMeanTheDefault(t *testing.T) {
	// The reference reads "" as 0 for the caps and limits, and as 3 cents for the budget cap.
	s := mustLoad(t, "", withBase(map[string]string{
		"COST_CAP_USD": "", "BUDGET_CAP_USD": "", "RATE_LIMIT_RPM": "", "RATE_LIMIT_SESSION_RPM": "",
	})).Spend
	if s.CostCapUSD != 0 || s.BudgetCapUSD != 0.03 || s.RateLimitRPM != 0 || s.RateLimitSessionRPM != 0 {
		t.Errorf("Spend = %+v", s)
	}
}

func TestSpendBadValuesFailStartup(t *testing.T) {
	for _, kv := range [][2]string{
		{"COST_CAP_USD", "lots"}, {"COST_CAP_USD", "-1"}, {"COST_CAP_USD", "nan"},
		{"BUDGET_CAP_USD", "x"}, {"RATE_LIMIT_RPM", "1.5"}, {"RATE_LIMIT_RPM", "-2"},
		{"RATE_LIMIT_SESSION_RPM", "many"}, {"COST_PER_1K_IN", ""}, {"COST_PER_1K_OUT", "inf"},
	} {
		msg := loadErr(t, "", withBase(map[string]string{kv[0]: kv[1]}))
		if !strings.Contains(msg, kv[0]) {
			t.Errorf("%s=%q: error %q does not name the variable", kv[0], kv[1], msg)
		}
	}
}

func TestSpendFromYAML(t *testing.T) {
	path := writeFile(t, "agent: {url: http://a}\nllm_guard: {url: http://g}\n"+
		"spend: {cost_cap_usd: 10, budget_cap_usd: 0.2, rate_limit_rpm: 5, rate_limit_session_rpm: 2, model_tier: nova}\n")
	s := mustLoad(t, path, nil).Spend
	if s.CostCapUSD != 10 || s.BudgetCapUSD != 0.2 || s.RateLimitRPM != 5 || s.RateLimitSessionRPM != 2 ||
		s.ModelTier != "nova" || s.CostPer1KIn != 0.0008 || s.CostPer1KOut != 0.0032 {
		t.Errorf("Spend = %+v", s)
	}
}

func TestSpendStorePath(t *testing.T) {
	if s := mustLoad(t, "", withBase(map[string]string{"SPEND_STORE_PATH": "/var/lib/guard-proxy/spend.db"})).Spend; s.StorePath != "/var/lib/guard-proxy/spend.db" {
		t.Errorf("StorePath = %q", s.StorePath)
	}
	path := writeFile(t, "agent: {url: http://a}\nllm_guard: {url: http://g}\nspend: {store_path: /data/from-yaml.db}\n")
	if c, err := Load(path, envFrom(nil)); err != nil || c.Spend.StorePath != "/data/from-yaml.db" {
		t.Errorf("Load = %+v, %v", c.Spend, err)
	}
	// Unset or blank, startup stops: the tally is never kept in memory alone.
	for name, env := range map[string]map[string]string{
		"unset": {"AGENT_URL": "http://a", "LLM_GUARD_URL": "http://g"},
		"blank": {"AGENT_URL": "http://a", "LLM_GUARD_URL": "http://g", "SPEND_STORE_PATH": " "},
	} {
		t.Run(name, func(t *testing.T) {
			_, err := Load("", envFrom(env))
			if err == nil || !strings.Contains(err.Error(), "spend.store_path (or SPEND_STORE_PATH) is required") {
				t.Errorf("Load = %v", err)
			}
		})
	}
}

func TestFeedDefaultsAndEnvironment(t *testing.T) {
	c := mustLoad(t, "", agentOnly)
	if c.Feed.Enabled || len(c.Feed.Profanity) == 0 || c.Feed.Profanity[0] != "fuck" {
		t.Errorf("Feed = %+v", c.Feed)
	}
	c = mustLoad(t, "", withBase(map[string]string{"STREAM_PROMPTS": "ON", "PROFANITY_LIST": " Cilantro ,, kale"}))
	if !c.Feed.Enabled || !reflect.DeepEqual(c.Feed.Profanity, []string{"cilantro", "kale"}) {
		t.Errorf("Feed = %+v", c.Feed)
	}
	if c = mustLoad(t, "", withBase(map[string]string{"STREAM_PROMPTS": "true"})); c.Feed.Enabled {
		t.Error("STREAM_PROMPTS=true enabled the feed; only on does")
	}
}

func TestTopicDefaultsAndEnvironment(t *testing.T) {
	c := mustLoad(t, "", agentOnly)
	if c.Guards.Topic || len(c.Topic.Off) != 0 || len(c.Topic.On) != 0 ||
		c.Topic.Refusal != "BurritoBot only talks burritos: the menu, our hours, and our locations." {
		t.Errorf("Topic = %+v, guard %v", c.Topic, c.Guards.Topic)
	}
	c = mustLoad(t, "", withBase(map[string]string{
		"TOPIC_GUARD": "On", "TOPIC_OFF_LIST": "Hot Dog, , song", "TOPIC_ON_LIST": "menu", "TOPIC_REFUSAL": "no",
	}))
	// Case is kept: matching ignores it.
	if !c.Guards.Topic || !reflect.DeepEqual(c.Topic.Off, []string{"Hot Dog", "song"}) ||
		!reflect.DeepEqual(c.Topic.On, []string{"menu"}) || c.Topic.Refusal != "no" {
		t.Errorf("Topic = %+v, guard %v", c.Topic, c.Guards.Topic)
	}
	// The legacy input switch never seeds the topic guard.
	if mustLoad(t, "", withBase(map[string]string{"INPUT_GUARD": "on"})).Guards.Topic {
		t.Error("INPUT_GUARD=on turned the topic guard on")
	}
}

func TestConsoleOrigins(t *testing.T) {
	c := mustLoad(t, "", agentOnly)
	want := []string{"https://start.agenticburn.com", "https://start.burritobot.co"}
	if !reflect.DeepEqual(c.CORS.ConsoleOrigins, want) {
		t.Errorf("ConsoleOrigins = %v, want %v", c.CORS.ConsoleOrigins, want)
	}
	c = mustLoad(t, "", withBase(map[string]string{"CONSOLE_ORIGIN": "https://a.example, https://b.example,"}))
	if !reflect.DeepEqual(c.CORS.ConsoleOrigins, []string{"https://a.example", "https://b.example"}) {
		t.Errorf("ConsoleOrigins = %v", c.CORS.ConsoleOrigins)
	}
}

func TestControlsDefaultsAndEnvironment(t *testing.T) {
	c := mustLoad(t, "", agentOnly)
	want := Controls{APIURL: "https://kubernetes.default.svc",
		TokenPath: "/var/run/secrets/kubernetes.io/serviceaccount/token",
		CAPath:    "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"}
	if c.Controls != want {
		t.Errorf("Controls = %+v, want %+v", c.Controls, want)
	}
	c = mustLoad(t, "", withBase(map[string]string{
		"K8S_API_URL": "http://127.0.0.1:9", "K8S_TOKEN_PATH": "/tmp/t", "WIB_PUBLIC_HOST": "me.example",
	}))
	if c.Controls.APIURL != "http://127.0.0.1:9" || c.Controls.TokenPath != "/tmp/t" || c.Controls.PublicHost != "me.example" {
		t.Errorf("Controls = %+v", c.Controls)
	}
	// An empty override keeps the in-cluster default, as in the reference.
	if c = mustLoad(t, "", withBase(map[string]string{"K8S_API_URL": ""})); c.Controls.APIURL != want.APIURL {
		t.Errorf("APIURL = %q", c.Controls.APIURL)
	}
}

func TestTelemetryDefaultsAndEnvironment(t *testing.T) {
	c := mustLoad(t, "", agentOnly)
	if c.Telemetry != (Telemetry{PeerService: "burritobot-gateway"}) {
		t.Errorf("Telemetry = %+v", c.Telemetry)
	}
	for mode, want := range map[string]bool{
		"SPAN_ONLY": true, "event_only": true, "Span_And_Event": true,
		"NO_CONTENT": false, "true": false, "": false, "span-only": false,
	} {
		c = mustLoad(t, "", withBase(map[string]string{"OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": mode}))
		if c.Telemetry.CaptureContent != want {
			t.Errorf("%q: CaptureContent = %v", mode, c.Telemetry.CaptureContent)
		}
	}
	c = mustLoad(t, "", withBase(map[string]string{"PEER_SERVICE": "agentgateway"}))
	if c.Telemetry.PeerService != "agentgateway" {
		t.Errorf("PeerService = %q", c.Telemetry.PeerService)
	}
	// An empty override keeps the default, as the reference's `or` does.
	if c = mustLoad(t, "", withBase(map[string]string{"PEER_SERVICE": ""})); c.Telemetry.PeerService != "burritobot-gateway" {
		t.Errorf("PeerService = %q", c.Telemetry.PeerService)
	}
}
