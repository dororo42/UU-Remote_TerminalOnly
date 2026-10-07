<div align="center">

# UU-Remote_TerminalOnly

**让无头 Ubuntu 服务器接入 UU 远程：PC / 手机控制端一键打开服务器的真实终端。**

[![CI](https://github.com/dororo42/UU-Remote_TerminalOnly/actions/workflows/ci.yml/badge.svg)](https://github.com/dororo42/UU-Remote_TerminalOnly/actions/workflows/ci.yml)
[![Upstream](https://img.shields.io/badge/upstream-llmir%2Fuu--remote--ubuntu--plus_v0.1.0-2F81F7)](https://github.com/llmir/uu-remote-ubuntu-plus)
[![License](https://img.shields.io/badge/license-MIT_%2B_AGPL--3.0_components-blue)](#许可)

</div>

## 这是什么

基于 [llmir/uu-remote-ubuntu-plus](https://github.com/llmir/uu-remote-ubuntu-plus) v0.1.0（MIT）的 **terminal-only 无头发行版**：在无桌面环境（无 GNOME/GDM）的 Ubuntu 服务器上运行网易 UU 远程被控端，跳过整条桌面投屏栈，只保留**终端通道**。适用于 VPS / 实验室服务器 / 家用 NAS——PC 与手机 UU 控制端在设备列表中点开 Terminal，即可获得服务器的真实登录 shell。

- 被控端运行在独立 Wine 环境中（GameViewerServer 4.42.0.2770，钉版 + SHA-256 补丁校验）
- 内存占用约 0.8 GB（完整桌面方案约 1.8 GB），空闲 CPU < 8% 单核
- 终端会话持久化（协议 v2 锚定会话，断开重连不丢 shell）
- 登录一次后凭据持久保存在 wineprefix 注册表，重启 / 升级免重登

## 主要特点（相对上游）

| 特点 | 说明 |
| --- | --- |
| **无头裁剪** | vnc relay + 自建 Xvfb `:50` 画布替代 GNOME RDP 桌面投屏；安装、画布、服务、自检一条命令完成（`terminal-only/`） |
| **终端契约修复** | UU 4.39+ 的终端启动依赖 `conpty_bridge → conpty.dll → uuyc-mux` 契约；本仓嫁接 [GaryOAO/UUWay](https://github.com/GaryOAO/UUWay) 的三件套（conpty shim / mux 代理 / 协议 v2 PTY broker）修复了上游 375 行代理无法通过该契约的问题 |
| **会话持久化** | PTY broker 协议 v2：锚定会话 + SCM_RIGHTS holder，控制端断线重连回到同一 shell，支持多会话 |
| **幂等部署与验证** | `install-terminal-only.sh` 幂等续装；`verify.sh --quick` 20 项检查；`test-terminal-bridge.sh` 端到端测试 |
| **4 项上游缺陷修复** | 无 GNOME 会话下的显式 display 选择、Wine 注册表空键崩溃、verify 时区解析、conpty 链路契约（见 `docs/` 与提交历史） |

## 安装（Ubuntu 22.04 – 26.04 x86_64，无桌面要求）

```bash
git clone https://github.com/dororo42/UU-Remote_TerminalOnly.git
cd UU-Remote_TerminalOnly
./terminal-only/install-terminal-only.sh   # 普通用户运行，按需 sudo
```

安装脚本自动完成：前置检查 → Xvfb 画布部署 → Wine + UU 4.42.0.2770 安装与补丁 → conpty shim 部署 → 桥服务启动与自检。

**首次登录（一次性）**：浏览器打开 noVNC 控制台（`http://<服务器IP>:6080/vnc.html`，端口转发由 `uu-console-lan` 服务提供），使用手机 UU App 扫码；登录态写入 wineprefix 注册表后无需重登。

**日常验证与运维**：

```bash
./scripts/verify.sh --quick          # 20 项部署检查
./terminal-only/verify-terminal-only.sh
./scripts/uu-remote status|logs|restart|stop
```

**终端入口**：PC / 手机 UU 控制端 → 设备 → Terminal。shell 选择以面板实际显示为准（可能显示 cmd、PowerShell 或不显示标签）——无论哪个入口，打开的都是 **Ubuntu 登录 shell**（Windows 侧只是占位宿主）。另有官方端口映射 + SSH 路径（`docs/ssh-and-port-mapping.md`）与 `scripts/uu-ssh` 助手。

## 项目参考来源与谱系

| 来源 | 关系 |
| --- | --- |
| [llmir/uu-remote-ubuntu-plus](https://github.com/llmir/uu-remote-ubuntu-plus)（MIT） | **上游基座**（v0.1.0 快照 + 本仓维护）：安装器、补丁引擎、Wine 编排、画布与桥服务 |
| [lachlanchen/uu-remote-ubuntu-bridge](https://github.com/lachlanchen/uu-remote-ubuntu-bridge)（MIT） | 谱系源头（RDP 中继初代） |
| [GaryOAO/UUWay](https://github.com/GaryOAO/UUWay)（AGPL-3.0） | **终端三件套嫁接来源**：conpty shim、mux 代理、协议 v2 PTY broker（`src/uu_conpty_shim.*`、`src/uu_terminal_proxy.c`、`src/uu_terminal_bridge.c`，commit f192f65） |
| [panxuc/uur](https://github.com/panxuc/uur)（MIT） | mux 契约的独立交叉印证 |
| [NetEase UU 远程](https://uuyc.163.com/) | 被控端程序本体（闭源，钉版 4.42.0.2770）；本仓库不含其任何二进制 |

也感谢官方 Mac 被控端"tmux 会话复用器"设计（[AlliotTech/uu-tmux](https://github.com/AlliotTech/uu-tmux) 逆向揭示）对本项目终端架构的启发。

## 文档

`docs/` 保留终端运营核心子集：[原生终端链路](docs/native-ubuntu-terminal.md) · [SSH 与端口映射](docs/ssh-and-port-mapping.md) · [故障排查](docs/troubleshooting.md) · [安全模型](docs/security.md) · [架构](docs/architecture.md) · [无启动部署](docs/unattended-startup.md) · [源码构建](docs/source-build.md) · [Ubuntu 26.04 移植](docs/ubuntu-26.04-port.md)。完整上游文档见上游仓库。

实施书、交接单与调研报告不随仓库分发（本地 `implementation-kit/`，已 gitignore）。

## 许可

- 上游衍生代码：MIT（继承 llmir/uu-remote-ubuntu-plus）
- `src/uu_conpty_shim.*`、`src/uu_terminal_proxy.c`、`src/uu_terminal_bridge.c`、`terminal_bridge_protocol.h`：来自 UUWay，**AGPL-3.0**（见 `LICENSE.UUWay`），合并作品相应部分按 AGPL-3.0 提供
- UU Remote 为网易产品，遵守其服务条款；补丁与 shim 仅限自用，请自行评估合规边界。本项目为独立社区项目，与网易无关联。
