#!/usr/bin/env python3
"""Launch desktop tools with their own fonts and explicit connection choices."""
import argparse
import ipaddress
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys


def font_config():
    installed = Path.home() / ".local/share/uu-remote/tools/uu-tools-fonts.conf"
    source = Path(__file__).resolve().parents[1] / "desktop/uu-tools-fonts.conf"
    for path in (installed, source):
        if path.is_file():
            return str(path)
    raise ValueError("The desktop tool font configuration is missing; reinstall the bridge.")


def dialog(*arguments):
    return subprocess.run(["/usr/bin/zenity", *arguments], capture_output=True, text=True)


def validate_address(address):
    address = address.strip()
    if not address or len(address) > 300 or re.search(r"[\s/\\@?#]", address):
        raise ValueError("请输入服务器名称或 IP 地址，可附端口；不要输入网址或命令参数。")
    port = None
    if address.startswith("["):
        match = re.fullmatch(r"\[([^]]+)\](?::([0-9]+))?", address)
        if not match:
            raise ValueError("IPv6 地址请使用 [地址]:端口 格式。")
        host, port = match.groups()
        ipaddress.IPv6Address(host)
    elif address.count(":") > 1:
        ipaddress.IPv6Address(address)
        return "[" + address + "]"
    else:
        host, separator, port = address.partition(":")
        port = port if separator else None
        if re.fullmatch(r"[0-9.]+", host):
            ipaddress.IPv4Address(host)
        elif len(host) > 253 or not all(
                re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                for label in host.rstrip(".").split(".")):
            raise ValueError("服务器名称无效。")
    if port is not None and (not port.isascii() or not port.isdigit() or not 1 <= int(port) <= 65535):
        raise ValueError("端口必须介于 1 和 65535。")
    return address


def validate_username(username):
    if len(username) > 256 or any(ord(character) < 32 or ord(character) == 127 for character in username):
        raise ValueError("用户名不能包含控制字符。")
    return username


def protect_bridge_rdp(address):
    """Do not let a manual client take over the bridge's local RDP session."""
    if address.startswith("["):
        host, suffix = address[1:].split("]", 1)
        port = int(suffix[1:]) if suffix else 3389
    else:
        host, separator, suffix = address.partition(":")
        port = int(suffix) if separator else 3389
    try:
        local = ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = host.rstrip(".").casefold() in {
            "localhost", socket.gethostname().rstrip(".").casefold()}
    if not local:
        return
    relay_port = 3390
    try:
        settings = (Path.home() / ".config/uu-remote-bridge/environment").read_text(encoding="utf-8")
        saved = [line.partition("=")[2].strip() for line in settings.splitlines()
                 if line.startswith("UURB_RDP_PORT=")]
        if saved and re.fullmatch(r"[0-9]{1,5}", saved[-1]) and 1 <= int(saved[-1]) <= 65535:
            relay_port = int(saved[-1])
    except (OSError, UnicodeError):
        pass
    if port == relay_port:
        raise ValueError(f"本机端口 {relay_port} 是 UU 内部桌面中继。请使用 uu-remote open 打开 UU；此工具请输入实际外部服务器，避免中断 UU 远控。")


def run_viewer():
    env = dict(os.environ, FONTCONFIG_FILE=font_config())
    return subprocess.run(["/usr/bin/xtigervncviewer"], env=env).returncode


def run_rdp(address, username):
    address = validate_address(address)
    protect_bridge_rdp(address)
    username = validate_username(username)
    command = ["/usr/bin/xfreerdp", "/v:" + address, "/from-stdin:force", "/network:auto"]
    if username:
        command.append("/u:" + username)
    print("FreeRDP 将在终端询问凭据；请确认服务器证书后连接。", flush=True)
    result = subprocess.run(command)
    print(f"FreeRDP 连接已结束（退出码 {result.returncode}）。", flush=True)
    if sys.stdin.isatty():
        try:
            input("按 Enter 关闭终端。")
        except EOFError:
            pass
    return result.returncode


def choose_rdp():
    result = dialog("--forms", "--title=FreeRDP · 连接远程桌面",
                    "--text=输入你要连接的服务器。密码将在终端询问；证书按 FreeRDP 默认策略验证。",
                    "--add-entry=服务器地址（例如 server.example:3389）",
                    "--add-entry=用户名（可留空）", "--separator=|")
    if result.returncode == 1:
        return 0
    if result.returncode or len(result.stdout.strip().split("|")) != 2:
        raise ValueError("连接窗口未返回有效的地址和用户名。")
    address, username = result.stdout.strip().split("|")
    address = validate_address(address)
    protect_bridge_rdp(address)
    username = validate_username(username)
    terminal = shutil.which("xdg-terminal-exec")
    if terminal:
        command = [terminal]
    else:
        terminal = shutil.which("x-terminal-emulator")
        if not terminal:
            raise ValueError("没有找到终端程序，无法安全输入远程桌面密码。")
        command = [terminal, "-e"]
    return subprocess.run([*command, "/usr/bin/python3", str(Path(__file__).resolve()),
                           "rdp-run", "--", address, username]).returncode


def run_openbox():
    result = dialog("--info", "--title=Openbox 配置管理器",
                    "--text=此工具编辑 Openbox 的主题和窗口设置。当前 GNOME 桌面由 GNOME 管理，Openbox 设置不会改变 GNOME 的窗口外观。",
                    "--ok-label=打开配置管理器")
    if result.returncode:
        return 0 if result.returncode == 1 else result.returncode
    dpi_scale = "1"
    try:
        resources = subprocess.run(["/usr/bin/xrdb", "-query"], capture_output=True,
                                   text=True, timeout=3)
        matches = re.findall(r"^Xft\.dpi:\s*([0-9]+(?:\.[0-9]+)?)\s*$",
                             resources.stdout or "", re.MULTILINE)
        if resources.returncode == 0 and matches:
            dpi = float(matches[-1])
            if 72 <= dpi <= 384:
                dpi_scale = f"{96 / dpi:g}"
    except (OSError, subprocess.TimeoutExpired):
        pass
    env = dict(os.environ, GDK_BACKEND="x11", GDK_SCALE="1", GDK_DPI_SCALE=dpi_scale)
    return subprocess.run(["/usr/bin/obconf"], env=env).returncode


def run_x11vnc():
    result = dialog("--list", "--radiolist", "--title=X11VNC · 查看配置",
                    "--text=X11VNC 共享 X11 桌面，不能完整采集当前 Wayland 桌面。查看配置不会开启服务器。请选择查看配置，或使用 VNC 查看器、UU远程进行远控。",
                    "--column=选择", "--column=操作", "--print-column=2",
                    "TRUE", "查看 X11VNC 配置", "FALSE", "打开 VNC 查看器", "FALSE", "打开 UU远程")
    if result.returncode == 1:
        return 0
    if result.returncode:
        raise ValueError("无法打开 X11VNC 选项窗口。")
    choice = result.stdout.strip()
    if choice == "打开 VNC 查看器":
        return run_viewer()
    if choice == "打开 UU远程":
        return subprocess.run([str(Path.home() / ".local/bin/uu-remote"), "open"]).returncode
    if choice != "查看 X11VNC 配置":
        raise ValueError("请选择一个 X11VNC 操作。")
    env = dict(os.environ, PATH="/usr/bin:/bin",
               X11VNC_FONT_BOLD="{Noto Sans CJK SC} -18 bold",
               X11VNC_FONT_FIXED="{Noto Sans Mono CJK SC} -17",
               X11VNC_FONT_BOLD_SMALL="{Noto Sans CJK SC} -15 bold",
               X11VNC_FONT_REG_SMALL="{Noto Sans CJK SC} -15")
    return subprocess.run(["/usr/bin/x11vnc", "-norc", "-gui", "wait", "-localhost", "-no6"], env=env).returncode


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="tool", required=True)
    for tool in ("viewer", "rdp", "openbox", "x11vnc"):
        commands.add_parser(tool)
    run = commands.add_parser("rdp-run")
    run.add_argument("address")
    run.add_argument("username", nargs="?", default="")
    args = parser.parse_args(argv)
    try:
        if args.tool == "rdp-run":
            return run_rdp(args.address, args.username)
        if not os.environ.get("DISPLAY"):
            raise ValueError("请从登录后的图形桌面打开此工具。")
        return {"viewer": run_viewer, "rdp": choose_rdp,
                "openbox": run_openbox, "x11vnc": run_x11vnc}[args.tool]()
    except (ValueError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        if os.environ.get("DISPLAY"):
            try:
                dialog("--error", "--title=远程桌面工具", "--text=" + str(error))
            except OSError:
                pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
