// ABOUTME: The structured logger: one JSON object per line with timestamp, level, logger, message and
// ABOUTME: an optional event name, the same keys the reference proxy emits.
// SPDX-License-Identifier: Apache-2.0
package logging

import (
	"io"
	"log/slog"
	"time"
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
	return slog.New(h).With("logger", Name)
}
