// ABOUTME: Tests for the structured log line shape, which Datadog log facets and monitors key on and
// ABOUTME: which the conformance suite reads back to see each guard decision.
// SPDX-License-Identifier: Apache-2.0
package logging

import (
	"bytes"
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"

	"go.opentelemetry.io/otel/trace"
)

func lines(t *testing.T, buf *bytes.Buffer) []map[string]any {
	t.Helper()
	var out []map[string]any
	for _, l := range strings.Split(strings.TrimSpace(buf.String()), "\n") {
		var rec map[string]any
		if err := json.Unmarshal([]byte(l), &rec); err != nil {
			t.Fatalf("not a JSON line: %q: %v", l, err)
		}
		out = append(out, rec)
	}
	return out
}

func TestLineShape(t *testing.T) {
	var buf bytes.Buffer
	New(&buf).Error("agent forward failed: boom", "event", "forward_error")
	recs := lines(t, &buf)
	if len(recs) != 1 {
		t.Fatalf("got %d lines", len(recs))
	}
	rec := recs[0]
	want := map[string]any{
		"level":   "ERROR",
		"logger":  "guard-proxy",
		"message": "agent forward failed: boom",
		"event":   "forward_error",
	}
	for k, v := range want {
		if rec[k] != v {
			t.Errorf("%s = %v, want %v", k, rec[k], v)
		}
	}
	ts, ok := rec["timestamp"].(string)
	if !ok {
		t.Fatalf("timestamp missing: %v", rec)
	}
	if _, err := time.Parse(time.RFC3339Nano, ts); err != nil {
		t.Errorf("timestamp %q is not RFC 3339: %v", ts, err)
	}
	for _, k := range []string{"time", "msg"} {
		if _, ok := rec[k]; ok {
			t.Errorf("slog's own key %q leaked into the line", k)
		}
	}
}

func TestLevelNamesMatchTheReference(t *testing.T) {
	var buf bytes.Buffer
	l := New(&buf)
	l.Info("i")
	l.Warn("w")
	got := []any{}
	for _, rec := range lines(t, &buf) {
		got = append(got, rec["level"])
	}
	if len(got) != 2 || got[0] != "INFO" || got[1] != "WARNING" {
		t.Errorf("levels = %v, want [INFO WARNING]", got)
	}
}

func TestDebugIsNotEmitted(t *testing.T) {
	var buf bytes.Buffer
	New(&buf).Debug("access line")
	if buf.Len() != 0 {
		t.Errorf("debug line emitted: %q", buf.String())
	}
}

func TestLinesCarryTheSpanFromTheContext(t *testing.T) {
	var buf bytes.Buffer
	log := New(&buf)
	sc := trace.NewSpanContext(trace.SpanContextConfig{
		TraceID: trace.TraceID{0x0a, 0xf7, 0x65, 0x19, 0x16, 0xcd, 0x43, 0xdd, 0x84, 0x48, 0xeb, 0x21, 0x1c, 0x80, 0x31, 0x9c},
		SpanID:  trace.SpanID{0xb7, 0xad, 0x6b, 0x71, 0x69, 0x20, 0x33, 0x31},
	})
	log.InfoContext(trace.ContextWithSpanContext(context.Background(), sc), "in a span", "event", "e")
	log.InfoContext(context.Background(), "no span")
	lines := strings.Split(strings.TrimSpace(buf.String()), "\n")
	var in, out map[string]any
	if err := json.Unmarshal([]byte(lines[0]), &in); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal([]byte(lines[1]), &out); err != nil {
		t.Fatal(err)
	}
	if in["trace_id"] != "0af7651916cd43dd8448eb211c80319c" || in["span_id"] != "b7ad6b7169203331" {
		t.Errorf("in a span: %v", in)
	}
	if _, ok := out["trace_id"]; ok {
		t.Errorf("no span: %v", out)
	}
}
