# Go Worker

Worker 使用原版 TOML 的 `data_dir` 和 `credentials_dir`，接入与 Python v0.2.0 对比过的解析器。需要已初始化的 SQLite schema 2 和 WAL。进程沿用 `daemon.lock`、`data.lock` 和维护标记，并更新原版状态查询读取的心跳。抓取、文章指纹、通知队列和条件请求验证信息以同一事务提交。发送结果不明确时，重启会用原事件编号恢复，接收端仍可能收到重复通知。

## 本地构建与测试

在仓库根目录使用 Go 1.27+、Python 3.12 和仓库锁定的 Python 依赖。

```sh
uv sync --locked --python 3.12
go build -o /tmp/rss-watch-worker ./cmd/rss-watch-worker
FEED_PARITY_PYTHON="$PWD/.venv/bin/python" go test -count=1 -v ./...
```

独立运行入口为 `/tmp/rss-watch-worker -config /绝对路径/临时配置.toml`。集成测试自行创建临时配置和 Python 初始化的 SQLite，HTTP 服务只在本机回环地址监听。测试用的飞书请求含合成凭据，并在测试传输层转发给本地模拟接口。

测试覆盖首次抓取仅建立基线、使用最终响应 URL 解析链接、ETag 与 304、Python 已写入文章的去重、按机器人合并命中、数据库事务回滚、429 后跨重启重试，以及进程被强制终止后的队列恢复。达到尝试上限的任务进入失败状态；其余任务沿用原事件编号。Python 可读回 Go 写入的投递状态和命中规则。解析器的合成与脱敏订阅对比测试仍由 `compat/feedparity` 运行。
