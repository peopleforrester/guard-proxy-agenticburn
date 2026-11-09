// ABOUTME: Tests for the code-of-conduct check: whole-word spans, the mask, an empty list, and the
// ABOUTME: menu words that hold a term inside them.
// SPDX-License-Identifier: Apache-2.0
package guard

import (
	"reflect"
	"testing"
)

func TestConductMatchesWholeWordsInOrder(t *testing.T) {
	c := NewConduct([]string{"shit", "fuck", "fucking"})
	got := c.Matches("Fucking hell, two SHITS and a fuck")
	want := [][]int{{0, 7}, {18, 23}, {30, 34}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("Matches = %v; want %v", got, want)
	}
	if !c.On() {
		t.Error("a check with terms is off")
	}
}

func TestConductLeavesTheMenuAlone(t *testing.T) {
	c := NewConduct([]string{"spic", "cock", "dick", "cunt"})
	for _, text := range []string{"spicy spices", "a cocktail and a peacock", "Mr. Dickens", "Scunthorpe", ""} {
		if got := c.Matches(text); got != nil {
			t.Errorf("Matches(%q) = %v; want none", text, got)
		}
		if got := c.Mask(text); got != text {
			t.Errorf("Mask(%q) = %q; want it unchanged", text, got)
		}
	}
}

func TestConductMasksEverySpan(t *testing.T) {
	c := NewConduct([]string{"dickhead", "fuck"})
	if got, want := c.Mask("Here, you Dickhead. Fuck off, fucks."), "Here, you [redacted]. [redacted] off, [redacted]."; got != want {
		t.Errorf("Mask = %q; want %q", got, want)
	}
}

func TestConductWithAnEmptyListChecksNothing(t *testing.T) {
	c := NewConduct(nil)
	if c.On() || c.Matches("shit") != nil || c.Mask("shit") != "shit" {
		t.Error("an empty list checked something")
	}
}
