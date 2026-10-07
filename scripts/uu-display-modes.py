#!/usr/bin/env python3
"""Register genuine private X11 modes and verify the live fullscreen SDL canvas."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import re
import subprocess
import sys


MODES = {
    "1280x720": ("74.50", 1280, 1344, 1472, 1664, 720, 723, 728, 748),
    "1920x1080": ("173.00", 1920, 2048, 2248, 2576, 1080, 1083, 1088, 1120),
    "2560x1440": ("312.25", 2560, 2752, 3024, 3488, 1440, 1443, 1448, 1493),
    "3840x2160": ("712.75", 3840, 4152, 4576, 5312, 2160, 2163, 2168, 2237),
}


def relay_windows():
    """Read bounded managed clients without searching unrelated X11 descendants."""
    xlib = ctypes.CDLL("libX11.so.6")
    pointer = ctypes.c_void_p
    ulong = ctypes.c_ulong
    xlib.XOpenDisplay.argtypes = [ctypes.c_char_p]
    xlib.XOpenDisplay.restype = pointer
    xlib.XDefaultRootWindow.argtypes = [pointer]
    xlib.XDefaultRootWindow.restype = ulong
    xlib.XInternAtom.argtypes = [pointer, ctypes.c_char_p, ctypes.c_int]
    xlib.XInternAtom.restype = ulong
    xlib.XGetWindowProperty.argtypes = [pointer, ulong, ulong, ctypes.c_long, ctypes.c_long,
                                      ctypes.c_int, ulong, ctypes.POINTER(ulong), ctypes.POINTER(ctypes.c_int),
                                      ctypes.POINTER(ulong), ctypes.POINTER(ulong), ctypes.POINTER(pointer)]
    xlib.XGetWindowProperty.restype = ctypes.c_int
    xlib.XFree.argtypes = [pointer]
    xlib.XSync.argtypes = [pointer, ctypes.c_int]
    xlib.XCloseDisplay.argtypes = [pointer]
    xlib.XSetErrorHandler.argtypes = [pointer]
    xlib.XSetErrorHandler.restype = pointer

    class XErrorEvent(ctypes.Structure):
        _fields_ = [("type", ctypes.c_int), ("display", pointer), ("resourceid", ulong),
                    ("serial", ulong), ("error_code", ctypes.c_ubyte),
                    ("request_code", ctypes.c_ubyte), ("minor_code", ctypes.c_ubyte)]

    connection = xlib.XOpenDisplay(None)
    if not connection:
        raise RuntimeError("Private X display could not be opened.")
    errors = []
    callback_type = ctypes.CFUNCTYPE(ctypes.c_int, pointer, ctypes.POINTER(XErrorEvent))

    @callback_type
    def error_handler(_display, event):
        errors.append(event.contents.error_code)
        return 0

    old_handler = xlib.XSetErrorHandler(ctypes.cast(error_handler, pointer))
    try:
        def atom(name):
            return xlib.XInternAtom(connection, name.encode("ascii"), 0)

        def property_value(window, name, maximum):
            actual, count, remaining = ulong(), ulong(), ulong()
            format_bits, data = ctypes.c_int(), pointer()
            errors.clear()
            status = xlib.XGetWindowProperty(connection, window, atom(name), 0, maximum + 1,
                                             0, 0, ctypes.byref(actual), ctypes.byref(format_bits),
                                             ctypes.byref(count), ctypes.byref(remaining), ctypes.byref(data))
            xlib.XSync(connection, 0)
            try:
                if errors:
                    if all(code == 3 for code in errors):
                        raise FileNotFoundError("Managed client vanished during its property query.")
                    raise RuntimeError("Private X property query failed.")
                if status:
                    raise RuntimeError("Private X property query failed.")
                if remaining.value or count.value > maximum:
                    raise ValueError("Managed client property exceeds its bounded limit: " + name)
                if actual.value == 0:
                    return 0, 0, b""
                if format_bits.value == 8:
                    value = ctypes.string_at(data, count.value)
                elif format_bits.value == 32:
                    value = list(ctypes.cast(data, ctypes.POINTER(ulong))[:count.value])
                else:
                    value = None
                return actual.value, format_bits.value, value
            finally:
                if data:
                    xlib.XFree(data)

        kind, format_bits, clients = property_value(xlib.XDefaultRootWindow(connection), "_NET_CLIENT_LIST", 512)
        if (kind != atom("WINDOW") or format_bits != 32 or len(set(clients)) != len(clients) or
                any(window == 0 for window in clients)):
            raise ValueError("Expected bounded WINDOW/32 managed client list is unavailable or malformed.")
        matches = []
        for window in clients:
            try:
                kind, format_bits, classes = property_value(window, "WM_CLASS", 512)
                if kind == 0:
                    continue
                if (kind != atom("STRING") or format_bits != 8 or
                        len(classes.split(b"\0")) != 3 or not classes.endswith(b"\0")):
                    raise ValueError("Managed client class is malformed.")
                if b"sdl-freerdp.exe" not in [value.lower() for value in classes.split(b"\0")[:2]]:
                    continue
                kind, format_bits, pid = property_value(window, "_NET_WM_PID", 1)
                if kind != atom("CARDINAL") or format_bits != 32 or len(pid) != 1 or pid[0] <= 0:
                    raise ValueError("SDL relay PID property is missing or malformed.")
                kind, format_bits, name = property_value(window, "_NET_WM_NAME", 4096)
                if kind == 0:
                    kind, format_bits, name = property_value(window, "WM_NAME", 4096)
                    if kind == 0:
                        raise ValueError("SDL relay title is missing.")
                    expected_type, encoding = atom("STRING"), "latin1"
                else:
                    expected_type, encoding = atom("UTF8_STRING"), "utf-8"
                if kind != expected_type or format_bits != 8 or b"\0" in name:
                    raise ValueError("Managed client title is malformed.")
                if name.decode(encoding) == "Ubuntu-Desktop-Relay":
                    matches.append(str(window))
            except FileNotFoundError:
                continue
        return matches
    finally:
        xlib.XCloseDisplay(connection)
        xlib.XSetErrorHandler(old_handler)


def run(arguments, check=True, timeout=5):
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"{Path(arguments[0]).name} failed: {result.stderr.strip()}")
    return result


def query():
    text = run(["/usr/bin/xrandr", "--current"]).stdout
    geometry = re.search(r"\bcurrent (\d+) x (\d+)", text)
    output = re.search(r"^screen connected(?: primary)?(?: |$)", text, re.M)
    if not geometry or not output:
        raise ValueError("Expected private Xvfb screen output is unavailable.")
    return text, "x".join(geometry.groups())


def verify_timing(verbose, name, timing):
    header = re.search(r"^\s+" + re.escape(name) + r" \([^\n]+?\) ([0-9.]+)MHz([^\n]*)\n([^\n]*)\n([^\n]*)", verbose, re.M)
    if not header:
        raise ValueError("Existing display mode cannot be verified: " + name)
    clock, flags, horizontal, vertical = header.groups()
    horizontal_numbers = re.search(r"width\s+(\d+)\s+start\s+(\d+)\s+end\s+(\d+)\s+total\s+(\d+)", horizontal)
    vertical_numbers = re.search(r"height\s+(\d+)\s+start\s+(\d+)\s+end\s+(\d+)\s+total\s+(\d+)", vertical)
    if (float(clock) != float(timing[0]) or "-HSync" not in flags or "+VSync" not in flags or
            not horizontal_numbers or not vertical_numbers or
            tuple(map(int, horizontal_numbers.groups() + vertical_numbers.groups())) != timing[1:]):
        raise ValueError("Display mode name has conflicting timing: " + name)


def register(resolution):
    query()
    for size, timing in MODES.items():
        name = size + "_60.00"
        result = run(["/usr/bin/xrandr", "--newmode", name, *map(str, timing), "-hsync", "+vsync"], check=False)
        if result.returncode:
            verbose = run(["/usr/bin/xrandr", "--verbose"]).stdout
            verify_timing(verbose, name, timing)
        run(["/usr/bin/xrandr", "--addmode", "screen", name])
    run(["/usr/bin/xrandr", "--output", "screen", "--mode", resolution + "_60.00"])
    text, actual = query()
    if actual != resolution or not all(re.search(r"^\s+" + re.escape(size + "_60.00") + r"\s+", text, re.M) for size in MODES):
        raise RuntimeError("Genuine private display mode registration was not confirmed.")
    return {"startup": resolution, "live": actual, "registered": list(MODES)}


def inspect(resolution, prefix, expected_pid=None):
    text, live = query()
    names = {size + "_60.00" for size in MODES}
    registered = {match.group(1) for match in re.finditer(r"^\s+(\S+)\s+[0-9.]+", text, re.M)}
    if not names <= registered or live not in MODES:
        raise ValueError("Private canvas does not expose the four registered standard modes.")
    verbose = run(["/usr/bin/xrandr", "--verbose"]).stdout
    for size, timing in MODES.items():
        verify_timing(verbose, size + "_60.00", timing)
    windows = relay_windows()
    if len(windows) != 1:
        raise ValueError("Fullscreen SDL relay window identity is ambiguous or missing.")
    window = windows[0]
    pid = int(run(["/usr/bin/xdotool", "getwindowpid", window]).stdout.strip())
    if expected_pid is not None and pid != expected_pid:
        raise ValueError("SDL relay process identity changed.")
    process = Path(f"/proc/{pid}")
    start = process.joinpath("stat").read_text().rsplit(") ", 1)[1].split()[19]
    environment = dict(item.split(b"=", 1) for item in process.joinpath("environ").read_bytes().split(b"\0") if b"=" in item)
    arguments = process.joinpath("cmdline").read_bytes().split(b"\0")
    classes = run(["/usr/bin/xprop", "-id", window, "WM_CLASS"]).stdout.lower()
    if (environment.get(b"WINEPREFIX") != os.fsencode(prefix) or
            environment.get(b"DISPLAY") != os.fsencode(os.environ.get("DISPLAY", "")) or
            process.stat().st_uid != os.getuid() or
            process.joinpath("comm").read_text().strip().lower() != "sdl-freerdp.exe" or
            '"sdl-freerdp.exe"' not in classes or
            os.fsencode("/size:" + resolution) not in arguments):
        raise ValueError("SDL relay does not match the saved RDP source and Wine prefix.")
    geometry = run(["/usr/bin/xwininfo", "-id", window]).stdout
    values = [int(re.search(r"\b" + key + r":\s+(-?\d+)", geometry).group(1))
              for key in ("Absolute upper-left X", "Absolute upper-left Y", "Width", "Height")]
    width, height = map(int, live.split("x"))
    if values != [0, 0, width, height]:
        raise ValueError(f"SDL fullscreen rectangle {values} does not match live canvas {live}.")
    if process.joinpath("stat").read_text().rsplit(") ", 1)[1].split()[19] != start:
        raise ValueError("SDL relay process identity changed during validation.")
    return {"startup": resolution, "live": live, "registered": list(MODES), "relay_pid": pid,
            "relay_start": start, "relay_window": int(window), "fullscreen": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("register", "inspect", "restore"))
    parser.add_argument("resolution", choices=MODES)
    parser.add_argument("--prefix", type=Path)
    parser.add_argument("--relay-pid", type=int)
    args = parser.parse_args(argv)
    try:
        display = os.environ.get("DISPLAY", "")
        authority = Path(os.environ.get("XAUTHORITY", ""))
        if (not re.fullmatch(r":[1-9][0-9]*(?:\.0)?", display) or authority.is_symlink() or
                not authority.is_file() or authority.stat().st_uid != os.getuid()):
            raise ValueError("An owned private X display and regular authority file are required.")
        if args.action == "register":
            result = register(args.resolution)
        else:
            if args.prefix is None or not args.prefix.is_absolute():
                raise ValueError("An absolute saved Wine prefix is required.")
            before = inspect(args.resolution, args.prefix, args.relay_pid)
            if args.action == "restore" and before["live"] != args.resolution:
                helper = args.prefix / "compat/uu-display-mode.exe"
                if helper.is_symlink() or not helper.is_file():
                    raise ValueError("Installed genuine Windows display mode helper is unavailable.")
                environment = dict(os.environ, WINEPREFIX=str(args.prefix), WINEDEBUG="-all")
                environment.pop("LD_PRELOAD", None)
                changed = subprocess.run(["/opt/wine-stable/bin/wine", str(helper), args.resolution],
                                         env=environment, capture_output=True, text=True, timeout=12)
                if changed.returncode:
                    raise RuntimeError(changed.stderr.strip() or "Windows fullscreen mode restore failed.")
                result = inspect(args.resolution, args.prefix, before["relay_pid"])
                if result["live"] != args.resolution or result["relay_start"] != before["relay_start"]:
                    raise RuntimeError("Windows live mode restore was not confirmed.")
            else:
                result = before
        print(json.dumps(result))
        return 0
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        print("ERROR: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
