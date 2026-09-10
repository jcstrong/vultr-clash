# vultr-clash

Vultr Shadowsocks 节点管理 + Clash 订阅的一体化工具（v0）。

提供 Web 管理界面（创建 / 销毁 / 更新节点），自动在 Vultr 云主机上部署 Shadowsocks，并生成可供 Clash / Shadowrocket 使用的订阅链接。

## 功能

- **Web 管理界面**：创建、销毁节点，更新订阅，实时查看进度日志
- **节点状态**：当前节点、套餐、IP、SS 密码、创建用时
- **费用信息**：账户余额、当前节点费用、历史节点消耗（精确到小时的按量计费）
- **套餐联动**：新泽西(ewr) ↔ $3.5，其他地区 ↔ $5，默认新泽西 + $3.5
- **订阅服务**：HTTP 提供 `clash.yaml`，Tailscale 内网 / 公网均可访问
- **自愈**：自动放行 VPS 防火墙 8388 端口、重启 shadowsocks 服务、从 VPS 读取真实密码（避免密码漂移）
- **IP 复用安全**：SSH 使用隔离 known_hosts，Vultr 复用 IP 也不报 host key 冲突

## 架构

```
┌────────────┐    create/destroy/update     ┌───────────────┐
│  Web UI    │ ───────────────────────────► │  Vultr API    │
│ :8333      │                              │ (你的 API Key)│
└────────────┘                              └───────┬───────┘
      │                                            │ 部署 cloud-init
      │ GET /sub/clash.yaml                        ▼
┌─────┴───────┐  订阅                          ┌───────────────┐
│ Sub Server  │ ◄─────────────────────────── │   VPS 节点     │
│ :8888       │                              │  shadowsocks   │
└─────────────┘                             └───────────────┘
```

- `webui.py` — Web 管理界面（:8333）
- `serve_sub.py` — 订阅服务，暴露 `clash.yaml`（:8888）
- `vultr-clash.sh` — 核心脚本：Vultr API 调用、计费记账、SSH 部署/自愈
- `cloud-init.yaml` — VPS 初始化模板（安装 shadowsocks、放行防火墙）

## 安装

依赖：`python3`、`bash`、`curl`、`jq`、`sshpass`、`openssl`、Vultr API Key。

```bash
git clone https://github.com/<你的用户名>/vultr-clash.git
cd vultr-clash

# 保存 API Key（或用环境变量 VULTR_API_KEY）
mkdir -p ~/.config/vultr-clash
echo "你的_VULTR_API_KEY" > ~/.config/vultr-clash/api_key
chmod 600 ~/.config/vultr-clash/api_key

# 启动 Web 界面（:8333）
python3 webui.py

# 启动订阅服务（:8888）
python3 serve_sub.py
```

### systemd 示例

```ini
# /etc/systemd/system/vultr-webui.service
[Unit]
Description=Vultr Clash WebUI
After=network.target

[Service]
User=youruser
WorkingDirectory=/path/to/vultr-clash
Environment=SUB_URL=http://你的地址:8888/sub/clash.yaml
ExecStart=/usr/bin/python3 webui.py
Restart=always

[Install]
WantedBy=multi-user.target
```

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `VULTR_API_KEY` | 读 `~/.config/vultr-clash/api_key` | Vultr API Key |
| `VULTR_WEB_PORT` | `8333` | Web 管理界面端口 |
| `SUB_PORT` | `8888` | 订阅服务端口 |
| `PUB_HOST` | `localhost` | 订阅链接中展示的主机地址 |
| `SUB_URL` | `http://localhost:8888/sub/clash.yaml` | 展示给用户的完整订阅链接（建议按实际部署地址覆盖） |

## 使用

1. 打开 Web 界面，粘贴 Vultr API Key 并保存
2. 选择地区/套餐（默认新泽西 + $3.5），点击「创建节点」
3. 等待节点初始化完成（SSH + 8388 就绪后，「更新订阅」按钮才可用）
4. 在 Clash / Shadowrocket 中填入订阅链接，即可使用

## 免责声明

本项目仅用于学习与合法用途。请遵守当地法律法规及 Vultr 服务条款，勿用于任何非法访问。