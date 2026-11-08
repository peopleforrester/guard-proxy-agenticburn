// ABOUTME: The guards on the A2A path: reading a message's text, the two input stages and the on-topic
// ABOUTME: guard with their fail-closed policy, and the output guard's scrub of every agent-authored text.
// SPDX-License-Identifier: Apache-2.0
package server

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"strings"

	"github.com/peopleforrester/guard-proxy-agenticburn/internal/config"
	"github.com/peopleforrester/guard-proxy-agenticburn/internal/guard"
)

const (
	classifierBlocked = "Request blocked by input guardrail (prompt injection detected)."
	outputBlocked     = "[BLOCKED BY OUTPUT GUARDRAIL]"
	// redacted replaces each match of the always-on pattern (#1092), the marker LLM Guard's Regex scanner writes.
	redacted = "[REDACTED]"
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
			s.log.InfoContext(ctx, fmt.Sprintf("input blocked by deterministic block-list (matched '%s')", term),
				"event", "input_blocklist_hit")
			return fmt.Sprintf("Request blocked by input block-list (matched '%s'). No model tokens were spent.", term)
		}
	}
	if g.InputClassifier {
		v, err := s.llmGuard.ScanPrompt(ctx, text)
		if err != nil {
			s.guardError(ctx, "input classifier", err)
			v.Allowed = !s.failClosed
		}
		if !v.Allowed {
			names := make([]string, len(v.Flagged))
			for i, f := range v.Flagged {
				names[i] = f.Scanner
			}
			s.log.InfoContext(ctx, "input blocked by prompt-injection classifier", "event", "input_classifier_block",
				"scanners", names)
			// Name the scanners, as the block-list names its term; without scores (or with no
			// verdict, failing closed) the message stays the generic one.
			if len(v.Flagged) > 0 {
				return fmt.Sprintf("Request blocked by input guardrail (flagged by %s).", v.Describe())
			}
			return classifierBlocked
		}
	}
	// The on-topic guard runs after both input stages and before the caps, so a refusal spends no
	// tokens and takes no rate-limit slot. With no turn deadline here, it cannot run out of time.
	if g.Topic {
		if step, _ := s.topicRefused(ctx, text); step != "" {
			s.log.InfoContext(ctx, "input refused as off-topic (step "+step+")", "event", "topic_guard_block", "step", step)
			return s.topicRefusal
		}
	}
	return ""
}

// topicRefused is the step that refuses an off-topic prompt, "off_phrase" or "classifier", or "" to let
// it through. Cheapest first: an off phrase refuses, an on phrase passes, and anything else goes to
// BanTopics, whose failure follows the fail-closed policy unless the /chat turn ran out of time.
func (s *Server) topicRefused(ctx context.Context, text string) (string, error) {
	switch s.topic.Phrase(text) {
	case guard.PhraseOff:
		return "off_phrase", nil
	case guard.PhraseOn:
		return "", nil
	}
	allowed, err := s.llmGuard.ScanTopic(ctx, text)
	if err != nil {
		if s.turnExpired(ctx) {
			return "", errTurnExpired
		}
		s.guardError(ctx, "topic classifier", err)
		allowed = !s.failClosed
	}
	if !allowed {
		return "classifier", nil
	}
	return "", nil
}

func (s *Server) guardError(ctx context.Context, stage string, err error) {
	policy := "failing open"
	if s.failClosed {
		policy = "failing closed"
	}
	s.log.WarnContext(ctx, fmt.Sprintf("%s unavailable, %s: %v", stage, policy, err), "event", "guard_error")
}

// scrubReply passes every agent-authored text part of a reply through the output guard: the
// artifacts, the agent's turns in the history, and the status message. A reply the guard leaves
// alone is returned byte for byte.
func (s *Server) scrubReply(ctx context.Context, body []byte) ([]byte, error) {
	return eachAgentText(body, askUserOnly, func(text string) string {
		out, err := s.llmGuard.ScanOutput(ctx, text)
		if err != nil {
			s.guardError(ctx, "output guard", err)
			out = text
			if s.failClosed {
				out = outputBlocked
			}
		}
		if out != text {
			s.log.InfoContext(ctx, "output guardrail modified response content", "event", "output_scrub")
		}
		return out
	})
}

// redactReply applies the always-on redaction (#1092) to the parts scrubReply reads and to every string in
// an agent-authored data part, whatever the guards say: it is in-process and rewrites only what the pattern
// matches, so a tool's call or response costs no LLM Guard call and no fail-closed block breaks an id (#75).
// A reply it leaves alone is returned byte for byte.
func (s *Server) redactReply(ctx context.Context, body []byte) ([]byte, error) {
	if s.alwaysRedact == nil {
		return body, nil
	}
	return eachAgentText(body, everyDataString, func(text string) string {
		out := s.redact(text)
		if out != text {
			s.log.InfoContext(ctx, "always-on redaction modified response content", "event", "always_redact")
		}
		return out
	})
}

// redact replaces every non-empty match of the always-on pattern with [REDACTED]. An empty match is left
// alone, so a pattern that can match nothing never sprinkles the marker between letters.
func (s *Server) redact(text string) string {
	if s.alwaysRedact == nil || text == "" {
		return text
	}
	return s.alwaysRedact.ReplaceAllStringFunc(text, func(m string) string {
		if m == "" {
			return m
		}
		return redacted
	})
}

// dataWalk is what eachAgentText reads in a data part.
type dataWalk bool

const (
	// askUserOnly reads each copy of an ask_user question and nothing else in a data part (#73).
	askUserOnly dataWalk = false
	// everyDataString reads every non-empty string in a data part's data, at any depth, whichever tool wrote
	// it (#75); keys, numbers and nulls are left alone.
	everyDataString dataWalk = true
)

// eachAgentText hands every agent-authored text of an A2A reply (the artifacts, the agent's turns in the
// history, and the status message, each text part and what walk reads in each data part) to fn and puts
// back what it returns. A reply fn changes nothing in is returned byte for byte.
func eachAgentText(body []byte, walk dataWalk, fn func(string) string) ([]byte, error) {
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
	str := func(v any, key string) {
		m, _ := v.(map[string]any)
		text, _ := m[key].(string)
		if text == "" {
			return
		}
		if out := fn(text); out != text {
			m[key] = out
			changed = true
		}
	}
	questions := func(callArgs any) {
		for _, q := range asList(field(callArgs, "questions")) {
			str(q, "question")
		}
	}
	// kagent 0.10.1 writes an ask_user question four times, all in data parts: the call and its pending
	// response in the history, and the status message's adk_request_confirmation, in its
	// originalFunctionCall and as its toolConfirmation hint (guard-proxy-agenticburn#73).
	asked := func(data any) {
		switch field(data, "name") {
		case "ask_user":
			questions(field(data, "args"))
			questions(field(data, "response"))
		case "adk_request_confirmation":
			args := field(data, "args")
			if call := field(args, "originalFunctionCall"); field(call, "name") == "ask_user" {
				questions(field(call, "args"))
				str(field(args, "toolConfirmation"), "hint")
			}
		}
	}
	var leaves func(v any) any
	leaves = func(v any) any {
		switch x := v.(type) {
		case string:
			if x != "" {
				if out := fn(x); out != x {
					changed = true
					return out
				}
			}
		case map[string]any:
			for k, e := range x {
				x[k] = leaves(e)
			}
		case []any:
			for i, e := range x {
				x[i] = leaves(e)
			}
		}
		return v
	}
	each := func(parts any) {
		for _, p := range asList(parts) {
			switch field(p, "kind") {
			case "text":
				str(p, "text")
			case "data":
				if walk == askUserOnly {
					asked(field(p, "data"))
				} else if m := p.(map[string]any); m["data"] != nil {
					m["data"] = leaves(m["data"])
				}
			}
		}
	}
	for _, a := range asList(result["artifacts"]) {
		each(field(a, "parts"))
	}
	for _, h := range asList(result["history"]) {
		if field(h, "role") == "agent" {
			each(field(h, "parts"))
		}
	}
	each(field(field(result["status"], "message"), "parts"))
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
