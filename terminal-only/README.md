# Terminal-Only 裁剪版

> 目标：Ubuntu 服务器只保留 UU 终端通道（PC/手机控制端打开服务器真实 shell），
> 跳过 GNOME/GDM 桌面栈，内存从 ~1.8GB 降到 ~1GB。
> 原理与决策链见实施包文档（IMPLEMENTATION-PLAN.md 与 reports/，仅随实施包分发，不入本仓库）。

## 为什么可行（上游代码事实）

1. 像素链路是 `target 画布 → relay(Wine 内) → UU 私有画布 → UU 抓取`，relay 支持 **vnc 模式**连任意精确 X display（`--desktop-target :N`，"explicit targets never fall back"）
2. bridge 自己拉起 UU 私有画布（`scripts/uu-remote-bridge` 内 `/usr/bin/Xvfb`）——**不依赖 GDM/GNOME 会话**
3. 上游 apt 包清单本就是扁平轻量件（xvfb/openbox/x11vnc），无 ubuntu-desktop/gdm
4. 登录辅助上游现成：`uu-remote-console.service` 的 noVNC web（默认 :6080）直接看 UU 私有画布

## 三步安装（服务器上，repo 根目录执行）

```bash
./terminal-only/install-terminal-only.sh
```

以**普通用户**运行（上游 install.sh 拒绝 root；apt 特权步骤由脚本按需调 sudo）。
脚本自动完成：

1. 前置检查（Ubuntu 22.04–26.04、内存、磁盘、UU 下载接口可达性）
2. 安装 Xvfb 并部署 `uu-canvas-session` 用户单元（画布**先于**上游安装启动）
3. 以上游参数调用 `./install.sh`：
   `--desktop-target :50 --desktop-relay vnc --resolution 1280x800 --skip-account-login`
4. 部署后自检（画布/桥服务状态）

**对上游的唯一偏离**：`install.sh` 在 `--desktop-relay vnc` 时跳过 GNOME RDP
配置段（TLS/grdctl/keyring 交互提示），改为非交互生成 relay 凭据——无头服务器
没有 GNOME 会话，该段原本必然卡死。vnc 模式实际需要的只有回环 VNC 凭据。
其余上游代码零修改。

## 登录（一次性，registry 持久后不再需要）

```bash
# 本地电脑开隧道后，浏览器打开 http://127.0.0.1:6080
ssh -L 6080:127.0.0.1:6080 user@server
# noVNC 里看到 UU 窗口 → 手机 UU App 扫码 → 登录态写入 wineprefix 注册表
./scripts/uu_connection_status.py   # 确认在线
```

备援（noVNC 不可用时）：`scripts/uu-remote open` + `import -window root` 截图，
截图辅助脚本见实施包文档附录 A。

## 验证

```bash
./terminal-only/verify-terminal-only.sh   # 画布/服务/终端链路/内存
./scripts/test-terminal-bridge.sh         # 上游终端桥端到端测试
```

PC 控制端 → 设备 → Terminal 面板 → `PowerShell` 入口（兼容占位标签）→ 得到 Ubuntu 登录 shell。
手机控制端 Terminal 入口依 App 版本而定；无入口时手机装 JuiceSSH 走 `scripts/uu-ssh` 别名。

## 回退完整桌面版

```bash
./terminal-only/rollback-desktop.sh       # 移除画布单元
sudo ./uninstall.sh --dry-run && sudo ./uninstall.sh
# 然后按上游 README 常规安装（relay=rdp + GNOME 会话）
```

## 已知限制

- 无 GNOME 会话 → 无 GNOME Keyring：RDP 凭据存储不可用（vnc relay 模式不需要它）；
  若上游 keyring 相关单元告警，属预期，见 verify 输出提示
- `--unattended`（TPM 自启）依赖 GDM autologin，terminal-only 模式不适用
- relay 像素仍走 Wine，但只渲染 1280x800 画布，CPU 占用远低于 1080p 桌面流
