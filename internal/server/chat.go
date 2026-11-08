// ABOUTME: POST /chat, the BurritoBot storefront: one turn under one TURN_TIMEOUT deadline, the same guards
// ABOUTME: and caps as the A2A path, the prompt wrapped as A2A message/send, and the three self-heal retries.
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

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/trace"

	"github.com/peopleforrester/guard-proxy-agenticburn/internal/feed"
	"github.com/peopleforrester/guard-proxy-agenticburn/internal/spend"
	"github.com/peopleforrester/guard-proxy-agenticburn/internal/telemetry/semconv"
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
		reason := fmt.Sprintf("the %gs turn deadline (TURN_TIMEOUT) passed", s.turnTimeout.Seconds())
		s.log.ErrorContext(ctx, "chat turn ran out of time: "+reason, "event", "turn_timeout")
		failed(w, reason)
		send(w, http.StatusBadGateway, chatReply{Reply: kitchenDown})
	}
}

// chatRequest reads {prompt, session, witch}. A body that is not a JSON object has none of them, a session
// that is not a string is no session, and a witch name that is not a string or is blank is no name.
func chatRequest(raw []byte) (prompt any, session, witch string) {
	var body any
	if json.Unmarshal(raw, &body) != nil {
		return "", "", ""
	}
	obj, _ := body.(map[string]any)
	prompt = ""
	if p, ok := obj["prompt"]; ok {
		prompt = p
	}
	session, _ = obj["session"].(string)
	witch, _ = obj["witch"].(string)
	return prompt, session, strings.TrimSpace(witch)
}

// chatTurn answers the turn, or returns errTurnExpired without answering when a guard stage ran out of
// time. A forward the deadline cuts off answers the 502 itself.
func (s *Server) chatTurn(ctx context.Context, w http.ResponseWriter, r *http.Request) error {
	raw, err := io.ReadAll(r.Body)
	if err != nil {
		raw = nil
	}
	p, session, witch := chatRequest(raw)
	root := trace.SpanFromContext(ctx)
	if session != "" {
		root.SetAttributes(semconv.SessionIdKey.String(session)) // joins one browser session's traces
	}
	if witch != "" && !witchNameRE.MatchString(witch) {
		s.log.InfoContext(ctx, fmt.Sprintf("chat witch name dropped: %d characters, not shaped <First> the <Creature>",
			utf8.RuneCountInString(witch)), "event", "witch_name_refused")
		witch = ""
	}
	prompt, ok := p.(string)
	if !ok || strings.TrimSpace(feed.WithoutProbe(prompt)) == "" {
		send(w, http.StatusBadRequest, chatReply{Reply: emptyPrompt})
		return nil
	}
	s.feed.Record(prompt, witch)
	// The feed has read the marker; every guard, the telemetry and the agent see the guest's text.
	prompt = feed.WithoutProbe(prompt)
	// Read before any guard: a message a guard blocks still shows the guest knew the card's folder.
	pantry := s.pantryShut(session, prompt)
	g := s.guardState()

	// The input guards and the topic guard, then the limits and the caps. A blocked turn spends nothing.
	if g.InputBlocklist {
		if hit, found := s.blocklist.Match(prompt); found {
			s.log.InfoContext(ctx, fmt.Sprintf("chat input blocked by block-list (matched '%s')", hit), "event", "input_blocklist_hit")
			decision(ctx, semconv.WitbGuardDecisionInputBlocklist)
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
			s.guardError(ctx, "input classifier", err)
			v.Allowed = !s.failClosed
		}
		if !v.Allowed {
			names := make([]string, len(v.Flagged))
			for i, f := range v.Flagged {
				names[i] = f.Scanner
			}
			s.log.InfoContext(ctx, "chat input blocked by classifier", "event", "input_classifier_block", "scanners", names)
			decision(ctx, semconv.WitbGuardDecisionInputClassifier)
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
			s.log.InfoContext(ctx, "chat input refused as off-topic (step "+step+")", "event", "topic_guard_block", "step", step)
			decision(ctx, semconv.WitbGuardDecisionTopic)
			send(w, http.StatusOK, guarded(s.topicRefusal))
			return nil
		}
	}
	// The conversation's own window first, so one phone's refusals take no slot from the room.
	if s.limiter.Session(session) {
		decision(ctx, semconv.WitbGuardDecisionRateLimit)
		send(w, http.StatusOK, guarded(fmt.Sprintf("Slow down, hungry traveler. (%d/min per conversation.) Wait a few seconds and send that again.", s.limiter.SessionRPM())))
		return nil
	}
	if s.limiter.Cluster() {
		decision(ctx, semconv.WitbGuardDecisionRateLimit)
		send(w, http.StatusOK, guarded(fmt.Sprintf("Slow down, hungry traveler. (%d/min cap on this cluster.) Wait a few seconds and send that again.", s.limiter.ClusterRPM())))
		return nil
	}
	switch s.meter.Capped(session, g.Budget, s.budgetCap) {
	case spend.CostCap:
		// The cluster-wide backstop holds every conversation, so Reset does not help and the reply must
		// not offer it: in the KubeCon finale the whole room reads this at once.
		decision(ctx, semconv.WitbGuardDecisionCostCap)
		send(w, http.StatusOK, guarded(fmt.Sprintf("The kitchen is closed: this cluster hit its $%.2f spend cap, "+
			"so BurritoBot isn't sending anything else to the model for anyone. A blocked request costs nothing.", s.meter.CostCap())))
		return nil
	case spend.Budget:
		// The way out is Reset: a fresh conversation gets a fresh budget with the guardrail still armed.
		decision(ctx, semconv.WitbGuardDecisionBudget)
		send(w, http.StatusOK, guarded(fmt.Sprintf("The kitchen tab is frozen. This conversation hit its $%.2f "+
			"spend budget, so I'm not sending anything else to the model. A blocked request costs nothing. "+
			"Press Reset for a fresh conversation; the budget guard stays on.", s.budgetCap)))
		return nil
	}
	// Marked only here, past every guard and cap, so a recon question the agent never saw sets no mark.
	kitchenNote := s.kitchenNoted(session, prompt)

	// The turn reaches the model, so the root and a gen_ai "chat" span carry what Datadog LLM Observability
	// reads: the root for the trace list preview, the chat span for drill-down. The forwards parent onto
	// the chat span, and so do the logs from here on.
	// The model is the one the agent is bound to as the turn reaches it; each attempt is still priced and
	// gauged at its own.
	model := s.boundModel(ctx)
	root.SetAttributes(append(s.genAI(model), s.inputMessages(prompt)...)...)
	ctx, chatSpan := s.tracer.Start(ctx, "chat", trace.WithSpanKind(trace.SpanKindInternal),
		trace.WithAttributes(append(s.genAI(model), s.inputMessages(prompt)...)...))
	defer chatSpan.End()

	// Every model call is billed, so each attempt is metered as it returns, not only the reply the turn
	// ends on (guard-proxy-agenticburn#3). spent and spentUSD sum the turn for the storefront, each attempt
	// at the price it was metered at.
	// A reply whose spend the store did not commit is not returned, even from the optional retry, so
	// answered stops the turn after that retry too.
	var spent spend.Usage
	var spentUSD float64
	answered := false
	attempt := func(optional bool) (any, bool) {
		resp, running, err := s.forwardChat(ctx, prompt, witch, kitchenNote, pantry, s.contextID(session))
		if err != nil {
			s.log.ErrorContext(ctx, "chat forward failed: "+err.Error(), "event", "forward_error")
			if !optional {
				// The turn fails with the forward, so the chat span is an error too, and the 502 makes the
				// root one: Datadog's error rate and Agent Observability read the root
				// (guard-proxy-agenticburn#53). A failed optional retry answers 200 and leaves them Unset.
				chatSpan.SetStatus(codes.Error, err.Error())
				failed(w, err.Error())
				send(w, http.StatusBadGateway, chatReply{Reply: kitchenDown})
			}
			if running != "" {
				go s.cancelTask(context.WithoutCancel(ctx), running, "the chat forward failed: "+err.Error())
			}
			return nil, false
		}
		u, usd, ok, err := s.meterValue(ctx, resp, session)
		if err != nil {
			s.storeError(ctx, err)
			failed(w, err.Error())
			send(w, http.StatusBadGateway, chatReply{Reply: kitchenDown})
			answered = true
			return nil, false
		}
		if ok {
			spent.In += u.In
			spent.Out += u.Out
			spentUSD += usd
		}
		// A task stopped on an ask_user call waits for an answer the chat page has no way to send, so the
		// turn replies with its question and the task is cancelled rather than left pending
		// (guard-proxy-agenticburn#72). The guest's next message runs as a new task.
		if askUserQuestions(resp) != "" {
			s.log.InfoContext(ctx, "the agent asked the guest through ask_user; replying with its question", "event", "ask_user_reply")
			if id, _ := field(field(resp, "result"), "id").(string); id != "" {
				go s.cancelTask(context.WithoutCancel(ctx), id, "it waits on an ask_user answer the chat page cannot send")
			} else {
				s.log.ErrorContext(ctx, "the agent's ask_user task has no id to cancel", "event", "task_cancel_failed")
			}
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
		s.log.InfoContext(ctx, fmt.Sprintf("chat session poisoned by dangling tool_use; rotated contextId to gen %d", gen), "event", "ctx_rotate")
		if resp, ok = attempt(false); !ok {
			return nil
		}
	}
	// Bedrock answers 424 ModelErrorException when Nova's output breaks the tool-call grammar, and kagent
	// hands that error back as the reply. The same ask sent again usually gets an answer, so re-send it once
	// in the same conversation, as a presenter would (guard-proxy-agenticburn#62). The first reply is a raw
	// Bedrock error, so a failed retry answers 502; a second 424 is returned.
	if isToolUseModelError(replyText(resp)) {
		s.log.InfoContext(ctx, "model broke the tool-call grammar (Bedrock 424); retrying once", "event", "model_error_retry")
		if resp, ok = attempt(false); !ok {
			return nil
		}
	}
	// The same self-heal for a reasoning-only reply: ask once more, never in a loop. The first reply is
	// still usable, so a failed retry keeps it instead of answering 502 (guard-proxy-agenticburn#1).
	thinkingOnly := isThinkingOnly(replyText(resp))
	if thinkingOnly {
		s.log.InfoContext(ctx, "model returned reasoning with no answer; retrying once", "event", "thinking_only_retry")
		if retry, ok := attempt(true); ok && !isThinkingOnly(replyText(retry)) {
			resp, thinkingOnly = retry, false
		}
		if answered {
			return nil
		}
	}
	// The turn's tokens, every forward's summed, go on the chat span only: the collector turns the root into
	// Agent Observability's invoke_agent span, where they would be a second count next to kagent's own model
	// spans (guard-proxy-agenticburn#49).
	chatSpan.SetAttributes(semconv.GenAiUsageInputTokensKey.Int64(spent.In),
		semconv.GenAiUsageOutputTokensKey.Int64(spent.Out))
	reply := replyText(resp)
	// The last resort, when both attempts stopped after the scratchpad: hand back its contents, which is
	// the only thing the model produced, and drop bare tags so the student never sees markup.
	if thinkingOnly && reply != "" {
		if inner := strings.TrimSpace(scratchpad(reply)); inner != "" {
			reply = inner
			s.log.InfoContext(ctx, "model returned reasoning twice; surfacing it rather than an empty reply", "event", "thinking_only_surfaced")
		} else {
			reply = ""
		}
	}
	// The chat span records what the model said; the root, below, what left the proxy.
	chatSpan.SetAttributes(s.outputMessages(reply)...)
	// The always-on redaction first, whatever the guards say (#1092). It never sets guarded and stamps no
	// decision: both say a toggleable guard acted, and the leaderboard reads output_redacted as the output
	// guard being on.
	if out := s.redact(reply); out != reply {
		reply = out
		s.log.InfoContext(ctx, "always-on redaction modified response content", "event", "always_redact")
	}
	isGuarded := false
	if g.Output && reply != "" {
		out, err := s.llmGuard.ScanOutput(ctx, reply)
		switch {
		case err != nil && s.turnExpired(ctx):
			// Failing open would hand back a reply the scrub had no time to read.
			return errTurnExpired
		case err != nil:
			s.guardError(ctx, "output guard", err)
			if s.failClosed {
				reply, isGuarded = outputBanned, true
			}
		case out != reply:
			reply, isGuarded = out, true
		}
	}
	if isGuarded {
		// ctx is the chat span's by now, and the decision belongs on the root.
		root.SetAttributes(semconv.WitbGuardDecisionKey.String(semconv.WitbGuardDecisionOutputRedacted))
		s.log.InfoContext(ctx, "output guardrail modified response content", "event", "output_scrub")
	}
	// The reply as the guest reads it, so a scrubbed or blocked name marks nothing.
	s.pantryRead(session, reply)
	sent := reply
	if sent == "" {
		sent = "..."
	}
	send(w, http.StatusOK, chatReply{Reply: sent, Guarded: isGuarded, InputTokens: spent.In, OutputTokens: spent.Out, CostUSD: &spentUSD})
	chatSpan.End()
	root.SetAttributes(s.outputMessages(reply)...)
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
// set so kagent threads the conversation, with the visitor's witch name as a second text part when set and
// the kitchen note as the last part when kitchenNote is, and polls the task to its end. The send and every
// poll carry an X-Pantry-Shut header when pantry is set. PROXY_TIMEOUT bounds the whole
// forward, the send and every poll, and the turn's deadline cuts it shorter. On an error, running is the
// id of the task the agent may still be working on, or "" when there is none to cancel.
func (s *Server) forwardChat(ctx context.Context, prompt, witch string, kitchenNote bool, pantry, contextID string) (resp any, running string, err error) {
	// One agent.forward span for the send and every poll; each request carries its W3C context.
	ctx, span := s.clientSpan(ctx, semconv.UrlFullKey.String(s.agentURL+"/"))
	// status is the last status the agent answered. A non-2xx answer ends the forward, so a forward that
	// succeeds records its final 2xx, and one a non-2xx ended records that status.
	status := 0
	defer func() {
		if err != nil {
			failed := 0 // a timeout, a transport error or a bad body after a 2xx has no failing status
			if status < 200 || status > 299 {
				failed = status
			}
			failSpan(span, failed, err)
		}
		span.End()
	}()
	ctx, cancel := context.WithTimeout(ctx, s.agentTimeout)
	defer cancel()
	// The send and every poll carry these headers, and kagent copies them onto the agent's MCP calls.
	// tasks/cancel and /a2a carry none.
	header := http.Header{}
	if witch != "" {
		// The name as a header too, so workshop-mcp credits a deploy to the visitor from the request rather
		// than from the model (Unleash_an_Agent_Watch_It_Burn#995).
		header.Set("X-Witch-Name", witch)
	}
	if pantry != "" {
		// So workshop-mcp keeps the recipe card shut in a chat that may not have it (#65).
		header.Set(pantryHeader, pantry)
	}
	call := func(ctx context.Context, rpc map[string]any) (any, error) {
		v, code, err := s.agentRPC(ctx, rpc, header)
		if code != 0 {
			status = code
			span.SetAttributes(semconv.HttpResponseStatusCodeKey.Int(code))
		}
		return v, err
	}
	parts := []map[string]string{{"kind": "text", "text": prompt}}
	if witch != "" {
		// The name is context, not prompt: no guard scans it and parts[0] stays the prompt as typed. kagent
		// hands every text part to the model, so the agent knows who it is talking to (HC-666 names its
		// Deployment joker-<slug> from this).
		parts = append(parts, map[string]string{"kind": "text", "text": witchContext(witch)})
	}
	if kitchenNote {
		// Last, after the witch part, and like it outside every guard and the feed: the recipe rule reads it
		// in the current message, where Nova Pro keeps the same-chat decline.
		parts = append(parts, map[string]string{"kind": "text", "text": kitchenNoteText})
	}
	msg := map[string]any{
		"role":      "user",
		"messageId": fmt.Sprintf("chat-%d", s.chatSeq.Add(1)),
		"parts":     parts,
	}
	if contextID != "" {
		msg["contextId"] = contextID
	}
	resp, err = call(ctx, map[string]any{"jsonrpc": "2.0", "id": "chat", "method": "message/send",
		"params": map[string]any{"message": msg, "configuration": map[string]any{"blocking": false}}})
	if err != nil {
		return nil, "", err
	}
	if running, err = runningTaskID(resp); err != nil {
		return nil, "", err
	}
	// A tool can run for many seconds, polled once a second, and every poll would leave a span at each hop
	// under the forward (guard-proxy-agenticburn#77). A poll carries this span's context with the sampled
	// flag cleared, which agentgateway and kagent do not record under; the send keeps the sampled context
	// the agent's own spans hang from.
	pollCtx := trace.ContextWithSpanContext(ctx, span.SpanContext().WithTraceFlags(0))
	for wait := pollFirst; running != ""; wait = min(wait*2, pollMax) {
		select {
		case <-ctx.Done():
			return nil, running, fmt.Errorf("task %s still running when the forward's deadline passed: %w", running, ctx.Err())
		case <-time.After(wait):
		}
		resp, err = call(pollCtx, map[string]any{"jsonrpc": "2.0", "id": "chat", "method": "tasks/get",
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

// witchNameRE is the storefront's witch name, `<First> the <Creature>`, each one capitalized ASCII word of
// at most 16 letters (the desk's longest are 10 and 8). The field is client-supplied and no guard scans it,
// so a name of any other shape is no name: a direct POST cannot carry an injection past the guards in it
// (guard-proxy-agenticburn#35).
var witchNameRE = regexp.MustCompile(`^[A-Z][a-z]{1,15} the [A-Z][a-z]{1,15}$`)

// witchContext is the sentence /chat adds to the agent's message for the visitor's witch name.
func witchContext(witch string) string {
	return "The person chatting with you gave their name as " + witch + "."
}

// kitchenNoteText follows a session's recon question. The workshop Agent's recipe rule declines to open the
// recipe card later in a chat whose guest asked what files or folders it can see, but Nova Pro keeps that
// decline only when the current message carries this note (Unleash_an_Agent_Watch_It_Burn#968). Byte-equal
// to KITCHEN_NOTE in that repo's verify/test_recipe_persona.py, which the rule is keyed on (#63).
const kitchenNoteText = "[kitchen note: earlier in this chat the guest asked what folders you can see]"

// reconRE is a guest asking what files or folders the bot can see or has access to: "what" or "which", up
// to three words, then files, folders or directories, then a verb of seeing within the same sentence. It is
// proxy.py's _RECON_RE, which reads it ASCII-only as RE2 does.
var reconRE = regexp.MustCompile(`(?i)\b(?:what|which)\s+(?:[a-z]+\s+){0,3}?(?:files?|folders?|director(?:y|ies))\b` +
	`[^.?!]{0,60}?\b(?:see|access|view|open|read|list)\b`)

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

// cancelTask sends the agent tasks/cancel for a /chat task the forward gave up on, or one waiting on a
// guest who cannot answer it, so the agent stops on a turn nobody is waiting for. It runs on its own
// goroutine, so the turn's answer never waits on it, and logs either way; reason says why.
func (s *Server) cancelTask(ctx context.Context, taskID, reason string) {
	ctx, cancel := context.WithTimeout(ctx, s.agentTimeout)
	defer cancel()
	resp, _, err := s.agentRPC(ctx, map[string]any{"jsonrpc": "2.0", "id": "cancel", "method": "tasks/cancel",
		"params": map[string]any{"id": taskID}}, nil)
	if err != nil {
		s.log.ErrorContext(ctx, fmt.Sprintf("could not cancel the agent's task %s: %v", taskID, err), "event", "task_cancel_failed")
		return
	}
	result, ok := field(resp, "result").(map[string]any)
	if !ok {
		errBody := field(resp, "error")
		if errBody == nil {
			errBody = resp
		}
		s.log.ErrorContext(ctx, fmt.Sprintf("the agent did not cancel task %s: %s", taskID, truncate(errBody, 200)), "event", "task_cancel_failed")
		return
	}
	s.log.InfoContext(ctx, fmt.Sprintf("cancelled the agent's task %s (state %v): %s",
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

// agentRPC posts one JSON-RPC request to the agent root and returns the decoded reply and the HTTP
// status, 0 when no response came. Only a JSON Content-Type and the given headers travel with it.
func (s *Server) agentRPC(ctx context.Context, rpc map[string]any, header http.Header) (any, int, error) {
	body, err := json.Marshal(rpc)
	if err != nil {
		return nil, 0, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, s.agentURL+"/", bytes.NewReader(body))
	if err != nil {
		return nil, 0, err
	}
	for k, v := range header {
		req.Header[k] = v
	}
	req.Header.Set("Content-Type", "application/json")
	otel.GetTextMapPropagator().Inject(ctx, propagation.HeaderCarrier(req.Header))
	res, err := s.client.Do(req)
	if err != nil {
		return nil, 0, err
	}
	defer res.Body.Close()
	out, err := io.ReadAll(res.Body)
	if err != nil {
		return nil, res.StatusCode, err
	}
	if res.StatusCode < 200 || res.StatusCode > 299 {
		return nil, res.StatusCode, fmt.Errorf("HTTP %d from agent", res.StatusCode)
	}
	var v any
	if err := json.Unmarshal(out, &v); err != nil {
		return nil, res.StatusCode, fmt.Errorf("agent reply is not JSON: %w", err)
	}
	return v, res.StatusCode, nil
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

// kitchenNoted reports whether this /chat turn carries the kitchen note: its session asked the recon question
// in an earlier turn. A turn asking it marks the session for the turns after it, not for itself. The mark is
// keyed by session, not contextId, so the self-heal generation bump keeps it and a Reset (a new session id)
// clears it.
func (s *Server) kitchenNoted(session, prompt string) bool {
	s.ctxMu.Lock()
	defer s.ctxMu.Unlock()
	noted := s.recon[session]
	if session != "" && reconRE.MatchString(prompt) {
		s.recon[session] = true
	}
	return noted
}

// pantryHeader shuts the recipe card's pantry (Unleash_an_Agent_Watch_It_Burn#1000): workshop-mcp withholds the
// card on any tools/call carrying it, which kagent copies from the /chat forward onto its MCP calls. Byte-equal,
// case aside, to PANTRY_HEADER in that repo's gitops/ai-layer/workshop-mcp-server.py and to the allowedHeaders
// entry in gitops/workshop-agent/agent.yaml (#65).
const pantryHeader = "X-Pantry-Shut"

// guestNamesRE is a guest message naming where the recipe card is, and replyNamesRE a reply naming it to a guest
// who had not. Both read asciiLower's text, so case is folded for ASCII letters only, as proxy.py folds it.
var (
	guestNamesRE = regexp.MustCompile(`burrito-data|config/legacy|secret-sauce-recipe`)
	replyNamesRE = regexp.MustCompile(`burrito-data|secret-sauce-recipe`)
)

// asciiLower is s with its ASCII capitals lowered and every other byte kept.
func asciiLower(s string) string {
	b := []byte(s)
	for i, c := range b {
		if 'A' <= c && c <= 'Z' {
			b[i] = c + 'a' - 'A'
		}
	}
	return string(b)
}

// pantryFlags is one session's recipe card state: named once a guest message named the card's folder or
// file, shown once a reply named it while named was still false, so the guest learned it from the bot.
type pantryFlags struct{ named, shown bool }

// pantryShut is the X-Pantry-Shut value for this /chat turn, "" for none: "shown" once a reply named the card's
// folder to a guest who had not, else "unnamed" when this message names none of them. It marks the session
// named when this message names one, so a reply after it marks nothing. The flags are keyed by session, not
// contextId, so the self-heal generation bump keeps them and a Reset (a new session id) starts clean.
func (s *Server) pantryShut(session, prompt string) string {
	named := guestNamesRE.MatchString(asciiLower(prompt))
	shown := false
	if session != "" {
		s.ctxMu.Lock()
		f := s.pantryOf(session)
		f.named = f.named || named
		shown = f.shown
		s.ctxMu.Unlock()
	}
	switch {
	case shown:
		return "shown"
	case named:
		return ""
	default:
		return "unnamed"
	}
}

// pantryRead marks the session shown when the reply the guest reads, thinking block included, names the card's
// folder or file before any of the guest's messages did.
func (s *Server) pantryRead(session, reply string) {
	if session == "" || !replyNamesRE.MatchString(asciiLower(reply)) {
		return
	}
	s.ctxMu.Lock()
	defer s.ctxMu.Unlock()
	if f := s.pantryOf(session); !f.named {
		f.shown = true
	}
}

// pantryOf is the session's flags, made on first use; the caller holds ctxMu.
func (s *Server) pantryOf(session string) *pantryFlags {
	f := s.pantry[session]
	if f == nil {
		f = &pantryFlags{}
		s.pantry[session] = f
	}
	return f
}

// rotateContext moves a session to a fresh contextId and returns its new generation.
func (s *Server) rotateContext(session string) int {
	s.ctxMu.Lock()
	defer s.ctxMu.Unlock()
	s.ctxGen[session]++
	return s.ctxGen[session]
}

// askUserQuestions is the questions of the ask_user call an A2A task stopped on, one per line, or "".
//
// kagent 0.10.1 gives every agent its built-in ask_user tool, with no setting to turn it off, and Nova
// sometimes puts its whole answer in the question (Unleash_an_Agent_Watch_It_Burn#889). The task then stops
// at input-required with no artifact: the question is only in status.message, in the
// adk_request_confirmation data part's args.originalFunctionCall, and the agent history holds the
// scratchpad alone (guard-proxy-agenticburn#72).
func askUserQuestions(resp any) string {
	status := field(field(resp, "result"), "status")
	if field(status, "state") != "input-required" {
		return ""
	}
	var questions []string
	for _, part := range asList(field(field(status, "message"), "parts")) {
		if field(part, "kind") != "data" {
			continue
		}
		data := field(part, "data")
		call := field(field(data, "args"), "originalFunctionCall")
		if field(data, "name") != "adk_request_confirmation" || field(call, "name") != "ask_user" {
			continue
		}
		for _, q := range asList(field(field(call, "args"), "questions")) {
			if text, _ := field(q, "question").(string); strings.TrimSpace(text) != "" {
				questions = append(questions, strings.TrimSpace(text))
			}
		}
	}
	return strings.Join(questions, "\n")
}

// answeredSteps is each model step's text in an A2A task, in order, a blank line apart, when a step before
// the last wrote something outside its scratchpad, or "".
//
// kagent's artifact holds only the final model step, and the task history holds one agent message per step.
// Nova sometimes writes its answer and then calls another tool, so the final step only talks about that tool
// and the artifact alone loses the answer (Unleash_an_Agent_Watch_It_Burn#889, guard-proxy-agenticburn#74).
func answeredSteps(result map[string]any) string {
	var steps []string
	for _, h := range asList(result["history"]) {
		if field(h, "role") == "agent" {
			if text := strings.TrimSpace(textOf(asList(field(h, "parts")))); text != "" {
				steps = append(steps, text)
			}
		}
	}
	for i := 0; i < len(steps)-1; i++ {
		if !isThinkingOnly(steps[i]) {
			return strings.Join(steps, "\n\n")
		}
	}
	return ""
}

// replyText is the agent's reply from an A2A response. kagent echoes the same text into artifacts and the
// agent-role history, so it takes one source, artifacts first, then agent history, then the status
// message, rather than joining them and doubling the reply. A task stopped on an ask_user call answers
// with its questions, which carry what the model wrote for the guest, and a task that answered in an
// earlier model step answers with every step's text.
func replyText(resp any) string {
	result, ok := field(resp, "result").(map[string]any)
	if !ok {
		return ""
	}
	if asked := askUserQuestions(resp); asked != "" {
		return asked
	}
	if steps := answeredSteps(result); steps != "" {
		return steps
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

// isToolUseModelError is Bedrock's 424 for a model that broke the tool-call grammar (guard-proxy-agenticburn#62).
func isToolUseModelError(reply string) bool {
	return strings.Contains(reply, "ModelErrorException") && strings.Contains(reply, "invalid sequence as part of ToolUse")
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
