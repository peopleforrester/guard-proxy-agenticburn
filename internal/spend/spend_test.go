// ABOUTME: Tests for the spend package: usage metering from A2A replies, the two caps and their
// ABOUTME: precedence, the runtime cost cap's parsing, and the sliding-minute rate limits.
// SPDX-License-Identifier: Apache-2.0
package spend

import (
	"encoding/json"
	"math"
	"path/filepath"
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

// newMeter is a meter on a fresh store in the test's temporary directory.
func newMeter(t *testing.T, envCap float64) *Meter {
	t.Helper()
	st, err := OpenStore(filepath.Join(t.TempDir(), "spend.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = st.Close() })
	m, err := NewMeter(envCap, st)
	if err != nil {
		t.Fatal(err)
	}
	return m
}

// testModel is the model the record helper meters each call as.
const testModel = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

func record(t *testing.T, m *Meter, p Prices, u Usage, session string) {
	t.Helper()
	if _, err := m.Record(u, p, testModel, session); err != nil {
		t.Fatal(err)
	}
}

func TestMeterTalliesClusterAndSession(t *testing.T) {
	p := Prices{In: 0.001, Out: 0.005}
	m := newMeter(t, 0)
	record(t, m, p, Usage{In: 1000, Out: 1000, Total: 2000}, "a")
	record(t, m, p, Usage{In: 1000, Out: 0, Total: 1000}, "")
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

func TestEachCallIsPricedAtItsOwnPrices(t *testing.T) {
	// The agent swapped from Nova Pro to Opus between two calls (guard-proxy-agenticburn#37).
	m := newMeter(t, 0)
	nova, err := m.Record(Usage{In: 1000, Out: 1000}, Prices{In: 0.0008, Out: 0.0032}, "nova-pro", "a")
	if err != nil {
		t.Fatal(err)
	}
	opus, err := m.Record(Usage{In: 1000, Out: 1000}, Prices{In: 0.005, Out: 0.025}, "opus", "a")
	if err != nil {
		t.Fatal(err)
	}
	if math.Abs(nova-0.004) > 1e-12 || math.Abs(opus-0.03) > 1e-12 {
		t.Errorf("Record spent %v then %v, want 0.004 then 0.03", nova, opus)
	}
	if got := m.Totals().USD; math.Abs(got-0.034) > 1e-12 {
		t.Errorf("USD = %v, want 0.034", got)
	}
	if got := m.SessionUSD("a"); math.Abs(got-0.034) > 1e-12 {
		t.Errorf("SessionUSD(a) = %v, want 0.034", got)
	}
}

func TestSpendIsSplitByTheModelEachCallWasMeteredAs(t *testing.T) {
	// The cost gauge's series, one per model (guard-proxy-agenticburn#38).
	m := newMeter(t, 0)
	if got := m.ByModel(); len(got) != 0 {
		t.Errorf("ByModel of a fresh meter = %v, want empty", got)
	}
	for _, c := range []struct {
		model string
		p     Prices
	}{{"nova-pro", Prices{In: 0.0008, Out: 0.0032}}, {"opus", Prices{In: 0.005, Out: 0.025}}, {"nova-pro", Prices{In: 0.0008, Out: 0.0032}}} {
		if _, err := m.Record(Usage{In: 1000, Out: 1000}, c.p, c.model, ""); err != nil {
			t.Fatal(err)
		}
	}
	got := m.ByModel()
	if len(got) != 2 || math.Abs(got["nova-pro"]-0.008) > 1e-12 || math.Abs(got["opus"]-0.03) > 1e-12 {
		t.Errorf("ByModel = %v, want nova-pro 0.008 and opus 0.03", got)
	}
	// A copy: the caller cannot change the meter's split.
	got["opus"] = 99
	if m.ByModel()["opus"] == 99 {
		t.Error("ByModel returned the meter's own map")
	}
}

func TestCapPrecedence(t *testing.T) {
	p := Prices{In: 1, Out: 0}
	m := newMeter(t, 2)
	if got := m.Capped("a", true, 1); got != None {
		t.Errorf("fresh: %v", got)
	}
	record(t, m, p, Usage{In: 1000}, "a") // $1 for a, $1 cluster
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
	record(t, m, p, Usage{In: 1000}, "b") // $2 cluster: the cost cap, at exactly the cap
	if got := m.Capped("a", true, 1); got != CostCap {
		t.Errorf("both tripped: %v", got)
	}
	if err := m.SetCostCap(0); err != nil {
		t.Fatal(err)
	}
	if got := m.Capped("c", false, 1); got != None {
		t.Errorf("cost cap off: %v", got)
	}
}

func TestCapUSD(t *testing.T) {
	m := newMeter(t, 25)
	if got := m.CapUSD(false, 0.05); got != 25 {
		t.Errorf("CapUSD = %v", got)
	}
	if got := m.CapUSD(true, 0.05); got != 0.05 {
		t.Errorf("CapUSD = %v", got)
	}
	if err := m.SetCostCap(0); err != nil {
		t.Fatal(err)
	}
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
