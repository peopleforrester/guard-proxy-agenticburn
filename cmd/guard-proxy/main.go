// ABOUTME: guard-proxy entry point: loads and validates config, opens the spend store, serves HTTP, and
// ABOUTME: drains in-flight requests on SIGTERM. A config or store error exits non-zero before the port is bound.
// SPDX-License-Identifier: Apache-2.0
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
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
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, "guard-proxy:", err)
		os.Exit(1)
	}
}

func run() error {
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
