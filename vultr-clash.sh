#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="$BASE_DIR/.state"
INSTANCE_FILE="$STATE_DIR/instance.json"
BILL_FILE="$STATE_DIR/billing.json"
CLOUDINIT="$BASE_DIR/cloud-init.yaml"
CLASH_YAML="$BASE_DIR/clash.yaml"

SUB_URL="${SUB_URL:-http://localhost:8888/sub/clash.yaml}"
SS_PORT=8388

VULTR_API_KEY="${VULTR_API_KEY:-}"
VULTR_REGION="${VULTR_REGION:-nrt}"
VULTR_PLAN="${VULTR_PLAN:-vc2-1c-1gb}"
# 镜像 ID 留空 = 每次创建时动态解析最新 Ubuntu LTS（2026-11-11 事故：2136 被下架后新装实例 v4 全黑洞两天）
VULTR_OS_ID="${VULTR_OS_ID:-}"
INSTANCE_LABEL="${INSTANCE_LABEL:-clash-ss}"

mkdir -p "$STATE_DIR"

log()  { printf '\033[1;34m[*]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

# 记录/结算节点计费（billing.json）
add_billing_record() {
    local id="$1" region="$2" plan="$3" now
    now="$(date +%s)"
    [[ -f "$BILL_FILE" ]] || echo '{"total_spent":0,"nodes":[]}' > "$BILL_FILE"
    # 结束同 id 的旧记录，并新增当前记录（active=true 表示未销毁）
    jq --arg id "$id" --arg region "$region" --arg plan "$plan" --argjson now "$now" \
       '.nodes |= map(if .id==$id and .active then .ended=$now else . end) |
        .nodes += [{"id":$id,"region":$region,"plan":$plan,"started":$now,"ended":null,"active":true,"cost":0.0,"paid":false}]' \
       "$BILL_FILE" > "$BILL_FILE.tmp" && mv "$BILL_FILE.tmp" "$BILL_FILE"
}

set_billing_plan() {
    # billing.json 可能与实例计划不一致时纠正（幂等）
    local id="$1" plan="$2"
    [[ -f "$BILL_FILE" ]] || return 0
    jq --arg id "$id" --arg plan "$plan" \
       '.nodes |= map(if .id==$id and .active then .plan=$plan else . end)' \
       "$BILL_FILE" > "$BILL_FILE.tmp" && mv "$BILL_FILE.tmp" "$BILL_FILE"
}

settle_billing() {
    # 在销毁动作后调用：把所有 active 记录结算并累加到 total_spent
    [[ -f "$BILL_FILE" ]] || return 0
    local now
    now="$(date +%s)"
    jq --argjson now "$now" \
       --argjson rate_05 "$(awk 'BEGIN{print 3.5/720}')" \
       --argjson rate_1 "$(awk 'BEGIN{print 5/720}')" '
       [.nodes[] | select(.active)] as $act |
       ([.nodes[] | if .active then
           .ended=$now | .active=false |
           .cost=((($now - .started)/3600.0) * (if .plan=="vc2-1c-0.5gb" then $rate_05 else $rate_1 end)) |
           .paid=true
         else . end]) as $updated |
       ($act | map((($now - .started)/3600.0) * (if .plan=="vc2-1c-0.5gb" then $rate_05 else $rate_1 end)) | add) as $add |
       {total_spent: ((.total_spent // 0.0) + ($add // 0.0)), nodes: $updated}' \
       "$BILL_FILE" > "$BILL_FILE.tmp" 2>/dev/null && mv "$BILL_FILE.tmp" "$BILL_FILE" || true
}

require_jq() { command -v jq >/dev/null || die "需要 jq"; }
require_curl() { command -v curl >/dev/null || die "需要 curl"; }

# 隔离 known_hosts：避免 Vultr 复用 IP 时 host key 变更导致 SSH 拒绝连接
SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o PreferredAuthentications=password -o PubkeyAuthentication=no)

# --- 平台兼容 (macOS / Linux) ---
# base64: GNU 用 -w0，BSD/macOS 无该参数 → 一律管道去换行
b64() { base64 | tr -d '\n'; }

# 可移植端口探测：优先 nc，回退 python3 socket，最后 /dev/tcp
port_open() {
    local host="$1" port="$2"
    if command -v nc >/dev/null 2>&1; then
        nc -z -w2 "$host" "$port" 2>/dev/null
    elif command -v python3 >/dev/null 2>&1; then
        python3 - "$host" "$port" <<'PY'
import socket
import sys
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(2)
try:
    s.connect((sys.argv[1], int(sys.argv[2])))
    sys.exit(0)
except Exception:
    sys.exit(1)
finally:
    s.close()
PY
    else
        (echo > "/dev/tcp/${host}/${port}") 2>/dev/null
    fi
}

# 密码 SSH：sshpass 优先；缺失时回退 expect（macOS 自带的 /usr/bin/expect，免额外安装）
# 用法: ssh_exec <ip> <password> <remote_command> [timeout_s]
ssh_exec() {
    local ip="$1" pw="$2" cmd="${3:-true}" timeout="${4:-10}"
    if command -v sshpass >/dev/null 2>&1; then
        sshpass -p "$pw" ssh "${SSH_OPTS[@]}" -o ConnectTimeout="$timeout" root@"$ip" "$cmd"
        return $?
    fi
    if command -v expect >/dev/null 2>&1; then
        SSH_HOST="$ip" SSH_PASS="$pw" SSH_CMD="$cmd" SSH_TIMEOUT="$timeout" \
        expect <<'EXP'
set timeout [expr {[string trim $env(SSH_TIMEOUT)] + 20}]
spawn ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
          -o PreferredAuthentications=password -o PubkeyAuthentication=no \
          -o ConnectTimeout=$env(SSH_TIMEOUT) root@$env(SSH_HOST) $env(SSH_CMD)
expect {
    -re {[Pp]assword(:| for root@)} { send -- "$env(SSH_PASS)\r"; exp_continue }
    -re {[Pp]ermission denied}     { puts stderr "SSH 认证失败(密码错误或无权限)"; exit 1 }
    eof
}
catch wait result
if {[llength $result] >= 4} { exit [lindex $result 3] }
exit 1
EXP
        return $?
    fi
    die "缺少 sshpass 或 expect：macOS 执行 'brew install hudochenkov/sshpass/sshpass' 安装其一"
}

gen_ss_password() {
    openssl rand -base64 32 | tr -d '/+=' | head -c 32
}

vultr_api() {
    local method="$1" path="$2" body="${3:-}"
    local args=(-4 -sS -w "\n%{http_code}" -X "$method" \
        -H "Authorization: Bearer ${VULTR_API_KEY}" \
        -H "Content-Type: application/json")
    [[ -n "$body" ]] && args+=(--data "$body")
    local resp
    resp="$(curl "${args[@]}" "https://api.vultr.com${path}")"
    local code
    code="$(printf '%s\n' "$resp" | tail -n1)"
    local data
    data="$(printf '%s\n' "$resp" | sed '$d')"
    if [[ "${code:0:1}" != "2" ]]; then
        die "Vultr API 错误($code): $(printf '%s' "$data" | jq -r '.error' 2>/dev/null || printf '%s' "$data")"
    fi
    printf '%s' "$data"
}

check_api_key() {
    [[ -n "$VULTR_API_KEY" ]] || die "未设置 VULTR_API_KEY"
}

# ---- 动态镜像解析（2026-11-12）：写死的镜像 ID 终会被 Vultr 下架，改为每次校验+按需重解析 ----
# os_lts_candidates：按版本从新到旧列出 Ubuntu LTS x64 的镜像 ID（每行一个）
os_lts_candidates() {
    curl -4 -sS --noproxy '*' -m 20 \
        -H "Authorization: Bearer $VULTR_API_KEY" \
        'https://api.vultr.com/v2/os?per_page=100' 2>/dev/null \
      | jq -r '.os[]
                | select(.name | test("^Ubuntu [0-9]+[.]04 LTS x64$"))
                | [(.name | capture("^Ubuntu (?<v>[0-9]+)[.]04").v | tonumber), (.id|tostring)]
                | @tsv' 2>/dev/null \
      | sort -rn | cut -f2
}

OS_CACHE_FILE="$STATE_DIR/os-cache.json"

os_cache_get() {
    [[ -f "$OS_CACHE_FILE" ]] || return 1
    jq -r --arg k "$1" '.[$k].os_id // empty' "$OS_CACHE_FILE" 2>/dev/null
}

os_cache_put() {
    mkdir -p "$STATE_DIR"
    jq --arg k "$1" --arg id "$2" --arg n "$3" --argjson now "$(date +%s)" \
       '.[$k] = {os_id:$id, name:$n, resolved_at:$now}' "$OS_CACHE_FILE" 2>/dev/null \
       > "$OS_CACHE_FILE.tmp" && mv "$OS_CACHE_FILE.tmp" "$OS_CACHE_FILE" || true
}

os_id_still_valid() {
    local code
    code="$(curl -4 -sS --noproxy '*' -o /dev/null -w '%{http_code}' -m 12 \
        -H "Authorization: Bearer $VULTR_API_KEY" \
        "https://api.vultr.com/v2/os/${1}" 2>/dev/null || echo 000)"
    [[ "$code" == "200" ]]
}

# resolve_os_for_plan <plan>：显式 VULTR_OS_ID 优先（不兜底，尊重显式意图）；
# 否则缓存 ID（先用 /v2/os/{id} 校验仍在目录，404 即作废重解析），后接全部 LTS 候选兜底
resolve_os_for_plan() {
    local plan="$1" cached rest
    if [[ -n "$VULTR_OS_ID" ]]; then
        printf '%s\n' "$VULTR_OS_ID"
        return 0
    fi
    cached="$(os_cache_get "$plan" || true)"
    if [[ -n "$cached" ]] && ! os_id_still_valid "$cached"; then
        log "⚠ 缓存镜像 id=$cached 已从 Vultr 目录下架(404)，重新解析最新 Ubuntu LTS"
        cached=""
    fi
    rest="$(os_lts_candidates)"
    if [[ -z "$rest" ]]; then
        [[ -n "$cached" ]] && { printf '%s\n' "$cached"; return 0; }
        die "无法从 /v2/os 解析 Ubuntu LTS 镜像列表（API 异常？）"
    fi
    if [[ -n "$cached" ]]; then
        printf '%s\n' "$cached"
        printf '%s\n' "$rest" | grep -vx "$cached" || true
    else
        printf '%s\n' "$rest"
    fi
}

make_cloudinit() {
    local password="$1"
    local result
    result="$(sed "s/__SS_PASSWORD__/${password}/g" "$CLOUDINIT")"
    printf '%s' "$result"
}

# SSH 等待时长：健康节点 IP→SSH 就绪实测 60-71 秒（2026-10 统计 8 例），
# SSH 等待时长（2026-10-19 全量统计：24 例成功，23 例落 59-71s，P100=89s；失败全部打满上限）
# 默认 15 次 × (2s 探测 + 5s 间隔) ≈ 100s：覆盖 P100 留 ~12% 余量；失败止损 150s → ~100s
# 覆盖优先级：.state/schedule.json 的 ssh_wait_tries（热生效）> 环境变量 VULTR_SSH_WAIT_TRIES > 默认 15
ssh_wait_tries() {
    local v
    v="$(jq -r '.ssh_wait_tries // empty' "$STATE_DIR/schedule.json" 2>/dev/null || true)"
    [[ "$v" =~ ^[0-9]+$ ]] || v="${VULTR_SSH_WAIT_TRIES:-}"
    [[ "$v" =~ ^[0-9]+$ ]] || v=15
    printf '%s' "$v"
}

# 双栈 SSH 等待：v4/v6 任一通即返回，成功地址写入全局 SSH_ADDR（供后续 ssh_exec/port_open 复用）
# 2026-11-12：Vultr v4 IP 池偶发"出生即 null-route"（v6 正常），双栈探测让部署不再被 v4 黑洞卡死
SSH_ADDR=""
wait_for_ssh() {
    local ip="$1" ip6="${2:-}"
    local tries=0 max interval
    max="$(ssh_wait_tries)"
    interval="${VULTR_SSH_WAIT_INTERVAL:-5}"
    SSH_ADDR=""
    while (( tries < max )); do
        if port_open "$ip" 22; then SSH_ADDR="$ip"; return 0; fi
        if [[ -n "$ip6" ]] && port_open "$ip6" 22; then SSH_ADDR="$ip6"; return 0; fi
        tries=$((tries + 1))
        (( tries < max )) && sleep "$interval"
    done
    return 1
}

# ---- create 失败自动重试：立刻销毁 → 换区域 → 重试 create ----
# 区域环默认 ewr nrt lax sjc sea icn；覆盖：schedule.json 的 create_retry_regions（逗号或空格分隔）
# 最大尝试次数默认 5；覆盖：schedule.json 的 create_max_attempts 或环境变量 VULTR_CREATE_MAX_ATTEMPTS
# 换区域后的套餐按 operator 策略：ewr=$3.5(vc2-1c-0.5gb)，其余 $5(vc2-1c-1gb)
retry_regions_cfg() {
    local v
    v="$(jq -r '.create_retry_regions // empty' "$STATE_DIR/schedule.json" 2>/dev/null || true \
        | tr ',' ' ' | tr -d '"')"
    v="$(printf '%s' "$v" | xargs)"
    printf '%s' "${v:-${VULTR_RETRY_REGIONS:-ewr nrt lax sjc sea icn}}"
}

max_attempts_cfg() {
    local v
    v="$(jq -r '.create_max_attempts // empty' "$STATE_DIR/schedule.json" 2>/dev/null || true)"
    [[ "$v" =~ ^[0-9]+$ ]] || v="${VULTR_CREATE_MAX_ATTEMPTS:-}"
    [[ "$v" =~ ^[0-9]+$ ]] || v=5
    printf '%s' "$v"
}

plan_for_region() {
    case "$1" in
        ewr) printf 'vc2-1c-0.5gb' ;;
        *)   printf 'vc2-1c-1gb' ;;
    esac
}

# 上次成功创建的地区（.state/last_success.json，create 成功时写入）；无记录时输出空
last_success_region() {
    jq -r '.region // empty' "$STATE_DIR/last_success.json" 2>/dev/null || true
}

# 建节点/更新订阅后强刷本机 mihomo 并钉选节点 (2026-11-03)
# 同 IP 换密码场景 provider 缓存旧密码、AUTO 组卡死旧引用，两连 PUT 才能恢复；mihomo 未配置时静默跳过
refresh_mihomo() {
    local ctl secret c1 c2
    ctl="${MIHOMO_API:-http://127.0.0.1:9090}"
    secret="$(grep -m1 '^secret:' "$HOME/.config/mihomo/config.yaml" 2>/dev/null | sed 's/^secret: *//; s/"//g')"
    if [[ -z "$secret" ]]; then
        log "ℹ 未找到 mihomo secret，跳过本机代理刷新"
        return 0
    fi
    c1="$(curl -s -o /dev/null -w '%{http_code}' -m 8 -X PUT -H "Authorization: Bearer $secret" "$ctl/providers/proxies/vultr" 2>/dev/null || echo 000)"
    sleep 2
    c2="$(curl -s -o /dev/null -w '%{http_code}' -m 8 -X PUT -H "Authorization: Bearer $secret" -H 'Content-Type: application/json' -d '{"name":"vultr-ss"}' "$ctl/proxies/Proxy" 2>/dev/null || echo 000)"
    if [[ "$c1" == 204 && "$c2" == 204 ]]; then
        log "✓ 本机 mihomo 已刷新并钉选 vultr-ss"
    else
        log "⚠ mihomo 刷新未完全成功 (provider=$c1 pin=$c2)，代理可能需手动处理"
    fi
    return 0
}

# 2026-11-10 手动开关制（用户指令）：节点销毁后出口确定性回 DIRECT。
# 不做自动兜底——魔戒/STC 机场只由 8333 面板开关手动启用。
set_mihomo_direct() {
    local ctl secret now c
    now="$(curl -s -m 5 -H "Authorization: Bearer $secret" "$ctl/proxies/Proxy" 2>/dev/null | jq -r '.now // empty' 2>/dev/null)"
    if [[ -n "$now" && "$now" != "vultr-ss" && "$now" != "null" ]]; then
        log "ℹ 出口保持 $now（用户手动选择，销毁不干预）"
        return 0
    fi
    c="$(curl -s -o /dev/null -w '%{http_code}' -m 8 -X PUT -H "Authorization: Bearer $secret" -H 'Content-Type: application/json' -d '{"name":"DIRECT"}' "$ctl/proxies/Proxy" 2>/dev/null || echo 000)"
    if [[ "$c" == 204 ]]; then
        log "✓ mihomo 出口已回 DIRECT（vultr 已死，未动用户手选的机场）"
    else
        log "⚠ mihomo 出口回 DIRECT 未成功 (http=$c)"
    fi
    return 0
}

# 以 start 为起点的区域环序列（去重、截断到 max 个）
region_sequence() {
    local start="$1" max="$2" list="$3"
    local seq="" r found=0 out="" n=0
    for r in $list; do
        [[ "$r" == "$start" ]] && { found=1; continue; }
        (( found )) && seq="$seq $r"
    done
    for r in $list; do
        [[ "$r" == "$start" ]] && break
        seq="$seq $r"
    done
    for r in $start $seq; do
        (( n >= max )) && break
        out="$out $r"; n=$((n + 1))
    done
    printf '%s' "${out# }"
}

action_create() {
    # 未显式指定地区时：优先尝试上次成功的地区（2026-10-23），无记录再回落 $VULTR_REGION
    local first_region first_plan
    if [[ -n "${1:-}" ]]; then
        first_region="$1"
        first_plan="${2:-$VULTR_PLAN}"
    else
        first_region="$(last_success_region)"
        if [[ -n "$first_region" ]]; then
            log "ℹ 未指定地区，优先尝试上次成功地区: $first_region"
        else
            first_region="$VULTR_REGION"
        fi
        first_plan="${2:-$(plan_for_region "$first_region")}"
    fi
    local list max seq region plan n=0 ok=0 tried_all=""

    list="$(retry_regions_cfg)"
    max="$(max_attempts_cfg)"
    seq="$(region_sequence "$first_region" "$max" "$list")"

    # 创建进行中标记：guard 见到即跳过本轮探测，避免误杀在建节点。
    # 必须覆盖整条重试链（trap 兜底清理），不能只标记单次尝试。
    touch "$STATE_DIR/creating"
    trap 'rm -f "$STATE_DIR/creating"' EXIT

    for region in $seq; do
        n=$((n + 1))
        tried_all="$tried_all $region"
        if (( n == 1 )); then
            plan="$first_plan"
        else
            plan="$(plan_for_region "$region")"
        fi
        log "▶ 第 ${n} 次尝试: region=$region plan=$plan"
        if ( create_attempt "$region" "$plan" ); then
            ok=1
            break
        fi
        # 幂等兜底销毁：失败路径大多已自毁，此处防 API 报错路径留下计费残留
        [[ -n "$VULTR_API_KEY" ]] && { destroy_instance || true; }
    done

    if (( ok )); then
        log "✓ 命中可用节点: region=$region（第 ${n} 次尝试）"
        # create 后自动跑后续：SSH 校正真实 ss 密码 → 重生成订阅 → 强刷本机 mihomo（2026-11-03 用户需求）
        log "ℹ 自动执行后续流程: update"
        if bash "$0" update; then
            :
        else
            log "⚠ 自动 update 失败，订阅/代理可能未同步"
        fi
    else
        log "✗ create 失败：已尝试区域[${tried_all# }] 共 ${n} 次"
        exit 1
    fi
}

update_clash_yaml() {
    # 2026-11-12 双栈：同时输出 vultr-ss4 / vultr-ss6 两个节点 + url-test 自动切换组。
    # 节点名不再叫 vultr-ss —— 该名字保留给本机 mihomo 主配置里的 url-test 组（见 ~/.config/mihomo/config.yaml），
    # 避免 provider 节点与主配置组重名冲突；webui 面板钉选/回 DIRECT 逻辑继续用 "vultr-ss" 不变。
    local ip="$1" ip6="${2:-}" password="$3"
    {
        cat <<EOF
mixed-port: 7890
allow-lan: false
mode: rule
log-level: info

proxies:
  - name: "vultr-ss4"
    type: ss
    server: ${ip}
    port: ${SS_PORT}
    cipher: aes-256-gcm
    password: "${password}"
    udp: true
EOF
        if [[ -n "$ip6" ]]; then
        cat <<EOF
  - name: "vultr-ss6"
    type: ss
    server: ${ip6}
    port: ${SS_PORT}
    cipher: aes-256-gcm
    password: "${password}"
    udp: true
EOF
        fi
        cat <<EOF

proxy-groups:
  - name: "vultr-auto"
    type: url-test
    url: "http://www.gstatic.com/generate_204"
    interval: 120
    proxies: [vultr-ss4${ip6:+, vultr-ss6}]

  - name: "Proxy"
    type: select
    proxies:
      - vultr-auto
      - DIRECT

rules:
  # tailnet (tailscale) 永远直连 (2026-11-05): Verge 经自身代理拉订阅时 100.64/10/ts.net 不能进 Proxy
  - IP-CIDR,100.64.0.0/10,DIRECT,no-resolve
  - IP-CIDR6,fd7a:115c:a1e0::/48,DIRECT,no-resolve
  - DOMAIN-SUFFIX,ts.net,DIRECT
  - DOMAIN-SUFFIX,tailscale.com,DIRECT
  # mini4k: .com 国内DNS被污染(假IP)，必须走代理由节点远程解析；.net 源站异常时代理兜底
  - DOMAIN-SUFFIX,mini4k.com,Proxy
  - DOMAIN-SUFFIX,mini4k.net,DIRECT
  # github.com: DNS污染假IP命中GEOIP,CN被直连→TLS重置，强制走Proxy (2026-10-06)
  - DOMAIN-SUFFIX,github.com,Proxy
  - GEOIP,CN,DIRECT
  - MATCH,Proxy
EOF
    } > "$CLASH_YAML"
    log "已更新 clash.yaml (v4: ${ip}${ip6:+, v6: ${ip6}})"
}

# 单次创建尝试；由 action_create 在子 shell 中调用，
# 失败路径（die）只终止本次尝试，交由上层换区域重试
# ---------------------------------------------------------------------------
# Tailscale exit node 部署（2026-11-06）：把节点注册为 tailnet 出口
# 前提：$STATE_DIR/tailscaled.state.vultr-exit（已注册设备身份备份，2438B）
# 每次重建新实例后恢复同一身份 → 设备 ID 不变，手机端 Auto exit node 无感切换
# 任一步失败仅告警跳过，绝不影响 SS 节点本身的创建结果
# ---------------------------------------------------------------------------
TS_STATE_BACKUP="$STATE_DIR/tailscaled.state.vultr-exit"
TS_HOSTNAME="vultr-exit"

setup_tailscale_exit() {
    local ip="$1" pw="$2"
    if [[ ! -s "$TS_STATE_BACKUP" ]]; then
        log "⚠ 未找到 $TS_STATE_BACKUP，跳过 exit node 部署"
        return 0
    fi
    log "部署 Tailscale exit node ($TS_HOSTNAME)..."
    # 1) 安装 tailscale（幂等）并停服务，准备恢复设备身份
    if ! ssh_exec "$ip" "$pw" '
        command -v tailscale >/dev/null 2>&1 || curl -fsSL https://tailscale.com/install.sh | sh >/tmp/ts-install.log 2>&1
        systemctl stop tailscaled >/dev/null 2>&1 || true
        command -v tailscale >/dev/null 2>&1
    ' 120 >/dev/null 2>&1; then
        log "⚠ Tailscale 安装失败，跳过 exit node 部署"
        return 0
    fi
    # 2) 传设备身份文件（管道传文件需要 sshpass；expect 路径不支持，跳过）
    if ! command -v sshpass >/dev/null 2>&1; then
        log "⚠ 缺少 sshpass 无法传身份文件，跳过 exit node 部署"
        return 0
    fi
    if ! sshpass -p "$pw" ssh -o PubkeyAuthentication=no -o PreferredAuthentications=password \
        -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        root@"$ip" 'cat > /var/lib/tailscale/tailscaled.state' < "$TS_STATE_BACKUP" >/dev/null 2>&1; then
        log "⚠ 身份文件传输失败，跳过 exit node 部署"
        return 0
    fi
    # 3) 开转发 + 起服务 + 上线（state 已注册，免授权直连）
    if ! ssh_exec "$ip" "$pw" '
        chmod 600 /var/lib/tailscale/tailscaled.state
        printf "net.ipv4.ip_forward = 1\nnet.ipv6.conf.all.forwarding = 1\n" > /etc/sysctl.d/99-tailscale.conf
        sysctl -p /etc/sysctl.d/99-tailscale.conf >/dev/null 2>&1
        systemctl start tailscaled
        sleep 3
        timeout 70 tailscale up --advertise-exit-node --hostname='"'"'"$TS_HOSTNAME"'"'"' --timeout=60s >/dev/null 2>&1
        tailscale status >/dev/null 2>&1 && echo TS-UP-OK
    ' 100 2>/dev/null | grep -q TS-UP-OK; then
        log "⚠ exit node 上线未确认（不影响 SS 节点），可稍后重跑检查"
        return 0
    fi
    log "Tailscale exit node 部署完成: $TS_HOSTNAME（设备身份复用，手机端无感）"
}

create_attempt() {
    check_api_key
    require_curl; require_jq

    local region="${1:-$VULTR_REGION}"
    local plan="${2:-$VULTR_PLAN}"

    if [[ -f "$INSTANCE_FILE" ]]; then
        local existing_id
        existing_id="$(jq -r '.instance.id // empty' "$INSTANCE_FILE" 2>/dev/null || true)"
        if [[ -n "$existing_id" ]]; then
            log "实例已存在 (id=$existing_id)，跳过创建。先 destroy 再 create"
            connect
            return
        fi
    fi

    local ss_password
    ss_password="$(gen_ss_password)"

    local userdata
    userdata="$(make_cloudinit "$ss_password" | b64)"

    local resp payload os_list oci made=0
    os_list="$(resolve_os_for_plan "$plan")"
    [[ -n "$os_list" ]] || die "无可用镜像候选"
    # 注意：本函数在 if 条件的子 shell 中运行，set -e 被抑制，
    # vultr_api 在 $( ) 内 die 只终止替换 shell —— 必须显式 || die 快速失败
    while IFS= read -r oci; do
        [[ -n "$oci" ]] || continue
        payload="$(jq -n \
            --arg region "$region" \
            --arg plan "$plan" \
            --arg os_id "$oci" \
            --arg label "$INSTANCE_LABEL" \
            --arg userdata "$userdata" \
            '{region:$region, plan:$plan, os_id:$os_id, enable_ipv6:true, label:$label, user_data:$userdata}')"
        log "正在创建实例: region=$region plan=$plan os_id=$oci"
        resp="$(vultr_api POST /v2/instances "$payload" 2>&1)" || {
            if printf '%s' "$resp" | grep -q 'requires a plan with at least'; then
                log "⚠ 镜像 $oci 内存要求超出套餐 $plan，自动降级下一个候选"
                continue
            fi
            die "创建实例 API 调用失败: $(printf '%s' "$resp" | tr '\n' ' ' | head -c 200)"
        }
        os_cache_put "$plan" "$oci" "$(printf '%s' "$resp" | jq -r '.instance.os // empty' 2>/dev/null)"
        made=1
        break
    done <<< "$os_list"
    [[ "$made" == 1 && -n "$(printf '%s' "$resp" | jq -r '.instance.id // empty' 2>/dev/null)" ]] \
        || die "创建实例失败：所有镜像候选均被拒绝（套餐内存不足或目录异常）"
    printf '%s\n' "$resp" > "$INSTANCE_FILE"
    chmod 600 "$INSTANCE_FILE"
    printf '%s\n' "$resp" > "$STATE_DIR/create-response.json"
    chmod 600 "$STATE_DIR/create-response.json"

    local id
    id="$(echo "$resp" | jq -r '.instance.id')"
    log "实例已创建: id=$id"
    # 开始计费记账（active=true）
    add_billing_record "$id" "$region" "$plan"

    local ip="" v6ip=""
    local tries=0
    log "等待 IP 分配..."
    while (( tries < 20 )); do
        ip="$(echo "$resp" | jq -r '.instance.main_ip // empty')"
        v6ip="$(echo "$resp" | jq -r '.instance.v6_main_ip // empty')"
        [[ -n "$ip" && "$ip" != "0.0.0.0" ]] && break
        sleep 5
        resp="$(vultr_api GET "/v2/instances/${id}")" || die "查询实例 API 调用失败"
        ip="$(echo "$resp" | jq -r '.instance.main_ip // empty')"
        tries=$((tries + 1))
    done
    [[ -n "$ip" && "$ip" != "0.0.0.0" ]] || die "获取 IP 超时"
    # v6 可能比 v4 晚几秒分配，最多补查 4 次
    if [[ -z "$v6ip" || "$v6ip" == "null" ]]; then
        local vt=0
        while (( vt < 4 )); do
            sleep 5
            resp="$(vultr_api GET "/v2/instances/${id}")" || break
            v6ip="$(echo "$resp" | jq -r '.instance.v6_main_ip // empty')"
            [[ -n "$v6ip" && "$v6ip" != "null" ]] && break
            vt=$((vt + 1))
        done
    fi
    [[ -z "$v6ip" || "$v6ip" == "null" ]] && v6ip=""

    log "IP: $ip (v6: ${v6ip:-未分配})"
    log "等待 SSH (v4/v6 双栈探测)..."
    if wait_for_ssh "$ip" "$v6ip"; then
        log "SSH 就绪 via ${SSH_ADDR}"
        # 先记录实例信息（节点已创建成功，必须落盘，避免后续 destroy/update 找不到）
        local root_pw
        root_pw="$(echo "$resp" | jq -r '.instance.default_password // empty')"
        echo "$resp" | jq --arg pw "$ss_password" --arg rpw "$root_pw" \
            --arg v6 "$v6ip" --arg saddr "$SSH_ADDR" \
            '{instance: {id: .instance.id, main_ip: .instance.main_ip, v6_ip: $v6, ssh_addr: $saddr, region: .instance.region, plan: .instance.plan, ss_password: $pw, default_password: $rpw}}' \
            > "$INSTANCE_FILE"
        chmod 600 "$INSTANCE_FILE"
        update_clash_yaml "$ip" "$v6ip" "$ss_password"

        log "等待 shadowsocks 端口(8388)开放..."
        local sstries=0
        while (( sstries < 30 )); do
            if port_open "$SSH_ADDR" "$SS_PORT"; then
                break
            fi
            sstries=$((sstries + 1))
            sleep 5
        done

        if port_open "$SSH_ADDR" "$SS_PORT"; then
            log "shadowsocks 端口已开放"
        else
            log "shadowsocks 端口未开放，尝试 SSH 远程修复（放行防火墙/重启服务）..."
            local fixed=0
            if ssh_exec "$SSH_ADDR" "$root_pw" '
                ufw allow 8388/tcp >/dev/null 2>&1 || true
                ufw allow 8388/udp >/dev/null 2>&1 || true
                systemctl restart shadowsocks-libev >/dev/null 2>&1 || \
                    ss-server -c /etc/shadowsocks-libev/config.json -f /tmp/ss.pid >/dev/null 2>&1 || true
            ' 10; then
                local stries=0
                while (( stries < 12 )); do
                    if port_open "$SSH_ADDR" "$SS_PORT"; then
                        fixed=1
                        break
                    fi
                    stries=$((stries + 1))
                    sleep 5
                done
            fi
            if (( fixed == 1 )); then
                log "远程修复成功，shadowsocks 端口已开放"
            else
                log "⚠ 远端 8388 仍不可达，节点不可用，立即注销止损..."
                destroy_instance
                die "shadowsocks 端口未开放，实例已注销"
            fi
        fi

        # 标记初始化完成（SSH + 8388 均就绪才置 true，供前端控制"更新订阅"按钮）
        local init_ok=0 init_try
        for init_try in 1 2; do
            if ssh_exec "$SSH_ADDR" "$root_pw" 'true' 10 >/dev/null 2>&1 && port_open "$SSH_ADDR" "$SS_PORT"; then
                init_ok=1
                break
            fi
            if (( init_try == 1 )); then sleep 10; fi
        done
        if (( init_ok == 1 )); then
            set_initialized 1
            log "节点初始化完成"
            # 记录上次成功节点：webui 展示（名称/花费/时间）+ 下次创建默认优先该地区 (2026-10-23)
            jq -n --arg id "$id" --arg region "$region" --arg plan "$plan" --arg ip "$ip" \
                --arg label "$INSTANCE_LABEL" --argjson now "$(date +%s)" \
                '{id:$id, region:$region, plan:$plan, ip:$ip, label:$label, succeeded_at:$now}' \
                > "$STATE_DIR/last_success.json" 2>/dev/null || true
            # 部署 Tailscale exit node（失败不致命，见函数内告警）(2026-11-06)
            setup_tailscale_exit "$SSH_ADDR" "$root_pw"
        else
            log "⚠ 节点未完全就绪，立即注销止损..."
            destroy_instance
            die "节点初始化未完成，实例已注销"
        fi

        connect
    else
        # SSH 一直不通 = 该 IP 不可用（出生即被墙/异常）：立即注销止损，不留僵尸实例
        log "SSH 等待超时，节点不可用，立即注销止损..."
        destroy_instance
        die "SSH 等待超时，实例已注销"
    fi
}

destroy_instance() {
    check_api_key
    require_curl; require_jq

    local id=""
    if [[ -f "$INSTANCE_FILE" ]]; then
        id="$(jq -r '.instance.id // empty' "$INSTANCE_FILE" 2>/dev/null || true)"
    fi

    if [[ -z "$id" ]]; then
        # 无本地记录，同步 API
        local list
        list="$(vultr_api GET /v2/instances)"
        id="$(echo "$list" | jq -r '.instances[0].id // empty')"
    fi

    [[ -n "$id" ]] || { log "没有实例，无需销毁"; return; }

    log "销毁实例: id=$id"
    vultr_api DELETE "/v2/instances/${id}" >/dev/null 2>&1 || true
    rm -f "$INSTANCE_FILE"

    # 结算本节点计费
    if [[ -f "$BILL_FILE" ]]; then
        settle_billing
        log "计费已结算，累计历史消耗: $(jq -r '.total_spent' "$BILL_FILE")"
    fi

    # 清空 clash.yaml
    echo "# 当前无活跃节点，请先 create" > "$CLASH_YAML"
    log "已销毁，clash.yaml 已清空"

    # 手动开关制：销毁后出口回 DIRECT，魔戒/STC 只由面板开关手动启用
    set_mihomo_direct
}

get_subscription_url() {
    local url="$SUB_URL" token=""
    # 若已启用订阅 token 鉴权，则自动把订阅路径升级为带 token 的路径
    if [[ -f "$STATE_DIR/sub_token" ]]; then
        token="$(tr -d '[:space:]' < "$STATE_DIR/sub_token")"
    fi
    if [[ -n "$token" && "$url" == */sub/clash.yaml ]]; then
        url="${url%/sub/clash.yaml}/sub/${token}/clash.yaml"
    fi
    printf '%s' "$url"
}

# 标记节点是否初始化完成（SSH + 8388 就绪）。1=完成 0=未完成
set_initialized() {
    local val="$1"
    [[ -f "$INSTANCE_FILE" ]] || return 0
    jq --argjson v "$val" '.instance.initialized = $v' "$INSTANCE_FILE" > "$INSTANCE_FILE.tmp" \
        && mv "$INSTANCE_FILE.tmp" "$INSTANCE_FILE" \
        && chmod 600 "$INSTANCE_FILE"
}

sync_and_update() {
    check_api_key
    require_curl; require_jq

    log "正在从 Vultr API 同步最新节点信息..."

    # 从 API 获取所有实例
    local list
    list="$(vultr_api GET /v2/instances)"
    local count
    count="$(echo "$list" | jq '.instances | length')"

    if [[ "$count" == "0" ]]; then
        log "Vultr 上无运行中的实例"
        echo "# 当前无活跃节点，请先 create" > "$CLASH_YAML"
        echo '{"ok":true,"output":"无活跃节点","subscription_url":"'"$(get_subscription_url)"'"}'
        return
    fi

    # 获取第一个实例信息
    local inst
    inst="$(echo "$list" | jq '.instances[0]')"
    local id ip v6ip region plan ssh_target
    id="$(echo "$inst" | jq -r '.id')"
    ip="$(echo "$inst" | jq -r '.main_ip')"
    v6ip="$(echo "$inst" | jq -r '.v6_main_ip // empty')"
    [[ -z "$v6ip" || "$v6ip" == "null" ]] && v6ip=""
    region="$(echo "$inst" | jq -r '.region')"
    plan="$(echo "$inst" | jq -r '.plan')"
    # SSH 目标优先用创建时探通的地址（可能是 v6），无记录再退回 v4 main_ip
    ssh_target="$ip"
    [[ -f "$INSTANCE_FILE" ]] && ssh_target="$(jq -r '.instance.ssh_addr // .instance.main_ip // empty' "$INSTANCE_FILE" 2>/dev/null || true)"
    [[ -n "$ssh_target" ]] || ssh_target="$ip"

    log "发现实例: id=$id ip=$ip region=$region"

    # 从 VPS SSH 读取真实 ss 密码（真值来源），失败则退回本地记录
    local ss_pw=""
    local password
    password="$(jq -r '.instance.default_password // empty' "$INSTANCE_FILE" 2>/dev/null || true)"
    if [[ -n "$password" ]]; then
        log "SSH 读取 VPS 上的真实 ss 密码..."
        # 顺便放行防火墙并确保 ss 服务运行（幂等，处理 UFW 只放行22的问题）
        ssh_exec "$ssh_target" "$password" "ufw allow 8388/tcp >/dev/null 2>&1 || true; ufw allow 8388/udp >/dev/null 2>&1 || true; systemctl is-active shadowsocks-libev >/dev/null 2>&1 || systemctl restart shadowsocks-libev >/dev/null 2>&1; true" 8
        ss_pw="$(ssh_exec "$ssh_target" "$password" "cat /etc/shadowsocks-libev/config.json | jq -r '.password'" 8 2>/dev/null || true)"
    fi

    # 如果 SSH 拿不到，则用本地保存的密码（创建时保存）
    if [[ -z "$ss_pw" ]]; then
        log "无法 SSH 获取，使用本地记录密码"
        if [[ -f "$INSTANCE_FILE" ]]; then
            ss_pw="$(jq -r '.instance.ss_password // empty' "$INSTANCE_FILE")"
        fi
    fi

    # 如果还是没有密码，生成一个新的并更新 VPS
    if [[ -z "$ss_pw" ]]; then
        log "无法获取密码，生成新密码并更新 VPS..."
        ss_pw="$(gen_ss_password)"
        local password
        password="$(jq -r '.instance.default_password // empty' "$INSTANCE_FILE" 2>/dev/null || true)"
        if [[ -n "$password" ]]; then
            ssh_exec "$ssh_target" "$password" "ufw allow 8388/tcp >/dev/null 2>&1 || true; ufw allow 8388/udp >/dev/null 2>&1 || true; sed -i 's/\"password\":.*/\"password\": \"$ss_pw\",/' /etc/shadowsocks-libev/config.json && systemctl restart shadowsocks-libev" 8 2>/dev/null || true
        fi
    fi

    # 更新本地 instance.json (保留 default_password)
    local rpw=""
    if [[ -f "$STATE_DIR/create-response.json" ]]; then
        rpw="$(jq -r '.instance.default_password // empty' "$STATE_DIR/create-response.json" 2>/dev/null || true)"
    elif [[ -f "$INSTANCE_FILE" ]]; then
        rpw="$(jq -r '.instance.default_password // empty' "$INSTANCE_FILE" 2>/dev/null || true)"
    fi
    jq --arg id "$id" --arg ip "$ip" --arg region "$region" --arg plan "$plan" --arg pw "$ss_pw" --arg rpw "$rpw" \
        --arg v6 "$v6ip" --arg saddr "$ssh_target" \
        '{instance: {id: $id, main_ip: $ip, v6_ip: $v6, ssh_addr: $saddr, region: $region, plan: $plan, ss_password: $pw, default_password: $rpw}}' \
        > "$INSTANCE_FILE" <<< "{}"
    chmod 600 "$INSTANCE_FILE"

    # 更新 clash.yaml（v4+v6 双节点）
    update_clash_yaml "$ip" "$v6ip" "$ss_pw"

    # update 能成功说明节点已就绪，标记初始化完成
    set_initialized 1

    log "订阅已更新: $ip / v6:${v6ip:-无} ($region)"
    connect
    # 订阅变更后强刷本机 mihomo：provider 重载 + Proxy 钉选（AUTO 组会卡死旧引用）
    refresh_mihomo
}

connect() {
    local sub_url
    sub_url="$(get_subscription_url)"

    printf '\n\033[1;32m==== 订阅链接 (固定，填入 Clash / Shadowrocket 即可) ====\033[0m\n'
    printf '\033[1;33m%s\033[0m\n' "$sub_url"

    if [[ -f "$INSTANCE_FILE" ]]; then
        local ip ss_pw v6ip
        ip="$(jq -r '.instance.main_ip // empty' "$INSTANCE_FILE")"
        v6ip="$(jq -r '.instance.v6_ip // empty' "$INSTANCE_FILE")"
        ss_pw="$(jq -r '.instance.ss_password // empty' "$INSTANCE_FILE")"
        [[ -n "$ip" ]] && printf '当前节点 IP: %s (v6: %s)\n' "$ip" "${v6ip:-无}"
        [[ -n "$ss_pw" ]] && printf 'Shadowsocks 密码: %s\n' "$ss_pw"
    fi
    printf '\n\033[1;33m[手动导入] ss:// 链接:\033[0m\n'
    if [[ -f "$INSTANCE_FILE" ]]; then
        local ip pw v6ip
        ip="$(jq -r '.instance.main_ip // empty' "$INSTANCE_FILE")"
        v6ip="$(jq -r '.instance.v6_ip // empty' "$INSTANCE_FILE")"
        pw="$(jq -r '.instance.ss_password // empty' "$INSTANCE_FILE")"
        [[ -n "$ip" && -n "$pw" ]] && printf 'ss://%s@%s:%d#vultr-ss4\n' \
            "$(printf "aes-256-gcm:%s" "$pw" | b64)" "$ip" "$SS_PORT"
        [[ -n "$v6ip" && "$v6ip" != "null" && -n "$pw" ]] && printf 'ss://%s@%s:%d#vultr-ss6\n' \
            "$(printf "aes-256-gcm:%s" "$pw" | b64)" "$v6ip" "$SS_PORT"
    fi
    printf '\n'
}

case "${1:-}" in
    create)     action_create "${2:-}" "${3:-}" ;;
    destroy)    destroy_instance ;;
    connect)    connect ;;
    export)     connect ;;
    status)     connect ;;
    update)     sync_and_update ;;
    guard)      exec bash "$BASE_DIR/node-guard.sh" "${2:-run}" ;;
    *)          echo "用法: $0 {create|destroy|connect|export|status|update|guard}"; exit 1 ;;
esac
