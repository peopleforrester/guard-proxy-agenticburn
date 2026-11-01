// ABOUTME: The structured logger: one JSON object per line with timestamp, level, logger, message, an
// ABOUTME: optional event name, and the active span's trace_id and span_id, the keys the reference emits.
// SPDX-License-Identifier: Apache-2.0
package logging

import (
	"context"
	"io"
	"log/slog"
	"time"

	"go.opentelemetry.io/otel/trace"
)

// Name is the value of every line's "logger" key.
const Name = "guard-proxy"

// New returns a logger writing INFO and above to w.
func New(w io.Writer) *slog.Logger {
	h := slog.NewJSONHandler(w, &slog.HandlerOptions{
		Level: slog.LevelInfo,
		ReplaceAttr: func(groups []string, a slog.Attr) slog.Attr {
			if len(groups) > 0 {
				return a
			}
			switch a.Key {
			case slog.TimeKey:
				return slog.String("timestamp", a.Value.Time().UTC().Format(time.RFC3339Nano))
			case slog.MessageKey:
				return slog.Attr{Key: "message", Value: a.Value}
			case slog.LevelKey:
				// Python's name for this level, so a log facet matches either implementation.
				if a.Value.String() == slog.LevelWarn.String() {
					return slog.String(slog.LevelKey, "WARNING")
				}
			}
			return a
		},
	})
	return slog.New(spanHandler{h}).With("logger", Name)
}

// spanHandler adds the trace_id and span_id of the span in a record's context, in the OTel-standard
// lowercase hex Datadog recognizes, so a guard-decision log joins its trace. A record logged without a
// context, or outside a valid span, carries neither.
type spanHandler struct{ slog.Handler }

func (h spanHandler) Handle(ctx context.Context, r slog.Record) error {
	if sc := trace.SpanContextFromContext(ctx); sc.IsValid() {
		r.AddAttrs(slog.String("trace_id", sc.TraceID().String()), slog.String("span_id", sc.SpanID().String()))
	}
	return h.Handler.Handle(ctx, r)
}

func (h spanHandler) WithAttrs(attrs []slog.Attr) slog.Handler {
	return spanHandler{h.Handler.WithAttrs(attrs)}
}

func (h spanHandler) WithGroup(name string) slog.Handler {
	return spanHandler{h.Handler.WithGroup(name)}
}
