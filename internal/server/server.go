// ABOUTME: guard-proxy's HTTP surface: health, the admin reads and toggles, the CORS echo, and the hop to
// ABOUTME: the agent for GET discovery and A2A POSTs. Client credentials and headers never travel past it.
// SPDX-License-Identifier: Apache-2.0
package server

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/peopleforrester/guard-proxy-agenticburn/internal/config"
	"github.com/peopleforrester/guard-proxy-agenticburn/internal/controls"
	"github.com/peopleforrester/guard-proxy-agenticburn/internal/feed"
	"github.com/peopleforrester/guard-proxy-agenticburn/internal/guard"
	"github.com/peopleforrester/guard-proxy-agenticburn/internal/spend"
)

// Server routes every request. It is safe for concurrent use.
type Server struct {
	agentURL     string
	agentTimeout time.Duration
	client       *http.Client
	log          *slog.Logger

	blocklist    guard.Blocklist
	llmGuard     *guard.LLMGuard
	failClosed   bool
	topic        guard.Topic
	topicRefusal string
	turnTimeout  time.Duration

	meter      *spend.Meter
	limiter    *spend.Limiter
	envCostCap float64
	budgetCap  float64
	tier       string

	feed           *feed.Feed
	consoleOrigins map[string]bool
	controls       *controls.Reader
	publicHost     string

	mu     sync.Mutex
	guards config.Guards

	// ctxGen is each /chat session's self-heal generation, under ctxMu; chatSeq numbers messageIds.
	ctxMu   sync.Mutex
	ctxGen  map[string]int
	chatSeq atomic.Int64
}

// New builds a Server from a validated configuration.
func New(cfg config.Config, log *slog.Logger) *Server {
	blocklist := guard.Blocklist(cfg.Blocklist)
	origins := map[string]bool{}
	for _, o := range cfg.CORS.ConsoleOrigins {
		origins[o] = true
	}
	return &Server{
		agentURL:     cfg.Agent.URL,
		agentTimeout: cfg.Agent.Timeout.Duration,
		client:       &http.Client{Timeout: cfg.Agent.Timeout.Duration},
		log:          log,

		blocklist:    blocklist,
		llmGuard:     guard.NewLLMGuard(cfg.LLMGuard.URL, cfg.LLMGuard.Token, cfg.LLMGuard.Timeout.Duration),
		failClosed:   cfg.FailClosed,
		topic:        guard.NewTopic(cfg.Topic.Off, cfg.Topic.On),
		topicRefusal: cfg.Topic.Refusal,
		turnTimeout:  cfg.Chat.TurnTimeout.Duration,

		meter:      spend.NewMeter(spend.Prices{In: cfg.Spend.CostPer1KIn, Out: cfg.Spend.CostPer1KOut}, cfg.Spend.CostCapUSD),
		limiter:    spend.NewLimiter(cfg.Spend.RateLimitRPM, cfg.Spend.RateLimitSessionRPM),
		envCostCap: cfg.Spend.CostCapUSD,
		budgetCap:  cfg.Spend.BudgetCapUSD,
		tier:       cfg.Spend.ModelTier,

		feed:           feed.New(cfg.Feed.Enabled, feed.NewModerator(blocklist, cfg.Feed.Profanity)),
		consoleOrigins: origins,
		controls:       controls.NewReader(cfg.Controls.APIURL, cfg.Controls.TokenPath, cfg.Controls.CAPath),
		publicHost:     cfg.Controls.PublicHost,

		guards: cfg.Guards,
		ctxGen: map[string]int{},
	}
}

// ServeHTTP routes on the raw request target, query included, as the reference does: /cost and
// /prompts match exactly, /guards, /controls and /toggle by prefix, and any other GET goes to the agent.
func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	uri := r.RequestURI
	if uri == "" {
		uri = r.URL.RequestURI()
	}
	switch r.Method {
	case http.MethodGet:
		switch {
		case r.URL.Path == "/healthz":
			send(w, http.StatusOK, map[string]string{"status": "ok"})
		case uri == "/cost":
			s.cors(w, r)
			s.cost(w)
		case uri == "/prompts":
			s.cors(w, r)
			send(w, http.StatusOK, map[string]any{"enabled": s.feed.Enabled(), "prompts": s.feed.Prompts()})
		case strings.HasPrefix(uri, "/guards"):
			s.cors(w, r)
			send(w, http.StatusOK, s.guardState())
		case strings.HasPrefix(uri, "/controls"):
			s.cors(w, r)
			send(w, http.StatusOK, map[string]any{"ai": s.guardState(), "infra": s.controls.Read(), "host": s.publicHost})
		case strings.HasPrefix(uri, "/toggle"):
			s.cors(w, r)
			s.toggle(w, r)
		default:
			s.getPassthrough(w, r)
		}
	case http.MethodPost:
		s.cors(w, r)
		if strings.TrimRight(uri, "/") == "/chat" {
			s.chat(w, r)
			return
		}
		s.forwardA2A(w, r)
	default:
		send(w, http.StatusNotImplemented, map[string]string{"error": "unsupported method " + r.Method})
	}
}

// cors lets the instructor console read the response cross-site. The Origin is echoed only when it is
// the request's own host or a configured console origin, never "*", so an arbitrary page cannot read
// this cluster's spend or its feed. There is deliberately no OPTIONS handler, so a preflight fails.
func (s *Server) cors(w http.ResponseWriter, r *http.Request) {
	origin := r.Header.Get("Origin")
	if origin == "" || !s.originAllowed(r, origin) {
		return
	}
	w.Header().Set("Access-Control-Allow-Origin", origin)
	w.Header().Set("Vary", "Origin")
}

func (s *Server) originAllowed(r *http.Request, origin string) bool {
	host := strings.ToLower(strings.SplitN(r.Host, ":", 2)[0])
	o, err := url.Parse(origin)
	if err != nil {
		return false
	}
	if h := strings.ToLower(o.Hostname()); h != "" && host != "" && h == host {
		return true // the attendee's own console page, served from this same hostname
	}
	return s.consoleOrigins[origin]
}

// cost reports the cluster's spend and the ceiling that would bite first.
func (s *Server) cost(w http.ResponseWriter) {
	g := s.guardState()
	t := s.meter.Totals()
	send(w, http.StatusOK, map[string]any{
		"tier":          s.tier,
		"requests":      t.Requests,
		"input_tokens":  t.InputTokens,
		"output_tokens": t.OutputTokens,
		"total_tokens":  t.TotalTokens,
		"usd":           t.USD,
		"cap_usd":       s.meter.CapUSD(g.Budget, s.budgetCap),
		"budget_guard":  g.Budget,
	})
}

// toggle flips guards at runtime, with no restart and no spec change, and sets the cost cap. A guard
// key is on only for the value "on"; input=on flips both input stages, and a specific key wins over
// it. A refused cost_cap answers 400 and changes nothing, guards included. Unknown keys are ignored.
func (s *Server) toggle(w http.ResponseWriter, r *http.Request) {
	q, _ := url.ParseQuery(r.URL.RawQuery)
	capVals, capAsked := q["cost_cap"]
	var newCap float64
	if capAsked {
		c, ok := spend.ParseCostCap(capVals[0], s.envCostCap)
		if !ok {
			send(w, http.StatusBadRequest, map[string]string{"error": spend.CostCapRefusal(s.envCostCap)})
			return
		}
		newCap = c
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	set := func(key string, dst *bool) {
		if v, ok := firstNonBlank(q[key]); ok {
			*dst = strings.ToLower(v) == "on"
		}
	}
	if v, ok := firstNonBlank(q["input"]); ok {
		on := strings.ToLower(v) == "on"
		s.guards.InputBlocklist, s.guards.InputClassifier = on, on
	}
	set("input_blocklist", &s.guards.InputBlocklist)
	set("input_classifier", &s.guards.InputClassifier)
	set("output", &s.guards.Output)
	set("budget", &s.guards.Budget)
	set("topic", &s.guards.Topic)
	if capAsked {
		s.meter.SetCostCap(newCap)
	}
	send(w, http.StatusOK, map[string]any{
		"input_blocklist":  s.guards.InputBlocklist,
		"input_classifier": s.guards.InputClassifier,
		"output":           s.guards.Output,
		"budget":           s.guards.Budget,
		"topic":            s.guards.Topic,
		"cost_cap_usd":     s.meter.CostCap(),
	})
}

// firstNonBlank is the first value of a key that has one, which is the value the reference reads: it
// drops blank values before taking the first.
func firstNonBlank(vals []string) (string, bool) {
	for _, v := range vals {
		if v != "" {
			return v, true
		}
	}
	return "", false
}

// getPassthrough forwards agent-card and other discovery GETs, without the client's headers.
func (s *Server) getPassthrough(w http.ResponseWriter, r *http.Request) {
	body, status, err := s.agent(r, http.MethodGet, nil)
	if err != nil {
		s.cors(w, r)
		send(w, http.StatusBadGateway, map[string]string{"error": "agent GET failed: " + err.Error()})
		return
	}
	writeJSON(w, status, body)
}

func (s *Server) guardState() config.Guards {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.guards
}

// forwardA2A runs the input guards over the message text, sends the client's body unchanged to the
// same path on the agent, and runs the output guard over the reply.
func (s *Server) forwardA2A(w http.ResponseWriter, r *http.Request) {
	raw, err := io.ReadAll(r.Body)
	if err != nil {
		send(w, http.StatusBadRequest, map[string]string{"error": "reading request body: " + err.Error()})
		return
	}
	// One snapshot per request, so a toggle mid-request cannot apply half a policy.
	g := s.guardState()
	id, text := parseA2A(raw)
	if text != "" {
		s.feed.Record(text) // moderated, before any guard, so a blocked prompt shows masked
		if blocked := s.inputGuards(r.Context(), g, text); blocked != "" {
			send(w, http.StatusForbidden, rpcError{JSONRPC: "2.0", ID: id, Error: rpcErrorBody{Code: -32600, Message: blocked}})
			return
		}
	}
	// Protect the demo from itself: the rate limit and the caps, before any model token is spent.
	if s.limiter.Cluster() {
		send(w, http.StatusTooManyRequests, rpcError{JSONRPC: "2.0", ID: id, Error: rpcErrorBody{Code: -32000,
			Message: fmt.Sprintf("Rate limit reached (%d/min on this cluster). Slow down; the cost demo will not run away.", s.limiter.ClusterRPM())}})
		return
	}
	switch s.meter.Capped("", g.Budget, s.budgetCap) {
	case spend.CostCap:
		send(w, http.StatusTooManyRequests, rpcError{JSONRPC: "2.0", ID: id, Error: rpcErrorBody{Code: -32000,
			Message: fmt.Sprintf("Cost cap reached ($%.2f on this cluster). Spend is frozen for everyone; a blocked request costs nothing.", s.meter.CostCap())}})
		return
	case spend.Budget:
		send(w, http.StatusTooManyRequests, rpcError{JSONRPC: "2.0", ID: id, Error: rpcErrorBody{Code: -32000,
			Message: fmt.Sprintf("Budget cap reached ($%.2f on this cluster). Spend is frozen; a blocked request costs nothing.", s.budgetCap)}})
		return
	}
	body, _, err := s.agent(r, http.MethodPost, raw)
	if err == nil && !json.Valid(body) {
		err = fmt.Errorf("agent reply is not JSON (%d bytes)", len(body))
	}
	if err != nil {
		s.log.Error("agent forward failed: "+err.Error(), "event", "forward_error")
		send(w, http.StatusBadGateway, map[string]string{"error": "agent forward failed: " + err.Error()})
		return
	}
	s.meterReply(body, "")
	if g.Output {
		if body, err = s.scrubReply(r.Context(), body); err != nil {
			// The scrubbed reply cannot be produced, and the unscrubbed one must not leave.
			s.log.Error("output guard failed: "+err.Error(), "event", "guard_error")
			send(w, http.StatusBadGateway, map[string]string{"error": "output guard failed: " + err.Error()})
			return
		}
	}
	writeJSON(w, http.StatusOK, body)
}

// meterReply adds a reply's token usage to the cost tally. A reply without usage is not counted.
func (s *Server) meterReply(body []byte, session string) (spend.Usage, bool) {
	var v any
	if json.Unmarshal(body, &v) != nil {
		return spend.Usage{}, false
	}
	return s.meterValue(v, session)
}

func (s *Server) meterValue(v any, session string) (spend.Usage, bool) {
	u, ok := spend.UsageOf(v)
	if ok {
		s.meter.Record(u, session)
	}
	return u, ok
}

// agent makes one upstream call to the same request URI on the agent. Only a JSON Content-Type
// travels with it. A status outside 2xx is an error, as it is in the reference proxy.
func (s *Server) agent(r *http.Request, method string, body []byte) ([]byte, int, error) {
	var rd io.Reader
	if body != nil {
		rd = bytes.NewReader(body)
	}
	req, err := http.NewRequestWithContext(r.Context(), method, s.agentURL+r.URL.RequestURI(), rd)
	if err != nil {
		return nil, 0, err
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := s.client.Do(req)
	if err != nil {
		return nil, 0, err
	}
	defer resp.Body.Close()
	out, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, 0, err
	}
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return nil, 0, fmt.Errorf("HTTP %d from agent", resp.StatusCode)
	}
	return out, resp.StatusCode, nil
}

func send(w http.ResponseWriter, code int, v any) {
	b, err := json.Marshal(v)
	if err != nil {
		code, b = http.StatusInternalServerError, []byte(`{"error":"encoding response"}`)
	}
	writeJSON(w, code, b)
}

func writeJSON(w http.ResponseWriter, code int, body []byte) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Content-Length", fmt.Sprint(len(body)))
	w.WriteHeader(code)
	_, _ = w.Write(body)
}
