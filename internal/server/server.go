// ABOUTME: guard-proxy's HTTP surface: health, the admin reads and toggles, the CORS echo, and the hop to
// ABOUTME: the agent for GET discovery and A2A POSTs. Client credentials and headers never travel past it.
// SPDX-License-Identifier: Apache-2.0
package server

import (
	"bytes"
	"context"
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

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/trace"

	"github.com/peopleforrester/guard-proxy-agenticburn/internal/config"
	"github.com/peopleforrester/guard-proxy-agenticburn/internal/controls"
	"github.com/peopleforrester/guard-proxy-agenticburn/internal/feed"
	"github.com/peopleforrester/guard-proxy-agenticburn/internal/guard"
	"github.com/peopleforrester/guard-proxy-agenticburn/internal/spend"
	"github.com/peopleforrester/guard-proxy-agenticburn/internal/telemetry/semconv"
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
	prices     config.Spend
	binding    *binding

	feed           *feed.Feed
	consoleOrigins map[string]bool
	controls       *controls.Reader
	publicHost     string

	tracer      trace.Tracer
	capture     bool
	peerService string

	mu     sync.Mutex
	guards config.Guards

	// ctxGen is each /chat session's self-heal generation and recon the sessions that asked what folders the
	// bot can see, both under ctxMu; chatSeq numbers messageIds.
	ctxMu   sync.Mutex
	ctxGen  map[string]int
	recon   map[string]bool
	chatSeq atomic.Int64
}

// New builds a Server from a validated configuration, metering into store. It fails when the spend
// state in store cannot be read.
func New(cfg config.Config, log *slog.Logger, store *spend.Store) (*Server, error) {
	meter, err := spend.NewMeter(cfg.Spend.CostCapUSD, store)
	if err != nil {
		return nil, err
	}
	blocklist := guard.Blocklist(cfg.Blocklist)
	origins := map[string]bool{}
	for _, o := range cfg.CORS.ConsoleOrigins {
		origins[o] = true
	}
	cluster := controls.NewReader(cfg.Controls.APIURL, cfg.Controls.TokenPath, cfg.Controls.CAPath)
	s := &Server{
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

		meter:      meter,
		limiter:    spend.NewLimiter(cfg.Spend.RateLimitRPM, cfg.Spend.RateLimitSessionRPM),
		envCostCap: cfg.Spend.CostCapUSD,
		budgetCap:  cfg.Spend.BudgetCapUSD,
		prices:     cfg.Spend,
		binding:    newBinding(cluster.ModelConfig, cfg.Spend.ModelTier, log),

		feed:           feed.New(cfg.Feed.Enabled, feed.NewModerator(blocklist, cfg.Feed.Profanity)),
		consoleOrigins: origins,
		controls:       cluster,
		publicHost:     cfg.Controls.PublicHost,

		tracer:      otel.Tracer(instrumentation),
		capture:     cfg.Telemetry.CaptureContent,
		peerService: cfg.Telemetry.PeerService,

		guards: cfg.Guards,
		ctxGen: map[string]int{},
		recon:  map[string]bool{},
	}
	if err := s.registerCostGauge(); err != nil {
		return nil, fmt.Errorf("registering gen_ai.client.cost: %w", err)
	}
	return s, nil
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
			s.cost(w, r)
		case uri == "/prompts":
			s.cors(w, r)
			prompts, witches := s.feed.Snapshot()
			send(w, http.StatusOK, map[string]any{"enabled": s.feed.Enabled(), "prompts": prompts, "witches": witches})
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
		sw, r, end := s.serverSpan(w, r, uri)
		defer end()
		if strings.TrimRight(uri, "/") == "/chat" {
			s.chat(sw, r)
			return
		}
		s.forwardA2A(sw, r)
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

// cost reports the cluster's spend, the tier requests are priced at now, and the ceiling that would bite
// first.
func (s *Server) cost(w http.ResponseWriter, r *http.Request) {
	g := s.guardState()
	t := s.meter.Totals()
	send(w, http.StatusOK, map[string]any{
		"tier":          s.binding.tier(r.Context()),
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
// it. A refused cost_cap answers 400 and changes nothing, guards included, and so does a cap the store
// cannot keep, with a 500. Unknown keys are ignored.
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
	if capAsked {
		if err := s.meter.SetCostCap(newCap); err != nil {
			s.storeError(r.Context(), err)
			send(w, http.StatusInternalServerError, map[string]string{"error": "cost cap not set: " + err.Error()})
			return
		}
	}
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

// getPassthrough forwards agent-card and other discovery GETs, without the client's headers. A GET has
// no server span (guard-proxy-agenticburn#57), so a failed one is logged as the forward_error /chat logs.
func (s *Server) getPassthrough(w http.ResponseWriter, r *http.Request) {
	body, status, err := s.agent(r.Context(), r.URL.RequestURI(), http.MethodGet, nil)
	if err != nil {
		s.log.ErrorContext(r.Context(), "agent GET failed: "+err.Error(), "event", "forward_error")
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
		s.feed.Record(text, "") // the A2A path carries no witch name; moderated, before any guard, so a blocked prompt shows masked
	}
	ctx := r.Context()
	body, ok := s.sanitize(ctx, w, r, g, id, text, raw)
	if !ok {
		return
	}
	if _, _, _, err := s.meterReply(ctx, body, ""); err != nil {
		s.storeError(ctx, err)
		failed(w, err.Error())
		send(w, http.StatusBadGateway, map[string]string{"error": "spend not recorded: " + err.Error()})
		return
	}
	if g.Output {
		if body, err = s.scrubReply(ctx, body); err != nil {
			// The scrubbed reply cannot be produced, and the unscrubbed one must not leave.
			s.log.ErrorContext(ctx, "output guard failed: "+err.Error(), "event", "guard_error")
			failed(w, err.Error())
			send(w, http.StatusBadGateway, map[string]string{"error": "output guard failed: " + err.Error()})
			return
		}
	}
	writeJSON(w, http.StatusOK, body)
}

// meterReply adds a reply's token usage to the cost tally, priced at the tier the agent is bound to, and
// returns it with what it cost. A reply without usage is not counted. An error is a tally the store did
// not commit, and the reply must not be returned.
func (s *Server) meterReply(ctx context.Context, body []byte, session string) (spend.Usage, float64, bool, error) {
	var v any
	if json.Unmarshal(body, &v) != nil {
		return spend.Usage{}, 0, false, nil
	}
	return s.meterValue(ctx, v, session)
}

func (s *Server) meterValue(ctx context.Context, v any, session string) (spend.Usage, float64, bool, error) {
	u, ok := spend.UsageOf(v)
	if !ok {
		return u, 0, false, nil
	}
	tier := s.binding.tier(ctx)
	p := s.prices.PricesAt(tier)
	spent, err := s.meter.Record(u, spend.Prices{In: p.In, Out: p.Out}, s.prices.ModelAt(tier), session)
	return u, spent, true, err
}

// storeError logs a write the spend store did not commit.
func (s *Server) storeError(ctx context.Context, err error) {
	s.log.ErrorContext(ctx, err.Error(), "event", "spend_store_error")
}

// sanitize is the guard decision and the forward, inside the gen_ai "sanitize" span Datadog renders as an
// LLM call. It answers a refusal or a failed forward itself; ok is false then. The prompt is forwarded
// unchanged, so the span's input is the prompt and its output the agent's raw, unscrubbed reply.
func (s *Server) sanitize(ctx context.Context, w http.ResponseWriter, r *http.Request, g config.Guards,
	id json.RawMessage, text string, raw []byte) (body []byte, ok bool) {
	ctx, span := s.tracer.Start(ctx, "sanitize", trace.WithSpanKind(trace.SpanKindInternal),
		trace.WithAttributes(append(s.genAI(s.boundModel(ctx)), s.inputMessages(text)...)...))
	defer span.End()
	if text != "" {
		if blocked := s.inputGuards(ctx, g, text); blocked != "" {
			send(w, http.StatusForbidden, rpcError{JSONRPC: "2.0", ID: id, Error: rpcErrorBody{Code: -32600, Message: blocked}})
			return nil, false
		}
	}
	// Protect the demo from itself: the rate limit and the caps, before any model token is spent.
	if s.limiter.Cluster() {
		send(w, http.StatusTooManyRequests, rpcError{JSONRPC: "2.0", ID: id, Error: rpcErrorBody{Code: -32000,
			Message: fmt.Sprintf("Rate limit reached (%d/min on this cluster). Slow down; the cost demo will not run away.", s.limiter.ClusterRPM())}})
		return nil, false
	}
	switch s.meter.Capped("", g.Budget, s.budgetCap) {
	case spend.CostCap:
		send(w, http.StatusTooManyRequests, rpcError{JSONRPC: "2.0", ID: id, Error: rpcErrorBody{Code: -32000,
			Message: fmt.Sprintf("Cost cap reached ($%.2f on this cluster). Spend is frozen for everyone; a blocked request costs nothing.", s.meter.CostCap())}})
		return nil, false
	case spend.Budget:
		send(w, http.StatusTooManyRequests, rpcError{JSONRPC: "2.0", ID: id, Error: rpcErrorBody{Code: -32000,
			Message: fmt.Sprintf("Budget cap reached ($%.2f on this cluster). Spend is frozen; a blocked request costs nothing.", s.budgetCap)}})
		return nil, false
	}
	uri := r.URL.RequestURI()
	fctx, fwd := s.clientSpan(ctx, semconv.UrlFullKey.String(s.agentURL+uri))
	body, status, err := s.agent(fctx, uri, http.MethodPost, raw)
	if err == nil && !json.Valid(body) {
		err = fmt.Errorf("agent reply is not JSON (%d bytes)", len(body))
	}
	if status != 0 {
		fwd.SetAttributes(semconv.HttpResponseStatusCodeKey.Int(status))
	}
	if err != nil {
		httpStatus := 0 // a reply that is not JSON arrived with a 2xx, which is not the failure
		if status < 200 || status > 299 {
			httpStatus = status
		}
		failSpan(fwd, httpStatus, err)
		fwd.End()
		// The workshop collector makes sanitize the turn's root in Agent Observability and drops
		// agent.forward (guard-proxy-agenticburn#61), so sanitize fails with it, without the status code.
		span.SetAttributes(semconv.ErrorTypeKey.String(errorType(httpStatus, err)))
		span.SetStatus(codes.Error, err.Error())
		s.log.ErrorContext(ctx, "agent forward failed: "+err.Error(), "event", "forward_error")
		failed(w, err.Error())
		send(w, http.StatusBadGateway, map[string]string{"error": "agent forward failed: " + err.Error()})
		return nil, false
	}
	fwd.End()
	var v any
	if json.Unmarshal(body, &v) == nil {
		span.SetAttributes(s.outputMessages(replyText(v))...)
	}
	return body, true
}

// agent makes one upstream call to the same request URI on the agent. Only a JSON Content-Type
// travels with it. A status outside 2xx is an error, as it is in the reference proxy.
func (s *Server) agent(ctx context.Context, uri, method string, body []byte) ([]byte, int, error) {
	var rd io.Reader
	if body != nil {
		rd = bytes.NewReader(body)
	}
	req, err := http.NewRequestWithContext(ctx, method, s.agentURL+uri, rd)
	if err != nil {
		return nil, 0, err
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	// The forward hop's W3C context, so the agent's spans parent onto it. A request outside a span
	// carries none.
	otel.GetTextMapPropagator().Inject(ctx, propagation.HeaderCarrier(req.Header))
	resp, err := s.client.Do(req)
	if err != nil {
		return nil, 0, err
	}
	defer resp.Body.Close()
	out, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, resp.StatusCode, err
	}
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return nil, resp.StatusCode, fmt.Errorf("HTTP %d from agent", resp.StatusCode)
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
