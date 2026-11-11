# 网络诊断考题 — SS Node 项目（2026-11-11）

> 欢迎。这是一台国内家庭宽带（联通）上的 Ubuntu 服务器，跑着一个全自动化的 Vultr SS 节点项目。
> 它从前天开始出了点问题，我们把所有现象列在下面，但**没有任何结论**。
> 你的任务：判断故障在哪一层、什么性质，并给出你的验证思路和恢复预期。
> 你在这台机器上有完全的 shell 权限，可以随意跑探测命令。

## 项目架构

- 本机：Ubuntu（LAN 192.168.31.162 / Tailscale 100.90.80.9），联通宽带
- 节点：Vultr VPS 按需创建（可选区 ewr/nrt/lax/sjc/sea），装机后自动部署 Shadowsocks（端口 8388）
- 用途：本机 mihomo（127.0.0.1:7890，混合端口）的主出口，全家旁路由/代理设备共用
- 自动化：每天 00:30 自动销毁节点（省钱）、次日凌晨 00:05 自动重建；SSH 不通即自动销毁止损、换区重试（最多 5 次）
- 管理面板：http://127.0.0.1:8333（本机可访问）

## 关键路径

- 项目脚本：~/vultr-clash/vultr-clash.sh（create/destroy/update 逻辑全在这）
- 行为日志：~/vultr-clash/.state/progress.log（两天完整记录，含每次尝试的时间戳/IP/结果）
- 实例状态：~/vultr-clash/.state/instance.json
- mihomo 配置：~/.config/mihomo/config.yaml（API 127.0.0.1:9090，secret 在配置文件里）
- Vultr API Key：~/.config/vultr-clash/api_key（查实例/套餐/区域用，勿外传）

## 现象时间线（无结论，纯事实）

### 11-10
- 五个区各建一次实例：ewr / nrt / lax / sjc / sea，全部创建成功（Vultr API status=active）
- 六个新 IP 的 TCP 22 探测全部超时，100 秒窗口无一成功。示例 IP：
  149.28.48.230(ewr) / 149.28.10.202(sea) / 45.77.126.16(lax) / 149.28.200.131(sjc) / 45.32.27.14(nrt)
- 每次超时后自动销毁止损，Vultr API 终态无残留实例

### 11-11
- Vultr 把 11-10 刚失败过的两个 IP **原封不动又分配了回来**：149.28.48.230(ewr)、45.77.126.16(lax)
- 探测结果与昨日完全一致（同样超时）

### 探测行为细节（nc -zvw5 <ip> 22，直连无代理）
- 多数 IP：connection timed out
- 个别 IP 在不同时刻：No route to host（路由级拒绝）

### 同一时刻的对照数据（同一条联通宽带、同一设备）
| 目标 | 结果 |
|---|---|
| 104.16.132.229:443（Cloudflare） | TCP 秒连成功 |
| 国内任意站点 | 正常 |
| api.vultr.com:443 | 解析出 IPv6 地址（2001:19f0:...）且连接成功（未单独测其 IPv4） |
| 上述 Vultr 实例 IP 的 22/443/8388 | 全部超时或 No route |

### 其他观察
- Vultr 控制台侧实例 power_status=running，Vultr 不报异常
- Tailscale 控制面/DERP 中继正常
- 家里另一台 macOS 走另一条联通出口（手机热点 CGNAT），国内与 Cloudflare 同样正常
- 11-10 与 11-11 两天的表现完全一致，无好转迹象

## 你可以立即使用的入口

- ssh 登录本机：LAN 内 ssh jun@192.168.31.162，密码 7890（或 Tailscale：jun@100.90.80.9）
- 查完整日志：cat ~/vultr-clash/.state/progress.log
- 跑探测：nc -zvw5 <ip> <port>；curl 直连加 --noproxy '*'
- 查 Vultr 侧实例与套餐库存：用上面 API Key 调 https://api.vultr.com/v2/instances、/v2/plans、/v2/regions
- mihomo API：curl -s -H "Authorization: Bearer <secret>" http://127.0.0.1:9090/proxies

## 考题

1. 故障发生在哪一层？（链路层/路由层/运营商互联/远端服务器/本地配置——说出你的判断和依据）
2. 给出至少两个可执行的进一步验证手段（可以在本机上跑）
3. 恢复预期：会自己好吗？需要多久？有什么绕行方案？

（你跑命令产生的输出自己分析；项目脚本逻辑如果怀疑有问题，也可以读源码质疑）
