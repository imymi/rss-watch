#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${RSS_WATCH_PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
    for candidate in python3.12 python3 /usr/bin/python3.12; do
        if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null; then
            PYTHON_BIN="$(command -v "$candidate")"
            break
        fi
    done
fi
if [[ -z "$PYTHON_BIN" ]]; then
    echo "错误：需要 Python 3.12+；可通过 RSS_WATCH_PYTHON_BIN 指定解释器。" >&2
    exit 1
fi
exec "$PYTHON_BIN" -B "$SCRIPT_DIR/uninstall_helper.py" "$@"
