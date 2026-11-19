package web

import (
	"context"
	"database/sql"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"

	"golang.org/x/sys/unix"
	"rss-watch/internal/worker"
)

//go:embed schema.sql
var schema string

const dbName = "rss-watch.sqlite3"

type row = map[string]any

type queryer interface {
	QueryContext(context.Context, string, ...any) (*sql.Rows, error)
}

func rows(ctx context.Context, db queryer, sqlText string, args ...any) ([]row, error) {
	result, err := db.QueryContext(ctx, sqlText, args...)
	if err != nil {
		return nil, err
	}
	defer result.Close()
	columns, err := result.Columns()
	if err != nil {
		return nil, err
	}
	out := []row{}
	for result.Next() {
		values, refs := make([]any, len(columns)), make([]any, len(columns))
		for i := range refs {
			refs[i] = &values[i]
		}
		if err := result.Scan(refs...); err != nil {
			return nil, err
		}
		item := row{}
		for i, col := range columns {
			item[col] = values[i]
		}
		out = append(out, item)
	}
	return out, result.Err()
}

func now() float64 { return float64(time.Now().UnixMicro()) / 1e6 }

func sqliteURI(path, query string) string {
	return (&url.URL{Scheme: "file", Path: path, RawQuery: query}).String()
}

func initialize(ctx context.Context, config worker.Config) (*sql.DB, error) {
	for _, dir := range []string{config.DataDir, config.CredentialsDir} {
		if !filepath.IsAbs(dir) {
			return nil, errors.New("数据与凭据目录必须为绝对路径")
		}
		if err := os.MkdirAll(dir, 0o700); err != nil {
			return nil, err
		}
	}
	lockCtx, cancel := context.WithTimeout(ctx, 45*time.Second)
	defer cancel()
	gate, err := worker.LockFile(lockCtx, filepath.Join(config.DataDir, "data.lock"), unix.LOCK_SH, true)
	if err != nil {
		return nil, err
	}
	defer gate.Close()
	path := filepath.Join(config.DataDir, dbName)
	if file, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_RDWR, 0o600); err == nil {
		file.Close()
	} else if !errors.Is(err, os.ErrExist) {
		return nil, err
	}
	db, err := sql.Open("sqlite", sqliteURI(path, "mode=rw&_txlock=immediate&_pragma=busy_timeout(5000)&_pragma=foreign_keys(1)&_pragma=trusted_schema(OFF)"))
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(8)
	db.SetMaxIdleConns(4)
	fail := func(err error) (*sql.DB, error) { db.Close(); return nil, err }
	var version int
	if err := db.QueryRowContext(ctx, "PRAGMA user_version").Scan(&version); err != nil {
		return fail(err)
	}
	if version < 0 || version > 2 {
		return fail(errors.New("数据库版本不兼容"))
	}
	var journal string
	if err := db.QueryRowContext(ctx, "PRAGMA journal_mode=WAL").Scan(&journal); err != nil || journal != "wal" {
		return fail(errors.New("无法启用 SQLite WAL"))
	}
	if version == 1 {
		path := filepath.Join(config.DataDir, fmt.Sprintf("migration-v1-%d.sqlite3", time.Now().UnixNano()))
		if err := snapshot(ctx, db, path, false); err != nil {
			return fail(err)
		}
	}
	if version < 2 {
		if err := worker.WriteTx(ctx, db, func(tx *sql.Tx) error {
			_, err := tx.ExecContext(ctx, schema+"\nPRAGMA user_version=2;")
			return err
		}); err != nil {
			return fail(err)
		}
	}
	return db, nil
}

func affected(result sql.Result, err error, empty string) error {
	if err != nil {
		return err
	}
	n, err := result.RowsAffected()
	if err == nil && n == 0 {
		return invalid(empty)
	}
	return err
}

func update(ctx context.Context, db interface {
	ExecContext(context.Context, string, ...any) (sql.Result, error)
}, empty, query string, args ...any) error {
	result, err := db.ExecContext(ctx, query, args...)
	return affected(result, err, empty)
}

type inputError string

func (e inputError) Error() string { return string(e) }
func invalid(message string) error { return inputError(message) }

func asString(value any) string {
	if value == nil {
		return ""
	}
	return fmt.Sprint(value)
}

func objectJSON(v any) string {
	data, _ := json.Marshal(v)
	return string(data)
}

func where(parts []string) string {
	if len(parts) == 0 {
		return ""
	}
	return " WHERE " + strings.Join(parts, " AND ")
}

func (a *App) gate(ctx context.Context, exclusive bool) (*os.File, error) {
	ctx, cancel := context.WithTimeout(ctx, 45*time.Second)
	defer cancel()
	flag := unix.LOCK_SH
	if exclusive {
		flag = unix.LOCK_EX
	}
	return worker.LockFile(ctx, filepath.Join(a.config.DataDir, "data.lock"), flag, true)
}

func (a *App) adminLock(ctx context.Context, name string, wait bool) (*os.File, error) {
	return worker.LockFile(ctx, filepath.Join(a.config.DataDir, name+".lock"), unix.LOCK_EX, wait)
}

func (a *App) maintenance() bool {
	_, err := os.Stat(filepath.Join(a.config.DataDir, "maintenance"))
	// Treat an inaccessible marker as active.
	return !errors.Is(err, os.ErrNotExist)
}
