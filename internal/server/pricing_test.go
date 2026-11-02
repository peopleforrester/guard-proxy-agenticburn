// ABOUTME: Unit tests for the tier each request is priced at: the workshop-agent's bound model, its
// ABOUTME: 5-second cache, MODEL_TIER until a read names a tier, and the log line when the binding changes.
// SPDX-License-Identifier: Apache-2.0
package server

import (
	"bytes"
	"context"
	"slices"
	"testing"
	"time"

	"github.com/peopleforrester/guard-proxy-agenticburn/internal/logging"
)

// fakeBinding is the Agent as the binding reads it: bound to name, or unreadable while !ok.
type fakeBinding struct {
	name  string
	ok    bool
	reads int
}

func (f *fakeBinding) read() (string, bool) {
	f.reads++
	return f.name, f.ok
}

func newTestBinding(t *testing.T, f *fakeBinding, startTier string) (*binding, *time.Time, *bytes.Buffer) {
	t.Helper()
	var logs bytes.Buffer
	b := newBinding(f.read, startTier, logging.New(&logs))
	now := time.Unix(0, 0)
	b.now = func() time.Time { return now }
	return b, &now, &logs
}

func TestBindingStartsOnModelTierWithoutTheAgent(t *testing.T) {
	f := &fakeBinding{}
	b, _, logs := newTestBinding(t, f, "nova")
	if got := b.tier(context.Background()); got != "nova" {
		t.Errorf("tier = %q, want MODEL_TIER nova", got)
	}
	if logs.Len() != 0 {
		t.Errorf("an unreadable Agent logged: %s", logs)
	}
}

func TestBindingFollowsASwapAfterTheCacheWindow(t *testing.T) {
	f := &fakeBinding{name: "bedrock-nova", ok: true}
	b, now, logs := newTestBinding(t, f, "nova")
	ctx := context.Background()
	if got := b.tier(ctx); got != "nova" {
		t.Fatalf("tier = %q", got)
	}
	f.name = "bedrock-opus"
	*now = now.Add(bindingTTL - time.Millisecond)
	if got := b.tier(ctx); got != "nova" || f.reads != 1 {
		t.Errorf("inside the window: tier = %q after %d reads, want the cached nova after 1", got, f.reads)
	}
	*now = now.Add(time.Millisecond)
	if got := b.tier(ctx); got != "opus" || f.reads != 2 {
		t.Errorf("after the window: tier = %q after %d reads, want opus after 2", got, f.reads)
	}
	if got := eventsOf(t, logs); !slices.Equal(got, []string{"price_tier", "price_tier"}) {
		t.Errorf("events = %v, want price_tier for each binding read", got)
	}
}

func TestBindingKeepsTheLastTierWhenTheAgentCannotBeRead(t *testing.T) {
	f := &fakeBinding{name: "bedrock-opus", ok: true}
	b, now, _ := newTestBinding(t, f, "nova")
	ctx := context.Background()
	b.tier(ctx)
	f.name, f.ok = "", false
	*now = now.Add(bindingTTL)
	if got := b.tier(ctx); got != "opus" || f.reads != 2 {
		t.Errorf("tier = %q after %d reads, want the last bound opus after 2", got, f.reads)
	}
}

func TestBindingPricesAModelConfigWithNoTierAtModelTier(t *testing.T) {
	f := &fakeBinding{name: "bedrock-opus", ok: true}
	b, now, logs := newTestBinding(t, f, "sonnet")
	ctx := context.Background()
	b.tier(ctx)
	f.name = "gateway-local-qwen3"
	for range 3 {
		*now = now.Add(bindingTTL)
		if got := b.tier(ctx); got != "sonnet" {
			t.Errorf("tier = %q, want MODEL_TIER sonnet", got)
		}
	}
	recs := records(t, logs)
	if len(recs) != 2 || recs[1]["event"] != "price_tier_unknown" || recs[1]["level"] != "WARNING" {
		t.Errorf("logs = %v, want price_tier then one price_tier_unknown warning", recs)
	}
}

func TestBindingLogsAnAgentWithNoModelConfigOnce(t *testing.T) {
	f := &fakeBinding{name: "", ok: true}
	b, now, logs := newTestBinding(t, f, "haiku")
	ctx := context.Background()
	b.tier(ctx)
	*now = now.Add(bindingTTL)
	b.tier(ctx)
	if got := eventsOf(t, logs); !slices.Equal(got, []string{"price_tier_unknown"}) {
		t.Errorf("events = %v", got)
	}
}
