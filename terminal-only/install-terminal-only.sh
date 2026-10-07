#!/usr/bin/env bash
# terminal-only/install-terminal-only.sh — UU 远程终端专用裁剪安装（编排上游 install.sh）
# 用法：./terminal-only/install-terminal-only.sh
#   以普通用户运行；apt 等特权步骤由脚本按需调用 sudo（上游 install.sh 拒绝 root）。
# 原理：vnc relay 连自建 Xvfb 画布（先于上游安装启动）；终端桥与上游完全一致；
#   首次 UU 账号登录改由 noVNC console（:6080）完成。
# 对上游的唯一偏离：install.sh 内 vnc 模式跳过 GNOME RDP 配置段（见 install.sh
#   "Terminal-only / headless deployment" 注释），其余零修改。
# 回退：terminal-only/rollback-desktop.sh + 上游 uninstall.sh

set -Eeuo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
self_dir="$repo_dir/terminal-only"
CANVAS_DISPLAY="${CANVAS_DISPLAY:-:50}"
CANVAS_RESOLUTION="${CANVAS_RESOLUTION:-1280x800}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

log()  { printf '\033[1;32m[terminal-only]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[terminal-only]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[terminal-only]\033[0m %s\n' "$*" >&2; exit 1; }

# ── 0. 特权模型：普通用户 + 可用 sudo ─────────────────────────
(( EUID != 0 )) || die "请用普通用户运行（上游 install.sh 拒绝 root，本脚本会按需调用 sudo）"
command -v sudo >/dev/null 2>&1 || die "未找到 sudo"
sudo -n true 2>/dev/null || warn "sudo 需要密码：安装过程中终端会提示输入"

# ── 1. 前置检查 ─────────────────────────────────────────────
command -v lsb_release >/dev/null 2>&1 || die "未找到 lsb_release，请确认 Ubuntu 系统"
ubu="$(lsb_release -rs)"
case "$ubu" in
    24.04|26.04) log "Ubuntu $ubu 检测通过（上游验证版本）" ;;
    22.04|24.10|25.04) warn "Ubuntu $ubu 非上游主验证版本，继续；失败时先换 24.04/26.04" ;;
    *) warn "Ubuntu $ubu 未在上游支持列表，继续但风险自负" ;;
esac

bridge_user="$(id -un)"
log "bridge 用户 = $bridge_user（上游约定：谁安装谁运行）"

free_mb=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)
(( free_mb >= 1800 )) || die "可用内存 ${free_mb}MB < 1800MB，至少需要 2GB 空闲"
disk_gb=$(df --output=avail -BG "$repo_dir" | tail -1 | tr -dc '0-9')
(( disk_gb >= 5 )) || die "磁盘可用 ${disk_gb}GB < 5GB"

curl -sfI --max-time 8 "https://api.nrd.nie.163.com/api/v1/release/dl/1?channel=gwqd" -o /dev/null \
    || die "无法访问网易 UU 下载接口（api.nrd.nie.163.com）"
log "资源与网络检查通过（内存 ${free_mb}MB / 磁盘 ${disk_gb}GB）"

# ── 2. 画布先行：装 Xvfb 并启动用户单元 ────────────────────────
# 先于上游安装启动画布，保证桥首启时 vnc relay 立即有像素源可用。
log "安装 Xvfb（画布依赖）"
sudo apt-get update -y
sudo apt-get install -y --no-install-recommends xvfb x11-utils

log "部署画布用户单元（Xvfb $CANVAS_DISPLAY，$CANVAS_RESOLUTION）"
install -d -m 0755 "$HOME/.config/systemd/user"
sed -e "s/:50/$CANVAS_DISPLAY/g" -e "s/1280x800/$CANVAS_RESOLUTION/g" \
    "$self_dir/uu-canvas-session.service" \
    > "$HOME/.config/systemd/user/uu-canvas-session.service"
systemctl --user daemon-reload
systemctl --user enable --now uu-canvas-session.service

canvas_ready=false
for _ in {1..30}; do
    if xdpyinfo -display "$CANVAS_DISPLAY" >/dev/null 2>&1; then
        canvas_ready=true
        break
    fi
    sleep 0.5
done
$canvas_ready || die "画布 $CANVAS_DISPLAY 未就绪（systemctl --user status uu-canvas-session）"
log "画布 $CANVAS_DISPLAY 就绪"

# ── 3. 上游安装（vnc relay + 精确 target + 跳过首次登录）───────
log "编译 conpty shim（mingw；绕开 build-compat.sh 的 FreeRDP 配方锁）"
mkdir -p build/compat
x86_64-w64-mingw32-gcc -std=c11 -O2 -Wall -Wextra -Werror \
    -Wl,--no-insert-timestamp -shared -I src \
    -o build/compat/uu-conpty.dll \
    src/uu_conpty_shim.c src/uu_conpty_shim.def -lws2_32
x86_64-w64-mingw32-strip build/compat/uu-conpty.dll

log "调用上游 install.sh（target=$CANVAS_DISPLAY relay=vnc $CANVAS_RESOLUTION，登录后置，桥由本脚本拉起）"
cd "$repo_dir"
./install.sh \
    --desktop-target "$CANVAS_DISPLAY" \
    --desktop-relay vnc \
    --resolution "$CANVAS_RESOLUTION" \
    --skip-account-login \
    --no-start

log "部署 conpty shim 到 prefix compat"
wine_prefix="${UURB_WINEPREFIX:-${WINEPREFIX:-$HOME/.local/share/wineprefixes/uu-remote}}"
/usr/bin/install -m 0644 build/compat/uu-conpty.dll \
    "$wine_prefix/compat/uu-conpty.dll"

# ── 4. 部署后自检 ───────────────────────────────────────────
log "启动桥并自检"
systemctl --user restart uu-remote-bridge.service
sleep 20
systemctl --user is-active --quiet uu-remote-bridge.service \
    && log "✓ uu-remote-bridge active" \
    || warn "✗ uu-remote-bridge 未运行：journalctl --user -u uu-remote-bridge -n 50"
systemctl --user is-active --quiet uu-canvas-session.service \
    && log "✓ uu-canvas-session active" \
    || warn "✗ uu-canvas-session 未激活"
if systemctl --user is-active --quiet uu-remote-bridge.service; then
    log "✓ uu-remote-bridge active"
else
    warn "✗ uu-remote-bridge 未运行：journalctl --user -u uu-remote-bridge -n 50"
fi
# 登录通道：noVNC console 直接看 UU 私有画布（上游仅在部分路径启用它）
systemctl --user enable --now uu-remote-console.service \
    && log "✓ noVNC console 已启用（http://127.0.0.1:6080）" \
    || warn "✗ uu-remote-console 启用失败：journalctl --user -u uu-remote-console"

cat <<'EOF'

════════════════════════════════════════════════════════
 安装完成。下一步（UU 账号登录，一次性）：
   方式 A（本机有桌面）：终端运行  uu-remote open  → 手机 UU App 扫码
   方式 B（无头/SSH）：  ssh -L 6080:127.0.0.1:6080 <user>@<server>
                         浏览器打开 http://127.0.0.1:6080 → 扫码
   登录态持久保存在 wineprefix 注册表，重启/升级后无需重登。
 验证：
   ./terminal-only/verify-terminal-only.sh
   ./scripts/test-terminal-bridge.sh
 说明与回退： terminal-only/README.md
════════════════════════════════════════════════════════
EOF
