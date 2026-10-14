// ABOUTME: End-to-end tests of the built binary: a bad config refuses to start, and SIGTERM lets an
// ABOUTME: in-flight request finish before the process exits cleanly.
// SPDX-License-Identifier: Apache-2.0
package main

import (
	"bytes"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
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

func TestConfigErrorsExitBeforeServing(t *testing.T) {
	cfg := filepath.Join(t.TempDir(), "bad.yaml")
	if err := os.WriteFile(cfg, []byte("agent: {url: http://a}\nguards: {ouput: true}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	for name, args := range map[string][]string{
		"no agent url": {},
		"unknown key":  {"--config", cfg},
		"stray arg":    {"extra"},
	} {
		t.Run(name, func(t *testing.T) {
			cmd := exec.Command(binary, append([]string{"--listen", freeAddr(t)}, args...)...)
			cmd.Env = []string{"PATH=" + os.Getenv("PATH")}
			if name != "no agent url" {
				cmd.Env = append(cmd.Env, "AGENT_URL=http://127.0.0.1:1")
			}
			var stderr bytes.Buffer
			cmd.Stderr = &stderr
			err := cmd.Run()
			if ee, ok := err.(*exec.ExitError); !ok || ee.ExitCode() != 1 {
				t.Fatalf("exit = %v, want status 1", err)
			}
			if !strings.HasPrefix(stderr.String(), "guard-proxy: ") {
				t.Errorf("stderr = %q", stderr.String())
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

	addr := freeAddr(t)
	cmd := exec.Command(binary, "--listen", addr)
	cmd.Env = []string{"AGENT_URL=" + agent.URL}
	var stdout bytes.Buffer
	cmd.Stdout = &stdout
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(5 * time.Second)
	for {
		if r, err := http.Get("http://" + addr + "/healthz"); err == nil {
			r.Body.Close()
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("never became healthy; output: %s", stdout.String())
		}
		time.Sleep(20 * time.Millisecond)
	}

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
