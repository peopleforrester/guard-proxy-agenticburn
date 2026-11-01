// ABOUTME: Tests for telemetry setup from the OTEL_* environment: off unless an exporter is configured,
// ABOUTME: refusal of protocols the build does not ship, and spans and metrics reaching an OTLP endpoint.
// SPDX-License-Identifier: Apache-2.0
package telemetry

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/propagation"
)

// otelVars are every variable Setup or the exporters read, cleared before each case.
var otelVars = []string{"OTEL_TRACES_EXPORTER", "OTEL_METRICS_EXPORTER", "OTEL_EXPORTER_OTLP_ENDPOINT",
	"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "OTEL_EXPORTER_OTLP_PROTOCOL",
	"OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", "OTEL_EXPORTER_OTLP_METRICS_PROTOCOL", "OTEL_SERVICE_NAME"}

func setEnv(t *testing.T, m map[string]string) {
	t.Helper()
	for _, k := range otelVars {
		t.Setenv(k, m[k])
	}
}

func TestOffWithoutAnExporter(t *testing.T) {
	for _, m := range []map[string]string{
		{},
		{"OTEL_SERVICE_NAME": "guard-proxy"},
		{"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318", "OTEL_TRACES_EXPORTER": "none", "OTEL_METRICS_EXPORTER": "none"},
	} {
		setEnv(t, m)
		tel, err := Setup(context.Background())
		if err != nil || tel.Enabled() {
			t.Errorf("%v: Setup = enabled %v, %v", m, tel.Enabled(), err)
		}
	}
}

func TestPropagatesW3CContextEvenWhenOff(t *testing.T) {
	setEnv(t, nil)
	if _, err := Setup(context.Background()); err != nil {
		t.Fatal(err)
	}
	h := http.Header{"Traceparent": {"00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"}}
	ctx := otel.GetTextMapPropagator().Extract(context.Background(), propagation.HeaderCarrier(h))
	out := http.Header{}
	otel.GetTextMapPropagator().Inject(ctx, propagation.HeaderCarrier(out))
	if out.Get("traceparent") != h.Get("traceparent") {
		t.Errorf("traceparent = %q", out.Get("traceparent"))
	}
}

func TestRefusesWhatThisBuildDoesNotShip(t *testing.T) {
	for _, m := range []map[string]string{
		{"OTEL_EXPORTER_OTLP_ENDPOINT": "http://c:4317", "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc"},
		{"OTEL_EXPORTER_OTLP_ENDPOINT": "http://c:4318", "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL": "http/json"},
		{"OTEL_TRACES_EXPORTER": "console"},
		{"OTEL_EXPORTER_OTLP_ENDPOINT": "http://c:4318", "OTEL_METRICS_EXPORTER": "prometheus"},
	} {
		setEnv(t, m)
		if _, err := Setup(context.Background()); err == nil || !strings.Contains(err.Error(), "OTEL_") {
			t.Errorf("%v: Setup err = %v, want one naming the variable", m, err)
		}
	}
}

func TestExportsSpansAndMetricsToTheEndpoint(t *testing.T) {
	var mu sync.Mutex
	paths := map[string]int{}
	c := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		paths[r.URL.Path]++
		mu.Unlock()
	}))
	defer c.Close()
	setEnv(t, map[string]string{"OTEL_EXPORTER_OTLP_ENDPOINT": c.URL})
	tel, err := Setup(context.Background())
	if err != nil || !tel.Enabled() {
		t.Fatalf("Setup = %v, %v", tel.Enabled(), err)
	}
	_, span := otel.Tracer("test").Start(context.Background(), "s")
	span.End()
	gauge, _ := otel.Meter("test").Float64ObservableGauge("g")
	_, _ = otel.Meter("test").RegisterCallback(func(_ context.Context, o metric.Observer) error {
		o.ObserveFloat64(gauge, 1)
		return nil
	}, gauge)
	if err := tel.Shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
	mu.Lock()
	defer mu.Unlock()
	if paths["/v1/traces"] == 0 || paths["/v1/metrics"] == 0 {
		t.Errorf("collector saw %v", paths)
	}
}
