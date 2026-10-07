#!/usr/bin/env python3
"""Select bridge canvas sizes and explain separate UU streaming controls."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time


PROFILES = {
    "720p": ("1280x720", "省流量"),
    "1080p": ("1920x1080", "流畅"),
    "1440p": ("2560x1440", "均衡"),
    "2160p": ("3840x2160", "清晰"),
}
GUIDE = """这些档位调整远控分辨率；UU 原生画质、码率和帧率由控制端分别设置。
电脑远控：右上角【控制中心】→【画质】，选择画质或帧率。
手机远控：侧边工具栏【操作】→【显示】，选择画质或帧率。
真彩：电脑【操作中心】→【显示】→【真彩】；手机【操作】→【显示】→【真彩4:4:4】。
真彩取决于控制端和编码能力，Android 暂不支持，具体以 UU 当前界面为准。
桥接画布提供 720p、1080p、1440p 和 4K；默认保留已保存的分辨率。
实际帧率未测定，端到端延迟也尚未测定。
高帧率或分辨率设置可能触发 UU 超级屏；Wine 中的虚拟显示驱动尚未验证，建议保留当前屏幕。
官方说明：https://uuyc.163.com/features/color/
帧率与超级屏：https://uuyc.163.com/help/superscreen.html
真彩兼容性：https://uuyc.163.com/help/20241216/40221_1200122.html
"""


def read_config(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("Bridge configuration is missing or is a symlink; run install.sh first.")
    return path.read_bytes()


def settings(data):
    return dict(line.split("=", 1) for line in data.decode("utf-8").splitlines()
                if re.match(r"^UURB_[A-Z0-9_]+=", line))


def atomic_write(path, data):
    fd, temporary = tempfile.mkstemp(prefix=".quality-environment-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def user_command(command, timeout=20):
    env = dict(os.environ)
    env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=" + env.get(
        "XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}") + "/bus"
    return subprocess.run(command, env=env, capture_output=True, text=True, timeout=timeout)


def systemctl(*arguments):
    result = user_command(["/usr/bin/systemctl", "--user", *arguments])
    if result.returncode:
        raise RuntimeError("Bridge restart failed; inspect uu-remote logs.")


def process_data(pid, cgroup=None):
    root = Path(f"/proc/{pid}")
    if cgroup and f"0::{cgroup}" not in (root / "cgroup").read_text().splitlines():
        raise ValueError("Process left the bridge cgroup")
    arguments = (root / "cmdline").read_bytes().decode(errors="replace").rstrip("\0").split("\0")
    environment = dict(item.split("=", 1) for item in (root / "environ").read_bytes().decode(errors="replace").split("\0") if "=" in item)
    return arguments, environment


def listener_ready(pid, port):
    listener = user_command(["/usr/bin/ss", "-H", "-ltnp", f"sport = :{port}"], timeout=3)
    if listener.returncode:
        return False
    if listener.stdout.strip():
        return any(f"pid={pid}," in line for line in listener.stdout.splitlines())
    # GRD may be in the bridge's network namespace; its table includes other
    # processes, so require the listening inode to be held by this PID.
    sockets = set()
    for fd in Path(f"/proc/{pid}/fd").iterdir():
        try:
            match = re.fullmatch(r"socket:\[([0-9]+)\]", os.readlink(fd))
        except OSError:
            continue
        if match:
            sockets.add(match.group(1))
    for name in ("tcp", "tcp6"):
        table = Path(f"/proc/{pid}/net/{name}")
        if table.is_file():
            for line in table.read_text().splitlines()[1:]:
                fields = line.split()
                if (len(fields) > 9 and fields[3] == "0A" and fields[9] in sockets and
                        fields[1].split(":")[-1].upper() == f"{port:04X}"):
                    return True
    return False


def bridge_ready(path):
    current = settings(read_config(path))
    prefix = current.get("UURB_WINEPREFIX", str(Path.home() / ".local/share/wineprefixes/uu-remote"))
    relay = current.get("UURB_DESKTOP_RELAY", "rdp")
    service = user_command(["/usr/bin/systemctl", "--user", "show", "uu-remote-bridge.service",
                            "--property=ActiveState", "--property=MainPID", "--property=ControlGroup"], timeout=3)
    properties = dict(line.split("=", 1) for line in service.stdout.splitlines() if "=" in line)
    cgroup = properties.get("ControlGroup", "")
    if (service.returncode or properties.get("ActiveState") != "active" or
            not cgroup.startswith("/") or ".." in cgroup.split("/") or
            not cgroup.endswith("/uu-remote-bridge.service")):
        return False
    pids = Path("/sys/fs/cgroup" + cgroup + "/cgroup.procs").read_text().splitlines()
    if properties.get("MainPID", "0") not in pids:
        return False
    processes = {}
    for pid in pids:
        if not pid.isdigit():
            continue
        try:
            processes[pid] = process_data(pid, cgroup)
        except (OSError, ValueError):
            continue
    servers = [(pid, env) for pid, (args, env) in processes.items()
               if any(arg.replace("\\", "/").split("/")[-1] == "GameViewerServer.exe" for arg in args)]
    if not servers or any(env.get("WINEPREFIX") != prefix for _, env in servers):
        return False
    server_display = servers[0][1].get("DISPLAY", "")
    if not server_display.startswith(":"):
        return False
    if relay == "rdp":
        freerdp = any(any(arg.replace("\\", "/").split("/")[-1] == "sdl-freerdp.exe" for arg in args)
                      and env.get("WINEPREFIX") == prefix and env.get("DISPLAY") == server_display
                      for args, env in processes.values())
        port = int(current.get("UURB_RDP_PORT", "3390"))
        grd = [pid for pid, (args, _) in processes.items()
               if args and Path(args[0]).name == "gnome-remote-desktop-daemon" and
               any(args[index:index + 2] == ["--rdp-port", str(port)] for index in range(len(args) - 1))]
        if not freerdp or not any(listener_ready(pid, port) for pid in grd):
            return False
    elif relay == "vnc":
        port_text = current.get("UURB_DESKTOP_VNC_PORT", "")
        if not port_text:
            log = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "uu-remote-bridge/desktop-x11vnc.log"
            ports = re.findall(r"^PORT=([0-9]+)$", log.read_text(), re.M)
            port_text = ports[-1] if ports else ""
        if not re.fullmatch(r"59[0-9]{2}", port_text):
            return False
        port = int(port_text)
        viewers = any(args and Path(args[0]).name == "vncviewer" and env.get("DISPLAY") == server_display
                      and any(arg in (f"127.0.0.1:{port}", f"127.0.0.1:{port - 5900}") for arg in args)
                      for args, env in processes.values())
        listener = user_command(["/usr/bin/ss", "-H", "-ltnp", f"sport = :{port}"], timeout=3)
        owners = re.findall(r"pid=(\d+),", "\n".join(line for line in listener.stdout.splitlines() if f"127.0.0.1:{port} " in line))
        matched = False
        for pid in owners:
            if pid in processes:
                args, _ = processes[pid]
            elif current.get("UURB_DESKTOP_VNC_PORT") and Path(f"/proc/{pid}").stat().st_uid == os.getuid():
                args, _ = process_data(pid)
            else:
                continue
            expected = ["-rfbport", str(port)] if current.get("UURB_DESKTOP_VNC_PORT") else ["-autoport", "5922"]
            matched = matched or (args and Path(args[0]).name == "x11vnc" and
                                 any(args[index:index + 2] == expected for index in range(len(args) - 1)))
        if listener.returncode or not viewers or not matched:
            return False
    else:
        return False
    status = user_command([str(Path.home() / ".local/bin/uu-agent"), "status"], timeout=6)
    response = json.loads(status.stdout)
    return status.returncode == 0 and isinstance(response, dict) and response.get("success") is True


def wait_ready(path, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if bridge_ready(path):
                return
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        time.sleep(0.5)
    raise RuntimeError("Bridge is not ready: its server, relay, listener or UU IPC did not respond.")


@contextlib.contextmanager
def configuration_lock(path):
    fd = os.open(path.parent / "quality.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def write_record(guard, record):
    atomic_write(guard / "record.json", (json.dumps(record, sort_keys=True) + "\n").encode())


def read_guard(guard):
    path = Path.home() / ".config/uu-remote-bridge/environment"
    root = path.parent / "quality-guards"
    if root.is_symlink() or guard.is_symlink() or guard.parent.resolve() != root.resolve() or not re.fullmatch(r"guard-[0-9a-f]{16}", guard.name):
        raise ValueError("Invalid quality recovery guard.")
    record = json.loads(read_config(guard / "record.json"))
    original = read_config(guard / "original.environment")
    expected = read_config(guard / "expected.environment")
    if (record.get("schema") != 1 or record.get("config") != str(path) or
            record.get("state") not in ("armed", "restoring", "committed", "rolled-back", "aborted") or
            record.get("unit") != "uu-quality-rollback-" + guard.name[6:] or
            record.get("original_sha256") != hashlib.sha256(original).hexdigest() or
            record.get("expected_sha256") != hashlib.sha256(expected).hexdigest()):
        raise ValueError("Quality recovery guard metadata is invalid.")
    return path, record, original, expected


def timer_control(command):
    result = user_command(command)
    if result.returncode:
        raise RuntimeError("The independent quality rollback timer could not be controlled; no unguarded change is allowed.")


def arm_guard(path, expected, original=None):
    with configuration_lock(path):
        active = path.parent / "quality-guard-active"
        if active.exists() or active.is_symlink():
            raise RuntimeError("A quality rollback guard is already active; finish or roll it back before another change.")
        observed = read_config(path)
        if original is not None and observed != original:
            raise RuntimeError("Bridge configuration changed; retry the quality selection.")
        original = observed
        try:
            wait_ready(path, timeout=20)
        except RuntimeError as error:
            raise RuntimeError("Previous configuration is not ready; refusing to start a change.") from error
        if read_config(path) != original:
            raise RuntimeError("Bridge configuration changed during the readiness check.")
        root = path.parent / "quality-guards"
        if root.is_symlink():
            raise ValueError("Quality recovery storage is a symlink; refusing an unguarded change.")
        root.mkdir(mode=0o700, exist_ok=True)
        root.chmod(0o700)
        guard = root / ("guard-" + secrets.token_hex(8))
        guard.mkdir(mode=0o700)
        atomic_write(guard / "original.environment", original)
        atomic_write(guard / "expected.environment", expected)
        atomic_write(guard / "recovery.py", Path(__file__).read_bytes())
        record = {"schema": 1, "config": str(path), "unit": "uu-quality-rollback-" + guard.name[6:],
                  "state": "armed", "original_sha256": hashlib.sha256(original).hexdigest(),
                  "expected_sha256": hashlib.sha256(expected).hexdigest()}
        write_record(guard, record)
        atomic_write(active, (str(guard) + "\n").encode())
        try:
            timer_control(["/usr/bin/systemd-run", "--user", "--quiet", "--no-ask-password", "--collect",
                           "--unit=" + record["unit"], "--on-active=90s", "--timer-property=AccuracySec=1s",
                           "--property=TimeoutStartSec=120s", "--property=Restart=on-failure",
                           "--property=RestartSec=5s", "--property=StartLimitIntervalSec=0",
                           "/usr/bin/python3", str(guard / "recovery.py"),
                           "guard", "rollback", str(guard)])
        except (RuntimeError, OSError, subprocess.SubprocessError):
            record["state"] = "aborted"
            write_record(guard, record)
            active.unlink()
            raise
        return guard


def finish_guard(path, guard, record, state):
    record["state"] = state
    write_record(guard, record)
    timer_control(["/usr/bin/systemctl", "--user", "stop", record["unit"] + ".timer"])
    active = path.parent / "quality-guard-active"
    if active.exists() and read_config(active).strip() == str(guard).encode():
        active.unlink()


def commit_guard(guard):
    path, record, _, expected = read_guard(guard)
    wait_ready(path)
    with configuration_lock(path):
        _, record, _, _ = read_guard(guard)
        if record["state"] not in ("armed", "committed") or read_config(path) != expected:
            raise RuntimeError("The quality trial changed or rollback already ran; success cannot be confirmed.")
        finish_guard(path, guard, record, "committed")


def rollback_guard(guard):
    path, record, original, expected = read_guard(guard)
    with configuration_lock(path):
        _, record, _, _ = read_guard(guard)
        if record["state"] == "rolled-back":
            if read_config(path) != original:
                raise RuntimeError("Configuration changed after recovery; configuration was preserved.")
            wait_ready(path)
            finish_guard(path, guard, record, "rolled-back")
            return True
        if record["state"] == "committed":
            finish_guard(path, guard, record, "committed")
            return False
        if record["state"] == "aborted":
            return False
        active = path.parent / "quality-guard-active"
        if read_config(active).strip() != str(guard).encode():
            raise RuntimeError("A different quality guard owns recovery; configuration was preserved.")
        observed = read_config(path)
        if observed not in (original, expected):
            raise RuntimeError("Bridge configuration changed externally and was preserved; rollback remains recorded.")
        atomic_write(path, original)
        record["state"] = "restoring"
        write_record(guard, record)
        systemctl("restart", "uu-remote-bridge.service")
        wait_ready(path)
        if read_config(path) != original:
            raise RuntimeError("Configuration changed during recovery; rollback remains recorded.")
        finish_guard(path, guard, record, "rolled-back")
    return True


def apply_profile(path, profile):
    resolution = PROFILES[profile][0]
    original = read_config(path)
    current = settings(original)
    if current.get("UURB_FOLLOW_DESKTOP_RESOLUTION", "off") != "off":
        raise ValueError("Desktop resolution following is enabled; disable it with install.sh before selecting a fixed canvas.")
    if current.get("UURB_RESOLUTION") == resolution:
        live = canvas_status(path)
        if live is None:
            message = f"启动尺寸（RDP请求）已保存为 {resolution}；当前UU画布无法验证，未更改实时分辨率。"
        elif live["live"] == resolution:
            message = f"保存的启动尺寸（RDP请求）和当前UU画布均为 {resolution}；无需重新连接。"
        else:
            guard = arm_guard(path, original, original)
            try:
                with configuration_lock(path):
                    _, record, _, _ = read_guard(guard)
                    if record["state"] != "armed" or read_config(path) != original:
                        raise RuntimeError("Configuration changed before the live mode trial.")
                    restored = canvas_command(path, "restore")
                if restored is None or restored["live"] != resolution:
                    raise RuntimeError("Live fullscreen canvas restore was not confirmed.")
                commit_guard(guard)
            except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as failure:
                try:
                    recovered = rollback_guard(guard)
                except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as recovery:
                    raise RuntimeError(f"Live mode change failed; bridge recovery is not confirmed: {recovery}") from failure
                if not recovered:
                    raise RuntimeError("The live canvas was confirmed, but guard cleanup failed; its configuration was preserved.") from failure
                raise RuntimeError("Live mode change failed; saved startup resolution restored and bridge ready.") from failure
            message = f"当前UU画布已恢复为 {resolution}；保存的启动尺寸（RDP请求）保持 {resolution}，无需重新连接。"
        print(message)
        return message
    lines = original.splitlines(keepends=True)
    replacement = f"UURB_RESOLUTION={resolution}\n".encode()
    updated = b"".join(replacement if line.startswith(b"UURB_RESOLUTION=") else line for line in lines)
    if not any(line.startswith(b"UURB_RESOLUTION=") for line in lines):
        updated += (b"" if not updated or updated.endswith(b"\n") else b"\n") + replacement
    guard = arm_guard(path, updated, original)
    try:
        with configuration_lock(path):
            _, record, _, _ = read_guard(guard)
            if record["state"] != "armed" or read_config(path) != original:
                raise RuntimeError("Bridge configuration changed or recovery already ran.")
            atomic_write(path, updated)
        systemctl("restart", "uu-remote-bridge.service")
        commit_guard(guard)
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as failure:
        try:
            restored = rollback_guard(guard)
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as recovery:
            raise RuntimeError(f"Quality change failed; recovery is not confirmed: {recovery}") from failure
        if not restored:
            raise RuntimeError("The bridge was ready, but guard cleanup failed; its confirmed configuration was preserved.") from failure
        raise RuntimeError("Quality change failed; previous configuration restored and ready.") from failure
    print(f"已应用 {profile}（{resolution}）；远控需要短暂重连。")
    print("运行 scripts/verify.sh --quick 验证；UU 原生画质和帧率请在控制端选择。")
    return f"启动尺寸（RDP请求）已保存为 {resolution}；远控需要短暂重连。"


def canvas_command(path, action):
    current = settings(read_config(path))
    resolution = current.get("UURB_RESOLUTION", "")
    if current.get("UURB_DESKTOP_RELAY", "rdp") != "rdp" or resolution not in {size for size, _ in PROFILES.values()}:
        return None
    prefix = Path(current.get("UURB_WINEPREFIX", str(Path.home() / ".local/share/wineprefixes/uu-remote")))
    helper = prefix / "compat/uu-display-modes.py"
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "uu-remote-bridge"
    display_file, authority = runtime / "private-display", runtime / "Xauthority"
    if any(item.is_symlink() or not item.is_file() for item in (helper, display_file, authority)):
        return None
    display = display_file.read_text().strip()
    if not re.fullmatch(r":[1-9][0-9]*(?:\.0)?", display):
        return None
    env = dict(os.environ, DISPLAY=display, XAUTHORITY=str(authority))
    result = subprocess.run(["/usr/bin/python3", str(helper), action, resolution, "--prefix", str(prefix)],
                            env=env, capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Live canvas validation failed.")
    record = json.loads(result.stdout)
    if (not isinstance(record, dict) or record.get("startup") != resolution or
            record.get("live") not in {size for size, _ in PROFILES.values()} or
            record.get("fullscreen") is not True):
        raise ValueError("Live canvas validation returned an invalid result.")
    return record


def canvas_status(path):
    try:
        return canvas_command(path, "inspect")
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError):
        return None


def bitrate(value):
    number = int(value)
    if not 0 <= number <= 500:
        raise argparse.ArgumentTypeError("bitrate must be 0 (automatic) or 1..500 Mbps")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command")
    for command in ("list", "status", "guide", "gui"):
        commands.add_parser(command)
    apply = commands.add_parser("apply", help="save startup/requested RDP size or restore its live canvas")
    apply.add_argument("profile", choices=PROFILES)
    guard = commands.add_parser("guard", help="arm, commit or roll back a guarded configuration trial")
    guards = guard.add_subparsers(dest="guard_command", required=True)
    guards.add_parser("arm").add_argument("expected", type=Path, nargs="?", help="complete target environment; defaults to the current configuration")
    for command in ("commit", "rollback"):
        guards.add_parser(command).add_argument("token", type=Path)
    limit = commands.add_parser("bitrate", help="set native UU maximum bitrate; 0 removes limit")
    limit.add_argument("mbps", type=bitrate)
    args = parser.parse_args(argv)
    config = Path.home() / ".config/uu-remote-bridge/environment"
    try:
        if args.command == "apply":
            apply_profile(config, args.profile)
        elif args.command == "guard":
            if args.guard_command == "arm":
                target = read_config(args.expected) if args.expected is not None else read_config(config)
                print(arm_guard(config, target))
            elif args.guard_command == "commit":
                commit_guard(args.token)
                print("Guard committed after the bridge became ready.")
            else:
                restored = rollback_guard(args.token)
                print("Guard recovery completed." if restored else "Guard was already resolved; configuration was preserved.")
        elif args.command == "bitrate":
            agent = Path.home() / ".local/bin/uu-agent"
            result = subprocess.run([str(agent), "cli", "--set-bitrate-limit", str(args.mbps)],
                                    capture_output=True, text=True)
            expected = ("Bitrate limit reset to default (no limit)" if args.mbps == 0
                        else f"Bitrate limit set to {args.mbps} Mbps")
            if result.returncode or result.stdout.strip() != expected:
                raise RuntimeError("UU did not confirm the requested bitrate limit; inspect the client and retry.")
            print("UU 原生 CLI 已接受取消码率上限。" if args.mbps == 0 else
                  f"UU 原生 CLI 已接受 {args.mbps} Mbps 码率上限；这是上限，不保证实际码率或帧率。")
        elif args.command == "guide":
            print(GUIDE, end="")
        elif args.command == "gui":
            zenity = shutil.which("zenity")
            if not zenity or not os.environ.get("DISPLAY"):
                raise ValueError("GUI requires a graphical session and zenity; use uu-remote quality list/apply instead.")
            current = settings(read_config(config)).get("UURB_RESOLUTION")
            live = canvas_status(config)
            live_text = live["live"] if live else "无法验证"
            rows = [item for key, (size, label) in PROFILES.items()
                    for item in ("TRUE" if size == current else "FALSE", key, size, label)]
            while True:
                result = subprocess.run([zenity, "--list", "--radiolist",
                                         "--title=UU远程 · 画质与分辨率",
                                         "--width=720", "--height=520",
                                         "--ok-label=应用", "--cancel-label=取消",
                                         f"--text=保存的启动尺寸（RDP请求）：{current}；当前UU画布：{live_text}。\n更换启动尺寸会短暂重连；重新应用已保存档位可恢复实时画布。\n不改变本机物理分辨率，实际共享桌面尺寸可能不同。\n画质和帧率在控制端设置。",
                                         "--extra-button=画质/帧率帮助", "--column=选择", "--column=档位",
                                         "--column=启动尺寸（RDP请求）", "--column=用途", "--print-column=2", *rows],
                                        capture_output=True, text=True)
                if result.returncode == 1:
                    return 0
                if result.returncode:
                    raise ValueError("Quality dialog could not be opened.")
                if result.stdout.strip() == "画质/帧率帮助":
                    subprocess.run([zenity, "--info", "--title=UU远程 · 画质与帧率帮助",
                                    "--width=620", "--no-markup", "--text=" + GUIDE],
                                   capture_output=True, text=True)
                    continue
                if result.stdout.strip() not in PROFILES:
                    raise ValueError("Quality dialog did not return a valid profile.")
                message = apply_profile(config, result.stdout.strip())
                subprocess.run([zenity, "--info", "--no-markup", "--title=UU远程 · 分辨率",
                                "--text=" + message], capture_output=True, text=True, timeout=30)
                break
        elif args.command == "list":
            for key, (size, label) in PROFILES.items():
                print(f"{key:6} {size:10} {label}")
            print("更换保存的启动尺寸（RDP请求）会短暂重连；重新应用已保存档位可恢复实时画布。原生画质/帧率：uu-remote quality guide")
        else:
            current = settings(read_config(config))
            print("保存的启动尺寸（RDP请求）：" + current.get("UURB_RESOLUTION", "unknown"))
            live = canvas_status(config)
            print("当前UU画布：" + (live["live"] if live else "无法验证；未更改实时分辨率"))
            print("跟随桌面分辨率：" + current.get("UURB_FOLLOW_DESKTOP_RESOLUTION", "off"))
            print("鼠标尺寸：" + current.get("UURB_CURSOR_SIZE", "auto"))
            print("实际帧率：未知；UU 控制端所选帧率不等于桥实际帧率。")
            print("UU 原生码率上限：未知；需要调整时使用 uu-remote quality bitrate 0..500。")
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        if args.command == "gui" and os.environ.get("DISPLAY"):
            zenity = shutil.which("zenity")
            if zenity:
                try:
                    subprocess.run([zenity, "--error", "--no-markup", "--title=UU远程 · 设置失败",
                                    "--text=" + str(error)], capture_output=True, text=True, timeout=30)
                except (OSError, subprocess.SubprocessError):
                    pass
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
