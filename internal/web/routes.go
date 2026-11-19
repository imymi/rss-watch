package web

import (
	"archive/zip"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"mime"
	"net/http"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
)

func (a *App) routes() {
	a.mux = http.NewServeMux()
	a.publicRoutes()
	a.apiRoutes()
	a.handle("GET /{$}", a.dashboard)
	for _, kind := range []string{"sources", "rules", "bots"} {
		a.handle("GET /"+kind, func(w http.ResponseWriter, r *http.Request) error { return a.entityPage(w, r, kind) })
		a.handle("POST /"+kind+"/save", func(w http.ResponseWriter, r *http.Request) error {
			if err := a.form(w, r, "rss_session"); err != nil {
				return err
			}
			var id int64
			if raw := r.PostForm.Get("id"); raw != "" {
				n, err := idValue(raw)
				if err != nil {
					return err
				}
				id = n
			}
			data := row{}
			for key := range r.PostForm {
				data[key] = r.PostForm.Get(key)
			}
			scopes := []any{}
			for _, scope := range r.PostForm["source_ids"] {
				scopes = append(scopes, scope)
			}
			data["source_ids"] = scopes
			saved, err := a.saveEntity(r.Context(), kind, id, data, r.PostForm.Get("all_sources") == "1")
			if err != nil {
				return err
			}
			return a.finish(w, r, "/"+kind, "已保存", fmt.Sprintf("保存%s #%d", kind, saved))
		})
		a.handle("POST /"+kind+"/{id}/action", func(w http.ResponseWriter, r *http.Request) error {
			if err := a.form(w, r, "rss_session"); err != nil {
				return err
			}
			id, err := idValue(r.PathValue("id"))
			if err != nil {
				return err
			}
			op := r.PostForm.Get("operation")
			data, err := a.entityAction(r.Context(), kind, op, id, r.PostForm.Get("title"))
			if err != nil {
				return err
			}
			notice := "操作完成"
			if op == "test" {
				notice = "测试消息已入队"
				if kind == "rules" {
					notice = "未命中规则"
					if data["matched"].(bool) {
						notice = "命中：" + strings.Join(data["keywords"].([]string), "、")
					}
					redirect(w, "/rules", notice)
					return nil
				}
			}
			return a.finish(w, r, "/"+kind, notice, fmt.Sprintf("%s %s #%d", op, kind, id))
		})
	}
	a.handle("GET /history", a.historyPage)
	a.handle("POST /history/{event}/retry", func(w http.ResponseWriter, r *http.Request) error {
		if err := a.form(w, r, "rss_session"); err != nil {
			return err
		}
		event := r.PathValue("event")
		if err := a.retry(r.Context(), event); err != nil {
			return err
		}
		return a.finish(w, r, "/history", "已重新入队", "重试推送 "+event)
	})
	a.settingsRoutes()
	a.operationsRoutes()
}

func (a *App) finish(w http.ResponseWriter, r *http.Request, target, notice, action string) error {
	if err := a.audit(r.Context(), action); err != nil {
		return err
	}
	redirect(w, target, notice)
	return nil
}

func (a *App) dashboard(w http.ResponseWriter, r *http.Request) error {
	data := row{}
	for _, kind := range []string{"sources", "rules", "bots"} {
		list, err := a.entities(r.Context(), kind)
		if err != nil {
			return err
		}
		data[kind] = list
	}
	status, err := a.status(r.Context())
	if err != nil {
		return err
	}
	data["status"] = status
	return a.page(w, r, 200, "dashboard", data)
}

func (a *App) entityPage(w http.ResponseWriter, r *http.Request, kind string) error {
	list, err := a.entities(r.Context(), kind)
	if err != nil {
		return err
	}
	data := row{"rows": list, "selected": nil}
	if id := r.URL.Query().Get("edit"); id != "" {
		n, err := idValue(id)
		if err != nil {
			return err
		}
		i := slices.IndexFunc(list, func(v row) bool { return v["id"] == n })
		if i < 0 {
			return invalid("记录不存在")
		}
		data["selected"] = list[i]
	}
	for _, name := range []string{"sources", "bots"} {
		if name == kind {
			data[name] = list
			continue
		}
		items, err := a.entities(r.Context(), name)
		if err != nil {
			return err
		}
		data[name] = items
	}
	return a.page(w, r, 200, kind, data)
}

func (a *App) status(ctx context.Context) (row, error) {
	runtime, err := rows(ctx, a.db, "SELECT key,value FROM runtime")
	if err != nil {
		return nil, err
	}
	values := row{}
	for _, r := range runtime {
		values[asString(r["key"])] = r["value"]
	}
	heartbeat, _ := strconv.ParseFloat(asString(values["heartbeat"]), 64)
	status := row{
		"后台运行": values["running"] == "1" && now()-heartbeat < 10, "心跳时间": nil,
		"maintenance": a.maintenance(), "settings_pending": asString(values["settings_revision"]) != asString(values["settings_applied"]),
		"last_backup": values["last_auto_backup"], "backup_error": values["auto_backup_error"],
	}
	if heartbeat > 0 {
		status["心跳时间"] = heartbeat
	}
	for name, query := range map[string]string{
		"队列":     "SELECT state,count(*) AS count FROM deliveries GROUP BY state",
		"源错误":    "SELECT id,name,last_error FROM sources WHERE deleted=0 AND last_error IS NOT NULL",
		"最近推送错误": "SELECT id,last_error FROM deliveries WHERE last_error IS NOT NULL ORDER BY updated DESC LIMIT 5",
	} {
		items, err := rows(ctx, a.db, query)
		if err != nil {
			return nil, err
		}
		status[name] = items
	}
	return status, nil
}

func (a *App) health(ctx context.Context) (row, error) {
	check, err := rows(ctx, a.db, "PRAGMA quick_check")
	ok := err == nil && len(check) == 1 && check[0]["quick_check"] == "ok"
	bots, err := rows(ctx, a.db, "SELECT id,credential_ref FROM bots WHERE deleted=0 ORDER BY id")
	if err != nil {
		return nil, err
	}
	failures := []any{}
	for _, bot := range bots {
		if !a.credentialOK(asString(bot["credential_ref"])) {
			failures = append(failures, bot["id"])
		}
	}
	return row{"database_ok": ok, "credential_errors": failures}, nil
}

func historyFilter(state string, bot int64) (string, []any, error) {
	clauses := []string{}
	args := []any{}
	if state != "" {
		if !slices.Contains([]string{"pending", "sending", "sent", "failed"}, state) {
			return "", nil, invalid("推送状态无效")
		}
		clauses, args = append(clauses, "d.state=?"), append(args, state)
	}
	if bot != 0 {
		clauses, args = append(clauses, "d.bot_id=?"), append(args, bot)
	}
	return where(clauses), args, nil
}

func (a *App) historyPage(w http.ResponseWriter, r *http.Request) error {
	ctx := r.Context()
	q := r.URL.Query()
	state := q.Get("state")
	number, err := integerOption(q.Get("page_number"), 1, 1, 100000)
	if err != nil {
		return err
	}
	var bot int64
	if raw := q.Get("bot_id"); raw != "" {
		bot, err = idValue(raw)
		if err != nil {
			return err
		}
	}
	filter, args, err := historyFilter(state, bot)
	if err != nil {
		return err
	}
	var total int
	if err := a.db.QueryRowContext(ctx, "SELECT count(*) FROM deliveries d"+filter, args...).Scan(&total); err != nil {
		return err
	}
	list, err := rows(ctx, a.db, "SELECT d.*,b.name AS bot_name FROM deliveries d JOIN bots b ON b.id=d.bot_id"+filter+
		" ORDER BY d.created DESC,d.id LIMIT 30 OFFSET ?", append(args, (number-1)*30)...)
	if err != nil {
		return err
	}
	bots, err := a.entities(ctx, "bots")
	if err != nil {
		return err
	}
	return a.page(w, r, 200, "history", row{"rows": list, "total": total, "page_number": number, "state": state, "bot_id": bot, "bots": bots})
}

func (a *App) retry(ctx context.Context, event string) error {
	return update(ctx, a.db, "任务不存在或不是失败状态", `UPDATE deliveries SET state='pending',attempts=0,next_attempt=0,
		updated=?,last_error=NULL WHERE id=? AND state='failed'`, now(), event)
}

func (a *App) settingsRoutes() {
	a.handle("GET /settings", func(w http.ResponseWriter, r *http.Request) error {
		status, err := a.status(r.Context())
		if err != nil {
			return err
		}
		options := row{}
		for key, f := range a.fields {
			options[key] = row{"label": f.Label, "minimum": f.Minimum, "maximum": f.Maximum, "group": f.Group, "help": f.Help}
		}
		return a.page(w, r, 200, "settings", row{"fields": options, "status": status})
	})
	a.handle("POST /settings/save", func(w http.ResponseWriter, r *http.Request) error {
		if err := a.form(w, r, "rss_session"); err != nil {
			return err
		}
		settings := row{}
		for key, f := range a.fields {
			if f.Group == "控制" {
				continue
			}
			raw := asString(f.defaultValue())
			if r.PostForm.Has(key) {
				raw = r.PostForm.Get(key)
			}
			if f.Maximum > 0 {
				settings[key] = json.Number(strings.TrimSpace(raw))
			} else {
				settings[key] = raw
			}
		}
		if err := a.saveSettings(r.Context(), settings); err != nil {
			return err
		}
		return a.finish(w, r, "/settings", "设置已保存，监控服务将自动应用", "修改系统设置")
	})
	a.handle("POST /settings/password", func(w http.ResponseWriter, r *http.Request) error {
		if err := a.form(w, r, "rss_session"); err != nil {
			return err
		}
		password := r.PostForm.Get("password")
		if password != r.PostForm.Get("confirm_password") {
			return invalid("两次输入的新密码不一致")
		}
		if err := a.changePassword(r.Context(), r.PostForm.Get("current"), password); err != nil {
			return err
		}
		a.cookie(w, "rss_session", "", -1)
		redirect(w, "/login", "密码已修改，请重新登录")
		return nil
	})
}

func (a *App) operationsRoutes() {
	a.handle("GET /operations", a.operationsPage)
	a.handle("POST /operations/control", func(w http.ResponseWriter, r *http.Request) error {
		if err := a.form(w, r, "rss_session"); err != nil {
			return err
		}
		key := r.PostForm.Get("key")
		if key != "fetch_paused" && key != "delivery_paused" {
			return invalid("控制选项无效")
		}
		paused := r.PostForm.Get("paused") == "1"
		if err := a.saveSettings(r.Context(), row{key: paused}); err != nil {
			return err
		}
		return a.finish(w, r, "/operations", "状态已保存，在途请求结束后生效", fmt.Sprintf("设置 %s=%t", key, paused))
	})
	a.handle("POST /operations/backup", func(w http.ResponseWriter, r *http.Request) error {
		if err := a.form(w, r, "rss_session"); err != nil {
			return err
		}
		name, err := a.createBackup(r.Context(), "manual")
		if err != nil {
			return err
		}
		return a.finish(w, r, "/operations", "备份已完成，包含机器人凭据", "创建备份 "+name)
	})
	a.handle("GET /operations/backups/{name}/download", a.download)
	a.handle("POST /operations/backups/{name}/delete", func(w http.ResponseWriter, r *http.Request) error {
		if err := a.form(w, r, "rss_session"); err != nil {
			return err
		}
		name := r.PathValue("name")
		if err := a.deleteBackup(r.Context(), name); err != nil {
			return err
		}
		return a.finish(w, r, "/operations", "备份已删除", "删除备份 "+name)
	})
	a.handle("POST /operations/restore", a.restoreUpload)
	a.handle("POST /operations/resume", func(w http.ResponseWriter, r *http.Request) error {
		if err := a.form(w, r, "rss_session"); err != nil {
			return err
		}
		if err := a.resume(r.Context(), session(r)); err != nil {
			return err
		}
		return a.finish(w, r, "/operations", "已退出维护模式，可分别恢复抓取和推送", "退出维护模式")
	})
}

func (a *App) operationsPage(w http.ResponseWriter, r *http.Request) error {
	ctx := r.Context()
	status, err := a.status(ctx)
	if err != nil {
		return err
	}
	health, err := a.health(ctx)
	if err != nil {
		return err
	}
	backups, err := a.listBackups()
	if err != nil {
		return err
	}
	audit, err := rows(ctx, a.db, "SELECT * FROM audit ORDER BY id DESC LIMIT 100")
	if err != nil {
		return err
	}
	logs := []row{}
	for _, name := range []string{"worker.log", "web.log"} {
		path := filepath.Join(a.config.DataDir, "logs", name)
		info, err := os.Lstat(path)
		if err != nil || !info.Mode().IsRegular() {
			continue
		}
		if file, err := os.Open(path); err == nil {
			file.Seek(max(0, info.Size()-32768), io.SeekStart)
			value, err := io.ReadAll(io.LimitReader(file, 32768))
			file.Close()
			if err == nil {
				logs = append(logs, row{"name": name, "content": strings.ToValidUTF8(string(value), "�")})
			}
		}
	}
	return a.page(w, r, 200, "operations", row{"status": status, "health": health, "backups": backups, "audit": audit, "logs": logs})
}

func (a *App) deleteBackup(ctx context.Context, name string) error {
	if state, err := os.ReadFile(filepath.Join(a.config.DataDir, "maintenance")); err == nil {
		var marker struct {
			Rollback string `json:"rollback"`
		}
		if json.Unmarshal(state, &marker) == nil && marker.Rollback == name {
			return invalid("维护模式当前使用此回退备份")
		}
	}
	lock, err := a.adminLock(ctx, "admin", true)
	if err != nil {
		return err
	}
	defer lock.Close()
	dir, err := a.findBackup(name)
	if err != nil {
		return err
	}
	return os.RemoveAll(dir)
}

func (a *App) download(w http.ResponseWriter, r *http.Request) error {
	name := r.PathValue("name")
	tmp, err := os.CreateTemp(a.config.DataDir, ".backup-*.zip")
	if err != nil {
		return err
	}
	defer tmp.Close()
	defer os.Remove(tmp.Name())
	lock, err := a.adminLock(r.Context(), "admin", true)
	if err != nil {
		return err
	}
	dir, err := a.findBackup(name)
	if err == nil {
		err = zipBackup(tmp, dir)
	}
	lock.Close()
	if err != nil {
		return err
	}
	if _, err := tmp.Seek(0, io.SeekStart); err != nil {
		return err
	}
	info, err := tmp.Stat()
	if err != nil {
		return err
	}
	w.Header().Set("Content-Type", "application/zip")
	w.Header().Set("Content-Disposition", mime.FormatMediaType("attachment", map[string]string{"filename": name + ".zip"}))
	http.ServeContent(w, r, name+".zip", info.ModTime(), tmp)
	return nil
}

func (a *App) restoreUpload(w http.ResponseWriter, r *http.Request) error {
	defer func() {
		if r.MultipartForm != nil {
			r.MultipartForm.RemoveAll()
		}
	}()
	if err := a.form(w, r, "rss_session"); err != nil {
		return err
	}
	if r.PostForm.Get("confirmation") != "恢复" {
		return invalid("请输入“恢复”确认覆盖业务数据")
	}
	file, info, err := r.FormFile("archive")
	if err != nil {
		return err
	}
	defer file.Close()
	if info.Size > maxArchive {
		return httpError{413, "上传文件超过 100 MiB 限制"}
	}
	zipReader, err := zip.NewReader(file, info.Size)
	if err != nil {
		return invalid("ZIP 备份无效")
	}
	dir, err := os.MkdirTemp(a.config.DataDir, ".restore-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(dir)
	if err := unpackBackup(zipReader, dir); err != nil {
		return err
	}
	if err := a.restore(r.Context(), dir, session(r)); err != nil {
		return err
	}
	// The restoration commits independently of the incoming HTTP request.
	redirect(w, "/login", "恢复完成，请重新登录，检查数据后退出维护模式；管理员密码保持不变")
	return nil
}
