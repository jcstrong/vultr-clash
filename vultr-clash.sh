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
VULTR_OS_ID="${VULTR_OS_ID:-2136}"
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

make_cloudinit() {
    local password="$1"
    local result
    result="$(sed "s/__SS_PASSWORD__/${password}/g" "$CLOUDINIT")"
    printf '%s' "$result"
}

wait_for_ssh() {
    local ip="$1"
    local tries=0
    while (( tries < 30 )); do
        if nc -z -w2 "$ip" 22 2>/dev/null; then
            return 0
        fi
        tries=$((tries + 1))
        sleep 8
    done
    return 1
}

update_clash_yaml() {
    local ip="$1" password="$2"
    cat > "$CLASH_YAML" <<EOF
mixed-port: 7890
allow-lan: false
mode: rule
log-level: info

proxies:
  - name: "vultr-ss"
    type: ss
    server: ${ip}
    port: ${SS_PORT}
    cipher: aes-256-gcm
    password: "${password}"
    udp: true

proxy-groups:
  - name: "Proxy"
    type: select
    proxies:
      - vultr-ss
      - DIRECT

rules:
  - GEOIP,CN,DIRECT
  - MATCH,Proxy
EOF
    log "已更新 clash.yaml"
}

create_instance() {
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
    userdata="$(make_cloudinit "$ss_password" | base64 -w0)"

    log "正在创建实例: region=$region plan=$plan"
    local payload
    payload="$(jq -n \
        --arg region "$region" \
        --arg plan "$plan" \
        --arg os_id "$VULTR_OS_ID" \
        --arg label "$INSTANCE_LABEL" \
        --arg userdata "$userdata" \
        '{region:$region, plan:$plan, os_id:$os_id, label:$label, user_data:$userdata}')"

    local resp
    resp="$(vultr_api POST /v2/instances "$payload")"
    printf '%s\n' "$resp" > "$INSTANCE_FILE"
    chmod 600 "$INSTANCE_FILE"
    printf '%s\n' "$resp" > "$STATE_DIR/create-response.json"
    chmod 600 "$STATE_DIR/create-response.json"

    local id
    id="$(echo "$resp" | jq -r '.instance.id')"
    log "实例已创建: id=$id"
    # 开始计费记账（active=true）
    add_billing_record "$id" "$region" "$plan"

    local ip=""
    local tries=0
    log "等待 IP 分配..."
    while (( tries < 20 )); do
        ip="$(echo "$resp" | jq -r '.instance.main_ip // empty')"
        [[ -n "$ip" && "$ip" != "0.0.0.0" ]] && break
        sleep 5
        resp="$(vultr_api GET "/v2/instances/${id}")"
        ip="$(echo "$resp" | jq -r '.instance.main_ip // empty')"
        tries=$((tries + 1))
    done
    [[ -n "$ip" && "$ip" != "0.0.0.0" ]] || die "获取 IP 超时"

    log "IP: $ip"
    log "等待 SSH..."
    if wait_for_ssh "$ip"; then
        log "SSH 就绪"
        # 先记录实例信息（节点已创建成功，必须落盘，避免后续 destroy/update 找不到）
        local root_pw
        root_pw="$(echo "$resp" | jq -r '.instance.default_password // empty')"
        echo "$resp" | jq --arg pw "$ss_password" --arg rpw "$root_pw" \
            '{instance: {id: .instance.id, main_ip: .instance.main_ip, region: .instance.region, plan: .instance.plan, ss_password: $pw, default_password: $rpw}}' \
            > "$INSTANCE_FILE"
        chmod 600 "$INSTANCE_FILE"
        update_clash_yaml "$ip" "$ss_password"

        log "等待 shadowsocks 端口(8388)开放..."
        local sstries=0
        while (( sstries < 30 )); do
            if nc -z -w2 "$ip" "$SS_PORT" 2>/dev/null; then
                break
            fi
            sstries=$((sstries + 1))
            sleep 5
        done

        if nc -z -w2 "$ip" "$SS_PORT" 2>/dev/null; then
            log "shadowsocks 端口已开放"
        else
            log "shadowsocks 端口未开放，尝试 SSH 远程修复（放行防火墙/重启服务）..."
            local fixed=0
            if sshpass -p "$root_pw" ssh "${SSH_OPTS[@]}" -o ConnectTimeout=10 \
                    root@"$ip" '
                ufw allow 8388/tcp >/dev/null 2>&1 || true
                ufw allow 8388/udp >/dev/null 2>&1 || true
                systemctl restart shadowsocks-libev >/dev/null 2>&1 || \
                    ss-server -c /etc/shadowsocks-libev/config.json -f /tmp/ss.pid >/dev/null 2>&1 || true
            '; then
                local stries=0
                while (( stries < 12 )); do
                    if nc -z -w2 "$ip" "$SS_PORT" 2>/dev/null; then
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
                log "⚠ 远端 8388 仍不可达，但节点已创建成功，现场已保存（可稍后 SSH 检查）"
            fi
        fi

        # 标记初始化完成（SSH + 8388 均就绪才置 true，供前端控制"更新订阅"按钮）
        if sshpass -p "$root_pw" ssh "${SSH_OPTS[@]}" -o ConnectTimeout=10 \
                root@"$ip" 'true' 2>/dev/null && nc -z -w2 "$ip" "$SS_PORT" 2>/dev/null; then
            set_initialized 1
            log "节点初始化完成"
        else
            set_initialized 0
            log "⚠ 节点尚未完全就绪（Update 将在就绪后启用）"
        fi

        connect
    else
        die "SSH 等待超时"
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
}

get_subscription_url() {
    printf '%s' "$SUB_URL"
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
    local id ip region plan
    id="$(echo "$inst" | jq -r '.id')"
    ip="$(echo "$inst" | jq -r '.main_ip')"
    region="$(echo "$inst" | jq -r '.region')"
    plan="$(echo "$inst" | jq -r '.plan')"

    log "发现实例: id=$id ip=$ip region=$region"

    # 从 VPS SSH 读取真实 ss 密码（真值来源），失败则退回本地记录
    local ss_pw=""
    local password
    password="$(jq -r '.instance.default_password // empty' "$INSTANCE_FILE" 2>/dev/null || true)"
    if [[ -n "$password" ]]; then
        log "SSH 读取 VPS 上的真实 ss 密码..."
        # 顺便放行防火墙并确保 ss 服务运行（幂等，处理 UFW 只放行22的问题）
        sshpass -p "$password" ssh "${SSH_OPTS[@]}" -o ConnectTimeout=8 root@"$ip" \
            "ufw allow 8388/tcp >/dev/null 2>&1 || true; ufw allow 8388/udp >/dev/null 2>&1 || true; systemctl is-active shadowsocks-libev >/dev/null 2>&1 || systemctl restart shadowsocks-libev >/dev/null 2>&1; true"
        ss_pw="$(sshpass -p "$password" ssh "${SSH_OPTS[@]}" -o ConnectTimeout=8 root@"$ip" \
            "cat /etc/shadowsocks-libev/config.json | jq -r '.password'" 2>/dev/null || true)"
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
            sshpass -p "$password" ssh "${SSH_OPTS[@]}" -o ConnectTimeout=8 root@"$ip" \
                "ufw allow 8388/tcp >/dev/null 2>&1 || true; ufw allow 8388/udp >/dev/null 2>&1 || true; sed -i 's/\"password\":.*/\"password\": \"$ss_pw\",/' /etc/shadowsocks-libev/config.json && systemctl restart shadowsocks-libev" 2>/dev/null || true
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
        '{instance: {id: $id, main_ip: $ip, region: $region, plan: $plan, ss_password: $pw, default_password: $rpw}}' \
        > "$INSTANCE_FILE" <<< "{}"
    chmod 600 "$INSTANCE_FILE"

    # 更新 clash.yaml
    update_clash_yaml "$ip" "$ss_pw"

    # update 能成功说明节点已就绪，标记初始化完成
    set_initialized 1

    log "订阅已更新: $ip ($region)"
    connect
}

connect() {
    local sub_url
    sub_url="$(get_subscription_url)"

    printf '\n\033[1;32m==== 订阅链接 (固定，填入 Clash / Shadowrocket 即可) ====\033[0m\n'
    printf '\033[1;33m%s\033[0m\n' "$sub_url"

    if [[ -f "$INSTANCE_FILE" ]]; then
        local ip ss_pw
        ip="$(jq -r '.instance.main_ip // empty' "$INSTANCE_FILE")"
        ss_pw="$(jq -r '.instance.ss_password // empty' "$INSTANCE_FILE")"
        [[ -n "$ip" ]] && printf '当前节点 IP: %s\n' "$ip"
        [[ -n "$ss_pw" ]] && printf 'Shadowsocks 密码: %s\n' "$ss_pw"
    fi
    printf '\n\033[1;33m[手动导入] ss:// 链接:\033[0m\n'
    if [[ -f "$INSTANCE_FILE" ]]; then
        local ip pw
        ip="$(jq -r '.instance.main_ip // empty' "$INSTANCE_FILE")"
        pw="$(jq -r '.instance.ss_password // empty' "$INSTANCE_FILE")"
        [[ -n "$ip" && -n "$pw" ]] && printf 'ss://%s@%s:%d#vultr-ss\n' \
            "$(printf "aes-256-gcm:%s" "$pw" | base64 -w0)" "$ip" "$SS_PORT"
    fi
    printf '\n'
}

case "${1:-}" in
    create)     create_instance "${2:-}" "${3:-}" ;;
    destroy)    destroy_instance ;;
    connect)    connect ;;
    export)     connect ;;
    status)     connect ;;
    update)     sync_and_update ;;
    *)          echo "用法: $0 {create|destroy|connect|export|status|update}"; exit 1 ;;
esac
