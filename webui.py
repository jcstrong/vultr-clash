#!/usr/bin/env python3
import json
import html
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(BASE_DIR, "vultr-clash.sh")
KEY_FILE = os.path.expanduser("~/.config/vultr-clash/api_key")
LOG_FILE = os.path.join(BASE_DIR, ".state", "progress.log")
LOCK_FILE = os.path.join(BASE_DIR, ".state", "action.lock")
TIME_FILE = os.path.join(BASE_DIR, ".state", "create-duration.txt")
BILL_FILE = os.path.join(BASE_DIR, ".state", "billing.json")
LAST_SUCCESS_FILE = os.path.join(BASE_DIR, ".state", "last_success.json")
SUB_URL = os.environ.get("SUB_URL", "http://localhost:8888/sub/clash.yaml")
STATE_DIR = os.path.join(BASE_DIR, ".state")
TOKEN_FILE = os.path.join(STATE_DIR, "sub_token")
SUB_CLASH_PATH = os.environ.get("SUB_PATH", "/sub/clash.yaml").lstrip("/")
VERSION_FILE = os.path.join(BASE_DIR, "VERSION")
SCHEDULE_FILE = os.path.join(STATE_DIR, "schedule.json")

def read_version():
    try:
        with open(VERSION_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return "0.0.0"

def read_sub_token():
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""

def rotate_sub_token():
    """轮换订阅 token: 旧链接立即失效, 新链接需重新填入客户端"""
    token = secrets.token_urlsafe(24)
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    os.chmod(TOKEN_FILE, 0o600)
    return token

def resolve_sub_url():
    """订阅链接: 启用 token 鉴权时升级为 /sub/{token}/clash.yaml"""
    base = SUB_URL.rstrip("/")
    token = read_sub_token()
    if not token:
        return base
    if "/" + SUB_CLASH_PATH in base:
        base = base[: base.find("/" + SUB_CLASH_PATH)]
    return f"{base}/sub/{token}/clash.yaml"

# --- 定时任务（自动销毁/自动创建 + Bark 通知）---
SCHEDULE_DEFAULT = {
    "enabled": True,            # 总开关（销毁与创建都受它控制）
    "destroy_time": "00:30",   # 每天自动销毁时刻；置 null 可单独停用销毁
    "create_time": None,       # 预留：每天自动创建时刻；null = 未启用
    "bark_url": "",            # Bark 推送地址，如 https://api.day.app/<你的Key>
    "create_region": "ewr",   # 自动创建时使用的地区/套餐
    "create_plan": "vc2-1c-0.5gb",
    "last_destroy_date": "",  # 调度器记账，勿手改
    "last_create_date": "",
}

def load_schedule():
    sched = dict(SCHEDULE_DEFAULT)
    try:
        with open(SCHEDULE_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            sched.update(saved)
    except (OSError, ValueError):
        pass
    return sched

def save_schedule(sched):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(SCHEDULE_FILE, "w", encoding="utf-8") as f:
        json.dump(sched, f, ensure_ascii=False, indent=2)
    os.chmod(SCHEDULE_FILE, 0o600)

def bark_notify(title, body):
    """通过 Bark 推送到手机；未配置时仅写日志。返回是否成功"""
    url = (load_schedule().get("bark_url") or "").strip()
    if not url:
        append_log("⚠ 未配置 bark_url（.state/schedule.json），跳过 Bark 通知")
        return False
    # 夜间免打扰 00:30-07:30：passive 静默投递（不响铃不亮屏，仍进通知中心）
    import time as _time
    _m = _time.localtime()
    payload = {"title": title, "body": body, "group": "vultr-clash"}
    if 30 <= _m.tm_hour * 60 + _m.tm_min < 450:
        payload["level"] = "passive"
    payload = json.dumps(payload)
    try:
        r = subprocess.run(
            ["curl", "-4", "-sS", "--fail-with-body", "--max-time", "10",
             "-X", "POST", "-H", "Content-Type: application/json",
             "-d", payload, url],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if r.returncode == 0:
            append_log(f"✓ Bark 通知已发送: {title}")
            return True
        append_log(f"✗ Bark 推送失败: {(r.stderr or r.stdout).strip()[:120]}")
        return False
    except Exception as e:
        append_log(f"✗ Bark 推送异常: {e}")
        return False

def node_exists():
    """Vultr 账号下是否有实例：True/False；API 异常时返回 None（调用方应重试而非跳过）"""
    try:
        return bool(get_vultr_instances())
    except Exception as e:
        append_log(f"⚠ 定时任务查询实例失败: {e}")
        return None

def total_spent_str():
    """读取计费台账累计消耗（用于通知文案，失败返回空串）"""
    try:
        with open(BILL_FILE, encoding="utf-8") as f:
            return f"，累计消耗 ${json.load(f).get('total_spent', 0.0):.2f}"
    except Exception:
        return ""

PLAN_HOURLY = {
    "vc2-1c-0.5gb": 3.5 / 720.0,
    "vc2-1c-1gb": 5.0 / 720.0,
}
PLAN_MONTHLY = {
    "vc2-1c-0.5gb": 3.5,
    "vc2-1c-1gb": 5.0,
}

# 全局锁：防止并发执行
action_lock = threading.Lock()
current_action = None

def get_api_key():
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE) as f:
            k = f.read().strip()
        if k:
            return k
    return os.environ.get("VULTR_API_KEY", "")

def save_api_key(key):
    os.makedirs(os.path.dirname(KEY_FILE), exist_ok=True)
    with open(KEY_FILE, "w") as f:
        f.write(key.strip())
    os.chmod(KEY_FILE, 0o600)

def get_vultr_instances():
    key = get_api_key()
    if not key:
        raise RuntimeError("未设置 Vultr API Key")
    result = subprocess.run(
        [
            "curl", "-4", "-sS", "--fail-with-body",
            "-H", f"Authorization: Bearer {key}",
            "https://api.vultr.com/v2/instances",
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Vultr API 请求失败")
    return json.loads(result.stdout).get("instances", [])

def get_last_success():
    """上次成功创建的节点：last_success.json + billing.json 的花费/使用时长"""
    if not os.path.exists(LAST_SUCCESS_FILE):
        return None
    try:
        with open(LAST_SUCCESS_FILE, encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, ValueError):
        return None
    if not info.get("region"):
        return None
    info["active"] = False
    info["cost"] = None
    info["duration_sec"] = None
    if os.path.exists(BILL_FILE):
        try:
            with open(BILL_FILE, encoding="utf-8") as f:
                bill = json.load(f)
            for n in bill.get("nodes", []):
                if n.get("id") == info.get("id") and n.get("started"):
                    hourly = 3.5 / 720 if n.get("plan") == "vc2-1c-0.5gb" else 5.0 / 720
                    end = time.time() if n.get("active") else (n.get("ended") or time.time())
                    if n.get("active"):
                        info["active"] = True
                    dur = max(0.0, end - float(n["started"]))
                    info["duration_sec"] = int(dur)
                    info["cost"] = round(dur / 3600.0 * hourly, 4)
                    break
        except (OSError, ValueError):
            pass
    return info

def get_billing():
    """返回余额、当前节点费用、历史节点消耗"""
    key = get_api_key()
    if not key:
        raise RuntimeError("未设置 Vultr API Key")

    # 1) 账户余额 / 待付款
    balance = None
    pending = None
    acct = subprocess.run(
        [
            "curl", "-4", "-sS", "--fail-with-body",
            "-H", f"Authorization: Bearer {key}",
            "https://api.vultr.com/v2/account",
        ],
        capture_output=True, text=True, timeout=20, check=False,
    )
    if acct.returncode == 0:
        a = json.loads(acct.stdout).get("account", {})
        balance = a.get("balance")
        pending = a.get("pending_charges")

    # 2) 历史节点消耗：从 billing.json 累计已销毁节点费用
    history_total = 0.0
    if os.path.exists(BILL_FILE):
        try:
            with open(BILL_FILE, encoding="utf-8") as f:
                hist = json.load(f)
            history_total += float(hist.get("total_spent", 0.0))
        except (OSError, ValueError):
            pass

    # 3) 当前节点费用：按计划小时价 × 运行时长
    current_cost = 0.0
    current_plan = None
    try:
        for inst in get_vultr_instances():
            created = inst.get("date_created") or ""
            plan = inst.get("plan", "")
            hourly = PLAN_HOURLY.get(plan, 0.0)
            current_plan = PLAN_MONTHLY.get(plan)
            try:
                from datetime import datetime
                created_dt = datetime.strptime(created.replace("Z", "+00:00"), "%Y-%m-%dT%H:%M:%S%z")
                from datetime import timezone
                hours = (datetime.now(timezone.utc) - created_dt).total_seconds() / 3600.0
            except Exception:
                hours = 0.0
            current_cost += round(hours * hourly, 4)
    except Exception:
        pass

    return {
        "balance": balance,
        "pending": pending,
        "current_cost": round(current_cost, 4),
        "current_plan_monthly": current_plan,
        "history_total": round(history_total, 4),
        "total_all": round(history_total + current_cost, 4),
    }

def validate_api_key(key):
    """校验 Vultr API Key: 调用 /v2/account 确认有效后才允许保存"""
    key = (key or "").strip()
    if not key:
        return {"ok": False, "output": "未填写 API Key"}
    r = subprocess.run(
        ["curl", "-4", "-sS", "--fail-with-body",
         "-H", f"Authorization: Bearer {key}",
         "https://api.vultr.com/v2/account"],
        capture_output=True, text=True, timeout=20, check=False,
    )
    if r.returncode == 0:
        try:
            a = json.loads(r.stdout).get("account", {})
        except Exception:
            a = {}
        return {"ok": True, "account": a}
    msg = (r.stderr.strip() or r.stdout.strip() or "校验失败")[:200]
    try:
        err = json.loads(r.stdout).get("error", "")
        if err:
            msg = err[:200]
    except Exception:
        pass
    return {"ok": False, "output": msg}

def run_selftest():
    """环境自检: 依赖二进制 / 平台 / Tailscale IP / API Key 是否存在"""
    items = [
        ("curl", "调用 Vultr API"),
        ("jq", "JSON 解析"),
        ("python3", "运行 webui/serve_sub"),
        ("openssl", "生成 SS 密码"),
        ("nc", "端口探活(可选)"),
        ("sshpass", "密码 SSH(缺失时回退 expect)"),
        ("expect", "密码 SSH 兜底(macOS 自带)"),
        ("tailscale", "订阅私有分发(可选)"),
    ]
    checks = []
    for name, desc in items:
        p = shutil.which(name)
        checks.append({"name": name, "desc": desc, "present": bool(p), "path": p or ""})
    ts_ip = ""
    ts = shutil.which("tailscale")
    if ts:
        r = subprocess.run([ts, "ip", "-4"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            ts_ip = (r.stdout.strip().split("\n") or [""])[0]
    return {
        "ok": True,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": ".".join(map(str, sys.version_info[:3])),
        "checks": checks,
        "tailscale_ip": ts_ip,
        "vultr_key_ok": bool(get_api_key()),
    }

def get_instance_status():
    instances = get_vultr_instances()
    if not instances:
        return None

    instance = instances[0]
    inst_file = os.path.join(BASE_DIR, ".state", "instance.json")
    if os.path.exists(inst_file):
        try:
            with open(inst_file) as f:
                local = json.load(f).get("instance", {})
            if local.get("id") == instance.get("id"):
                instance["ss_password"] = local.get("ss_password", "")
                # 旧节点未写 initialized 字段时，默认视为已就绪
                initialized = local.get("initialized")
                instance["initialized"] = True if initialized is None else bool(initialized)
        except (OSError, ValueError):
            pass
    duration = get_create_duration()
    if duration is not None:
        instance["create_duration"] = duration
    return instance

def get_mihomo_status():
    """本机 mihomo 出口链与机场 provider 存活状态（vultr 无节点时也展示兜底出口）"""
    import urllib.request
    cfg = os.path.expanduser("~/.config/mihomo/config.yaml")
    secret = ""
    try:
        with open(cfg, encoding="utf-8") as f:
            for line in f:
                if line.startswith("secret:"):
                    secret = line.split(":", 1)[1].strip().strip('"').strip("'")
                    break
    except OSError:
        pass
    def _get(path):
        req = urllib.request.Request(
            "http://127.0.0.1:9090" + path,
            headers={"Authorization": "Bearer " + secret})
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    try:
        p = (_get("/proxies").get("proxies") or {})
        provs = (_get("/providers/proxies").get("providers") or {})
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}
    chain, cur, seen = [], "Proxy", set()
    while cur in p and cur not in seen and len(chain) < 5:
        seen.add(cur)
        cur = p[cur].get("now", "") or ""
        if cur:
            chain.append(cur)
    exit_node = chain[-1] if chain else ""
    providers = []
    for name, pv in provs.items():
        # 只显示真订阅源（HTTP/File），滤掉 Compatible 内部组
        if pv.get("vehicleType") not in ("HTTP", "File", "Local"):
            continue
        pl = pv.get("proxies") or []
        if not pl:
            continue
        providers.append({
            "name": name,
            "alive": sum(1 for x in pl if x.get("alive")),
            "total": len(pl),
            "has_exit": any(x.get("name") == exit_node for x in pl),
        })
    providers.sort(key=lambda x: (not x["has_exit"], x["name"]))
    now = p.get("Proxy", {}).get("now", "")
    ai = p.get("AI", {}).get("now", "")
    return {"ok": True, "now": now, "ai": ai, "chain": " → ".join(chain), "exit": exit_node,
            "providers": providers}

def set_mihomo_exit(target, group="Proxy"):
    """面板手动开关：PUT mihomo 组选择（Proxy 主出口 / AI 专用组），不做任何自动兜底"""
    import urllib.request
    cfg = os.path.expanduser("~/.config/mihomo/config.yaml")
    secret = ""
    try:
        with open(cfg, encoding="utf-8") as f:
            for line in f:
                if line.startswith("secret:"):
                    secret = line.split(":", 1)[1].strip().strip('"').strip("'")
                    break
    except OSError:
        pass
    req = urllib.request.Request(
        "http://127.0.0.1:9090/proxies/" + group,
        data=json.dumps({"name": target}).encode("utf-8"),
        headers={"Authorization": "Bearer " + secret,
                 "Content-Type": "application/json"},
        method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            r.status  # 非 2xx 会抛 HTTPError 走 except
    except Exception as e:
        return {"ok": False, "output": str(e)[:160]}
    append_log("🎚 出口切换 → " + target + ("（AI组）" if group == "AI" else ""))
    return {"ok": True, "mihomo": get_mihomo_status()}

def get_ai_routes():
    """AI 站点实测路由：经 mihomo(127.0.0.1:7890) 访问各站点 Cloudflare trace 端点，
    返回每个站点实际出口 IP 与国家——让"走的是哪"可见（2026-11-12 用户需求）"""
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor
    targets = [("ChatGPT", "chatgpt.com"), ("Codex", "api.openai.com"), ("Claude", "claude.ai")]
    op = urllib.request.build_opener(urllib.request.ProxyHandler(
        {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}))

    def _probe(pair):
        name, host = pair
        item = {"name": name, "host": host, "ok": False, "ip": "", "loc": "", "err": ""}
        try:
            req = urllib.request.Request("https://" + host + "/cdn-cgi/trace",
                                         headers={"User-Agent": "curl/8"})
            with op.open(req, timeout=6) as resp:
                body = resp.read().decode("utf-8", "replace")
            for line in body.splitlines():
                if line.startswith("ip="):
                    item["ip"] = line[3:]
                elif line.startswith("loc="):
                    item["loc"] = line[4:]
            item["ok"] = bool(item["ip"])
        except Exception as e:
            item["err"] = str(e)[:80]
        return item

    with ThreadPoolExecutor(max_workers=3) as ex:
        routes = list(ex.map(_probe, targets))
    return {"ok": True, "routes": routes}

def render_page():
    values = {
        "__NODE_DOT_CLASS__": "off",
        "__NODE_LABEL__": "加载中…",
        "__NODE_CARD_CLASS__": "inactive",
        "__NODE_NAME__": "加载中…",
        "__NODE_STATUS_CLASS__": "off",
        "__NODE_STATUS__": "…",
        "__NODE_REGION__": "-",
        "__NODE_PLAN__": "-",
        "__NODE_IP__": "-",
        "__NODE_PASSWORD__": "-",
        "__NODE_CREATE_TIME__": "-",
    }

    page = PAGE
    for marker, value in values.items():
        page = page.replace(marker, html.escape(str(value)))
    page = page.replace("__SUB_URL__", html.escape(resolve_sub_url()))
    page = page.replace("__PROGRESS_LOG__", html.escape(read_log()))

    # 费用信息 SSR 占位：真实数据由前端 loadCost() 加载
    page = page.replace("__COST_BALANCE__", "加载中…")
    page = page.replace("__COST_CURRENT__", "-")
    page = page.replace("__COST_HISTORY__", "-")
    return page

def render_setup_page():
    page = SETUP_PAGE
    page = page.replace("__VERSION__", html.escape(read_version()))
    page = page.replace("__SUB_URL_HINT__", html.escape(resolve_sub_url()))
    return page

SETUP_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>vultr-clash 初始化</title>
<style>
:root{--bg:#0f1117;--card:#161a23;--border:#262c3a;--text:#e6e6e6;--muted:#888;--accent:#2f6fed;--green:#4ade80}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,"SF Pro","Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--text);min-height:100dvh;display:flex;align-items:center;justify-content:center;padding:24px}
.wrap{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:28px;max-width:460px;width:100%}
h1{font-size:18px;margin-bottom:6px}
.sub{font-size:12px;color:var(--muted);margin-bottom:20px;line-height:1.5}
label{display:block;font-size:13px;color:var(--muted);margin-bottom:8px}
input{width:100%;background:#0d1117;border:1px solid var(--border);border-radius:8px;padding:12px;color:#fff;font-size:13px;font-family:monospace;margin-bottom:12px}
input:focus{outline:none;border-color:var(--accent)}
.btns{display:flex;gap:8px;margin-bottom:12px}
.btn{flex:1;border:none;border-radius:8px;padding:11px;font-size:14px;font-weight:500;cursor:pointer;color:#fff;transition:all .12s}
.btn:active{transform:scale(.97)}
.btn-p{background:var(--accent)}.btn-s{background:#2b3245;border:1px solid var(--border);color:#ddd}
pre{background:#0a0c11;border:1px solid var(--border);border-radius:8px;padding:12px;font-size:12px;line-height:1.6;color:#aaa;white-space:pre-wrap;word-break:break-all;min-height:20px;max-height:240px;overflow:auto}
.ok{color:var(--green)}
    .subbtn{flex:1;padding:10px 0;border-radius:10px;border:1px solid #2a2e37;background:#1b1e24;color:#9aa0a6;font-size:14px;cursor:pointer}
    .subbtn.active{background:#10b981;border-color:#10b981;color:#fff;font-weight:600}
</style>
</head>
<body>
<div class="wrap">
  <h1>⚡ vultr-clash v__VERSION__</h1>
  <div class="sub">管理面板尚未配置 Vultr API Key。填写后将自动校验，通过后进入管理页。<br>预计订阅链接形如: __SUB_URL_HINT__</div>
  <label>Vultr API Key（在 my.vultr.com/settings 创建）</label>
  <input type="password" id="key" placeholder="粘贴 API Key" autocomplete="off">
  <div class="btns">
    <button class="btn btn-s" onclick="runSelftest()">环境自检</button>
    <button class="btn btn-p" onclick="saveKey()">保存并校验</button>
  </div>
  <pre id="out">就绪。可先运行「环境自检」确认依赖齐全。</pre>
</div>
<script>
async function api(payload) {
  const r = await fetch("/api", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify(payload)});
  return r.json();
}
async function saveKey() {
  const key = document.getElementById('key').value.trim();
  const out = document.getElementById('out');
  if (!key) { out.textContent = '请先粘贴 API Key'; return; }
  out.textContent = '校验中…';
  const j = await api({action:'savekey', key});
  if (j.ok) {
    out.innerHTML = '<span class="ok">✓ API Key 有效并已保存</span>，即将进入管理页…';
    setTimeout(() => location.reload(), 800);
  } else {
    out.textContent = '✗ 校验失败: ' + (j.output || '未知错误');
  }
}
async function runSelftest() {
  const out = document.getElementById('out');
  out.textContent = '自检中…';
  const j = await api({action:'selftest'});
  const lines = ['平台: ' + (j.platform||''), '机器: ' + (j.machine||'') + ' · Python ' + (j.python||'')];
  (j.checks||[]).forEach(c => lines.push((c.present?'✓ ':'✗ ') + c.name + (c.path?' ('+c.path+')':'') + ' — ' + (c.desc||'')));
  if (j.tailscale_ip) lines.push('Tailscale IP: ' + j.tailscale_ip);
  if (j.vultr_key_ok) lines.push('Vultr API Key: 已配置');
  out.textContent = lines.join('\n');
}
document.getElementById('key').addEventListener('keydown', e => { if (e.key === 'Enter') saveKey(); });
async function doSwitchSub(mode){
  const j = await api({action:'switch-sub', mode});
  if(j.ok) setLight(mode); else alert('失败: '+j.output);
}
async function doFetchMojie(){
  const j = await api({action:'fetch-mojie'});
  alert(j.ok ? '魔戒节点已刷新' : ('失败: '+j.output));
}
async function doSaveStc(){
  const c = document.getElementById('stcBox').value;
  if(!c.trim()) return alert('请先粘贴订阅内容');
  const j = await api({action:'save-stc', content: c});
  alert(j.ok ? 'STC 订阅已导入' : ('失败: '+j.output));
}
function setLight(mode){
  document.getElementById('subv').classList.toggle('active', mode==='vultr'||mode==='all');
  document.getElementById('subm').classList.toggle('active', mode==='mojie');
  document.getElementById('subs').classList.toggle('active', mode==='stc');
}
async function loadSubMode(){
  try{ const j = await api({action:'sub-status'}); if(j.ok) setLight(j.mode); }catch(e){}
}
loadSubMode();
}
async function doFetchMojie(){
  const j = await api({action:'fetch-mojie'});
  alert(j.ok ? '魔戒节点已刷新' : ('失败: '+j.output));
}
async function doSaveStc(){
  const c = document.getElementById('stcBox').value;
  if(!c.trim()) return alert('请先粘贴订阅内容');
  const j = await api({action:'save-stc', content: c});
  alert(j.ok ? 'STC 订阅已导入' : ('失败: '+j.output));
}
async function loadSubMode(){
  try{ const j = await api({action:'sub-status'}); if(j.ok) document.getElementById('subModeBadge').textContent = '当前: ' + (j.mode==='all' ? '全自动(三源)' : j.mode); }catch(e){}
}
loadSubMode();
  </script>
</body>
</html>"""

def append_log(msg):
    """追加日志到持久化文件"""
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    timestamp = time.strftime("%H:%M:%S")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] {msg}\n")

def read_log():
    """读取最近 100 行日志"""
    if not os.path.exists(LOG_FILE):
        return "准备就绪，点击上方按钮开始操作"
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()
    return "".join(lines[-100:])  # 最近 100 行

def clear_log():
    """清空日志"""
    if os.path.exists(LOG_FILE):
        os.remove(LOG_FILE)

def save_create_duration(seconds):
    """保存创建用时（秒）"""
    try:
        with open(TIME_FILE, "w") as f:
            f.write(f"{seconds:.0f}")
    except OSError:
        pass

def get_create_duration():
    """读取上次创建用时（秒），不存在返回 None"""
    try:
        with open(TIME_FILE) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None

def is_action_running():
    """检查是否有操作正在运行"""
    return os.path.exists(LOCK_FILE)

def set_action_lock(action):
    """设置操作锁"""
    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    with open(LOCK_FILE, "w") as f:
        f.write(action)

def release_action_lock():
    """释放操作锁"""
    if os.path.exists(LOCK_FILE):
        os.remove(LOCK_FILE)

def run_action(action, region="", plan=""):
    """后台运行脚本，实时写入进度日志"""
    global current_action
    
    # 检查是否已有操作在运行
    if not action_lock.acquire(blocking=False):
        return {"ok": False, "output": "有操作正在执行中，请稍候", "exit": -1}
    
    try:
        set_action_lock(action)
        current_action = action
        append_log(f"开始执行: {action}")

        if action == "create":
            create_start = time.time()
        else:
            create_start = None
        
        env = dict(os.environ)
        env["VULTR_API_KEY"] = get_api_key()
        env["SUB_URL"] = resolve_sub_url()
        cmd = ["bash", SCRIPT, action]
        if region:
            cmd.append(region)
        if plan:
            cmd.append(plan)
        
        # 实时捕获输出并写入日志
        p = subprocess.Popen(cmd, cwd=BASE_DIR, env=env, 
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
        
        output_lines = []
        for line in iter(p.stdout.readline, ''):
            if line:
                clean_line = line.rstrip()
                # 移除 ANSI 颜色码
                import re
                clean_line = re.sub(r'\x1b\[[0-9;]+m', '', clean_line)
                if clean_line:
                    append_log(clean_line)
                    output_lines.append(clean_line)
        
        p.wait()
        
        if p.returncode == 0:
            append_log(f"✓ {action} 完成")
            if action == "create" and create_start is not None:
                duration = time.time() - create_start
                save_create_duration(duration)
                append_log(f"ℹ 创建用时 {duration:.0f} 秒")
        else:
            append_log(f"✗ {action} 失败 (exit={p.returncode})")
        
        return {"ok": p.returncode == 0, "output": "\n".join(output_lines), "exit": p.returncode}
        
    except subprocess.TimeoutExpired:
        append_log(f"✗ {action} 超时")
        return {"ok": False, "output": "操作超时", "exit": -1}
    except Exception as e:
        append_log(f"✗ {action} 错误: {str(e)}")
        return {"ok": False, "output": str(e), "exit": -1}
    finally:
        current_action = None
        release_action_lock()
        action_lock.release()

def scheduler_loop():
    """定时调度线程：按 schedule.json 自动销毁/创建节点。
    - 每 20 秒轮询一次，重新读取配置（改 schedule.json 无需重启）
    - 命中目标时刻且当天未执行过才触发；触发时有任务在跑则同分钟内重试
    - 销毁/创建的结果通过 Bark 通知（已配置时）；无节点则静默跳过
    """
    append_log("⏰ 定时调度器已启动")
    while True:
        try:
            sched = load_schedule()
            if sched.get("enabled"):
                now_hm = time.strftime("%H:%M")
                today = time.strftime("%Y-%m-%d")

                # ---- 自动销毁 ----
                dt = (sched.get("destroy_time") or "").strip()
                if dt and now_hm == dt and sched.get("last_destroy_date") != today:
                    exists = node_exists()
                    if exists is False:
                        append_log(f"⏰ {dt} 定时销毁：当前无节点，跳过")
                        sched["last_destroy_date"] = today
                        save_schedule(sched)
                    elif exists is True:
                        res = run_action("destroy")
                        busy = res.get("exit") == -1 and "正在执行" in (res.get("output") or "")
                        if not busy:
                            sched["last_destroy_date"] = today
                            save_schedule(sched)
                            if res.get("ok"):
                                bark_notify("✅ 节点已自动销毁",
                                            f"定时任务（{dt}）已销毁节点并结算计费{total_spent_str()}，订阅已停止")
                            else:
                                bark_notify("❌ 自动销毁失败",
                                            ((res.get("output") or "")[:200] or "详见管理页日志") + " — 节点仍在计费，请手动处理")
                        # busy：不标记日期，20 秒后在本分钟内重试

                # ---- 自动创建（预留）----
                ct = (sched.get("create_time") or "").strip()
                if ct and now_hm == ct and sched.get("last_create_date") != today:
                    exists = node_exists()
                    if exists is True:
                        append_log(f"⏰ {ct} 定时创建：已有节点，跳过")
                        sched["last_create_date"] = today
                        save_schedule(sched)
                    elif exists is False:
                        # create_region/plan 留空 → 不传参 → 脚本按「上次成功地区」优先（v0.5.0, 2026-11-03）
                        res = run_action("create",
                                         (sched.get("create_region") or "").strip(),
                                         (sched.get("create_plan") or "").strip())
                        busy = res.get("exit") == -1 and "正在执行" in (res.get("output") or "")
                        if not busy:
                            sched["last_create_date"] = today
                            save_schedule(sched)
                            if res.get("ok"):
                                bark_notify("✅ 节点已自动创建", f"定时任务（{ct}）创建完成，订阅已更新")
                            else:
                                bark_notify("❌ 自动创建失败", (res.get("output") or "")[:200] or "详见管理页日志")
        except Exception as e:
            append_log(f"✗ 定时调度器异常: {e}")
        time.sleep(20)

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>SS Node Manager</title>
<style>
:root{--bg:#0f1117;--card:#161a23;--border:#262c3a;--text:#e6e6e6;--muted:#888;--accent:#2f6fed;--green:#1f7a3d;--red:#a33;--radius:12px}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,"SF Pro","Segoe UI",Roboto,system-ui,sans-serif;background:var(--bg);color:var(--text);min-height:100dvh}

/* 顶栏 */
header{background:linear-gradient(135deg,#1a2030,#161a23);border-bottom:1px solid var(--border);padding:16px 24px;display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
header h1{font-size:18px;font-weight:600;white-space:nowrap}
.header-right{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.badge{display:inline-flex;align-items:center;gap:6px;background:var(--card);border:1px solid var(--border);border-radius:8px;padding:6px 12px;font-size:13px;cursor:pointer;transition:all .15s}
.badge:hover{border-color:var(--accent)}
.badge .dot{width:8px;height:8px;border-radius:50%;background:#555}
.badge .dot.on{background:#4ade80;box-shadow:0 0 6px #4ade8060}
.badge .dot.off{background:#f87171}

/* 主体 */
.container{max-width:600px;margin:0 auto;padding:20px 16px 40px}

/* 订阅卡片 */
.sub-card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:20px;margin-bottom:16px;text-align:center;position:relative;overflow:hidden}
.sub-card::before{content:'';position:absolute;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg,var(--accent),#4ade80)}
.sub-label{font-size:12px;color:var(--muted);margin-bottom:10px;letter-spacing:1px;text-transform:uppercase}
.sub-url-wrap{position:relative;background:#0d1117;border:1px solid var(--border);border-radius:8px;padding:12px 44px 12px 12px;cursor:pointer;transition:all .15s}
.sub-url-wrap:hover{border-color:var(--accent)}
.sub-url-text{font-family:ui-monospace,"SF Mono","Cascadia Mono",monospace;font-size:13px;color:#7ec8ff;word-break:break-all;line-height:1.4}
.sub-url-mask{font-family:ui-monospace,"SF Mono",monospace;font-size:14px;color:#7ec8ff;letter-spacing:2px}
.copy-btn{position:absolute;right:6px;top:50%;transform:translateY(-50%);background:var(--accent);border:none;border-radius:6px;padding:6px 10px;font-size:12px;color:#fff;cursor:pointer;transition:all .15s}
.copy-btn:hover{background:#1e57d4}
.copy-btn:active{transform:translateY(-50%) scale(.95)}
.copy-hint{font-size:11px;color:var(--muted);margin-top:8px;transition:opacity .3s}
.copy-hint.show{color:#4ade80}

/* 节点状态 */
.node-card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:16px 20px;margin-bottom:16px}
.node-card.active{border-color:#4ade8040;background:linear-gradient(135deg,#0d1a0d,#161a23)}
.node-card.inactive{border-color:var(--border);opacity:.7}
.node-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
.node-name{font-size:15px;font-weight:500}
.node-status{font-size:12px;padding:3px 10px;border-radius:20px;font-weight:500}
.node-status.on{background:#4ade8020;color:#4ade80;border:1px solid #4ade8040}
.node-status.off{background:#f8717120;color:#f87171;border:1px solid #f8717140}
.node-info{display:grid;grid-template-columns:auto 1fr;gap:6px 16px;font-size:13px}
.node-info dt{color:var(--muted);white-space:nowrap}
.node-info dd{font-family:ui-monospace,monospace;word-break:break-all;color:#ccc}

/* 操作区 */
.action-card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:20px;margin-bottom:16px}
.row{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap}
select{flex:1;min-width:0;background:#1d2029;color:#fff;border:1px solid var(--border);border-radius:8px;padding:10px 12px;font-size:14px;cursor:pointer}
select:focus{outline:none;border-color:var(--accent)}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;border:none;border-radius:8px;padding:11px 20px;font-size:14px;font-weight:500;cursor:pointer;transition:all .12s;flex:1;min-width:0}
.btn:active{transform:scale(.97)}
.btn:disabled{opacity:.5;cursor:not-allowed;transform:none}
.btn-primary{background:var(--accent);color:#fff}.btn-primary:hover{background:#1e57d4}
.btn-secondary{background:#2b3245;color:#ddd;border:1px solid var(--border)}.btn-secondary:hover{background:#37415a}
.btn-danger{background:var(--red);color:#fff}.btn-danger:hover{background:#c22}
.btn-green{background:var(--green);color:#fff}.btn-green:hover{background:#166030}
.btn-full{width:100%;flex:none}

/* 日志 */
.log-card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;margin-bottom:16px}
.log-header{display:flex;justify-content:space-between;align-items:center;padding:12px 16px;border-bottom:1px solid var(--border)}
.log-header h3{font-size:14px;font-weight:500;color:var(--muted)}
.log-body{background:#0a0c11;padding:12px 16px;max-height:260px;overflow:auto;font-size:12px;line-height:1.6;font-family:ui-monospace,monospace;white-space:pre-wrap;word-break:break-all;color:#aaa;min-height:60px}
.log-body::-webkit-scrollbar{width:6px}
.log-body::-webkit-scrollbar-thumb{background:#333;border-radius:3px}

/* API Key 弹窗 */
.modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:100;align-items:center;justify-content:center;padding:20px}
.modal-overlay.show{display:flex}
.modal{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:24px;width:100%;max-width:420px}
.modal h3{font-size:16px;margin-bottom:16px}
.modal input{width:100%;background:#0d1117;border:1px solid var(--border);border-radius:8px;padding:10px 12px;color:#fff;font-size:13px;font-family:monospace;margin-bottom:12px}
.modal input:focus{outline:none;border-color:var(--accent)}
.modal-actions{display:flex;gap:8px;justify-content:flex-end}

/* Loading spinner */
.spinner{display:inline-block;width:14px;height:14px;border:2px solid transparent;border-top-color:currentColor;border-radius:50%;animation:spin .6s linear infinite;vertical-align:middle;margin-right:6px}
@keyframes spin{to{transform:rotate(360deg)}}
.spinner-lg{width:20px;height:20px;border-width:3px}

/* 移动端适配 */
@media(max-width:480px){
  header{padding:12px 16px}
  header h1{font-size:16px}
  .container{padding:16px 12px 32px}
  .sub-card{padding:16px 14px}
  .node-card{padding:14px 16px}
  .action-card{padding:16px 14px}
  .btn{padding:12px 16px;font-size:13px}
}
</style>
</head>
<body>

<header>
  <h1>⚡ SS 节点管理</h1>
  <div class="header-right">
    <div class="badge" id="nodeBadge" onclick="loadStatus()" title="点击刷新节点状态">
      <span class="dot __NODE_DOT_CLASS__" id="nodeDot"></span>
      <span id="nodeLabel">__NODE_LABEL__</span>
    </div>
    <div class="badge" onclick="showModal()" title="设置 API Key">🔑 API Key</div>
  </div>
</header>

<div class="container">

  <div class="action-card">
    <div class="row">
      <select id="region" onchange="document.getElementById('plan').value = (this.value==='ewr') ? 'vc2-1c-0.5gb' : 'vc2-1c-1gb'">
        <option value="ewr" selected>ewr · 新泽西 (New Jersey)</option>
        <option value="nrt">nrt · 东京 (Tokyo)</option>
        <option value="sgp">sgp · 新加坡 (Singapore)</option>
        <option value="icn">icn · 首尔 (Seoul)</option>
        <option value="lax">lax · 洛杉矶 (LA)</option>
        <option value="sjc">sjc · 硅谷圣何塞 (San Jose)</option>
        <option value="sea">sea · 西雅图 (Seattle)</option>
        <option value="fra">fra · 法兰克福 (Frankfurt)</option>
      </select>
      <select id="plan">
        <option value="vc2-1c-0.5gb" selected>$3.5/mo · 512MB</option>
        <option value="vc2-1c-1gb">$5/mo · 1GB</option>
      </select>
    </div>
    <button class="btn btn-primary btn-full" id="createBtn" onclick="doAction('create')">创建节点</button>
    <div style="height:8px"></div>
    <button class="btn btn-green btn-full" id="updateBtn" onclick="doAction('update')" disabled>等待初始化…</button>
    <div style="height:8px"></div>
    <button class="btn btn-danger btn-full" id="destroyBtn" onclick="doAction('destroy')">销毁节点</button>
  </div>
    <div class="log-card">
    <div class="log-header">
      <h3>操作日志</h3>
      <span style="font-size:12px;color:var(--muted)" id="logTime"></span>
    </div>
    <div class="log-body" id="logBody">__PROGRESS_LOG__</div>
  </div>

  <div class="sub-card">
    <div class="sub-label">订阅链接 (固定不变 · Shadowrocket / Clash)</div>
    <div class="sub-url-wrap" onclick="copySub()" id="subWrap" title="点击复制">
      <div class="sub-url-mask" id="subDisplay">__SUB_URL__</div>
      <button class="copy-btn" id="copyBtn">复制</button>
    </div>
    <div class="copy-hint" id="copyHint">✓ 已复制到剪贴板</div>
  </div>

  <div class="node-card __NODE_CARD_CLASS__" id="nodeCard">
    <div class="node-header">
      <span class="node-name" id="nodeName">__NODE_NAME__</span>
      <span style="display:flex;gap:8px;align-items:center">
        <span class="node-status __NODE_STATUS_CLASS__" id="nodeStatus">__NODE_STATUS__</span>
        <span class="node-status on" title="点击刷新节点状态" onclick="loadStatus()" style="cursor:pointer">刷新</span>
      </span>
    </div>
    <dl class="node-info" id="nodeInfo">
      <dt>地区</dt><dd id="niRegion">__NODE_REGION__</dd>
      <dt>套餐</dt><dd id="niPlan">__NODE_PLAN__</dd>
      <dt>IP</dt><dd id="niIP">__NODE_IP__</dd>
      <dt>密码</dt><dd id="niPass">__NODE_PASSWORD__</dd>
      <dt>创建用时</dt><dd id="niCreateTime">__NODE_CREATE_TIME__</dd>
    </dl>
  </div>

  <div class="node-card cost-card">
    <div class="node-header">
      <span class="node-name">💰 费用信息</span>
      <span class="node-status on" id="costRefresh" title="点击刷新" onclick="loadCost()">刷新</span>
    </div>
    <dl class="node-info" id="costInfo">
      <dt>余额</dt><dd id="coBalance">__COST_BALANCE__</dd>
      <dt>当前节点费用</dt><dd id="coCurrent">__COST_CURRENT__</dd>
      <dt>历史节点消耗</dt><dd id="coHistory">__COST_HISTORY__</dd>
      <dt>上次成功节点</dt><dd id="coLastNode">加载中…</dd>
      <dt>上次节点花费</dt><dd id="coLastCost">-</dd>
    </dl>
  </div>

  <div class="node-card">
    <div class="node-header">
      <span class="node-name">⏰ 定时任务</span>
      <span class="node-status on" title="点击刷新" onclick="loadSchedule()" style="cursor:pointer">刷新</span>
    </div>
    <dl class="node-info" id="scheduleInfo">
      <dt>自动销毁</dt><dd id="scDestroy">加载中…</dd>
      <dt>自动创建</dt><dd id="scCreate">加载中…</dd>
      <dt>销毁通知</dt><dd id="scBark">加载中…</dd>
    </dl>
  </div>

  <style>
  .switch{position:relative;display:inline-block;width:46px;height:26px;flex:none}
  .switch input{opacity:0;width:0;height:0}
  .switch .slider{position:absolute;cursor:pointer;inset:0;background:#3a3f47;border-radius:26px;transition:.2s}
  .switch .slider:before{content:"";position:absolute;height:20px;width:20px;left:3px;top:3px;background:#fff;border-radius:50%;transition:.2s;box-shadow:0 1px 3px rgba(0,0,0,.4)}
  .switch input:checked + .slider{background:#2ea86c}
  .switch input:checked + .slider:before{transform:translateX(20px)}
  .switch input:disabled + .slider{opacity:.35;cursor:not-allowed}
  </style>
  <div class="node-card" id="exitCard">
    <div class="node-header">
      <span class="node-name">🎚️ 手动兜底开关</span>
      <span class="node-status on" id="exitNow">…</span>
    </div>
    <div style="padding:14px 12px;display:flex;flex-direction:column;gap:14px">
      <div style="display:flex;align-items:center;justify-content:space-between">
        <span style="font-size:14px">魔戒机场</span>
        <label class="switch"><input type="checkbox" id="swMojie" onchange="toggleExit('魔戒')"><span class="slider"></span></label>
      </div>
      <div style="display:flex;align-items:center;justify-content:space-between">
        <span style="font-size:14px">STC机场</span>
        <label class="switch"><input type="checkbox" id="swStc" onchange="toggleExit('STC')"><span class="slider"></span></label>
      </div>
    </div>
    <div style="padding:0 12px 12px;font-size:12px;color:var(--muted)" id="exitHint">…</div>
  </div>

  <div class="node-card" id="aiCard">
    <div class="node-header">
      <span class="node-name">🤖 AI 站点路由</span>
      <span class="node-status on" title="点击重新实测" onclick="loadAI()" style="cursor:pointer">实测</span>
    </div>
    <div style="padding:12px 12px 0;display:flex;align-items:center;justify-content:space-between">
      <span style="font-size:14px">AI 专用开关</span>
      <label class="switch"><input type="checkbox" id="swAI" onchange="toggleAI()"><span class="slider"></span></label>
    </div>
    <div style="padding:6px 12px 0;font-size:12px;color:var(--muted)">开 = AI 走魔戒（过风控）· 关 = AI 走 vultr（省流量）</div>
    <div id="aiRows" style="padding:12px;font-size:13px;display:flex;flex-direction:column;gap:10px">实测中…</div>
    <div style="padding:0 12px 10px;font-size:12px;color:var(--muted)">经 mihomo 当前规则实测各站点真实出口 IP 与国家；❌ = 该路不通（换出口再试）</div>
  </div>

</div>

<!-- API Key 弹窗 -->
<div class="modal-overlay" id="modal">
  <div class="modal">
    <h3>设置 Vultr API Key</h3>
    <input type="password" id="keyInput" placeholder="粘贴你的 Vultr API Key">
    <div id="selfMsg" style="font-size:12px;color:var(--muted);margin:2px 0 12px;white-space:pre-wrap;word-break:break-all;max-height:180px;overflow:auto"></div>
    <div class="modal-actions">
      <button class="btn btn-secondary" onclick="runSelftest()">环境自检</button>
      <button class="btn btn-primary" onclick="saveKey()">保存</button>
      <button class="btn btn-secondary" onclick="hideModal()">取消</button>
    </div>
  </div>
</div>

<script>
let busy = false;
let currentSub = "";
let logPollInterval = null;

function showModal() {
  document.getElementById('modal').classList.add('show');
  document.getElementById('keyInput').focus();
}
function hideModal() {
  document.getElementById('modal').classList.remove('show');
}

async function saveKey() {
  const key = document.getElementById('keyInput').value.trim();
  if (!key) return;
  const r = await fetch("/api", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({action:"savekey", key})});
  const j = await r.json();
  const box = document.getElementById('selfMsg');
  if (box) box.textContent = j.ok ? '✓ API Key 有效并已保存' : ('✗ ' + (j.output || '校验失败'));
  if (j.ok) {
    setTimeout(() => { hideModal(); loadStatus(); }, 600);
  }
}

async function runSelftest() {
  const box = document.getElementById('selfMsg');
  if (box) box.textContent = '自检中…';
  try {
    const r = await fetch("/api", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({action:"selftest"})});
    const j = await r.json();
    const lines = ['平台: ' + (j.platform||''), '机器: ' + (j.machine||'') + ' · Python ' + (j.python||'')];
    (j.checks||[]).forEach(c => lines.push((c.present?'✓ ':'✗ ') + c.name + (c.path?' ('+c.path+')':'') + ' — ' + (c.desc||'')));
    if (j.tailscale_ip) lines.push('Tailscale IP: ' + j.tailscale_ip);
    if (box) box.textContent = lines.join('\n');
  } catch(e) {
    if (box) box.textContent = '自检失败: ' + e.message;
  }
}

function setDD(id, label, val) {
  const dd = document.getElementById(id);
  if (!dd) return;
  const dt = dd.previousElementSibling;
  if (dt && dt.tagName === 'DT') dt.textContent = label;
  dd.textContent = val;
}

async function loadStatus() {
  try {
    const r = await fetch("/api", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({action:"status"})});
    const j = await r.json();
    if (!j.ok) throw new Error(j.output || '状态查询失败');
    if (j.instance) {
      const inst = j.instance;
      const running = inst.status === 'active' && inst.power_status === 'running';
      const readyInit = inst.initialized === true;    // 节点初始化完成才能 Update
      document.getElementById('nodeDot').className = running ? 'dot on' : 'dot off';
      document.getElementById('nodeLabel').textContent = inst.region + ' · ' + inst.status;
      document.getElementById('nodeCard').className = running ? 'node-card active' : 'node-card inactive';
      document.getElementById('nodeName').textContent = inst.region + ' (' + inst.main_ip + ')';
      document.getElementById('nodeStatus').className = running ? 'node-status on' : 'node-status off';
      document.getElementById('nodeStatus').textContent = running ? (readyInit ? '运行中' : '初始化中…') : inst.status;
      setDD('niRegion', '地区', inst.region);
      setDD('niPlan', '套餐', inst.plan);
      document.getElementById('niIP').textContent = inst.main_ip;
      document.getElementById('niPass').textContent = inst.ss_password || '-';
      document.getElementById('niCreateTime').textContent = inst.create_duration != null ? inst.create_duration + ' 秒' : '-';
      // 仅节点已创建且初始化完成，才允许"更新订阅"
      document.getElementById('updateBtn').disabled = !(running && readyInit);
      document.getElementById('updateBtn').textContent = (running && readyInit) ? '更新订阅' : '等待初始化…';
      currentSub = j.subscription_url || '';
      return;
    }
    const mh = j.mihomo || null;
    const mhOk = !!(mh && mh.ok);
    document.getElementById('nodeDot').className = 'dot off';
    document.getElementById('nodeLabel').textContent = '无vultr节点';
    document.getElementById('nodeCard').className = mhOk ? 'node-card active' : 'node-card inactive';
    document.getElementById('nodeName').textContent = (mhOk && mh.exit) ? mh.exit : '无活跃节点';
    document.getElementById('nodeStatus').className = mhOk ? 'node-status on' : 'node-status off';
    document.getElementById('nodeStatus').textContent = mhOk ? 'mihomo 兜底中' : '未激活';
    setDD('niRegion', '当前出口', mhOk ? (mh.chain || '-') : '-');
    setDD('niPlan', '机场存活', (mhOk && mh.providers && mh.providers.length)
      ? mh.providers.map(p => p.name + ' ' + p.alive + '/' + p.total).join(' · ') : '-');
    document.getElementById('niIP').textContent = '-';
    document.getElementById('niPass').textContent = '-';
    document.getElementById('updateBtn').disabled = true;
    document.getElementById('updateBtn').textContent = '更新订阅';
    currentSub = j.subscription_url || '';
  } catch (e) {
    document.getElementById('nodeLabel').textContent = '状态查询失败';
    document.getElementById('nodeName').textContent = e.message;
  }
}

async function loadLog() {
  try {
    const r = await fetch("/api", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({action:"getlog"})});
    const j = await r.json();
    const el = document.getElementById('logBody');
    el.textContent = j.log || '准备就绪';
    el.scrollTop = el.scrollHeight;
  } catch(e) {}
}

async function checkBusy() {
  try {
    const r = await fetch("/api", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({action:"isbusy"})});
    const j = await r.json();
    return j.busy || false;
  } catch(e) {
    return false;
  }
}

function setButtons(disabled) {
  document.getElementById('createBtn').disabled = disabled;
  document.getElementById('updateBtn').disabled = disabled;
  document.getElementById('destroyBtn').disabled = disabled;
}

async function doAction(action) {
  // 先给即时反馈：确认弹窗同步弹出，不被待网络卡住
  if (action === 'create' && !confirm('即将创建新实例，确认？')) return;
  if (action === 'destroy' && !confirm('确认销毁实例？将停止计费')) return;

  setButtons(true);
  document.getElementById('logBody').textContent = '正在执行…';

  // 检查是否已有操作在运行（仅提示，不阻塞）
  try {
    const isBusy = await checkBusy();
    if (isBusy) {
      setButtons(false);
      loadLog();
      return;
    }
  } catch(e) {}

  const region = document.getElementById('region').value;
  const plan = document.getElementById('plan').value;

  let resp;
  try {
    resp = await fetch("/api", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({action, region, plan})});
  } catch(e) {
    console.error(e);
    loadLog();
    loadStatus();
    setButtons(false);
    return;
  }

  // 立即刷新一次日志（服务端已写入"开始执行"）
  loadLog();

  // 若服务端未真正启动后台任务（如已有节点），立即结束
  try {
    const j = await resp.json();
    if (j.started === false) {
      loadLog();
      loadStatus();
      setButtons(false);
      return;
    }
  } catch(e) {}

  // 执行期间轮询日志；动作完成（busy=false）后结束并刷新状态
  if (logPollInterval) clearInterval(logPollInterval);
  logPollInterval = setInterval(async () => {
    loadLog();
    const stillBusy = await checkBusy();
    if (!stillBusy) {
      if (logPollInterval) { clearInterval(logPollInterval); logPollInterval = null; }
      loadLog();
      loadStatus();
      setButtons(false);
    }
  }, 2000);
}

function copySub() {
  const url = currentSub || '__SUB_URL__';
  const showCopied = () => {
    const hint = document.getElementById('copyHint');
    hint.classList.add('show');
    setTimeout(() => hint.classList.remove('show'), 1500);
  };
  const fallbackCopy = () => {
    const t = document.createElement('textarea');
    t.value = url;
    t.style.position = 'fixed';
    t.style.opacity = '0';
    document.body.appendChild(t);
    t.focus();
    t.select();
    t.setSelectionRange(0, t.value.length);
    const copied = document.execCommand('copy');
    document.body.removeChild(t);
    if (copied) showCopied();
  };

  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(url).then(showCopied).catch(fallbackCopy);
  } else {
    fallbackCopy();
  }
}

// 套餐↔地区联动：新泽西↔$3.5，其他地区↔$5
function syncPlanRegion() {
  const region = document.getElementById('region');
  const plan = document.getElementById('plan');
  if (region.value === 'ewr') {
    plan.value = 'vc2-1c-0.5gb';          // 新泽西 → $3.5
  } else {
    plan.value = 'vc2-1c-1gb';            // 日本/其他 → $5
  }
  if (plan.value === 'vc2-1c-0.5gb') {
    region.value = 'ewr';                 // $3.5 → 新泽西
  }
}
document.getElementById('region').addEventListener('change', () => { regionTouched = true; syncPlanRegion(); });
document.getElementById('plan').addEventListener('change', syncPlanRegion);

async function loadCost() {
  try {
    const r = await fetch("/api", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({action:"cost"})});
    const j = await r.json();
    if (!j.ok) throw new Error(j.output || '费用查询失败');
    const b = j.billing;
    // 余额 = -(充入金额 + 已花费)：-10 为充入，0.22 为本月已花费
    if (b.balance != null && b.pending != null) {
      const remaining = -1 * (b.balance + b.pending);
      document.getElementById('coBalance').textContent =
        '$' + remaining.toFixed(2) + (b.pending > 0 ? '（本月已花费 $' + b.pending + '）' : '');
    } else {
      document.getElementById('coBalance').textContent = '-';
    }
    document.getElementById('coCurrent').textContent =
      (b.current_cost != null ? '$' + b.current_cost.toFixed(4) : '-') + (b.current_plan_monthly ? '（月费 $' + b.current_plan_monthly + '）' : '');
    document.getElementById('coHistory').textContent =
      (b.history_total != null ? '$' + b.history_total.toFixed(4) : '-') + (b.total_all != null && b.total_all > 0 ? ' · 累计 $' + b.total_all.toFixed(4) : '');
    renderLastSuccess(j.last_success);
  } catch(e) {
    document.getElementById('coBalance').textContent = '查询失败';
  }
}

// 上次成功节点：费用卡片展示 + 未手动改选时地区下拉默认选中它（创建优先上次成功地区）
let regionTouched = false;
function fmtDur(sec) {
  if (sec == null) return '-';
  const h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60);
  return h > 0 ? h + '小时' + m + '分' : m + '分钟';
}
function fmtTime(epoch) {
  if (!epoch) return '-';
  const d = new Date(epoch * 1000);
  const p = n => String(n).padStart(2, '0');
  return (d.getMonth()+1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
}
function renderLastSuccess(ls) {
  const nameEl = document.getElementById('coLastNode'), costEl = document.getElementById('coLastCost');
  if (!ls) { nameEl.textContent = '暂无记录'; costEl.textContent = '-'; return; }
  nameEl.textContent =
    (ls.region || '-') + ' · ' + (ls.plan || '-') + (ls.ip ? ' · ' + ls.ip : '') + (ls.active ? '（当前运行中）' : '');
  costEl.textContent =
    (ls.cost != null ? '$' + ls.cost.toFixed(4) : '-') +
    ' · 时长 ' + fmtDur(ls.duration_sec) + ' · 成功于 ' + fmtTime(ls.succeeded_at);
  if (!regionTouched && ls.region) {
    const sel = document.getElementById('region');
    if (sel && Array.from(sel.options).some(o => o.value === ls.region)) {
      sel.value = ls.region;
      syncPlanRegion();
    }
  }
}

async function loadSchedule() {
  try {
    const r = await fetch("/api", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({action:"schedule"})});
    const j = await r.json();
    const s = j.schedule || {};
    const on = s.enabled !== false;
    document.getElementById('scDestroy').textContent =
      (on && s.destroy_time) ? ('每天 ' + s.destroy_time + '（销毁并结算计费）') : '未启用';
    document.getElementById('scCreate').textContent =
      (on && s.create_time) ? ('每天 ' + s.create_time) : '未启用（预留）';
    document.getElementById('scBark').textContent =
      s.bark_configured ? 'Bark 已配置，销毁结果推送到手机' : '未配置（编辑 .state/schedule.json 的 bark_url）';
  } catch(e) {
    document.getElementById('scDestroy').textContent = '查询失败';
  }
}

// Enter 键保存 API Key
document.getElementById('keyInput').addEventListener('keydown', e => {
  if (e.key === 'Enter') saveKey();
});

// 初始加载（各页首次各请求一次，后续用户手动刷新）
let currentExit = '';
async function loadExit() {
  try {
    const r = await fetch("/api", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({action:"status"})});
    const j = await r.json();
    const mh = j.mihomo || {};
    currentExit = mh.now || mh.exit || '未知';
    const el = document.getElementById('exitNow');
    if (el) el.textContent = '出口: ' + currentExit + ((mh.exit && mh.exit !== currentExit) ? ' → ' + mh.exit : '');
    const vultrRunning = !!(j.instance && j.instance.status === 'active' && j.instance.power_status === 'running');
    const m = document.getElementById('swMojie'), s = document.getElementById('swStc');
    if (m) { m.checked = (currentExit === '魔戒'); m.disabled = vultrRunning; }
    if (s) { s.checked = (currentExit === 'STC'); s.disabled = vultrRunning; }
    const hint = document.getElementById('exitHint');
    if (hint) hint.textContent = vultrRunning
      ? 'vultr 运行中，无需兜底（开关已自动关闭）'
      : '打开即接管全部代理流量；关闭回直连 DIRECT。tailscale / opencode.ai 永远直连，不受影响。';
  } catch(e) {}
}
async function toggleExit(name) {
  const target = (currentExit === name) ? 'DIRECT' : name;
  const r = await fetch("/api", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({action:"mihomo-exit", target})});
  const j = await r.json();
  if (!j.ok) alert('切换失败: ' + (j.output || '未知错误'));
  loadExit(); loadStatus();
}
let aiExitNow = '';
async function loadAI() {
  const box = document.getElementById('aiRows');
  try {
    const sr = await fetch("/api", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({action:"status"})});
    const sj = await sr.json();
    aiExitNow = (sj.mihomo && sj.mihomo.ai) || '';
    const sw = document.getElementById('swAI');
    if (sw) sw.checked = (aiExitNow === '魔戒');
  } catch(e) {}
  if (box) box.textContent = '实测中…';
  try {
    const r = await fetch("/api", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({action:"ai-routes"})});
    const j = await r.json();
    if (box) {
      box.textContent = '';
      (j.routes || []).forEach(t => {
        const row = document.createElement('div');
        row.style.cssText = 'display:flex;justify-content:space-between;align-items:center';
        const nm = document.createElement('span');
        nm.textContent = t.name;
        const st = document.createElement('span');
        st.style.cssText = 'font-family:ui-monospace,monospace;font-size:12px';
        st.textContent = t.ok ? ('✅ ' + t.ip + ' · ' + t.loc) : ('❌ 无连接' + (t.err ? '（' + t.err + '）' : ''));
        if (t.err && t.ok) st.title = t.err;
        row.appendChild(nm); row.appendChild(st);
        box.appendChild(row);
      });
    }
  } catch(e) { if (box) box.textContent = '实测失败: ' + e.message; }
}
async function toggleAI() {
  const sw = document.getElementById('swAI');
  if (!sw || sw.disabled) return;
  const target = sw.checked ? '魔戒' : 'vultr-ss';
  sw.disabled = true;
  try {
    const r = await fetch("/api", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({action:"ai-exit", target})});
    const j = await r.json();
    if (!j.ok) { alert('切换失败: ' + (j.output || '未知错误')); }
  } catch(e) { alert('切换失败: ' + e.message); }
  loadAI();
}
loadStatus();
loadCost();
loadLog();
loadSchedule();
loadExit();
loadAI();

// 操作执行期间由 doAction 内部轮询日志；无操作时不再自动刷新
</script>
</body>
</html>"""

# 记入 progress.log 的"用户主动操作"白名单；轮询类(isbusy/getlog/status等)不记避免刷屏
CLICK_ACTIONS = {"create", "destroy", "update", "selftest", "rotatetoken", "savekey"}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok"); return
        if self.path == "/clash.yaml":
            clash_file = os.path.join(BASE_DIR, "clash.yaml")
            if os.path.exists(clash_file):
                self.send_response(200)
                self.send_header("Content-Type", "text/yaml; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                with open(clash_file, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_response(404); self.end_headers()
                self.wfile.write(b"no active node")
            return
        page_content = render_setup_page() if not get_api_key() else render_page()
        page_bytes = page_content.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page_bytes)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(page_bytes)

    def do_POST(self):
        if not self.path.startswith("/api"):
            self.send_response(404); self.end_headers(); return
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            data = {}
        action = data.get("action", "")

        # 按钮点击审计：谁在什么时候点了什么按钮（含参数与来源 IP），便于排查与总结
        if action in CLICK_ACTIONS:
            client_ip = self.client_address[0] if self.client_address else "?"
            extras = []
            if data.get("region"):
                extras.append(f"region={data['region']}")
            if data.get("plan"):
                extras.append(f"plan={data['plan']}")
            if data.get("key"):
                extras.append("key=***")
            suffix = (" " + " ".join(extras)) if extras else ""
            append_log(f"🖱 [ui] {action}{suffix} ← {client_ip}")
        
        if action == "savekey":
            key = (data.get("key") or "").strip()
            res = validate_api_key(key)
            if res["ok"]:
                save_api_key(key)
                append_log("✓ API Key 已保存并校验通过")
            resp = res
        elif action == "mihomo-exit":
            target = (data.get("target") or "DIRECT").strip()
            if target not in ("DIRECT", "魔戒", "STC", "vultr-ss"):
                resp = {"ok": False, "output": "非法出口: " + target}
            else:
                resp = set_mihomo_exit(target)
        elif action == "ai-routes":
            resp = get_ai_routes()
        elif action == "ai-exit":
            target = (data.get("target") or "").strip()
            if target not in ("魔戒", "vultr-ss"):
                resp = {"ok": False, "output": "AI 组仅支持 魔戒/vultr-ss: " + target}
            else:
                resp = set_mihomo_exit(target, group="AI")
        elif action == "getlog":
            resp = {"ok": True, "log": read_log()}
        elif action == "isbusy":
            resp = {"ok": True, "busy": is_action_running()}
        elif action == "selftest":
            resp = run_selftest()
        elif action == "rotatetoken":
            token = rotate_sub_token()
            resp = {"ok": True, "token": token, "subscription_url": resolve_sub_url(),
                    "note": "旧订阅链接已失效，请用新链接重新订阅"}
        elif action == "cost":
            try:
                resp = {"ok": True, "billing": get_billing(), "last_success": get_last_success()}
            except Exception as e:
                resp = {"ok": False, "output": str(e)}
        elif action == "schedule":
            sched = load_schedule()
            resp = {"ok": True, "schedule": {
                "enabled": bool(sched.get("enabled")),
                "destroy_time": (sched.get("destroy_time") or "").strip(),
                "create_time": (sched.get("create_time") or "").strip(),
                "bark_configured": bool((sched.get("bark_url") or "").strip()),
            }}
        elif action in ["connect", "status"]:
            sub_url = resolve_sub_url()
            try:
                resp = {"ok": True, "instance": get_instance_status(), "subscription_url": sub_url,
                        "mihomo": get_mihomo_status()}
            except Exception as e:
                resp = {"ok": False, "output": str(e), "subscription_url": sub_url}
        elif action in ["create", "destroy", "update"]:
            # 后台异步执行
            region = data.get("region", "nrt")
            plan = data.get("plan", "")

            if action == "create" and get_vultr_instances():
                append_log("当前节点已创建")
                resp = {"ok": True, "output": "当前节点已创建", "started": False}
            else:
                def run_in_background():
                    run_action(action, region, plan)

                thread = threading.Thread(target=run_in_background, daemon=True)
                thread.start()
                resp = {"ok": True, "output": "任务已启动，请查看日志", "started": True}
        elif action == "switch-sub":
            mode = (data.get("mode") or "").strip()
            names = {"all": "全自动(三源)", "vultr": "Vultr", "mojie": "魔戒", "stc": "STC"}
            if mode not in names:
                resp = {"ok": False, "output": "未知模式(可选 all/vultr/mojie/stc)"}
            else:
                try:
                    import re, subprocess, urllib.request, json as _json
                    p = "/home/jun/.config/mihomo/config.yaml"
                    s = open(p).read()
                    use = "[vultr, mojie, stc]" if mode == "all" else "[" + mode + "]"
                    s2 = re.sub(r"use: \[[^\]]*\]", "use: " + use, s)
                    if s2 == s:
                        resp = {"ok": False, "output": "配置未修改(未找到 use 行)"}
                    else:
                        open(p + ".swp", "w").write(s2)
                        t = subprocess.run(["/usr/local/bin/mihomo", "-d", "/home/jun/.config/mihomo", "-t", "-f", p + ".swp"], capture_output=True, text=True, timeout=30)
                        if t.returncode != 0:
                            resp = {"ok": False, "output": "校验失败未切换: " + (t.stderr or t.stdout)[-180:]}
                        else:
                            subprocess.run(["cp", p + ".swp", p], check=True)
                            sec = ""
                            for _l in open(p):
                                if _l.startswith("secret:"):
                                    import re as _re
                                    _m = _re.search(r'"([^"]+)"', _l)
                                    if _m: sec = _m.group(1)
                            req = urllib.request.Request("http://127.0.0.1:9090/configs?force=true",
                                data=_json.dumps({"path": p, "payload": ""}).encode(),
                                headers={"Authorization": "Bearer " + sec, "Content-Type": "application/json"}, method="PUT")
                            urllib.request.urlopen(req, timeout=15)
                            open("/home/jun/vultr-clash/.state/sub-mode", "w").write(mode)
                            append_log("✓ 订阅切换: " + names[mode])
                            resp = {"ok": True, "output": "已切换: " + names[mode]}
                except Exception as e:
                    resp = {"ok": False, "output": "切换失败: " + str(e)[:200]}
        elif action == "save-stc":
            content = data.get("content") or ""
            head = content[:500].lower()
            if "proxies:" not in content:
                resp = {"ok": False, "output": "未找到 proxies 段, 请粘贴 Clash 格式订阅"}
            elif "<html" in head or "just a moment" in head:
                resp = {"ok": False, "output": "这是 Cloudflare 拦截页, 请在浏览器通过验证拿到订阅内容"}
            else:
                open("/home/jun/.config/mihomo/stc.yaml", "w").write(content)
                append_log("✓ STC 订阅内容导入")
                resp = {"ok": True, "output": "STC 订阅已导入"}
        elif action == "fetch-mojie":
            import subprocess
            r = subprocess.run(["curl", "-sk", "-m", "20", "-o", "/home/jun/.config/mihomo/mojie.yaml",
                "https://47.112.97.173:5000/api/v1/client/subscribe?token=c8d358708b731ff640789c69eb722b71&flag=clash"], capture_output=True, text=True, timeout=30)
            ok = r.returncode == 0 and "proxies:" in open("/home/jun/.config/mihomo/mojie.yaml").read()
            append_log(("✓" if ok else "✗") + " 魔戒订阅刷新")
            resp = {"ok": ok, "output": "魔戒节点已刷新" if ok else "魔戒刷新失败"}
        elif action == "sub-status":
            try:
                mode = open("/home/jun/vultr-clash/.state/sub-mode").read().strip() or "all"
                resp = {"ok": True, "mode": mode}
            except Exception:
                resp = {"ok": True, "mode": "all"}
        else:
            resp = {"ok": False, "output": "未知操作"}
            
        body = json.dumps(resp, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

def main():
    # 初始化日志文件
    os.makedirs(os.path.join(BASE_DIR, ".state"), exist_ok=True)
    if not os.path.exists(LOG_FILE):
        append_log("系统启动")

    host = os.environ.get("VULTR_WEB_HOST", "0.0.0.0")
    port = int(os.environ.get("VULTR_WEB_PORT", "8333"))
    print(f"vultr-clash webui v{read_version()}")
    print(f"平台: {platform.platform()} ({platform.machine()})")
    print(f"WebUI: http://{host}:{port}/")
    print(f"Sub:   {resolve_sub_url()}")
    sched = load_schedule()
    print(f"定时:  销毁={sched.get('destroy_time')} 创建={sched.get('create_time') or '未启用'}"
          f" Bark={'已配置' if (sched.get('bark_url') or '').strip() else '未配置'}")
    # 首次运行生成默认调度配置；启动调度线程
    if not os.path.exists(SCHEDULE_FILE):
        save_schedule(dict(SCHEDULE_DEFAULT))
    threading.Thread(target=scheduler_loop, daemon=True).start()
    ThreadingHTTPServer((host, port), Handler).serve_forever()

if __name__ == "__main__":
    main()
