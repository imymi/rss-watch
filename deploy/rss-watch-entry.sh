#!/usr/bin/env bash
set -euo pipefail
CLI=@@CLI@@
CONFIG=@@CONFIG@@
CHECK=@@CHECK@@
SERVICE_USER=@@USER@@
app() {
    if [[ ! -x "$CLI" ]]; then
        echo 'rss-watch 尚未安装，或 current 版本入口不可用。' >&2
        exit 1
    fi
    if [[ "$(id -un)" == "$SERVICE_USER" ]]; then
        exec "$CLI" --config "$CONFIG" "$@"
    else
        exec sudo -u "$SERVICE_USER" -- "$CLI" --config "$CONFIG" "$@"
    fi
}
admin() {
    if [[ $EUID -eq 0 ]]; then exec "$@"; else exec sudo -- "$@"; fi
}
if [[ $# -eq 0 ]]; then app menu; fi
case "$1" in
    help|--help|-h)
        cat <<'HELP'
rss-watch 中文管理与运维入口

  rss-watch                  打开中文管理菜单
  rss-watch menu             打开中文管理菜单
  rss-watch status           查看中文状态概览
  rss-watch status --detail  展开错误详情
  rss-watch status --json    输出 JSON，供脚本使用
  rss-watch check            执行完整健康检查
  rss-watch service-status   查看 systemd 服务状态
  rss-watch logs             实时查看日志（Ctrl+C 退出）
  rss-watch restart          重启服务
  rss-watch stop             停止监控
  rss-watch start            启动监控
  rss-watch source list      列出订阅源
  rss-watch rule list        列出关键词规则
  rss-watch bot list         列出机器人（脱敏）
  rss-watch history          查看命中和推送历史
  rss-watch cli-help         查看原始 CLI 完整帮助

菜单及业务命令以服务账户运行，普通登录账户可能需要 sudo 授权。
HELP
        ;;
    check)
        shift
        [[ -x "$CHECK" ]] || { echo '健康检查脚本不可用。' >&2; exit 1; }
        admin "$CHECK" --config "$CONFIG" "$@"
        ;;
    service-status)
        shift
        exec systemctl status rss-watch.service --no-pager "$@"
        ;;
    logs)
        shift
        admin journalctl -u rss-watch.service -n 50 -f "$@"
        ;;
    restart|stop|start)
        action="$1"
        shift
        if [[ $# -ne 0 ]]; then echo '服务启停命令不接受额外参数。' >&2; exit 2; fi
        admin systemctl "$action" rss-watch.service
        ;;
    cli-help) app --help ;;
    *) app "$@" ;;
esac
