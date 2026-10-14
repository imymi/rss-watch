# 交付验证记录

## 2026-10-15：MIT 许可分发产物验收

基线：最新 `main` 的 `8bb0560b08cef02c39c41320d33f17e855cc0354`。从该提交的干净源码构建 Wheel 与 sdist；在基于该提交的测试分支实际构建 Linux/amd64 Docker 最终镜像，Dockerfile、锁文件与打包配置均未改变。

- Wheel：`rss_watch-0.2.0.dist-info/licenses/` 包含与源码逐字节一致的 `LICENSE` 和 `THIRD_PARTY_NOTICES.md`，两文件均列入 Wheel `RECORD`。
- sdist：`rss_watch-0.2.0/` 根目录包含与源码逐字节一致的两文件。
- Wheel `METADATA` 和 sdist `PKG-INFO` 均为 `Metadata-Version: 2.5`、`License-Expression: MIT`，且各有 `License-File: LICENSE` 和 `License-File: THIRD_PARTY_NOTICES.md`。自动检查接受支持这些字段的元数据版本 2.4 及以上。
- 最终容器以运行用户读取安装后的分发元数据，确认同样的 MIT 和两项文件声明。两文件位于 `/opt/venv/lib/python3.12/site-packages/rss_watch-0.2.0.dist-info/licenses/`，SHA-256 与源码一致；仅复制 `/opt/venv` 的最终阶段保留了这些材料。
- [Linux CI 38615282991](https://github.com/imymi/rss-watch/actions/runs/38615282991)（测试提交 `1dee86fbfd889cb9d6e85c097a22ce5cf11374e4`）：Ruff 通过；249 项单元测试通过、3 项按环境跳过；构建后许可专项 2 项通过；实际 Docker 集成测试 1 项通过，包含原有健康、权限及持久化检查。
- 本地以非 editable 安装复现构建阶段，也检查了全部 30 个分发包（项目及 29 个运行依赖）。分别人为删除 Wheel 的 LICENSE、删除 sdist 的第三方说明、修改 MIT 元数据或删除 License-File 声明，四种损坏均被专项测试拒绝。

Docker 内第三方 Python 材料实际位置如下，路径均相对于 `/opt/venv/lib/python3.12/site-packages/`；完整的 29 项名称、版本和绝对路径保留在上述 CI 的 Docker job 日志 `Final image license inventory` 中。

| 材料 | 实际位置 |
| --- | --- |
| 27 个依赖的随包许可 | 各自的 `<名称>-<版本>.dist-info/licenses/`，原文件名可能为 `LICENSE`、`LICENSE.txt` 或 `LICENSE.md` |
| mdurl、shellingham 的旧式许可位置 | `mdurl-0.1.2.dist-info/LICENSE`、`shellingham-1.5.4.dist-info/LICENSE` |
| Typer 内置 Click 的 BSD 声明 | `typer/_click/LICENSE.txt`；Typer 自身 MIT 文件另在其 dist-info/licenses 中 |
| markdown-it-py 的两份 MIT 材料 | `markdown_it_py-4.2.0.dist-info/licenses/LICENSE`、同目录 `LICENSE.markdown-it` |
| Pygments 许可及作者材料 | `pygments-2.21.0.dist-info/licenses/LICENSE`、同目录 `AUTHORS` |
| tzdata 许可材料 | `tzdata-2026.5.dist-info/licenses/LICENSE`、同目录下 `licenses/LICENSE_APACHE` |
| certifi 许可材料 | `certifi-2026.7.22.dist-info/licenses/LICENSE`；对应源码获取说明随项目 THIRD_PARTY_NOTICES.md 保留 |

本轮检查项目许可声明与 Python 分发包随附材料，不把 Python 文件清单视为 Debian、CPython 或原生扩展内所有组件的完整许可审计。已发布 `v0.2.0` 标签、发行附件和 GHCR 镜像未修改，本次结果不追溯适用于那些历史产物。

## 2026-10-13：Docker 与中文管理后台 v0.2.0

环境：macOS / Python 3.12.13。本机无 Docker Engine。

- 完整测试：`uv run pytest -q --tb=short`，249 项通过，1 项 Docker 集成测试按环境跳过（50.04 秒）；包含跨 schema 升级回滚测试。部署、卸载与回滚专项另有 97 项通过，与完整测试有重叠。
- `uv run ruff check .` 与 Shell 语法检查通过；`uv build` 成功生成 v0.2.0 wheel 与源码包。
- 已检查 wheel 内含模板、CSS、JS、图标及容器入口，后台资源不依赖 CDN。
- 浏览器使用独立临时示例数据验证实际登录、规则页面、在线备份按钮、数据库/凭据检查及手机宽度布局。修复 favicon 请求导致登录 CSRF 失效，并加入多登录页回归测试。截图保存于工作区 `output/rss-watch-preview-20261013/`；预览进程及临时数据已清理。
- 新测试覆盖管理员初始化、中文用户名、密码哈希、会话注销/换密码、登录限流、CSRF、凭据不回显、表单/API、范围清空确认、设置原子校验、热应用/暂停恢复、自定义重试/保留策略、自动备份、ZIP 路径穿越、缺失凭据与 FIFO 拒绝、v1 导入、完整恢复失败回滚、恢复后凭据清理和会话失效。
- 使用真实 CLI 后台子进程验证恢复时释放共享锁、保留文章基线和指纹、维护状态等待及正常退出；测试没有发送真实飞书消息。
- 原 systemd 安装器跨 schema 升级失败时，先确认新服务停止，再使用验证过的完整备份恢复旧 schema 与活动凭据，随后回退代码；相同 schema 保持原有数据行为。
- Linux GitHub Actions 验证通过：[运行 38284815660](https://github.com/imymi/rss-watch/actions/runs/38284815660)，代码提交 `4ef31e0966ffb88ed9b9ec0babdca6a83d361acc`。Python 测试、Ruff 和打包成功；Docker 隔离 Compose 测试实际构建并启动镜像，验证 web/worker 健康检查、管理员登录、设置及数据在容器重建后保留、worker 凭据卷只读。
- v0.2.0 未实测真实飞书群；原版的真实飞书签名与 RSS 联调记录见下文。
- 有 1 条 Starlette TestClient 关于 HTTPX 的弃用提示，不影响本轮测试结果。

## 2026-10-13：GHCR 镜像发布

- [发布流程 38289627270](https://github.com/imymi/rss-watch/actions/runs/38289627270) 的 validate、publish、原生 amd64 验证及原生 arm64 验证全部成功。发布前执行版本校验、Python 测试及源码 Docker 集成测试。
- 镜像从已有 `v0.2.0` 标签的原始提交 `8e26e0f899b7fbcf5b3a3c6719d087a1070eb9bb` 构建，原版分支及两个发布标签未修改。
- [GHCR Package](https://github.com/imymi/rss-watch/pkgs/container/rss-watch) 页面显示 Public；`ghcr.io/imymi/rss-watch` 的 `0.2.0`、`v0.2.0`、`latest` 指向同一清单：`sha256:a3d405242bdaa12a25bbf89a3b7e74c951bfe52f3fa054f59648b0d92f6e0156`。
- 从本机不使用 GitHub / Docker 登录凭据申请匿名只读 registry token，读取清单与配置：确认 linux/amd64、linux/arm64 两个平台，版本标注 0.2.0，源码 revision 标注与上述提交一致。
- 两个原生验证 job 均不登录 GHCR，使用实际发布镜像运行生产 Compose 模板，验证匿名 pull、web/worker 健康检查、管理员初始化/登录、设置及订阅持久化、容器重建和 worker 凭据卷只读。
- `main` 的默认 Compose 改为拉取公开镜像，新增 `compose.build.yaml` 保留源码构建；[配置变更 CI 38289582899](https://github.com/imymi/rss-watch/actions/runs/38289582899) 的 Python/Ruff/打包和源码 Docker 测试全部成功。
- 本轮没有发送真实飞书群消息。

## 历史验证记录

验证日期：2026-09-14；卸载补充验证：2026-09-15。环境：macOS / Python 3.12.13。

- `pytest -q`：58 项通过，覆盖解析、中文匹配、基线、去重、事务回滚、缓存验证信息、超时和限流、飞书业务错误、失败重试、历史清理、在线备份、CLI 菜单、后台并发及重启。
- `ruff check .`：通过。
- 实际子进程检查：CLI 启动后台，第二个后台进程被拒绝；另一 CLI 能修改机器人配置并在线备份；SIGTERM 正常退出，再启动后配置保留。
- 网络相关测试均使用 HTTPX MockTransport；实际子进程检查没有订阅源及待发送任务，不访问真实 webhook。
- 提供 `uv.lock`、源码压缩包和 Python wheel。构建使用锁文件对应的 Python 环境。

- 部署交付脚本修复与强化：
  1. **P1 修复虚拟环境路径与启动入口失效（Shebang Invalidation）**：`deploy/install.sh` 放弃原地重命名 staging 目录的方案，改造为在固定版本目录 `/opt/rss-watch/releases/<release_id>` 中构建虚拟环境与锁定依赖，通过原子切换 `/opt/rss-watch/current` 软链接进行发布，并经真实 `uv sync` 构建及 CLI 帮助调用验证。
  2. **P1 修复启动失败异常未捕获及旧版本回滚验证**：`deploy/install.sh` 采用统一的条件执行捕获机制，启动失败时自动将 `current` 软链接原子回滚至上一发布版本，并重新拉起旧版本服务；同时严格校验旧版本服务是否真正恢复运行。
  3. **P1 修复旧版单目录迁移提前破坏现有安装**：`deploy/install.sh` 将旧版单目录归档时机延后至新版本构建及 CLI 自检成功之后；在新版本构建失败时，绝不移动旧单目录下的源码与 `.venv`，原 CLI 保持 100% 完好可执行；当新服务启动失败时，通过 `backup_helper.py restore-legacy` 完整恢复旧版单目录所有文件（含 `.venv` 及隐藏文件）及旧版 systemd 服务配置，并重新拉起旧服务。
  4. **P1 修复兜底备份语法错误并将内嵌 Python 提取为独立模块**：将原 `install.sh` 中的内嵌 Python 字符串整体提取为独立的轻量级模块 `deploy/backup_helper.py`（仅使用 Python 3.12 标准库），纳入常规 `ruff` 代码检查与单元测试，彻底杜绝转义导致的 `SyntaxError`；严格校验数据库快照与所有活动机器人的物理凭据文件（`0600` 权限），任一缺失立即输出明确错误原因并中止升级。
  5. **P1 修复从旧安装目录直接运行升级及启动失败回滚**：修复当用户直接在旧安装目录（如 `/opt/rss-watch`）内运行 `./deploy/install.sh` 时，旧目录被移至 `releases/legacy_...` 导致正在调用的 `deploy/backup_helper.py` 丢失（原报 FileNotFoundError 退出码 2）的问题。改造为优先调用新版本已构建版本目录 `$RELEASE_DIR/deploy/backup_helper.py`（因位于 `releases/` 下而在旧目录迁移中被绝对保护），并具备 `legacy` 与脚本自身路径多重回退；同时在源码同步中显式排除 `releases`、`releases/*` 与 `current`，彻底杜绝嵌套复制目标目录。
  6. **P2 凭据深度合规与权限校验**：`deploy/check.sh` 深度复用应用的 `read_credential` 与 `validate_url`，对飞书 Webhook 格式、密钥类型及物理文件 `0600` 权限进行严格校验；以服务账户身份校验目录与凭据访问权限，拦截非法 URL 及权限越界。
- 部署脚本专项行为测试（[tests/test_deploy.py](tests/test_deploy.py)）：15 项专项行为测试全部通过：
  - `test_real_uv_install_symlink_and_cli_execution`：真实 `uv sync` 构建 releases 目录，切换 `current` 软链接并直接调用 CLI 入口，验证退出码为 0，彻底根除 `bad interpreter`（退出码 126）问题，且支持连续升级；
  - `test_backup_fails_when_credential_missing_aborts_install`：活动机器人引用的凭据物理文件丢失时，在线备份严格校验失败并安全中止升级；明确断言排除 `SyntaxError` 并校验具体缺失凭据名称及机器人信息；
  - `test_legacy_upgrade_dependency_failure_preserves_old_cli`：旧版单目录升级遇到依赖安装失败时，原路径源码及 `.venv` 绝不被提前破坏，原 CLI 仍可正常执行，旧服务不被停止；
  - `test_legacy_migration_startup_failure_restores_old_service`：旧版单目录迁移遇到新服务启动失败时，原单目录环境、`.venv` 及 systemd 服务配置完整恢复，且旧服务成功拉起；
  - `test_legacy_upgrade_executed_from_within_target_dir_succeeds`：直接在旧安装目录内部执行 `./deploy/install.sh` 进行升级，验证平滑升级、生成 `current` 软链接，并严格断言不发生任何 `releases` 目录嵌套复制；
  - `test_legacy_upgrade_executed_from_within_target_dir_startup_failure_rolls_back`：直接在旧安装目录内部执行 `./deploy/install.sh` 遇新服务启动失败，验证回滚机制正常调用 helper，退出码为 1（杜绝退出码 2），旧版本单目录文件、`.venv`、原 CLI 及 systemd 服务配置全部恢复，且成功拉起旧服务；
  - `test_fallback_backup_succeeds_without_old_cli_and_is_restorable`：完整数据库及合规凭据在系统中没有旧版 CLI 时，兜底备份成功完成且数据与凭据完整可恢复；
  - `test_backup_helper_direct_execution`：独立模块 `deploy/backup_helper.py` 的 backup、verify、migrate-legacy 与 restore-legacy 各子命令独立运行验证；
  - `test_restart_non_zero_triggers_rollback`：启动命令非零失败（模拟退出码 42）时，安全捕获异常，将 `current` 软链接原子回滚至上一发布版本，并重新拉起旧服务；
  - `test_rollback_failure_reports_fatal_error`：回滚时若拉起旧服务亦失败，准确报告致命错误，严禁误报“恢复成功”；
  - `test_dependency_failure_preserves_old_version`：新版本依赖安装失败时，保留旧版本、旧服务与符号链接不受损害；
  - `test_check_fails_on_invalid_credential_webhook`：Webhook 填 `not-a-url` 时自检准确报错并返回非零退出码；
  - `test_check_fails_on_bad_permission_credential`：凭据权限非 `0600` 时自检报错并返回非零退出码；
  - `test_check_fails_on_stale_heartbeat`：心跳超时时自检报错并返回非零退出码；
  - `test_check_succeeds_on_healthy_environment`：健康环境下自检返回 0 且显示全部通过。
- 卸载专项测试已迁移至 `tests/test_uninstall.py`，36 项测试覆盖：默认保留数据及账户、彻底清理实际配置路径、终端确认与取消、非交互必须显式确认、dry-run 文件内容/权限/修改时间不变且无 systemctl 调用、重复卸载、停止失败和停止后仍活跃、查询/禁用/reload 失败、危险路径、父目录及数据路径符号链接、外部链接目标保留、未知数据与其他应用 SQLite 保留、旧布局和 current 布局安装目录内执行、嵌套数据保留后再次卸载、账户归属记录及身份变化。
- 2026-09-15 补充验证：全量 106 项通过（35.16 秒）；随后补充 3 项归属与符号链接测试，卸载专项 36 项通过。合计 109 项已通过。`ruff check .` 和三个部署 Shell 脚本语法检查通过。
- systemctl 在部署/卸载测试中使用模拟程序；文件复制、删除、SQLite、终端输入和 Python 子进程均实际运行在临时目录。账户判断使用模拟 passwd/group 数据，没有删除宿主机账户。`test_real_uv_install_symlink_and_cli_execution` 使用真实 uv 构建虚拟环境，但仍不是 Linux systemd 实测。
- 构建产物：重新执行 `uv build`，更新 `dist/rss_watch-0.1.0-py3-none-any.whl` 与 `dist/rss_watch-0.1.0.tar.gz`。

## 2026-09-16 Linux 实机验收

环境：用户提供的 Debian GNU/Linux 13.7 x86_64，Linux 6.12.107，systemd 257.13。使用独立工具目录的 Python 3.12.14 和 uv，不替换系统 Python 3.13.5。开始前确认没有 rss-watch 安装、单元、数据目录或同名账户。

- Linux 全量自动化测试：`pytest -q`，**109 passed in 37.97s**；`ruff check .` 通过。该测试集中的 systemctl/账户测试仍使用模拟，不与以下真实操作混淆。
- 额外实机脚本使用真实 systemctl、useradd/userdel、文件系统和 uv：首次安装并自启成功，健康检查零失败/警告；实际服务为 `User=rss-watch`、`ProtectHome=yes`、`ProtectSystem=strict`、`active/running`。
- 默认及 purge 的 dry-run 保持服务 PID、凭据、备份不变；非交互 purge 缺少 `--yes` 时拒绝执行。
- 临时给真实单元设置 `RefuseManualStop=yes`，卸载正确中止，代码、配置和单元保留，服务继续运行；测试后已移除该 drop-in。
- 从 current 安装目录内部执行升级，真实 uv 创建新发布版本、服务重启成功，无 releases 嵌套复制。
- 从安装目录内部默认卸载，代码、历史版本及单元删除，SQLite 中测试机器人、凭据、备份和账户 UID 保留；重复卸载成功。
- 重新安装后服务健康、UID 和数据保留，账户创建记录延续；从安装目录内部 `--purge --yes` 删除代码、配置、凭据、数据库、备份和安装器创建的用户/组；再次 purge 成功。
- 额外构造真实旧版单目录和自定义数据路径 `/var/lib/rss-watch-legacy-test`，服务启动和备份正常；目录内执行 purge 清理实际配置路径，缺少创建记录的账户正确保留。随后由验收脚本清理其手工创建的测试账户。旧版测试 unit 的固定 `StateDirectory=rss-watch` 另生成空目录 `/var/lib/rss-watch`，已在验收收尾时用 `rmdir` 清理；卸载器未猜测配置外目录并递归删除。

证据：[Linux 验收脚本与原始日志](validation/linux-20260916/evidence.tar.gz)。其中 `acceptance.py` 与 `legacy-acceptance.py` 是本次服务器专用测试脚本，包含安装及删除操作，不应在已有业务环境直接运行。

结束状态：rss-watch 服务为 `LoadState=not-found`、`ActiveState=inactive`，测试业务目录及账户已清理。服务器保留 `/opt/rss-watch-test-tools`（独立 uv/Python）、`/var/tmp/rss-watch-acceptance-20260916`（源码、环境和日志）、上传的临时源码包及工具缓存，便于复查；未留下正在运行的 rss-watch 服务。

此轮未验证真实飞书通知，随后已完成下述联调。整机重启后的启动行为及实际沙箱越权攻击测试仍未执行。

## 2026-09-16 真实飞书签名与 RSS 联调

使用用户提供的机器人及签名密钥，从同一 Linux 服务器经真实后台队列发送；凭据只保存在服务账户拥有的 `0600` 文件中，本记录不包含 webhook 或密钥。

- 签名测试消息：事件 `ea01e73b-709c-4192-a456-45fbc6cf9ca0`，`state=sent`、`attempts=1`。发送器同时检查 HTTP 成功和飞书业务码为 0；这是接口接收确认，未代替用户查看群内显示。
- 本机临时 HTTP RSS 源通过真实 systemd 后台抓取，已有命中文章首次建基线时没有新建推送任务。
- 追加一篇测试文章，命中同一机器人的两条规则（任一/全部模式），合并成一条消息。事件 `1b18dbc4-59a4-4a1d-9726-c292c037e02c`，`state=sent`、`attempts=1`，命中记录包含两条规则。
- 改变 ETag、返回相同文章强制重新解析，未新增任务；随后条件请求返回 304，也未新增任务。
- 重启 systemd 服务后，两条发送记录仍为 sent、尝试次数均为 1。机器人配置完整，凭据权限为 0600，健康检查零失败/警告。

本轮结束状态更新：服务器保留已安装并启用的 rss-watch 服务及机器人配置。测试源与两条测试规则全部停用，临时 HTTP 服务已关闭，没有启用的正式订阅源，不会继续产生测试通知。尚未单独验证飞书 IP 白名单拒绝、真实限流/断网重试，以及用户端群消息显示；失败重试仍以自动化模拟测试为依据。

## 2026-09-16 全局命令纳入安装和卸载

新增 `deploy/rss-watch-entry.sh` 模板和 `deploy/entry_helper.py`，安装前预检冲突，服务启动成功后原子配置 `/usr/local/bin/rss-watch`，默认打开中文菜单。业务命令转发保留配置及服务账户，运维入口支持 check、service-status、logs、start/stop/restart。旧服务器快捷入口按完整 SHA256 识别，新入口使用安装/配置路径和正文 SHA256 标记；本记录不保存任何凭据。

- macOS 全量自动化测试：127 项通过（47.26 秒）；新增快捷入口转发、参数引用、同名命令保护、卸载/dry-run/停止失败保护测试。随后给真实 uv 安装升级和启动失败回滚测试增加入口文件断言，并单独复核。
- `ruff check .` 和部署 Shell 语法检查通过。源码包和 wheel 已重新构建。
- 本次新安装器的 systemctl 使用模拟；服务器上此前手工配置的快捷入口已验证默认中文菜单、status、source/rule list、check、service-status，但未以本次新安装器重新部署生产服务。生产 NodeSeek 和飞书配置未修改。
