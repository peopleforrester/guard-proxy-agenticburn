// ABOUTME: The tier each request is priced at: the model the workshop-agent is bound to, read from its
// ABOUTME: Agent at most every 5 seconds, with MODEL_TIER until a read finds a ModelConfig that names one.
// SPDX-License-Identifier: Apache-2.0
package server

import (
	"context"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"github.com/peopleforrester/guard-proxy-agenticburn/internal/config"
)

// bindingTTL is how long one read of the Agent prices requests, as long as the badges' cache. A swap
// restarts the agent's pod, which takes longer.
const bindingTTL = 5 * time.Second

// binding follows the workshop-agent's spec.declarative.modelConfig (guard-proxy-agenticburn#37). The
// workshop swaps the model live by patching it to bedrock-haiku, bedrock-sonnet or bedrock-opus, which
// Argo CD leaves out of its diff, while MODEL_TIER is read once at start. A ModelConfig bedrock-<tier>
// prices at that tier; any other name prices at MODEL_TIER. A read that fails says nothing about the
// binding, so it keeps the tier the last read found. It is safe for concurrent use.
type binding struct {
	read      func() (string, bool)
	startTier string
	log       *slog.Logger
	now       func() time.Time

	mu          sync.Mutex
	tried       bool
	at          time.Time
	seen        bool
	modelConfig string
	current     string
}

// newBinding prices at startTier until read, the Agent's ModelConfig name, finds a tier.
func newBinding(read func() (string, bool), startTier string, log *slog.Logger) *binding {
	return &binding{read: read, startTier: startTier, log: log, now: time.Now, current: startTier}
}

// tier is the tier a request is priced at now. It reads the Agent when the last read is older than
// bindingTTL, outside the lock, so a slow API server holds up only the requests that read it.
func (b *binding) tier(ctx context.Context) string {
	now := b.now()
	b.mu.Lock()
	if b.tried && now.Sub(b.at) < bindingTTL {
		defer b.mu.Unlock()
		return b.current
	}
	b.mu.Unlock()

	name, ok := b.read()
	b.mu.Lock()
	b.tried, b.at = true, now
	if !ok {
		defer b.mu.Unlock()
		return b.current
	}
	tier, named := config.TierOf(name)
	changed := !b.seen || name != b.modelConfig
	b.seen, b.modelConfig = true, name
	if named {
		b.current = tier
	} else {
		b.current = b.startTier
	}
	priced := b.current
	b.mu.Unlock()

	switch {
	case changed && named:
		b.log.InfoContext(ctx, fmt.Sprintf("pricing at %s: workshop-agent is bound to %s", tier, name), "event", "price_tier")
	case changed:
		b.log.WarnContext(ctx, fmt.Sprintf("workshop-agent is bound to %q, which names no price tier; pricing at MODEL_TIER %s",
			name, b.startTier), "event", "price_tier_unknown")
	}
	return priced
}
