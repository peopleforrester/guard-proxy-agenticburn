// ABOUTME: The on-topic guard: off and on phrases matched as whole words, cheapest first, and the
// ABOUTME: BanTopics scan that decides a prompt carrying neither. Also the whole-word matcher.
// SPDX-License-Identifier: Apache-2.0
package guard

import (
	"context"
	"regexp"
	"sort"
	"strings"
)

// WholeWords matches any of terms as whole words, case-insensitive, with an optional plural "s",
// longest term first: "song" matches "songs" and "rice" does not match "price". It is nil for an empty
// list, which joined as-is would be an empty alternation that matches everywhere.
func WholeWords(terms []string) *regexp.Regexp {
	alts := make([]string, 0, len(terms))
	for _, t := range terms {
		if t = strings.TrimSpace(t); t != "" {
			alts = append(alts, regexp.QuoteMeta(t))
		}
	}
	if len(alts) == 0 {
		return nil
	}
	sort.SliceStable(alts, func(i, j int) bool { return len(alts[i]) > len(alts[j]) })
	return regexp.MustCompile(`(?i)\b(?:` + strings.Join(alts, "|") + `)s?\b`)
}

// Phrase is what the phrase lists say about a prompt.
type Phrase int

const (
	// PhraseNone means neither list matched, so the classifier decides.
	PhraseNone Phrase = iota
	// PhraseOff means an off phrase matched, and the prompt is refused.
	PhraseOff
	// PhraseOn means an on phrase matched, and the prompt passes.
	PhraseOn
)

// Topic holds the on-topic guard's phrase lists.
type Topic struct{ off, on *regexp.Regexp }

// NewTopic compiles the off and on phrase lists.
func NewTopic(off, on []string) Topic {
	return Topic{off: WholeWords(off), on: WholeWords(on)}
}

// Phrase checks the off list before the on list, so "salsa dance moves" is refused although "salsa"
// is on the menu.
func (t Topic) Phrase(text string) Phrase {
	if t.off != nil && t.off.MatchString(text) {
		return PhraseOff
	}
	if t.on != nil && t.on.MatchString(text) {
		return PhraseOn
	}
	return PhraseNone
}

// ScanTopic reports whether BanTopics allows text. PromptInjection is suppressed: the injection
// classifier is its own stage with its own toggle. An error means no verdict was reached.
func (g *LLMGuard) ScanTopic(ctx context.Context, text string) (bool, error) {
	v, err := g.post(ctx, "/analyze/prompt", map[string]any{
		"prompt":            text,
		"scanners_suppress": []string{"PromptInjection"},
	})
	if err != nil {
		return false, err
	}
	return valid(v), nil
}
