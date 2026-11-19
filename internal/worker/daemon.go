// Package worker runs the RSS watcher and Feishu queue against schema 2.
package worker

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"time"

	"github.com/pelletier/go-toml/v2"
	"golang.org/x/sys/unix"
)

// Config uses the existing Python configuration paths.
type Config struct {
	DataDir        string `toml:"data_dir"`
	CredentialsDir string `toml:"credentials_dir"`
}

// LoadConfig reads the two absolute paths from an rss-watch TOML file.
func LoadConfig(path string) (Config, error) {
	file, err := os.Open(path)
	if err != nil {
		return Config{}, err
	}
	defer file.Close()
	var config Config
	if err := toml.NewDecoder(file).DisallowUnknownFields().Decode(&config); err != nil {
		return Config{}, errors.New("Worker 配置文件格式无效")
	}
	return config, config.validate()
}

func (c Config) validate() error {
	if !filepath.IsAbs(c.DataDir) || !filepath.IsAbs(c.CredentialsDir) {
		return errors.New("data_dir 和 credentials_dir 必须是绝对路径")
	}
	return nil
}

// Run holds the same daemon and maintenance locks as the Python worker.
func Run(ctx context.Context, config Config) error {
	return run(ctx, config, newHTTPClients)
}

func run(ctx context.Context, config Config, makeClients func(networkSettings) (httpClients, error)) error {
	if err := config.validate(); err != nil {
		return err
	}
	if ctx.Err() != nil {
		return nil
	}
	daemonLock, err := lock(ctx, filepath.Join(config.DataDir, "daemon.lock"), unix.LOCK_EX, false)
	if err != nil {
		return fmt.Errorf("获取后台进程锁：%w", err)
	}
	defer daemonLock.Close()
	for ctx.Err() == nil {
		maintenance, err := config.maintenance()
		if err != nil {
			return err
		}
		if maintenance {
			wait(ctx, time.Second)
			continue
		}
		err = runData(ctx, config, makeClients)
		if err != nil && !(ctx.Err() != nil && errors.Is(err, ctx.Err())) {
			return err
		}
	}
	return nil
}

func runData(ctx context.Context, config Config, makeClients func(networkSettings) (httpClients, error)) error {
	lockCtx, cancel := context.WithTimeout(ctx, 45*time.Second)
	dataLock, err := lock(lockCtx, filepath.Join(config.DataDir, "data.lock"), unix.LOCK_SH, true)
	cancel()
	if err != nil {
		return err
	}
	defer dataLock.Close()
	if maintenance, err := config.maintenance(); err != nil || maintenance {
		return err
	}
	db, err := openDatabase(ctx, filepath.Join(config.DataDir, "rss-watch.sqlite3"))
	if err != nil {
		return err
	}
	defer db.Close()
	initial, err := readSettings(ctx, db)
	if err != nil {
		return err
	}
	clients, err := makeClients(initial.networkSettings)
	if err != nil {
		return err
	}
	defer clients.close()
	w := worker{config: config, db: db, clients: clients}
	return w.runSession(ctx, initial)
}

func (c Config) maintenance() (bool, error) {
	_, err := os.Stat(filepath.Join(c.DataDir, "maintenance"))
	if errors.Is(err, os.ErrNotExist) {
		return false, nil
	}
	return err == nil, err
}

func lock(ctx context.Context, path string, operation int, retry bool) (*os.File, error) {
	file, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, err
	}
	for ctx.Err() == nil {
		err = unix.Flock(int(file.Fd()), operation|unix.LOCK_NB)
		if err == nil {
			return file, nil
		}
		if !retry || !errors.Is(err, unix.EWOULDBLOCK) {
			file.Close()
			return nil, err
		}
		wait(ctx, 50*time.Millisecond)
	}
	file.Close()
	return nil, ctx.Err()
}

func wait(ctx context.Context, duration time.Duration) {
	timer := time.NewTimer(duration)
	defer timer.Stop()
	select {
	case <-ctx.Done():
	case <-timer.C:
	}
}

type worker struct {
	config   Config
	db       *sql.DB
	clients  httpClients
	settings atomic.Pointer[settings]
}

type fetchCompletion struct {
	id  int64
	err error
}

func (w *worker) runSession(parent context.Context, initial settings) (err error) {
	ctx, cancel := context.WithCancel(parent)
	var tasks sync.WaitGroup
	defer func() {
		cancel()
		tasks.Wait()
		if parent.Err() != nil {
			err = nil
		}
		stop, cancelStop := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
		defer cancelStop()
		if stopErr := w.heartbeat(stop, false, ""); stopErr != nil {
			err = errors.Join(err, stopErr)
		}
	}()
	if err := w.recover(ctx, initial); err != nil {
		return err
	}

	w.settings.Store(&initial)
	sender := make(chan error, 1)
	senderStarted := false

	const maxConcurrentFetches = 20
	fetched := make(chan fetchCompletion, maxConcurrentFetches)
	inFlight := make(map[int64]bool)
	var lastCleanup time.Time
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		maintenance, err := w.config.maintenance()
		if err != nil || maintenance {
			return err
		}
		s, err := readSettings(ctx, w.db)
		if err != nil {
			return err
		}
		if s.networkSettings != initial.networkSettings {
			return nil // restart the session with a fresh network transport
		}
		w.settings.Store(&s)
		if err := w.heartbeat(ctx, true, s.revision); err != nil {
			return err
		}
		if !senderStarted {
			senderStarted = true
			tasks.Go(func() { sender <- w.deliveryLoop(ctx) })
		}
		now := time.Now()
		if now.Sub(lastCleanup) >= time.Duration(s.CleanupHours)*time.Hour {
			if err := w.cleanup(ctx, now, s.RetentionDays); err != nil {
				return err
			}
			lastCleanup = now
		}
		if !s.FetchPaused && len(inFlight) < s.FetchConcurrency {
			sources, err := w.dueSources(ctx, now)
			if err != nil {
				return err
			}
			for _, src := range sources {
				if len(inFlight) >= s.FetchConcurrency {
					break
				}
				if inFlight[src.id] {
					continue
				}
				inFlight[src.id] = true
				tasks.Go(func() { fetched <- fetchCompletion{src.id, w.fetch(ctx, src, s)} })
			}
		}

	tick:
		for {
			select {
			case <-ctx.Done():
				return nil
			case err := <-sender:
				return err
			case result := <-fetched:
				delete(inFlight, result.id)
				if result.err != nil {
					return result.err
				}
			case <-ticker.C:
				break tick
			}
		}
	}
}

func (w *worker) deliveryLoop(ctx context.Context) error {
	for ctx.Err() == nil {
		s := w.settings.Load()
		maintenance, err := w.config.maintenance()
		if err != nil || maintenance {
			return err
		}
		if !s.DeliveryPaused {
			worked, err := w.deliverOne(ctx, *s)
			if err != nil {
				return err
			}
			if worked {
				continue
			}
		}
		wait(ctx, 250*time.Millisecond)
	}
	return nil
}
