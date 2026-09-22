#!/usr/bin/env bash
# 通用启动器 (Linux/macOS)：读取 .state/webui.env(可选) 后启动管理面板
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ -f "$DIR/.state/webui.env" ]]; then
    set -a; source "$DIR/.state/webui.env"; set +a
fi
cd "$DIR"
exec python3 webui.py "$@"