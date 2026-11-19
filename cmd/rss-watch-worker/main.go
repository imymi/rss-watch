package main

import (
	"context"
	"flag"
	"log"
	"os"
	"os/signal"
	"syscall"

	"rss-watch/internal/worker"
)

func main() {
	configPath := flag.String("config", "", "rss-watch TOML 配置文件")
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
	if err := worker.Run(ctx, config); err != nil {
		log.Fatal(err)
	}
}
