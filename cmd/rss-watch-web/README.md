# Go Web

Go Web 和 Go Worker 可以使用同一份 TOML、SQLite schema 2 和凭据目录。Web 可在空目录初始化数据库。页面共用 Python 版的模板、CSS、JavaScript 和静态资源；两种模板引擎共用的条件写法保留原有表单和页面结构。管理接口沿用 `/api` 路径和会话 Cookie。

## 本地验证

在仓库根目录使用 Go 1.27+、Python 3.12 和锁定的 Python 依赖。

```sh
uv sync --locked --python 3.12
go build -o /tmp/rss-watch-web ./cmd/rss-watch-web
FEED_PARITY_PYTHON="$PWD/.venv/bin/python" go test -race -count=1 ./...
```

独立 Web 入口为 `/tmp/rss-watch-web -config /绝对路径/临时配置.toml -listen 127.0.0.1:8080`。首次登录前从临时数据目录读取 `bootstrap-token` 完成初始化。Go Worker 使用同一份临时配置。

集成测试通过本机 HTTP 调用管理员初始化和登录、订阅表单、开关与编辑 API、推送历史查询和重试，并与运行中的 Go Worker 一起完成 ZIP 下载、上传、恢复和重新启用监控。订阅服务器和 Feishu 服务均为本地模拟，实例使用测试创建的临时数据库和凭据目录。

恢复先校验 ZIP、SQLite 结构和活动凭据，进入维护模式后等待 Worker 让出数据锁，再保留一份恢复前备份。恢复后的数据库保留当前管理员密码，注销旧会话，并暂停抓取及推送。Python 兼容性测试覆盖旧密码哈希、schema 1 和 schema 2 ZIP 导入，以及 Python 读取 Go 导出的 ZIP。
