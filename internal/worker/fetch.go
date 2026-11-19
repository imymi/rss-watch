package worker

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"slices"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/google/uuid"
	"rss-watch/internal/feed"
)

type source struct {
	id, revision, interval, failures int64
	url                              string
	initialized                      bool
	etag, modified                   sql.NullString
}

func (w *worker) dueSources(ctx context.Context, now time.Time) ([]source, error) {
	rows, err := w.db.QueryContext(ctx, `SELECT id,revision,interval,failures,url,initialized,etag,modified
		FROM sources WHERE enabled=1 AND deleted=0 AND next_fetch<=? ORDER BY next_fetch,id`, epoch(now))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var sources []source
	for rows.Next() {
		var s source
		if err := rows.Scan(&s.id, &s.revision, &s.interval, &s.failures, &s.url, &s.initialized, &s.etag, &s.modified); err != nil {
			return nil, err
		}
		sources = append(sources, s)
	}
	return sources, rows.Err()
}

func (w *worker) fetch(ctx context.Context, src source, s settings) error {
	result, reason := fetchFeed(ctx, w.clients.fetch, src, s)
	if ctx.Err() != nil {
		return ctx.Err()
	}
	now := epoch(time.Now())
	if reason != "" {
		delay := min(int64(s.FetchBackoffMax), src.interval*(1<<max(0, min(src.failures, 10))))
		_, err := w.db.ExecContext(ctx, `UPDATE sources SET failures=failures+1,last_error=?,next_fetch=?
			WHERE id=? AND revision=? AND deleted=0 AND enabled=1`, reason, now+float64(delay), src.id, src.revision)
		log.Printf("源 %d：%s", src.id, reason)
		return err
	}
	return w.commitFeed(ctx, src, result, now)
}

type fetchResult struct {
	articles    []feed.Article
	headers     http.Header
	notModified bool
}

func fetchFeed(ctx context.Context, client *http.Client, src source, s settings) (fetchResult, string) {
	const networkError = "抓取网络连接失败或超时"
	const parseError = "订阅解析失败或响应超限"
	ctx, cancel := context.WithTimeout(ctx, time.Duration(s.RequestDeadline)*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, src.url, nil)
	if err != nil {
		return fetchResult{}, networkError
	}
	req.Header.Set("User-Agent", "rss-watch/0.2 (+RSS keyword monitor)")
	req.Header.Set("Accept", "application/atom+xml, application/rss+xml, application/xml, text/xml")
	if src.initialized {
		if src.etag.String != "" {
			req.Header.Set("If-None-Match", src.etag.String)
		}
		if src.modified.String != "" {
			req.Header.Set("If-Modified-Since", src.modified.String)
		}
	}
	resp, err := client.Do(req)
	if err != nil {
		return fetchResult{}, networkError
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusNotModified {
		if !src.initialized {
			return fetchResult{}, parseError
		}
		return fetchResult{headers: resp.Header, notModified: true}, ""
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fetchResult{}, fmt.Sprintf("抓取 HTTP %d", resp.StatusCode)
	}
	limit := int64(s.MaxBodyMiB) << 20
	body, err := io.ReadAll(io.LimitReader(resp.Body, limit+1))
	if err != nil {
		return fetchResult{}, networkError
	}
	if int64(len(body)) > limit {
		return fetchResult{}, parseError
	}
	articles, err := feed.Parse(body, resp.Request.URL.String())
	if ctx.Err() != nil {
		return fetchResult{}, networkError
	}
	if err != nil {
		return fetchResult{}, parseError
	}
	return fetchResult{articles: articles, headers: resp.Header}, ""
}

func (w *worker) commitFeed(ctx context.Context, src source, result fetchResult, now float64) error {
	return WriteTx(ctx, w.db, func(tx *sql.Tx) error {
		var name string
		current := source{id: src.id}
		err := tx.QueryRowContext(ctx, `SELECT name,initialized,interval,etag,modified FROM sources
			WHERE id=? AND revision=? AND deleted=0 AND enabled=1`, src.id, src.revision).
			Scan(&name, &current.initialized, &current.interval, &current.etag, &current.modified)
		if err == sql.ErrNoRows {
			return nil // source was edited or disabled while the request was in flight
		}
		if err != nil {
			return err
		}
		var rules []rule
		if current.initialized && len(result.articles) > 0 {
			rules, err = readRules(ctx, tx)
			if err != nil {
				return err
			}
		}
		for _, article := range result.articles {
			var articleID int64
			err := tx.QueryRowContext(ctx, `INSERT INTO articles(source_id,fingerprint,discovered,title,summary,link,published)
				VALUES (?,?,?,?,?,?,?) ON CONFLICT(source_id,fingerprint) DO NOTHING RETURNING id`,
				src.id, article.Fingerprint, now, article.Title, article.Summary, article.Link, article.Published).Scan(&articleID)
			if err == sql.ErrNoRows {
				continue
			}
			if err != nil {
				return err
			}
			for bot, hits := range matchesByBot(rules, src.id, article.Title) {
				id, err := uuid.NewRandom()
				if err != nil {
					return err
				}
				matches, err := json.Marshal(hits)
				if err != nil {
					return err
				}
				event := id.String()
				if _, err := tx.ExecContext(ctx, `INSERT INTO deliveries(id,article_id,bot_id,payload,matches,created,updated)
					VALUES (?,?,?,?,?,?,?)`, event, articleID, bot, message(event, name, article, hits), string(matches), now, now); err != nil {
					return err
				}
			}
		}
		_, err = tx.ExecContext(ctx, `UPDATE sources SET initialized=1,etag=?,modified=?,last_success=?,
			next_fetch=?,failures=0,last_error=NULL WHERE id=?`,
			validator(result.headers, "ETag", current.etag, result.notModified),
			validator(result.headers, "Last-Modified", current.modified, result.notModified),
			now, now+float64(current.interval), src.id)
		return err
	})
}

func validator(headers http.Header, key string, prior sql.NullString, keep bool) sql.NullString {
	if values := headers.Values(key); len(values) > 0 {
		return sql.NullString{String: strings.Join(values, ", "), Valid: true}
	}
	if keep {
		return prior
	}
	return sql.NullString{}
}

type rule struct {
	id, botID          int64
	name, mode         string
	includes, excludes []string
	sourceIDs          []int64
}

type match struct {
	ID       int64    `json:"id"`
	Name     string   `json:"name"`
	Keywords []string `json:"keywords"`
}

func readRules(ctx context.Context, tx *sql.Tx) ([]rule, error) {
	rows, err := tx.QueryContext(ctx, `SELECT r.id,r.bot_id,r.name,r.mode,r.includes,r.excludes,r.source_ids
		FROM rules r JOIN bots b ON b.id=r.bot_id WHERE r.enabled=1 AND b.deleted=0 ORDER BY r.id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var rules []rule
	for rows.Next() {
		var r rule
		var includes, excludes, scope string
		if err := rows.Scan(&r.id, &r.botID, &r.name, &r.mode, &includes, &excludes, &scope); err != nil {
			return nil, err
		}
		if json.Unmarshal([]byte(includes), &r.includes) != nil ||
			json.Unmarshal([]byte(excludes), &r.excludes) != nil ||
			json.Unmarshal([]byte(scope), &r.sourceIDs) != nil {
			return nil, fmt.Errorf("规则 %d 格式无效", r.id)
		}
		rules = append(rules, r)
	}
	return rules, rows.Err()
}

func matchesByBot(rules []rule, source int64, title string) map[int64][]match {
	if len(rules) == 0 {
		return nil
	}
	title = feed.FoldTitle(title)
	matches := make(map[int64][]match)
	for _, r := range rules {
		if len(r.sourceIDs) > 0 && !slices.Contains(r.sourceIDs, source) {
			continue
		}
		hits := feed.MatchWords(title, r.mode, r.includes, r.excludes)
		if len(hits) > 0 {
			matches[r.botID] = append(matches[r.botID], match{r.id, r.name, hits})
		}
	}
	return matches
}

func message(event, source string, article feed.Article, matches []match) string {
	rules := make([]string, 0, len(matches))
	for _, hit := range matches {
		rules = append(rules, hit.Name+"（"+strings.Join(hit.Keywords, ", ")+"）")
	}
	published := article.Published
	if published == "" {
		published = "未提供"
	}
	summary := article.Summary
	runes := 0
	for i := range summary {
		if runes == 300 {
			summary = summary[:i]
			break
		}
		runes++
	}
	return fmt.Sprintf("RSS 命中提醒\n事件：%s\n标题：%s\n来源：%s\n规则及关键词：%s\n发布时间：%s\n摘要：%s\n原文：%s",
		event, bounded(article.Title, 1500), bounded(source, 400), bounded(strings.Join(rules, "；"), 2500),
		bounded(published, 200), summary, bounded(article.Link, 2048))
}

func bounded(value string, size int) string {
	if len(value) <= size {
		return value
	}
	end := size - len("…")
	for !utf8.RuneStart(value[end]) {
		end--
	}
	return value[:end] + "…"
}
