// ABOUTME: guard-proxy entry point: serves HTTP, draining on SIGTERM; a config or store error exits non-zero
// ABOUTME: before the port is bound. `reset-spend --store PATH` clears a stopped proxy's spend store.
// SPDX-License-Identifier: Apache-2.0
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"io/fs"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/peopleforrester/guard-proxy-agenticburn/internal/config"
	"github.com/peopleforrester/guard-proxy-agenticburn/internal/logging"
	"github.com/peopleforrester/guard-proxy-agenticburn/internal/server"
	"github.com/peopleforrester/guard-proxy-agenticburn/internal/spend"
)

// shutdownGrace bounds how long in-flight requests may finish after SIGTERM. It matches the
// Kubernetes default terminationGracePeriodSeconds.
const shutdownGrace = 30 * time.Second

func main() {
	run := serve
	if len(os.Args) > 1 && os.Args[1] == "reset-spend" {
		run = func() error { return resetSpend(os.Args[2:]) }
	}
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, "guard-proxy:", err)
		os.Exit(1)
	}
}

func serve() error {
	configPath := flag.String("config", "", "path to the YAML config file")
	listen := flag.String("listen", "", "address to listen on, overriding the config's listen")
	flag.Parse()
	if flag.NArg() > 0 {
		return fmt.Errorf("unexpected arguments: %v", flag.Args())
	}

	cfg, err := config.Load(*configPath, os.LookupEnv)
	if err != nil {
		return err
	}
	if *listen != "" {
		cfg.Listen = *listen
	}

	log := logging.New(os.Stdout)
	// The store closes last, after every in-flight request has recorded its spend.
	store, err := spend.OpenStore(cfg.Spend.StorePath)
	if err != nil {
		return err
	}
	defer store.Close()
	handler, err := server.New(cfg, log, store)
	if err != nil {
		return err
	}
	ln, err := net.Listen("tcp", cfg.Listen)
	if err != nil {
		return err
	}
	srv := &http.Server{
		Handler:           handler,
		ReadHeaderTimeout: 10 * time.Second,
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	errc := make(chan error, 1)
	go func() { errc <- srv.Serve(ln) }()
	log.Info("listening on " + ln.Addr().String() + ", agent " + cfg.Agent.URL)

	select {
	case err := <-errc:
		return err
	case <-ctx.Done():
	}
	shutdownCtx, cancel := context.WithTimeout(context.Background(), shutdownGrace)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil && !errors.Is(err, http.ErrServerClosed) {
		return err
	}
	return nil
}

// resetSpend clears the spend store at --store so the next delivery starts from $0, and prints what it
// cleared. It runs from the same signed image as the proxy, with the Deployment scaled to 0: a store the
// proxy still holds is refused, and a path with no store is refused rather than created.
func resetSpend(args []string) error {
	flags := flag.NewFlagSet("reset-spend", flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	path := flags.String("store", "", "path to the spend store (the proxy's SPEND_STORE_PATH)")
	if err := flags.Parse(args); err != nil {
		return fmt.Errorf("reset-spend: %w", err)
	}
	if flags.NArg() > 0 {
		return fmt.Errorf("reset-spend: unexpected arguments: %v", flags.Args())
	}
	if *path == "" {
		return errors.New("reset-spend: --store is required")
	}
	if _, err := os.Stat(*path); errors.Is(err, fs.ErrNotExist) {
		return fmt.Errorf("reset-spend: no spend store at %s", *path)
	}
	store, err := spend.OpenStore(*path)
	if err != nil {
		return err
	}
	defer store.Close()
	c, err := store.Reset()
	if err != nil {
		return err
	}
	costCap := "no runtime cost cap"
	if c.CostCapSet {
		costCap = fmt.Sprintf("the runtime cost cap of $%.4f", c.CostCap)
	}
	fmt.Printf("reset-spend: cleared $%.4f over %s (%d tokens), %s, and %s from %s\n", c.Totals.USD,
		plural(c.Totals.Requests, "request"), c.Totals.TotalTokens, plural(int64(c.Sessions), "session"), costCap, *path)
	return nil
}

func plural(n int64, noun string) string {
	if n == 1 {
		return "1 " + noun
	}
	return fmt.Sprintf("%d %ss", n, noun)
}
