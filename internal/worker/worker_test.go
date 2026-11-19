package worker

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"rss-watch/internal/feed"
)

const hook = "/open-apis/bot/v2/hook/fixture"
const secret = "test-signing-secret"

func testDatabase(t *testing.T) (Config, *sql.DB) {
	t.Helper()
	dir := t.TempDir()
	config := Config{DataDir: dir, CredentialsDir: filepath.Join(dir, "credentials")}
	if err := os.Mkdir(config.CredentialsDir, 0o700); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, "rss-watch.sqlite3")
	pythonFixture(t,
		"import sys; from pathlib import Path; from rss_watch.db import Database; Database(Path(sys.argv[1]))", path)
	db, err := OpenDatabase(t.Context(), path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	creds := fmt.Sprintf(`{"webhook":"https://open.feishu.cn%s","secret":"%s"}`, hook, secret)
	if err := os.WriteFile(filepath.Join(config.CredentialsDir, "fixture.json"), []byte(creds), 0o600); err != nil {
		t.Fatal(err)
	}
	execute(t, db, "INSERT INTO bots(id,name,credential_ref) VALUES (1,'fixture','fixture.json')")
	return config, db
}

func pythonFixture(t *testing.T, script string, args ...string) {
	t.Helper()
	python := os.Getenv("FEED_PARITY_PYTHON")
	if python == "" {
		python = "python3"
	}
	src, err := filepath.Abs("../../src")
	if err != nil {
		t.Fatal(err)
	}
	cmd := exec.CommandContext(t.Context(), python, append([]string{"-B", "-c", script}, args...)...)
	cmd.Env = append(os.Environ(), "PYTHONPATH="+src)
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("Python fixture: %v\n%s", err, out)
	}
}

func execute(t *testing.T, db *sql.DB, statement string, args ...any) {
	t.Helper()
	if _, err := db.ExecContext(t.Context(), statement, args...); err != nil {
		t.Fatal(err)
	}
}

func scalar[T any](t *testing.T, db *sql.DB, query string, args ...any) T {
	t.Helper()
	var value T
	if err := db.QueryRowContext(t.Context(), query, args...).Scan(&value); err != nil {
		t.Fatal(err)
	}
	return value
}

func until(t *testing.T, description string, condition func() bool) {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for !condition() {
		if time.Now().After(deadline) {
			t.Fatalf("timed out waiting for %s", description)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

type rewriteHook struct {
	target *url.URL
	base   http.RoundTripper
}

func (hook rewriteHook) RoundTrip(req *http.Request) (*http.Response, error) {
	if req.URL.Scheme != "https" || req.URL.Host != "open.feishu.cn" {
		return nil, fmt.Errorf("unexpected Feishu test destination")
	}
	req = req.Clone(req.Context())
	req.URL.Scheme, req.URL.Host = hook.target.Scheme, hook.target.Host
	return hook.base.RoundTrip(req)
}

func localClients(address string) func(networkSettings) (httpClients, error) {
	return func(s networkSettings) (httpClients, error) {
		clients, err := newHTTPClients(s)
		if err != nil {
			return clients, err
		}
		target, err := url.Parse(address)
		if err != nil {
			return httpClients{}, err
		}
		clients.send.Transport = rewriteHook{target: target, base: clients.send.Transport}
		return clients, nil
	}
}

func startWorker(t *testing.T, config Config, address string) func() {
	t.Helper()
	ctx, cancel := context.WithCancel(t.Context())
	done := make(chan struct{})
	var err error
	go func() {
		defer close(done)
		err = run(ctx, config, localClients(address))
	}()
	stop := func() {
		cancel()
		select {
		case <-done:
			if err != nil {
				t.Errorf("worker error: %v", err)
			}
		case <-time.After(7 * time.Second):
			t.Error("worker did not stop")
		}
	}
	t.Cleanup(stop)
	return stop
}

func readHook(t *testing.T, r *http.Request) feishuMessage {
	t.Helper()
	var msg feishuMessage
	if err := json.NewDecoder(r.Body).Decode(&msg); err != nil {
		t.Errorf("read Feishu message: %v", err)
	}
	if r.Method != http.MethodPost || msg.Type != "text" ||
		msg.Timestamp == "" || msg.Sign != signature(msg.Timestamp, secret) {
		t.Errorf("invalid signed Feishu request: %+v", msg)
	}
	return msg
}

const oldEntry = `<item><guid isPermaLink="false">old</guid><title>AI 历史</title></item>`
const pythonEntry = `<item><title>AI 历史数据</title><link>../preexisting-article</link></item>`
const addedEntries = `
  <item><title>ＡＩ 新研究</title><link>../article</link></item>
  <item><title>重复条目</title><link>../article</link></item>
  <item><guid isPermaLink="false">summary-only</guid><title>普通标题</title><description>AI</description></item>
  <item><guid isPermaLink="false">exclude</guid><title>AI 研究 广告</title></item>`

func TestFetchDedupRetryAcrossRestart(t *testing.T) {
	config, db := testDatabase(t)
	var phase, deliveries, unchanged atomic.Int32
	payloads := make(chan string, 8)
	server := httptest.NewServer(http.HandlerFunc(func(out http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/feed":
			http.Redirect(out, r, "/feeds/current.xml", http.StatusFound)
		case "/feeds/current.xml":
			switch phase.Load() {
			case 0:
				out.Header().Set("ETag", `"baseline"`)
				out.Header().Set("Last-Modified", "Thu, 19 Nov 2026 00:00:00 GMT")
				fmt.Fprint(out, "<rss version='2.0'><channel>"+oldEntry+"</channel></rss>")
			case 1:
				if r.Header.Get("If-None-Match") != `"baseline"` || r.Header.Get("If-Modified-Since") == "" {
					t.Error("baseline validators were not sent")
				}
				out.Header().Set("ETag", `"changed"`)
				out.Header().Set("Last-Modified", "Thu, 19 Nov 2026 01:00:00 GMT")
				fmt.Fprint(out, "<rss version='2.0'><channel>"+oldEntry+pythonEntry+addedEntries+"</channel></rss>")
			case 2:
				if r.Header.Get("If-None-Match") != `"changed"` || r.Header.Get("If-Modified-Since") != "Thu, 19 Nov 2026 01:00:00 GMT" {
					t.Error("updated validators were not sent")
				}
				unchanged.Add(1)
				out.WriteHeader(http.StatusNotModified)
			}
		case hook:
			msg := readHook(t, r)
			payloads <- msg.Content["text"]
			if deliveries.Add(1) == 1 {
				out.Header().Set("Retry-After", "3")
				out.WriteHeader(http.StatusTooManyRequests)
				fmt.Fprint(out, `{"msg":"discard this response text"}`)
			} else {
				fmt.Fprint(out, `{"StatusCode":0}`)
			}
		default:
			t.Errorf("unexpected path %q", r.URL.Path)
			out.WriteHeader(http.StatusNotFound)
		}
	}))
	t.Cleanup(server.Close)
	execute(t, db, "INSERT INTO sources(id,name,url,interval) VALUES (1,'临时源',?,60)", server.URL+"/feed")
	execute(t, db, `INSERT INTO rules(id,name,includes,excludes,mode,source_ids,bot_id) VALUES
		(1,'关键字','["AI"]','["广告"]','any','[]',1),
		(2,'研究','["AI","研究"]','["广告"]','all','[1]',1),
		(3,'其他源','["AI"]','[]','any','[99]',1)`)
	execute(t, db, `INSERT INTO settings VALUES ('retry_delays','"1"')`)
	stop := startWorker(t, config, server.URL)
	until(t, "RSS baseline", func() bool { return scalar[int](t, db, "SELECT initialized FROM sources") == 1 })
	if scalar[int](t, db, "SELECT count(*) FROM deliveries") != 0 ||
		scalar[int](t, db, "SELECT count(*) FROM articles") != 1 {
		t.Fatal("baseline should store one fingerprint without sending")
	}
	// Add a match that Python has already seen (including final-URL resolution).
	pythonFixture(t, `
import sys
from pathlib import Path
from rss_watch.db import Database
from rss_watch.domain import parse_feed
article = parse_feed(sys.argv[3].encode(), sys.argv[2])[0]
with Database(Path(sys.argv[1])).transaction() as db:
    db.execute("INSERT INTO articles(source_id,fingerprint,discovered,title,link) VALUES (1,?,0,?,?)",
               (article["fingerprint"], article["title"], article["link"]))
`, filepath.Join(config.DataDir, "rss-watch.sqlite3"), server.URL+"/feeds/current.xml", "<rss version='2.0'><channel>"+pythonEntry+"</channel></rss>")
	phase.Store(1)
	execute(t, db, "UPDATE sources SET next_fetch=0")
	until(t, "persisted 429 retry", func() bool {
		return scalar[int](t, db, "SELECT count(*) FROM deliveries WHERE state='pending' AND attempts=1") == 1
	})
	stop()
	if scalar[string](t, db, "SELECT value FROM runtime WHERE key='running'") != "0" {
		t.Fatal("graceful shutdown did not clear worker heartbeat")
	}
	event := scalar[string](t, db, "SELECT id FROM deliveries")
	if got := scalar[string](t, db, "SELECT last_error FROM deliveries"); got != "飞书 HTTP 429" {
		t.Fatalf("unexpected delivery error: %q", got)
	}
	due := scalar[float64](t, db, "SELECT next_attempt FROM deliveries")
	if delay := scalar[float64](t, db, "SELECT next_attempt-updated FROM deliveries"); delay < 2.99 ||
		scalar[float64](t, db, "SELECT next_send FROM bots") < due {
		t.Fatal("Retry-After did not persist on the delivery and bot")
	}
	if err := db.QueryRow(`SELECT id FROM articles WHERE link=?`, server.URL+"/article").Scan(new(int)); err != nil {
		t.Fatalf("link was not resolved from the final response URL: %v", err)
	}

	phase.Store(2)
	execute(t, db, "UPDATE sources SET next_fetch=0")
	startWorker(t, config, server.URL)
	until(t, "same event sent on retry", func() bool {
		return scalar[int](t, db, "SELECT count(*) FROM deliveries WHERE id=? AND state='sent' AND attempts=2 AND updated>=?", event, due) == 1
	})
	until(t, "conditional fetch after restart", func() bool { return unchanged.Load() > 0 })
	if deliveries.Load() != 2 || scalar[int](t, db, "SELECT count(*) FROM articles") != 5 ||
		scalar[int](t, db, "SELECT count(*) FROM deliveries") != 1 ||
		scalar[string](t, db, "SELECT etag FROM sources") != `"changed"` {
		t.Fatal("duplicate article or changed validators after restart")
	}
	first, second := <-payloads, <-payloads
	if first != second || !strings.Contains(first, "事件："+event) ||
		!strings.Contains(first, "关键字（AI）；研究（AI, 研究）") || !strings.Contains(first, server.URL+"/article") {
		t.Fatalf("unexpected or unstable queued message: %q / %q", first, second)
	}
	pythonFixture(t, `
import json
import sys
from pathlib import Path
from rss_watch.db import Database
rows = Database(Path(sys.argv[1])).rows("SELECT id,state,attempts,matches FROM deliveries")
assert len(rows) == 1
row = rows[0]
assert (row["id"], row["state"], row["attempts"]) == (sys.argv[2], "sent", 2)
assert [r["id"] for r in json.loads(row["matches"])] == [1, 2]
`, filepath.Join(config.DataDir, "rss-watch.sqlite3"), event)
}

func TestFeedCommitRollbackAndSourceRevision(t *testing.T) {
	config, db := testDatabase(t)
	execute(t, db, `INSERT INTO sources(id,name,url,initialized,etag) VALUES (1,'temp','https://example.test',1,'old')`)
	execute(t, db, `INSERT INTO rules(name,includes,excludes,mode,source_ids,bot_id) VALUES ('rule','["AI"]','[]','any','[]',1)`)
	execute(t, db, `CREATE TRIGGER reject_delivery BEFORE INSERT ON deliveries BEGIN SELECT RAISE(ABORT,'fixture'); END`)
	articles, err := feed.Parse([]byte("<rss><channel>"+oldEntry+"</channel></rss>"), "")
	if err != nil {
		t.Fatal(err)
	}
	w := worker{config: config, db: db}
	ctx := t.Context()
	out := fetchResult{articles: articles, headers: http.Header{"Etag": {"new"}}}
	if err := w.commitFeed(ctx, source{id: 1}, out, 100); err == nil {
		t.Fatal("injected queue insertion failure did not propagate")
	}
	if scalar[int](t, db, "SELECT count(*) FROM articles") != 0 ||
		scalar[string](t, db, "SELECT etag FROM sources") != "old" {
		t.Fatal("articles and validators must roll back together")
	}
	execute(t, db, "DROP TRIGGER reject_delivery")
	execute(t, db, "UPDATE sources SET revision=1")
	if err := w.commitFeed(ctx, source{id: 1}, out, 101); err != nil {
		t.Fatal(err)
	}
	if scalar[int](t, db, "SELECT count(*) FROM articles") != 0 {
		t.Fatal("committed an obsolete fetch after editing the source")
	}
	if err := w.commitFeed(ctx, source{id: 1, revision: 1}, out, 102); err != nil {
		t.Fatal(err)
	}
	if scalar[int](t, db, "SELECT count(*) FROM deliveries") != 1 ||
		scalar[string](t, db, "SELECT etag FROM sources") != "new" {
		t.Fatal("could not commit the article and validator after retry")
	}
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (fn roundTripFunc) RoundTrip(request *http.Request) (*http.Response, error) {
	return fn(request)
}

func TestOnlyValidFeedEstablishesBaseline(t *testing.T) {
	config, db := testDatabase(t)
	execute(t, db, "INSERT INTO sources(id,name,url,interval) VALUES (1,'temp','https://example.test/feed',60)")
	var status int
	var body string
	cl := &http.Client{Transport: roundTripFunc(func(req *http.Request) (*http.Response, error) {
		return &http.Response{
			StatusCode: status, Request: req, Header: http.Header{"Etag": {"response"}},
			Body: io.NopCloser(strings.NewReader(body)),
		}, nil
	})}
	w := worker{config: config, db: db, clients: httpClients{fetch: cl}}
	s := settings{networkSettings: networkSettings{RequestDeadline: 1}, MaxBodyMiB: 1, FetchBackoffMax: 3600}
	src := source{id: 1, interval: 60, url: "https://example.test/feed"}
	for _, invalid := range []struct {
		status int
		body   string
	}{
		{http.StatusNotModified, ""},
		{http.StatusOK, "<rss><channel><item>"},
		{http.StatusOK, strings.Repeat("x", (1<<20)+1)},
	} {
		status, body = invalid.status, invalid.body
		if err := w.fetch(t.Context(), src, s); err != nil {
			t.Fatal(err)
		}
		src.failures++
		if scalar[int](t, db, "SELECT count(*) FROM sources WHERE initialized=0 AND etag IS NULL AND failures=?", src.failures) != 1 {
			t.Fatal("an unusable response established a baseline or changed validators")
		}
		delay := float64(src.interval << (src.failures - 1))
		if next := scalar[float64](t, db, "SELECT next_fetch FROM sources") - epoch(time.Now()); next < delay-2 || next > delay+1 {
			t.Fatalf("wrong fetch backoff: %v, expected around %v", next, delay)
		}
	}
	status, body = http.StatusOK, "<rss version='2.0'><channel/></rss>"
	if err := w.fetch(t.Context(), src, s); err != nil {
		t.Fatal(err)
	}
	if scalar[int](t, db, "SELECT count(*) FROM sources WHERE initialized=1 AND etag='response' AND failures=0 AND last_error IS NULL") != 1 ||
		scalar[int](t, db, "SELECT count(*) FROM articles") != 0 {
		t.Fatal("a valid empty feed should establish a clean baseline")
	}
}
