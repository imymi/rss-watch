# Docker 升级回退演练

`Dockerfile.golang` 将 Web 和 Worker 打入同一个镜像。容器入口沿用 `rss-watch-container web`、`worker`、`check-web` 和 `check-worker`。附加的 `compose.go.yaml` 只为两项服务选择 Go 镜像，默认 Compose 文件仍指向 Python 镜像。Go 镜像以 UID/GID `10001:10001` 运行，沿用 `/data`、`/credentials`、`/app/container.toml` 和日志文件。空卷初始化后可用 `rss-watch-container bootstrap-token` 读取初始化口令。

## 复现

准备本机 Docker（Engine API 1.49 或更新）、支持 `!reset` 的 Compose、Python 3.12 和 OpenSSL。以下命令以本机 Docker Desktop 上的 AMD64 演练为例。ARM64 演练可以将 `platform` 改为 `linux/arm64`。

```sh
platform=linux/amd64
docker --context desktop-linux build --platform "$platform" -f Dockerfile.golang -t rss-watch-go:local .
docker --context desktop-linux pull --platform "$platform" ghcr.io/imymi/rss-watch@sha256:a3d405242bdaa12a25bbf89a3b7e74c951bfe52f3fa054f59648b0d92f6e0156
python3 -B tests/compose/rehearsal.py --context desktop-linux --platform "$platform" --go-image rss-watch-go:local
```

脚本为每次演练分配独立项目名和两个空卷，不读取仓库的 `.env`。Web、Worker 和模拟接口使用内部网络，无宿主机映射端口。模拟飞书使用单独生成的 TLS 证书，并逐条验证签名。脚本退出时清理演练项目、网络和卷，保留构建的镜像。

## 验收

1. Python v0.2.0 创建订阅、规则和机器人，留下已发送、失败和待发的任务。
2. Go 使用同一对卷，核对订阅和规则、凭据文件名及内容哈希、文章指纹和完整队列记录。Go 发送 Python 留下的任务，再留下一个新的待发任务。
3. 回退到同一 Python 镜像，复核 Go 留下的记录并发送待发任务。最后更换 ETag 并返回相同的四篇文章，验证重新解析后没有额外通知。

每次切换都检查运行中的容器实际使用的镜像平台和摘要、健康状态、容器用户、只读根文件系统和 Worker 凭据卷的只读挂载。SQLite schema 始终为 2。

## 2026-11-19 实测

在 Docker Desktop 的原生 `linux/arm64` 引擎上完成 `Python → Go → Python`。回退并强制重新抓取后，保留 1 个订阅、1 份活动凭据、4 条文章指纹、3 条已发送记录和 1 条失败记录。两次切换前的待发任务都由接手版本沿用原事件编号发送。模拟接口收到 3 条成功通知和 1 条预期的失败测试消息，签名检查均通过。

本地镜像 `rss-watch-go:rehearsal-ac29eec-d9d4` 用于 ARM64 端到端演练，也通过了空卷启动检查。`rss-watch-go:rehearsal-amd64-d9d4` 完成 AMD64 构建。最终演练项目为 `rss-watch-rehearsal-0f53ab5d0b36`，容器、内部网络和临时卷已清理。

AMD64 使用 `rss-watch-go:rehearsal-amd64-d9d4` 在同一台 ARM64 Docker Desktop 上通过模拟运行。项目 `rss-watch-rehearsal-ba59cb762fc9` 的 Web、Worker 和模拟服务均核实为 `linux/amd64`。Python 和 Go 的平台镜像摘要分别为 `sha256:72622f013106156a6446ff73883d2134f75828a728c8230b7f8bcc1acf6c1d85` 和 `sha256:ffdd6737e98359f0b5262097d5c07d4b81050d76723a1d2f5a4fbe338d093e6c`。两次切换都保留了订阅、规则、凭据及全部队列记录，待发任务沿用原事件编号。回退后以新 ETag 强制重新解析同一批文章，未新增通知，最终仍为 4 条指纹、3 条已发送记录和 1 条失败记录。演练资源已清理。
