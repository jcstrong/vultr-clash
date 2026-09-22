#!/usr/bin/env bash
# 节点健康守卫：定期检查当前节点是否被封（SS 8388 + SSH 22 均不可达）。
# 连续失败 GUARD_FAILS 次后自动销毁节点（停止计费）。
# 设计原则：
#   1. 本地无节点 → 直接退出，不做任何事
#   2. Vultr API 自身不可达（401/网络错误） → 跳过本轮，绝不误杀
#   3. 连续失败 N 次才触发销毁，瞬时抖动不算
# 用法: ./node-guard.sh          # 执行一轮检查（适合 cron 每 30 分钟）
#       ./node-guard.sh status   # 查看守卫状态
set -uo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="$BASE_DIR/.state"
INSTANCE_FILE="$STATE_DIR/instance.json"
GUARD_FILE="$STATE_DIR/guard_state.json"
LOG_FILE="$STATE_DIR/progress.log"
SCRIPT="$BASE_DIR/vultr-clash.sh"
SS_PORT=8388
GUARD_FAILS="${GUARD_FAILS:-2}"
GUARD_RECREATE="${GUARD_RECREATE:-1}"          # 被封销毁后自动重建（0=关闭）
GUARD_RECREATE_MAX="${GUARD_RECREATE_MAX:-2}"  # 每日最多自动重建次数（防死循环）
SCHEDULE_FILE="$STATE_DIR/schedule.json"

KEY_FILE="$HOME/.config/vultr-clash/api_key"
VULTR_API_KEY="${VULTR_API_KEY:-}"
if [[ -z "$VULTR_API_KEY" && -f "$KEY_FILE" ]]; then
    VULTR_API_KEY="$(tr -d '[:space:]' < "$KEY_FILE")"
fi

log()  { printf '\033[1;34m[*]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }

append_state_log() {
    local msg="$1"
    mkdir -p "$STATE_DIR"
    printf '[%s] [guard] %s\n' "$(date +%H:%M:%S)" "$msg" >> "$LOG_FILE"
}

bark_push() {
    # bark_push <title> <body> ：读取 schedule.json 的 bark_url 推送；未配置时静默跳过
    local title="$1" body="$2" url
    url="$(jq -r '.bark_url // empty' "$SCHEDULE_FILE" 2>/dev/null || true)"
    [[ -n "$url" ]] || return 0
    if jq -n --arg t "$title" --arg b "$body" '{title:$t, body:$b, group:"vultr-clash"}' \
        | curl -4 -sS --fail-with-body -m 10 -X POST -H "Content-Type: application/json" -d @- "$url" >/dev/null 2>&1; then
        append_state_log "✓ Bark 通知已发送: $title"
    else
        append_state_log "✗ Bark 推送失败: $title"
    fi
}

guard_write() {
    # guard_write <field> <json-value> ：更新 guard_state.json 单个字段（保留其他字段）
    local field="$1" val="$2" tmp="$GUARD_FILE.tmp"
    mkdir -p "$STATE_DIR"
    [[ -f "$GUARD_FILE" ]] || echo '{}' > "$GUARD_FILE"
    jq --arg k "$field" --argjson v "$val" '.[$k] = $v' "$GUARD_FILE" > "$tmp" && mv "$tmp" "$GUARD_FILE"
}

read_recreates_today() {
    # 今日已用掉的自动重建次数（跨日自动清零）
    local today count date
    today="$(date +%F)"
    count="$(jq -r '.recreate_count // 0' "$GUARD_FILE" 2>/dev/null || echo 0)"
    date="$(jq -r '.recreate_date // ""' "$GUARD_FILE" 2>/dev/null || true)"
    [[ "$date" == "$today" ]] || count=0
    echo "${count:-0}"
}

flip_target() {
    # 被封节点套餐 → 对侧 地区/套餐：新泽西$3.5 ↔ 日本$5
    if [[ "$1" == "vc2-1c-1gb" ]]; then
        echo "ewr vc2-1c-0.5gb"
    else
        echo "nrt vc2-1c-1gb"
    fi
}

port_open() {
    local host="$1" port="$2"
    if command -v nc >/dev/null 2>&1; then
        nc -z -w3 "$host" "$port" 2>/dev/null
    elif command -v python3 >/dev/null 2>&1; then
        python3 - "$host" "$port" <<'PY'
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(3)
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

read_fail_count() {
    [[ -f "$GUARD_FILE" ]] || { echo 0; return; }
    jq -r '.fail_count // 0' "$GUARD_FILE" 2>/dev/null || echo 0
}

write_fail_count() {
    local n="$1"
    guard_write fail_count "$n"
    guard_write last_check "\"$(date -Iseconds)\""
}

vultr_instance() {
    # 输出实例 JSON（Vultr 上第一个实例）；不可达/失败时输出空
    curl -4 -sS -m 15 \
        -H "Authorization: Bearer ${VULTR_API_KEY}" \
        https://api.vultr.com/v2/instances 2>/dev/null \
        | jq -r '.instances[0] // empty' 2>/dev/null
}

guard_status() {
    local ip id fails
    ip="$(jq -r '.instance.main_ip // empty' "$INSTANCE_FILE" 2>/dev/null || true)"
    id="$(jq -r '.instance.id // empty' "$INSTANCE_FILE" 2>/dev/null || true)"
    fails="$(read_fail_count)"
    echo "节点 id : ${id:-无}"
    echo "节点 IP : ${ip:-无}"
    echo "本地失败计数: $fails / $GUARD_FAILS"
    [[ -z "$id" ]] && echo "状态: 无节点，守卫空闲"
    return 0
}

run_once() {
    # 1. 无节点 → 空闲
    local id ip
    id="$(jq -r '.instance.id // empty' "$INSTANCE_FILE" 2>/dev/null || true)"
    ip="$(jq -r '.instance.main_ip // empty' "$INSTANCE_FILE" 2>/dev/null || true)"
    if [[ -z "$id" || -z "$ip" ]]; then
        echo "guard: 无节点，跳过"
        return 0
    fi

    # 2. API 自身不可达 → 跳过，不累计、不误杀
    local inst
    inst="$(vultr_instance)"
    if [[ -z "$inst" ]]; then
        warn "guard: Vultr API 不可达或未授权，跳过本轮（避免本地断网/被墙误判）"
        return 0
    fi
    local api_ip api_status
    api_ip="$(printf '%s' "$inst" | jq -r '.main_ip // empty')"
    api_status="$(printf '%s' "$inst" | jq -r '.status // empty')"
    if [[ -n "$api_ip" && "$api_ip" != "$ip" ]]; then
        append_state_log "节点 IP 已变更: $ip → $api_ip，同步更新"
        jq --arg ip "$api_ip" '.instance.main_ip = $ip' "$INSTANCE_FILE" \
            > "$INSTANCE_FILE.tmp" && mv "$INSTANCE_FILE.tmp" "$INSTANCE_FILE"
        ip="$api_ip"
    fi
    if [[ "$api_status" != "active" && "$api_status" != "pending" ]]; then
        append_state_log "Vultr 实例状态非运行中 (status=$api_status)，跳过本轮评测"
        return 0
    fi

    # 3. 连通性检测：SS 8388 与 SSH 22 都不可达才算"被封"
    if port_open "$ip" "$SS_PORT" || port_open "$ip" 22; then
        local fails
        fails="$(read_fail_count)"
        if (( fails > 0 )); then
            write_fail_count 0
            append_state_log "节点恢复正常，失败计数清零"
        fi
        echo "guard: 节点 $ip 可达"
        return 0
    fi

    # 4. 累计失败
    local fails
    fails="$(read_fail_count)"
    fails=$((fails + 1))
    write_fail_count "$fails"
    append_state_log "节点 $ip 不可达 (SS/SSH 均超时)，失败计数 $fails/$GUARD_FAILS"

    if (( fails >= GUARD_FAILS )); then
        local plan region
        plan="$(printf '%s' "$inst" | jq -r '.plan // empty')"
        region="$(printf '%s' "$inst" | jq -r '.region // empty')"
        append_state_log "节点疑似被封，自动销毁..."
        bark_push "❌ 节点被封，已自动销毁" "${region:-未知}/${plan:-未知} 节点 ${ip} 连续 ${GUARD_FAILS} 轮不可达，销毁止损"
        VULTR_API_KEY="$VULTR_API_KEY" bash "$SCRIPT" destroy \
            2>>"$LOG_FILE" | while IFS= read -r l; do
                append_state_log "$l"
            done
        write_fail_count 0

        # --- 被封自动重建：换到对侧地区/套餐（新泽西$3.5 ↔ 日本$5）---
        if [[ "$GUARD_RECREATE" == "1" && -n "$plan" ]]; then
            local rc_today rc_max target t_region t_plan create_rc
            rc_today="$(read_recreates_today)"
            rc_max="$GUARD_RECREATE_MAX"
            if (( rc_today >= rc_max )); then
                append_state_log "⚠ 今日自动重建已达上限 ${rc_max} 次，停止重建（防循环），请手动处理"
                bark_push "⛔ 自动重建已达上限" "今日 ${rc_today} 次重建仍被封，停止自动重建，请手动检查"
            else
                target="$(flip_target "$plan")"
                t_region="${target%% *}"
                t_plan="${target##* }"
                append_state_log "🔁 自动重建: 在对侧配置 ${t_region}/${t_plan} 创建新节点（今日第 $((rc_today + 1))/${rc_max} 次）"
                VULTR_API_KEY="$VULTR_API_KEY" bash "$SCRIPT" create "$t_region" "$t_plan" \
                    2>>"$LOG_FILE" | while IFS= read -r l; do
                        append_state_log "$l"
                    done
                create_rc="${PIPESTATUS[0]}"
                if [[ "$(jq -r '.recreate_date // ""' "$GUARD_FILE" 2>/dev/null)" == "$(date +%F)" ]]; then
                    guard_write recreate_count "$((rc_today + 1))"
                else
                    guard_write recreate_date "\"$(date +%F)\""
                    guard_write recreate_count 1
                fi
                if (( create_rc == 0 )); then
                    append_state_log "✅ 自动重建成功，订阅已切换到新节点"
                    bark_push "✅ 节点已自动重建" "${t_region} 新节点已就绪（原 ${region}/${plan} 被封已销毁），客户端订阅将自动更新"
                else
                    append_state_log "⚠ 自动重建失败 (exit=$create_rc)，请手动检查"
                    bark_push "❌ 自动重建失败" "${t_region} 创建未成功 (exit=$create_rc)，详见管理页日志，请手动处理"
                fi
            fi
        else
            append_state_log "✅ 已自动销毁被封锁节点（未启用自动重建，下一节点请手动 create）"
        fi
    fi
    return 0
}

case "${1:-run}" in
    status) guard_status ;;
    *)      run_once ;;
esac