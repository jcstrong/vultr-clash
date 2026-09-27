#!/usr/bin/env python3
"""vultr-clash MCP 服务器 — 把 WebUI 管理 API 暴露为 MCP 工具（AI 助手可管理节点）。

零依赖（纯 Python 标准库），stdio 传输（newline-delimited JSON-RPC 2.0）。
所有工具转发到 WebUI 的 /api（默认本机 8333），复用其并发锁/日志/计费体系。

环境变量：
  VULTR_WEBUI_URL   WebUI /api 地址，默认 http://127.0.0.1:8333/api
                    远程部署用 Tailscale 地址，如 http://100.90.80.9:8333/api

客户端配置示例（Claude Desktop / OpenClaw 等）：

  {
    "mcpServers": {
      "vultr-clash": {
        "command": "python3",
        "args": ["/home/jun/vultr-clash/mcp_server.py"],
        "env": {"VULTR_WEBUI_URL": "http://127.0.0.1:8333/api"}
      }
    }
  }

  远程（不经 SSH、直接连 Tailscale 地址）：
    "command": "python3", "args": ["/local/path/mcp_server.py"],
    "env": {"VULTR_WEBUI_URL": "http://100.90.80.9:8333/api"}

  经 SSH 在服务器上运行（本文件不在本机也能用）：
    "command": "ssh", "args": ["jun@100.90.80.9", "python3", "/home/jun/vultr-clash/mcp_server.py"]
"""
import json
import os
import sys
import time
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEBUI_URL = os.environ.get("VULTR_WEBUI_URL", "http://127.0.0.1:8333/api").rstrip("/")
PROTO_DEFAULT = "2024-11-05"
SERVER_NAME = "vultr-clash"

REGIONS = ["ewr", "nrt", "sgp", "icn", "lax", "fra"]
PLANS = ["vc2-1c-0.5gb", "vc2-1c-1gb"]


def read_version():
    try:
        with open(os.path.join(BASE_DIR, "VERSION"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return "0.0.0"


def api(action, timeout=30, **fields):
    """调用 WebUI /api，返回解析后的 JSON dict"""
    payload = json.dumps({"action": action, **fields}).encode()
    req = urllib.request.Request(
        WEBUI_URL, data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def wait_until_idle(seconds):
    """轮询 isbusy 直到任务结束或超时；返回是否等到空闲"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            if not api("isbusy", timeout=10).get("busy"):
                return True
        except Exception:
            pass
        time.sleep(3)
    return False


def log_tail(lines=25):
    try:
        log = api("getlog", timeout=15).get("log", "")
    except Exception as e:
        return f"(日志获取失败: {e})"
    return "".join(log.splitlines(keepends=True)[-lines:]).rstrip() or "(空)"


def run_async_action(action, wait_seconds, region=None, plan=None):
    """启动 create/destroy/update 并等待完成，返回汇总文本"""
    fields = {}
    if region:
        fields["region"] = region
    if plan:
        fields["plan"] = plan
    start = api(action, timeout=30, **fields)
    if not start.get("started", True):
        return f"{action} 未启动：{start.get('output', '未知原因')}"
    parts = [f"⏳ {action} 已启动（后台执行，进度写入日志）"]
    if wait_until_idle(wait_seconds):
        parts.append(f"✅ 任务已结束。最近日志：\n{log_tail()}")
    else:
        parts.append(f"⚠ 等待超时仍在执行中（可稍后用 get_logs / node_status 查看）。最近日志：\n{log_tail()}")
    try:
        st = api("status", timeout=45)
        if st.get("instance"):
            i = st["instance"]
            parts.append(f"当前节点: {i.get('region')}/{i.get('plan')} {i.get('main_ip')} "
                         f"status={i.get('status')} initialized={i.get('initialized', '?')}")
        else:
            parts.append("当前节点: 无")
        parts.append(f"订阅链接: {st.get('subscription_url', '?')}")
    except Exception as e:
        parts.append(f"(状态获取失败: {e})")
    return "\n\n".join(parts)


# ---------------- 工具实现 ----------------

def tool_node_status():
    j = api("status", timeout=45)
    if not j.get("ok"):
        return f"查询失败: {j.get('output', '未知错误')}"
    inst = j.get("instance")
    if not inst:
        return f"当前无活跃节点。\n订阅链接: {j.get('subscription_url', '?')}"
    running = inst.get("status") == "active" and inst.get("power_status") == "running"
    lines = [
        f"地区: {inst.get('region')}    套餐: {inst.get('plan')}",
        f"IP: {inst.get('main_ip')}",
        f"状态: {inst.get('status')}/{inst.get('power_status')}"
        + ("" if not running else ("（初始化完成）" if inst.get("initialized") else "（初始化中…）")),
        f"SS 密码: {inst.get('ss_password') or '-'}",
        f"创建用时: {inst.get('create_duration')} 秒" if inst.get("create_duration") is not None else "创建用时: -",
        f"订阅链接: {j.get('subscription_url', '?')}",
    ]
    return "\n".join(lines)


def tool_node_create(region="ewr", plan="vc2-1c-0.5gb", wait=True):
    return run_async_action("create", 360 if wait else 0, region=region, plan=plan)


def tool_node_destroy(wait=True):
    return run_async_action("destroy", 90 if wait else 0)


def tool_subscription_update(wait=True):
    return run_async_action("update", 150 if wait else 0)


def tool_get_logs(lines=25):
    return log_tail(int(lines))


def tool_is_busy():
    j = api("isbusy", timeout=10)
    return "有任务正在执行中" if j.get("busy") else "空闲"


def tool_get_cost():
    j = api("cost", timeout=45)
    if not j.get("ok"):
        return f"查询失败: {j.get('output', '未知错误')}"
    b = j.get("billing", {})
    if b.get("balance") is not None and b.get("pending") is not None:
        remaining = -1 * (b["balance"] + b["pending"])
        bal = f"${remaining:.2f}" + (f"（本月已花费 ${b['pending']}）" if b.get("pending") else "")
    else:
        bal = "-"
    cur = f"${b.get('current_cost', 0):.4f}" + (f"（月费 ${b['current_plan_monthly']}）" if b.get("current_plan_monthly") else "")
    his = f"${b.get('history_total', 0):.4f}" + (f" · 累计 ${b.get('total_all', 0):.4f}" if b.get("total_all") else "")
    return f"余额: {bal}\n当前节点费用: {cur}\n历史节点消耗: {his}"


def tool_get_schedule():
    j = api("schedule", timeout=15)
    s = j.get("schedule", {})
    on = s.get("enabled", False)
    d = f"每天 {s.get('destroy_time')}" if on and s.get("destroy_time") else "未启用"
    c = f"每天 {s.get('create_time')}" if on and s.get("create_time") else "未启用（预留）"
    bark = "已配置" if s.get("bark_configured") else "未配置"
    return f"自动销毁: {d}\n自动创建: {c}\nBark 推送: {bark}"


def tool_rotate_subscription_token():
    j = api("rotatetoken", timeout=15)
    if not j.get("ok"):
        return f"轮换失败: {j.get('output', '未知错误')}"
    return f"✅ 已轮换。新订阅链接: {j.get('subscription_url')}\n注意: {j.get('note', '旧链接已失效')}"


def tool_selftest():
    j = api("selftest", timeout=30)
    lines = [f"平台: {j.get('platform')} · Python {j.get('python')}"]
    if j.get("tailscale_ip"):
        lines.append(f"Tailscale IP: {j['tailscale_ip']}")
    for c in j.get("checks", []):
        lines.append(f"{'✓' if c.get('present') else '✗'} {c.get('name')} — {c.get('desc', '')}")
    lines.append(f"Vultr API Key: {'已配置' if j.get('vultr_key_ok') else '未配置'}")
    return "\n".join(lines)


# ---------------- 工具描述（MCP schema） ----------------

def _schema(props=None, required=None):
    return {"type": "object", "properties": props or {}, "required": required or []}


_STR_LINES = {"type": "integer", "title": "返回日志行数", "default": 25, "minimum": 1, "maximum": 100}
_BOOL_WAIT = {"type": "boolean", "title": "等待任务完成再返回", "default": True}
_REGION = {"type": "string", "enum": REGIONS, "default": "ewr",
           "description": "地区（惯例：ewr ↔ $3.5，其他 ↔ $5）"}
_PLAN = {"type": "string", "enum": PLANS, "default": "vc2-1c-0.5gb",
         "description": "套餐：vc2-1c-0.5gb=$3.5/月，vc2-1c-1gb=$5/月"}

TOOLS = [
    {"name": "node_status", "description": "查看当前 Vultr 节点状态（地区/套餐/IP/SS密码/初始化）与订阅链接",
     "inputSchema": _schema()},
    {"name": "node_create", "description": "创建新节点（已有节点会拒绝）。成功后订阅自动切换到新节点",
     "inputSchema": _schema({"region": _REGION, "plan": _PLAN, "wait": _BOOL_WAIT})},
    {"name": "node_destroy", "description": "销毁当前节点并结算计费（销毁后订阅失效，按量计费停止）",
     "inputSchema": _schema({"wait": _BOOL_WAIT})},
    {"name": "subscription_update", "description": "更新订阅：从 Vultr API 同步节点信息并从 VPS 读取真实 SS 密码（防漂移）",
     "inputSchema": _schema({"wait": _BOOL_WAIT})},
    {"name": "get_logs", "description": "查看操作日志（含守卫/定时任务的 [guard] 记录）",
     "inputSchema": _schema({"lines": _STR_LINES})},
    {"name": "is_busy", "description": "查询是否有节点操作正在执行", "inputSchema": _schema()},
    {"name": "get_cost", "description": "查看费用：账户余额、当前节点费用、历史累计消耗",
     "inputSchema": _schema()},
    {"name": "get_schedule", "description": "查看定时任务配置（自动销毁/自动创建时间、Bark 状态）",
     "inputSchema": _schema()},
    {"name": "rotate_subscription_token", "description": "轮换订阅 token（旧订阅链接立即失效，需重新填入客户端）",
     "inputSchema": _schema()},
    {"name": "selftest", "description": "服务器环境自检（依赖/Tailscale/API Key 状态）",
     "inputSchema": _schema()},
]

HANDLERS = {
    "node_status": tool_node_status,
    "node_create": tool_node_create,
    "node_destroy": tool_node_destroy,
    "subscription_update": tool_subscription_update,
    "get_logs": tool_get_logs,
    "is_busy": tool_is_busy,
    "get_cost": tool_get_cost,
    "get_schedule": tool_get_schedule,
    "rotate_subscription_token": tool_rotate_subscription_token,
    "selftest": tool_selftest,
}


# ---------------- JSON-RPC / MCP 协议 ----------------

def handle_message(msg):
    """处理单条消息（或批量数组），返回 result 或 None（通知）"""
    if isinstance(msg, list):
        for m in msg:
            handle_message(m)
        return None
    if not isinstance(msg, dict):
        return None
    method = msg.get("method", "")
    if msg.get("id") is None:
        return None  # 通知（如 notifications/initialized）无需响应
    if method == "initialize":
        pv = (msg.get("params") or {}).get("protocolVersion") or PROTO_DEFAULT
        return {
            "protocolVersion": pv,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": read_version()},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name", "")
        args = params.get("arguments") or {}
        handler = HANDLERS.get(name)
        if handler is None:
            raise MethodError(-32602, f"未知工具: {name}")
        try:
            text = handler(**args)
            return {"content": [{"type": "text", "text": text}], "isError": False}
        except TypeError as e:
            return {"content": [{"type": "text", "text": f"参数错误: {e}"}], "isError": True}
        except Exception as e:  # 网络/后端错误 → 工具级错误
            return {"content": [{"type": "text", "text": f"工具执行失败: {e}"}], "isError": True}
    raise MethodError(-32601, f"未知方法: {method}")


class MethodError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def main():
    sys.stderr.write(f"{SERVER_NAME} mcp server v{read_version()} → {WEBUI_URL}\n")
    sys.stderr.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        rid = None
        try:
            msg = json.loads(line)
            rid = msg.get("id") if isinstance(msg, dict) else None
            result = handle_message(msg)
            if result is not None:
                print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}, ensure_ascii=False), flush=True)
        except MethodError as e:
            print(json.dumps({"jsonrpc": "2.0", "id": rid,
                              "error": {"code": e.code, "message": str(e)}}, ensure_ascii=False), flush=True)
        except json.JSONDecodeError:
            print(json.dumps({"jsonrpc": "2.0", "id": None,
                              "error": {"code": -32700, "message": "Parse error"}}, ensure_ascii=False), flush=True)
        except Exception as e:
            print(json.dumps({"jsonrpc": "2.0", "id": rid,
                              "error": {"code": -32603, "message": f"Internal error: {e}"}}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
