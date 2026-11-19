package web

import (
	"archive/zip"
	"context"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"time"

	"modernc.org/sqlite"
	"rss-watch/internal/worker"
)

const (
	maxArchive  = 100 << 20
	maxExpanded = 512 << 20
)

type sqliteBackup interface {
	NewBackup(string) (*sqlite.Backup, error)
	NewRestore(string) (*sqlite.Backup, error)
}

func snapshot(ctx context.Context, db *sql.DB, path string, restore bool) error {
	if !restore {
		if err := writeNew(path, nil); err != nil {
			return err
		}
	}
	connection, err := db.Conn(ctx)
	if err != nil {
		return err
	}
	defer connection.Close()
	return connection.Raw(func(conn any) (err error) {
		driver, ok := conn.(sqliteBackup)
		if !ok {
			return errors.New("SQLite 在线备份不可用")
		}
		var backup *sqlite.Backup
		if restore {
			backup, err = driver.NewRestore(sqliteURI(path, "mode=ro&immutable=1"))
		} else {
			backup, err = driver.NewBackup(sqliteURI(path, "mode=rw"))
		}
		if err != nil {
			return err
		}
		defer func() { err = errors.Join(err, backup.Finish()) }()
		for {
			if err := ctx.Err(); err != nil {
				return err
			}
			more, err := backup.Step(256)
			if err != nil || !more {
				return err
			}
		}
	})
}

func syncDir(path string) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()
	return f.Sync()
}

func atomicWrite(path string, data []byte) error {
	dir := filepath.Dir(path)
	tmp := filepath.Join(dir, ".write-"+hex.EncodeToString(randomBytes(16)))
	if err := writeNew(tmp, data); err != nil {
		return err
	}
	defer os.Remove(tmp)
	if err := os.Rename(tmp, path); err != nil {
		return err
	}
	return syncDir(dir)
}

func (a *App) backupRoot() string { return filepath.Join(a.config.DataDir, "backups") }

func (a *App) listBackups() ([]row, error) {
	items, err := os.ReadDir(a.backupRoot())
	if errors.Is(err, os.ErrNotExist) {
		return []row{}, nil
	}
	if err != nil {
		return nil, err
	}
	out := []row{}
	for i := len(items) - 1; i >= 0; i-- {
		item := items[i]
		if !item.IsDir() || strings.HasPrefix(item.Name(), ".") {
			continue
		}
		manifest, err := os.Lstat(filepath.Join(a.backupRoot(), item.Name(), "manifest.json"))
		if err == nil && manifest.Mode().IsRegular() {
			out = append(out, row{"name": item.Name()})
		}
	}
	return out, nil
}

func (a *App) findBackup(name string) (string, error) {
	if filepath.Base(name) != name || strings.HasPrefix(name, ".") {
		return "", invalid("备份不存在")
	}
	items, err := a.listBackups()
	if err != nil {
		return "", err
	}
	for _, item := range items {
		if item["name"] == name {
			return filepath.Join(a.backupRoot(), name), nil
		}
	}
	return "", invalid("备份不存在")
}

func openSnapshot(path string) (*sql.DB, error) {
	return sql.Open("sqlite", sqliteURI(path, "mode=ro&immutable=1&_pragma=trusted_schema(OFF)&_pragma=query_only(1)"))
}

func references(ctx context.Context, db queryer) ([]string, error) {
	list, err := rows(ctx, db, "SELECT DISTINCT credential_ref FROM bots WHERE deleted=0 ORDER BY credential_ref")
	if err != nil {
		return nil, err
	}
	refs := make([]string, 0, len(list))
	for _, item := range list {
		name, ok := item["credential_ref"].(string)
		if !ok || !credentialName(name) {
			return nil, invalid("备份凭据引用无效")
		}
		refs = append(refs, name)
	}
	return refs, nil
}

func (a *App) createBackup(ctx context.Context, prefix string) (string, error) {
	lock, err := a.adminLock(ctx, "admin", true)
	if err != nil {
		return "", err
	}
	defer lock.Close()
	root := a.backupRoot()
	if err := os.MkdirAll(root, 0o700); err != nil {
		return "", err
	}
	staged, err := os.MkdirTemp(root, ".backup-")
	if err != nil {
		return "", err
	}
	defer os.RemoveAll(staged)
	path := filepath.Join(staged, dbName)
	if err := snapshot(ctx, a.db, path, false); err != nil {
		return "", err
	}
	db, err := openSnapshot(path)
	if err != nil {
		return "", err
	}
	refs, err := references(ctx, db)
	db.Close()
	if err != nil {
		return "", err
	}
	creds := filepath.Join(staged, "credentials")
	if err := os.Mkdir(creds, 0o700); err != nil {
		return "", err
	}
	for _, ref := range refs {
		content, err := worker.ReadCredentialFile(a.config.CredentialsDir, ref)
		if err != nil {
			return "", invalid("无法备份活动机器人凭据")
		}
		if err := writeNew(filepath.Join(creds, ref), content); err != nil {
			return "", err
		}
	}
	metadata := row{"schema_version": 2, "created": now(), "data_dir": a.config.DataDir, "credentials_dir": a.config.CredentialsDir}
	if err := writeNew(filepath.Join(staged, "manifest.json"), []byte(objectJSON(metadata))); err != nil {
		return "", err
	}
	for _, dir := range []string{creds, staged} {
		if err := syncDir(dir); err != nil {
			return "", err
		}
	}
	name := prefix + "-" + time.Now().UTC().Format("20060102-150405") + "-" + hex.EncodeToString(randomBytes(4))
	if err := os.Rename(staged, filepath.Join(root, name)); err != nil {
		return "", err
	}
	return name, syncDir(root)
}

func zipBackup(output io.Writer, directory string) error {
	entries, err := os.ReadDir(filepath.Join(directory, "credentials"))
	if err != nil {
		return err
	}
	names := []string{dbName, "manifest.json"}
	for _, item := range entries {
		if !credentialName(item.Name()) || !item.Type().IsRegular() {
			return invalid("备份包含无效凭据文件")
		}
		names = append(names, "credentials/"+item.Name())
	}
	archive := zip.NewWriter(output)
	for _, name := range names {
		path := filepath.Join(directory, name)
		info, err := os.Lstat(path)
		if err != nil || !info.Mode().IsRegular() {
			return invalid("备份文件无效")
		}
		file, err := os.Open(path)
		if err != nil {
			return err
		}
		header := &zip.FileHeader{Name: name, Method: zip.Deflate}
		header.SetMode(0o600)
		entry, err := archive.CreateHeader(header)
		if err == nil {
			_, err = io.Copy(entry, file)
		}
		file.Close()
		if err != nil {
			return err
		}
	}
	return archive.Close()
}

func unpackBackup(archive *zip.Reader, directory string) error {
	if len(archive.File) > 4096 {
		return invalid("备份文件数量过多")
	}
	if err := os.Mkdir(filepath.Join(directory, "credentials"), 0o700); err != nil {
		return err
	}
	seen := map[string]bool{}
	remaining := uint64(maxExpanded)
	for _, file := range archive.File {
		name := file.Name
		if seen[name] || file.Flags&1 != 0 {
			return invalid("备份包含重复或加密条目")
		}
		seen[name] = true
		if name == "credentials/" && file.FileInfo().IsDir() && file.UncompressedSize64 == 0 {
			continue
		}
		ref, credential := strings.CutPrefix(name, "credentials/")
		if !file.Mode().IsRegular() || (name != dbName && name != "manifest.json" && !(credential && credentialName(ref))) {
			return invalid("备份包含不允许的文件")
		}
		if file.UncompressedSize64 > remaining || (name != dbName && file.UncompressedSize64 > 65536) {
			return invalid("备份解压大小超过限制")
		}
		remaining -= file.UncompressedSize64
		if err := extractFile(file, filepath.Join(directory, name)); err != nil {
			return err
		}
	}
	if !seen[dbName] || !seen["manifest.json"] {
		return invalid("备份缺少数据库或清单")
	}
	return nil
}

func extractFile(file *zip.File, path string) error {
	src, err := file.Open()
	if err != nil {
		return err
	}
	defer src.Close()
	dst, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	n, err := io.Copy(dst, io.LimitReader(src, int64(file.UncompressedSize64)+1))
	err = errors.Join(err, dst.Close())
	if err != nil || n != int64(file.UncompressedSize64) {
		return invalid("备份条目损坏或解压大小不符")
	}
	return nil
}

type databaseObject struct {
	kind, name, definition string
}

func dbObjects(ctx context.Context, db *sql.DB) ([]databaseObject, error) {
	data, err := rows(ctx, db, "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL AND NOT (type='table' AND name IN ('sqlite_stat1','sqlite_stat4')) ORDER BY type,name")
	if err != nil {
		return nil, err
	}
	out := make([]databaseObject, 0, len(data))
	for _, item := range data {
		out = append(out, databaseObject{asString(item["type"]), asString(item["name"]), strings.Join(strings.Fields(asString(item["sql"])), " ")})
	}
	return out, nil
}

func (a *App) validateBackup(ctx context.Context, directory string) (int, []string, error) {
	db, err := openSnapshot(filepath.Join(directory, dbName))
	if err != nil {
		return 0, nil, err
	}
	defer db.Close()
	var version int
	var check string
	if err := db.QueryRowContext(ctx, "PRAGMA user_version").Scan(&version); err != nil || (version != 1 && version != 2) {
		return 0, nil, invalid("备份数据库版本不兼容")
	}
	if err := db.QueryRowContext(ctx, "PRAGMA quick_check").Scan(&check); err != nil || check != "ok" {
		return 0, nil, invalid("备份数据库完整性检查失败")
	}
	items, err := dbObjects(ctx, db)
	if err != nil || !slices.Equal(items, a.schemaObjects[version]) {
		return 0, nil, invalid("备份数据库结构不兼容")
	}
	if violations, err := rows(ctx, db, "PRAGMA foreign_key_check"); err != nil || len(violations) != 0 {
		return 0, nil, invalid("备份数据库外键检查失败")
	}
	content, err := os.ReadFile(filepath.Join(directory, "manifest.json"))
	var manifest struct {
		Version int `json:"schema_version"`
	}
	if err != nil || json.Unmarshal(content, &manifest) != nil || manifest.Version != version {
		return 0, nil, invalid("备份清单与数据库版本不一致")
	}
	if version == 2 {
		settings, err := rows(ctx, db, "SELECT key,value FROM settings")
		if err != nil {
			return 0, nil, err
		}
		values := row{}
		for _, item := range settings {
			decoder := json.NewDecoder(strings.NewReader(asString(item["value"])))
			decoder.UseNumber()
			var value any
			if err := decoder.Decode(&value); err != nil {
				return 0, nil, invalid("备份设置格式无效")
			}
			if err := decoder.Decode(new(any)); err != io.EOF {
				return 0, nil, invalid("备份设置格式无效")
			}
			values[asString(item["key"])] = value
		}
		if _, err := a.validateSettings(values); err != nil {
			return 0, nil, err
		}
	}
	refs, err := references(ctx, db)
	if err != nil {
		return 0, nil, err
	}
	credentials := filepath.Join(directory, "credentials")
	entries, err := os.ReadDir(credentials)
	if err != nil || len(entries) != len(refs) {
		return 0, nil, invalid("备份凭据集合与活动机器人不一致")
	}
	for _, ref := range refs {
		if err := worker.ValidateCredential(credentials, ref); err != nil {
			return 0, nil, invalid("备份中的机器人凭据无效或缺失")
		}
	}
	return version, refs, nil
}

func (a *App) installBackup(ctx context.Context, directory string, refs []string) error {
	for _, ref := range refs {
		data, err := os.ReadFile(filepath.Join(directory, "credentials", ref))
		if err != nil {
			return err
		}
		if err := atomicWrite(filepath.Join(a.config.CredentialsDir, ref), data); err != nil {
			return err
		}
	}
	return snapshot(ctx, a.db, filepath.Join(directory, dbName), true)
}

func prepareRestore(ctx context.Context, directory string, version int, username, hash string) error {
	db, err := sql.Open("sqlite", sqliteURI(filepath.Join(directory, dbName), "mode=rw&_txlock=immediate&_pragma=trusted_schema(OFF)"))
	if err != nil {
		return err
	}
	defer db.Close()
	var mode string
	if err := db.QueryRowContext(ctx, "PRAGMA journal_mode=DELETE").Scan(&mode); err != nil || mode != "delete" {
		return invalid("无法准备独立的恢复数据库")
	}
	return worker.WriteTx(ctx, db, func(tx *sql.Tx) error {
		if version == 1 {
			if _, err := tx.ExecContext(ctx, schema+"\nPRAGMA user_version=2;"); err != nil {
				return err
			}
		}
		if _, err := tx.ExecContext(ctx, `INSERT OR REPLACE INTO admins VALUES (1,?,?);
			DELETE FROM sessions; DELETE FROM runtime;`, username, hash); err != nil {
			return err
		}
		if err := setSettings(ctx, tx, row{"fetch_paused": true, "delivery_paused": true}); err != nil {
			return err
		}
		_, err := tx.ExecContext(ctx, "INSERT INTO audit(created,actor,action) VALUES (?,?,?)", now(), username, "恢复备份，等待检查并退出维护模式")
		return err
	})
}

func (a *App) restore(ctx context.Context, directory, session string) error {
	version, refs, err := a.validateBackup(ctx, directory)
	if err != nil {
		return err
	}
	lock, err := a.adminLock(ctx, "restore", false)
	if err != nil {
		return invalid("已有恢复操作正在执行")
	}
	defer lock.Close()
	marker := filepath.Join(a.config.DataDir, "maintenance")
	previous, precedingErr := os.ReadFile(marker)
	if precedingErr != nil && !errors.Is(precedingErr, os.ErrNotExist) {
		return precedingErr
	}
	state := row{"started": now(), "state": "restoring"}
	mark := func() error { return atomicWrite(marker, []byte(objectJSON(state))) }
	if err := mark(); err != nil {
		return err
	}
	gate, err := a.gate(ctx, true)
	if err != nil {
		return err
	}
	defer gate.Close()
	if ok, err := a.authenticated(ctx, session); err != nil || !ok {
		var reset error
		if previous == nil && errors.Is(precedingErr, os.ErrNotExist) {
			reset = errors.Join(os.Remove(marker), syncDir(a.config.DataDir))
		} else {
			reset = atomicWrite(marker, previous)
		}
		if err == nil {
			err = httpError{401, "请先登录"}
		}
		return errors.Join(err, reset)
	}
	// Once the live operation begins, complete or roll back even if HTTP disconnects.
	ctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 2*time.Minute)
	defer cancel()
	rollback, err := a.createBackup(ctx, "pre-restore")
	if err != nil {
		return err
	}
	state["rollback"] = rollback
	if err := mark(); err != nil {
		return err
	}
	username, hash, err := a.admin(ctx)
	if err != nil {
		return err
	}
	old, err := references(ctx, a.db)
	if err != nil {
		return err
	}
	if err := prepareRestore(ctx, directory, version, username, hash); err != nil {
		// Only the staging database was changed.
		state["state"] = "ready"
		return errors.Join(err, mark())
	}
	err = a.installBackup(ctx, directory, refs)
	if err != nil {
		rollbackCtx, cleanup := context.WithTimeout(context.WithoutCancel(ctx), 2*time.Minute)
		defer cleanup()
		if restoreErr := a.installBackup(rollbackCtx, filepath.Join(a.backupRoot(), rollback), old); restoreErr != nil {
			return errors.Join(err, fmt.Errorf("回退失败：%w", restoreErr))
		}
		for _, ref := range refs {
			_ = a.removeUnusedCredential(rollbackCtx, ref)
		}
		state["state"] = "ready"
		return errors.Join(err, mark())
	}
	for _, ref := range old {
		if !slices.Contains(refs, ref) {
			_ = a.removeUnusedCredential(ctx, ref) // The restored database is already committed.
		}
	}
	state["state"] = "ready"
	return mark()
}

func (a *App) resume(ctx context.Context, session string) error {
	lock, err := a.adminLock(ctx, "restore", false)
	if err != nil {
		return invalid("已有恢复操作正在执行")
	}
	defer lock.Close()
	gate, err := a.gate(ctx, true)
	if err != nil {
		return err
	}
	defer gate.Close()
	if ok, err := a.authenticated(ctx, session); err != nil {
		return err
	} else if !ok {
		return httpError{401, "请先登录"}
	}
	path := filepath.Join(a.config.DataDir, "maintenance")
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	var state struct {
		Status string `json:"state"`
	}
	if len(data) != 0 && (json.Unmarshal(data, &state) != nil || state.Status == "restoring") {
		return invalid("上次恢复未完成，请重新导入有效备份")
	}
	if err := os.Remove(path); err != nil {
		return err
	}
	return syncDir(a.config.DataDir)
}

// RunBackups schedules automatic backups using the existing runtime keys.
func (a *App) RunBackups(ctx context.Context) {
	lock, err := a.adminLock(ctx, "backup-scheduler", false)
	if err != nil {
		return
	}
	defer lock.Close()
	tick := time.NewTicker(30 * time.Second)
	defer tick.Stop()
	for ctx.Err() == nil {
		a.autoBackup(ctx)
		select {
		case <-ctx.Done():
		case <-tick.C:
		}
	}
}

func (a *App) autoBackup(ctx context.Context) {
	gate, err := a.gate(ctx, false)
	if err != nil {
		return
	}
	defer gate.Close()
	if a.maintenance() {
		return
	}
	if err := a.runAutoBackup(ctx); err != nil {
		_, _ = a.db.ExecContext(ctx, "INSERT OR REPLACE INTO runtime VALUES ('auto_backup_error','自动备份失败，请检查数据目录和机器人凭据')")
	}
}

func (a *App) runAutoBackup(ctx context.Context) error {
	cfg, err := a.readSettings(ctx)
	if err != nil {
		return err
	}
	hours, _ := cfg["backup_hours"].(float64)
	keep, _ := cfg["backup_keep"].(float64)
	if hours == 0 || keep < 1 {
		return nil
	}
	var last float64
	if err := a.db.QueryRowContext(ctx, "SELECT COALESCE((SELECT value FROM runtime WHERE key='last_auto_backup'),'0')").Scan(&last); err != nil {
		return err
	}
	if now()-last < hours*3600 {
		return nil
	}
	if _, err := a.createBackup(ctx, "auto"); err != nil {
		return err
	}
	if _, err := a.db.ExecContext(ctx, `INSERT OR REPLACE INTO runtime VALUES ('last_auto_backup',?);
		DELETE FROM runtime WHERE key='auto_backup_error';`, now()); err != nil {
		return err
	}
	lock, err := a.adminLock(ctx, "admin", true)
	if err != nil {
		return err
	}
	defer lock.Close()
	items, err := a.listBackups()
	if err != nil {
		return err
	}
	for _, item := range items {
		name := asString(item["name"])
		if strings.HasPrefix(name, "auto-") {
			if keep <= 0 {
				if err := os.RemoveAll(filepath.Join(a.backupRoot(), name)); err != nil {
					return err
				}
			}
			keep--
		}
	}
	return nil
}
