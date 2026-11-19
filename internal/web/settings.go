package web

import (
	"context"
	"database/sql"
	_ "embed"
	"encoding/json"
	"fmt"
	"net"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"
	_ "time/tzdata"
	"unicode"
	"unicode/utf8"

	"rss-watch/internal/worker"
)

//go:embed settings.json
var settingsSpec []byte

type field struct {
	Key     string          `json:"key"`
	Label   string          `json:"label"`
	Default json.RawMessage `json:"default"`
	Minimum int             `json:"minimum"`
	Maximum int             `json:"maximum"`
	Group   string          `json:"group"`
	Help    string          `json:"help"`
}

func (a *App) readSettings(ctx context.Context) (row, error) {
	out := row{}
	for _, field := range a.fields {
		out[field.Key] = field.defaultValue()
	}
	items, err := rows(ctx, a.db, "SELECT key,value FROM settings")
	if err != nil {
		return nil, err
	}
	for _, item := range items {
		key := asString(item["key"])
		if _, ok := out[key]; ok {
			var value any
			if err := json.Unmarshal([]byte(asString(item["value"])), &value); err != nil {
				return nil, err
			}
			out[key] = value
		}
	}
	return out, nil
}

func (f field) defaultValue() any {
	var value any
	_ = json.Unmarshal(f.Default, &value)
	return value
}

var noProxyHost = regexp.MustCompile(`^\.?[A-Za-z0-9][A-Za-z0-9.-]*$`)

func (a *App) validateSettings(values row) (row, error) {
	validated := row{}
	for key, value := range values {
		f, ok := a.fields[key]
		if !ok {
			return nil, invalid("设置包含未知字段")
		}
		switch f.defaultValue().(type) {
		case bool:
			if _, ok := value.(bool); !ok {
				return nil, invalid(f.Label + "必须是布尔值")
			}
		case float64:
			number, ok := value.(json.Number)
			n, err := strconv.Atoi(string(number))
			if !ok || err != nil || n < f.Minimum || n > f.Maximum {
				return nil, invalid(fmt.Sprintf("%s 应在 %d 到 %d 之间", f.Label, f.Minimum, f.Maximum))
			}
			value = n
		case string:
			raw, ok := value.(string)
			if !ok || utf8.RuneCountInString(raw) > 2048 {
				return nil, invalid(f.Label + "格式无效")
			}
			raw = strings.TrimSpace(raw)
			switch key {
			case "http_proxy", "https_proxy":
				if raw != "" {
					proxy, err := url.Parse(raw)
					if err != nil || proxy.Hostname() == "" || proxy.Port() == "0" || proxy.RawQuery != "" || proxy.Fragment != "" ||
						(proxy.Scheme != "http" && proxy.Scheme != "https") || (proxy.Path != "" && proxy.Path != "/") ||
						strings.ContainsFunc(raw, unicode.IsSpace) {
						return nil, invalid("代理地址格式无效")
					}
				}
			case "no_proxy":
				if raw != "" {
					for _, host := range strings.Split(raw, ",") {
						host = strings.TrimSpace(host)
						if host != "*" && net.ParseIP(strings.Trim(host, "[]")) == nil && !noProxyHost.MatchString(host) {
							return nil, invalid("代理排除地址应为逗号分隔的主机名或 IP")
						}
					}
				}
			case "retry_delays":
				parts := strings.Split(raw, ",")
				if len(parts) > 10 {
					return nil, invalid("重试间隔应包含 1 到 10 个值")
				}
				for i, part := range parts {
					n, err := strconv.Atoi(strings.TrimSpace(part))
					if err != nil || n < 1 || n > 86400 {
						return nil, invalid("重试间隔应在 1 到 86400 秒之间")
					}
					parts[i] = strconv.Itoa(n)
				}
				raw = strings.Join(parts, ",")
			case "timezone":
				if raw == "" || raw == "Local" {
					return nil, invalid("时区无效，例如 Asia/Shanghai")
				}
				if _, err := time.LoadLocation(raw); err != nil {
					return nil, invalid("时区无效，例如 Asia/Shanghai")
				}
			}
			value = raw
		}
		validated[key] = value
	}
	return validated, nil
}

func (a *App) saveSettings(ctx context.Context, values row) error {
	values, err := a.validateSettings(values)
	if err != nil {
		return err
	}
	return worker.WriteTx(ctx, a.db, func(tx *sql.Tx) error {
		return setSettings(ctx, tx, values)
	})
}

func setSettings(ctx context.Context, tx *sql.Tx, values row) error {
	for key, value := range values {
		if _, err := tx.ExecContext(ctx, "INSERT OR REPLACE INTO settings VALUES (?,?)", key, objectJSON(value)); err != nil {
			return err
		}
	}
	_, err := tx.ExecContext(ctx, "INSERT OR REPLACE INTO runtime VALUES ('settings_revision',?)", token())
	return err
}
