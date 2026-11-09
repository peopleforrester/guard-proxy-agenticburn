// ABOUTME: The code-of-conduct check every /chat turn gets whatever the guards say (#78): one matcher
// ABOUTME: over the wall's term list, read by the prompt's refusal and the reply's mask alike.
// SPDX-License-Identifier: Apache-2.0
package guard

import (
	"regexp"
	"strings"
)

// ConductRefusal answers a /chat prompt the check refuses. It names no term.
const ConductRefusal = "Let's keep it kind: BurritoBot only answers within the event's code of conduct."

// Conduct is the code-of-conduct check (Unleash_an_Agent_Watch_It_Burn#1267). It is the wall's own list,
// matched as WholeWords does, so the wall, the chat page's saved rail and the chat agree on what a term
// is. In-process, with no network call. An empty list checks nothing.
type Conduct struct{ re *regexp.Regexp }

// NewConduct checks for terms as whole words, plural included, case-insensitive.
func NewConduct(terms []string) Conduct { return Conduct{re: WholeWords(terms)} }

// On reports whether the check has a term to look for.
func (c Conduct) On() bool { return c.re != nil }

// Matches is the check's one matcher: the [start, end) byte spans of text that break the event's code of
// conduct, in order. The refusal and the mask both read it, so a classifier replaces the term list here
// alone; one with no spans answers the whole text as one.
func (c Conduct) Matches(text string) [][]int {
	if c.re == nil || text == "" {
		return nil
	}
	return c.re.FindAllStringIndex(text, -1)
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
