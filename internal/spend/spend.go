// ABOUTME: Spend control: token usage read from A2A replies, the cluster and per-session cost tally,
// ABOUTME: the cost cap and budget cap, and the sliding-minute rate limits. Safe for concurrent use.
// SPDX-License-Identifier: Apache-2.0
package spend

import (
	"fmt"
	"math"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Prices is USD per 1,000 tokens.
type Prices struct{ In, Out float64 }

// Cost is what in and out tokens cost at p, computed in the reference proxy's order.
func (p Prices) Cost(in, out int64) float64 {
	return (float64(in)/1000.0)*p.In + (float64(out)/1000.0)*p.Out
}

// Usage is one model call's token counts.
type Usage struct{ In, Out, Total int64 }

// UsageOf reads the token usage kagent reports in an A2A reply: result.metadata, then
// result.status.message.metadata, each under kagent_usage_metadata or adk_usage_metadata. ok is false
// when there is none, and such a reply is not counted.
func UsageOf(resp any) (Usage, bool) {
	result, _ := field(resp, "result").(map[string]any)
	meta := usageBlock(result["metadata"])
	if meta == nil {
		meta = usageBlock(field(field(result["status"], "message"), "metadata"))
	}
	if meta == nil {
		return Usage{}, false
	}
	u := Usage{In: toInt(meta["promptTokenCount"]), Out: toInt(meta["candidatesTokenCount"])}
	if total, present := meta["totalTokenCount"]; present {
		u.Total = toInt(total)
	} else {
		u.Total = u.In + u.Out
	}
	return u, true
}

// usageBlock is kagent_usage_metadata, or adk_usage_metadata when that is absent or empty.
func usageBlock(metadata any) map[string]any {
	m, _ := metadata.(map[string]any)
	for _, key := range []string{"kagent_usage_metadata", "adk_usage_metadata"} {
		if b, ok := m[key].(map[string]any); ok && len(b) > 0 {
			return b
		}
	}
	return nil
}

// toInt reads a token count the way the reference's int() does for the values JSON can carry: a
// number is truncated, a numeric string is parsed, and anything else is 0.
func toInt(v any) int64 {
	switch x := v.(type) {
	case float64:
		if math.IsNaN(x) || math.IsInf(x, 0) {
			return 0
		}
		return int64(x)
	case bool:
		if x {
			return 1
		}
	case string:
		if n, err := strconv.ParseInt(strings.TrimSpace(x), 10, 64); err == nil {
			return n
		}
	}
	return 0
}

func field(v any, key string) any {
	m, _ := v.(map[string]any)
	return m[key]
}

// Totals is the cluster-wide tally /cost reports.
type Totals struct {
	Requests     int64
	InputTokens  int64
	OutputTokens int64
	TotalTokens  int64
	USD          float64
}

// Cap names the cap a request has reached.
type Cap int

const (
	// None means no cap has been reached.
	None Cap = iota
	// CostCap is the cluster-wide backstop. It wins when both have tripped, because Reset cannot
	// release it.
	CostCap
	// Budget is the per-session cap the budget guard enforces.
	Budget
)

// Meter tallies spend for the cluster and for each /chat session, and holds the runtime cost cap.
type Meter struct {
	prices Prices

	mu      sync.Mutex
	totals  Totals
	session map[string]float64
	costCap float64
}

// NewMeter returns a meter pricing at p, with the cost cap at costCap (0 is off).
func NewMeter(p Prices, costCap float64) *Meter {
	return &Meter{prices: p, session: map[string]float64{}, costCap: costCap}
}

// Prices is the meter's price per 1,000 tokens.
func (m *Meter) Prices() Prices { return m.prices }

// Record adds one model call's usage to the cluster tally and, when session is set, to that session.
func (m *Meter) Record(u Usage, session string) {
	spent := m.prices.Cost(u.In, u.Out)
	m.mu.Lock()
	defer m.mu.Unlock()
	m.totals.Requests++
	m.totals.InputTokens += u.In
	m.totals.OutputTokens += u.Out
	m.totals.TotalTokens += u.Total
	m.totals.USD += spent
	if session != "" {
		m.session[session] += spent
	}
}

// Totals is a snapshot of the cluster tally.
func (m *Meter) Totals() Totals {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.totals
}

// SessionUSD is what one session has spent.
func (m *Meter) SessionUSD(session string) float64 {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.session[session]
}

// CostCap is the runtime cost cap.
func (m *Meter) CostCap() float64 {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.costCap
}

// SetCostCap replaces the runtime cost cap. The tally is untouched.
func (m *Meter) SetCostCap(usd float64) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.costCap = usd
}

// Capped reports which cap spend has reached. The cost cap is measured against the cluster total; the
// budget cap, while budgetOn, against the session's own spend, or the cluster total for no session.
func (m *Meter) Capped(session string, budgetOn bool, budgetCap float64) Cap {
	m.mu.Lock()
	defer m.mu.Unlock()
	spend := m.totals.USD
	sessionSpend := spend
	if session != "" {
		sessionSpend = m.session[session]
	}
	if m.costCap > 0 && spend >= m.costCap {
		return CostCap
	}
	if budgetOn && budgetCap > 0 && sessionSpend >= budgetCap {
		return Budget
	}
	return None
}

// CapUSD is the ceiling /cost shows: the lower of the cost cap and, while the budget guard is on, the
// budget cap, ignoring either one that is off. 0 means no cap applies.
func (m *Meter) CapUSD(budgetOn bool, budgetCap float64) float64 {
	caps := []float64{m.CostCap()}
	if budgetOn {
		caps = append(caps, budgetCap)
	}
	low := 0.0
	for _, c := range caps {
		if c > 0 && (low == 0 || c < low) {
			low = c
		}
	}
	return low
}

// pyFloat matches what Python's float() accepts for a decimal: digits with single underscores between
// them, an optional fraction and exponent, or inf, infinity and nan. It refuses Go-only forms like hex.
var pyFloat = regexp.MustCompile(`(?i)^[+-]?(?:(?:\d(?:_?\d)*(?:\.(?:\d(?:_?\d)*)?)?|\.\d(?:_?\d)*)(?:e[+-]?\d(?:_?\d)*)?|inf|infinity|nan)$`)

// ParseCostCap reads a /toggle?cost_cap= value. "default" restores envCap. Anything else must be a
// finite number above 0 and, while envCap is set, no higher than it: the runtime cap only lowers the
// backstop, since /toggle has no auth and the agent's tool pod can reach it. ok is false for a refusal.
func ParseCostCap(value string, envCap float64) (float64, bool) {
	v := strings.ToLower(strings.TrimSpace(value))
	if v == "default" {
		return envCap, true
	}
	if !pyFloat.MatchString(v) {
		return 0, false
	}
	// An out-of-range value parses as ±Inf, as Python's float() gives, and is refused below.
	c, err := strconv.ParseFloat(strings.ReplaceAll(v, "_", ""), 64)
	if err != nil && !isRange(err) {
		return 0, false
	}
	if math.IsNaN(c) || math.IsInf(c, 0) || c <= 0 || (envCap > 0 && c > envCap) {
		return 0, false
	}
	return c, true
}

func isRange(err error) bool {
	ne, ok := err.(*strconv.NumError)
	return ok && ne.Err == strconv.ErrRange
}

// CostCapRefusal is the 400 message for a value ParseCostCap refuses.
func CostCapRefusal(envCap float64) string {
	ceiling := ""
	if envCap > 0 {
		ceiling = fmt.Sprintf(" and no higher than COST_CAP_USD ($%.2f)", envCap)
	}
	return "cost_cap must be a number above 0" + ceiling + ", or 'default' to restore COST_CAP_USD"
}

// window is how far back the rate limits look.
const window = 60 * time.Second

// maxSessions bounds the per-session windows: past it, sessions idle for a whole window are dropped.
const maxSessions = 5000

// Limiter is the two sliding-minute rate limits: the cluster's, and each /chat session's.
type Limiter struct {
	clusterRPM, sessionRPM int
	now                    func() time.Time

	mu       sync.Mutex
	cluster  []time.Time
	sessions map[string][]time.Time
}

// NewLimiter returns limits of clusterRPM and sessionRPM requests per minute; 0 turns one off.
func NewLimiter(clusterRPM, sessionRPM int) *Limiter {
	return &Limiter{clusterRPM: clusterRPM, sessionRPM: sessionRPM, now: time.Now, sessions: map[string][]time.Time{}}
}

// ClusterRPM is the cluster limit.
func (l *Limiter) ClusterRPM() int { return l.clusterRPM }

// SessionRPM is the per-session limit.
func (l *Limiter) SessionRPM() int { return l.sessionRPM }

// Cluster reports whether the cluster is over its limit. A request that is not limited takes a slot.
func (l *Limiter) Cluster() bool {
	if l.clusterRPM <= 0 {
		return false
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	limited, q := take(l.cluster, l.now(), l.clusterRPM)
	l.cluster = q
	return limited
}

// Session reports whether session is over its limit. No session is never limited here.
func (l *Limiter) Session(session string) bool {
	if l.sessionRPM <= 0 || session == "" {
		return false
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	now := l.now()
	if len(l.sessions) > maxSessions {
		for k, q := range l.sessions {
			if len(q) == 0 || now.Sub(q[len(q)-1]) > window {
				delete(l.sessions, k)
			}
		}
	}
	limited, q := take(l.sessions[session], now, l.sessionRPM)
	l.sessions[session] = q
	return limited
}

// take drops timestamps older than the window, then either reports the limit reached or adds now.
func take(q []time.Time, now time.Time, limit int) (bool, []time.Time) {
	i := 0
	for i < len(q) && now.Sub(q[i]) > window {
		i++
	}
	q = q[i:]
	if len(q) >= limit {
		return true, q
	}
	return false, append(q, now)
}
