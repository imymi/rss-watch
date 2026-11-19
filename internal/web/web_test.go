package web

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"io"
	"mime/multipart"
	"net"
	"net/http"
	"net/http/cookiejar"
	"net/http/httptest"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"rss-watch/internal/worker"
	"rss-watch/src/rss_watch/ui"
)

type browser struct {
	t    *testing.T
	url  string
	http *http.Client
}

func newBrowser(t *testing.T, app *App) browser {
	t.Helper()
	server := httptest.NewServer(app)
	t.Cleanup(server.Close)
	cookies, err := cookiejar.New(nil)
	if err != nil {
		t.Fatal(err)
	}
	return browser{t: t, url: server.URL, http: &http.Client{Jar: cookies, Timeout: 10 * time.Second, Transport: http.DefaultTransport,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}}
}

func (b browser) request(method, path, contentType string, body io.Reader, csrf string) (int, []byte, http.Header) {
	b.t.Helper()
	r, err := http.NewRequestWithContext(b.t.Context(), method, b.url+path, body)
	if err != nil {
		b.t.Fatal(err)
	}
	if contentType != "" {
		r.Header.Set("Content-Type", contentType)
	}
	if csrf != "" {
		r.Header.Set("X-CSRF-Token", csrf)
	}
	resp, err := b.http.Do(r)
	if err != nil {
		b.t.Fatal(err)
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(resp.Body)
	if err != nil {
		b.t.Fatal(err)
	}
	return resp.StatusCode, data, resp.Header
}

func (b browser) raw(method, path, contentType string, body io.Reader, csrf string, want int) []byte {
	b.t.Helper()
	status, data, _ := b.request(method, path, contentType, body, csrf)
	if status != want {
		b.t.Fatalf("%s %s returned %d, want %d\n%s", method, path, status, want, data)
	}
	return data
}

func (b browser) get(path string) string {
	b.t.Helper()
	return string(b.raw("GET", path, "", nil, "", 200))
}

var csrfField = regexp.MustCompile(`name="csrf" value="([a-f0-9]{64})"`)

func (b browser) pageToken(path string) string {
	b.t.Helper()
	matches := csrfField.FindStringSubmatch(b.get(path))
	if len(matches) != 2 {
		b.t.Fatalf("%s did not render a CSRF field", path)
	}
	return matches[1]
}

func (b browser) postForm(path string, form url.Values, code int) {
	b.t.Helper()
	b.raw("POST", path, "application/x-www-form-urlencoded", strings.NewReader(form.Encode()), "", code)
}

func (b browser) apiToken() string {
	b.t.Helper()
	var token row
	if err := json.Unmarshal([]byte(b.get("/api/session/csrf")), &token); err != nil {
		b.t.Fatal(err)
	}
	return asString(token["csrf"])
}

func (b browser) api(method, path string, values any, csrf string, code int) []byte {
	b.t.Helper()
	return b.raw(method, path, "application/json", strings.NewReader(objectJSON(values)), csrf, code)
}

func (b browser) list(path string) []row {
	b.t.Helper()
	var list []row
	if err := json.Unmarshal([]byte(b.get(path)), &list); err != nil {
		b.t.Fatal(err)
	}
	return list
}

const username = "测试管理员"
const password = "temporary-password-1"
const newerPassword = "temporary-password-2"

func (b browser) signIn(password string, status int) {
	b.t.Helper()
	b.postForm("/login", url.Values{"csrf": {b.pageToken("/login")}, "username": {username}, "password": {password}}, status)
}

func testApp(t *testing.T) *App {
	t.Helper()
	dir := t.TempDir()
	app, err := New(t.Context(), worker.Config{DataDir: dir, CredentialsDir: filepath.Join(dir, "credentials")}, false, "")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { app.Close() })
	return app
}

func waitFor(t *testing.T, description string, ready func() bool) {
	t.Helper()
	deadline := time.Now().Add(9 * time.Second)
	for !ready() {
		if time.Now().After(deadline) {
			t.Fatalf("timed out waiting for %s", description)
		}
		time.Sleep(35 * time.Millisecond)
	}
}

func startTestWorker(t *testing.T, config worker.Config, mock *httptest.Server) {
	t.Helper()
	original := http.DefaultTransport
	transport := original.(*http.Transport).Clone()
	mockAddr := strings.TrimPrefix(mock.URL, "https://")
	transport.DialTLSContext = func(ctx context.Context, network, address string) (net.Conn, error) {
		if address != "open.feishu.cn:443" {
			return nil, fmt.Errorf("unexpected external test request to %s", address)
		}
		tlsConfig := mock.Client().Transport.(*http.Transport).TLSClientConfig.Clone()
		return (&tls.Dialer{Config: tlsConfig}).DialContext(ctx, network, mockAddr)
	}
	http.DefaultTransport = transport
	t.Setenv("HTTPS_PROXY", "")
	t.Setenv("HTTP_PROXY", "")
	t.Setenv("NO_PROXY", "*")
	ctx, stop := context.WithCancel(t.Context())
	done := make(chan error, 1)
	go func() { done <- worker.Run(ctx, config) }()
	t.Cleanup(func() {
		stop()
		select {
		case err := <-done:
			if err != nil {
				t.Errorf("Go Worker failed: %v", err)
			}
		case <-time.After(8 * time.Second):
			t.Error("Go Worker did not stop")
		}
		http.DefaultTransport = original
	})
}

func upload(b browser, csrf string, data []byte, want int) {
	b.t.Helper()
	var buf bytes.Buffer
	mp := multipart.NewWriter(&buf)
	_ = mp.WriteField("csrf", csrf)
	_ = mp.WriteField("confirmation", "恢复")
	file, err := mp.CreateFormFile("archive", "fixture.zip")
	if err != nil {
		b.t.Fatal(err)
	}
	_, _ = file.Write(data)
	if err := mp.Close(); err != nil {
		b.t.Fatal(err)
	}
	b.raw("POST", "/operations/restore", mp.FormDataContentType(), &buf, "", want)
}

func TestWebTemplates(t *testing.T) {
	app := testApp(t)
	rec := httptest.NewRecorder()
	app.ServeHTTP(rec, httptest.NewRequest("GET", "/login", nil))
	if rec.Code != 200 || !strings.Contains(rec.Body.String(), "创建管理员") {
		t.Fatalf("setup page failed: %d %s", rec.Code, rec.Body.String())
	}
}

func python(t *testing.T, script string, args ...string) {
	t.Helper()
	executable := os.Getenv("FEED_PARITY_PYTHON")
	if executable == "" {
		executable = "python3"
	}
	src, err := filepath.Abs("../../src")
	if err != nil {
		t.Fatal(err)
	}
	cmd := exec.CommandContext(t.Context(), executable, append([]string{"-B", "-c", script}, args...)...)
	cmd.Env = append(os.Environ(), "PYTHONPATH="+src)
	if output, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("Python compatibility fixture failed: %v\n%s", err, output)
	}
}

func TestPythonAuthAndBackupCompatibility(t *testing.T) {
	app := testApp(t)
	files := t.TempDir()
	db1, db2 := filepath.Join(files, "python-v1.zip"), filepath.Join(files, "python-v2.zip")
	python(t, `
import json, sqlite3, sys, zipfile
from pathlib import Path
from rss_watch.archives import zip_backup
from rss_watch.auth import Auth
from rss_watch.config import Config
from rss_watch.db import SCHEMA
from rss_watch.service import Service

live = Service(Config(Path(sys.argv[1]), Path(sys.argv[2])))
auth = Auth(live)
auth.setup(auth.bootstrap_path.read_text(), sys.argv[3], sys.argv[4])
fixture = Path(sys.argv[6]).parent
config = Config(fixture / "source-data", fixture / "source-credentials")
config.prepare()
source = Service(config)
source.source_save("Python schema 2 订阅", "https://s2.example.test/feed")
source.bot_save("Python schema 2 机器人", "https://open.feishu.cn/open-apis/bot/v2/hook/python-fixture", "backup-secret")
source.backup(fixture / "source-backup")
zip_backup(fixture / "source-backup", Path(sys.argv[6]))

legacy = fixture / "schema-1.sqlite3"
with sqlite3.connect(legacy) as db:
    db.executescript(SCHEMA.split("CREATE TABLE IF NOT EXISTS settings")[0])
    db.execute("PRAGMA user_version=1")
    db.execute("INSERT INTO sources(name,url,initialized) VALUES (?,?,1)", ("Python schema 1 订阅","https://s1.example.test/feed"))
with zipfile.ZipFile(sys.argv[5], "w") as archive:
    archive.write(legacy, "rss-watch.sqlite3")
    archive.writestr("manifest.json", json.dumps(dict(schema_version=1)))
`, app.config.DataDir, app.config.CredentialsDir, username, password, db1, db2)
	b := newBrowser(t, app)
	b.signIn(password, 303)
	for _, fixture := range []struct {
		path, source string
		botCount     int
	}{
		{db1, "Python schema 1 订阅", 0},
		{db2, "Python schema 2 订阅", 1},
	} {
		data, err := os.ReadFile(fixture.path)
		if err != nil {
			t.Fatal(err)
		}
		upload(b, b.apiToken(), data, 303)
		b.signIn(password, 303)
		list := b.list("/api/sources")
		if len(list) != 1 || list[0]["name"] != fixture.source || len(b.list("/api/bots")) != fixture.botCount {
			t.Fatalf("incorrect restored Python data for %s", fixture.source)
		}
		b.postForm("/operations/resume", url.Values{"csrf": {b.apiToken()}}, 303)
	}
	b.postForm("/operations/backup", url.Values{"csrf": {b.apiToken()}}, 303)
	links, err := app.listBackups()
	if err != nil {
		t.Fatal(err)
	}
	var name string
	for _, backup := range links {
		if s := asString(backup["name"]); strings.HasPrefix(s, "manual-") {
			name = s
			break
		}
	}
	if name == "" {
		t.Fatal("no Go backup")
	}
	download := b.raw("GET", "/operations/backups/"+name+"/download", "", nil, "", 200)
	file := filepath.Join(files, "from-go.zip")
	if err := os.WriteFile(file, download, 0o600); err != nil {
		t.Fatal(err)
	}
	python(t, `
import sys
from pathlib import Path
from rss_watch.archives import unpack_archive, verify_directory
from rss_watch.config import Config
from rss_watch.db import Database

archive = Path(sys.argv[1])
with unpack_archive(Config(archive.parent, archive.parent), archive) as directory:
    assert verify_directory(directory) == 2
    db = Database(directory / "rss-watch.sqlite3")
    assert db.rows("SELECT name FROM sources")[0]["name"] == "Python schema 2 订阅"
    assert db.rows("SELECT name FROM bots")[0]["name"] == "Python schema 2 机器人"
`, file)
}

func TestWebAndWorkerEndToEnd(t *testing.T) {
	app := testApp(t)
	b := newBrowser(t, app)
	b.raw("GET", "/api/sources", "", nil, "", 401)
	data := b.get("/static/app.js")
	want, err := ui.Assets.ReadFile("static/app.js")
	if err != nil || data != string(want) {
		t.Fatal("Web did not serve the original app.js")
	}
	bootstrap, err := os.ReadFile(app.bootstrapPath())
	if err != nil {
		t.Fatal(err)
	}
	b.postForm("/setup", url.Values{"csrf": {b.pageToken("/setup")}, "username": {username}, "password": {password}, "token": {string(bootstrap)}}, 303)
	b.signIn(password, 303)
	if _, err := os.Stat(app.bootstrapPath()); !os.IsNotExist(err) {
		t.Fatal("setup token was not removed")
	}
	csrf := b.apiToken()
	b.api("POST", "/api/sources", row{}, "", 403)
	if !strings.Contains(b.get("/"), username) {
		t.Fatal("dashboard missing logged-in user")
	}

	var phase, notifications atomic.Int32
	source := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `<rss version="2.0"><channel><item><guid isPermaLink="false">old</guid><title>AI 历史</title></item>`)
		if phase.Load() >= 1 {
			fmt.Fprint(w, `<item><guid isPermaLink="false">new</guid><title>ＡＩ 新文章</title></item>`)
		}
		if phase.Load() >= 2 {
			fmt.Fprint(w, `<item><guid isPermaLink="false">restored</guid><title>AI 恢复后</title></item>`)
		}
		fmt.Fprint(w, "</channel></rss>")
	}))
	t.Cleanup(source.Close)
	const webhook = "https://open.feishu.cn/open-apis/bot/v2/hook/only-local-fixture"
	const secret = "local-feishu-test-secret"
	mock := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var data struct {
			Content map[string]string
			Sign    string
		}
		if err := json.NewDecoder(r.Body).Decode(&data); err != nil || data.Sign == "" || !strings.Contains(data.Content["text"], "AI ") || r.URL.Path != "/open-apis/bot/v2/hook/only-local-fixture" {
			t.Errorf("invalid simulated Feishu request: %s (%v)", r.URL.Path, err)
		}
		if notifications.Add(1) == 1 {
			fmt.Fprint(w, `{"code":9499}`)
		} else {
			fmt.Fprint(w, `{"code":0}`)
		}
	}))
	t.Cleanup(mock.Close)

	b.api("POST", "/api/bots", row{"name": "模拟机器人", "webhook": webhook, "secret": secret}, csrf, 200)
	if raw := b.get("/api/bots"); strings.Contains(raw, secret) || strings.Contains(raw, "credential_ref") {
		t.Fatal("bot list leaked a credential")
	}
	b.postForm("/sources/save", url.Values{"csrf": {b.pageToken("/sources")}, "name": {"临时订阅"}, "url": {source.URL}, "interval": {"60"}}, 303)
	b.api("POST", "/api/rules", row{"name": "AI 主题", "includes": []string{"AI"}, "source_ids": []int{1}, "bot_id": 1}, csrf, 200)
	for page, expected := range map[string]string{
		"/sources?edit=1": "临时订阅", "/rules?edit=1": "AI 主题", "/bots?edit=1": "模拟机器人",
		"/settings": "fetch_concurrency", "/operations": "校验并恢复",
	} {
		if body := b.get(page); !strings.Contains(body, expected) || strings.Contains(body, secret) {
			t.Fatalf("%s did not render its expected content", page)
		}
	}
	if html := b.get("/rules?edit=1"); !strings.Contains(html, `value="1" checked`) {
		t.Fatal("rule page did not preserve the selected source")
	}
	b.api("POST", "/api/sources/1/disable", nil, csrf, 200)
	b.api("POST", "/api/sources/1/enable", nil, csrf, 200)
	b.api("PUT", "/api/sources/1", row{"name": "<script>alert(1)</script>", "interval": 60}, csrf, 200)
	if page := b.get("/sources?edit=1"); strings.Contains(page, "<script>alert(1)</script>") || !strings.Contains(page, "&lt;script&gt;") {
		t.Fatal("source name was not escaped in the template")
	}
	b.api("PUT", "/api/sources/1", row{"name": "临时订阅", "interval": 60}, csrf, 200)
	b.api("POST", "/api/settings", row{"no_proxy": "*"}, csrf, 200)
	startTestWorker(t, app.config, mock)
	waitFor(t, "baseline", func() bool { return b.list("/api/sources")[0]["initialized"] == float64(1) })
	if notifications.Load() != 0 {
		t.Fatal("baseline sent a notification")
	}
	phase.Store(1)
	b.api("PUT", "/api/sources/1", row{"name": "临时订阅", "interval": 60, "url": "https://ignored.example.test"}, csrf, 200)
	waitFor(t, "failed event", func() bool { return len(b.list("/api/history?state=failed")) == 1 })
	event := asString(b.list("/api/history?state=failed")[0]["id"])
	if history := b.get("/history?state=failed&bot_id=1"); !strings.Contains(history, event) || !strings.Contains(history, "重新入队") {
		t.Fatal("history page missing failed delivery and retry action")
	}
	b.api("POST", "/api/deliveries/"+event+"/retry", nil, csrf, 200)
	waitFor(t, "retried notification", func() bool {
		events := b.list("/api/history?state=sent")
		return len(events) == 1 && events[0]["id"] == event
	})
	if notifications.Load() != 2 {
		t.Fatal("unexpected Feishu request count")
	}

	b.postForm("/operations/backup", url.Values{"csrf": {csrf}}, 303)
	regex := regexp.MustCompile(`href="/operations/backups/([^/]+)/download"`)
	match := regex.FindStringSubmatch(b.get("/operations"))
	if len(match) == 0 {
		t.Fatal("could not find backup download link")
	}
	archive := b.raw("GET", "/operations/backups/"+match[1]+"/download", "", nil, "", 200)
	entries, err := zip.NewReader(bytes.NewReader(archive), int64(len(archive)))
	if err != nil || len(entries.File) != 3 || entries.File[0].Name != dbName {
		t.Fatalf("unexpected backup ZIP: %v", err)
	}
	upload(b, csrf, []byte("invalid backup"), 400)
	if app.maintenance() {
		t.Fatal("invalid backup entered maintenance")
	}
	b.api("PUT", "/api/sources/1", row{"name": "备份之后的名称", "interval": 60}, csrf, 200)
	b.api("PUT", "/api/bots/1", row{"name": "备份之后的机器人", "webhook": webhook, "secret": "rotated"}, csrf, 200)
	b.postForm("/settings/password", url.Values{"csrf": {csrf}, "current": {password}, "password": {newerPassword}, "confirm_password": {newerPassword}}, 303)
	b.signIn(newerPassword, 303)
	csrf = b.apiToken()
	upload(b, csrf, archive, 303)
	b.raw("GET", "/api/history", "", nil, "", 401)
	b.signIn(password, 400)
	b.signIn(newerPassword, 303)
	csrf = b.apiToken()
	if !app.maintenance() || b.list("/api/sources")[0]["name"] != "临时订阅" ||
		b.list("/api/history?state=sent")[0]["id"] != event || b.list("/api/bots")[0]["name"] != "模拟机器人" {
		t.Fatal("backup did not restore source, bot, and delivery in maintenance")
	}
	var settings row
	if err := json.Unmarshal([]byte(b.get("/api/settings")), &settings); err != nil || settings["fetch_paused"] != true || settings["delivery_paused"] != true {
		t.Fatal("restore did not pause fetch and delivery")
	}
	b.api("PUT", "/api/sources/1", row{"name": "blocked"}, csrf, 409)
	b.postForm("/operations/resume", url.Values{"csrf": {csrf}}, 303)
	phase.Store(2)
	b.api("POST", "/api/settings", row{"fetch_paused": false, "delivery_paused": false}, csrf, 200)
	b.api("PUT", "/api/sources/1", row{"name": "临时订阅", "interval": 60}, csrf, 200)
	waitFor(t, "worker resume after restoring", func() bool { return len(b.list("/api/history?state=sent")) == 2 })
	if notifications.Load() != 3 || app.maintenance() {
		t.Fatal("restored worker either redelivered the old article or failed to resume")
	}
	b.api("POST", "/api/sources/1/delete", nil, csrf, 200)
	if len(b.list("/api/sources")) != 0 || len(b.list("/api/history?state=sent")) != 2 {
		t.Fatal("deleting a source must preserve push history")
	}
}
