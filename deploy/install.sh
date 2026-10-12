#!/usr/bin/env bash
set -euo pipefail

# deploy/install.sh - rss-watch Linux deployment & upgrade script

echo "=================================================="
echo "          rss-watch 部署与升级安装程序"
echo "=================================================="

# 路径与配置参数（支持环境变量覆盖，便于测试与非标准路径部署）
TARGET_DIR="${RSS_WATCH_TARGET_DIR:-/opt/rss-watch}"
CONFIG_FILE="${RSS_WATCH_CONFIG:-/etc/rss-watch/config.toml}"
DATA_DIR="${RSS_WATCH_DATA_DIR:-/var/lib/rss-watch}"
CREDS_DIR="${RSS_WATCH_CREDS_DIR:-/etc/rss-watch/credentials}"
SYSTEMCTL="${RSS_WATCH_SYSTEMCTL:-systemctl}"
SERVICE_NAME="${RSS_WATCH_SERVICE_NAME:-rss-watch}"
SERVICE_USER="${RSS_WATCH_SERVICE_USER:-rss-watch}"
SERVICE_GROUP="${RSS_WATCH_SERVICE_GROUP:-$SERVICE_USER}"
IS_TEST="${RSS_WATCH_TEST_MOCK_ENV:-0}"

# 1. 生产环境基础校验（测试模式下跳过真实 root 与 systemd 检查）
if [[ "$IS_TEST" != "1" ]]; then
    if [[ $EUID -ne 0 ]]; then
        echo "错误：请以 root 或 sudo 权限运行此脚本 (例如: sudo ./deploy/install.sh)" >&2
        exit 1
    fi

    if [[ "$(uname -s)" != "Linux" ]]; then
        echo "错误：此安装脚本专为 Linux 系统设计 (当前系统: $(uname -s))" >&2
        exit 1
    fi

    if [[ ! -d /run/systemd/system ]]; then
        echo "错误：未检测到运行中的 systemd 守护进程" >&2
        exit 1
    fi
fi

# 2. 寻找与验证 Python 3.12 解释器
PYTHON_BIN="${RSS_WATCH_PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
    for candidate in /usr/bin/python3.12 /usr/local/bin/python3.12 /opt/python3.12/bin/python3.12; do
        if [[ -x "$candidate" ]]; then
            PYTHON_BIN="$candidate"
            break
        fi
    done

    if [[ -z "$PYTHON_BIN" ]]; then
        if command -v python3.12 >/dev/null 2>&1; then
            PYTHON_BIN="$(command -v python3.12)"
        elif command -v python3 >/dev/null 2>&1; then
            VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
            if [[ "$VER" == "3.12" ]]; then
                PYTHON_BIN="$(command -v python3)"
            fi
        fi
    fi
fi

if [[ -z "$PYTHON_BIN" ]] || [[ ! -x "$PYTHON_BIN" ]]; then
    echo "错误：未找到系统 Python 3.12 解释器。请先安装 Python 3.12" >&2
    exit 1
fi

REAL_PYTHON="$(readlink -f "$PYTHON_BIN" 2>/dev/null || realpath "$PYTHON_BIN" 2>/dev/null || echo "$PYTHON_BIN")"
if [[ "$IS_TEST" != "1" ]] && ([[ "$REAL_PYTHON" =~ ^/home/ ]] || [[ "$REAL_PYTHON" =~ ^/root/ ]]); then
    echo "错误：Python 解释器真实路径为 $REAL_PYTHON" >&2
    echo "rss-watch 服务在 systemd 中配置了 ProtectHome=true，位于 /home 或 /root 下的解释器会导致服务无法启动。" >&2
    echo "请安装系统级 Python 解释器（例如 /usr/bin/python3.12）后再行安装。" >&2
    exit 1
fi

echo "[1/7] Python 解释器检测通过: $PYTHON_BIN"

# 3. 检查 uv
UV_BIN="${RSS_WATCH_UV_BIN:-}"
if [[ -z "$UV_BIN" ]]; then
    if command -v uv >/dev/null 2>&1; then
        UV_BIN="$(command -v uv)"
    elif [[ -x /usr/local/bin/uv ]]; then
        UV_BIN="/usr/local/bin/uv"
    elif [[ -x /usr/bin/uv ]]; then
        UV_BIN="/usr/bin/uv"
    elif [[ -x /root/.cargo/bin/uv ]]; then
        UV_BIN="/root/.cargo/bin/uv"
    elif [[ -x /root/.local/bin/uv ]]; then
        UV_BIN="/root/.local/bin/uv"
    else
        echo "错误：未检测到 uv 工具。请先安装 uv (https://docs.astral.sh/uv/)" >&2
        exit 1
    fi
fi
echo "[2/7] uv 工具检测通过: $UV_BIN"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENTRY_PATH=/usr/local/bin/rss-watch
if [[ "$IS_TEST" == "1" ]]; then
    ENTRY_PATH="${RSS_WATCH_ENTRY_PATH:-$(dirname "$TARGET_DIR")/bin/rss-watch}"
fi
# 在创建账户、备份及停止旧服务前发现同名命令冲突。
"$PYTHON_BIN" "$SCRIPT_DIR/entry_helper.py" \
    "$ENTRY_PATH" "$TARGET_DIR" "$CONFIG_FILE" "$SERVICE_USER" --check

# 4. 服务账户与基础目录
echo "[3/7] 检查系统服务账户与存储目录..."
USER_CREATED=0
GROUP_CREATED=0
if [[ "$IS_TEST" != "1" ]]; then
    if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
        if ! getent group "$SERVICE_GROUP" >/dev/null 2>&1; then
            GROUP_CREATED=1
            useradd --system --user-group --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
        else
            useradd --system --gid "$SERVICE_GROUP" --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
        fi
        USER_CREATED=1
        echo "      已创建系统服务账户 $SERVICE_USER"
    else
        echo "      服务账户 $SERVICE_USER 已存在"
    fi
    install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$(dirname "$CONFIG_FILE")" "$CREDS_DIR" "$DATA_DIR"
else
    mkdir -p "$(dirname "$CONFIG_FILE")" "$CREDS_DIR" "$DATA_DIR"
    chmod 0700 "$(dirname "$CONFIG_FILE")" "$CREDS_DIR" "$DATA_DIR"
fi

SOURCE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# 5. P1 修复：升级前安全在线备份（严格校验数据库与引用的所有物理凭据，缺失立即中止）
if [[ -f "$DATA_DIR/rss-watch.sqlite3" ]]; then
    BACKUP_TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
    BACKUP_DEST="$DATA_DIR/backups/upgrade-$BACKUP_TIMESTAMP"
    echo "[4/7] 检测到已有数据库，正在执行升级前 SQLite 在线备份至 $BACKUP_DEST..."
    mkdir -p "$DATA_DIR/backups"
    if [[ "$IS_TEST" != "1" ]]; then
        chown "$SERVICE_USER:$SERVICE_GROUP" "$DATA_DIR/backups"
    fi
    chmod 0700 "$DATA_DIR/backups"

    BACKUP_FAILED=0
    # 优先使用已安装的 CLI 进行官方在线备份（已内置 admin.lock、在线 backup 和凭据导出）
    CLI_BIN="$TARGET_DIR/current/.venv/bin/rss-watch"
    if [[ ! -x "$CLI_BIN" ]] && [[ -x "$TARGET_DIR/.venv/bin/rss-watch" ]]; then
        CLI_BIN="$TARGET_DIR/.venv/bin/rss-watch"
    fi

    if [[ -x "$CLI_BIN" ]] && [[ -f "$CONFIG_FILE" ]]; then
        if [[ "$IS_TEST" != "1" ]]; then
            sudo -u "$SERVICE_USER" "$CLI_BIN" --config "$CONFIG_FILE" backup "$BACKUP_DEST" || BACKUP_FAILED=1
        else
            "$CLI_BIN" --config "$CONFIG_FILE" backup "$BACKUP_DEST" || BACKUP_FAILED=1
        fi
    else
        # 兜底：使用独立 Python 模块 backup_helper.py 进行在线备份与凭据拷贝
        echo "      未找到可执行的旧版本 CLI，使用 Python 独立模块执行兜底在线备份..."
        if [[ "$IS_TEST" != "1" ]]; then
            sudo -u "$SERVICE_USER" "$PYTHON_BIN" "$SCRIPT_DIR/backup_helper.py" backup "$DATA_DIR" "$CREDS_DIR" "$BACKUP_DEST" || BACKUP_FAILED=1
        else
            "$PYTHON_BIN" "$SCRIPT_DIR/backup_helper.py" backup "$DATA_DIR" "$CREDS_DIR" "$BACKUP_DEST" || BACKUP_FAILED=1
        fi
    fi

    # 深度校验备份完整性：不仅验证 user_version=1，更验证数据库引用的凭据文件全部存在且权限合规
    BACKUP_VERIFIED=0
    if [[ $BACKUP_FAILED -eq 0 ]] && [[ -d "$BACKUP_DEST" ]]; then
        if "$PYTHON_BIN" "$SCRIPT_DIR/backup_helper.py" verify "$BACKUP_DEST"; then
            BACKUP_VERIFIED=1
        fi
    fi

    if [[ $BACKUP_VERIFIED -ne 1 ]]; then
        echo "错误：升级前在线备份失败或凭据快照校验未通过！" >&2
        echo "升级已中止以保护现有数据，当前服务与代码保持原样不变。" >&2
        rm -rf "$BACKUP_DEST" 2>/dev/null || true
        exit 1
    fi
    echo "      备份成功并已校验: $BACKUP_DEST"
else
    echo "[4/7] 首次全新安装，无需执行升级备份"
fi

# 6. 版本检测与环境构建
mkdir -p "$TARGET_DIR/releases"

IS_UPGRADE=0
IS_LEGACY_UPGRADE=0
PREV_RELEASE_TARGET=""

if [[ -L "$TARGET_DIR/current" ]]; then
    IS_UPGRADE=1
    PREV_RELEASE_TARGET="$(readlink -f "$TARGET_DIR/current" 2>/dev/null || realpath "$TARGET_DIR/current" 2>/dev/null || echo "")"
elif [[ -d "$TARGET_DIR" ]] && [[ -f "$TARGET_DIR/pyproject.toml" || -d "$TARGET_DIR/.venv" ]]; then
    # 旧版单目录安装：不提前移动旧文件，保留原路径直到新版本构建且启动成功
    IS_UPGRADE=1
    IS_LEGACY_UPGRADE=1
fi

WAS_ACTIVE=0
if "$SYSTEMCTL" is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    WAS_ACTIVE=1
fi

RELEASE_ID="$(date +%Y%m%d_%H%M%S)_$$"
RELEASE_DIR="$TARGET_DIR/releases/$RELEASE_ID"
echo "[5/7] 在固定版本目录中构建新版本环境: $RELEASE_DIR..."
mkdir -p "$RELEASE_DIR"

if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete \
        --exclude=".venv" \
        --exclude=".local" \
        --exclude="__pycache__" \
        --exclude="*.pyc" \
        --exclude=".git" \
        --exclude=".pytest_cache" \
        --exclude=".ruff_cache" \
        --exclude="dist" \
        --exclude="releases" \
        --exclude="releases/*" \
        --exclude="current" \
        --exclude=".prev_service_unit.bak" \
        "$SOURCE_DIR/" "$RELEASE_DIR/"
else
    tar -C "$SOURCE_DIR" \
        --exclude=".venv" \
        --exclude=".local" \
        --exclude="__pycache__" \
        --exclude="*.pyc" \
        --exclude=".git" \
        --exclude=".pytest_cache" \
        --exclude=".ruff_cache" \
        --exclude="dist" \
        --exclude="releases" \
        --exclude="releases/*" \
        --exclude="current" \
        --exclude=".prev_service_unit.bak" \
        -cf - . | tar -C "$RELEASE_DIR" -xf -
fi

# 在永久版本目录直接执行 uv sync，生成的虚拟环境 shebang 永远固定，无需 rename
BUILD_FAILED=0
if ! (cd "$RELEASE_DIR" && "$UV_BIN" sync --locked --no-dev --python "$PYTHON_BIN"); then
    BUILD_FAILED=1
fi

if [[ $BUILD_FAILED -eq 0 ]]; then
    NEW_PY="$RELEASE_DIR/.venv/bin/python"
    if [[ ! -x "$NEW_PY" ]]; then
        BUILD_FAILED=1
    elif [[ "$IS_TEST" != "1" ]]; then
        REAL_NEW_PY="$(readlink -f "$NEW_PY" 2>/dev/null || realpath "$NEW_PY" 2>/dev/null || echo "$NEW_PY")"
        if [[ "$REAL_NEW_PY" =~ ^/home/ ]] || [[ "$REAL_NEW_PY" =~ ^/root/ ]]; then
            echo "错误：虚拟环境解释器指向了 $REAL_NEW_PY (落入 /home 或 /root，会被 ProtectHome 阻止)" >&2
            BUILD_FAILED=1
        fi
    fi

    if [[ $BUILD_FAILED -eq 0 ]]; then
        if ! (cd "$RELEASE_DIR" && "$NEW_PY" -m rss_watch.cli --help >/dev/null 2>&1); then
            echo "错误：新版本 CLI 自检执行失败" >&2
            BUILD_FAILED=1
        fi
    fi
fi

if [[ $BUILD_FAILED -ne 0 ]]; then
    echo "错误：新版本环境构建或校验失败！" >&2
    echo "旧版本环境与服务完整保持原样，正在清理本次失败的构建目录..." >&2
    rm -rf "$RELEASE_DIR"
    exit 1
fi
echo "      新版本构建与环境校验通过"

# 7. 原子化切换符号链接；P1 修复：安全捕获启动失败并执行可靠回滚
echo "[6/7] 切换版本符号链接并更新配置..."

if [[ $WAS_ACTIVE -eq 1 ]]; then
    echo "      暂停运行中的旧服务以进行安全切换..."
    "$SYSTEMCTL" stop "$SERVICE_NAME"
fi

# 备份原 systemd 单元文件以便启动失败时可靠恢复
SYSTEMD_UNIT_PATH="/etc/systemd/system/rss-watch.service"
if [[ "$IS_TEST" == "1" ]] && [[ -n "${RSS_WATCH_SYSTEMD_UNIT:-}" ]]; then
    SYSTEMD_UNIT_PATH="$RSS_WATCH_SYSTEMD_UNIT"
fi

SYSTEMD_UNIT_BACKUP=""
if [[ -f "$SYSTEMD_UNIT_PATH" ]]; then
    SYSTEMD_UNIT_BACKUP="$TARGET_DIR/.prev_service_unit.bak"
    cp -f "$SYSTEMD_UNIT_PATH" "$SYSTEMD_UNIT_BACKUP"
fi

LEGACY_REL=""
if [[ $IS_LEGACY_UPGRADE -eq 1 ]]; then
    LEGACY_REL="$TARGET_DIR/releases/legacy_$(date +%Y%m%d_%H%M%S)"
    echo "      正在将旧版单目录文件安全归档至: $LEGACY_REL"
    # 使用新版本中已构建好的 helper，防止从旧安装目录执行时 helper 被移走导致脚本中断
    MIGRATE_HELPER="$RELEASE_DIR/deploy/backup_helper.py"
    if [[ ! -f "$MIGRATE_HELPER" ]]; then
        MIGRATE_HELPER="$SCRIPT_DIR/backup_helper.py"
    fi
    "$PYTHON_BIN" "$MIGRATE_HELPER" migrate-legacy "$TARGET_DIR" "$LEGACY_REL"
fi

# 原子化切换符号链接 current -> releases/$RELEASE_ID
ln -sfn "$RELEASE_DIR" "$TARGET_DIR/current"

if [[ "$IS_TEST" != "1" ]]; then
    chown -R root:"$SERVICE_GROUP" "$TARGET_DIR"
    chmod -R u=rwX,go=rX "$TARGET_DIR"
fi

# 初始化配置（若不存在则生成，若已存在则完整保留）
if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "      初始化默认配置文件: $CONFIG_FILE"
    if [[ "$IS_TEST" != "1" ]]; then
        sudo -u "$SERVICE_USER" "$TARGET_DIR/current/.venv/bin/rss-watch" --config "$CONFIG_FILE" init \
            --data-dir "$DATA_DIR" --credentials-dir "$CREDS_DIR"
        chmod 0600 "$CONFIG_FILE"
        chown "$SERVICE_USER:$SERVICE_GROUP" "$CONFIG_FILE"
    else
        "$TARGET_DIR/current/.venv/bin/rss-watch" --config "$CONFIG_FILE" init \
            --data-dir "$DATA_DIR" --credentials-dir "$CREDS_DIR"
        chmod 0600 "$CONFIG_FILE"
    fi
else
    echo "      保留已有配置: $CONFIG_FILE"
fi

# 安装 systemd 服务文件
if [[ "$IS_TEST" != "1" ]]; then
    install -m 0644 "$TARGET_DIR/current/deploy/rss-watch.service" /etc/systemd/system/rss-watch.service
    "$SYSTEMCTL" daemon-reload
    "$SYSTEMCTL" enable "$SERVICE_NAME"
elif [[ -n "${RSS_WATCH_SYSTEMD_UNIT:-}" ]]; then
    cp -f "$TARGET_DIR/current/deploy/rss-watch.service" "$RSS_WATCH_SYSTEMD_UNIT"
fi

# 启动/重启服务，并安全捕获可能的启动非零退出码
echo "[7/7] 启动/重启 systemd 服务并验证运行状态..."
START_FAILED=0
if ! "$SYSTEMCTL" restart "$SERVICE_NAME"; then
    echo "警告：systemctl restart 返回非零状态" >&2
    START_FAILED=1
fi

if [[ $START_FAILED -eq 0 ]]; then
    sleep 1
    if ! "$SYSTEMCTL" is-active --quiet "$SERVICE_NAME"; then
        START_FAILED=1
    fi
fi

# 启动失败时自动回滚符号链接并恢复旧服务，且严格验证旧服务恢复结果
if [[ $START_FAILED -ne 0 ]]; then
    echo "❌ 错误：新版本服务启动失败！正在自动回滚至旧版本..." >&2
    if ! "$SYSTEMCTL" stop "$SERVICE_NAME"; then
        echo "❌ 无法停止新服务，未回退代码或数据库；请先停止服务并使用升级前完整备份恢复。" >&2
        exit 1
    fi
    if [[ -n "${BACKUP_DEST:-}" ]] && [[ -d "$BACKUP_DEST" ]]; then
        if ! "$PYTHON_BIN" "$RELEASE_DIR/deploy/backup_helper.py" restore-if-migrated \
            "$DATA_DIR" "$CREDS_DIR" "$BACKUP_DEST"; then
            echo "❌ 数据库版本回滚失败，未启动旧程序；完整备份位于 $BACKUP_DEST。" >&2
            exit 1
        fi
    fi

    if [[ $IS_LEGACY_UPGRADE -eq 1 ]] && [[ -n "$LEGACY_REL" ]] && [[ -d "$LEGACY_REL" ]]; then
        echo "==> 检测到旧版单目录迁移失败，正在回滚恢复旧版单目录环境与服务配置..."
        rm -f "$TARGET_DIR/current"
        # 使用未被移动的新版本 helper 或已归档的旧版本 helper，防止脚本所在旧路径失效
        RESTORE_HELPER="$RELEASE_DIR/deploy/backup_helper.py"
        if [[ ! -f "$RESTORE_HELPER" ]]; then
            if [[ -f "$LEGACY_REL/deploy/backup_helper.py" ]]; then
                RESTORE_HELPER="$LEGACY_REL/deploy/backup_helper.py"
            elif [[ -f "$SCRIPT_DIR/backup_helper.py" ]]; then
                RESTORE_HELPER="$SCRIPT_DIR/backup_helper.py"
            fi
        fi
        "$PYTHON_BIN" "$RESTORE_HELPER" restore-legacy "$LEGACY_REL" "$TARGET_DIR"

        if [[ -n "$SYSTEMD_UNIT_BACKUP" ]] && [[ -f "$SYSTEMD_UNIT_BACKUP" ]]; then
            echo "==> 正在恢复升级前的 systemd 服务配置: $SYSTEMD_UNIT_PATH"
            cp -f "$SYSTEMD_UNIT_BACKUP" "$SYSTEMD_UNIT_PATH"
            rm -f "$SYSTEMD_UNIT_BACKUP"
            if [[ "$IS_TEST" != "1" ]]; then
                "$SYSTEMCTL" daemon-reload
            fi
        fi

        if [[ $WAS_ACTIVE -eq 1 ]]; then
            echo "==> 正在拉起旧版本服务..."
            RESTORE_OK=1
            if ! "$SYSTEMCTL" start "$SERVICE_NAME"; then
                RESTORE_OK=0
            fi
            sleep 1
            if ! "$SYSTEMCTL" is-active --quiet "$SERVICE_NAME"; then
                RESTORE_OK=0
            fi

            if [[ $RESTORE_OK -eq 1 ]]; then
                echo "==> 已成功回滚至旧版本单目录环境，且旧服务已恢复正常运行！" >&2
            else
                echo "❌ 致命错误：回滚后拉起旧版本服务亦失败！服务处于停机状态，请人工介入检查日志排查！" >&2
                rm -rf "$RELEASE_DIR" 2>/dev/null || true
                exit 1
            fi
        else
            echo "==> 已成功回滚至旧版本单目录环境（升级前旧服务未运行，保持非活跃状态）。" >&2
        fi
        rm -rf "$RELEASE_DIR" 2>/dev/null || true
    elif [[ $IS_UPGRADE -eq 1 ]] && [[ -n "$PREV_RELEASE_TARGET" ]] && [[ -d "$PREV_RELEASE_TARGET" ]]; then
        echo "==> 切换符号链接回滚至前一版本: $PREV_RELEASE_TARGET"
        ln -sfn "$PREV_RELEASE_TARGET" "$TARGET_DIR/current"

        if [[ -n "$SYSTEMD_UNIT_BACKUP" ]] && [[ -f "$SYSTEMD_UNIT_BACKUP" ]]; then
            echo "==> 正在恢复升级前的 systemd 服务配置: $SYSTEMD_UNIT_PATH"
            cp -f "$SYSTEMD_UNIT_BACKUP" "$SYSTEMD_UNIT_PATH"
            rm -f "$SYSTEMD_UNIT_BACKUP"
            if [[ "$IS_TEST" != "1" ]]; then
                "$SYSTEMCTL" daemon-reload
            fi
        elif [[ "$IS_TEST" != "1" ]]; then
            install -m 0644 "$TARGET_DIR/current/deploy/rss-watch.service" /etc/systemd/system/rss-watch.service
            "$SYSTEMCTL" daemon-reload
        fi

        if [[ $WAS_ACTIVE -eq 1 ]]; then
            echo "==> 正在拉起旧版本服务..."
            RESTORE_OK=1
            if ! "$SYSTEMCTL" start "$SERVICE_NAME"; then
                RESTORE_OK=0
            fi
            sleep 1
            if ! "$SYSTEMCTL" is-active --quiet "$SERVICE_NAME"; then
                RESTORE_OK=0
            fi

            if [[ $RESTORE_OK -eq 1 ]]; then
                echo "==> 已成功回滚至旧版本，且旧版本服务已恢复正常运行！" >&2
            else
                echo "❌ 致命错误：回滚后拉起旧版本服务亦失败！服务处于停机状态，请人工介入检查日志排查！" >&2
                rm -rf "$RELEASE_DIR" 2>/dev/null || true
                exit 1
            fi
        else
            echo "==> 已成功恢复旧版本代码（升级前旧服务未运行，保持非活跃状态）。" >&2
        fi
        rm -rf "$RELEASE_DIR" 2>/dev/null || true
    else
        echo "首次安装启动失败，已停止异常服务。" >&2
    fi
    exit 1
fi

# 新版本服务已通过健康判定后才更新入口；启动失败回滚时保留旧入口。
"$PYTHON_BIN" "$RELEASE_DIR/deploy/entry_helper.py" \
    "$ENTRY_PATH" "$TARGET_DIR" "$CONFIG_FILE" "$SERVICE_USER"

# 仅记录安装器实际创建的标准账户；已有账户永不推断为项目创建。
if [[ "$SERVICE_USER" == "rss-watch" ]] && [[ "$SERVICE_GROUP" == "rss-watch" ]]; then
    RECEIPT_ARGS=("$TARGET_DIR" "$CONFIG_FILE" "$SYSTEMD_UNIT_PATH")
    if [[ $USER_CREATED -eq 1 ]]; then RECEIPT_ARGS+=(--user-created); fi
    if [[ $GROUP_CREATED -eq 1 ]]; then RECEIPT_ARGS+=(--group-created); fi
    "$PYTHON_BIN" "$RELEASE_DIR/deploy/install_receipt.py" \
        "${RECEIPT_ARGS[@]}"
fi

# 升级成功，清理临时单元备份与多余版本
rm -f "$TARGET_DIR/.prev_service_unit.bak" 2>/dev/null || true
if [[ -d "$TARGET_DIR/releases" ]]; then
    (
        cd "$TARGET_DIR/releases"
        # 按修改时间保留最近 5 个版本，清理多余版本
        ls -dt */ 2>/dev/null | tail -n +6 | xargs rm -rf 2>/dev/null || true
    )
fi

echo "=================================================="
echo "✅ rss-watch 部署/升级成功并已正常启动！"
echo "=================================================="
echo "当前版本路径: $RELEASE_DIR"
echo "全局命令入口: $ENTRY_PATH"
echo "服务状态检查: rss-watch check"
echo "进入管理菜单: rss-watch"
