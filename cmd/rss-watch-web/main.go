package main

import (
	"context"
	"errors"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"rss-watch/internal/web"
	"rss-watch/internal/worker"
)

func main() {
	configPath := flag.String("config", "", "rss-watch TOML 配置文件")
	listen := flag.String("listen", "127.0.0.1:8080", "Web 监听地址")
	flag.Parse()
	if *configPath == "" || flag.NArg() != 0 {
		flag.Usage()
		os.Exit(2)
	}
	syscall.Umask(0o077)
	config, err := worker.LoadConfig(*configPath)
	if err != nil {
		log.Fatal(err)
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	app, err := web.New(ctx, config, os.Getenv("RSS_WATCH_SECURE_COOKIES") == "1", os.Getenv("RSS_WATCH_TRUSTED_PROXIES"))
	if err != nil {
		log.Fatal(err)
	}
	defer app.Close()
	server := &http.Server{
		Addr: *listen, Handler: app, ReadHeaderTimeout: 5 * time.Second,
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
		timeout, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
		defer cancel()
		if err := server.Shutdown(timeout); err != nil {
			_ = server.Close()
		}
	}()
	err = server.ListenAndServe()
	stop()
	<-shutdownDone
	<-backupsDone
	if err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatal(err)
	}
}
