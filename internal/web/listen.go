package web

import (
	"context"
	"errors"
	"net/http"
	"os"
	"time"

	"rss-watch/internal/worker"
)

// Listen runs the Web service and its scheduler until the caller cancels ctx.
func Listen(ctx context.Context, config worker.Config, addr string) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	app, err := New(ctx, config, os.Getenv("RSS_WATCH_SECURE_COOKIES") == "1", os.Getenv("RSS_WATCH_TRUSTED_PROXIES"))
	if err != nil {
		return err
	}
	defer app.Close()
	server := &http.Server{
		Addr: addr, Handler: app, ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout: 2 * time.Minute, IdleTimeout: 60 * time.Second,
	}
	backupsDone := make(chan struct{})
	go func() {
		defer close(backupsDone)
		app.RunBackups(ctx)
	}()
	shutdownDone := make(chan struct{})
	go func() {
		<-ctx.Done()
		defer close(shutdownDone)
		timeout, stop := context.WithTimeout(context.Background(), 3*time.Minute)
		defer stop()
		if err := server.Shutdown(timeout); err != nil {
			_ = server.Close()
		}
	}()
	err = server.ListenAndServe()
	cancel()
	<-shutdownDone
	<-backupsDone
	if errors.Is(err, http.ErrServerClosed) {
		return nil
	}
	return err
}
