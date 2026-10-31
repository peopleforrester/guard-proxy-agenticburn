// ABOUTME: The moderated prompt feed behind GET /prompts: personal data, block-listed and profane terms
// ABOUTME: masked before a 280-character cut, probes skipped, and only the latest 50 kept.
// SPDX-License-Identifier: Apache-2.0
package feed

import (
	"regexp"
	"strings"
	"sync"
	"unicode"

	"github.com/peopleforrester/guard-proxy-agenticburn/internal/guard"
)

// ProbeMarker marks an automated probe's prompt. A probe is answered normally and never recorded, so
// smoke tests do not fill the projected wall with prompts nobody typed.
const ProbeMarker = "[[wib-probe]]"

// IsProbe reports whether text carries the probe marker.
func IsProbe(text string) bool { return strings.Contains(text, ProbeMarker) }

const (
	redacted = "[redacted]"
	maxChars = 280
	keep     = 50
)

// The wall is projected, so a prompt carrying an email, a phone or card number, or a street address
// goes up redacted. Only the feed copy is masked; the prompt reaches the bot unchanged. Deterministic and
// a floor: a name on its own is not caught.
var (
	// Email addresses. RE2 matches in linear time, so the reference's lookbehind, which is there to keep
	// Python's backtracking linear, is not needed; a match starts at the start of an address run either
	// way. The classes are Unicode, as Python's \w is.
	emailRE = regexp.MustCompile(`[\p{L}\p{N}_.+-]+@[\p{L}\p{N}_-]+(?:\.[\p{L}\p{N}_-]+)+`)
	// Seven or more digits in one run, with spaces, dots, dashes or brackets between them: phone, card,
	// US Social Security and account numbers. A burrito order never needs seven digits in a row.
	digitsRE = regexp.MustCompile(`\+?\(?\p{Nd}(?:[\s\v\p{Zs}().-]{0,2}\p{Nd}){6,}\)?`)
)

// addressStopwords end an address match: "3 tacos for the road" stays.
var addressStopwords = []string{"the", "a", "an", "and", "or", "for", "on", "to", "of", "in", "at", "with",
	"my", "your", "our", "all", "some"}

// addressSuffixes are the street suffixes, in the reference's alternation order.
var addressSuffixes = []string{"street", "st", "avenue", "ave", "road", "rd", "boulevard", "blvd", "lane", "ln",
	"drive", "dr", "way", "court", "ct", "place", "pl", "terrace", "ter", "parkway", "pkwy", "highway", "hwy",
	"circle", "cir"}

// Moderator masks text for the feed.
type Moderator struct {
	blocklist []*regexp.Regexp
	profanity *regexp.Regexp
}

// NewModerator masks the block-list's terms as case-insensitive substrings, in list order, and the
// profanity terms as whole words with an optional plural "s". An empty profanity list masks nothing.
func NewModerator(blocklist guard.Blocklist, profanity []string) *Moderator {
	m := &Moderator{}
	for _, t := range blocklist {
		m.blocklist = append(m.blocklist, regexp.MustCompile(`(?i)`+regexp.QuoteMeta(t)))
	}
	m.profanity = guard.WholeWords(profanity)
	return m
}

// Moderate masks personal data first, while an address or number is still whole, then block-listed and
// profane terms, and cuts the result to 280 characters, so the cut never leaves half a number showing.
func (m *Moderator) Moderate(text string) string {
	masked := emailRE.ReplaceAllLiteralString(text, redacted)
	masked = digitsRE.ReplaceAllLiteralString(masked, redacted)
	masked = maskAddresses(masked)
	for _, re := range m.blocklist {
		masked = re.ReplaceAllLiteralString(masked, redacted)
	}
	if m.profanity != nil {
		masked = m.profanity.ReplaceAllLiteralString(masked, redacted)
	}
	if r := []rune(masked); len(r) > maxChars {
		masked = string(r[:maxChars])
	}
	return masked
}

// maskAddresses replaces each street address: a house number of one to six digits at a word boundary,
// one to four words, and a street suffix ("1600 Pennsylvania Avenue", "42 elm st"). It is the
// reference's regular expression written out by hand, because its stopword lookahead has no RE2 form.
func maskAddresses(s string) string {
	rs := []rune(s)
	var out strings.Builder
	for i := 0; i < len(rs); {
		if end := addressAt(rs, i); end > 0 {
			out.WriteString(redacted)
			i = end
			continue
		}
		out.WriteRune(rs[i])
		i++
	}
	return out.String()
}

// addressAt returns the end of an address starting at i, or -1.
func addressAt(rs []rune, i int) int {
	if !unicode.IsDigit(rs[i]) || (i > 0 && isWord(rs[i-1])) {
		return -1
	}
	j := i
	for j < len(rs) && unicode.IsDigit(rs[j]) {
		j++
	}
	// \d{1,6} must be followed by whitespace, so a longer run of digits is never a house number.
	if j-i > 6 {
		return -1
	}
	// The tokens after the number: each starts after a run of whitespace.
	var starts, ends []int
	for p := j; len(starts) < 5; {
		q := p
		for q < len(rs) && unicode.IsSpace(rs[q]) {
			q++
		}
		if q == p || q == len(rs) {
			break
		}
		e := q
		for e < len(rs) && !unicode.IsSpace(rs[e]) {
			e++
		}
		starts, ends = append(starts, q), append(ends, e)
		p = e
	}
	// The words are as many valid tokens in a row as there are, up to four.
	words := 0
	for words < len(starts) && words < 4 && addressWord(rs[starts[words]:ends[words]]) {
		words++
	}
	// Fewer words first only after more words fail, as the greedy {1,4} backtracks.
	for k := words; k >= 1; k-- {
		if k >= len(starts) {
			continue
		}
		if end := suffixAt(rs, starts[k]); end > 0 {
			return end
		}
	}
	return -1
}

// addressWord is a whole token of letters, digits, dots, apostrophes and hyphens that does not start
// with a stopword at a word boundary ("the" is one, "theory" is not).
func addressWord(tok []rune) bool {
	for _, r := range tok {
		if !isASCIILetter(r) && !(r >= '0' && r <= '9') && r != '.' && r != '\'' && r != '-' {
			return false
		}
	}
	for _, sw := range addressStopwords {
		if hasPrefixFold(tok, sw) && (len(tok) == len(sw) || !isWord(tok[len(sw)])) {
			return false
		}
	}
	return true
}

// suffixAt returns the end of a street suffix at p, an optional dot included, or -1.
func suffixAt(rs []rune, p int) int {
	for _, suf := range addressSuffixes {
		end := p + len(suf)
		if hasPrefixFold(rs[p:], suf) && (end == len(rs) || !isWord(rs[end])) {
			if end < len(rs) && rs[end] == '.' {
				end++
			}
			return end
		}
	}
	return -1
}

func hasPrefixFold(rs []rune, prefix string) bool {
	if len(rs) < len(prefix) {
		return false
	}
	for i := 0; i < len(prefix); i++ {
		if unicode.ToLower(rs[i]) != rune(prefix[i]) {
			return false
		}
	}
	return true
}

func isASCIILetter(r rune) bool { return (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') }

// isWord is Python's \w: a letter, digit or underscore in any script (combining marks are not word characters there).
func isWord(r rune) bool {
	return r == '_' || unicode.IsLetter(r) || unicode.IsDigit(r) || unicode.IsNumber(r)
}

// Feed holds the latest moderated prompts. It is safe for concurrent use.
type Feed struct {
	enabled bool
	mod     *Moderator

	mu      sync.Mutex
	prompts []string
}

// New returns a feed that records only when enabled.
func New(enabled bool, mod *Moderator) *Feed {
	return &Feed{enabled: enabled, mod: mod}
}

// Enabled reports whether the feed captures prompts.
func (f *Feed) Enabled() bool { return f.enabled }

// Record moderates text and keeps it, unless the feed is off or text is a probe.
func (f *Feed) Record(text string) {
	if !f.enabled || IsProbe(text) {
		return
	}
	masked := f.mod.Moderate(text)
	f.mu.Lock()
	defer f.mu.Unlock()
	f.prompts = append(f.prompts, masked)
	if len(f.prompts) > keep {
		f.prompts = append([]string(nil), f.prompts[len(f.prompts)-keep:]...)
	}
}

// Prompts is a copy of the kept prompts, oldest first.
func (f *Feed) Prompts() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string{}, f.prompts...)
}
