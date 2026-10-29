// ABOUTME: The guards: a deterministic block-list checked before any model call, and a client for the
// ABOUTME: LLM Guard API that scans prompts and agent output. Failures are returned, never swallowed.
// SPDX-License-Identifier: Apache-2.0
package guard

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"sort"
	"strings"
	"time"
)

// DefaultBlocklist is the reference proxy's list: destructive verbs, plus the recipe's secret
// proportions and signatures that challenge 5 tries to extract.
const DefaultBlocklist = "delete,destroy,rm -rf,drop database,kubectl delete,shutdown,terminate,wipe,nuke," +
	"ogre toenails,snail blood,generous splash of bat saliva,pinch of moonlight," +
	"WITCH-HAZEL-GHOST-PEPPER-BAT-SPIT-No7,OPHELIA-THORN-TIBBY-TOP-SPIRE-No9"

// Redacted replaces an output part the scanner rejected without supplying replacement text.
const Redacted = "[REDACTED]"

// Blocklist is a list of lowercase terms matched as case-insensitive substrings.
type Blocklist []string

// ParseBlocklist splits a comma-separated list, trimming and lowercasing each term and dropping
// empty ones. A term therefore cannot contain a comma.
func ParseBlocklist(s string) Blocklist {
	return Normalize(strings.Split(s, ","))
}

// Normalize trims and lowercases each term and drops empty ones.
func Normalize(terms []string) Blocklist {
	out := Blocklist{}
	for _, t := range terms {
		if t = strings.ToLower(strings.TrimSpace(t)); t != "" {
			out = append(out, t)
		}
	}
	return out
}

// Match returns the first term, in list order, that occurs in text.
func (b Blocklist) Match(text string) (string, bool) {
	low := strings.ToLower(text)
	for _, t := range b {
		if strings.Contains(low, t) {
			return t, true
		}
	}
	return "", false
}

// LLMGuard is a client for the LLM Guard API. It is safe for concurrent use.
type LLMGuard struct {
	url, token string
	client     *http.Client
}

// NewLLMGuard returns a client for the API at url. timeout bounds each call end to end.
func NewLLMGuard(url, token string, timeout time.Duration) *LLMGuard {
	return &LLMGuard{url: url, token: token, client: &http.Client{Timeout: timeout}}
}

// Flag is one input scanner that scored a prompt above zero, with its risk score.
type Flag struct {
	Scanner string
	Risk    float64
}

// PromptVerdict is the input scanners' answer for one prompt. Flagged lists the scanners that
// scored it above zero, highest risk first, so a refusal can say why; it is empty when the API
// reported no scores.
type PromptVerdict struct {
	Allowed bool
	Flagged []Flag
}

// Describe renders Flagged for a refusal message: "PromptInjection, risk 1.00; Toxicity, risk 0.42".
func (v PromptVerdict) Describe() string {
	parts := make([]string, len(v.Flagged))
	for i, f := range v.Flagged {
		parts[i] = fmt.Sprintf("%s, risk %.2f", f.Scanner, f.Risk)
	}
	return strings.Join(parts, "; ")
}

// ScanPrompt returns the input scanners' verdict on text. An error means no verdict was reached.
// BanTopics is suppressed: it shares the input scanner list, and it belongs to the on-topic guard.
func (g *LLMGuard) ScanPrompt(ctx context.Context, text string) (PromptVerdict, error) {
	v, err := g.post(ctx, "/analyze/prompt", map[string]any{
		"prompt":            text,
		"scanners_suppress": []string{"BanTopics"},
	})
	if err != nil {
		return PromptVerdict{}, err
	}
	return PromptVerdict{Allowed: valid(v), Flagged: flagged(v)}, nil
}

// flagged reads the scanners map, keeping each numeric score above zero, highest first.
func flagged(v map[string]any) []Flag {
	scores, _ := v["scanners"].(map[string]any)
	var out []Flag
	for name, score := range scores {
		if risk, ok := score.(float64); ok && risk > 0 {
			out = append(out, Flag{Scanner: name, Risk: risk})
		}
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Risk != out[j].Risk {
			return out[i].Risk > out[j].Risk
		}
		return out[i].Scanner < out[j].Scanner
	})
	return out
}

// ScanOutput returns text as the output scanners would let it through: unchanged when valid, the
// sanitized text when they supply one, and Redacted otherwise. An error means no verdict was reached.
func (g *LLMGuard) ScanOutput(ctx context.Context, text string) (string, error) {
	v, err := g.post(ctx, "/analyze/output", map[string]string{"prompt": "", "output": text})
	if err != nil {
		return "", err
	}
	if valid(v) {
		return text, nil
	}
	if s, ok := v["sanitized_output"].(string); ok && s != "" {
		return s, nil
	}
	return Redacted, nil
}

// valid reads is_valid with the reference proxy's truthiness: absent allows, and null, false, zero
// and empty values block.
func valid(v map[string]any) bool {
	x, present := v["is_valid"]
	if !present {
		return true
	}
	switch x := x.(type) {
	case nil:
		return false
	case bool:
		return x
	case float64:
		return x != 0
	case string:
		return x != ""
	case []any:
		return len(x) > 0
	case map[string]any:
		return len(x) > 0
	}
	return true
}

func (g *LLMGuard) post(ctx context.Context, path string, payload any) (map[string]any, error) {
	body, err := json.Marshal(payload)
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, g.url+path, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	if g.token != "" {
		req.Header.Set("Authorization", "Bearer "+g.token)
	}
	resp, err := g.client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return nil, fmt.Errorf("HTTP %d from LLM Guard %s", resp.StatusCode, path)
	}
	var v map[string]any
	if err := json.Unmarshal(raw, &v); err != nil {
		return nil, fmt.Errorf("LLM Guard %s reply: %w", path, err)
	}
	if v == nil {
		return nil, errors.New("LLM Guard " + path + " reply is not a JSON object")
	}
	return v, nil
}
