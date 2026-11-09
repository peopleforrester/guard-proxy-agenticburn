// ABOUTME: The code-of-conduct check every /chat turn gets whatever the guards say (#78): one matcher
// ABOUTME: over the wall's term list, folded spellings included (#81), read by the refusal, the mask and the wall.
// SPDX-License-Identifier: Apache-2.0
package guard

import (
	"fmt"
	"regexp"
	"sort"
	"strings"
	"unicode"
	"unicode/utf8"

	"golang.org/x/text/unicode/norm"
)

// ConductRefusal answers a /chat prompt the check refuses. It names no term.
const ConductRefusal = "Let's keep it kind: BurritoBot only answers within the event's code of conduct."

// Conduct is the code-of-conduct check (Unleash_an_Agent_Watch_It_Burn#1267). It is the wall's own list,
// so the wall, the chat page's saved rail and the chat agree on what a term is. In-process, with no
// network call. An empty list checks nothing.
//
// It is proxy.py's conduct_matches, which follows the spike's floor.py (guard-proxy-agenticburn#81). Each
// character of a text is folded on its own before matching, so a match maps back onto the original text
// one to one: zero-width characters and soft hyphens are dropped, compatibility and accented forms reduced
// to their base letter, Cyrillic and Greek look-alikes mapped to Latin, and the result lower-cased. A term
// then matches as a whole word with each letter repeatable as itself or a digit stand-in, each letter's
// leetspeak stand-ins, "*" for any vowel, or the whole term spelt out with one separator between every letter; a phrase's words match with
// any run of separators between them. RE2 has no lookaround, so the left word edge is checked by hand and
// the right one is consumed after the capture group, which leftmost-first matching resolves as Python's
// lookahead does.
type Conduct struct{ re *regexp.Regexp }

// NewConduct checks for terms as the spike's floor matches them.
func NewConduct(terms []string) Conduct { return Conduct{re: conductPattern(terms)} }

// On reports whether the check has a term to look for.
func (c Conduct) On() bool { return c.re != nil }

// Matches is the check's one matcher: the [start, end) byte spans of text that break the event's code of
// conduct, in order. The refusal and the mask both read it, so a classifier replaces the term list here
// alone; one with no spans answers the whole text as one.
func (c Conduct) Matches(text string) [][]int {
	if c.re == nil || text == "" {
		return nil
	}
	folded, starts, ends := conductFold(text)
	var spans [][]int
	for pos := 0; pos < len(folded); {
		loc := c.re.FindStringSubmatchIndex(folded[pos:])
		if loc == nil {
			break
		}
		start, end := pos+loc[2], pos+loc[3]
		if start > 0 && isWordByte(folded[start-1]) {
			// Not a word's start: Python's lookbehind fails here, and the search goes on one character later.
			_, size := utf8.DecodeRuneInString(folded[start:])
			pos = start + size
			continue
		}
		spans = append(spans, []int{starts[start], ends[end-1]})
		pos = end
	}
	return spans
}

// Mask replaces every span Matches finds with [redacted], the wall's own mark.
func (c Conduct) Mask(text string) string {
	spans := c.Matches(text)
	if len(spans) == 0 {
		return text
	}
	var b strings.Builder
	last := 0
	for _, s := range spans {
		b.WriteString(text[last:s[0]])
		b.WriteString("[redacted]")
		last = s[1]
	}
	b.WriteString(text[last:])
	return b.String()
}

// conductLeet is each letter's leetspeak stand-ins.
var conductLeet = map[rune]string{'a': "4@", 'b': "8", 'c': "(", 'e': "3", 'g': "9", 'i': "1!|", 'l': "1|",
	'o': "0", 's': "5$", 't': "7+"}

// conductSep is a separator between spelt-out letters or a phrase's words: Python's \s (str.isspace), or one
// of . _ * -.
const conductSep = `[\pZ\t\n\v\f\r\x{1c}-\x{1f}\x{85}._*\-]`

var conductZeroWidth = map[rune]bool{'\u200b': true, '\u200c': true, '\u200d': true, '\u2060': true,
	'\ufeff': true, '\u00ad': true}

// conductHomoglyph maps the Cyrillic and Greek letters that render as Latin ones.
var conductHomoglyph = func() map[rune]rune {
	from := []rune("аеорсухіјѕԁԛԝкмтвнАЕОРСУХІЈЅКМТВНαοερτυνκιΑΒΕΖΗΙΚΜΝΟΡΤΥΧ")
	to := []rune("aeopcyxijsdqwkmtbhaeopcyxijskmtbhaoeptuvkiabezhikmnoptyx")
	if len(from) != len(to) {
		panic("guard: the homoglyph table's two sides differ in length")
	}
	m := make(map[rune]rune, len(from))
	for i, r := range from {
		m[r] = to[i]
	}
	return m
}()

// foldRune is one character folded: its compatibility decomposition's first character that is not a
// combining mark (the character itself when there is none), a look-alike mapped to Latin, lower-cased.
func foldRune(r rune) rune {
	if r < utf8.RuneSelf {
		return unicode.ToLower(r)
	}
	base := r
	for _, c := range norm.NFKD.String(string(r)) {
		if norm.NFD.PropertiesString(string(c)).CCC() == 0 {
			base = c
			break
		}
	}
	if h, ok := conductHomoglyph[base]; ok {
		base = h
	}
	return unicode.ToLower(base)
}

// conductFold is text folded one character at a time and, for each byte of the folded text, the byte span
// in text of the character it came from.
func conductFold(text string) (string, []int, []int) {
	var b strings.Builder
	b.Grow(len(text))
	starts := make([]int, 0, len(text))
	ends := make([]int, 0, len(text))
	for i := 0; i < len(text); {
		r, size := utf8.DecodeRuneInString(text[i:])
		if !conductZeroWidth[r] {
			f := foldRune(r)
			b.WriteRune(f)
			for k := utf8.RuneLen(f); k > 0; k-- {
				starts = append(starts, i)
				ends = append(ends, i+size)
			}
		}
		i += size
	}
	return b.String(), starts, ends
}

// isWordByte is [a-z0-9], the word characters on either side of a match in the folded text.
func isWordByte(c byte) bool { return 'a' <= c && c <= 'z' || '0' <= c && c <= '9' }

// isPySpace is Python's str.isspace: the Unicode separators and the control characters it counts as space.
func isPySpace(r rune) bool {
	switch r {
	case '\t', '\n', '\v', '\f', '\r', 0x1c, 0x1d, 0x1e, 0x1f, 0x85:
		return true
	}
	return unicode.In(r, unicode.Z)
}

// classRune writes r for use inside a character class: a letter or digit as itself, anything else escaped.
func classRune(b *strings.Builder, r rune) {
	if r < utf8.RuneSelf && isWordByte(byte(r)) {
		b.WriteRune(r)
		return
	}
	fmt.Fprintf(b, `\x{%x}`, r)
}

// conductLetter is one letter of a term: itself, its leetspeak stand-ins, and "*" for a vowel.
func conductLetter(r rune) string {
	var b strings.Builder
	b.WriteByte('[')
	classRune(&b, r)
	for _, l := range conductLeet[r] {
		classRune(&b, l)
	}
	if strings.ContainsRune("aeiou", r) {
		b.WriteString(`\*`)
	}
	b.WriteByte(']')
	return b.String()
}

// conductRun is one letter of a word, after being the letter that follows it (0 at the word's end): one
// character of its class, then any run of the letter and its digit stand-ins that the next letter's class
// does not hold. With each letter its whole class repeated, a run of `$`, `5$`, `(` or `4@` cost time in the
// square of its length in a backtracking engine (Unleash_an_Agent_Watch_It_Burn#1300). RE2 is linear in any
// pattern, but this builds the pattern proxy.py's _conduct_run and the spike's floor.py _run build.
func conductRun(r, after rune) string {
	own := string(r) + conductLeet[r]
	held := conductLeet[after]
	if after != 0 {
		held = string(after) + held
	}
	var again []rune
	for _, c := range own {
		if (c == r || '0' <= c && c <= '9') && !strings.ContainsRune(held, c) {
			again = append(again, c)
		}
	}
	switch {
	case len(again) == 0:
		return conductLetter(r)
	case string(again) == own && !strings.ContainsRune("aeiou", r):
		return conductLetter(r) + "+"
	}
	var b strings.Builder
	b.WriteByte('[')
	for _, c := range again {
		classRune(&b, c)
	}
	b.WriteString("]*")
	return conductLetter(r) + b.String()
}

// conductWord is one word of a term, each letter a conductRun.
func conductWord(word string) string {
	rs := []rune(word)
	var b strings.Builder
	for i, r := range rs {
		var after rune
		if i+1 < len(rs) {
			after = rs[i+1]
		}
		b.WriteString(conductRun(r, after))
	}
	return b.String()
}

// conductWords is a term folded and split into words, as Python's str.split() splits it.
func conductWords(term string) []string {
	folded, _, _ := conductFold(term)
	return strings.FieldsFunc(folded, isPySpace)
}

// conductTerm is one term's pattern: every letter a conductRun, the words joined by any run of separators,
// and a one-word term of three or more letters also spelt out with one separator between every letter.
func conductTerm(term string) string {
	words := conductWords(term)
	joined := make([]string, len(words))
	for i, w := range words {
		joined[i] = conductWord(w)
	}
	pattern := strings.Join(joined, conductSep+"+")
	if len(words) == 1 && utf8.RuneCountInString(words[0]) >= 3 {
		var spaced []string
		for _, r := range words[0] {
			spaced = append(spaced, conductLetter(r))
		}
		return "(?:" + pattern + "|" + strings.Join(spaced, conductSep) + ")"
	}
	return "(?:" + pattern + ")"
}

// conductPattern is the terms, longest first as Python sorts them, with an optional plural "s", then the
// right word edge. An empty list, or one whose every term folds to nothing, matches nothing.
func conductPattern(terms []string) *regexp.Regexp {
	var kept []string
	for _, t := range terms {
		if len(conductWords(t)) > 0 {
			kept = append(kept, t)
		}
	}
	if len(kept) == 0 {
		return nil
	}
	sort.SliceStable(kept, func(i, j int) bool {
		return utf8.RuneCountInString(kept[i]) > utf8.RuneCountInString(kept[j])
	})
	alts := make([]string, len(kept))
	for i, t := range kept {
		alts[i] = conductTerm(t)
	}
	return regexp.MustCompile(`((?:` + strings.Join(alts, "|") + `)s?)(?:[^a-z0-9]|$)`)
}
