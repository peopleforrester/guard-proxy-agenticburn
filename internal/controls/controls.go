// ABOUTME: The cluster-object controls behind the /controls badges, read live from the Kubernetes API
// ABOUTME: with the pod's own service account token, get-only on named objects, cached for 5 seconds.
// SPDX-License-Identifier: Apache-2.0
package controls

import (
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"
)

// cacheTTL is how long one reading serves the badge poll, which runs every 12 seconds.
const cacheTTL = 5 * time.Second

// requestTimeout bounds each API read.
const requestTimeout = 5 * time.Second

// The C7 gateway rule's contract with the workshop repo (Unleash_an_Agent_Watch_It_Burn #514): ConfigMap
// agent/agentgateway-c7-config, key config.yaml, and the one line ending `# c7-rule`. 'true' is open;
// the web terminal's guard-mcp-on rewrites that line to admit get_weather alone.
const (
	c7Marker   = "# c7-rule"
	c7Open     = "true"
	c7Narrowed = `mcp.tool.name == "get_weather"`
)

// Infra is each badge's state: installed or enforcing, measured absent, or (nil) could not be read.
type Infra struct {
	// NetworkPolicy is C1: the agent-egress-allowlist NetworkPolicy exists.
	NetworkPolicy bool `json:"networkpolicy"`
	// Kyverno is C2: restrict-image-registries' validationActions list holds Deny.
	Kyverno bool `json:"kyverno"`
	// KubeArmor is C3 prevention: the block-recipe-snoop KubeArmorPolicy exists.
	KubeArmor bool `json:"kubearmor"`
	// Falco is C3 detection: the security/falco DaemonSet has a ready pod.
	Falco bool `json:"falco"`
	// ToolAllowlist is C7: the agentgateway rule or the agent's toolNames blocks the rogue tools.
	ToolAllowlist *bool `json:"tool_allowlist"`
	// RBACScoped is C8: the agent's Role no longer grants broad secrets access.
	RBACScoped *bool `json:"rbac_scoped"`
}

// Reader reads the badges. It is safe for concurrent use.
type Reader struct {
	apiURL, tokenPath, caPath string
	now                       func() time.Time

	mu     sync.Mutex
	at     time.Time
	cached *Infra
}

// NewReader reads from the API server at apiURL with the token in tokenPath, trusting the CA in caPath
// when that file exists.
func NewReader(apiURL, tokenPath, caPath string) *Reader {
	return &Reader{apiURL: apiURL, tokenPath: tokenPath, caPath: caPath, now: time.Now}
}

// Read returns the badges, from the cache when it is fresh.
func (r *Reader) Read() Infra {
	now := r.now()
	r.mu.Lock()
	if r.cached != nil && now.Sub(r.at) < cacheTTL {
		v := *r.cached
		r.mu.Unlock()
		return v
	}
	r.mu.Unlock()

	client := r.client()
	get := func(path string) any { return r.get(client, path) }
	var out Infra
	out.NetworkPolicy = get("/apis/networking.k8s.io/v1/namespaces/agent/networkpolicies/agent-egress-allowlist") != nil
	// Audit does not block, so only a list holding Deny arms the badge.
	if actions, ok := at(get("/apis/policies.kyverno.io/v1/validatingpolicies/restrict-image-registries"), "spec", "validationActions").([]any); ok {
		for _, a := range actions {
			if a == "Deny" {
				out.Kyverno = true
			}
		}
	}
	out.KubeArmor = get("/apis/security.kubearmor.com/v1/namespaces/agent/kubearmorpolicies/block-recipe-snoop") != nil
	// A DaemonSet with no ready pod detects nothing.
	out.Falco = count(at(get("/apis/apps/v1/namespaces/security/daemonsets/falco"), "status", "numberReady")) > 0
	gateway := gatewayNarrowed(get("/api/v1/namespaces/agent/configmaps/agentgateway-c7-config"))
	allowlist := allowlistNarrowed(get("/apis/kagent.dev/v1alpha2/namespaces/agent/agents/workshop-agent"))
	switch {
	case isTrue(gateway) || isTrue(allowlist):
		out.ToolAllowlist = ptr(true)
	case isFalse(gateway) && isFalse(allowlist):
		out.ToolAllowlist = ptr(false)
	}
	out.RBACScoped = rbacScoped(get("/apis/rbac.authorization.k8s.io/v1/namespaces/agent/roles/workshop-agent"))

	r.mu.Lock()
	r.at, r.cached = now, &out
	r.mu.Unlock()
	return out
}

// client trusts the service account CA when the pod has one, and the system roots otherwise.
func (r *Reader) client() *http.Client {
	c := &http.Client{Timeout: requestTimeout}
	if pem, err := os.ReadFile(r.caPath); err == nil {
		pool := x509.NewCertPool()
		if pool.AppendCertsFromPEM(pem) {
			c.Transport = &http.Transport{TLSClientConfig: &tls.Config{RootCAs: pool, MinVersion: tls.VersionTLS12}}
		}
	}
	return c
}

// get fetches one object. nil means could not determine: no token, a 404 (the control is not
// installed), a denied read, or any other failure. A caller renders nil as an unlit badge.
func (r *Reader) get(c *http.Client, path string) any {
	tok, err := os.ReadFile(r.tokenPath)
	if err != nil {
		return nil
	}
	req, err := http.NewRequest(http.MethodGet, r.apiURL+path, nil)
	if err != nil {
		return nil
	}
	req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(string(tok)))
	resp, err := c.Do(req)
	if err != nil {
		return nil
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil || resp.StatusCode < 200 || resp.StatusCode > 299 {
		return nil
	}
	var v any
	if json.Unmarshal(body, &v) != nil {
		return nil
	}
	return v
}

// gatewayNarrowed reads agentgateway's C7 rule by its marker line, the same line the toggle rewrites.
// A missing marker, a second marked line, or an unrecognized rule is unknown: it could still admit a
// rogue tool, so the badge claims nothing either way.
func gatewayNarrowed(cm any) *bool {
	cfg, ok := at(cm, "data", "config.yaml").(string)
	if !ok {
		return nil
	}
	var marked []string
	for _, ln := range splitLines(cfg) {
		if strings.HasSuffix(strings.TrimRight(ln, " \t\r\n\v\f"), c7Marker) {
			marked = append(marked, strings.TrimSpace(ln))
		}
	}
	if len(marked) != 1 {
		return nil
	}
	item := strings.TrimSpace(strings.TrimSuffix(marked[0], c7Marker))
	if !strings.HasPrefix(item, "- ") {
		return nil
	}
	rule, ok := yamlQuoted(strings.TrimSpace(item[2:]))
	switch {
	case !ok:
		return nil
	case rule == c7Narrowed:
		return ptr(true)
	case rule == c7Open:
		return ptr(false)
	}
	return nil
}

// splitLines splits on the line endings Python's str.splitlines recognizes in YAML text.
func splitLines(s string) []string {
	return strings.FieldsFunc(s, func(r rune) bool {
		switch r {
		case '\n', '\r', '\v', '\f', 0x1c, 0x1d, 0x1e, 0x85, 0x2028, 0x2029:
			return true
		}
		return false
	})
}

// yamlQuoted is the value of a single- or double-quoted YAML scalar, or the scalar itself unquoted.
func yamlQuoted(s string) (string, bool) {
	if len(s) >= 2 && s[0] == '\'' && s[len(s)-1] == '\'' {
		return strings.ReplaceAll(s[1:len(s)-1], "''", "'"), true
	}
	if len(s) >= 2 && s[0] == '"' && s[len(s)-1] == '"' {
		var v string
		if json.Unmarshal([]byte(s), &v) != nil {
			return "", false
		}
		return v, true
	}
	return s, true
}

// rogueTools are the two tools the evil-mcp server ships beside get_weather.
var rogueTools = []string{"read_internal_config", "apply_optimization"}

// allowlistNarrowed is true when the agent's toolNames no longer carry the rogue tools, false when they
// do, and nil when the Agent could not be read. kagent treats an empty toolNames as every tool on the
// server, so an emptied evil-mcp entry still ships them (verified live 2026-09-05).
func allowlistNarrowed(ag any) *bool {
	if ag == nil {
		return nil
	}
	present := map[string]bool{}
	tools, _ := at(ag, "spec", "declarative", "tools").([]any)
	for _, t := range tools {
		srv, _ := at(t, "mcpServer").(map[string]any)
		names, _ := srv["toolNames"].([]any)
		if srv["name"] == "evil-mcp" && len(names) == 0 {
			for _, r := range rogueTools {
				present[r] = true
			}
		}
		for _, n := range names {
			if s, ok := n.(string); ok {
				present[s] = true
			}
		}
	}
	for _, r := range rogueTools {
		if present[r] {
			return ptr(false)
		}
	}
	return ptr(true)
}

// rbacScoped is true once no rule grants list on secrets, or get on secrets without naming them.
func rbacScoped(role any) *bool {
	if role == nil {
		return nil
	}
	m, ok := role.(map[string]any)
	if !ok {
		return ptr(false)
	}
	rules := []any{}
	if raw, present := m["rules"]; present {
		if rules, ok = raw.([]any); !ok {
			return ptr(false) // not a list of rules, so the broad access cannot be ruled out
		}
	}
	for _, raw := range rules {
		r, ok := raw.(map[string]any)
		if !ok {
			return ptr(false)
		}
		if !contains(r["apiGroups"], "") || !contains(r["resources"], "secrets") {
			continue
		}
		names, _ := r["resourceNames"].([]any)
		if contains(r["verbs"], "list") || (contains(r["verbs"], "get") && len(names) == 0) {
			return ptr(false)
		}
	}
	return ptr(true)
}

func contains(list any, want string) bool {
	l, _ := list.([]any)
	for _, v := range l {
		if v == want {
			return true
		}
	}
	return false
}

// count reads a status count the way the reference's int() does; anything unreadable is 0.
func count(v any) int64 {
	switch x := v.(type) {
	case float64:
		return int64(x)
	case bool:
		if x {
			return 1
		}
	case string:
		var n int64
		if _, err := fmt.Sscan(strings.TrimSpace(x), &n); err == nil {
			return n
		}
	}
	return 0
}

// at walks nested objects by key, and is nil where any step is missing or not an object.
func at(v any, keys ...string) any {
	for _, k := range keys {
		m, ok := v.(map[string]any)
		if !ok {
			return nil
		}
		v = m[k]
	}
	return v
}

func ptr(b bool) *bool     { return &b }
func isTrue(b *bool) bool  { return b != nil && *b }
func isFalse(b *bool) bool { return b != nil && !*b }
