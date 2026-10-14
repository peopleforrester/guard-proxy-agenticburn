// ABOUTME: guard-proxy's HTTP surface: health, the guard state, and the hop to the agent for GET
// ABOUTME: discovery and A2A POSTs. Client credentials and headers never travel past this hop.
// SPDX-License-Identifier: Apache-2.0
package server

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"sync"

	"github.com/peopleforrester/guard-proxy-agenticburn/internal/config"
	"github.com/peopleforrester/guard-proxy-agenticburn/internal/guard"
)

// Server routes every request. It is safe for concurrent use.
type Server struct {
	agentURL string
	client   *http.Client
	log      *slog.Logger

	blocklist  guard.Blocklist
	llmGuard   *guard.LLMGuard
	failClosed bool

	mu     sync.Mutex
	guards config.Guards
}

// New builds a Server from a validated configuration.
func New(cfg config.Config, log *slog.Logger) *Server {
	return &Server{
		agentURL: cfg.Agent.URL,
		client:   &http.Client{Timeout: cfg.Agent.Timeout.Duration},
		log:      log,

		blocklist:  guard.Blocklist(cfg.Blocklist),
		llmGuard:   guard.NewLLMGuard(cfg.LLMGuard.URL, cfg.LLMGuard.Token, cfg.LLMGuard.Timeout.Duration),
		failClosed: cfg.FailClosed,

		guards: cfg.Guards,
	}
}

// unbuilt are the routes later milestones of PRD #412 implement. Until then they answer 501, so a
// request meant for them is never forwarded to the agent as if it were A2A traffic.
var unbuiltGET = []string{"/cost", "/prompts", "/controls", "/toggle"}

func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	path := r.URL.Path
	switch r.Method {
	case http.MethodGet:
		switch {
		case path == "/healthz":
			send(w, http.StatusOK, map[string]string{"status": "ok"})
		case strings.HasPrefix(path, "/guards"):
			send(w, http.StatusOK, s.guardState())
		case isUnbuiltGET(path):
			notImplemented(w, "GET "+path)
		default:
			s.getPassthrough(w, r)
		}
	case http.MethodPost:
		if strings.TrimRight(path, "/") == "/chat" {
			notImplemented(w, "POST /chat")
			return
		}
		s.forwardA2A(w, r)
	default:
		send(w, http.StatusNotImplemented, map[string]string{"error": "unsupported method " + r.Method})
	}
}

func isUnbuiltGET(path string) bool {
	for _, p := range unbuiltGET {
		if strings.HasPrefix(path, p) {
			return true
		}
	}
	return false
}

func notImplemented(w http.ResponseWriter, route string) {
	send(w, http.StatusNotImplemented, map[string]string{"error": route + " is not implemented in this build"})
}

// getPassthrough forwards agent-card and other discovery GETs, without the client's headers.
func (s *Server) getPassthrough(w http.ResponseWriter, r *http.Request) {
	body, status, err := s.agent(r, http.MethodGet, nil)
	if err != nil {
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
		if blocked := s.inputGuards(r.Context(), g, text); blocked != "" {
			send(w, http.StatusForbidden, rpcError{JSONRPC: "2.0", ID: id, Error: rpcErrorBody{Code: -32600, Message: blocked}})
			return
		}
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
