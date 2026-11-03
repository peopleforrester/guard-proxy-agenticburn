// ABOUTME: The spans, attributes and cost gauge guard-proxy emits: the POST server span, the gen_ai message
// ABOUTME: envelopes in the reference's exact JSON form, the agent hop's error type, and gen_ai.client.cost.
// SPDX-License-Identifier: Apache-2.0
package server

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"strconv"
	"strings"
	"unicode/utf16"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/trace"

	"github.com/peopleforrester/guard-proxy-agenticburn/internal/telemetry/semconv"
)

// instrumentation is the tracer and meter scope name.
const instrumentation = "guard-proxy"

// statusWriter records the status a handler answered, for the server span.
type statusWriter struct {
	http.ResponseWriter
	code int
}

func (w *statusWriter) WriteHeader(code int) {
	w.code = code
	w.ResponseWriter.WriteHeader(code)
}

// serverSpan starts the POST <path> SERVER span, joined to the caller's W3C context, so the guard hop
// sits in the agentgateway to agent waterfall. The path is the raw request target, query included, as
// the reference records it. end records the status the handler answered and ends the span.
func (s *Server) serverSpan(w http.ResponseWriter, r *http.Request, uri string) (*statusWriter, *http.Request, func()) {
	ctx := otel.GetTextMapPropagator().Extract(r.Context(), propagation.HeaderCarrier(r.Header))
	ctx, span := s.tracer.Start(ctx, "POST "+uri, trace.WithSpanKind(trace.SpanKindServer),
		trace.WithAttributes(semconv.HttpRequestMethodKey.String(http.MethodPost), semconv.UrlPathKey.String(uri)))
	sw := &statusWriter{ResponseWriter: w, code: http.StatusOK}
	return sw, r.WithContext(ctx), func() {
		span.SetAttributes(semconv.HttpResponseStatusCodeKey.Int(sw.code))
		span.End()
	}
}

// genAI is the gen_ai attributes every LLM-kind span carries, so Datadog LLM Observability classifies it.
func (s *Server) genAI(model string) []attribute.KeyValue {
	return []attribute.KeyValue{
		semconv.GenAiOperationNameKey.String(semconv.GenAiOperationNameChat),
		semconv.GenAiRequestModelKey.String(model),
	}
}

// boundModel is gen_ai.request.model for a request made now: the model of the tier the agent is bound to,
// as the request is priced, or MODEL_NAME when set (guard-proxy-agenticburn#38).
func (s *Server) boundModel(ctx context.Context) string {
	return s.prices.ModelAt(s.binding.tier(ctx))
}

// inputMessages is gen_ai.input.messages for text, when content capture is on.
func (s *Server) inputMessages(text string) []attribute.KeyValue {
	if !s.capture {
		return nil
	}
	return []attribute.KeyValue{semconv.GenAiInputMessagesKey.String(
		`[{"role": "user", "parts": [{"type": "text", "content": ` + pyJSONString(text) + `}]}]`)}
}

// outputMessages is gen_ai.output.messages for text, when content capture is on and there is text.
func (s *Server) outputMessages(text string) []attribute.KeyValue {
	if !s.capture || text == "" {
		return nil
	}
	return []attribute.KeyValue{semconv.GenAiOutputMessagesKey.String(
		`[{"role": "assistant", "parts": [{"type": "text", "content": ` + pyJSONString(text) +
			`}], "finish_reason": "stop"}]`)}
}

// pyJSONString encodes s as Python's json.dumps does by default, so a message envelope is the same string
// from either implementation: ASCII only, with everything else as \u escapes and surrogate pairs.
func pyJSONString(s string) string {
	var b strings.Builder
	b.WriteByte('"')
	for _, r := range s {
		switch r {
		case '"':
			b.WriteString(`\"`)
		case '\\':
			b.WriteString(`\\`)
		case '\n':
			b.WriteString(`\n`)
		case '\r':
			b.WriteString(`\r`)
		case '\t':
			b.WriteString(`\t`)
		case '\b':
			b.WriteString(`\b`)
		case '\f':
			b.WriteString(`\f`)
		default:
			switch {
			case r < 0x20 || (r > 0x7e && r < 0x10000):
				if r == 0x7f {
					b.WriteRune(r)
					continue
				}
				fmt.Fprintf(&b, `\u%04x`, r)
			case r >= 0x10000:
				hi, lo := utf16.EncodeRune(r)
				fmt.Fprintf(&b, `\u%04x\u%04x`, hi, lo)
			default:
				b.WriteRune(r)
			}
		}
	}
	b.WriteByte('"')
	return b.String()
}

// clientSpan starts an agent.forward CLIENT span: the egress hop the Service Map draws to peerService.
func (s *Server) clientSpan(ctx context.Context, attrs ...attribute.KeyValue) (context.Context, trace.Span) {
	attrs = append([]attribute.KeyValue{semconv.PeerServiceKey.String(s.peerService),
		semconv.HttpRequestMethodKey.String(http.MethodPost)}, attrs...)
	return s.tracer.Start(ctx, "agent.forward", trace.WithSpanKind(trace.SpanKindClient), trace.WithAttributes(attrs...))
}

// failSpan marks a span whose call failed, with the HTTP status when the agent answered one.
func failSpan(span trace.Span, status int, err error) {
	if status != 0 {
		span.SetAttributes(semconv.HttpResponseStatusCodeKey.Int(status))
	}
	span.SetAttributes(semconv.ErrorTypeKey.String(errorType(status, err)))
	span.SetStatus(codes.Error, err.Error())
}

// errorType is error.type's value: the status code for an HTTP error, as the HTTP conventions name it,
// "timeout" for a deadline, and the conventions' "_OTHER" for anything else.
func errorType(status int, err error) string {
	var ne net.Error
	switch {
	case status != 0:
		return strconv.Itoa(status)
	case errors.Is(err, context.DeadlineExceeded) || (errors.As(err, &ne) && ne.Timeout()):
		return "timeout"
	}
	return "_OTHER"
}

// stamp sets an attribute on the request's server span. A blocked /chat turn ends before any agent span
// exists, so the root is the only span a dashboard can scope a session or name a guard from.
func stamp(ctx context.Context, kv ...attribute.KeyValue) {
	trace.SpanFromContext(ctx).SetAttributes(kv...)
}

// decision stamps which guard decided the turn.
func decision(ctx context.Context, value string) {
	stamp(ctx, semconv.WitbGuardDecisionKey.String(value))
}

// registerCostGauge exports the running spend as gen_ai.client.cost, beside the standard token metric
// the agent emits. The GenAI conventions define no monetary metric, so cost is a suffix under gen_ai.
//
// It has one series per model (guard-proxy-agenticburn#38): the workshop swaps the model live, so each
// series is what requests reported as that model have spent, and the series sum to the cluster's spend.
// The model the agent is bound to now always has a series, at 0 until it spends, so an idle cluster
// still reads $0.
func (s *Server) registerCostGauge() error {
	m := otel.Meter(instrumentation)
	gauge, err := m.Float64ObservableGauge("gen_ai.client.cost", metric.WithUnit("USD"),
		metric.WithDescription("Estimated Bedrock spend (USD) for this cluster, derived from token usage."))
	if err != nil {
		return err
	}
	provider := semconv.GenAiProviderNameKey.String(semconv.GenAiProviderNameAwsBedrock)
	_, err = m.RegisterCallback(func(ctx context.Context, o metric.Observer) error {
		byModel := s.meter.ByModel()
		bound := s.boundModel(ctx)
		if _, spent := byModel[bound]; !spent {
			byModel[bound] = 0
		}
		for model, usd := range byModel {
			o.ObserveFloat64(gauge, usd, metric.WithAttributes(semconv.GenAiRequestModelKey.String(model), provider))
		}
		return nil
	}, gauge)
	return err
}
