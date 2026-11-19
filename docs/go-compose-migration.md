# 从 Python v0.2.0 升级到 Go v0.3.0-rc.1

本手册适用于已经运行 Python v0.2.0、使用 SQLite schema 2 的 Docker Compose 安装。Go 候选版和该 Python 版本可以交替使用同一套数据。涉及 Python v0.1 或 schema 1 时，先用完整 ZIP 导入新安装；本手册中的原卷回退目标为 Python v0.2.0。

## 准备

1. 记录当前 Compose 项目名、实际使用的 Compose 文件和 `.env` 路径，以及两项服务正在使用的镜像。使用与当前安装相符的精确 Python 镜像摘要，或为自建镜像保留独立的本地标签和镜像归档。如果当前运行的是演练使用的官方 Python v0.2.0，摘要为 `ghcr.io/imymi/rss-watch@sha256:a3d405242bdaa12a25bbf89a3b7e74c951bfe52f3fa054f59648b0d92f6e0156`。
2. 取得已审核的 Go 候选镜像，将其加载到宿主机并记录固定引用。在源码构建时可用 `docker build -f Dockerfile.golang -t rss-watch-go:0.3.0-rc.1 .`。停机前核对下方的版本命令输出为 `v0.3.0-rc.1`。
3. 保存原配置，准备一个宿主机的私有备份目录。记录抓取和推送原先是否暂停，以及待发、失败、最近已发送任务的事件编号。

以下命令在**原部署目录**执行。填入实际值，`GO_OVERLAY` 指向候选源码中的 [compose.go.yaml](../compose.go.yaml)。使用多份原 Compose 文件时，在两个函数里按原顺序补上其余的 `-f` 参数。

```sh
PROJECT='原 Compose 项目名'
ENV_FILE='/原部署目录/.env'
BASE_COMPOSE='/原部署目录/compose.yaml'
GO_OVERLAY='/候选源码目录/compose.go.yaml'
PYTHON_IMAGE='原 Python 镜像的固定引用'
GO_IMAGE='已加载的 Go 候选镜像引用'

compose_original() {
  docker compose -p "$PROJECT" --env-file "$ENV_FILE" -f "$BASE_COMPOSE" "$@"
}
compose_with_image() {
  selected_image=$1
  shift
  RSS_WATCH_GO_IMAGE="$selected_image" docker compose -p "$PROJECT" \
    --env-file "$ENV_FILE" -f "$BASE_COMPOSE" -f "$GO_OVERLAY" "$@"
}

compose_original ps
docker image inspect "$PYTHON_IMAGE" "$GO_IMAGE" >/dev/null
docker run --rm --network none "$GO_IMAGE" --version
```

用 `docker inspect` 核对 `web` 和 `worker` 的 `/data`、`/credentials` 挂载名称，后续与两次切换后的容器逐一比较。原部署的项目名、原数据卷、凭据卷及配置文件路径要贯穿全过程。尤其要避免执行 `docker compose down --volumes`。

## 原卷升级

1. 在 Python 后台“运维与备份”暂停抓取和推送，等 Worker 应用设置并让在途发送结束。然后执行 `compose_original stop worker`，确认 Worker 已停止。若仍有“发送中”的任务，记录事件编号和发送结果，便于切换后核对。
2. 保持 Python Web 运行，创建并下载最后一份**完整 ZIP**。将文件保存到宿主机私有目录，并用 `unzip -t` 检查。ZIP 包含数据库、`manifest.json` 和活动机器人凭据，妥善保管。然后停止 Python Web。

   ```sh
   compose_original stop web
   compose_with_image "$GO_IMAGE" up -d --no-build --pull never --wait --wait-timeout 90 web worker
   compose_with_image "$GO_IMAGE" ps
   ```

3. 用原管理员登录。核对后台版本、数据库和凭据健康、订阅基线、规则、最近历史及待发与失败事件编号。确认 Go 使用原挂载且 Worker 的凭据卷仍为只读。可在停留于暂停状态时下载一份 Go ZIP，验证备份可用。
4. 按此前记录恢复抓取与推送的开关。确认待发任务用原事件编号继续处理，Worker 心跳和新抓取正常。保留 Python 镜像和升级前完整 ZIP，直到决定结束回退观察。

## 原卷回退到 Python

Go 仍可管理时，先暂停两项工作，等在途发送结束后停 Worker，由 Go Web 下载一份最新完整 ZIP。Go 无法打开管理后台时，直接停止 Go 两项服务，保留数据卷原状。

```sh
compose_with_image "$GO_IMAGE" stop worker web
compose_with_image "$PYTHON_IMAGE" up -d --no-build --pull never --wait --wait-timeout 90 web worker
compose_with_image "$PYTHON_IMAGE" ps
```

这里使用同一叠加配置将两项服务固定到保存的 Python 镜像。回退继续使用**当前卷**，所以能保留 Go 运行期间新增的文章、队列和推送历史。核对版本、原挂载和健康状态，检查 Go 留下的待发、失败及已发送事件，再按照原开关状态恢复监控。确认再次抓取旧内容时不会新增通知。以后移除叠加配置前，先确认原 Compose 配置解析出的镜像仍是预期的 Python 版本。

原卷无法正常启动且需要从备份恢复时，优先保留当前卷，使用隔离的 Python v0.2.0 Compose 项目和新卷。启动 Web 后创建管理员，上传最后一份可用的完整 ZIP。恢复后会注销会话并保持维护模式及两项暂停；重新登录，检查订阅、凭据和历史。用备份时间及事件编号核对备份后曾送达的消息，再决定恢复监控的时点。确认替代实例可用后再安排切换，并始终只运行一个接管同一订阅集的 Worker。
