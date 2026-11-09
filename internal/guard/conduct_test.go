// ABOUTME: Tests for the code-of-conduct check: whole-word spans, the mask, an empty list, and the
// ABOUTME: menu words that hold a term inside them.
// SPDX-License-Identifier: Apache-2.0
package guard

import (
	"context"
	"os"
	"reflect"
	"regexp"
	"strings"
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

// rot13 decodes the spike's fixtures, which keep their terms ROT13-encoded.
func rot13(s string) string {
	return strings.Map(func(r rune) rune {
		switch {
		case 'a' <= r && r <= 'z':
			return 'a' + (r-'a'+13)%26
		case 'A' <= r && r <= 'Z':
			return 'A' + (r-'A'+13)%26
		}
		return r
	}, s)
}

// spikeTerms is the default list as the spike's floor reads it, from the conformance fixture's own source:
// config holds it, and config imports this package.
func spikeTerms(t *testing.T) []string {
	t.Helper()
	raw, err := os.ReadFile("../../proxy.py")
	if err != nil {
		t.Fatal(err)
	}
	m := regexp.MustCompile(`(?s)_DEFAULT_PROFANITY = codecs\.decode\((.*?)"rot13",\n\)`).FindSubmatch(raw)
	if m == nil {
		t.Fatal("proxy.py holds no _DEFAULT_PROFANITY")
	}
	var b strings.Builder
	for _, q := range regexp.MustCompile(`"([^"]*)"`).FindAllSubmatch(m[1], -1) {
		b.Write(q[1])
	}
	return strings.Split(rot13(b.String()), ",")
}

func TestConductMatchesTheSpikesMaskOnTheAbuseSet(t *testing.T) {
	c := NewConduct(spikeTerms(t))
	raw, err := os.ReadFile("../../conformance/coc_floor/abuse-set.rot13.tsv")
	if err != nil {
		t.Fatal(err)
	}
	caught := 0
	for _, line := range strings.Split(strings.TrimSpace(string(raw)), "\n") {
		if strings.HasPrefix(line, "#") {
			continue
		}
		f := strings.Split(line, "\t")
		text, want := rot13(f[1]), rot13(f[2])
		if got := c.Mask(text); got != want {
			// ROT13-encoded, as the fixture keeps them, so a failure prints no term.
			t.Errorf("%s: Mask = %q; want %q", f[0], rot13(got), f[2])
		}
		if want != text {
			caught++
		}
	}
	if caught != 111 {
		t.Errorf("caught %d of the abuse set; the spike's floor catches 111", caught)
	}
}

func TestConductFoldsEvasions(t *testing.T) {
	c := NewConduct([]string{"fuck", "cunt", "puta", "kill yourself"})
	for _, tc := range []struct{ text, want string }{
		{"fuuuuck", "[redacted]"},
		{"f*ck it", "[redacted] it"},
		{"FUCK", "[redacted]"},
		{"f u c k", "[redacted]"},
		{"c.u.n.t!", "[redacted]!"},
		{"fu\u200bck", "[redacted]"},
		{"ｆｕｃｋ", "[redacted]"},
		{"fúck", "[redacted]"},
		{"fuсk", "[redacted]"}, // a Cyrillic "с"
		{"kill   yourself", "[redacted]"},
		{"k1ll y0urs3lf", "[redacted]"},
		{"put a hat on", "put a hat on"},
		{"fucks", "[redacted]"},
		{"fuckx", "fuckx"},
		{"xfuck fuck", "xfuck [redacted]"},
		{"«fuck»", "«[redacted]»"},
	} {
		if got := c.Mask(tc.text); got != tc.want {
			t.Errorf("Mask(%q) = %q; want %q", tc.text, got, tc.want)
		}
	}
}

func TestConductSpansAreTheOriginalBytes(t *testing.T) {
	c := NewConduct([]string{"fuck"})
	text := "é ｆｕ\u200bｃｋ!"
	got := c.Matches(text)
	want := [][]int{{3, len(text) - 1}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("Matches(%q) = %v; want %v", text, got, want)
	}
}

func TestConductTermThatFoldsToNothingIsNoTerm(t *testing.T) {
	c := NewConduct([]string{"\u200b", " "})
	if c.On() || c.Matches("anything") != nil {
		t.Error("a term of zero-width characters matched")
	}
}

func TestNoGuardrailBlocksNothing(t *testing.T) {
	g, err := NewGuardrail(context.Background(), "", "")
	if g != nil || err != nil {
		t.Fatalf("NewGuardrail with no id = %v, %v; want nil, nil", g, err)
	}
	if blocked, err := g.Blocks(context.Background(), "anything"); blocked || err != nil {
		t.Errorf("a nil Guardrail: Blocks = %v, %v", blocked, err)
	}
}

// The #1300 letter rule (Unleash_an_Agent_Watch_It_Burn#1300): a letter is one character of its class, then a
// run of only the letter and its digit stand-ins, less any the next letter's class holds. These are the
// spike's floor.py _term for the same words, written in RE2's escapes.
func TestConductTermIsOneCharacterThenARunOfTheLetter(t *testing.T) {
	sep := conductSep
	for term, want := range map[string]string{
		"ass": `(?:[a4\x{40}\*][a4]*[s5\x{24}][s5\x{24}][s5]*|` +
			`[a4\x{40}\*]` + sep + `[s5\x{24}]` + sep + `[s5\x{24}])`,
		"kill": `(?:[k]+[i1\x{21}\x{7c}\*][i]*[l1\x{7c}][l1\x{7c}][l1]*|` +
			`[k]` + sep + `[i1\x{21}\x{7c}\*]` + sep + `[l1\x{7c}]` + sep + `[l1\x{7c}])`,
		"f u": `(?:[f]+` + sep + `+[u\*][u]*)`,
	} {
		if got := conductTerm(term); got != want {
			t.Errorf("conductTerm(%q) = %s; want %s", term, got, want)
		}
	}
}

// A run of the symbols the old form split every way (#1300) matches in RE2 as it does in proxy.py: no term.
func TestConductLeetRunsMatchNothing(t *testing.T) {
	c := NewConduct(spikeTerms(t))
	for _, unit := range []string{"$", "5$", "(", "4@"} {
		if got := c.Matches(strings.Repeat(unit, 2000)); got != nil {
			t.Errorf("a run of %q matched %v", unit, got)
		}
	}
}
