package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"rss-watch/internal/containerlog"
	"rss-watch/internal/web"
	"rss-watch/internal/worker"
)

func main() {
	syscall.Umask(0o077)
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, os.Interrupt)
	defer stop()
	if err := run(ctx, os.Args[1:]); err != nil {
		log.Fatal(err)
	}
}

func run(ctx context.Context, args []string) error {
	if len(args) == 0 {
		return errors.New("用法：rss-watch-container {web|worker|check-web|check-worker|bootstrap-token} [--config PATH]")
	}
	role := args[0]
	config := os.Getenv("RSS_WATCH_CONFIG")
	if config == "" {
		config = "/app/container.toml"
	}
	flags := flag.NewFlagSet(role, flag.ContinueOnError)
	configPath := flags.String("config", config, "rss-watch TOML 配置文件")
	if err := flags.Parse(args[1:]); err != nil {
		return err
	}
	if flags.NArg() != 0 {
		return errors.New("存在未识别的参数")
	}
	configured, err := worker.LoadConfig(*configPath)
	if err != nil {
		return err
	}
	switch role {
	case "check-web":
		return checkWeb(ctx)
	case "check-worker":
		ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
		defer cancel()
		return worker.Check(ctx, configured)
	case "bootstrap-token":
		file, err := os.Open(filepath.Join(configured.DataDir, "bootstrap-token"))
		if err != nil {
			return err
		}
		defer file.Close()
		_, err = io.Copy(os.Stdout, file)
		if err == nil {
			fmt.Fprintln(os.Stdout)
		}
		return err
	case "web", "worker":
		output, err := containerlog.Open(configured.DataDir, role)
		if err != nil {
			return err
		}
		log.SetOutput(output)
		defer func() {
			log.SetOutput(os.Stderr)
			_ = output.Close()
		}()
		if role == "web" {
			return web.Listen(ctx, configured, "0.0.0.0:8080")
		}
		return worker.Run(ctx, configured)
	}
	return errors.New("未知的容器角色")
}

func checkWeb(ctx context.Context) error {
	ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://127.0.0.1:8080/healthz", nil)
	if err != nil {
		return err
	}
	client := &http.Client{Transport: &http.Transport{}}
	response, err := client.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("Web 健康检查 HTTP %d", response.StatusCode)
	}
	return nil
}
