# RSS Watch

Linux 单机 RSS/Atom 关键词监控，中文交互式 CLI，飞书群自定义机器人推送。
Python 3.12+，SQLite，无 Web 服务、Redis 或外部数据库。默认每 5 分钟抓取，最多 100 个源。

## 本地开发

安装 Python 3.12 和 [uv](https://docs.astral.sh/uv/)，在项目目录执行：

```sh
uv sync --locked --python 3.12
uv run rss-watch --config "$PWD/.local/config.toml" init \
  --data-dir "$PWD/.local/data" --credentials-dir "$PWD/.local/credentials"
export RSS_WATCH_CONFIG="$PWD/.local/config.toml"
uv run rss-watch menu
# 另一个终端，设置相同 RSS_WATCH_CONFIG 后执行：
uv run rss-watch run
```

`menu` 与 `run` 是两个独立进程。退出菜单不会停止监控。后台每秒检查配置和维护心跳。
路径优先级：`--config` > `RSS_WATCH_CONFIG` > `/etc/rss-watch/config.toml`；目录必须为绝对路径。
`init` 只创建新配置，不覆盖已有文件。

## Linux / systemd 部署

适用带 systemd 的 Linux；预先安装 Python 3.12、uv、CA 证书与准确的系统时间。
代码与虚拟环境默认位于 `/opt/rss-watch`。

### 方式一：一键自动安装/升级（推荐）

在包含源码的项目目录下以 root/sudo 权限直接执行自动化安装脚本：

```sh
sudo ./deploy/install.sh
```

`deploy/install.sh` 具备幂等性与企业级安全保障：
1. 自动检查 Linux/systemd、Python 3.12（强制校验未位于 `/home` 或 `/root`，避免 `ProtectHome=true` 拦截）和 `uv`；
2. 自动创建 `rss-watch` 系统账户与所需目录结构；
3. **升级前自动全量备份**：若检测到已有数据库，自动调用 SQLite 在线备份并强制校验数据库与所有有效凭据文件一致性；
4. **固定版本目录与原子发布**：在 `/opt/rss-watch/releases/<release_id>` 中永久安装虚拟环境，通过原子切换 `/opt/rss-watch/current` 符号链接发布，彻底避免搬移虚拟环境导致 `bad interpreter` 错误；
5. **安全捕获与可靠回滚**：新版本启动失败时，自动回滚符号链接并恢复旧服务运行，且严格校验旧服务恢复状态；
6. 保留已有配置、凭据与历史数据库，配置安全权限（凭据 `0600`，目录 `0700`，代码只读），并重载启用 systemd 守护服务。

### 方式二：手动分步安装

将本项目放置在 `/opt/rss-watch/releases/initial` 并软链到 `/opt/rss-watch/current`：

```sh
sudo useradd --system --user-group --home-dir /var/lib/rss-watch --shell /usr/sbin/nologin rss-watch
sudo mkdir -p /opt/rss-watch/releases/initial
# 同步代码至 /opt/rss-watch/releases/initial
sudo cp -r . /opt/rss-watch/releases/initial/
cd /opt/rss-watch/releases/initial
# 在项目目录具备写权限的部署账户执行；Python 解释器必须位于 /usr 或 /opt，
# 不可引用 /home 下的解释器，否则 ProtectHome 会阻止服务运行。
uv sync --locked --no-dev --python /usr/bin/python3.12
sudo ln -sfn /opt/rss-watch/releases/initial /opt/rss-watch/current
sudo install -d -m 0700 -o rss-watch -g rss-watch /etc/rss-watch /var/lib/rss-watch
sudo -u rss-watch /opt/rss-watch/current/.venv/bin/rss-watch init
sudo install -m 0644 deploy/rss-watch.service /etc/systemd/system/rss-watch.service
sudo systemctl daemon-reload
sudo systemctl enable --now rss-watch
sudo -u rss-watch /opt/rss-watch/current/.venv/bin/rss-watch menu
```

已有 `rss-watch` 系统账户时跳过 `useradd`。部署账户安装完成后，确保 `/opt/rss-watch` 及虚拟环境对服务账户可读、可执行；服务账户不需要修改代码的权限。

### 服务健康自检

安装后或日常运维可通过自带的自检脚本进行无干扰验证（**默认不发送任何真实消息**）：

```sh
sudo /opt/rss-watch/current/deploy/check.sh
```

自检脚本将依次校验：
- systemd 单元处于 `active (running)` 状态且已启用自启；
- 配置文件存在且目录/凭据权限严格合规（目录 `0700`，凭据物理文件 `0600`）；
- 数据库可读写且 schema 为当前版本（1）；
- 后台心跳处于 10 秒安全窗口内且标记为运行中；
- 机器人绑定的凭据物理文件完整无缺损，URL 格式与密钥类型完全合规；
- 订阅源与推送队列最近错误诊断（非零退出码标示致命故障）。

日常查看命令：

```sh
sudo systemctl status rss-watch
sudo journalctl -u rss-watch -n 100 --no-pager
sudo -u rss-watch /opt/rss-watch/current/.venv/bin/rss-watch status
```

### 全局中文管理与运维入口

`deploy/install.sh` 首次安装或升级成功后，自动创建 `/usr/local/bin/rss-watch`。无需激活虚拟环境或记住完整路径：

```sh
rss-watch                  # 中文数字管理菜单；退出不影响后台
rss-watch status           # 中文状态概览
rss-watch status --detail  # 展开错误详情
rss-watch status --json    # 原始 JSON，供脚本使用
rss-watch check            # 完整健康检查
rss-watch service-status   # systemd 状态
rss-watch logs             # 实时日志，Ctrl+C 退出
rss-watch restart          # 重启服务
rss-watch stop             # 停止监控
rss-watch start            # 启动监控
rss-watch --help           # 快捷入口帮助
rss-watch cli-help         # 原始 CLI 完整帮助
```

原有 `source`、`rule`、`bot`、`history` 等业务子命令继续可用。菜单和业务命令自动以服务账户运行，运维写操作通过 root/sudo 执行；不修改 sudoers。入口使用安装时的配置路径和服务账户，并通过 `current` 跟随版本切换。普通账户可能需要 sudo 授权；激活虚拟环境时 PATH 可能优先选中原始 CLI，可退出虚拟环境或调用 `/usr/local/bin/rss-watch`。

安装器在修改服务前检查同名命令；无法验证归属或内容被修改时保留并中止，不覆盖其他应用。可安全接管本项目此前在服务器创建、且完整文件指纹匹配的旧快捷入口。只有新版本服务启动成功后才原子更新入口，启动失败回滚保留旧入口。默认卸载和彻底卸载均移除可验证属于此安装的入口；dry-run 只显示计划，未知或修改过的入口保留并提示。

此功能由安装脚本配置；直接 `pip install` 或 `uv sync` 仍提供原始 Python CLI，不安装系统运维入口。手工部署可在确认路径正确后以 root 执行 `python3.12 deploy/entry_helper.py /usr/local/bin/rss-watch /opt/rss-watch /etc/rss-watch/config.toml rss-watch`。

服务只能写 `/var/lib/rss-watch`，凭据目录在服务的挂载命名空间内只读。CLI 通过 `sudo -u rss-watch` 管理凭据。
服务不监听端口。仅出站访问订阅地址及飞书 API。HTTPX 默认支持 `HTTP_PROXY`、`HTTPS_PROXY`、`NO_PROXY` 环境变量；后台需要通过 systemd override 设置相同环境。

## 配置与命令

建议顺序：添加飞书机器人 → 添加订阅源 → 添加关键词规则 → 启动服务。
飞书使用群设置中的“自定义机器人”，无需应用 App ID/App Secret。可选安全设置“签名校验”；若启用关键词校验，将固定前缀 `RSS 命中提醒` 配为允许词。IP 白名单使用服务器出口 IP。

```sh
rss-watch bot add "研究通知"                  # 隐藏输入 webhook 和可选密钥
rss-watch source add "示例订阅" https://example.com/feed.xml --interval 300
rss-watch rule add "AI 动态" --bot 1 --include "人工智能" --include "AI" \
  --exclude "广告" --mode any --source 1
rss-watch bot test 1                         # 入队，由运行中的后台发送
rss-watch rule test 1 "ＡＩ 最新消息" --summary "技术研究"
rss-watch history --detail
rss-watch history --state failed --json
rss-watch delivery retry EVENT_ID
```

命令中的示例 ID 应替换成创建后返回的 ID。`--include`、`--exclude`、`--source` 可重复。
包含词、排除词均只匹配标题，摘要不参与命中或排除判断，仅保留在通知中展示。
匹配采用字面子串，不做分词、正则或词边界判断，例如 `bero` 可命中标题中的 `berostar`。
标题去 HTML、解码实体、NFKC 标准化并忽略大小写；标题中任一排除词命中即排除。
`any` 要求标题至少命中一个包含词，`all` 要求全部包含词都在标题中命中。
已有规则无需重建，更新程序并重启后生效。

| 命令 | 行为 |
| --- | --- |
| `source list [--json]` | 列出源、间隔、初始化与抓取状态 |
| `source update ID [--name NAME] [--interval SECONDS]` | 修改名称和抓取间隔，至少 60 秒 |
| `source enable/disable/delete ID` | 启用、停用或软删除源 |
| `rule list [--json]` | 列出规则，包括源范围与目标机器人 |
| `rule update ID NAME --bot ID --include WORD …` | 完整替换规则内容，保留启停状态 |
| `rule enable/disable/delete ID` | 管理规则 |
| `bot list [--json]` | 列出机器人 ID 和名称，不输出凭据 |
| `bot update ID NAME` | 隐藏输入并替换凭据；空密钥关闭请求加签 |
| `bot delete ID` | 无规则引用且无未完成/失败任务时才能删除 |
| `history [--limit N] [--state STATE] [--detail] [--json]` | 查看任务、命中规则、消息及错误 |
| `status [--detail] [--json]` | 默认显示中文状态概览；`--detail` 展开错误，`--json` 输出原始数据 |
| `backup NEW_DIRECTORY` | SQLite 在线备份，包含凭据 |

`rule update` 省略 `--exclude` 清空排除词，省略 `--source` 表示全部源。逗号菜单方便输入普通词；含逗号的字面关键词使用 CLI 的独立参数传入。
机器人可通过 `--credential-file /absolute/path/input.json` 非交互配置，JSON 为 `{"webhook":"https://open.feishu.cn/open-apis/bot/v2/hook/…","secret":"…"}`。输入文件应为 `0600` 并由管理员妥善保管；不要将 webhook 或密钥写进 shell 命令参数、版本库或日志。

## 已定义的监控行为

- 第一次成功解析只建立当前源内容的基线，不推送历史；有效空源也可以完成初始化。畸形 XML 不建立基线，下次重试。
- 后续按首次发现判断新增，不按发布时间过滤；源重新启用后会处理停用期间仍留在源内的新文章。轮询无法发现两次抓取之间已从源中移除的文章。
- GUID/Atom ID 优先，其次链接，最后标题与时间组合哈希；去重范围是单个源。同身份文章修改不重推。
- 同文章、同机器人命中多条规则时合并通知。规则变更只影响之后发现的文章；已生成的任务保留原内容并继续发送。
- URL 是源的身份。更换 URL 请删除并新增，新源重新建立基线；删除源保留原指纹、历史和已经入队的任务。
- 并发最多 5 个源。连接超时 10 秒，每次抓取总时限 30 秒，解压后响应上限 5 MiB；失败指数退避至 1 小时。
- 使用 ETag / Last-Modified 条件请求；文章、命中快照、发送任务与验证信息在同一事务保存。
- 推送为持久化队列。每机器人发送间隔至少 1 秒；重试最多 5 次（含首次），延迟 30 秒、2 分钟、10 分钟、30 分钟，遵守更长的 `Retry-After`。签名、凭据等永久错误等待人工修复。
- 超时或崩溃可能发生在远端接收之后，因此采用至少一次投递。稳定事件编号帮助识别偶发重复；不会声称恰好一次送达。
- 手动重试重置该事件的重试次数，保持事件编号。测试消息也经过同一限流、持久化及重试队列。
- 已发送任务及完整文章内容保留 90 天；精简文章指纹、未完成/失败任务保留。后台启动时及每日清理。
- 所有持久化时间为 Unix UTC 时间戳；来源发布时间保留原文。服务心跳超过 10 秒视为不在线。状态概览的心跳时间使用当前机器的本地时区；在线仅表示后台心跳有效。队列计数基于当前保留的推送记录，最近错误可能包含正在重试的任务和已停用订阅源的历史错误。原先解析 `status` JSON 的脚本需改用 `status --json`。

凭据为随机引用的独立 JSON 文件，强制 `0600`，文件目录 `0700`；数据库仅存文件引用。替换凭据会生成新文件，旧文件保留以避免与在途读取竞争；备份仅包含当前引用的文件。备份本身也包含密钥，应限制访问。

## 升级与恢复

### 方式一：一键自动升级（推荐）

将最新代码包解压到本地或覆盖到 `/opt/rss-watch` 后，直接执行：

```sh
sudo ./deploy/install.sh
```

脚本将自动执行以下升级保障：
- 在 `/var/lib/rss-watch/backups/upgrade-YYYYMMDD_HHMMSS` 自动生成全量备份（含 SQLite 在线备份与物理凭据完整性校验）；
- 在独立版本目录 `/opt/rss-watch/releases/<release_id>` 中构建新版本并安装依赖；
- 优雅停止当前运行中的 `rss-watch` 旧服务；
- 原子切换 `/opt/rss-watch/current` 软链接；
- 重启 systemd 服务并执行启动校验；若启动失败，自动原子回滚软链接至上一版本并拉起旧服务，杜绝半更新或服务长期中断。

### 方式二：手动升级流程

```sh
# 1. 运行中执行在线备份
sudo -u rss-watch /opt/rss-watch/current/.venv/bin/rss-watch backup "/var/lib/rss-watch/backups/manual-$(date +%Y%m%d_%H%M%S)"

# 2. 停止服务
sudo systemctl stop rss-watch

# 3. 更新源码与锁文件（保留配置与数据）
cd /opt/rss-watch/releases/new_release
# 示例：更新代码（git pull 或解压覆盖最新源码，排除 .venv 与数据目录）
uv sync --locked --no-dev --python /usr/bin/python3.12
ln -sfn /opt/rss-watch/releases/new_release /opt/rss-watch/current

# 4. 重启服务并检查状态
sudo systemctl restart rss-watch
sudo /opt/rss-watch/current/deploy/check.sh
```

第一版数据库 schema 为 1，不支持未知版本；后续 schema 变更必须提供显式迁移。回滚时恢复旧代码及对应数据库备份。

### 灾难恢复步骤（从备份恢复）

若发生误操作、磁盘损坏或需要回滚旧版本，可使用以下步骤从备份目录完整恢复：

```sh
# 假设备份位于 /var/lib/rss-watch/backups/upgrade-20260914_180000

# 1. 停止 systemd 服务和所有管理 CLI
sudo systemctl stop rss-watch

# 2. 备份当前故障现场（防止二次损坏）
sudo tar -czf "/tmp/rss-watch-crash-$(date +%s).tar.gz" /var/lib/rss-watch /etc/rss-watch 2>/dev/null || true

# 3. 恢复数据库文件（注意清理旧的 -wal 和 -shm 临时文件，避免混入）
sudo rm -f /var/lib/rss-watch/rss-watch.sqlite3-wal /var/lib/rss-watch/rss-watch.sqlite3-shm
sudo cp -f /var/lib/rss-watch/backups/upgrade-20260914_180000/rss-watch.sqlite3 /var/lib/rss-watch/
sudo chown rss-watch:rss-watch /var/lib/rss-watch/rss-watch.sqlite3
sudo chmod 0600 /var/lib/rss-watch/rss-watch.sqlite3

# 4. 恢复物理凭据文件
sudo cp -f /var/lib/rss-watch/backups/upgrade-20260914_180000/credentials/*.json /etc/rss-watch/credentials/ 2>/dev/null || true
sudo chown -R rss-watch:rss-watch /etc/rss-watch/credentials
sudo chmod 0700 /etc/rss-watch/credentials
sudo find /etc/rss-watch/credentials -type f -name "*.json" -exec chmod 0600 {} +

# 5. 启动服务并运行自检验证
sudo systemctl start rss-watch
sudo /opt/rss-watch/current/deploy/check.sh
```

恢复旧备份可能重发备份后已送达的通知，这是至少一次投递的预期行为。

### 卸载与清理

使用 `deploy/uninstall.sh`，需要系统 Python 3.12+；实际卸载需在 Linux 上以 root 执行。脚本及同目录的 `uninstall_helper.py`、`entry_helper.py` 必须一起保留。支持旧版单目录和 `releases/current` 布局，也支持从安装目录内部执行。

```sh
# 查看参数；预览不改变文件、账户或服务状态
sudo /opt/rss-watch/current/deploy/uninstall.sh --help
sudo /opt/rss-watch/current/deploy/uninstall.sh --dry-run

# 默认卸载：停止并禁用服务、删除单元并 daemon-reload，清理代码、虚拟环境和历史版本
sudo /opt/rss-watch/current/deploy/uninstall.sh
```

默认保留配置、飞书凭据、数据库、数据目录内的备份，以及服务账户和用户组，并逐项输出实际保留路径。默认路径为 `/etc/rss-watch/config.toml`、`/etc/rss-watch/credentials`、`/var/lib/rss-watch`。如果数据位于安装目录的子目录中，只删除代码，保留这些子目录及必要的父目录。安装目录与数据目录相同或位于数据目录内时，拒绝卸载，避免无法区分两者。

```sh
# 彻底卸载：先列出实际路径，再要求输入 rss-watch 确认
sudo /opt/rss-watch/current/deploy/uninstall.sh --purge

# 非交互执行必须同时提供这两个参数
sudo /opt/rss-watch/current/deploy/uninstall.sh --purge --yes

# 自定义安装和配置路径；数据与凭据路径始终从这个实际配置读取
sudo ./deploy/uninstall.sh --target /srv/rss-watch --config /etc/rss-watch/config.toml --purge --dry-run
```

`--purge` 删除配置、凭据、数据库及数据目录内的备份，删除后无法由本机保留数据恢复。独立保存在其他位置的备份不在清理范围内。默认卸载会删除已安装的卸载脚本；之后如需彻底清理或再次卸载，请从解压的源码包运行 `sudo ./deploy/uninstall.sh`，并传入原来的 `--target` 和 `--config`。旧版路径为 `/opt/rss-watch/deploy/uninstall.sh`。

路径保护和失败行为：

- 拒绝空路径、根目录、系统公共目录、父目录穿越及经过符号链接的配置、数据或安装路径。代码目录内的符号链接仅删除链接本身，不递归进入外部目标。
- 配置损坏或现有安装缺少配置时，在修改前中止，不回退到默认数据路径。`RSS_WATCH_DATA_DIR`、`RSS_WATCH_CREDS_DIR` 不覆盖配置。
- 通过项目文件、数据库表结构、凭据格式和备份结构检查归属；不以目录名字包含 `rss-watch` 作为依据。目录含未知文件或数据库结构无法核实时，程序仍可卸载，但保留全部业务目录和配置并返回 `1`。SQLite 检查采用 immutable 只读方式；若表结构尚仅存在于 WAL 中，需先正常停止服务并检查点，再重试清理。
- systemd 查询、停止或停后状态核验失败，立即中止且不删除项目文件。禁用和 daemon-reload 失败也会报告错误。文件和服务已不存在时可重复运行。
- 新安装器在配置目录写入 root 所有、权限 `0600` 的 `install-receipt.json`，记录实际创建的账户、UID/GID、home、shell 和单元摘要。彻底卸载只删除记录可验证且身份仍匹配的专用账户和无其他成员的组。旧安装无记录、原有账户或身份变化时保留并说明；默认卸载始终保留账户和记录。共享配置父目录不会递归删除，只尝试移除清理后为空的配置父目录。

退出码：`0` 表示完成（或预览通过），`1` 表示取消、保护性拒绝、清理不完整或系统操作失败，`2` 表示参数错误。预览即使带 `--purge` 也不要求确认、不调用 systemctl。

## 测试与开发交接

```sh
uv sync --locked
uv run pytest
uv run ruff check .
uv build
```

自动化测试使用 HTTPX MockTransport，不访问真实飞书群。2026-09-16 已在 Debian 13 / systemd 257 完成真实安装、升级、默认卸载、重装、彻底卸载、停止失败保护和旧版单目录验收；Linux 自动化测试 109 项通过。另经用户授权完成真实飞书签名消息、RSS 基线静默、新文章推送、多规则合并及去重联调。详细证据及尚未验证的范围见 [VALIDATION.md](VALIDATION.md)。

模块划分：`config` 配置及凭据；`db` schema/事务/清理；`domain` 解析与匹配；`service` 管理业务；`worker` 抓取和发送；`cli` 中文菜单及命令。菜单和子命令调用同一 Service。进一步交接见 [ANTIGRAVITY.md](ANTIGRAVITY.md)。

实现依据：[飞书自定义机器人](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot)、[feedparser 条件请求](https://feedparser.readthedocs.io/en/stable/http-etag.html)、[HTTPX 超时](https://www.python-httpx.org/advanced/timeouts/)。
