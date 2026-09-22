#!/usr/bin/env bash
# 通用启动器 (Linux/macOS)：读取 .state/sub.env(可选) 后启动订阅服务器
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ -f "$DIR/.state/sub.env" ]]; then
    set -a; source "$DIR/.state/sub.env"; set +a
fi
cd "$DIR"
exec python3 serve_sub.py "$@"