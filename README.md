# vultr-clash

Vultr Shadowsocks 节点管理 + Clash 订阅的一体化工具（跨平台：Linux / macOS）。

**输入你的 Vultr API Key（+ 可选 Tailscale 分发通道），输出一个 Clash 内核可直接订阅的 YAML 链接。**

## 功能

- **Web 管理界面**：创建、销毁节点，更新订阅，实时查看进度日志
- **节点状态**：当前节点、套餐、IP、SS 密码、创建用时
- **费用信息**：账户余额、当前节点费用、历史节点消耗（精确到小时按量计费）
- **套餐联动**：新泽西(ewr) ↔ $3.5，其他地区 ↔ $5，默认新泽西 + $3.5
- **首启引导**：未配置 API Key 时自动进入初始化页（填写并校验 Key、一键环境自检）
- **环境自检**：`/api selftest` 逐个探测 curl/jq/python3/openssl/nc/sshpass/expect/tailscale，并显示平台与 Tailscale IP
- **订阅服务**：HTTP 提供 `clash.yaml`，Tailscale 内网 / 公网均可访问；可选 token 鉴权（`/sub/{token}/clash.yaml`，轮换后旧链接立即失效）
- **MAC 支持**：管理端可在 macOS 运行（`install.sh` 自动用 launchd；密码 SSH 缺 sshpass 时回退系统自带 expect；`nc` 探活回退 python3）
- **自愈**：自动放行 VPS 防火墙 8388、重启 shadowsocks、从 VPS 读取真实密码（避免密码漂移）
- **止损**：create 失败（SSH 超时 / SS 端口起不来 / 初始化未完成）立即注销实例，不留僵尸计费；guard 对从未就绪的节点跳过 2 轮防抖，并用 .state/creating 标记避开在建节点
- **IP 复用安全**：SSH 使用隔离 known_hosts，Vultr 复用 IP 也不报 host key 冲突
- **定时任务**：每天自动销毁/创建节点（如凌晨 00:30 销毁止损），结果通过 Bark 推送到手机；管理页展示当前计划
- **被封自动换区重建**：守卫销毁被封节点后自动在对侧配置重建（新泽西 $3.5 ↔ 日本 $5），订阅随之切换，每日限次防循环，全程 Bark 通知
- **MCP 服务器**：`mcp_server.py` 把管理能力暴露为 MCP 工具，AI 助手（Claude/OpenClaw 等）可直接查状态/建节点/销毁/看日志

## 架构

```
┌────────────┐   create/destroy/update    ┌───────────────┐
│ Web UI     │ ────────────────────────►  │  Vultr API    │
│ :8333      │                            │ (你的 API Key)│
└────────────┘                            └───────┬───────┘
      │                                          │ 部署 cloud-init
      │ GET /sub/{token}/clash.yaml              ▼
┌─────┴───────┐  订阅                        ┌───────────────┐
│ Sub Server  │ ◄────────────────────────  │   VPS 节点     │
│ :8888       │                             │  shadowsocks  │
└─────────────┘                            └───────────────┘
```

- `webui.py` — Web 管理界面（:8333，纯 Python 标准库）
- `serve_sub.py` — 订阅服务（:8888，纯 Python 标准库，含 token 鉴权）
- `vultr-clash.sh` — 核心脚本：Vultr API、计费记账、SSH 部署/自愈（Linux/macOS 兼容）
- `install.sh` — 一键安装：依赖检查 + systemd(Linux) / launchd(macOS) 服务
- `run-webui.sh` / `run-sub.sh` — 跨平台启动器（读取 `.state/*.env`）
- `cloud-init.yaml` — VPS 初始化模板（安装 shadowsocks、放行防火墙）

## 安装

依赖（macOS）：`brew install jq`；密码 SSH 需 `sshpass` **或** `expect`（macOS 自带 expect，无需安装）。
系统依赖：`python3`、`bash`、`curl`、`openssl`。

```bash
git clone https://github.com/<你的用户名>/vultr-clash.git
cd vultr-clash

# 一键安装 + 启动服务（自动识别 Linux systemd / macOS launchd）
./install.sh            # 等价于 ./install.sh install

# 查看/管理
./install.sh status     # start | stop | restart | uninstall | selftest
```

手动运行也可以：

```bash
./run-webui.sh          # Web 管理界面 :8333（首次进入引导填 API Key）
./run-sub.sh            # 订阅服务 :8888
```

## 使用

1. 打开 Web 界面（http://本机IP:8333/）
2. 首次进入为**初始化页**：粘贴 Vultr API Key → 自动校验 →「环境自检」确认依赖 → 进入管理页
3. 选择地区/套餐（默认新泽西 + $3.5），点击「创建节点」
4. 等待初始化完成（SSH + 8388 就绪后「更新订阅」按钮才可用）
5. 把页面上的订阅链接填入 Clash / Shadowrocket 即可

### 订阅 token 鉴权（可选）

默认订阅链接形如 `/sub/clash.yaml`（未启用鉴权，适合 Tailnet 私有场景）。
想防止同 Tailnet 内其他设备盗用，启用 token：

```bash
# 生成 token（自动写入 .state/sub_token）
python3 -c "import secrets;open('.state/sub_token','w').write(secrets.token_urlsafe(24))"
chmod 600 .state/sub_token
```

启用后订阅链接自动升级为 `/sub/{token}/clash.yaml`，未带 token 一律 404。
轮换 token = 删除/重写 `.state/sub_token`，旧链接立即失效（WebUI 也提供 `/api rotatetoken`）。
不想要鉴权就删除该文件，恢复原链接形态。

### 节点健康守卫（自动销毁被封节点，可选）

节点建立后，可用 cron 每 30 分钟探测节点（SS 8388 / SSH 22）。连续失败 `GUARD_FAILS` 次（默认 2，约 1 小时）即判定“被封”，**自动销毁节点并结算计费**，避免被封 IP 傻等浪费日常费用。

销毁后自动在对侧配置**重建节点**（新泽西 $3.5 ↔ 日本 $5 互换）：原节点是 ewr/$3.5 就建 nrt/$5，反之亦然；新节点就绪后 clash.yaml 自动切换。防循环保护：每日最多重建 `GUARD_RECREATE_MAX` 次（默认 2），超限只销毁不重建并推送提醒；`GUARD_RECREATE=0` 关闭自动重建。销毁/重建全程 Bark 推送（读取 `.state/schedule.json` 的 `bark_url`）。

```bash
./install.sh guard install    # 写入 cron（每 30 分钟）
./install.sh guard status     # 查看守卫状态
./install.sh guard uninstall  # 移除定时任务
bash vultr-clash.sh guard run          # 手动跑一轮
```

安全设计：
- 本地无节点（`.state/instance.json` 缺失）→ 直接跳过，不做事
- Vultr API 自身不可达/未授权 → 跳过本轮，**绝不误杀**（防止本地断网/家宽被墙误判）
- 节点 IP 与 Vultr 不一致 → 自动同步新 IP 后再探
- 判定后的销毁日志写入 `progress.log`，WebUI「日志」可见

## 定时任务（自动销毁 / 自动创建 + Bark 通知）

为减少计费时间，可让节点在夜间自动销毁、需要时再自动创建。配置在 `.state/schedule.json`（首次启动自动生成，修改后**无需重启**，调度器每 20 秒重新读取）：

```json
{
  "enabled": true,
  "destroy_time": "00:30",
  "create_time": null,
  "bark_url": "https://api.day.app/<你的Key>",
  "create_region": "ewr",
  "create_plan": "vc2-1c-0.5gb"
}
```

| 字段 | 说明 |
|---|---|
| `enabled` | 总开关，false 时所有定时动作停用 |
| `destroy_time` | 每天自动销毁时刻（本地时间，24h 制）；`null` 停用销毁 |
| `create_time` | 预留的每天自动创建时刻；`null` = 未启用 |
| `bark_url` | [Bark](https://bark.day.app) 推送地址；销毁/创建的成功与失败都会推送 |
| `create_region` / `create_plan` | 自动创建时的地区与套餐（默认新泽西 $3.5） |

行为细节：

- 销毁与手动按钮走完全相同的通道：结算计费（billing.json）、清空 clash.yaml、日志可见
- 触发时无节点 → 静默跳过（只记日志，不推送）
- 触发时有任务在执行 → 同一分钟内自动重试（20s × 3 次）
- Vultr API 不可达 → 不误标记当天，等待下轮重试
- 管理页「⏰ 定时任务」卡片实时展示当前计划与 Bark 配置状态

## MCP 接入（AI 助手管理节点）

`mcp_server.py` 是零依赖的 MCP 服务器（stdio，纯标准库），把 WebUI 的全部管理能力暴露为 10 个工具：

`node_status` / `node_create` / `node_destroy` / `subscription_update` / `get_logs` / `is_busy` / `get_cost` / `get_schedule` / `rotate_subscription_token` / `selftest`

所有工具转发到 WebUI `/api`（复用其并发锁/日志/计费），不直接接触 Vultr 凭据。默认连本机 `http://127.0.0.1:8333/api`，用环境变量 `VULTR_WEBUI_URL` 覆盖。

客户端配置示例：

```jsonc
// 服务器本机（或 SSH 远程执行）
{
  "mcpServers": {
    "vultr-clash": {
      "command": "python3",
      "args": ["/home/jun/vultr-clash/mcp_server.py"]
    }
  }
}

// 其他机器（不经 SSH，直连 Tailscale 地址）
{
  "mcpServers": {
    "vultr-clash": {
      "command": "python3",
      "args": ["/本地路径/mcp_server.py"],
      "env": { "VULTR_WEBUI_URL": "http://100.90.80.9:8333/api" }
    }
  }
}

// 经 SSH 在服务器上运行（本地无需代码）
{
  "mcpServers": {
    "vultr-clash": {
      "command": "ssh",
      "args": ["jun@100.90.80.9", "python3", "/home/jun/vultr-clash/mcp_server.py"]
    }
  }
}
```

说明：

- `node_create` / `node_destroy` / `subscription_update` 是后台异步任务，工具默认轮询等它完成再返回（`wait:false` 可立即返回，用 `get_logs` 查进度）
- 管理 `/api` 无鉴权（内网服务），请用 Tailscale ACL / 防火墙限制访问面

## Tailscale 分发

- 推荐把 **本机加入 Tailnet**，订阅链接用 Tailscale IP，如 `http://100.x.x.x:8888/sub/clash.yaml`（在 `.state/webui.env` 里设置 `SUB_URL`）。
- 需要 HTTPS（`.ts.net`）时，可在 Tailnet 启用 HTTPS 后 `tailscale serve --bg 8888`，再把 `SUB_URL` 换成 `https://<hostname>.ts.net/sub/...`。
- 管理面板与订阅服务默认 `0.0.0.0` 监听：请用 Tailscale ACL 或系统防火墙限制访问。

## 配置（环境变量）

在 `.state/webui.env` / `.state/sub.env` 中按需覆写（`install.sh` 会生成样例）：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `VULTR_API_KEY` | 读 `~/.config/vultr-clash/api_key` | Vultr API Key |
| `VULTR_WEB_HOST` | `0.0.0.0` | Web 管理界面监听地址 |
| `VULTR_WEB_PORT` | `8333` | Web 管理界面端口 |
| `SUB_URL` | `http://localhost:8888/sub/clash.yaml` | 展示给用户的订阅链接基址（按 Tailnet 地址覆盖） |
| `SUB_PORT` | `8888` | 订阅服务端口 |
| `PUB_HOST` | `localhost` | 订阅链接中展示的主机地址 |
| `SUB_UPDATE_INTERVAL` | `14400` | 客户端自动更新订阅间隔（秒） |
| `SUB_PATH` | `/sub/clash.yaml` | 订阅路径（token 路径在此基础上生成） |
| `SUB_AUTH` | `off` | 未来版本预留给强制鉴权开关（当前由 `.state/sub_token` 存在性决定） |

## CLI 用法

```bash
VULTR_API_KEY=xxx bash vultr-clash.sh create ewr vc2-1c-0.5gb  # 创建
VULTR_API_KEY=xxx bash vultr-clash.sh update                   # 同步订阅(从 VPS 读真实密码)
VULTR_API_KEY=xxx bash vultr-clash.sh destroy                  # 销毁(自动结算计费)
VULTR_API_KEY=xxx bash vultr-clash.sh connect                  # 打印订阅链接/ss:// 链接
```

## 免责声明

本项目仅用于学习与合法用途。请遵守当地法律法规及 Vultr 服务条款，勿用于任何非法访问。