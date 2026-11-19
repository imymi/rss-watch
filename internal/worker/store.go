package worker

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"net/url"
	"path/filepath"
	"strconv"
	"time"

	_ "modernc.org/sqlite"
)

func OpenDatabase(ctx context.Context, path string) (*sql.DB, error) {
	address := url.URL{Scheme: "file", Path: filepath.ToSlash(path)}
	db, err := sql.Open("sqlite", address.String()+"?mode=rw&_txlock=immediate&_pragma=foreign_keys(1)&_pragma=busy_timeout(5000)")
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(4)
	var version int
	if err := db.QueryRowContext(ctx, "PRAGMA user_version").Scan(&version); err != nil {
		db.Close()
		return nil, err
	}
	if version != 2 {
		db.Close()
		return nil, fmt.Errorf("Worker 需要已初始化的 SQLite schema 2，实际版本 %d", version)
	}
	var journal string
	if err := db.QueryRowContext(ctx, "PRAGMA journal_mode").Scan(&journal); err != nil {
		db.Close()
		return nil, err
	}
	if journal != "wal" {
		db.Close()
		return nil, errors.New("Worker 需要 SQLite WAL 模式")
	}
	return db, nil
}

func WriteTx(ctx context.Context, db *sql.DB, write func(*sql.Tx) error) error {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if err := write(tx); err != nil {
		return err
	}
	return tx.Commit()
}

func epoch(now time.Time) float64 {
	return float64(now.UnixMicro()) / 1e6
}

func (w *worker) heartbeat(ctx context.Context, running bool, revision string) error {
	now := strconv.FormatFloat(epoch(time.Now()), 'f', -1, 64)
	if !running {
		_, err := w.db.ExecContext(ctx, `INSERT OR REPLACE INTO runtime(key,value)
			VALUES ('heartbeat',?),('running','0')`, now)
		return err
	}
	_, err := w.db.ExecContext(ctx, `INSERT OR REPLACE INTO runtime(key,value)
		VALUES ('heartbeat',?),('running','1'),('settings_applied',?)`, now, revision)
	return err
}

func (w *worker) cleanup(ctx context.Context, now time.Time, retention int) error {
	cutoff := epoch(now) - float64(retention)*86400
	return WriteTx(ctx, w.db, func(tx *sql.Tx) error {
		for _, query := range []struct {
			sql    string
			cutoff float64
		}{
			{`DELETE FROM sessions WHERE expires < ?`, epoch(now)},
			{`DELETE FROM audit WHERE created < ?`, cutoff},
			{`DELETE FROM deliveries WHERE state='sent' AND updated < ?`, cutoff},
			{`UPDATE articles SET title=NULL,summary=NULL,link=NULL,published=NULL
				WHERE discovered < ? AND title IS NOT NULL AND NOT EXISTS
				(SELECT 1 FROM deliveries d WHERE d.article_id=articles.id
				AND d.state IN ('pending','sending','failed'))`, cutoff},
		} {
			if _, err := tx.ExecContext(ctx, query.sql, query.cutoff); err != nil {
				return err
			}
		}
		return nil
	})
}
