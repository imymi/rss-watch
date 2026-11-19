package worker

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"database/sql"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"math"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
	"unicode"

	"golang.org/x/sys/unix"
)

type credential struct {
	Webhook string  `json:"webhook"`
	Secret  *string `json:"secret"`
}

func loadCredential(dir, ref string) (credential, error) {
	var cred credential
	if filepath.Base(ref) != ref || !strings.HasSuffix(ref, ".json") {
		return cred, errors.New("凭据引用无效")
	}
	path := filepath.Join(dir, ref)
	fd, err := unix.Open(path, unix.O_RDONLY|unix.O_NONBLOCK|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if err != nil {
		return cred, err
	}
	file := os.NewFile(uintptr(fd), path)
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return cred, err
	}
	if info.Mode() != 0o600 || info.Size() > 64<<10 {
		return cred, errors.New("凭据文件格式无效")
	}
	body, err := io.ReadAll(io.LimitReader(file, 64<<10+1))
	if err != nil {
		return cred, err
	}
	if len(body) > 64<<10 || json.Unmarshal(body, &cred) != nil || cred.Secret == nil || !validWebhook(cred.Webhook) {
		return credential{}, errors.New("凭据内容无效")
	}
	return cred, nil
}

func validWebhook(value string) bool {
	if strings.ContainsFunc(value, unicode.IsSpace) {
		return false
	}
	u, err := url.Parse(value)
	if err != nil {
		return false
	}
	const prefix = "/open-apis/bot/v2/hook/"
	path := u.EscapedPath()
	return u.Scheme == "https" && strings.EqualFold(u.Hostname(), "open.feishu.cn") &&
		(u.Port() == "" || u.Port() == "443") && u.User == nil && u.RawQuery == "" && u.Fragment == "" &&
		strings.HasPrefix(path, prefix) && len(path) > len(prefix)
}

type delivery struct {
	id, text    string
	botID       int64
	attempt     int
	credentials credential
}

type deliveryError struct {
	message   string
	transient bool
	delay     float64
}

func (w *worker) recover(ctx context.Context, s settings) error {
	_, err := w.db.ExecContext(ctx, `UPDATE deliveries SET
		state=CASE WHEN attempts>=? THEN 'failed' ELSE 'pending' END,
		last_error='上次发送中断，可能已被接收',updated=? WHERE state='sending'`, s.MaxAttempts, epoch(time.Now()))
	return err
}

func (w *worker) claim(ctx context.Context, s settings, now float64) (*delivery, bool, error) {
	var claimed *delivery
	var found bool
	err := writeTx(ctx, w.db, func(tx *sql.Tx) error {
		var task delivery
		var ref string
		err := tx.QueryRowContext(ctx, `SELECT d.id,d.bot_id,d.payload,d.attempts,b.credential_ref
			FROM deliveries d JOIN bots b ON b.id=d.bot_id WHERE d.state='pending'
			AND d.next_attempt<=? AND b.next_send<=? AND b.deleted=0 ORDER BY d.next_attempt,d.created LIMIT 1`, now, now).
			Scan(&task.id, &task.botID, &task.text, &task.attempt, &ref)
		if err == sql.ErrNoRows {
			return nil
		}
		if err != nil {
			return err
		}
		found = true
		if task.attempt >= s.MaxAttempts {
			_, err := tx.ExecContext(ctx, `UPDATE deliveries SET state='failed',updated=?,
				last_error='已达到当前发送次数上限' WHERE id=?`, now, task.id)
			return err
		}
		task.attempt++
		if _, err := tx.ExecContext(ctx, `UPDATE deliveries SET state='sending',attempts=?,updated=?,next_attempt=? WHERE id=?`,
			task.attempt, now, now+s.retryDelay(task.attempt), task.id); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `UPDATE bots SET next_send=? WHERE id=?`, now+float64(s.SendInterval), task.botID); err != nil {
			return err
		}
		// Rotation can only unlink this credential after the transaction releases the database.
		task.credentials, _ = loadCredential(w.config.CredentialsDir, ref)
		claimed = &task
		return nil
	})
	return claimed, found, err
}

func (w *worker) deliverOne(ctx context.Context, s settings) (bool, error) {
	task, found, err := w.claim(ctx, s, epoch(time.Now()))
	if err != nil || task == nil {
		return found, err
	}
	var failure *deliveryError
	if task.credentials.Secret == nil {
		failure = &deliveryError{message: "凭据不可读、权限不是 0600 或格式无效"}
	} else {
		failure = sendFeishu(ctx, w.clients.send, *task, s.RequestDeadline)
	}
	if ctx.Err() != nil {
		return true, ctx.Err() // recover the durable claim on the next start
	}
	s = *w.settings.Load()
	now := epoch(time.Now())
	return true, writeTx(ctx, w.db, func(tx *sql.Tx) error {
		sendDelay := float64(s.SendInterval)
		if failure == nil {
			if _, err := tx.ExecContext(ctx, `UPDATE deliveries SET state='sent',updated=?,last_error=NULL WHERE id=?`, now, task.id); err != nil {
				return err
			}
		} else {
			state := "failed"
			if failure.transient && task.attempt < s.MaxAttempts {
				state = "pending"
			}
			next := now + max(s.retryDelay(task.attempt), failure.delay)
			if _, err := tx.ExecContext(ctx, `UPDATE deliveries SET state=?,next_attempt=?,updated=?,last_error=? WHERE id=?`,
				state, next, now, failure.message, task.id); err != nil {
				return err
			}
			sendDelay = max(sendDelay, failure.delay)
			log.Printf("推送 %s：%s", task.id, failure.message)
		}
		_, err := tx.ExecContext(ctx, `UPDATE bots SET next_send=max(next_send,?) WHERE id=?`, now+sendDelay, task.botID)
		return err
	})
}

type feishuMessage struct {
	Type      string            `json:"msg_type"`
	Content   map[string]string `json:"content"`
	Timestamp string            `json:"timestamp,omitempty"`
	Sign      string            `json:"sign,omitempty"`
}

func signature(timestamp, secret string) string {
	mac := hmac.New(sha256.New, []byte(timestamp+"\n"+secret))
	return base64.StdEncoding.EncodeToString(mac.Sum(nil))
}

func sendFeishu(ctx context.Context, client *http.Client, task delivery, deadline int) *deliveryError {
	const networkError = "飞书网络连接失败或超时"
	ctx, cancel := context.WithTimeout(ctx, time.Duration(deadline)*time.Second)
	defer cancel()
	payload := feishuMessage{Type: "text", Content: map[string]string{"text": task.text}}
	if secret := *task.credentials.Secret; secret != "" {
		payload.Timestamp = strconv.FormatInt(time.Now().Unix(), 10)
		payload.Sign = signature(payload.Timestamp, secret)
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return &deliveryError{message: "飞书请求格式无效"}
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, task.credentials.Webhook, bytes.NewReader(body))
	if err != nil {
		return &deliveryError{message: "飞书请求格式无效"}
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := client.Do(req)
	if err != nil {
		return &deliveryError{message: networkError, transient: true}
	}
	defer resp.Body.Close()
	delay := retryAfter(resp.Header.Get("Retry-After"), time.Now())
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		if resp.StatusCode == http.StatusTooManyRequests || resp.StatusCode >= 500 {
			return &deliveryError{fmt.Sprintf("飞书 HTTP %d", resp.StatusCode), true, delay}
		}
		return &deliveryError{message: fmt.Sprintf("飞书 HTTP %d", resp.StatusCode)}
	}
	body, err = io.ReadAll(io.LimitReader(resp.Body, 64<<10+1))
	if err != nil {
		return &deliveryError{message: networkError, transient: true}
	}
	if len(body) > 64<<10 {
		return &deliveryError{message: "飞书响应体过大", transient: true}
	}
	var result map[string]json.RawMessage
	if json.Unmarshal(body, &result) != nil || result == nil {
		return &deliveryError{message: "飞书响应不是有效 JSON 对象", transient: true}
	}
	raw, ok := result["code"]
	if !ok {
		raw = result["StatusCode"]
	}
	var code *int64
	if json.Unmarshal(raw, &code) != nil || code == nil {
		return &deliveryError{message: "飞书响应缺少有效业务状态码", transient: true}
	}
	switch *code {
	case 0:
		return nil
	case 11232, 99991400, 5000, 99991500:
		return &deliveryError{fmt.Sprintf("飞书业务错误 %d", *code), true, delay}
	default:
		return &deliveryError{message: fmt.Sprintf("飞书业务错误 %d（请检查机器人配置）", *code)}
	}
}

func retryAfter(value string, now time.Time) float64 {
	if seconds, err := strconv.ParseFloat(strings.TrimSpace(value), 64); err == nil && !math.IsNaN(seconds) && !math.IsInf(seconds, 0) {
		return max(0, seconds)
	}
	if deadline, err := http.ParseTime(value); err == nil {
		return max(0, epoch(deadline)-epoch(now))
	}
	return 0
}
