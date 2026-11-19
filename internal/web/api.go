package web

import (
	"encoding/json"
	"fmt"
	"net/http"
)

func (a *App) apiRoutes() {
	a.handle("GET /api/session/csrf", func(w http.ResponseWriter, r *http.Request) error {
		respond(w, 200, row{"csrf": a.csrf(session(r))})
		return nil
	})
	a.handle("GET /api/{kind}", a.apiRead)
	a.handle("POST /api/{kind}", a.apiSave)
	a.handle("PUT /api/{kind}/{id}", a.apiSave)
	a.handle("POST /api/deliveries/{event}/retry", func(w http.ResponseWriter, r *http.Request) error {
		event := r.PathValue("event")
		if err := a.retry(r.Context(), event); err != nil {
			return err
		}
		if err := a.audit(r.Context(), "通过 API 重试推送 "+event); err != nil {
			return err
		}
		respond(w, 200, row{"ok": true})
		return nil
	})
	a.handle("POST /api/{kind}/{id}/{operation}", a.apiAction)
}

func (a *App) apiRead(w http.ResponseWriter, r *http.Request) error {
	ctx := r.Context()
	var result any
	var err error
	switch kind := r.PathValue("kind"); kind {
	case "status":
		status, err := a.status(ctx)
		if err != nil {
			return err
		}
		cfg, err := a.readSettings(ctx)
		if err != nil {
			return err
		}
		status["timezone"] = cfg["timezone"]
		status["settings"] = row{"fetch_paused": cfg["fetch_paused"], "delivery_paused": cfg["delivery_paused"]}
		result = status
	case "sources", "bots", "rules":
		list, err := a.entities(ctx, kind)
		if err != nil {
			return err
		}
		if kind == "rules" {
			if err := decodeColumns(list, "includes", "excludes", "source_ids"); err != nil {
				return err
			}
		}
		result = list
	case "history":
		limit, err := integerOption(r.URL.Query().Get("limit"), 30, 1, 1000)
		if err != nil {
			return err
		}
		filter, args, err := historyFilter(r.URL.Query().Get("state"), 0)
		if err != nil {
			return err
		}
		list, err := rows(ctx, a.db, "SELECT d.* FROM deliveries d"+filter+" ORDER BY d.created DESC LIMIT ?", append(args, limit)...)
		if err != nil {
			return err
		}
		if err := decodeColumns(list, "matches"); err != nil {
			return err
		}
		result = list
	case "settings":
		result, err = a.readSettings(ctx)
	case "health":
		result, err = a.health(ctx)
	default:
		return httpError{404, "Not Found"}
	}
	if err != nil {
		return err
	}
	respond(w, 200, result)
	return nil
}

func decodeColumns(rows []row, columns ...string) error {
	for _, item := range rows {
		for _, name := range columns {
			raw := json.RawMessage(asString(item[name]))
			if !json.Valid(raw) {
				return invalid("数据库记录格式无效")
			}
			item[name] = raw
		}
	}
	return nil
}

func (a *App) apiSave(w http.ResponseWriter, r *http.Request) error {
	kind, rawID := r.PathValue("kind"), r.PathValue("id")
	ctx := r.Context()
	values, err := readObject(r)
	if err != nil {
		return err
	}
	if kind == "settings" && rawID == "" {
		if err := a.saveSettings(ctx, values); err != nil {
			return err
		}
		if err := a.audit(ctx, "通过 API 修改系统设置"); err != nil {
			return err
		}
		respond(w, 200, row{"ok": true})
		return nil
	}
	var id int64
	if rawID != "" {
		id, err = idValue(rawID)
		if err != nil {
			return err
		}
	}
	saved, err := a.saveEntity(ctx, kind, id, values, true)
	if err != nil {
		return err
	}
	if err := a.audit(ctx, fmt.Sprintf("通过 API 保存%s #%d", kind, saved)); err != nil {
		return err
	}
	respond(w, 200, row{"id": saved})
	return nil
}

func (a *App) apiAction(w http.ResponseWriter, r *http.Request) error {
	kind, op := r.PathValue("kind"), r.PathValue("operation")
	id, err := idValue(r.PathValue("id"))
	if err != nil {
		return err
	}
	title := ""
	if kind == "rules" && op == "test" {
		data, err := readObject(r)
		if err != nil {
			return err
		}
		var ok bool
		title, ok = data["title"].(string)
		if !ok {
			return invalid("规则测试需要 title 字符串")
		}
	}
	result, err := a.entityAction(r.Context(), kind, op, id, title)
	if err != nil {
		return err
	}
	if !(kind == "rules" && op == "test") {
		if err := a.audit(r.Context(), fmt.Sprintf("通过 API %s %s #%d", op, kind, id)); err != nil {
			return err
		}
	}
	respond(w, 200, result)
	return nil
}
