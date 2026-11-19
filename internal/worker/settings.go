package worker

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
)

type networkSettings struct {
	ConnectTimeout  int    `json:"connect_timeout"`
	RequestDeadline int    `json:"request_deadline"`
	HTTPProxy       string `json:"http_proxy"`
	HTTPSProxy      string `json:"https_proxy"`
	NoProxy         string `json:"no_proxy"`
}

type settings struct {
	networkSettings
	FetchConcurrency int    `json:"fetch_concurrency"`
	MaxBodyMiB       int    `json:"max_body_mib"`
	MaxAttempts      int    `json:"max_attempts"`
	RetryDelays      string `json:"retry_delays"`
	SendInterval     int    `json:"send_interval"`
	FetchBackoffMax  int    `json:"fetch_backoff_max"`
	RetentionDays    int    `json:"retention_days"`
	CleanupHours     int    `json:"cleanup_hours"`
	FetchPaused      bool   `json:"fetch_paused"`
	DeliveryPaused   bool   `json:"delivery_paused"`
	revision         string
	delays           []float64
}

func readSettings(ctx context.Context, db *sql.DB) (settings, error) {
	s := settings{
		networkSettings:  networkSettings{ConnectTimeout: 10, RequestDeadline: 30},
		FetchConcurrency: 5, MaxBodyMiB: 5, MaxAttempts: 5, RetryDelays: "30,120,600,1800",
		SendInterval: 1, FetchBackoffMax: 3600, RetentionDays: 90, CleanupHours: 24,
	}
	// A single read statement gives the settings and their revision the same snapshot.
	rows, err := db.QueryContext(ctx, `SELECT key,value FROM settings WHERE key IN (
		'connect_timeout','request_deadline','http_proxy','https_proxy','no_proxy',
		'fetch_concurrency','max_body_mib','max_attempts','retry_delays','send_interval',
		'fetch_backoff_max','retention_days','cleanup_hours','fetch_paused','delivery_paused')
		UNION ALL SELECT '@revision',value FROM runtime WHERE key='settings_revision'`)
	if err != nil {
		return s, err
	}
	defer rows.Close()
	values := make(map[string]json.RawMessage)
	for rows.Next() {
		var key, value string
		if err := rows.Scan(&key, &value); err != nil {
			return s, err
		}
		if key == "@revision" {
			s.revision = value
		} else {
			values[key] = json.RawMessage(value)
		}
	}
	if err := rows.Err(); err != nil {
		return s, err
	}
	config, err := json.Marshal(values)
	if err != nil || json.Unmarshal(config, &s) != nil {
		return s, fmt.Errorf("Worker 设置格式无效")
	}
	for _, field := range []struct {
		name          string
		val, min, max int
	}{
		{"connect_timeout", s.ConnectTimeout, 1, 120}, {"request_deadline", s.RequestDeadline, 1, 300},
		{"fetch_concurrency", s.FetchConcurrency, 1, 20}, {"max_body_mib", s.MaxBodyMiB, 1, 50},
		{"max_attempts", s.MaxAttempts, 1, 10}, {"send_interval", s.SendInterval, 1, 60},
		{"fetch_backoff_max", s.FetchBackoffMax, 60, 86400}, {"retention_days", s.RetentionDays, 1, 3650},
		{"cleanup_hours", s.CleanupHours, 1, 168},
	} {
		if field.val < field.min || field.val > field.max {
			return s, fmt.Errorf("Worker 设置 %s 超出范围", field.name)
		}
	}
	parts := strings.Split(s.RetryDelays, ",")
	if len(parts) > 10 {
		return s, fmt.Errorf("重试间隔无效")
	}
	for _, part := range parts {
		seconds, err := strconv.Atoi(strings.TrimSpace(part))
		if err != nil || seconds < 1 || seconds > 86400 {
			return s, fmt.Errorf("重试间隔无效")
		}
		s.delays = append(s.delays, float64(seconds))
	}
	return s, nil
}

func (s settings) retryDelay(attempt int) float64 {
	return s.delays[min(attempt-1, len(s.delays)-1)]
}
