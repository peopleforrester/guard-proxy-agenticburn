// ABOUTME: Tests for the on-topic guard's phrase rule and its BanTopics scan, and for the whole-word
// ABOUTME: matcher it shares with the prompt feed.
// SPDX-License-Identifier: Apache-2.0
package guard

import (
	"context"
	"testing"
	"time"
)

func TestPhraseVerdict(t *testing.T) {
	topic := NewTopic([]string{"dance", "dance move", "hot dog", "cha-cha", "linked list", "song"},
		[]string{"menu", "salsa", "rice", "Dragon's Blood"})
	for prompt, want := range map[string]Phrase{
		"how do I throw a hot dog party":  PhraseOff,
		"teach me some salsa dance moves": PhraseOff, // off before on
		"which Hot Dogs should I grill":   PhraseOff,
		"show me the cha-cha":             PhraseOff,
		"rank the top songs":              PhraseOff,
		"a dancer walks in":               PhraseNone,
		"reverse a linked-list":           PhraseNone,
		"what's the price":                PhraseNone,
		"do you have mild salsa":          PhraseOn,
		"how spicy is DRAGON'S BLOOD":     PhraseOn,
	} {
		if got := topic.Phrase(prompt); got != want {
			t.Errorf("Phrase(%q) = %v, want %v", prompt, got, want)
		}
	}
	if got := NewTopic(nil, nil).Phrase("anything"); got != PhraseNone {
		t.Errorf("empty lists: %v", got)
	}
}

func TestScanTopicSuppressesTheInjectionClassifier(t *testing.T) {
	s, calls := scanner(t, 200, `{"is_valid": false}`, 0)
	allowed, err := NewLLMGuard(s.URL, "", time.Second).ScanTopic(context.Background(), "x")
	if err != nil || allowed {
		t.Fatalf("ScanTopic = %v, %v", allowed, err)
	}
	suppress, _ := calls()[0].body["scanners_suppress"].([]any)
	if calls()[0].path != "/analyze/prompt" || len(suppress) != 1 || suppress[0] != "PromptInjection" {
		t.Errorf("call = %+v", calls()[0])
	}
}
