// ABOUTME: POST /chat, the BurritoBot storefront: one turn under one TURN_TIMEOUT deadline, the same guards
// ABOUTME: and caps as the A2A path, the prompt wrapped as A2A message/send, and both self-heal retries.
// SPDX-License-Identifier: Apache-2.0
package server

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"regexp"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/peopleforrester/guard-proxy-agenticburn/internal/spend"
)

const (
	kitchenDown  = "BurritoBot's kitchen isn't answering. Try again in a moment."
	emptyPrompt  = "Tell BurritoBot what you'd like."
	outputBanned = "[blocked by the output guardrail]"
)

// errTurnExpired is a guard stage cut off by the turn's deadline. That is not a scanner outage, so the
// fail-closed policy does not decide it: the turn answers the forward's 502 instead.
var errTurnExpired = errors.New("turn deadline passed")

// turnExpired reports whether ctx is a /chat turn whose TURN_TIMEOUT has run out.
func (s *Server) turnExpired(ctx context.Context) bool {
	return s.turnTimeout > 0 && errors.Is(ctx.Err(), context.DeadlineExceeded)
}

// chatReply is the storefront's response body.
type chatReply struct {
	Reply        string   `json:"reply"`
	Guarded      bool     `json:"guarded"`
	InputTokens  int64    `json:"input_tokens"`
	OutputTokens int64    `json:"output_tokens"`
	CostUSD      *float64 `json:"cost_usd,omitempty"`
}

// guarded is a turn a guard or a limit ended before any model token was spent.
func guarded(reply string) chatReply { return chatReply{Reply: reply, Guarded: true} }

// chat runs one storefront turn. TURN_TIMEOUT bounds every upstream call the turn makes: each gets its
// client timeout or what is left of the turn, whichever is less.
func (s *Server) chat(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	if s.turnTimeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, s.turnTimeout)
		defer cancel()
	}
	if err := s.chatTurn(ctx, w, r); errors.Is(err, errTurnExpired) {
		s.log.Error(fmt.Sprintf("chat turn ran out of time: the %gs turn deadline (TURN_TIMEOUT) passed",
			s.turnTimeout.Seconds()), "event", "turn_timeout")
		send(w, http.StatusBadGateway, chatReply{Reply: kitchenDown})
	}
}

// chatRequest reads {prompt, session}. A body that is not a JSON object has neither, and a session
// that is not a string is no session.
func chatRequest(raw []byte) (prompt any, session string) {
	var body any
	if json.Unmarshal(raw, &body) != nil {
		return "", ""
	}
	obj, _ := body.(map[string]any)
	prompt = ""
	if p, ok := obj["prompt"]; ok {
		prompt = p
	}
	session, _ = obj["session"].(string)
	return prompt, session
}

// chatTurn answers the turn, or returns errTurnExpired without answering when a guard stage ran out of
// time. A forward the deadline cuts off answers the 502 itself.
func (s *Server) chatTurn(ctx context.Context, w http.ResponseWriter, r *http.Request) error {
	raw, err := io.ReadAll(r.Body)
	if err != nil {
		raw = nil
	}
	p, session := chatRequest(raw)
	prompt, ok := p.(string)
	if !ok || strings.TrimSpace(prompt) == "" {
		send(w, http.StatusBadRequest, chatReply{Reply: emptyPrompt})
		return nil
	}
	s.feed.Record(prompt)
	g := s.guardState()

	// The input guards and the topic guard, then the limits and the caps. A blocked turn spends nothing.
	if g.InputBlocklist {
		if hit, found := s.blocklist.Match(prompt); found {
			s.log.Info(fmt.Sprintf("chat input blocked by block-list (matched '%s')", hit), "event", "input_blocklist_hit")
			send(w, http.StatusOK, guarded(fmt.Sprintf("BurritoBot can't help with that (blocked: '%s'). No model tokens were spent.", hit)))
			return nil
		}
	}
	if g.InputClassifier {
		v, err := s.llmGuard.ScanPrompt(ctx, prompt)
		if err != nil {
			if s.turnExpired(ctx) {
				return errTurnExpired
			}
			s.guardError("input classifier", err)
			v.Allowed = !s.failClosed
		}
		if !v.Allowed {
			names := make([]string, len(v.Flagged))
			for i, f := range v.Flagged {
				names[i] = f.Scanner
			}
			s.log.Info("chat input blocked by classifier", "event", "input_classifier_block", "scanners", names)
			reason := ""
			if len(v.Flagged) > 0 {
				reason = ": " + v.Describe()
			}
			send(w, http.StatusOK, guarded("BurritoBot can't help with that (blocked by the input guardrail"+reason+")."))
			return nil
		}
	}
	if g.Topic {
		step, err := s.topicRefused(ctx, prompt)
		if err != nil {
			return err
		}
		if step != "" {
			s.log.Info("chat input refused as off-topic (step "+step+")", "event", "topic_guard_block", "step", step)
			send(w, http.StatusOK, guarded(s.topicRefusal))
			return nil
		}
	}
	// The conversation's own window first, so one phone's refusals take no slot from the room.
	if s.limiter.Session(session) {
		send(w, http.StatusOK, guarded(fmt.Sprintf("Slow down, hungry traveler. (%d/min per conversation.)", s.limiter.SessionRPM())))
		return nil
	}
	if s.limiter.Cluster() {
		send(w, http.StatusOK, guarded(fmt.Sprintf("Slow down, hungry traveler. (%d/min cap on this cluster.)", s.limiter.ClusterRPM())))
		return nil
	}
	switch s.meter.Capped(session, g.Budget, s.budgetCap) {
	case spend.CostCap:
		// The cluster-wide backstop holds every conversation, so Reset does not help and the reply must
		// not offer it: in the KubeCon finale the whole room reads this at once.
		send(w, http.StatusOK, guarded(fmt.Sprintf("The kitchen is closed: this cluster hit its $%.2f spend cap, "+
			"so BurritoBot isn't sending anything else to the model for anyone. A blocked request costs nothing.", s.meter.CostCap())))
		return nil
	case spend.Budget:
		// The way out is Reset: a fresh conversation gets a fresh budget with the guardrail still armed.
		send(w, http.StatusOK, guarded(fmt.Sprintf("The kitchen tab is frozen. This conversation hit its $%.2f "+
			"spend budget, so I'm not sending anything else to the model. A blocked request costs nothing. "+
			"Press Reset for a fresh conversation; the budget guard stays on.", s.budgetCap)))
		return nil
	}

	// Every model call is billed, so each attempt is metered as it returns, not only the reply the turn
	// ends on (guard-proxy-agenticburn#3). spent sums the turn for the storefront.
	var spent spend.Usage
	attempt := func(optional bool) (any, bool) {
		resp, running, err := s.forwardChat(ctx, prompt, s.contextID(session))
		if err != nil {
			s.log.Error("chat forward failed: "+err.Error(), "event", "forward_error")
			if !optional {
				send(w, http.StatusBadGateway, chatReply{Reply: kitchenDown})
			}
			if running != "" {
				go s.cancelTask(context.WithoutCancel(ctx), running, err.Error())
			}
			return nil, false
		}
		if u, ok := s.meterValue(resp, session); ok {
			spent.In += u.In
			spent.Out += u.Out
		}
		return resp, true
	}
	resp, ok := attempt(false)
	if !ok {
		return nil
	}
	// Self-heal: a dangling tool_use in the session's history makes Bedrock reject every later turn, so
	// rotate to a fresh contextId, abandoning the poisoned history, and retry once.
	if session != "" && isDanglingToolUse(replyText(resp)) {
		gen := s.rotateContext(session)
		s.log.Info(fmt.Sprintf("chat session poisoned by dangling tool_use; rotated contextId to gen %d", gen), "event", "ctx_rotate")
		if resp, ok = attempt(false); !ok {
			return nil
		}
	}
	// The same self-heal for a reasoning-only reply: ask once more, never in a loop. The first reply is
	// still usable, so a failed retry keeps it instead of answering 502 (guard-proxy-agenticburn#1).
	thinkingOnly := isThinkingOnly(replyText(resp))
	if thinkingOnly {
		s.log.Info("model returned reasoning with no answer; retrying once", "event", "thinking_only_retry")
		if retry, ok := attempt(true); ok && !isThinkingOnly(replyText(retry)) {
			resp, thinkingOnly = retry, false
		}
	}
	reply := replyText(resp)
	// The last resort, when both attempts stopped after the scratchpad: hand back its contents, which is
	// the only thing the model produced, and drop bare tags so the student never sees markup.
	if thinkingOnly && reply != "" {
		if inner := strings.TrimSpace(scratchpad(reply)); inner != "" {
			reply = inner
			s.log.Info("model returned reasoning twice; surfacing it rather than an empty reply", "event", "thinking_only_surfaced")
		} else {
			reply = ""
		}
	}
	isGuarded := false
	if g.Output && reply != "" {
		out, err := s.llmGuard.ScanOutput(ctx, reply)
		switch {
		case err != nil && s.turnExpired(ctx):
			// Failing open would hand back a reply the scrub had no time to read.
			return errTurnExpired
		case err != nil:
			s.guardError("output guard", err)
			if s.failClosed {
				reply, isGuarded = outputBanned, true
			}
		case out != reply:
			reply, isGuarded = out, true
		}
	}
	if reply == "" {
		reply = "..."
	}
	cost := s.meter.Prices().Cost(spent.In, spent.Out)
	send(w, http.StatusOK, chatReply{Reply: reply, Guarded: isGuarded, InputTokens: spent.In, OutputTokens: spent.Out, CostUSD: &cost})
	return nil
}

// The /chat forward is an A2A task the proxy can name (guard-proxy-agenticburn#27). kagent runs a turn in
// a context detached from the request, so a forward that gave up left the agent working, and spending,
// for hours. The forward is therefore sent non-blocking, which answers with the task's id at once, then
// polled with tasks/get until the task leaves the running states; a forward that fails or runs out of
// time sends tasks/cancel for that id. The poll waits pollFirst, doubling to at most pollMax.
const (
	pollFirst = 250 * time.Millisecond
	pollMax   = time.Second
)

// forwardChat sends the prompt to the agent root as a non-blocking A2A message/send, under contextID when
// set so kagent threads the conversation, and polls the task to its end. PROXY_TIMEOUT bounds the whole
// forward, the send and every poll, and the turn's deadline cuts it shorter. On an error, running is the
// id of the task the agent may still be working on, or "" when there is none to cancel.
func (s *Server) forwardChat(ctx context.Context, prompt, contextID string) (resp any, running string, err error) {
	ctx, cancel := context.WithTimeout(ctx, s.agentTimeout)
	defer cancel()
	msg := map[string]any{
		"role":      "user",
		"messageId": fmt.Sprintf("chat-%d", s.chatSeq.Add(1)),
		"parts":     []map[string]string{{"kind": "text", "text": prompt}},
	}
	if contextID != "" {
		msg["contextId"] = contextID
	}
	resp, err = s.agentRPC(ctx, map[string]any{"jsonrpc": "2.0", "id": "chat", "method": "message/send",
		"params": map[string]any{"message": msg, "configuration": map[string]any{"blocking": false}}})
	if err != nil {
		return nil, "", err
	}
	if running, err = runningTaskID(resp); err != nil {
		return nil, "", err
	}
	for wait := pollFirst; running != ""; wait = min(wait*2, pollMax) {
		select {
		case <-ctx.Done():
			return nil, running, fmt.Errorf("task %s still running when the forward's deadline passed: %w", running, ctx.Err())
		case <-time.After(wait):
		}
		resp, err = s.agentRPC(ctx, map[string]any{"jsonrpc": "2.0", "id": "chat", "method": "tasks/get",
			"params": map[string]any{"id": running}})
		if err != nil {
			return nil, running, err
		}
		if _, ok := field(resp, "result").(map[string]any); !ok {
			return nil, running, fmt.Errorf("tasks/get answered no task: %s", truncate(resp, 200))
		}
		id, err := runningTaskID(resp)
		if err != nil {
			return nil, running, err
		}
		if id == "" {
			running = ""
		}
	}
	return resp, "", nil
}

// runningTaskID is the id of the task an A2A response carries while the agent is still working on it,
// or "" when it carries none. A running task without an id cannot be polled or cancelled, so it is an error.
func runningTaskID(resp any) (string, error) {
	result, ok := field(resp, "result").(map[string]any)
	if !ok {
		return "", nil
	}
	if state := field(result["status"], "state"); state != "submitted" && state != "working" {
		return "", nil
	}
	if id, _ := result["id"].(string); id != "" {
		return id, nil
	}
	return "", errors.New("the agent answered a running task with no id")
}

// cancelTask sends the agent tasks/cancel for a /chat task the forward gave up on, so the agent stops
// working on a turn nobody is waiting for. It runs after the turn has answered, and logs either way.
func (s *Server) cancelTask(ctx context.Context, taskID, reason string) {
	ctx, cancel := context.WithTimeout(ctx, s.agentTimeout)
	defer cancel()
	resp, err := s.agentRPC(ctx, map[string]any{"jsonrpc": "2.0", "id": "cancel", "method": "tasks/cancel",
		"params": map[string]any{"id": taskID}})
	if err != nil {
		s.log.Error(fmt.Sprintf("could not cancel the agent's task %s: %v", taskID, err), "event", "task_cancel_failed")
		return
	}
	result, ok := field(resp, "result").(map[string]any)
	if !ok {
		errBody := field(resp, "error")
		if errBody == nil {
			errBody = resp
		}
		s.log.Error(fmt.Sprintf("the agent did not cancel task %s: %s", taskID, truncate(errBody, 200)), "event", "task_cancel_failed")
		return
	}
	s.log.Info(fmt.Sprintf("cancelled the agent's task %s (state %v) after the chat forward failed: %s",
		taskID, field(result["status"], "state"), reason), "event", "task_cancel")
}

// truncate is v as JSON, cut to n bytes, for a log line.
func truncate(v any, n int) string {
	b, _ := json.Marshal(v)
	if len(b) > n {
		b = b[:n]
	}
	return string(b)
}

// agentRPC posts one JSON-RPC request to the agent root and returns the decoded reply. Only a JSON
// Content-Type travels with it.
func (s *Server) agentRPC(ctx context.Context, rpc map[string]any) (any, error) {
	body, err := json.Marshal(rpc)
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, s.agentURL+"/", bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	res, err := s.client.Do(req)
	if err != nil {
		return nil, err
	}
	defer res.Body.Close()
	out, err := io.ReadAll(res.Body)
	if err != nil {
		return nil, err
	}
	if res.StatusCode < 200 || res.StatusCode > 299 {
		return nil, fmt.Errorf("HTTP %d from agent", res.StatusCode)
	}
	var v any
	if err := json.Unmarshal(out, &v); err != nil {
		return nil, fmt.Errorf("agent reply is not JSON: %w", err)
	}
	return v, nil
}

// contextID is the A2A contextId for a session, with its self-heal generation suffix.
func (s *Server) contextID(session string) string {
	s.ctxMu.Lock()
	defer s.ctxMu.Unlock()
	if g := s.ctxGen[session]; g > 0 {
		return fmt.Sprintf("%s-g%d", session, g)
	}
	return session
}

// rotateContext moves a session to a fresh contextId and returns its new generation.
func (s *Server) rotateContext(session string) int {
	s.ctxMu.Lock()
	defer s.ctxMu.Unlock()
	s.ctxGen[session]++
	return s.ctxGen[session]
}

// replyText is the agent's reply from an A2A response. kagent echoes the same text into artifacts and the
// agent-role history, so it takes one source, artifacts first, then agent history, then the status
// message, rather than joining them and doubling the reply.
func replyText(resp any) string {
	result, ok := field(resp, "result").(map[string]any)
	if !ok {
		return ""
	}
	var art []string
	for _, a := range asList(result["artifacts"]) {
		art = append(art, textOf(asList(field(a, "parts"))))
	}
	if t := strings.TrimSpace(strings.Join(art, " ")); t != "" {
		return t
	}
	var hist []string
	for _, h := range asList(result["history"]) {
		if field(h, "role") == "agent" {
			hist = append(hist, textOf(asList(field(h, "parts"))))
		}
	}
	if t := strings.TrimSpace(strings.Join(hist, " ")); t != "" {
		return t
	}
	return strings.TrimSpace(textOf(asList(field(field(result["status"], "message"), "parts"))))
}

// isDanglingToolUse is the Bedrock error for a tool_use without its tool_result: poisoned history.
func isDanglingToolUse(reply string) bool {
	return strings.Contains(reply, "tool_use") && strings.Contains(reply, "tool_result") &&
		strings.Contains(reply, "ValidationException")
}

// thinkRE matches a scratchpad block. Nova varies the tag (<Thinking>, </thinking >, inner padding) and
// sometimes never closes it, so an unclosed block runs to the end of the reply (guard-proxy-agenticburn#10).
// Group 1 is the scratchpad's contents.
var thinkRE = regexp.MustCompile(`(?is)<\s*thinking\s*>(.*?)(?:<\s*/\s*thinking\s*>|$)`)

// isThinkingOnly is true when stripping the scratchpad leaves nothing a student could read.
func isThinkingOnly(reply string) bool {
	if reply == "" {
		return false
	}
	return utf8.RuneCountInString(strings.TrimSpace(thinkRE.ReplaceAllString(reply, ""))) < 2
}

// scratchpad is the contents of every scratchpad block, each trimmed, joined with spaces.
func scratchpad(reply string) string {
	var parts []string
	for _, m := range thinkRE.FindAllStringSubmatch(reply, -1) {
		parts = append(parts, strings.TrimSpace(m[1]))
	}
	return strings.Join(parts, " ")
}
