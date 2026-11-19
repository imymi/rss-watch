package worker

import (
	"context"
	"errors"
	"path/filepath"
	"time"
)

// Check reports whether the existing worker heartbeat is healthy for Compose.
func Check(ctx context.Context, config Config) error {
	if maintenance, err := config.maintenance(); err != nil || maintenance {
		return err
	}
	db, err := OpenDatabase(ctx, filepath.Join(config.DataDir, "rss-watch.sqlite3"))
	if err != nil {
		return err
	}
	defer db.Close()
	var healthy bool
	err = db.QueryRowContext(ctx, `SELECT COALESCE((SELECT value FROM runtime WHERE key='running'),'0')='1'
		AND ? - CAST(COALESCE((SELECT value FROM runtime WHERE key='heartbeat'),'0') AS REAL) < 10`,
		epoch(time.Now())).Scan(&healthy)
	if err != nil {
		return err
	}
	if !healthy {
		return errors.New("Worker 心跳已超时")
	}
	return nil
}
