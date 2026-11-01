// ABOUTME: OpenTelemetry setup from the standard OTEL_* environment: the tracer and meter providers with
// ABOUTME: the OTLP/HTTP exporters, off unless an exporter is configured, and W3C propagation either way.
// SPDX-License-Identifier: Apache-2.0
package telemetry

import (
	"context"
	"errors"
	"fmt"
	"os"
	"strings"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetrichttp"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/propagation"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

// Telemetry is the installed providers. Its zero value is telemetry that is off.
type Telemetry struct {
	shutdown []func(context.Context) error
}

// Enabled reports whether any provider exports.
func (t *Telemetry) Enabled() bool { return len(t.shutdown) > 0 }

// Shutdown flushes and stops every provider.
func (t *Telemetry) Shutdown(ctx context.Context) error {
	var errs []error
	for _, f := range t.shutdown {
		errs = append(errs, f(ctx))
	}
	return errors.Join(errs...)
}

// Setup installs the global providers the environment asks for. Traces and metrics each export over
// OTLP/HTTP when an OTLP endpoint is set or their *_EXPORTER is "otlp", and not when it is "none". With
// neither, telemetry is off and the no-op providers stay, which is what proxy.py does without the SDK.
// The W3C trace context and baggage propagators are installed regardless, so a caller's trace still
// reaches the agent. A value this build cannot honor, such as the gRPC protocol, is an error. It reads
// the process environment, which is where the exporters read the rest of their settings.
func Setup(ctx context.Context) (*Telemetry, error) {
	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(propagation.TraceContext{}, propagation.Baggage{}))
	get := func(k string) string { return strings.TrimSpace(os.Getenv(k)) }
	traces, err := wanted(get, "TRACES")
	if err != nil {
		return nil, err
	}
	metrics, err := wanted(get, "METRICS")
	if err != nil {
		return nil, err
	}
	t := &Telemetry{}
	if !traces && !metrics {
		return t, nil
	}
	res, err := resource.Merge(resource.Default(), resource.Environment())
	if err != nil {
		return nil, fmt.Errorf("telemetry resource: %w", err)
	}
	if traces {
		exp, err := otlptracehttp.New(ctx)
		if err != nil {
			return nil, fmt.Errorf("OTLP trace exporter: %w", err)
		}
		tp := sdktrace.NewTracerProvider(sdktrace.WithBatcher(exp), sdktrace.WithResource(res))
		otel.SetTracerProvider(tp)
		t.shutdown = append(t.shutdown, tp.Shutdown)
	}
	if metrics {
		exp, err := otlpmetrichttp.New(ctx)
		if err != nil {
			return nil, fmt.Errorf("OTLP metric exporter: %w", err)
		}
		mp := sdkmetric.NewMeterProvider(sdkmetric.WithReader(sdkmetric.NewPeriodicReader(exp)), sdkmetric.WithResource(res))
		otel.SetMeterProvider(mp)
		t.shutdown = append(t.shutdown, mp.Shutdown)
	}
	return t, nil
}

// wanted reads OTEL_<signal>_EXPORTER and the OTLP protocol for one signal.
func wanted(get func(string) string, signal string) (bool, error) {
	exporter := get("OTEL_" + signal + "_EXPORTER")
	switch exporter {
	case "none":
		return false, nil
	case "", "otlp":
	default:
		return false, fmt.Errorf("OTEL_%s_EXPORTER=%q: this build exports only otlp, or none", signal, exporter)
	}
	on := exporter == "otlp" || get("OTEL_EXPORTER_OTLP_ENDPOINT") != "" || get("OTEL_EXPORTER_OTLP_"+signal+"_ENDPOINT") != ""
	if !on {
		return false, nil
	}
	for _, k := range []string{"OTEL_EXPORTER_OTLP_" + signal + "_PROTOCOL", "OTEL_EXPORTER_OTLP_PROTOCOL"} {
		if p := get(k); p != "" {
			if p != "http/protobuf" {
				return false, fmt.Errorf("%s=%q: this build exports OTLP over http/protobuf only", k, p)
			}
			break
		}
	}
	return true, nil
}
