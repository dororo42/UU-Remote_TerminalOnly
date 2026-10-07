#!/usr/bin/env python3
"""Fit one verified UU controller to its private screen without changing focus."""
import ctypes as C
import json
import os
from pathlib import Path
import sys
import time


class ClientMessage(C.Structure):
    _fields_ = [("type", C.c_int), ("serial", C.c_ulong), ("send_event", C.c_int),
                ("display", C.c_void_p), ("window", C.c_ulong), ("message_type", C.c_ulong),
                ("format", C.c_int), ("data", C.c_long * 5)]


class Event(C.Union):
    _fields_ = [("message", ClientMessage), ("padding", C.c_long * 24)]


def fit(window, pid, start, preferred=None, observe=False):
    lib = C.CDLL("libX11.so.6")
    signatures = {
        "XOpenDisplay": ([C.c_char_p], C.c_void_p),
        "XDefaultRootWindow": ([C.c_void_p], C.c_ulong),
        "XInternAtom": ([C.c_void_p, C.c_char_p, C.c_int], C.c_ulong),
        "XGetWindowProperty": ([C.c_void_p, C.c_ulong, C.c_ulong, C.c_long, C.c_long, C.c_int,
                                C.c_ulong, C.POINTER(C.c_ulong), C.POINTER(C.c_int),
                                C.POINTER(C.c_ulong), C.POINTER(C.c_ulong), C.POINTER(C.c_void_p)], C.c_int),
        "XGetGeometry": ([C.c_void_p, C.c_ulong, C.POINTER(C.c_ulong), C.POINTER(C.c_int),
                          C.POINTER(C.c_int), C.POINTER(C.c_uint), C.POINTER(C.c_uint),
                          C.POINTER(C.c_uint), C.POINTER(C.c_uint)], C.c_int),
        "XTranslateCoordinates": ([C.c_void_p, C.c_ulong, C.c_ulong, C.c_int, C.c_int,
                                    C.POINTER(C.c_int), C.POINTER(C.c_int), C.POINTER(C.c_ulong)], C.c_int),
        "XSendEvent": ([C.c_void_p, C.c_ulong, C.c_int, C.c_long, C.POINTER(Event)], C.c_int),
        "XGrabServer": ([C.c_void_p], C.c_int), "XUngrabServer": ([C.c_void_p], C.c_int),
        "XSync": ([C.c_void_p, C.c_int], C.c_int), "XFree": ([C.c_void_p], C.c_int),
        "XCloseDisplay": ([C.c_void_p], C.c_int),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(lib, name)
        function.argtypes, function.restype = arguments, result
    display = lib.XOpenDisplay(os.environ.get("DISPLAY", "").encode())
    if not display:
        raise RuntimeError("Private controller display is unavailable")
    root = lib.XDefaultRootWindow(display)

    def atom(name):
        return lib.XInternAtom(display, name.encode(), 0)

    def property(name, expected):
        actual, count, remaining, pointer = C.c_ulong(), C.c_ulong(), C.c_ulong(), C.c_void_p()
        format = C.c_int()
        if lib.XGetWindowProperty(display, window, atom(name), 0, 128, 0, expected,
                C.byref(actual), C.byref(format), C.byref(count), C.byref(remaining), C.byref(pointer)):
            raise RuntimeError("Controller property could not be read")
        try:
            if actual.value == 0:
                return None
            if (expected != 0 and actual.value != expected) or remaining.value or not pointer.value:
                raise RuntimeError("Controller property does not match its expected type")
            if format.value == 8:
                return C.string_at(pointer, count.value)
            if format.value == 32:
                return list(C.cast(pointer, C.POINTER(C.c_ulong))[:count.value])
            raise RuntimeError("Unsupported controller property format")
        finally:
            if pointer.value:
                lib.XFree(pointer)

    def geometry(target):
        returned_root, child = C.c_ulong(), C.c_ulong()
        x, y, width, height, border, depth = C.c_int(), C.c_int(), C.c_uint(), C.c_uint(), C.c_uint(), C.c_uint()
        if not lib.XGetGeometry(display, target, C.byref(returned_root), C.byref(x), C.byref(y),
                C.byref(width), C.byref(height), C.byref(border), C.byref(depth)):
            raise RuntimeError("Controller geometry is unavailable")
        if not lib.XTranslateCoordinates(display, target, root, 0, 0, C.byref(x), C.byref(y), C.byref(child)):
            raise RuntimeError("Controller origin is unavailable")
        return x.value, y.value, width.value, height.value

    grabbed = False
    try:
        lib.XGrabServer(display)
        grabbed = True
        actual_start = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]
        if actual_start != start or property("_NET_WM_PID", 6) != [pid]:
            raise RuntimeError("Controller process identity changed")
        classes = property("WM_CLASS", 31) or b""
        if b"gameviewer.exe" not in [value.lower() for value in classes.split(b"\0")]:
            raise RuntimeError("Window is not a UU controller")
        if property("WM_TRANSIENT_FOR", 33) is not None or property("_NET_WM_WINDOW_TYPE", 4) != [atom("_NET_WM_WINDOW_TYPE_NORMAL")]:
            raise RuntimeError("Only a normal controller may be fitted")
        name = property("_NET_WM_NAME", atom("UTF8_STRING"))
        if name is None:
            # Wine may use COMPOUND_TEXT for a device name; the console has
            # already excluded the exact lobby titles before this helper.
            name = property("WM_NAME", 0) or b""
        if not name or name in ("网易UU远程".encode(), b"UU Remote", b"GameViewer"):
            raise RuntimeError("The UU management lobby cannot be fitted")
        _, _, root_width, root_height = geometry(root)
        x, y, width, height = geometry(window)
        if preferred is None:
            preferred = (x, y, width, height)
        if (len(preferred) != 4 or not all(-8192 <= value <= 8192 for value in preferred[:2])
                or not 640 <= preferred[2] <= 8192 or not 360 <= preferred[3] <= 8192):
            raise RuntimeError("Controller preferred geometry is outside the supported range")
        result = {"root": [root_width, root_height], "geometry": [x, y, width, height],
                  "preferred": list(preferred), "changed": False}
        if observe:
            return result
        extents = property("_NET_FRAME_EXTENTS", 6) or [0, 0, 0, 0]
        if len(extents) != 4 or any(value > 128 for value in extents):
            raise RuntimeError("Controller decorations are outside the supported range")
        left, right, top, bottom = extents
        new_width = min(preferred[2], root_width - left - right)
        new_height = min(preferred[3], root_height - top - bottom)
        if new_width < 640 or new_height < 360:
            raise RuntimeError("Private screen is too small for the controller and its decorations")
        new_x = max(left, min(preferred[0], root_width - right - new_width))
        new_y = max(top, min(preferred[1], root_height - bottom - new_height))
        desired = (new_x, new_y, new_width, new_height)
        if desired == (x, y, width, height):
            return result
        event = Event()
        event.message.type, event.message.display, event.message.window = 33, display, window
        event.message.message_type, event.message.format = atom("_NET_MOVERESIZE_WINDOW"), 32
        event.message.data[:] = [10 | (15 << 8) | (2 << 12), *desired]
        if not lib.XSendEvent(display, root, 0, (1 << 19) | (1 << 20), C.byref(event)):
            raise RuntimeError("Window manager rejected the controller fit request")
        lib.XUngrabServer(display)
        grabbed = False
        lib.XSync(display, 0)
        for _ in range(30):
            if geometry(window) == desired:
                result.update(geometry=list(desired), changed=True)
                return result
            time.sleep(0.02)
        raise RuntimeError("Controller fit did not complete; its session was preserved")
    finally:
        if grabbed:
            lib.XUngrabServer(display)
        lib.XSync(display, 0)
        lib.XCloseDisplay(display)


if __name__ == "__main__":
    try:
        arguments = sys.argv[1:]
        observe = bool(arguments and arguments[-1] == "--observe")
        if observe:
            arguments.pop()
        if len(arguments) not in (3, 7) or not all(value.isdecimal() for value in arguments[:3]):
            raise ValueError("Usage: uu-controller-fit.py WINDOW PID PROCESS_START [X Y WIDTH HEIGHT] [--observe]")
        preferred = tuple(map(int, arguments[3:])) if len(arguments) == 7 else None
        print(json.dumps(fit(int(arguments[0]), int(arguments[1]), arguments[2], preferred, observe)))
    except (OSError, ValueError, RuntimeError) as error:
        print(f"UU controller fit: {error}", file=sys.stderr)
        raise SystemExit(1)
