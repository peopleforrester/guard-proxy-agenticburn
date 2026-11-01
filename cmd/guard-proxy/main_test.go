// ABOUTME: End-to-end tests of the built binary: a bad config or store refuses to start, SIGTERM drains
// ABOUTME: in-flight requests, and spend metered through /chat survives the process being killed.
// SPDX-License-Identifier: Apache-2.0
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"
)

var binary string

func TestMain(m *testing.M) {
	dir, err := os.MkdirTemp("", "guard-proxy-e2e")
	if err != nil {
		panic(err)
	}
	binary = filepath.Join(dir, "guard-proxy")
	if out, err := exec.Command("go", "build", "-o", binary, ".").CombinedOutput(); err != nil {
		panic(fmt.Sprintf("build: %v\n%s", err, out))
	}
	code := m.Run()
	_ = os.RemoveAll(dir)
	os.Exit(code)
}

func freeAddr(t *testing.T) string {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	return ln.Addr().String()
}

// proxy is a running binary.
type proxy struct {
	addr   string
	cmd    *exec.Cmd
	output *syncBuffer
}

// syncBuffer is the process's output, written by exec's copying goroutine while a test may read it.
type syncBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *syncBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *syncBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

// start runs the binary with env on a free port and waits until it answers /healthz.
func start(t *testing.T, env ...string) *proxy {
	t.Helper()
	p := &proxy{addr: freeAddr(t), output: &syncBuffer{}}
	p.cmd = exec.Command(binary, "--listen", p.addr)
	p.cmd.Env = env
	p.cmd.Stdout, p.cmd.Stderr = p.output, p.output
	if err := p.cmd.Start(); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(10 * time.Second)
	for {
		if r, err := http.Get("http://" + p.addr + "/healthz"); err == nil {
			r.Body.Close()
			return p
		}
		if time.Now().After(deadline) {
			_ = p.cmd.Process.Kill()
			_ = p.cmd.Wait()
			t.Fatalf("never became healthy; output: %s", p.output.String())
		}
		time.Sleep(20 * time.Millisecond)
	}
}

// kill ends the process with SIGKILL, as a crash or an OOM kill would: nothing gets to flush or close.
func (p *proxy) kill(t *testing.T) {
	t.Helper()
	if err := p.cmd.Process.Kill(); err != nil {
		t.Fatal(err)
	}
	_ = p.cmd.Wait()
}

func (p *proxy) getJSON(t *testing.T, path string) map[string]any {
	t.Helper()
	r, err := http.Get("http://" + p.addr + path)
	if err != nil {
		t.Fatal(err)
	}
	defer r.Body.Close()
	var v map[string]any
	if err := json.NewDecoder(r.Body).Decode(&v); err != nil {
		t.Fatal(err)
	}
	return v
}

// chat sends one /chat turn and returns its reply body.
func (p *proxy) chat(t *testing.T, session string) map[string]any {
	t.Helper()
	body, _ := json.Marshal(map[string]string{"prompt": "one burrito", "session": session})
	r, err := http.Post("http://"+p.addr+"/chat", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	defer r.Body.Close()
	var v map[string]any
	if err := json.NewDecoder(r.Body).Decode(&v); err != nil {
		t.Fatal(err)
	}
	if r.StatusCode != http.StatusOK {
		t.Fatalf("chat = %d %v; output: %s", r.StatusCode, v, p.output.String())
	}
	return v
}

// stubAgent answers every A2A call with a finished task that used 10 tokens in and 10 out.
func stubAgent(t *testing.T) *httptest.Server {
	t.Helper()
	a := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"jsonrpc": "2.0", "id": "chat", "result": {"kind": "task", "id": "t1",
			"status": {"state": "completed", "message": {"role": "agent", "parts": [{"kind": "text", "text": "Here is your burrito."}]}},
			"metadata": {"kagent_usage_metadata": {"promptTokenCount": 10, "candidatesTokenCount": 10}}}}`)
	}))
	t.Cleanup(a.Close)
	return a
}

// TestSpendSurvivesAKillMidWindow is PRD #412 M3's restart criterion: a process killed with spend on the
// tally starts again on the same store with the same spent-so-far, so a restart cannot reset a budget.
func TestSpendSurvivesAKillMidWindow(t *testing.T) {
	agent := stubAgent(t)
	// Each turn costs $0.02 (20 tokens at $1 per 1,000); the budget is $0.05 per session.
	env := []string{"AGENT_URL=" + agent.URL, "LLM_GUARD_URL=http://127.0.0.1:1",
		"SPEND_STORE_PATH=" + filepath.Join(t.TempDir(), "spend.db"),
		"COST_PER_1K_IN=1", "COST_PER_1K_OUT=1", "BUDGET_GUARD=on", "BUDGET_CAP_USD=0.05"}
	before := start(t, env...)
	for i := 0; i < 3; i++ { // $0.06: session a is over its budget
		if r := before.chat(t, "a"); r["guarded"] != false {
			t.Fatalf("turn %d on a: %v", i, r)
		}
	}
	if r := before.chat(t, "b"); r["guarded"] != false { // $0.02: session b is under it
		t.Fatalf("turn on b: %v", r)
	}
	if r := before.chat(t, "a"); r["guarded"] != true {
		t.Fatalf("a over budget before the restart was answered: %v", r)
	}
	cost := before.getJSON(t, "/cost")
	if cost["requests"] != 4.0 || cost["total_tokens"] != 80.0 {
		t.Fatalf("/cost before the restart = %v", cost)
	}

	// A second process on the same store while the first holds it would overwrite its tally.
	second := exec.Command(binary, "--listen", freeAddr(t))
	second.Env = env
	if out, err := second.CombinedOutput(); err == nil || !strings.Contains(string(out), "held by another process") {
		t.Errorf("a second process on a held store: %v, %s", err, out)
	}

	before.kill(t)
	after := start(t, env...)
	defer func() {
		after.kill(t)
	}()
	if got := after.getJSON(t, "/cost"); !reflect.DeepEqual(got, cost) {
		t.Errorf("/cost after the restart = %v, want %v", got, cost)
	}
	// The session over its budget before the restart is still refused after it.
	if r := after.chat(t, "a"); r["guarded"] != true || !strings.Contains(r["reply"].(string), "$0.05 spend budget") {
		t.Errorf("a after the restart: %v", r)
	}
	// Session b kept its $0.02: one more turn fits under $0.05, a second does not.
	if r := after.chat(t, "b"); r["guarded"] != false {
		t.Errorf("b's second turn: %v", r)
	}
	if r := after.chat(t, "b"); r["guarded"] != false {
		t.Errorf("b's third turn: %v", r)
	}
	if r := after.chat(t, "b"); r["guarded"] != true {
		t.Errorf("b at $0.06 was answered: its spend before the restart was lost: %v", r)
	}
	// Reset in the storefront is a fresh session id, and a fresh session has no stored spend.
	if r := after.chat(t, "a-after-reset"); r["guarded"] != false {
		t.Errorf("a fresh session after Reset was refused: %v", r)
	}
	if got := after.getJSON(t, "/cost"); got["requests"] != 7.0 {
		t.Errorf("/cost requests = %v, want the tally carried on from 4", got["requests"])
	}
}

// TestRuntimeCostCapSurvivesAKill: a cap lowered with /toggle for the KubeCon finale still holds when
// the pod is killed and comes back, so a restart cannot reopen a closed kitchen.
func TestRuntimeCostCapSurvivesAKill(t *testing.T) {
	agent := stubAgent(t)
	env := []string{"AGENT_URL=" + agent.URL, "LLM_GUARD_URL=http://127.0.0.1:1",
		"SPEND_STORE_PATH=" + filepath.Join(t.TempDir(), "spend.db"),
		"COST_PER_1K_IN=1", "COST_PER_1K_OUT=1", "COST_CAP_USD=25"}
	before := start(t, env...)
	if got := before.getJSON(t, "/toggle?cost_cap=0.03"); got["cost_cap_usd"] != 0.03 {
		t.Fatalf("toggle = %v", got)
	}
	before.chat(t, "a")
	before.chat(t, "b") // $0.04, over the lowered cap
	before.kill(t)

	after := start(t, env...)
	defer after.kill(t)
	if r := after.chat(t, "c"); r["guarded"] != true || !strings.Contains(r["reply"].(string), "$0.03 spend cap") {
		t.Errorf("chat after the restart: %v", r)
	}
	if got := after.getJSON(t, "/cost"); got["cap_usd"] != 0.03 {
		t.Errorf("/cost cap_usd = %v", got["cap_usd"])
	}
}

func TestConfigErrorsExitBeforeServing(t *testing.T) {
	cfg := filepath.Join(t.TempDir(), "bad.yaml")
	if err := os.WriteFile(cfg, []byte("agent: {url: http://a}\nguards: {ouput: true}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	readOnly := filepath.Join(t.TempDir(), "ro")
	if err := os.Mkdir(readOnly, 0o500); err != nil {
		t.Fatal(err)
	}
	store := "SPEND_STORE_PATH=" + filepath.Join(t.TempDir(), "spend.db")
	agent := "AGENT_URL=http://127.0.0.1:1"
	for name, tc := range map[string]struct {
		args   []string
		env    []string
		stderr string
	}{
		"no agent url":     {env: []string{store}, stderr: "agent.url"},
		"unknown key":      {args: []string{"--config", cfg}, env: []string{agent, store}, stderr: "ouput"},
		"stray arg":        {args: []string{"extra"}, env: []string{agent, store}, stderr: "unexpected arguments"},
		"no store path":    {env: []string{agent}, stderr: "spend.store_path (or SPEND_STORE_PATH) is required"},
		"unwritable store": {env: []string{agent, "SPEND_STORE_PATH=" + filepath.Join(readOnly, "spend.db")}, stderr: "permission denied"},
	} {
		t.Run(name, func(t *testing.T) {
			addr := freeAddr(t)
			cmd := exec.Command(binary, append([]string{"--listen", addr}, tc.args...)...)
			cmd.Env = append([]string{"PATH=" + os.Getenv("PATH"), "LLM_GUARD_URL=http://127.0.0.1:1"}, tc.env...)
			var stderr bytes.Buffer
			cmd.Stderr = &stderr
			err := cmd.Run()
			if ee, ok := err.(*exec.ExitError); !ok || ee.ExitCode() != 1 {
				t.Fatalf("exit = %v, want status 1", err)
			}
			if !strings.HasPrefix(stderr.String(), "guard-proxy: ") || !strings.Contains(stderr.String(), tc.stderr) {
				t.Errorf("stderr = %q, want it to name %q", stderr.String(), tc.stderr)
			}
		})
	}
}

func TestSIGTERMDrainsInFlightRequests(t *testing.T) {
	release := make(chan struct{})
	agent := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		<-release
		_, _ = io.WriteString(w, `{"ok":true}`)
	}))
	defer agent.Close()

	p := start(t, "AGENT_URL="+agent.URL, "LLM_GUARD_URL=http://127.0.0.1:1",
		"SPEND_STORE_PATH="+filepath.Join(t.TempDir(), "spend.db"))
	addr, cmd := p.addr, p.cmd

	type result struct {
		status int
		body   string
		err    error
	}
	done := make(chan result, 1)
	go func() {
		r, err := http.Post("http://"+addr+"/", "application/json", strings.NewReader(`{}`))
		if err != nil {
			done <- result{err: err}
			return
		}
		b, _ := io.ReadAll(r.Body)
		r.Body.Close()
		done <- result{status: r.StatusCode, body: string(b)}
	}()
	time.Sleep(200 * time.Millisecond) // let the request reach the agent
	if err := cmd.Process.Signal(syscall.SIGTERM); err != nil {
		t.Fatal(err)
	}
	time.Sleep(200 * time.Millisecond)
	close(release)

	res := <-done
	if res.err != nil || res.status != http.StatusOK || res.body != `{"ok":true}` {
		t.Errorf("in-flight request: %+v", res)
	}
	if err := cmd.Wait(); err != nil {
		t.Errorf("exit after SIGTERM: %v", err)
	}
}
