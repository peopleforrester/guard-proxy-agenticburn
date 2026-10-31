// ABOUTME: Tests for the spend package: usage metering from A2A replies, the two caps and their
// ABOUTME: precedence, the runtime cost cap's parsing, and the sliding-minute rate limits.
// SPDX-License-Identifier: Apache-2.0
package spend

import (
	"encoding/json"
	"math"
	"testing"
	"time"
)

func reply(t *testing.T, s string) any {
	t.Helper()
	var v any
	if err := json.Unmarshal([]byte(s), &v); err != nil {
		t.Fatal(err)
	}
	return v
}

func TestUsageReadsEitherKeyAndLocation(t *testing.T) {
	for _, body := range []string{
		`{"result": {"metadata": {"kagent_usage_metadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}}}}`,
		`{"result": {"metadata": {"adk_usage_metadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}}}}`,
		`{"result": {"status": {"message": {"metadata": {"kagent_usage_metadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}}}}}}`,
		// An empty kagent block falls through to the adk key, as the reference's `or` does.
		`{"result": {"metadata": {"kagent_usage_metadata": {}, "adk_usage_metadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}}}}`,
	} {
		u, ok := UsageOf(reply(t, body))
		if !ok || u.In != 10 || u.Out != 5 || u.Total != 15 {
			t.Errorf("%s: UsageOf = %+v, %v", body, u, ok)
		}
	}
}

func TestUsageTotalPrefersTheReportedOne(t *testing.T) {
	u, _ := UsageOf(reply(t, `{"result": {"metadata": {"kagent_usage_metadata": {"promptTokenCount": 1, "candidatesTokenCount": 2, "totalTokenCount": 40}}}}`))
	if u.Total != 40 {
		t.Errorf("Total = %d", u.Total)
	}
	// A reported total that is null reads as 0, not as in + out.
	u, _ = UsageOf(reply(t, `{"result": {"metadata": {"kagent_usage_metadata": {"promptTokenCount": 1, "candidatesTokenCount": 2, "totalTokenCount": null}}}}`))
	if u.Total != 0 {
		t.Errorf("Total = %d", u.Total)
	}
}

func TestUsageAbsent(t *testing.T) {
	for _, body := range []string{`{}`, `[]`, `{"result": {}}`, `{"result": {"metadata": {"kagent_usage_metadata": {}}}}`, `"x"`} {
		if _, ok := UsageOf(reply(t, body)); ok {
			t.Errorf("%s: usage found", body)
		}
	}
}

func TestUsageCountsCoerceLikeInt(t *testing.T) {
	u, ok := UsageOf(reply(t, `{"result": {"metadata": {"kagent_usage_metadata": {"promptTokenCount": 3.9, "candidatesTokenCount": "7"}}}}`))
	if !ok || u.In != 3 || u.Out != 7 {
		t.Errorf("UsageOf = %+v, %v", u, ok)
	}
}

func TestMeterTalliesClusterAndSession(t *testing.T) {
	m := NewMeter(Prices{In: 0.001, Out: 0.005}, 0)
	m.Record(Usage{In: 1000, Out: 1000, Total: 2000}, "a")
	m.Record(Usage{In: 1000, Out: 0, Total: 1000}, "")
	tot := m.Totals()
	if tot.Requests != 2 || tot.InputTokens != 2000 || tot.OutputTokens != 1000 || tot.TotalTokens != 3000 {
		t.Errorf("Totals = %+v", tot)
	}
	if math.Abs(tot.USD-0.007) > 1e-12 {
		t.Errorf("USD = %v", tot.USD)
	}
	if got := m.SessionUSD("a"); math.Abs(got-0.006) > 1e-12 {
		t.Errorf("SessionUSD(a) = %v", got)
	}
}

func TestCapPrecedence(t *testing.T) {
	m := NewMeter(Prices{In: 1, Out: 0}, 2)
	if got := m.Capped("a", true, 1); got != None {
		t.Errorf("fresh: %v", got)
	}
	m.Record(Usage{In: 1000}, "a") // $1 for a, $1 cluster
	if got := m.Capped("a", true, 1); got != Budget {
		t.Errorf("budget reached: %v", got)
	}
	if got := m.Capped("a", false, 1); got != None {
		t.Errorf("budget guard off: %v", got)
	}
	if got := m.Capped("b", true, 1); got != None {
		t.Errorf("other session: %v", got)
	}
	// With no session, the budget is measured against the cluster total.
	if got := m.Capped("", true, 1); got != Budget {
		t.Errorf("no session: %v", got)
	}
	m.Record(Usage{In: 1000}, "b") // $2 cluster: the cost cap, at exactly the cap
	if got := m.Capped("a", true, 1); got != CostCap {
		t.Errorf("both tripped: %v", got)
	}
	m.SetCostCap(0)
	if got := m.Capped("c", false, 1); got != None {
		t.Errorf("cost cap off: %v", got)
	}
}

func TestCapUSD(t *testing.T) {
	m := NewMeter(Prices{}, 25)
	if got := m.CapUSD(false, 0.05); got != 25 {
		t.Errorf("CapUSD = %v", got)
	}
	if got := m.CapUSD(true, 0.05); got != 0.05 {
		t.Errorf("CapUSD = %v", got)
	}
	m.SetCostCap(0)
	if got := m.CapUSD(false, 0.05); got != 0 {
		t.Errorf("CapUSD = %v", got)
	}
}

func TestParseCostCap(t *testing.T) {
	for _, tc := range []struct {
		v        string
		env      float64
		want     float64
		accepted bool
	}{
		{"2.00", 25, 2, true}, {" 0.5 ", 25, 0.5, true}, {"25", 25, 25, true}, {"default", 25, 25, true},
		{"DEFAULT", 0, 0, true}, {"1_0", 25, 10, true}, {".5", 25, 0.5, true}, {"5.", 25, 5, true},
		{"1e1", 25, 10, true}, {"100", 0, 100, true},
		{"", 25, 0, false}, {"abc", 25, 0, false}, {"0", 25, 0, false}, {"-1", 25, 0, false},
		{"nan", 25, 0, false}, {"inf", 25, 0, false}, {"-inf", 25, 0, false}, {"1e400", 25, 0, false},
		{"25.01", 25, 0, false}, {"$2", 25, 0, false}, {"2,00", 25, 0, false}, {"0x10", 25, 0, false},
		{"1__0", 25, 0, false}, {"_1", 25, 0, false}, {".", 25, 0, false},
	} {
		got, ok := ParseCostCap(tc.v, tc.env)
		if ok != tc.accepted || (ok && got != tc.want) {
			t.Errorf("ParseCostCap(%q, %v) = %v, %v; want %v, %v", tc.v, tc.env, got, ok, tc.want, tc.accepted)
		}
	}
}

func TestCostCapRefusal(t *testing.T) {
	if got := CostCapRefusal(25); got != "cost_cap must be a number above 0 and no higher than COST_CAP_USD ($25.00), or 'default' to restore COST_CAP_USD" {
		t.Error(got)
	}
	if got := CostCapRefusal(0); got != "cost_cap must be a number above 0, or 'default' to restore COST_CAP_USD" {
		t.Error(got)
	}
}

func TestLimiterClusterWindow(t *testing.T) {
	now := time.Unix(0, 0)
	l := NewLimiter(2, 0)
	l.now = func() time.Time { return now }
	if l.Cluster() || l.Cluster() || !l.Cluster() {
		t.Fatal("want two allowed, then limited")
	}
	now = now.Add(61 * time.Second)
	if l.Cluster() {
		t.Error("the window did not slide")
	}
	if NewLimiter(0, 0).Cluster() {
		t.Error("a zero limit limited")
	}
}

func TestLimiterSessionWindow(t *testing.T) {
	l := NewLimiter(0, 1)
	if l.Session("a") || !l.Session("a") || l.Session("b") || l.Session("") || l.Session("") {
		t.Error("want one per session, and none for no session")
	}
}
