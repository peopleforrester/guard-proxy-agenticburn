// ABOUTME: The guards on the A2A path: reading a message's text, the two input stages with their
// ABOUTME: fail-closed policy, and the output guard's scrub of every agent-authored text part.
// SPDX-License-Identifier: Apache-2.0
package server

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"strings"

	"github.com/peopleforrester/guard-proxy-agenticburn/internal/config"
)

const (
	classifierBlocked = "Request blocked by input guardrail (prompt injection detected)."
	outputBlocked     = "[BLOCKED BY OUTPUT GUARDRAIL]"
)

type rpcError struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id"`
	Error   rpcErrorBody    `json:"error"`
}

type rpcErrorBody struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

// parseA2A returns the request's JSON-RPC id, verbatim, and the text the input guards read: every
// text part of params.message.parts, joined with single spaces. A body of another shape has no text.
func parseA2A(raw []byte) (json.RawMessage, string) {
	var req map[string]json.RawMessage
	if json.Unmarshal(raw, &req) != nil {
		return nil, ""
	}
	var v struct {
		Params struct {
			Message struct {
				Parts []any `json:"parts"`
			} `json:"message"`
		} `json:"params"`
	}
	if json.Unmarshal(raw, &v) != nil {
		return req["id"], ""
	}
	return req["id"], textOf(v.Params.Message.Parts)
}

// textOf joins the text of each part whose kind is "text". A text part without a string text
// contributes an empty string, so the join keeps its separator, as in the reference.
func textOf(parts []any) string {
	var texts []string
	for _, p := range parts {
		m, ok := p.(map[string]any)
		if !ok || m["kind"] != "text" {
			continue
		}
		t, _ := m["text"].(string)
		texts = append(texts, t)
	}
	return strings.Join(texts, " ")
}

// inputGuards runs the enabled input stages in order, cheapest first, and returns the refusal
// message for a blocked request, or "" to let it through.
func (s *Server) inputGuards(ctx context.Context, g config.Guards, text string) string {
	if g.InputBlocklist {
		if term, hit := s.blocklist.Match(text); hit {
			s.log.Info(fmt.Sprintf("input blocked by deterministic block-list (matched '%s')", term),
				"event", "input_blocklist_hit")
			return fmt.Sprintf("Request blocked by input block-list (matched '%s'). No model tokens were spent.", term)
		}
	}
	if g.InputClassifier {
		allowed, err := s.llmGuard.ScanPrompt(ctx, text)
		if err != nil {
			s.guardError("input classifier", err)
			allowed = !s.failClosed
		}
		if !allowed {
			s.log.Info("input blocked by prompt-injection classifier", "event", "input_classifier_block")
			return classifierBlocked
		}
	}
	return ""
}

func (s *Server) guardError(stage string, err error) {
	policy := "failing open"
	if s.failClosed {
		policy = "failing closed"
	}
	s.log.Warn(fmt.Sprintf("%s unavailable, %s: %v", stage, policy, err), "event", "guard_error")
}

// scrubReply passes every agent-authored text part of a reply through the output guard: the
// artifacts, the agent's turns in the history, and the status message. A reply the guard leaves
// alone is returned byte for byte.
func (s *Server) scrubReply(ctx context.Context, body []byte) ([]byte, error) {
	dec := json.NewDecoder(bytes.NewReader(body))
	dec.UseNumber() // integers past 2^53 must survive the round trip
	var resp map[string]any
	if dec.Decode(&resp) != nil {
		return body, nil // not an object, so there is no result to scrub
	}
	result, ok := resp["result"].(map[string]any)
	if !ok {
		return body, nil
	}
	changed := false
	scrub := func(parts any) {
		list, _ := parts.([]any)
		for _, p := range list {
			m, ok := p.(map[string]any)
			if !ok || m["kind"] != "text" {
				continue
			}
			text, _ := m["text"].(string)
			if text == "" {
				continue
			}
			out, err := s.llmGuard.ScanOutput(ctx, text)
			if err != nil {
				s.guardError("output guard", err)
				out = text
				if s.failClosed {
					out = outputBlocked
				}
			}
			if out != text {
				s.log.Info("output guardrail modified response content", "event", "output_scrub")
				m["text"] = out
				changed = true
			}
		}
	}
	for _, a := range asList(result["artifacts"]) {
		scrub(field(a, "parts"))
	}
	for _, h := range asList(result["history"]) {
		if field(h, "role") == "agent" {
			scrub(field(h, "parts"))
		}
	}
	scrub(field(field(result["status"], "message"), "parts"))
	if !changed {
		return body, nil
	}
	var out bytes.Buffer
	enc := json.NewEncoder(&out)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(resp); err != nil {
		return nil, fmt.Errorf("encoding the scrubbed reply: %w", err)
	}
	return bytes.TrimRight(out.Bytes(), "\n"), nil
}

func asList(v any) []any {
	l, _ := v.([]any)
	return l
}

func field(v any, key string) any {
	m, _ := v.(map[string]any)
	return m[key]
}
