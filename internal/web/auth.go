package web

import (
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"unicode/utf8"

	"golang.org/x/crypto/scrypt"
	"rss-watch/internal/worker"
)

func randomBytes(size int) []byte {
	bytes := make([]byte, size)
	rand.Read(bytes)
	return bytes
}

func token() string { return base64.RawURLEncoding.EncodeToString(randomBytes(32)) }

func tokenHash(token string) string {
	sum := sha256.Sum256([]byte(token))
	return hex.EncodeToString(sum[:])
}

func (a *App) csrf(token string) string {
	h := hmac.New(sha256.New, a.key)
	h.Write([]byte(token))
	return hex.EncodeToString(h.Sum(nil))
}

func (a *App) validCSRF(token, received string) bool {
	return token != "" && hmac.Equal([]byte(a.csrf(token)), []byte(received))
}

func writeNew(path string, data []byte) error {
	file, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	if _, err := file.Write(data); err != nil {
		file.Close()
		os.Remove(path)
		return err
	}
	err = errors.Join(file.Sync(), file.Close())
	if err != nil {
		os.Remove(path)
	}
	return err
}

func passwordHash(password string) (string, error) {
	if n := utf8.RuneCountInString(password); n < 12 || n > 256 {
		return "", invalid("密码长度必须为 12 到 256 个字符")
	}
	salt := randomBytes(16)
	key, err := scrypt.Key([]byte(password), salt, 16384, 8, 1, 64)
	if err != nil {
		return "", err
	}
	return "scrypt$" + hex.EncodeToString(salt) + "$" + hex.EncodeToString(key), nil
}

func passwordMatches(password, hash string) bool {
	parts := strings.Split(hash, "$")
	if utf8.RuneCountInString(password) > 256 || len(parts) != 3 || parts[0] != "scrypt" {
		return false
	}
	salt, e1 := hex.DecodeString(parts[1])
	want, e2 := hex.DecodeString(parts[2])
	if e1 != nil || e2 != nil || len(salt) != 16 || len(want) != 64 {
		return false
	}
	key, err := scrypt.Key([]byte(password), salt, 16384, 8, 1, 64)
	return err == nil && hmac.Equal(want, key)
}

func (a *App) bootstrapPath() string { return filepath.Join(a.config.DataDir, "bootstrap-token") }

func (a *App) initAuth(ctx context.Context) error {
	gate, err := a.gate(ctx, false)
	if err != nil {
		return err
	}
	defer gate.Close()
	lock, err := a.adminLock(ctx, "auth", true)
	if err != nil {
		return err
	}
	defer lock.Close()
	key := filepath.Join(a.config.DataDir, "session-key")
	if err := writeNew(key, randomBytes(32)); err != nil && !errors.Is(err, os.ErrExist) {
		return err
	}
	a.key, err = os.ReadFile(key)
	if err != nil || len(a.key) != 32 {
		return errors.New("会话密钥无法读取或长度无效")
	}
	if _, _, err := a.admin(ctx); err == sql.ErrNoRows {
		if err := writeNew(a.bootstrapPath(), []byte(token())); err != nil && !errors.Is(err, os.ErrExist) {
			return err
		}
	} else if err != nil {
		return err
	}
	return nil
}

func (a *App) admin(ctx context.Context) (username, hash string, err error) {
	err = a.db.QueryRowContext(ctx, "SELECT username,password_hash FROM admins WHERE id=1").Scan(&username, &hash)
	return
}

func (a *App) setup(ctx context.Context, bootstrap, username, password string) error {
	username = strings.TrimSpace(username)
	if n := utf8.RuneCountInString(username); n < 1 || n > 64 {
		return invalid("管理员名称应为 1 到 64 个字符")
	}
	lock, err := a.adminLock(ctx, "auth", true)
	if err != nil {
		return err
	}
	defer lock.Close()
	if _, _, err := a.admin(ctx); err == nil {
		return invalid("管理员已创建，请登录")
	} else if err != sql.ErrNoRows {
		return err
	}
	key, err := os.ReadFile(a.bootstrapPath())
	if err != nil || !hmac.Equal(key, []byte(bootstrap)) {
		return invalid("初始化口令无效")
	}
	hash, err := passwordHash(password)
	if err != nil {
		return err
	}
	if _, err := a.db.ExecContext(ctx, "INSERT INTO admins VALUES (1,?,?)", username, hash); err != nil {
		return err
	}
	if err := os.Remove(a.bootstrapPath()); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return a.audit(ctx, "创建管理员")
}

func (a *App) throttle(ctx context.Context, host string) (string, error) {
	key := "login:" + tokenHash(host)
	return key, worker.WriteTx(ctx, a.db, func(tx *sql.Tx) error {
		var raw string
		attempt := struct {
			Since float64 `json:"since"`
			Count int     `json:"count"`
		}{Since: now()}
		err := tx.QueryRowContext(ctx, "SELECT value FROM runtime WHERE key=?", key).Scan(&raw)
		if err == nil {
			if err := json.Unmarshal([]byte(raw), &attempt); err != nil {
				return err
			}
		} else if err != sql.ErrNoRows {
			return err
		}
		if now()-attempt.Since >= 900 {
			attempt.Since, attempt.Count = now(), 0
		}
		if attempt.Count >= 10 {
			return invalid("登录尝试过多，请在 15 分钟后重试")
		}
		attempt.Count++
		_, err = tx.ExecContext(ctx, "INSERT OR REPLACE INTO runtime VALUES (?,?)", key, objectJSON(attempt))
		return err
	})
}

func (a *App) login(ctx context.Context, username, password, host string) (string, error) {
	rate, err := a.throttle(ctx, host)
	if err != nil {
		return "", err
	}
	current, hash, err := a.admin(ctx)
	if err != nil && err != sql.ErrNoRows {
		return "", err
	}
	if err == sql.ErrNoRows || !hmac.Equal([]byte(current), []byte(username)) || !passwordMatches(password, hash) {
		return "", invalid("用户名或密码错误")
	}
	session := token()
	err = worker.WriteTx(ctx, a.db, func(tx *sql.Tx) error {
		if _, err := tx.ExecContext(ctx, "DELETE FROM runtime WHERE key=?", rate); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, "DELETE FROM sessions WHERE expires<?", now()); err != nil {
			return err
		}
		result, err := tx.ExecContext(ctx, "INSERT INTO sessions SELECT ?,? FROM admins WHERE id=1 AND password_hash=?", tokenHash(session), now()+43200, hash)
		return affected(result, err, "请重新登录")
	})
	if err == nil {
		err = a.audit(ctx, "登录后台")
	}
	return session, err
}

func (a *App) authenticated(ctx context.Context, session string) (bool, error) {
	if session == "" || len(session) > 128 {
		return false, nil
	}
	var exists bool
	err := a.db.QueryRowContext(ctx, "SELECT EXISTS(SELECT 1 FROM sessions WHERE token_hash=? AND expires>?)", tokenHash(session), now()).Scan(&exists)
	return exists, err
}

func (a *App) changePassword(ctx context.Context, current, password string) error {
	_, oldHash, err := a.admin(ctx)
	if err != nil || !passwordMatches(current, oldHash) {
		return invalid("当前密码错误")
	}
	hash, err := passwordHash(password)
	if err != nil {
		return err
	}
	if err := worker.WriteTx(ctx, a.db, func(tx *sql.Tx) error {
		if err := update(ctx, tx, "当前密码错误", "UPDATE admins SET password_hash=? WHERE id=1 AND password_hash=?", hash, oldHash); err != nil {
			return err
		}
		_, err := tx.ExecContext(ctx, "DELETE FROM sessions")
		return err
	}); err != nil {
		return err
	}
	return a.audit(ctx, "修改管理员密码")
}

func (a *App) audit(ctx context.Context, action string) error {
	_, err := a.db.ExecContext(ctx, "INSERT INTO audit(created,actor,action) VALUES (?,COALESCE((SELECT username FROM admins WHERE id=1),'管理员'),?)", now(), action)
	return err
}
