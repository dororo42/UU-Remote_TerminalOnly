#!/usr/bin/env bash
# terminal-only/rollback-desktop.sh — 回退终端-only 裁剪
# 只移除本目录新增的画布单元；上游安装物用上游 uninstall.sh 处理。

set -euo pipefail

log() { printf '\033[1;34m[rollback]\033[0m %s\n' "$*"; }

if systemctl --user list-unit-files 2>/dev/null | grep -q '^uu-canvas-session'; then
    systemctl --user disable --now uu-canvas-session.service || true
    rm -f "$HOME/.config/systemd/user/uu-canvas-session.service"
    systemctl --user daemon-reload
    log "画布单元已移除"
else
    log "未发现 uu-canvas-session 单元（可能未安装）"
fi

log "如需完整移除 UU 远程：./uninstall.sh --dry-run 预览后 ./uninstall.sh（保留账号态）"
log "如需恢复完整桌面版：按上游 README 安装（默认 relay=rdp + GNOME 会话），本目录无残留"
