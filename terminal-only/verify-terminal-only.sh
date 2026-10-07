#!/usr/bin/env bash
# terminal-only/verify-terminal-only.sh — 终端-only 部署自检
# 用法：以 bridge 用户（或可读其 runtime 的用户）运行：./terminal-only/verify-terminal-only.sh

set -uo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
CANVAS_DISPLAY="${CANVAS_DISPLAY:-:50}"
pass=0; fail=0; warn_n=0

ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; pass=$((pass+1)); }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; fail=$((fail+1)); }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; warn_n=$((warn_n+1)); }

echo "── 1. 画布（vnc relay 像素源）──"
if DISPLAY="$CANVAS_DISPLAY" timeout 5 xset q >/dev/null 2>&1; then
    ok "Xvfb $CANVAS_DISPLAY 在线"
else
    bad "Xvfb $CANVAS_DISPLAY 无响应（systemctl --user status uu-canvas-session）"
fi
if systemctl --user is-active --quiet uu-canvas-session.service 2>/dev/null; then
    ok "uu-canvas-session.service active"
else
    warn "uu-canvas-session.service 未激活（若用其他方式提供画布可忽略）"
fi

echo "── 2. 上游桥服务 ──"
if systemctl --user is-active --quiet uu-remote-bridge.service 2>/dev/null; then
    ok "uu-remote-bridge.service active"
else
    bad "uu-remote-bridge.service 未运行（journalctl --user -u uu-remote-bridge -n 50）"
fi
runtime_dir="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/uu-remote-bridge"
if [[ -f "$runtime_dir/private-display" ]]; then
    ok "bridge 私有画布 display 已注册（$(cat "$runtime_dir/private-display")）"
else
    warn "未找到 private-display runtime（bridge 未完成启动或路径不同）"
fi

echo "── 3. UU 进程与在线态 ──"
if pgrep -f 'C:.*GameViewerServer\.exe' >/dev/null 2>&1; then
    ok "UU GameViewerServer 进程存在（Wine 内）"
else
    bad "未发现 UU 进程（Wine prefix 未启动或安装未完成）"
fi
connection_status="$("$repo_dir/scripts/uu_connection_status.py" 2>/dev/null |
    sed -n 's/^status=//p')"
if [[ "$connection_status" == "recent" ]]; then
    ok "UU 有近期的控制会话"
elif [[ "$connection_status" == "stale" ]]; then
    warn "UU 会话记录已过期（超过 5 分钟未连接）"
else
    warn "UU 在线态未确认——未登录？浏览器走 noVNC 扫码：ssh -L 6080:127.0.0.1:6080 后开 http://127.0.0.1:6080"
fi

echo "── 4. 终端桥组件 ──"
wine_prefix="${UURB_WINEPREFIX:-${WINEPREFIX:-$HOME/.local/share/wineprefixes/uu-remote}}"
if [[ -f "$wine_prefix/compat/uu-terminal-proxy.exe" ]]; then
    ok "uu-terminal-proxy.exe 已安装"
else
    warn "未定位到 uu-terminal-proxy.exe（应在 $wine_prefix/compat/）"
fi
shim_dll="$wine_prefix/compat/uu-conpty.dll"
conpty_dll="$wine_prefix/drive_c/Program Files/Netease/GameViewer/bin/conpty.dll"
if [[ -f "$shim_dll" && -f "$conpty_dll" ]] && cmp -s "$shim_dll" "$conpty_dll"; then
    ok "conpty.dll shim 已部署"
else
    warn "conpty.dll shim 未部署/不一致（broker 启动时会重新部署；无 shim 则终端不可用）"
fi

echo "── 5. 资源画像 ──"
free -h | awk 'NR<=2{printf "  %s\n",$0}'
wine_rss=$(ps -eo rss,comm --sort=-rss | awk '/wine|GameViewer/{s+=$1} END{print int(s/1024)}')
[[ -n "$wine_rss" && "$wine_rss" -gt 0 ]] 2>/dev/null && warn "Wine/UU 相关内存约 ${wine_rss}MB（基线记录用）" || true

echo
echo "══ 结果：$pass 通过 / $fail 失败 / $warn_n 提示 ══"
(( fail == 0 )) || exit 1
echo "终端功能最终确认：PC UU 控制端 → 设备 → Terminal → PowerShell 入口 → 应得到 Ubuntu shell"
