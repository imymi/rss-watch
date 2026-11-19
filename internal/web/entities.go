package web

import (
	"context"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"unicode"
	"unicode/utf8"

	"github.com/google/uuid"
	"rss-watch/internal/feed"
	"rss-watch/internal/worker"
)

var entityQueries = map[string]string{
	"sources": "SELECT * FROM sources WHERE deleted=0 ORDER BY id",
	"rules":   "SELECT * FROM rules ORDER BY id",
	"bots":    "SELECT id,name FROM bots WHERE deleted=0 ORDER BY id",
}

func (a *App) entities(ctx context.Context, kind string) ([]row, error) {
	query, ok := entityQueries[kind]
	if !ok {
		return nil, invalid("未知管理类型")
	}
	return rows(ctx, a.db, query)
}

func idValue(value any) (int64, error) {
	n, err := strconv.ParseInt(strings.TrimSpace(asString(value)), 10, 64)
	if err != nil || n < 1 {
		return 0, invalid("记录 ID 无效")
	}
	return n, nil
}

func sourceURL(raw string) bool {
	u, err := url.Parse(raw)
	if err != nil || u.Hostname() == "" || (u.Scheme != "https" && u.Scheme != "http") {
		return false
	}
	port, err := strconv.Atoi(u.Port())
	return u.User == nil && u.Fragment == "" && !strings.ContainsFunc(raw, unicode.IsSpace) &&
		(u.Port() == "" || (err == nil && port >= 0 && port <= 65535))
}

func (a *App) saveEntity(ctx context.Context, kind string, id int64, values row, confirmScope bool) (int64, error) {
	name := strings.TrimSpace(asString(values["name"]))
	if name == "" || utf8.RuneCountInString(name) > 200 {
		return 0, invalid("名称应为 1 到 200 个字符")
	}
	switch kind {
	case "sources":
		return a.saveSource(ctx, id, name, values)
	case "rules":
		return a.saveRule(ctx, id, name, values, confirmScope)
	case "bots":
		return a.saveBot(ctx, id, name, asString(values["webhook"]), asString(values["secret"]))
	}
	return 0, invalid("未知管理类型")
}

func (a *App) saveSource(ctx context.Context, id int64, name string, values row) (int64, error) {
	cfg, err := a.readSettings(ctx)
	if err != nil {
		return 0, err
	}
	interval, err := strconv.ParseInt(asString(cfg["default_interval"]), 10, 64)
	if raw := asString(values["interval"]); raw != "" && raw != "0" {
		interval, err = strconv.ParseInt(strings.TrimSpace(raw), 10, 64)
	}
	if err != nil || interval < 60 {
		return 0, invalid("抓取间隔至少为 60 秒")
	}
	if id != 0 {
		return id, update(ctx, a.db, "订阅源不存在", "UPDATE sources SET name=?,interval=?,revision=revision+1,next_fetch=0 WHERE id=? AND deleted=0", name, interval, id)
	}
	fetchURL := asString(values["url"])
	if utf8.RuneCountInString(fetchURL) > 2048 || !sourceURL(fetchURL) {
		return 0, invalid("订阅地址必须是有效的 HTTP 或 HTTPS URL")
	}
	limit, err := strconv.Atoi(asString(cfg["max_sources"]))
	if err != nil {
		return 0, err
	}
	err = worker.WriteTx(ctx, a.db, func(tx *sql.Tx) error {
		var count int
		if err := tx.QueryRowContext(ctx, "SELECT count(*) FROM sources WHERE deleted=0").Scan(&count); err != nil {
			return err
		}
		if count >= limit {
			return invalid(fmt.Sprintf("最多支持 %d 个订阅源", limit))
		}
		result, err := tx.ExecContext(ctx, "INSERT INTO sources(name,url,interval) VALUES (?,?,?)", name, fetchURL, interval)
		if err == nil {
			id, err = result.LastInsertId()
		}
		return err
	})
	return id, err
}

func words(value any) ([]string, error) {
	values := []any{}
	switch v := value.(type) {
	case nil:
	case []any:
		values = v
	case string:
		for _, word := range strings.FieldsFunc(v, func(r rune) bool { return r == '\n' || r == '\r' || r == '\u2028' || r == '\u2029' }) {
			if strings.TrimSpace(word) != "" {
				values = append(values, word)
			}
		}
	default:
		return nil, invalid("关键词应为字符串数组或逐行文本")
	}
	out, seen := []string{}, map[string]bool{}
	for _, v := range values {
		word, ok := v.(string)
		word = strings.TrimSpace(word)
		if !ok || word == "" {
			return nil, invalid("关键词格式无效")
		}
		key := feed.NormalizeWord(word)
		if !seen[key] {
			out = append(out, word)
			seen[key] = true
		}
	}
	return out, nil
}

func scopeIDs(input any) ([]int64, error) {
	values := []any{}
	switch v := input.(type) {
	case nil:
	case []any:
		values = v
	case string:
		for _, id := range strings.Split(v, ",") {
			if strings.TrimSpace(id) != "" {
				values = append(values, id)
			}
		}
	default:
		return nil, invalid("适用订阅源格式无效")
	}
	ids := []int64{}
	for _, value := range values {
		id, err := idValue(value)
		if err != nil {
			return nil, err
		}
		ids = append(ids, id)
	}
	slices.Sort(ids)
	return slices.Compact(ids), nil
}

func (a *App) saveRule(ctx context.Context, id int64, name string, values row, confirmScope bool) (int64, error) {
	includes, err := words(values["includes"])
	if err != nil || len(includes) == 0 {
		return 0, invalid("至少需要一个有效的包含词")
	}
	excludes, err := words(values["excludes"])
	if err != nil {
		return 0, err
	}
	scope, err := scopeIDs(values["source_ids"])
	if err != nil {
		return 0, err
	}
	mode := asString(values["mode"])
	if mode == "" {
		mode = "any"
	}
	if mode != "any" && mode != "all" {
		return 0, invalid("匹配模式必须是 any 或 all")
	}
	bot, err := idValue(values["bot_id"])
	if err != nil {
		return 0, invalid("请选择机器人")
	}
	err = worker.WriteTx(ctx, a.db, func(tx *sql.Tx) error {
		if err := requireRow(tx.QueryRowContext(ctx, "SELECT id FROM bots WHERE id=? AND deleted=0", bot), "机器人不存在"); err != nil {
			return err
		}
		for _, sourceID := range scope {
			if err := requireRow(tx.QueryRowContext(ctx, "SELECT id FROM sources WHERE id=? AND deleted=0", sourceID), "指定的订阅源不存在"); err != nil {
				return err
			}
		}
		args := []any{name, objectJSON(includes), objectJSON(excludes), mode, objectJSON(scope), bot}
		if id != 0 {
			if !confirmScope && len(scope) == 0 {
				var old string
				if err := tx.QueryRowContext(ctx, "SELECT source_ids FROM rules WHERE id=?", id).Scan(&old); err != nil {
					if err == sql.ErrNoRows {
						return invalid("规则不存在")
					}
					return err
				}
				var ids []int64
				if err := json.Unmarshal([]byte(old), &ids); err != nil {
					return err
				}
				if len(ids) > 0 {
					return invalid("清空来源范围会适用于全部订阅源，请勾选确认后保存")
				}
			}
			return update(ctx, tx, "规则不存在", "UPDATE rules SET name=?,includes=?,excludes=?,mode=?,source_ids=?,bot_id=? WHERE id=?", append(args, id)...)
		}
		result, err := tx.ExecContext(ctx, "INSERT INTO rules(name,includes,excludes,mode,source_ids,bot_id) VALUES (?,?,?,?,?,?)", args...)
		if err == nil {
			id, err = result.LastInsertId()
		}
		return err
	})
	return id, err
}

func requireRow(result *sql.Row, missing string) error {
	var id int64
	if err := result.Scan(&id); err != sql.ErrNoRows {
		return err
	}
	return invalid(missing)
}

func credentialName(name string) bool {
	return filepath.Base(name) == name && strings.HasSuffix(name, ".json") && !strings.ContainsAny(name, "/\\\x00")
}

func (a *App) saveBot(ctx context.Context, id int64, name, webhook, secret string) (int64, error) {
	if id != 0 && webhook == "" {
		if secret != "" {
			return 0, invalid("替换签名密钥时请同时填写 Webhook")
		}
		return id, update(ctx, a.db, "机器人不存在", "UPDATE bots SET name=? WHERE id=? AND deleted=0", name, id)
	}
	if utf8.RuneCountInString(webhook) > 2048 || utf8.RuneCountInString(secret) > 2048 || !worker.ValidWebhook(webhook) {
		return 0, invalid("请使用有效的飞书群自定义机器人 HTTPS webhook")
	}
	lock, err := a.adminLock(ctx, "admin", true)
	if err != nil {
		return 0, err
	}
	defer lock.Close()
	ref, oldRef := hex.EncodeToString(randomBytes(16))+".json", ""
	err = worker.WriteTx(ctx, a.db, func(tx *sql.Tx) error {
		if id != 0 {
			if err := tx.QueryRowContext(ctx, "SELECT credential_ref FROM bots WHERE id=? AND deleted=0", id).Scan(&oldRef); err != nil {
				if err == sql.ErrNoRows {
					return invalid("机器人不存在")
				}
				return err
			}
			if !credentialName(oldRef) {
				return invalid("凭据引用无效")
			}
		}
		if err := writeNew(filepath.Join(a.config.CredentialsDir, ref), []byte(objectJSON(row{"webhook": webhook, "secret": secret}))); err != nil {
			return err
		}
		if err := syncDir(a.config.CredentialsDir); err != nil {
			return err
		}
		if id != 0 {
			_, err := tx.ExecContext(ctx, "UPDATE bots SET name=?,credential_ref=? WHERE id=?", name, ref, id)
			return err
		}
		result, err := tx.ExecContext(ctx, "INSERT INTO bots(name,credential_ref) VALUES (?,?)", name, ref)
		if err == nil {
			id, err = result.LastInsertId()
		}
		return err
	})
	if err != nil {
		_ = a.removeUnusedCredential(context.WithoutCancel(ctx), ref)
		return 0, err
	}
	if oldRef != "" {
		err = a.removeUnusedCredential(ctx, oldRef)
	}
	return id, err
}

func (a *App) removeUnusedCredential(ctx context.Context, ref string) error {
	if !credentialName(ref) {
		return invalid("凭据引用无效")
	}
	var used bool
	if err := a.db.QueryRowContext(ctx, "SELECT EXISTS(SELECT 1 FROM bots WHERE credential_ref=? AND deleted=0)", ref).Scan(&used); err != nil || used {
		return err
	}
	if err := os.Remove(filepath.Join(a.config.CredentialsDir, ref)); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return nil
}

func (a *App) entityAction(ctx context.Context, kind, op string, id int64, title string) (row, error) {
	ok := row{"ok": true}
	if op == "enable" || op == "disable" {
		enabled := op == "enable"
		switch kind {
		case "sources":
			return ok, update(ctx, a.db, "订阅源不存在", "UPDATE sources SET enabled=?,revision=revision+1,next_fetch=0 WHERE id=? AND deleted=0", enabled, id)
		case "rules":
			return ok, update(ctx, a.db, "规则不存在", "UPDATE rules SET enabled=? WHERE id=?", enabled, id)
		}
	}
	if op == "delete" {
		switch kind {
		case "sources":
			return ok, update(ctx, a.db, "订阅源不存在", "UPDATE sources SET deleted=1,enabled=0,revision=revision+1 WHERE id=? AND deleted=0", id)
		case "rules":
			return ok, update(ctx, a.db, "规则不存在", "DELETE FROM rules WHERE id=?", id)
		case "bots":
			return ok, a.deleteBot(ctx, id)
		}
	}
	if op == "test" {
		switch kind {
		case "bots":
			event := uuid.NewString()
			err := update(ctx, a.db, "机器人不存在", `INSERT INTO deliveries(id,bot_id,payload,matches,created,updated)
				SELECT ?,id,?,'[]',?,? FROM bots WHERE id=? AND deleted=0`, event, "RSS 命中提醒\n测试消息\n事件："+event, now(), now(), id)
			return row{"event_id": event}, err
		case "rules":
			var in, ex, mode string
			err := a.db.QueryRowContext(ctx, "SELECT includes,excludes,mode FROM rules WHERE id=?", id).Scan(&in, &ex, &mode)
			if err == sql.ErrNoRows {
				return nil, invalid("规则不存在")
			}
			if err != nil {
				return nil, err
			}
			var includes, excludes []string
			if json.Unmarshal([]byte(in), &includes) != nil || json.Unmarshal([]byte(ex), &excludes) != nil {
				return nil, invalid("规则格式无效")
			}
			hits := feed.MatchWords(feed.FoldTitle(title), mode, includes, excludes)
			return row{"matched": len(hits) > 0, "keywords": hits}, nil
		}
	}
	return nil, invalid("操作无效")
}

func (a *App) deleteBot(ctx context.Context, id int64) error {
	lock, err := a.adminLock(ctx, "admin", true)
	if err != nil {
		return err
	}
	defer lock.Close()
	var ref string
	err = worker.WriteTx(ctx, a.db, func(tx *sql.Tx) error {
		var rules, pending bool
		if err := tx.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM rules WHERE bot_id=?),
			EXISTS(SELECT 1 FROM deliveries WHERE bot_id=? AND state!='sent')`, id, id).Scan(&rules, &pending); err != nil {
			return err
		}
		if rules {
			return invalid("请先删除或改绑引用该机器人的规则")
		}
		if pending {
			return invalid("该机器人还有未完成或失败任务，不能删除")
		}
		if err := tx.QueryRowContext(ctx, "SELECT credential_ref FROM bots WHERE id=? AND deleted=0", id).Scan(&ref); err != nil {
			if err == sql.ErrNoRows {
				return invalid("机器人不存在")
			}
			return err
		}
		if !credentialName(ref) {
			return invalid("凭据引用无效")
		}
		_, err := tx.ExecContext(ctx, "UPDATE bots SET deleted=1 WHERE id=?", id)
		return err
	})
	if err != nil {
		return err
	}
	return a.removeUnusedCredential(ctx, ref)
}
