#!/usr/bin/env bash
# vultr-clash 安装/服务管理 (Linux systemd / macOS launchd / 前台运行)
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
VERSION="$(cat "$BASE_DIR/VERSION" 2>/dev/null || echo 0.0.0)"
STATE_DIR="$BASE_DIR/.state"
RUN_WEBUI="$BASE_DIR/run-webui.sh"
RUN_SUB="$BASE_DIR/run-sub.sh"

log(){ printf '\033[1;34m[*]\033[0m %s\n' "$*"; }
warn(){ printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }
die(){ printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

cmd(){ command -v "$1" >/dev/null 2>&1; }
is_linux_systemd(){ [[ "$(uname -s)" == "Linux" ]] && cmd systemctl; }
is_macos(){ [[ "$(uname -s)" == "Darwin" ]]; }

check_deps(){
    local missing=0
    for b in python3 curl jq openssl; do
        if ! cmd "$b"; then warn "缺失依赖: $b"; missing=1; fi
    done
    if ! cmd sshpass && ! cmd expect; then
        warn '缺失 sshpass/expect (密码 SSH 需要其一): mac 可执行  brew install hudochenkov/sshpass/sshpass'
        missing=1
    fi
    if ! python3 -c 'import http.server' 2>/dev/null; then
        warn "python3 缺少 http.server(系统自带, 正常情况下不应缺失)"; missing=1
    fi
    if (( missing )); then warn "依赖不全 → 可运行 '$0 selftest' 复核"; fi
}

# ---------- 环境变量样例(可选, 用户按需写入 .state/*.env) ----------
write_env_samples(){
    mkdir -p "$STATE_DIR"
    [[ -f "$STATE_DIR/webui.env" ]] || cat > "$STATE_DIR/webui.env" <<EOF
# 管理面板监听(Host/Port)
VULTR_WEB_HOST=0.0.0.0
VULTR_WEB_PORT=8333
# 订阅链接基址: 填 tailnet 可达地址, 如 http://100.90.80.9:8888/sub/clash.yaml
SUB_URL=http://localhost:8888/sub/clash.yaml
EOF
    [[ -f "$STATE_DIR/sub.env" ]] || cat > "$STATE_DIR/sub.env" <<EOF
# 订阅服务器
SUB_PORT=8888
PUB_HOST=localhost
# 订阅刷新间隔(秒), 供客户端自动更新
SUB_UPDATE_INTERVAL=14400
EOF
    log "已生成环境变量样例: .state/webui.env / .state/sub.env"
}

# ---------- Linux systemd ----------
SYSD_WEB="vultr-webui.service"
SYSD_SUB="vultr-sub.service"

write_systemd(){
    local dir ctl wanted
    if [[ "$(id -u)" == "0" ]]; then
        dir="/etc/systemd/system"; ctl="systemctl"; wanted="multi-user.target"
    else
        dir="$HOME/.config/systemd/user"; ctl="systemctl --user"; wanted="default.target"
        log "非 root: 使用用户级 systemd ($dir)"
    fi
    mkdir -p "$dir"
    cat > "$dir/$SYSD_WEB" <<EOF
[Unit]
Description=vultr-clash WebUI (v$VERSION)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$BASE_DIR
ExecStart=/usr/bin/env bash $RUN_WEBUI
EnvironmentFile=-$STATE_DIR/webui.env
Restart=on-failure
RestartSec=3

[Install]
WantedBy=$wanted
EOF

    cat > "$dir/$SYSD_SUB" <<EOF
[Unit]
Description=vultr-clash Subscription Server (v$VERSION)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$BASE_DIR
ExecStart=/usr/bin/env bash $RUN_SUB
EnvironmentFile=-$STATE_DIR/sub.env
Restart=on-failure
RestartSec=3

[Install]
WantedBy=$wanted
EOF

    $ctl daemon-reload
    log "已写入 systemd 单元: $dir/$SYSD_WEB, $dir/$SYSD_SUB"
    if [[ "$(id -u)" != "0" ]]; then
        warn "用户级服务需持久化: 请执行  loginctl enable-linger $USER"
    fi
}

# ---------- macOS launchd ----------
LA_DIR="$HOME/Library/LaunchAgents"
LA_WEB="com.vultrclash.webui"
LA_SUB="com.vultrclash.sub"

write_launchd(){
    mkdir -p "$LA_DIR" "$STATE_DIR"
    local base="$BASE_DIR" state="$STATE_DIR/launchd"
    mkdir -p "$state"
    for which in webui sub; do
        local label runfile logfile
        label="com.vultrclash.$which"
        logfile="$state/$which"
        runfile="$( [[ "$which" == "webui" ]] && echo "$RUN_WEBUI" || echo "$RUN_SUB" )"
        cat > "$LA_DIR/$label.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$runfile</string>
  </array>
  <key>WorkingDirectory</key><string>$base</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$logfile.log</string>
  <key>StandardErrorPath</key><string>$logfile.err</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
</dict>
</plist>
EOF
        launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
        launchctl bootstrap "gui/$(id -u)" "$LA_DIR/$label.plist"
        launchctl enable "gui/$(id -u)/$label"
    done
    log "已写入 LaunchAgents: $LA_DIR/com.vultrclash.{webui,sub}.plist"
}

# ---------- 节点健康守卫 (cron 每 30 分钟) ----------
GUARD_CRON="*/30 * * * * $BASE_DIR/node-guard.sh run >> $STATE_DIR/guard_cron.log 2>&1"

install_guard_cron(){
    mkdir -p "$STATE_DIR"
    chmod +x "$BASE_DIR/node-guard.sh"
    if ! crontab -l 2>/dev/null | grep -qF "node-guard.sh run"; then
        ( crontab -l 2>/dev/null; echo "$GUARD_CRON" ) | crontab -
        log "已安装节点守卫 (每 30 分钟检查一次)"
    else
        log "节点守卫已存在"
    fi
}

remove_guard_cron(){
    if crontab -l 2>/dev/null | grep -qF "node-guard.sh run"; then
        crontab -l 2>/dev/null | grep -vF "node-guard.sh run" | crontab -
        log "已移除节点守卫定时任务"
    fi
}

# ---------- 服务启停 ----------
svc_do(){
    local op="$1"
    if is_linux_systemd; then
        local ctl
        if [[ "$(id -u)" == "0" ]]; then ctl="systemctl"; else ctl="systemctl --user"; fi
        $ctl "$op" "$SYSD_WEB"; $ctl "$op" "$SYSD_SUB"
    elif is_macos; then
        local uid label
        uid="$(id -u)"; label=webui; 
        case "$op" in
            start)   launchctl bootstrap "gui/$uid" "$LA_DIR/com.vultrclash.webui.plist"; launchctl bootstrap "gui/$uid" "$LA_DIR/com.vultrclash.sub.plist" ;;
            stop)    launchctl bootout "gui/$uid/com.vultrclash.webui"; launchctl bootout "gui/$uid/com.vultrclash.sub" ;;
            restart) svc_do stop; sleep 1; svc_do start ;;
            status)  launchctl print "gui/$uid/com.vultrclash.webui" 2>/dev/null || echo "webui: 未运行"; launchctl print "gui/$uid/com.vultrclash.sub" 2>/dev/null || echo "sub: 未运行" ;;
        esac
    else
        case "$op" in
            start)   nohup bash "$RUN_WEBUI" >> "$STATE_DIR/webui.log" 2>&1 & echo "webui pid=$!"; nohup bash "$RUN_SUB" >> "$STATE_DIR/sub.log" 2>&1 & echo "sub pid=$!" ;;
            stop)    pkill -f "python3 webui.py" 2>/dev/null || true; pkill -f "python3 serve_sub.py" 2>/dev/null || true; echo "已停止" ;;
            restart) svc_do stop; sleep 1; svc_do start ;;
            status)  pgrep -af "webui.py|serve_sub.py" || echo "未运行" ;;
        esac
    fi
}

do_install(){
    check_deps
    mkdir -p "$STATE_DIR"
    chmod +x "$RUN_WEBUI" "$RUN_SUB"
    write_env_samples
    install_guard_cron
    if is_linux_systemd; then
        write_systemd; svc_do start || true; svc_do status
    elif is_macos; then
        if ! xcode-select -p >/dev/null 2>&1; then
            warn "未检测到 Xcode Command Line Tools, 请先执行: xcode-select --install"
        fi
        write_launchd
        log "launchd 已加载。日志: ~/Library/LaunchAgents 及 .state/launchd/"
    else
        warn "未检测到 systemd/launchd，退化为前台/后台运行模式"
        svc_do start
    fi
    echo ""
    echo "======================================================"
    echo " vultr-clash v$VERSION 安装就绪"
    echo " 管理面板: http://<本机IP>:8333/   (首次进入引导填 Vultr API Key)"
    echo " 订阅服务: 默认 http://localhost:8888/sub/clash.yaml"
    echo " 常用命令: $0 {status|restart|stop|start|uninstall|selftest}"
    echo "======================================================"
}

do_uninstall(){
    if is_linux_systemd; then
        local ctl
        if [[ "$(id -u)" == "0" ]]; then ctl="systemctl"; else ctl="systemctl --user"; fi
        $ctl disable --now "$SYSD_WEB" 2>/dev/null || true
        $ctl disable --now "$SYSD_SUB" 2>/dev/null || true
        rm -f "/etc/systemd/system/$SYSD_WEB" /etc/systemd/system/"$SYSD_SUB" \
              "$HOME/.config/systemd/user/$SYSD_WEB" "$HOME/.config/systemd/user/$SYSD_SUB" 2>/dev/null || true
    elif is_macos; then
        local uid; uid="$(id -u)"
        launchctl bootout "gui/$uid/com.vultrclash.webui" 2>/dev/null || true
        launchctl bootout "gui/$uid/com.vultrclash.sub"  2>/dev/null || true
        rm -f "$LA_DIR/com.vultrclash.webui.plist" "$LA_DIR/com.vultrclash.sub.plist"
    else
        pkill -f "python3 webui.py" 2>/dev/null || true
        pkill -f "python3 serve_sub.py" 2>/dev/null || true
    fi
    log "服务已停止并移除（保留 .state/ 数据与源码）"
    remove_guard_cron
}

do_selftest(){
    echo "== 平台 =="
    uname -a
    echo "== 依赖 =="
    for b in python3 curl jq openssl nc sshpass expect tailscale; do
        p="$(command -v "$b" 2>/dev/null || true)"
        printf '  %-10s %s\n' "$b" "${p:-缺失}"
    done
    echo "== 订阅 URL 相关 =="
    if [[ -f "$STATE_DIR/sub_token" ]]; then
        local tok; tok="$(tr -d '[:space:]' < "$STATE_DIR/sub_token")"
        echo "  订阅鉴权: 已启用 (token 已生成)"
    else
        echo "  订阅鉴权: 未启用 (无 .state/sub_token)"
    fi
    ( "$BASE_DIR/vultr-clash.sh" connect ) 2>/dev/null | sed -n '1,6p' || true
}

case "${1:-install}" in
    install)     do_install ;;
    selftest)    do_selftest ;;
    start|stop|restart|status) svc_do "$1" ;;
    guard)       case "${2:-install}" in
                     install)   install_guard_cron ;;
                     uninstall) remove_guard_cron ;;
                     *)         bash "$BASE_DIR/node-guard.sh" "${2:-status}" ;;
                 esac ;;
    uninstall)   do_uninstall ;;
    *) echo "用法: $0 {install|start|stop|restart|status|selftest|guard|uninstall}"; exit 1 ;;
esac