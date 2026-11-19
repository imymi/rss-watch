// Package web serves the existing RSS Watch UI and JSON administration API.
package web

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/nikolalohinski/gonja/v2"
	templateConfig "github.com/nikolalohinski/gonja/v2/config"
	"github.com/nikolalohinski/gonja/v2/exec"
	"github.com/nikolalohinski/gonja/v2/loaders"
	"modernc.org/sqlite"
	"rss-watch/internal/worker"
	"rss-watch/src/rss_watch/ui"
)

type App struct {
	db            *sql.DB
	config        worker.Config
	secureCookies bool
	trusted       []*net.IPNet
	key           []byte
	fields        map[string]field
	schemaObjects map[int][]databaseObject
	loginPage     *exec.Template
	mainPage      *exec.Template
	mux           *http.ServeMux
}

// New initializes a separate Go web process against the supplied data paths.
func New(ctx context.Context, config worker.Config, secureCookies bool, trustedProxies string) (*App, error) {
	a := &App{
		config: config, secureCookies: secureCookies,
		fields: map[string]field{}, schemaObjects: map[int][]databaseObject{},
	}
	for _, proxy := range strings.Split(trustedProxies, ",") {
		proxy = strings.TrimSpace(proxy)
		if proxy == "" {
			continue
		}
		if ip := net.ParseIP(proxy); ip != nil {
			if ip.To4() != nil {
				proxy += "/32"
			} else {
				proxy += "/128"
			}
		}
		_, mask, err := net.ParseCIDR(proxy)
		if err != nil {
			return nil, invalid("可信代理地址无效")
		}
		a.trusted = append(a.trusted, mask)
	}
	var fields []field
	if err := json.Unmarshal(settingsSpec, &fields); err != nil {
		return nil, err
	}
	for _, f := range fields {
		a.fields[f.Key] = f
	}
	if err := a.initTemplates(); err != nil {
		return nil, err
	}
	reference, err := sql.Open("sqlite", ":memory:")
	if err != nil {
		return nil, err
	}
	defer reference.Close()
	reference.SetMaxOpenConns(1)
	if _, err := reference.ExecContext(ctx, schema); err != nil {
		return nil, err
	}
	a.schemaObjects[2], err = dbObjects(ctx, reference)
	if err != nil {
		return nil, err
	}
	for _, obj := range a.schemaObjects[2] {
		switch obj.name {
		case "admins", "sessions", "audit", "settings":
		default:
			a.schemaObjects[1] = append(a.schemaObjects[1], obj)
		}
	}
	a.db, err = initialize(ctx, config)
	if err != nil {
		return nil, err
	}
	if err := a.initAuth(ctx); err != nil {
		a.Close()
		return nil, err
	}
	a.routes()
	return a, nil
}

func (a *App) Close() error { return a.db.Close() }

func (a *App) initTemplates() error {
	templates := map[string]string{}
	for _, name := range []string{"page.html", "login.html"} {
		source, err := ui.Assets.ReadFile(name)
		if err != nil {
			return err
		}
		templates["/"+name] = string(source)
	}
	loader, err := loaders.NewMemoryLoader(templates)
	if err != nil {
		return err
	}
	cfg := templateConfig.New()
	cfg.AutoEscape = true
	env := *gonja.DefaultEnvironment
	env.Filters = exec.NewFilterSet(map[string]exec.FilterFunction{}).Update(gonja.DefaultEnvironment.Filters)
	for name, filter := range map[string]exec.FilterFunction{
		"unjson": func(_ *exec.Evaluator, in *exec.Value, _ *exec.VarArgs) *exec.Value {
			var data any
			err := json.Unmarshal([]byte(in.String()), &data)
			if err != nil {
				return exec.AsValue(err)
			}
			// The template compares decoded ID lists to SQLite integer IDs.
			if list, ok := data.([]any); ok {
				for i, item := range list {
					if n, ok := item.(float64); ok {
						list[i] = int64(n)
					}
				}
			}
			return exec.AsValue(data)
		},
		"localtime": func(_ *exec.Evaluator, in *exec.Value, args *exec.VarArgs) *exec.Value {
			if in.IsNil() || in.Float() == 0 {
				return exec.AsValue("—")
			}
			zone := "Asia/Shanghai"
			if len(args.Args) == 1 {
				zone = args.Args[0].String()
			}
			loc, err := time.LoadLocation(zone)
			if err != nil {
				loc = time.UTC
			}
			return exec.AsValue(time.UnixMicro(int64(in.Float() * 1e6)).In(loc).Format("2006-01-02 15:04:05"))
		},
	} {
		if err := env.Filters.Register(name, filter); err != nil {
			return err
		}
	}
	a.mainPage, err = exec.NewTemplate("/page.html", cfg, loader, &env)
	if err != nil {
		return err
	}
	a.loginPage, err = exec.NewTemplate("/login.html", cfg, loader, &env)
	return err
}

type httpError struct {
	code int
	text string
}

func (e httpError) Error() string { return e.text }

func respond(w http.ResponseWriter, code int, value any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(value)
}

func cookieValue(r *http.Request, name string) string {
	cookie, err := r.Cookie(name)
	if err != nil {
		return ""
	}
	return cookie.Value
}

func session(r *http.Request) string { return cookieValue(r, "rss_session") }

func (a *App) cookie(w http.ResponseWriter, name, value string, age int) {
	http.SetCookie(w, &http.Cookie{
		Name: name, Value: value, Path: "/", HttpOnly: true, Secure: a.secureCookies,
		SameSite: http.SameSiteStrictMode, MaxAge: age,
	})
}

func redirect(w http.ResponseWriter, path, notice string) {
	if notice != "" {
		path += "?" + url.Values{"notice": {notice}}.Encode()
	}
	w.Header().Set("Location", path)
	w.WriteHeader(http.StatusSeeOther)
}

func (a *App) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	for name, value := range map[string]string{
		"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "same-origin",
		"Content-Security-Policy": "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
	} {
		w.Header().Set(name, value)
	}
	path := r.URL.Path
	if strings.HasPrefix(path, "/static/") || path == "/favicon.ico" || path == "/healthz" {
		a.mux.ServeHTTP(w, r)
		return
	}
	maximum := int64(64 << 10)
	exclusive := r.Method == http.MethodPost && (path == "/operations/restore" || path == "/operations/resume")
	if path == "/operations/restore" {
		maximum = maxArchive + 1<<20
	}
	if r.ContentLength > maximum {
		a.renderError(w, r, httpError{413, "请求或上传文件过大"})
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, maximum)
	gate, err := a.gate(r.Context(), false)
	if err != nil {
		a.renderError(w, r, httpError{503, "正在等待维护操作完成，请稍后重试"})
		return
	}
	defer gate.Close()
	if path != "/login" && path != "/setup" {
		loggedIn, err := a.authenticated(r.Context(), session(r))
		if err != nil {
			a.renderError(w, r, err)
			return
		}
		if !loggedIn {
			if strings.HasPrefix(path, "/api/") {
				respond(w, 401, row{"detail": "请先登录"})
				return
			}
			target := "/login"
			if _, _, err := a.admin(r.Context()); err == sql.ErrNoRows {
				target = "/setup"
			}
			redirect(w, target, "")
			return
		}
		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			if !exclusive && path != "/logout" && a.maintenance() {
				a.renderError(w, r, httpError{409, "系统处于维护模式，请先检查数据并退出维护模式"})
				return
			}
			if strings.HasPrefix(path, "/api/") && !a.validCSRF(session(r), r.Header.Get("X-CSRF-Token")) {
				a.renderError(w, r, httpError{403, "CSRF 校验失败"})
				return
			}
		}
	}
	if exclusive {
		gate.Close()
	}
	a.mux.ServeHTTP(w, r)
}

type endpoint func(http.ResponseWriter, *http.Request) error

func (a *App) handle(path string, fn endpoint) {
	a.mux.HandleFunc(path, func(w http.ResponseWriter, r *http.Request) {
		if err := fn(w, r); err != nil {
			a.renderError(w, r, err)
		}
	})
}

func (a *App) renderError(w http.ResponseWriter, r *http.Request, err error) {
	status, message := http.StatusBadRequest, "操作失败，请检查数据和输入"
	var httpErr httpError
	var userErr inputError
	var large *http.MaxBytesError
	var sqliteErr *sqlite.Error
	switch {
	case errors.As(err, &httpErr):
		status, message = httpErr.code, httpErr.text
	case errors.As(err, &large):
		status, message = http.StatusRequestEntityTooLarge, "请求或上传文件过大"
	case errors.As(err, &userErr):
		message = userErr.Error()
	case errors.As(err, &sqliteErr) && sqliteErr.Code()&255 == 19:
		message = "记录已存在或仍被其他记录引用"
	default:
		log.Printf("Web 请求 %s 操作失败 (%T)", r.Method, err)
	}
	if strings.HasPrefix(r.URL.Path, "/api/") {
		respond(w, status, row{"detail": message})
	} else if err := a.page(w, r, status, "error", row{"error": message}); err != nil {
		http.Error(w, message, status)
	}
}

var nav = [][]string{{"/", "总览"}, {"/sources", "订阅源"}, {"/rules", "关键词规则"},
	{"/bots", "飞书机器人"}, {"/history", "推送历史"}, {"/settings", "系统设置"}, {"/operations", "运维与备份"}}

func (a *App) page(w http.ResponseWriter, r *http.Request, code int, view string, props row) error {
	cfg, err := a.readSettings(r.Context())
	if err != nil && view != "error" {
		return err
	}
	if cfg == nil {
		cfg = row{"timezone": "Asia/Shanghai"}
	}
	name, _, _ := a.admin(r.Context())
	data := row{
		"view": view, "nav": nav, "settings": cfg, "notice": r.URL.Query().Get("notice"),
		"csrf": a.csrf(session(r)), "maintenance": a.maintenance(), "username": name,
		"request": row{"url": row{"path": r.URL.Path}},
	}
	for key, value := range props {
		data[key] = value
	}
	return render(w, a.mainPage, code, data)
}

func render(w http.ResponseWriter, template *exec.Template, status int, data row) error {
	body, err := template.ExecuteToBytes(exec.NewContext(data))
	if err != nil {
		return fmt.Errorf("render template: %w", err)
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.WriteHeader(status)
	_, _ = w.Write(body)
	return nil
}

func (a *App) form(w http.ResponseWriter, r *http.Request, cookie string) error {
	if r.URL.Path == "/operations/restore" {
		if err := r.ParseMultipartForm(1 << 20); err != nil {
			return err
		}
		if len(r.MultipartForm.File) != 1 || len(r.MultipartForm.File["archive"]) != 1 {
			return invalid("请选择一个 ZIP 备份")
		}
	} else if err := r.ParseForm(); err != nil {
		return err
	}
	if !a.validCSRF(cookieValue(r, cookie), r.PostForm.Get("csrf")) {
		return httpError{403, "页面已过期，请刷新后再试"}
	}
	return nil
}

func readObject(r *http.Request) (row, error) {
	var data row
	decoder := json.NewDecoder(r.Body)
	decoder.UseNumber()
	if err := decoder.Decode(&data); err != nil {
		return nil, err
	}
	if data == nil || decoder.Decode(new(any)) != io.EOF {
		return nil, invalid("请求必须是 JSON 对象")
	}
	return data, nil
}

func (a *App) clientIP(r *http.Request) string {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		host = r.RemoteAddr
	}
	if !a.trustedIP(host) {
		return host
	}
	chain := strings.Split(r.Header.Get("X-Forwarded-For"), ",")
	for i := len(chain) - 1; i >= 0; i-- {
		address := strings.TrimSpace(chain[i])
		if net.ParseIP(address) != nil && !a.trustedIP(address) {
			return address
		}
	}
	return host
}

func (a *App) trustedIP(host string) bool {
	ip := net.ParseIP(host)
	for _, proxy := range a.trusted {
		if proxy.Contains(ip) {
			return true
		}
	}
	return false
}

func (a *App) credentialOK(ref string) bool {
	return credentialName(ref) && worker.ValidateCredential(a.config.CredentialsDir, ref) == nil
}

func (a *App) publicRoutes() {
	static, _ := fs.Sub(ui.Assets, "static")
	a.mux.Handle("GET /static/", http.StripPrefix("/static/", http.FileServerFS(static)))
	a.mux.HandleFunc("GET /favicon.ico", func(w http.ResponseWriter, r *http.Request) {
		http.ServeFileFS(w, r, static, "favicon.svg")
	})
	a.mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		respond(w, 200, row{"status": "ok"})
	})
	loginPage := func(w http.ResponseWriter, r *http.Request) error {
		_, _, err := a.admin(r.Context())
		if err != nil && err != sql.ErrNoRows {
			return err
		}
		nonce := cookieValue(r, "rss_preauth")
		if len(nonce) < 32 || len(nonce) > 128 || strings.ContainsFunc(nonce, func(r rune) bool { return r > 127 }) {
			nonce = token()
		}
		a.cookie(w, "rss_preauth", nonce, 1800)
		return render(w, a.loginPage, 200, row{"setup": err == sql.ErrNoRows,
			"csrf": a.csrf(nonce), "notice": r.URL.Query().Get("notice")})
	}
	a.handle("GET /login", loginPage)
	a.handle("GET /setup", loginPage)
	a.handle("POST /setup", func(w http.ResponseWriter, r *http.Request) error {
		if err := a.form(w, r, "rss_preauth"); err != nil {
			return err
		}
		if a.maintenance() {
			return invalid("系统处于维护模式")
		}
		if _, err := a.throttle(r.Context(), a.clientIP(r)); err != nil {
			return err
		}
		if err := a.setup(r.Context(), r.PostForm.Get("token"), r.PostForm.Get("username"), r.PostForm.Get("password")); err != nil {
			return err
		}
		redirect(w, "/login", "管理员已创建，请登录")
		return nil
	})
	a.handle("POST /login", func(w http.ResponseWriter, r *http.Request) error {
		if err := a.form(w, r, "rss_preauth"); err != nil {
			return err
		}
		token, err := a.login(r.Context(), r.PostForm.Get("username"), r.PostForm.Get("password"), a.clientIP(r))
		if err != nil {
			return err
		}
		a.cookie(w, "rss_session", token, 43200)
		a.cookie(w, "rss_preauth", "", -1)
		redirect(w, "/", "")
		return nil
	})
	a.handle("POST /logout", func(w http.ResponseWriter, r *http.Request) error {
		if err := a.form(w, r, "rss_session"); err != nil {
			return err
		}
		if _, err := a.db.ExecContext(r.Context(), "DELETE FROM sessions WHERE token_hash=?", tokenHash(session(r))); err != nil {
			return err
		}
		a.cookie(w, "rss_session", "", -1)
		redirect(w, "/login", "")
		return nil
	})
}

func integerOption(raw string, fallback int, min int, max int) (int, error) {
	if raw == "" {
		return fallback, nil
	}
	n, err := strconv.Atoi(raw)
	if err != nil || n < min || n > max {
		return 0, invalid(fmt.Sprintf("数字应在 %d 到 %d 之间", min, max))
	}
	return n, nil
}
