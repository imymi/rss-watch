#!/usr/bin/env bash
set -euo pipefail

# deploy/check.sh - rss-watch health & diagnostic check script

CONFIG_FILE="${RSS_WATCH_CONFIG:-/etc/rss-watch/config.toml}"
SERVICE_USER="${RSS_WATCH_SERVICE_USER:-rss-watch}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --config)
            CONFIG_FILE="$2"
            shift 2
            ;;
        --user)
            SERVICE_USER="$2"
            shift 2
            ;;
        -h|--help)
            echo "用法: $0 [--config 配置文件路径] [--user 服务用户名]"
            echo "说明: 检查 rss-watch 服务状态、心跳、数据库、凭据权限及最近错误。默认不发送真实消息。"
            exit 0
            ;;
        *)
            echo "未知参数: $1" >&2
            exit 1
            ;;
    esac
done

echo "=================================================="
echo "          rss-watch 健康与服务自检"
echo "=================================================="
echo "检查时间: $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "配置文件: $CONFIG_FILE"
echo "服务账户: $SERVICE_USER"
echo "--------------------------------------------------"

FAIL_COUNT=0
WARN_COUNT=0

pass() {
    echo "  [PASS] $1"
}

warn() {
    echo "  [WARN] $1"
    WARN_COUNT=$((WARN_COUNT + 1))
}

fail() {
    echo "  [FAIL] $1" >&2
    FAIL_COUNT=$((FAIL_COUNT + 1))
}

info() {
    echo "  [INFO] $1"
}

# 1. 检查 systemd 服务状态（若在 Linux 环境）
echo "1. 检查 systemd 服务状态..."
if [[ "${RSS_WATCH_TEST_MOCK_ENV:-0}" != "1" ]] && [[ "$(uname -s)" == "Linux" ]] && [[ -d /run/systemd/system ]]; then
    if command -v systemctl >/dev/null 2>&1; then
        if systemctl is-active --quiet rss-watch; then
            pass "systemd 单元 rss-watch.service 处于 active (running) 状态"
        else
            STATUS="$(systemctl is-active rss-watch 2>/dev/null || echo 'unknown')"
            fail "systemd 单元 rss-watch.service 未运行 (当前状态: $STATUS)"
        fi

        if systemctl is-enabled --quiet rss-watch 2>/dev/null; then
            pass "systemd 单元 rss-watch.service 已设为开机自启 (enabled)"
        else
            warn "systemd 单元 rss-watch.service 未设为开机自启"
        fi
    else
        warn "未找到 systemctl 命令，跳过 systemd 单元状态检查"
    fi
else
    if [[ "${RSS_WATCH_TEST_MOCK_ENV:-0}" == "1" ]]; then
        info "测试模式：跳过真实 systemctl 守护进程检查"
    else
        warn "当前非 Linux systemd 运行环境，跳过 systemd 守护进程状态检查"
    fi
fi

# 2. 检查配置文件
echo "2. 检查配置文件与解释器..."
if [[ ! -f "$CONFIG_FILE" ]]; then
    fail "配置文件不存在: $CONFIG_FILE"
    echo "--------------------------------------------------"
    echo "❌ 检查终止：核心配置文件缺失。" >&2
    exit 1
fi
pass "配置文件存在: $CONFIG_FILE"

# 寻找可用的 Python 解释器
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
export PYTHONPATH="$PROJECT_ROOT/src:${PYTHONPATH:-}"

PYTHON_EXEC=""
for py_cand in /opt/rss-watch/current/.venv/bin/python /opt/rss-watch/.venv/bin/python "$PROJECT_ROOT/.venv/bin/python" python3.12 python3 python; do
    if [[ -x "$py_cand" ]]; then
        PYTHON_EXEC="$py_cand"
        break
    elif command -v "$py_cand" >/dev/null 2>&1; then
        PYTHON_EXEC="$(command -v "$py_cand")"
        break
    fi
done

if [[ -z "$PYTHON_EXEC" ]]; then
    fail "未找到可用的 Python 解释器"
    echo "--------------------------------------------------"
    echo "❌ 检查终止：缺少 Python 运行环境。" >&2
    exit 1
fi
info "使用 Python 解释器: $PYTHON_EXEC"

# 3. 运行深度结构、数据库、心跳与凭据权限检查
echo "3. 检查数据库、服务心跳与凭据深度校验（安全模式，不发消息）..."

# 如果当前是 root，且目标服务账户存在，则凭据内容与访问检查以服务账户身份进行测试
RUN_AS_USER=""
if [[ $EUID -eq 0 ]] && id -u "$SERVICE_USER" >/dev/null 2>&1; then
    info "以服务账户 $SERVICE_USER 身份执行凭据与数据自检..."
    RUN_AS_USER="$SERVICE_USER"
fi

run_python() {
    if [[ -n "$RUN_AS_USER" ]]; then
        sudo -u "$RUN_AS_USER" PYTHONPATH="${PYTHONPATH:-}" "$PYTHON_EXEC" "$@"
    else
        "$PYTHON_EXEC" "$@"
    fi
}

CHECK_RESULT="$(
run_python - "$CONFIG_FILE" "$SERVICE_USER" << 'PYEOF'
import sys
import os
import stat
import time
import json
import sqlite3
import tomllib
import pwd
from pathlib import Path

# 复用核心业务校验模块
from rss_watch.config import Config, read_credential, validate_url

config_path = Path(sys.argv[1])
service_user = sys.argv[2]
test_mode = os.environ.get("RSS_WATCH_TEST_MOCK_ENV") == "1"

try:
    with config_path.open("rb") as f:
        data = tomllib.load(f)
except Exception as e:
    print(f"FATAL: 解析配置文件失败: {e}")
    sys.exit(0)

data_dir = Path(data.get("data_dir", "/var/lib/rss-watch"))
creds_dir = Path(data.get("credentials_dir", "/etc/rss-watch/credentials"))
config_obj = Config(data_dir=data_dir, credentials_dir=creds_dir)
db_path = data_dir / "rss-watch.sqlite3"

# 获取服务账户 UID
target_uid = None
if not test_mode:
    try:
        target_uid = pwd.getpwnam(service_user).pw_uid
    except KeyError:
        print(f"WARN: 系统中未找到服务账户用户: {service_user}")

# 目录存在性及权限检查
if not data_dir.is_dir():
    print(f"FATAL: 数据目录不存在或非目录: {data_dir}")
else:
    d_stat = data_dir.stat()
    d_mode = stat.S_IMODE(d_stat.st_mode)
    if d_mode != 0o700:
        print(f"WARN: 数据目录权限为 {oct(d_mode)}，推荐为 0700")
    else:
        print(f"OK: 数据目录权限合规: {data_dir} (0700)")
    if target_uid is not None and d_stat.st_uid != target_uid:
        print(f"FATAL: 数据目录所有者为 UID {d_stat.st_uid}，而非服务账户 {service_user} (UID {target_uid})，服务可能无权读写！")

if not creds_dir.is_dir():
    print(f"FATAL: 凭据目录不存在或非目录: {creds_dir}")
else:
    c_stat = creds_dir.stat()
    c_mode = stat.S_IMODE(c_stat.st_mode)
    if c_mode != 0o700:
        print(f"FATAL: 凭据目录权限为 {oct(c_mode)}，必须为 0700")
    else:
        print(f"OK: 凭据目录权限合规: {creds_dir} (0700)")
    if target_uid is not None and c_stat.st_uid != target_uid:
        print(f"FATAL: 凭据目录所有者为 UID {c_stat.st_uid}，而非服务账户 {service_user} (UID {target_uid})，服务账户无权访问！")

    # 凭据物理文件权限检查
    for cred_file in creds_dir.glob("*.json"):
        f_stat = cred_file.stat()
        f_mode = stat.S_IMODE(f_stat.st_mode)
        if f_mode != 0o600:
            print(f"FATAL: 凭据文件 {cred_file.name} 权限为 {oct(f_mode)}，必须严格为 0600")
        if target_uid is not None and f_stat.st_uid != target_uid:
            print(f"FATAL: 凭据文件 {cred_file.name} 所有者为 UID {f_stat.st_uid}，非服务账户 {service_user}")

# 数据库检查
if not db_path.is_file():
    print(f"FATAL: 数据库文件不存在: {db_path}")
    sys.exit(0)

try:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    version = cursor.execute("PRAGMA user_version").fetchone()[0]
    if version != 1:
        print(f"FATAL: 数据库 schema 版本不兼容 (当前: {version}，预期: 1)")
    else:
        print(f"OK: 数据库访问正常，schema 版本为 1")

    # 心跳与运行状态检查
    runtime = dict(cursor.execute("SELECT key, value FROM runtime").fetchall())
    heartbeat_str = runtime.get("heartbeat")
    running_str = runtime.get("running")

    now = time.time()
    if not heartbeat_str:
        print("FATAL: 数据库无心跳记录，后台服务可能从未启动")
    else:
        hb_time = float(heartbeat_str)
        hb_diff = now - hb_time
        if running_str == "1" and hb_diff < 10:
            print(f"OK: 后台服务心跳健康 (最近心跳: {hb_diff:.1f} 秒前，状态: running)")
        elif running_str == "1":
            print(f"FATAL: 后台心跳超时！上次心跳在 {hb_diff:.1f} 秒前（超过 10s 阈值，服务可能卡死或崩溃）")
        else:
            print(f"FATAL: 后台未处于运行状态 (running={running_str}，心跳: {hb_diff:.1f} 秒前)")

    # P2 深度校验：完全复用应用 read_credential 校验凭据有效性与服务账户读取权限
    bots = cursor.execute("SELECT id, name, credential_ref FROM bots WHERE deleted=0").fetchall()
    print(f"INFO: 当前有效飞书机器人数量: {len(bots)}")
    for bot in bots:
        b_id = bot["id"]
        b_name = bot["name"]
        ref = bot["credential_ref"]
        ref_file = creds_dir / ref
        if not ref_file.is_file():
            print(f"FATAL: 机器人 #{b_id} ({b_name}) 凭据文件不存在: {ref}")
            continue

        # 核心：调用应用内同款凭据读取与严格 URL/密钥校验
        try:
            cred = read_credential(config_obj, ref)
            webhook = cred.get("webhook", "")
            validate_url(webhook, webhook=True)
            print(f"OK: 机器人 #{b_id} ({b_name}) 凭据格式与权限合规")
        except Exception as err:
            print(f"FATAL: 机器人 #{b_id} ({b_name}) 凭据校验失败: {err}")

    # 订阅源与推送队列异常概况
    sources = cursor.execute("SELECT id, name, url, enabled, initialized, last_error FROM sources WHERE deleted=0").fetchall()
    print(f"INFO: 当前订阅源总数: {len(sources)} (已启用: {sum(1 for s in sources if s['enabled'])})")
    source_errors = [s for s in sources if s["last_error"]]
    if source_errors:
        for se in source_errors:
            print(f"WARN: 订阅源 #{se['id']} ({se['name']}) 最近抓取报错: {se['last_error']}")
    else:
        print(f"OK: 所有有效订阅源无抓取错误记录")

    deliveries_rows = cursor.execute("SELECT state, count(*) as count FROM deliveries GROUP BY state").fetchall()
    deliveries_summary = {r["state"]: r["count"] for r in deliveries_rows}
    print(f"INFO: 任务队列分布: {deliveries_summary}")
    failed_deliveries = cursor.execute("SELECT id, bot_id, last_error FROM deliveries WHERE state='failed' ORDER BY updated DESC LIMIT 5").fetchall()
    if failed_deliveries:
        print(f"WARN: 存在 {deliveries_summary.get('failed', 0)} 条失败推送任务，最近失败原因: {failed_deliveries[0]['last_error']}")

    conn.close()
except Exception as e:
    print(f"FATAL: 数据库访问或查询异常: {e}")
PYEOF
)"

while IFS= read -r line; do
    if [[ "$line" =~ ^OK:\ (.*) ]]; then
        pass "${BASH_REMATCH[1]}"
    elif [[ "$line" =~ ^WARN:\ (.*) ]]; then
        warn "${BASH_REMATCH[1]}"
    elif [[ "$line" =~ ^FATAL:\ (.*) ]]; then
        fail "${BASH_REMATCH[1]}"
    elif [[ "$line" =~ ^INFO:\ (.*) ]]; then
        info "${BASH_REMATCH[1]}"
    elif [[ -n "$line" ]]; then
        info "$line"
    fi
done <<< "$CHECK_RESULT"

# 4. 检查最近系统日志（若支持）
if [[ "${RSS_WATCH_TEST_MOCK_ENV:-0}" != "1" ]] && [[ "$(uname -s)" == "Linux" ]] && command -v journalctl >/dev/null 2>&1; then
    echo "4. 检查最近 10 分钟内的服务日志告警/报错..."
    RECENT_ERRORS="$(journalctl -u rss-watch --since "10 minutes ago" -p err..emerg --no-pager -q 2>/dev/null || true)"
    if [[ -n "$RECENT_ERRORS" ]]; then
        warn "检测到最近 10 分钟内 systemd 日志包含错误级别记录："
        echo "$RECENT_ERRORS" | sed 's/^/      /'
    else
        pass "最近 10 分钟内无错误级别 journal 日志"
    fi
fi

# 5. 结果汇总与退出码
echo "--------------------------------------------------"
echo "自检结果统计: 失败项=$FAIL_COUNT, 警告项=$WARN_COUNT"

if [[ $FAIL_COUNT -gt 0 ]]; then
    echo "❌ 自检未通过，检测到 $FAIL_COUNT 个致命异常问题！" >&2
    exit 1
elif [[ $WARN_COUNT -gt 0 ]]; then
    echo "⚠️  自检完成，服务可用，但存在 $WARN_COUNT 个警示项，请留意。"
    exit 0
else
    echo "✅ 自检全部通过！rss-watch 处于健康运行状态。"
    exit 0
fi
