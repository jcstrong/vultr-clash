# RECOVERY — 恢复与运维文档（面向 Agent）

> 生成：2026-09-22。本仓库是公开的运维工具仓库；涉及私有基础设施的地址/账号以运维侧私有记录（服务器 AGENTS.md / agent memory）为准，本文用 `<server>`、`<tailscale-ip>` 指代。

## 1. 服务身份

| 项 | 值 |
|---|---|
| 服务 | vultr-clash：Vultr Shadowsocks 节点管理 + Clash 订阅 |
| Web 管理 | `http://<tailscale-ip>:8333/`（`webui.py`，systemd 单元 `vultr-webui`） |
| 订阅服务 | `:8888`（`serve_sub.py`，token 鉴权可选：`/sub/{token}/clash.yaml`） |
| 部署目录 | 服务器 `~/vultr-clash/`（本仓库的活工作区，git 管理） |
| 守卫 | `node-guard.sh`，cron `*/30`：探测 8388/22 → 连续 2 败判被封 → 销毁+结算 → 对侧重建（ewr/$3.5 ↔ nrt/$5 互换，每日上限 2 次）→ Bark 通知 |

## 2. 架构速览

`webui.py`（前后端单文件：后端 do_POST action 分发 + 前端 PAGE/SETUP_PAGE 字符串）→ `vultr-clash.sh`（Vultr API、计费、SSH 部署 cloud-init.yaml）→ VPS 跑 shadowsocks（8388）→ `serve_sub.py` 用 `clash.yaml`（生成物）供订阅。

**重启规则**：只有改 `webui.py` 需要 `sudo -n systemctl restart vultr-webui`；`vultr-clash.sh` / `node-guard.sh` / `.state/` 配置即改即生效（每次调用重新读取；调度器每 20s 重读 schedule.json）。

## 3. 备份机制（2026-09-22 起）

- **本仓库即备份**：服务器 `~/vultr-clash` 是活 git 仓库，cron 每日 04:30 运行 `~/backups/vultr-clash-backup/backup.sh`：`git add -A` → 变化才 commit（前缀 `auto-backup:`，便于与人写的提交区分）→ push。
- 推送通道：origin 已切到 SSH deploy key 直连（别名 `github-vultr-clash`，密钥 `~/.ssh/vultr_clash_ed25519`）。**服务器 HTTPS 直推 GitHub 会 GnuTLS reset**（国内网络），以前靠 Mac 中继推送，现在 SSH 直连即可；旧 HTTPS remote 保留为 `https` 备用。
- **`.state/` 永不入库**（.gitignore 已排除）：运行时状态+密钥——`api_key`（真实位置 `~/.config/vultr-clash/api_key`，KEY_FILE 硬编码 `~/.config/vultr-clash/api_key`）、`instance.json`（节点）、`sub_token`（订阅鉴权）、`schedule.json`（定时销毁/创建 + bark_url）、`billing.json`（计费）、`guard_state.json`（守卫计数）、`progress.log`。**重建环境时这些要单独迁移或 UI 重配。**

## 4. 恢复步骤

```bash
# 1) 取回代码
git clone git@github.com:jcstrong/vultr-clash.git ~/vultr-clash && cd ~/vultr-clash

# 2) 安装服务（依赖检查 + systemd/launchd 自适应）
./install.sh              # status | start | stop | restart | uninstall | selftest

# 3) 恢复运行时状态（从旧机 .state/ 整目录 rsync；没有就 UI 引导重配）
#    必要项：~/.config/vultr-clash/api_key（Vultr API Key）
#    可选项：.state/{schedule.json, sub_token, billing.json, guard_state.json}
mkdir -p ~/.config/vultr-clash && rsync -a <旧机>/.state/ ~/vultr-clash/.state/

# 4) 守卫定时任务
./install.sh guard install && ./install.sh guard status

# 5) 验证
curl http://127.0.0.1:8333/ | head            # 管理页/初始化页
POST /api {"action":"selftest"}               # 依赖自检
POST /api {"action":"status"}                 # 节点状态（无节点时 clash.yaml 为 stub）
```

## 5. 已知坑与运维要点（实测）

- **UI 编辑入口**：线上页面是 `webui.py` 内嵌 `PAGE`/`SETUP_PAGE` 字符串；根目录 `page.html`/`page.js` 只是离线 DOM 测试副本，改了不生效
- **clash.yaml 是生成物**：别手编；规则源头在 `vultr-clash.sh` 的 `update_clash_yaml()` 里 `rules:` 列表（既有约定：mini4k.com → Proxy，mini4k.net → DIRECT）
- **ewr 会连续发放已封 IP**（黑洞 ping 100% 丢）：创建卡"等待 SSH"超时=实例在计费但未初始化；先从外部 `nc -z <ip> 22` 分辨——22 通则 `POST {"action":"update"}` 抢救，不通则销毁换区重建（nrt 顶过 ewr 一天两连封）
- **guard 不会误杀**：本地无节点直接跳过；Vultr API 不可达跳过本轮；IP 与记录不一致先自同步再探
- 长任务必须走 `run_action()`（动作锁+进度日志），否则 UI 按钮和日志页失效
- rsync 部署**只传改动文件**：本地冒烟测试会写 `.state/` 垃圾，整目录 rsync 会冲掉活的节点/计费状态
- 冒烟测试用 `HOME=/tmp/vc-home` + dummy api_key（渲染管理页而非初始化页）
- 提交惯例：VERSION + README 同批更新（v0.2.0 起）；repo 常有未提交的手工改动，自动备份以 `auto-backup:` 前缀整体快照，人的提交自己写信息

## 6. 相关资源

- 项目 README（功能与配置详解）：本仓库
- 运维侧私有 skills（Mac workshop）：`vultr-clash-ops`（断网分诊/订阅应用到 Clash Verge）、`vultr-clash-changes`（开发改动流程）、`github-push-relay`（HTTPS 中继推送，本仓库改 SSH 直连后已非必需）
- 同机备份流水线：homarr-backup（04:45）、immich-backup（04:15）、bambu-backup（05:00）
