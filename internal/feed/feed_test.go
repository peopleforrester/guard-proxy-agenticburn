// ABOUTME: Tests for the moderated prompt feed: personal data, block-listed and profane terms masked
// ABOUTME: before the 280-character cut, whole-word matching, probes skipped, and the 50-entry bound.
// SPDX-License-Identifier: Apache-2.0
package feed

import (
	"fmt"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/peopleforrester/guard-proxy-agenticburn/internal/config"
	"github.com/peopleforrester/guard-proxy-agenticburn/internal/guard"
)

func moderator(profanity string) *Moderator {
	return NewModerator(guard.ParseBlocklist(guard.DefaultBlocklist), config.SplitList(strings.ToLower(profanity)))
}

func TestModerate(t *testing.T) {
	m := moderator(config.DefaultProfanity)
	for in, want := range map[string]string{
		"please delete the menu":                          "please [redacted] the menu",
		"Delete it, DELETE it, dElEtE it":                 "[redacted] it, [redacted] it, [redacted] it",
		"Fuck, FUCK, FuCk":                                "[redacted], [redacted], [redacted]",
		"this fucking bullshit, these bitches, two shits": "this [redacted] [redacted], these [redacted], two [redacted]",
		"email jane.doe+burrito@example.com or call (503) 555-0100, deliver to 1600 Pennsylvania Avenue": "email [redacted] or call [redacted], deliver to [redacted]",
		"card 4111 1111 1111 1111, or 4111-1111-1111-1111, or 4111111111111111":                          "card [redacted], or [redacted], or [redacted]",
		"ssn 123-45-6789, uk +44 20 7946 0958, local 555.0100":                                           "ssn [redacted], uk [redacted], local [redacted]",
		"send it to 42 elm st and ring twice":                                                            "send it to [redacted] and ring twice",
		"1600 Pennsylvania Avenue NW":                                                                    "[redacted] NW",
		"meet at 10 Downing St. tonight":                                                                 "meet at [redacted] tonight",
		"josé.núñez@example.com":                                                                         "[redacted]",
	} {
		if got := m.Moderate(in); got != want {
			t.Errorf("Moderate(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestModerateLeavesAnOrdinaryOrderAlone(t *testing.T) {
	order := "2 burritos and 3 tacos for the road, all the way, at 12:30, order 12345: spicy, spiced, " +
		"spices, a cocktail, grapes, a peacock and Dickens"
	if got := moderator(config.DefaultProfanity).Moderate(order); got != order {
		t.Errorf("Moderate changed an ordinary order: %q", got)
	}
}

func TestAddressStopwordsEndTheMatch(t *testing.T) {
	m := moderator("")
	for _, s := range []string{"3 tacos for the road", "2 of the street tacos", "12 and a half st", "7 elm"} {
		if got := m.Moderate(s); got != s {
			t.Errorf("Moderate(%q) = %q", s, got)
		}
	}
	// A word that only starts with a stopword is still a word.
	if got := m.Moderate("5 theory st"); got != "[redacted]" {
		t.Errorf("Moderate = %q", got)
	}
	// More than six digits is not a house number.
	if got := m.Moderate("x 1234567 is a code"); got != "x [redacted] is a code" {
		t.Errorf("Moderate = %q", got)
	}
}

func TestModerateIsLinearOnALongRun(t *testing.T) {
	m := moderator(config.DefaultProfanity)
	start := time.Now()
	m.Moderate(strings.Repeat("a.", 50000))
	// The reference's unanchored email pattern took 30 seconds here. The bound leaves room for the race
	// detector, which runs this about twenty times slower than a plain build.
	if took := time.Since(start); took > 10*time.Second {
		t.Errorf("100 KB took %v", took)
	}
}

func TestModerateCutsAfterMasking(t *testing.T) {
	m := moderator("")
	if got := m.Moderate(strings.Repeat("a", 269) + " 4111 1111 1111 1111"); got != strings.Repeat("a", 269)+" [redacted]" {
		t.Errorf("Moderate = %q", got)
	}
	// The cut counts characters, not bytes.
	if got := m.Moderate(strings.Repeat("é", 300)); got != strings.Repeat("é", 280) {
		t.Errorf("cut %d characters", len([]rune(got)))
	}
}

func TestEmptyProfanityListMasksNothing(t *testing.T) {
	if got := moderator("").Moderate("this shit is good"); got != "this shit is good" {
		t.Errorf("Moderate = %q", got)
	}
}

func TestFeedKeepsTheLatest50AndSkipsProbes(t *testing.T) {
	f := New(true, moderator(""))
	for i := 0; i < 55; i++ {
		f.Record(fmt.Sprintf("order %d", i), "")
	}
	f.Record("smoke "+ProbeMarker, "Agatha the Banshee")
	got := f.Prompts()
	if len(got) != 50 || got[0] != "order 5" || got[49] != "order 54" {
		t.Errorf("Prompts = %v", got)
	}
}

func TestWithoutProbeDropsOnlyTheMarker(t *testing.T) {
	for in, want := range map[string]string{
		"one burrito " + ProbeMarker: "one burrito",
		ProbeMarker + " two tacos":   "two tacos",
		"  " + ProbeMarker + " ":     "",
		"  one burrito, no marker\n": "  one burrito, no marker\n",
	} {
		if got := WithoutProbe(in); got != want {
			t.Errorf("WithoutProbe(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestDisabledFeedRecordsNothing(t *testing.T) {
	f := New(false, moderator(""))
	f.Record("one burrito", "Agatha the Banshee")
	if got := f.Prompts(); !reflect.DeepEqual(got, []string{}) || f.Enabled() {
		t.Errorf("Prompts = %v, Enabled = %v", got, f.Enabled())
	}
}

func TestFeedCarriesEachPromptsWitchNameAtItsIndex(t *testing.T) {
	f := New(true, moderator(""))
	f.Record("one burrito", "Agatha the Banshee")
	f.Record("no name here", "")
	f.Record("call 555 123 4567", "Hazel the Ghoul")
	prompts, witches := f.Snapshot()
	if !reflect.DeepEqual(prompts, []string{"one burrito", "no name here", "call [redacted]"}) {
		t.Errorf("prompts = %v", prompts)
	}
	if !reflect.DeepEqual(witches, []string{"Agatha the Banshee", "", "Hazel the Ghoul"}) {
		t.Errorf("witches = %q", witches)
	}
}

func TestFeedDropsAWitchNameModerationWouldMask(t *testing.T) {
	f := New(true, moderator(config.DefaultProfanity))
	f.Record("one burrito", "Shit the Ghoul")
	f.Record("two burritos", "Delete the Witch")
	if _, witches := f.Snapshot(); !reflect.DeepEqual(witches, []string{"", ""}) {
		t.Errorf("witches = %q", witches)
	}
}

func TestFeedWitchesKeepTheLatest50InStepWithPrompts(t *testing.T) {
	f := New(true, moderator(""))
	for i := 0; i < 55; i++ {
		f.Record(fmt.Sprintf("order %d", i), fmt.Sprintf("Witch the %s", strings.Repeat("o", i%15+1)))
	}
	prompts, witches := f.Snapshot()
	if len(prompts) != 50 || len(witches) != 50 {
		t.Fatalf("kept %d prompts, %d witches", len(prompts), len(witches))
	}
	for i := range prompts {
		n := i + 5
		if prompts[i] != fmt.Sprintf("order %d", n) || witches[i] != "Witch the "+strings.Repeat("o", n%15+1) {
			t.Errorf("index %d: %q by %q", i, prompts[i], witches[i])
		}
	}
}

func TestDisabledFeedRecordsNoWitchName(t *testing.T) {
	f := New(false, moderator(""))
	f.Record("one burrito", "Agatha the Banshee")
	if prompts, witches := f.Snapshot(); len(prompts) != 0 || len(witches) != 0 {
		t.Errorf("prompts = %v, witches = %q", prompts, witches)
	}
}
