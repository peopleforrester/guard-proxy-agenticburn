// ABOUTME: Tests for the /controls cluster reads against a fake API server: each badge's measurement,
// ABOUTME: "could not read" without credentials, the service account token, and the short cache.
// SPDX-License-Identifier: Apache-2.0
package controls

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

type fakeAPI struct {
	mu      sync.Mutex
	objects map[string]string
	calls   []*http.Request
}

func newAPI(t *testing.T, objects map[string]string) (*fakeAPI, string) {
	t.Helper()
	f := &fakeAPI{objects: objects}
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		f.calls = append(f.calls, r)
		body, ok := f.objects[r.URL.Path]
		f.mu.Unlock()
		if !ok {
			w.WriteHeader(http.StatusNotFound)
			_, _ = w.Write([]byte(`{"kind":"Status","reason":"NotFound"}`))
			return
		}
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(s.Close)
	return f, s.URL
}

func reader(t *testing.T, url string) *Reader {
	t.Helper()
	tok := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tok, []byte("sa-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	return NewReader(url, tok, filepath.Join(t.TempDir(), "absent-ca.crt"))
}

func TestWithoutATokenEveryReadIsUnknown(t *testing.T) {
	_, url := newAPI(t, nil)
	got := NewReader(url, filepath.Join(t.TempDir(), "none"), "").Read()
	want := Infra{NetworkPolicy: false, Kyverno: false, KubeArmor: false, Falco: false}
	if got != want {
		t.Errorf("Read = %+v, want %+v", got, want)
	}
	if b, _ := json.Marshal(got); string(b) != `{"networkpolicy":false,"kyverno":false,"kubearmor":false,"falco":false,"tool_allowlist":null,"rbac_scoped":null}` {
		t.Errorf("JSON = %s", b)
	}
}

const (
	c7Path    = "/api/v1/namespaces/agent/configmaps/agentgateway-c7-config"
	agentPath = "/apis/kagent.dev/v1alpha2/namespaces/agent/agents/workshop-agent"
	rolePath  = "/apis/rbac.authorization.k8s.io/v1/namespaces/agent/roles/workshop-agent"
)

func c7(rule string) string {
	b, _ := json.Marshal(map[string]any{"data": map[string]string{"config.yaml": "rules:\n          " + rule + "\n  backends: []\n"}})
	return string(b)
}

func agent(names string) string {
	return `{"spec": {"declarative": {"tools": [{"mcpServer": {"name": "workshop-mcp", "toolNames": []}},
		{"mcpServer": {"name": "evil-mcp", "toolNames": ` + names + `}}]}}}`
}

func TestToolAllowlist(t *testing.T) {
	all := `["get_weather", "read_internal_config", "apply_optimization"]`
	narrowed := `- 'mcp.tool.name == "get_weather"' # c7-rule`
	open := "- 'true' # c7-rule"
	yes, no := true, false
	for _, tc := range []struct {
		name    string
		objects map[string]string
		want    *bool
	}{
		{"gateway narrowed", map[string]string{c7Path: c7(narrowed), agentPath: agent(all)}, &yes},
		{"both open", map[string]string{c7Path: c7(open), agentPath: agent(all)}, &no},
		{"allow-list narrowed", map[string]string{c7Path: c7(open), agentPath: agent(`["get_weather"]`)}, &yes},
		{"emptied allow-list exposes every tool", map[string]string{c7Path: c7(open), agentPath: agent(`[]`)}, &no},
		{"double-quoted rule", map[string]string{c7Path: c7(`- "mcp.tool.name == \"get_weather\"" # c7-rule`), agentPath: agent(all)}, &yes},
		{"unrecognized rule", map[string]string{c7Path: c7(`- 'mcp.tool.name != "x"' # c7-rule`), agentPath: agent(all)}, nil},
		{"no marker", map[string]string{c7Path: c7("- 'true'"), agentPath: agent(all)}, nil},
		{"no configmap", map[string]string{agentPath: agent(all)}, nil},
		{"narrowed without the agent", map[string]string{c7Path: c7(narrowed)}, &yes},
		{"an agent without tools blocks nothing it lists", map[string]string{c7Path: c7(open), agentPath: `{}`}, &yes},
	} {
		_, url := newAPI(t, tc.objects)
		got := reader(t, url).Read().ToolAllowlist
		if (got == nil) != (tc.want == nil) || (got != nil && *got != *tc.want) {
			t.Errorf("%s: ToolAllowlist = %v, want %v", tc.name, deref(got), deref(tc.want))
		}
	}
}

func deref(b *bool) any {
	if b == nil {
		return nil
	}
	return *b
}

func TestTwoMarkedLinesAreUnknown(t *testing.T) {
	b, _ := json.Marshal(map[string]any{"data": map[string]string{"config.yaml": "- 'true' # c7-rule\n- 'true' # c7-rule\n"}})
	_, url := newAPI(t, map[string]string{c7Path: string(b), agentPath: agent(`["get_weather","read_internal_config","apply_optimization"]`)})
	if got := reader(t, url).Read().ToolAllowlist; got != nil {
		t.Errorf("ToolAllowlist = %v", *got)
	}
}

func TestKyvernoFalcoAndTheExistenceBadges(t *testing.T) {
	_, url := newAPI(t, map[string]string{
		"/apis/networking.k8s.io/v1/namespaces/agent/networkpolicies/agent-egress-allowlist":    `{}`,
		"/apis/policies.kyverno.io/v1/validatingpolicies/restrict-image-registries":             `{"spec": {"validationActions": ["Audit", "Deny"]}}`,
		"/apis/security.kubearmor.com/v1/namespaces/agent/kubearmorpolicies/block-recipe-snoop": `{}`,
		"/apis/apps/v1/namespaces/security/daemonsets/falco":                                    `{"status": {"numberReady": 2}}`,
	})
	got := reader(t, url).Read()
	if !got.NetworkPolicy || !got.Kyverno || !got.KubeArmor || !got.Falco {
		t.Errorf("Read = %+v", got)
	}
	for body, want := range map[string]bool{
		`{"spec": {"validationActions": "Deny"}}`:    false,
		`{"spec": {"validationActions": ["Audit"]}}`: false,
		`{"spec": {}}`: false,
	} {
		_, url := newAPI(t, map[string]string{"/apis/policies.kyverno.io/v1/validatingpolicies/restrict-image-registries": body})
		if got := reader(t, url).Read().Kyverno; got != want {
			t.Errorf("%s: Kyverno = %v", body, got)
		}
	}
	for body, want := range map[string]bool{`{"status": {"numberReady": 0}}`: false, `{"status": {}}`: false, `{}`: false, `{"status": {"numberReady": "3"}}`: true} {
		_, url := newAPI(t, map[string]string{"/apis/apps/v1/namespaces/security/daemonsets/falco": body})
		if got := reader(t, url).Read().Falco; got != want {
			t.Errorf("%s: Falco = %v", body, got)
		}
	}
}

func TestRBACScoped(t *testing.T) {
	yes, no := true, false
	for body, want := range map[string]*bool{
		`{"rules": [{"apiGroups": [""], "resources": ["secrets"], "verbs": ["get"], "resourceNames": ["recipe"]}]}`: &yes,
		`{"rules": [{"apiGroups": [""], "resources": ["secrets"], "verbs": ["get", "list"]}]}`:                      &no,
		`{"rules": [{"apiGroups": [""], "resources": ["secrets"], "verbs": ["get"]}]}`:                              &no,
		`{"rules": [{"apiGroups": ["apps"], "resources": ["secrets"], "verbs": ["list"]}]}`:                         &yes,
		`{}`:                    &yes,
		`{"rules": "nonsense"}`: &no,
	} {
		_, url := newAPI(t, map[string]string{rolePath: body})
		got := reader(t, url).Read().RBACScoped
		if got == nil || *got != *want {
			t.Errorf("%s: RBACScoped = %v, want %v", body, deref(got), *want)
		}
	}
}

func TestReadsAreGetsWithTheTokenAndCached(t *testing.T) {
	f, url := newAPI(t, nil)
	r := reader(t, url)
	r.Read()
	r.Read()
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(f.calls) != 7 {
		t.Errorf("%d calls; want 7, once each, the second Read cached", len(f.calls))
	}
	for _, c := range f.calls {
		if c.Method != http.MethodGet || c.Header.Get("Authorization") != "Bearer sa-token" {
			t.Errorf("%s %s with %q", c.Method, c.URL.Path, c.Header.Get("Authorization"))
		}
	}
}

func TestCacheExpires(t *testing.T) {
	f, url := newAPI(t, nil)
	r := reader(t, url)
	now := time.Unix(0, 0)
	r.now = func() time.Time { return now }
	r.Read()
	now = now.Add(6 * time.Second)
	r.Read()
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(f.calls) != 14 {
		t.Errorf("%d calls; want 14", len(f.calls))
	}
}
