"""Genuine XRandR modes on private Xvfb; no live bridge/Wine processes."""
import contextlib
import ctypes
import importlib.util
import inspect as python_inspect
import io
import json
import re
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from Xlib import display, Xatom

from test_manager_capture import isolated_display

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("display_modes", ROOT / "scripts/uu-display-modes.py")
modes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(modes)


class DisplayModeTests(unittest.TestCase):
    def test_inspect_rejects_wrong_pid_prefix_source_and_stale_fullscreen_rectangle(self):
        fixture = '''import ctypes, os, pathlib, sys, time
from Xlib import display, Xatom
ctypes.CDLL(None).prctl(15, b"sdl-freerdp.exe", 0, 0, 0)
connection = display.Display()
window = connection.screen().root.create_window(0, 0, 3840, 2160, 0,
    connection.screen().root_depth, 1, 0, background_pixel=0xffffff)
window.set_wm_name("Ubuntu-Desktop-Relay")
window.set_wm_class("sdl-freerdp.exe", "sdl-freerdp.exe")
window.change_property(connection.intern_atom("_NET_WM_PID"), Xatom.CARDINAL, 32, [os.getpid()])
window.map(); connection.sync()
connection.screen().root.change_property(connection.intern_atom("_NET_CLIENT_LIST"), Xatom.WINDOW, 32, [window.id]); connection.sync()
ready = pathlib.Path(sys.argv[1]); ready.write_text(str(window.id))
while not ready.with_suffix(".stop").exists():
    if ready.with_suffix(".resize").exists():
        window.configure(width=1280, height=720); connection.sync()
        ready.with_suffix(".changed").write_text("done")
    time.sleep(.02)
connection.close()
'''
        with tempfile.TemporaryDirectory() as case:
            directory = Path(case)
            ready = directory / "ready"
            prefix = directory / "prefix"
            with isolated_display(directory / "display", "5120x2880x24") as env, patch.dict(os.environ, env):
                modes.register("3840x2160")
                process = subprocess.Popen(["/usr/bin/python3", "-c", fixture, str(ready), "/size:3840x2160"],
                                           env=dict(env, WINEPREFIX=str(prefix)), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                try:
                    deadline = time.monotonic() + 5
                    while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                        time.sleep(.02)
                    self.assertTrue(ready.exists(), "Owned SDL identity fixture failed to map")
                    record = modes.inspect("3840x2160", prefix, process.pid)
                    self.assertTrue(record["fullscreen"])
                    self.assertEqual(record["relay_pid"], process.pid)
                    with self.assertRaisesRegex(ValueError, "identity changed"):
                        modes.inspect("3840x2160", prefix, process.pid + 1)
                    for source, other_prefix in (("1920x1080", prefix), ("3840x2160", directory / "wrong")):
                        with self.subTest(source=source, prefix=other_prefix), self.assertRaisesRegex(ValueError, "saved RDP source"):
                            modes.inspect(source, other_prefix, process.pid)
                    ready.with_suffix(".resize").touch()
                    while not ready.with_suffix(".changed").exists() and time.monotonic() < deadline:
                        time.sleep(.02)
                    self.assertTrue(ready.with_suffix(".changed").exists())
                    with self.assertRaisesRegex(ValueError, "fullscreen rectangle"):
                        modes.inspect("3840x2160", prefix, process.pid)
                finally:
                    ready.with_suffix(".stop").touch()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill(); process.wait(timeout=3)
                    process.stderr.close()

    def test_managed_list_and_title_types_are_bounded_and_duplicates_or_lost_relay_are_rejected(self):
        with tempfile.TemporaryDirectory() as case:
            with isolated_display(Path(case) / "display", "5120x2880x24") as env, patch.dict(os.environ, env):
                modes.register("3840x2160")
                connection = display.Display()
                try:
                    root = connection.screen().root
                    client_list = connection.intern_atom("_NET_CLIENT_LIST")
                    net_name = connection.intern_atom("_NET_WM_NAME")
                    utf8 = connection.intern_atom("UTF8_STRING")
                    window = root.create_window(0, 0, 3840, 2160, 0, connection.screen().root_depth, 1, 0)
                    window.set_wm_name("Ubuntu-Desktop-Relay")
                    window.set_wm_class("sdl-freerdp.exe", "sdl-freerdp.exe")
                    window.change_property(connection.intern_atom("_NET_WM_PID"), Xatom.CARDINAL, 32, [os.getpid()])
                    window.change_property(net_name, utf8, 8, b"Ubuntu-Desktop-Relay")
                    window.map()
                    root.change_property(client_list, Xatom.WINDOW, 32, [window.id])
                    connection.sync()
                    self.assertEqual(modes.relay_windows(), [str(window.id)])
                    window.change_property(connection.intern_atom("WM_NAME"), connection.intern_atom("COMPOUND_TEXT"),
                                           8, b"Ubuntu-Desktop-Relay")
                    connection.sync()
                    self.assertEqual(modes.relay_windows(), [str(window.id)])
                    for kind, bits, values in ((Xatom.CARDINAL, 32, [window.id]),
                                               (Xatom.WINDOW, 16, [1]),
                                               (Xatom.WINDOW, 32, [window.id, window.id]),
                                               (Xatom.WINDOW, 32, list(range(1, 514)))):
                        with self.subTest(kind=kind, bits=bits, count=len(values)):
                            root.change_property(client_list, kind, bits, values)
                            connection.sync()
                            with self.assertRaises(ValueError):
                                modes.relay_windows()
                    root.change_property(client_list, Xatom.WINDOW, 32, [window.id])
                    for kind, bits, value in ((Xatom.CARDINAL, 32, [1]),
                                              (utf8, 16, [1]),
                                              (utf8, 8, b"Ubuntu-Desktop-Relay\0spoof"),
                                              (utf8, 8, b"\xc0\xaf"),
                                              (utf8, 8, b"x" * 4097)):
                        with self.subTest(kind=kind, bits=bits, value_length=len(value)):
                            window.change_property(net_name, kind, bits, value)
                            connection.sync()
                            with self.assertRaises(ValueError):
                                modes.relay_windows()
                    window.delete_property(net_name)
                    window.set_wm_name("Ubuntu-Desktop-Relay")
                    connection.sync()
                    self.assertEqual(modes.relay_windows(), [str(window.id)])
                    window.change_property(connection.intern_atom("WM_NAME"), connection.intern_atom("COMPOUND_TEXT"),
                                           8, b"Ubuntu-Desktop-Relay")
                    connection.sync()
                    with self.assertRaisesRegex(ValueError, "title is malformed"):
                        modes.relay_windows()
                    window.set_wm_name("Ubuntu-Desktop-Relay")
                    duplicate = root.create_window(0, 0, 40, 40, 0, connection.screen().root_depth, 1, 0)
                    duplicate.set_wm_name("Ubuntu-Desktop-Relay")
                    duplicate.set_wm_class("sdl-freerdp.exe", "sdl-freerdp.exe")
                    duplicate.change_property(connection.intern_atom("_NET_WM_PID"), Xatom.CARDINAL, 32, [os.getpid()])
                    duplicate.map()
                    root.change_property(client_list, Xatom.WINDOW, 32, [window.id, duplicate.id])
                    connection.sync()
                    with self.assertRaisesRegex(ValueError, "ambiguous or missing"):
                        modes.inspect("3840x2160", Path(case) / "prefix")
                    duplicate.destroy()
                    window.destroy()
                    connection.sync()
                    with self.assertRaisesRegex(ValueError, "ambiguous or missing"):
                        modes.inspect("3840x2160", Path(case) / "prefix")
                finally:
                    connection.close()

    def test_partial_foreign_publication_cannot_mask_or_weaken_actual_sdl_identity(self):
        # Preserve the real pre-fix property reader as a negative control; only
        # the new candidate classification is removed, with identical Xlib calls.
        source = python_inspect.getsource(modes.relay_windows)
        begin = source.index('                kind, format_bits, classes = property_value(window, "WM_CLASS", 512)')
        end = source.index('                kind, format_bits, name = property_value(window, "_NET_WM_NAME", 4096)', begin)
        namespace = {"ctypes": ctypes}
        exec(source[:begin] + source[end:], namespace)
        old_reader = namespace["relay_windows"]
        with tempfile.TemporaryDirectory() as case:
            with isolated_display(Path(case) / "display", "5120x2880x24") as env, patch.dict(os.environ, env):
                modes.register("3840x2160")
                connection = display.Display()
                try:
                    root = connection.screen().root
                    clients, name, classes, pid = [connection.intern_atom(key) for key in
                                                  ("_NET_CLIENT_LIST", "_NET_WM_NAME", "WM_CLASS", "_NET_WM_PID")]
                    utf8 = connection.intern_atom("UTF8_STRING")
                    main = root.create_window(0, 0, 3840, 2160, 0, connection.screen().root_depth, 1, 0)
                    main.set_wm_class("sdl-freerdp.exe", "sdl-freerdp.exe")
                    main.set_wm_name("Ubuntu-Desktop-Relay")
                    main.change_property(pid, Xatom.CARDINAL, 32, [os.getpid()])
                    main.map()
                    foreign = root.create_window(0, 0, 40, 40, 0, connection.screen().root_depth, 1, 0)
                    foreign.change_property(connection.intern_atom("WM_NAME"), utf8, 8, "中文".encode())
                    foreign.map()
                    root.change_property(clients, Xatom.WINDOW, 32, [main.id, foreign.id])
                    connection.sync()
                    with self.assertRaisesRegex(ValueError, "title is malformed"):
                        old_reader()
                    self.assertEqual(modes.relay_windows(), [str(main.id)])
                    foreign.set_wm_class("gameviewer.exe", "gameviewer.exe")
                    foreign.change_property(name, Xatom.CARDINAL, 32, [1])
                    connection.sync()
                    self.assertEqual(modes.relay_windows(), [str(main.id)])
                    for kind, bits, value in ((Xatom.CARDINAL, 32, [1]),
                                              (Xatom.STRING, 8, b"sdl-freerdp.exe\0")):
                        main.change_property(classes, kind, bits, value)
                        connection.sync()
                        with self.assertRaisesRegex(ValueError, "class is malformed"):
                            modes.relay_windows()
                    main.delete_property(classes)
                    connection.sync()
                    with self.assertRaisesRegex(ValueError, "ambiguous or missing"):
                        modes.inspect("3840x2160", Path(case) / "prefix")
                    main.set_wm_class("sdl-freerdp.exe", "sdl-freerdp.exe")
                    for kind, bits, value in ((Xatom.STRING, 8, b"123"), (Xatom.CARDINAL, 32, [0]),
                                              (Xatom.CARDINAL, 32, [os.getpid(), os.getpid()])):
                        main.change_property(pid, kind, bits, value)
                        connection.sync()
                        with self.assertRaises(ValueError):
                            modes.relay_windows()
                    main.delete_property(pid)
                    connection.sync()
                    with self.assertRaisesRegex(ValueError, "PID property is missing"):
                        modes.relay_windows()
                    main.change_property(pid, Xatom.CARDINAL, 32, [os.getpid()])
                    main.delete_property(connection.intern_atom("WM_NAME"))
                    connection.sync()
                    with self.assertRaisesRegex(ValueError, "relay title is missing"):
                        modes.relay_windows()
                    main.set_wm_name("Ubuntu-Desktop-Relay")
                    main.change_property(name, utf8, 8, b"Ubuntu-Desktop-Relay\0invalid")
                    connection.sync()
                    with self.assertRaisesRegex(ValueError, "title is malformed"):
                        modes.relay_windows()
                finally:
                    connection.close()

    def test_unrelated_destroyed_clients_break_old_tree_search_but_not_verified_managed_relay(self):
        fixture = '''import ctypes, os, pathlib, sys, time
from Xlib import display, Xatom
ctypes.CDLL(None).prctl(15, b"sdl-freerdp.exe", 0, 0, 0)
c = display.Display(); r = c.screen().root
w = r.create_window(0, 0, 3840, 2160, 0, c.screen().root_depth, 1, 0)
w.set_wm_name("Ubuntu-Desktop-Relay"); w.set_wm_class("sdl-freerdp.exe", "sdl-freerdp.exe")
w.change_property(c.intern_atom("_NET_WM_PID"), Xatom.CARDINAL, 32, [os.getpid()]); w.map()
p = pathlib.Path(sys.argv[1]); p.write_text(str(w.id))
while not p.with_suffix(".stop").exists():
    windows = []
    for i in range(64):
        a = r.create_window(i, i, 40, 40, 0, c.screen().root_depth, 1, 0)
        a.change_property(c.intern_atom("WM_NAME"), c.intern_atom("UTF8_STRING"), 8, ("中文-" + str(i)).encode())
        a.change_property(c.intern_atom("_NET_WM_NAME"), c.intern_atom("UTF8_STRING"), 8, ("中文-" + str(i)).encode())
        a.map(); windows.append(a)
    r.change_property(c.intern_atom("_NET_CLIENT_LIST"), Xatom.WINDOW, 32, [w.id] + [a.id for a in windows])
    c.sync(); time.sleep(.002)
    for a in windows: a.destroy()
    c.sync(); time.sleep(.002)
c.close()
'''
        with tempfile.TemporaryDirectory() as case:
            directory = Path(case)
            ready, prefix = directory / "ready", directory / "prefix"
            with isolated_display(directory / "display", "5120x2880x24") as env, patch.dict(os.environ, env):
                modes.register("3840x2160")
                process = subprocess.Popen(["/usr/bin/python3", "-c", fixture, str(ready), "/size:3840x2160"],
                                           env=dict(env, WINEPREFIX=str(prefix)), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                try:
                    deadline = time.monotonic() + 5
                    while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                        time.sleep(.02)
                    self.assertTrue(ready.exists(), "Owned churn fixture failed to map")
                    old_error = None
                    for _ in range(100):
                        try:
                            with patch.object(modes, "relay_windows", side_effect=lambda: modes.run(
                                ["/usr/bin/xdotool", "search", "--name", "^Ubuntu-Desktop-Relay$"]).stdout.splitlines()):
                                modes.inspect("3840x2160", prefix, process.pid)
                        except RuntimeError as error:
                            old_error = str(error)
                            break
                    self.assertIsNotNone(old_error, "Real old whole-tree query did not reproduce its destruction race")
                    self.assertIn("BadWindow", old_error)
                    for _ in range(10):
                        record = modes.inspect("3840x2160", prefix, process.pid)
                        self.assertEqual(record["relay_window"], int(ready.read_text()))
                        self.assertTrue(record["fullscreen"])
                finally:
                    ready.with_suffix(".stop").touch()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill(); process.wait(timeout=3)
                    process.stderr.close()

    def test_four_real_modes_register_idempotently_and_change_actual_root(self):
        with tempfile.TemporaryDirectory() as case:
            with isolated_display(Path(case) / "display", "3840x2160x24") as env, patch.dict(os.environ, env):
                for size in ("3840x2160", "1280x720", "1920x1080", "2560x1440", "3840x2160"):
                    record = modes.register(size)
                    self.assertEqual(record["live"], size)
                    self.assertEqual(modes.query()[1], size)
                    self.assertEqual(record["registered"], list(modes.MODES))
                output = modes.run(["/usr/bin/xrandr", "--current"]).stdout
                self.assertNotIn("5120x2880", output)
                self.assertIn("maximum 3840 x 2160", output)
                for size in modes.MODES:
                    self.assertRegex(output, size + r"_60\.00\s+59\.[0-9]+")

    def test_5k_is_not_a_public_display_mode_choice(self):
        with contextlib.redirect_stderr(io.StringIO()), patch.object(modes, "run") as run:
            with self.assertRaises(SystemExit) as error:
                modes.main(["register", "5120x2880"])
        self.assertEqual(error.exception.code, 2)
        run.assert_not_called()

    def test_existing_conflicting_timing_is_rejected_without_canvas_change(self):
        with tempfile.TemporaryDirectory() as case:
            with isolated_display(Path(case) / "display", "3840x2160x24") as env, patch.dict(os.environ, env):
                modes.run(["/usr/bin/xrandr", "--newmode", "1280x720_60.00", "80.00",
                           "1280", "1344", "1472", "1664", "720", "723", "728", "748", "-hsync", "+vsync"])
                modes.run(["/usr/bin/xrandr", "--addmode", "screen", "1280x720_60.00"])
                with self.assertRaisesRegex(ValueError, "conflicting timing"):
                    modes.register("1280x720")
                self.assertEqual(modes.query()[1], "3840x2160")

    def test_physical_display_or_symlink_authority_is_rejected_before_xrandr(self):
        with tempfile.TemporaryDirectory() as case:
            authority = Path(case) / "auth"
            authority.write_bytes(b"owned")
            link = Path(case) / "link"
            link.symlink_to(authority)
            for display, auth in ((":0", authority), (":1", link)):
                with self.subTest(display=display, authority=auth), patch.dict(os.environ, DISPLAY=display, XAUTHORITY=str(auth)), patch.object(modes, "run") as run, contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(modes.main(["register", "3840x2160"]), 1)
                    run.assert_not_called()

    def test_only_standard_rdp_profiles_expand_max_canvas_and_custom_vnc_stay_exact(self):
        source = (ROOT / "scripts/uu-remote-bridge").read_text()
        block = source.split('private_max_resolution="$resolution"\n', 1)[1].split('/usr/bin/Xvfb "$DISPLAY"', 1)[0]
        block = 'private_max_resolution="$resolution"\n' + block
        for relay, size, expected, enabled in (("rdp", "1280x720", "3840x2160", "true"),
                                             ("rdp", "3840x2160", "3840x2160", "true"),
                                             ("rdp", "5120x2880", "5120x2880", "false"),
                                             ("rdp", "1366x768", "1366x768", "false"),
                                             ("vnc", "1280x720", "1280x720", "false"),
                                             ("vnc", "5120x2880", "5120x2880", "false")):
            with self.subTest(relay=relay, size=size):
                result = subprocess.run(["bash", "-c", block + '\nprintf "%s %s" "$private_max_resolution" "$native_display_modes"'],
                                        env=dict(os.environ, desktop_relay=relay, resolution=size), capture_output=True, text=True, check=True)
                self.assertEqual(result.stdout, expected + " " + enabled)
        self.assertLess(source.index('openbox_pid=$!'), source.index('register "$resolution"'))
        self.assertLess(source.index('register "$resolution"'), source.index('start_x11_input_helper\n', source.index('openbox_pid=$!')))

    def test_verifier_rejects_old_installed_catalog_and_malformed_records(self):
        source = (ROOT / "scripts/verify.sh").read_text()
        code = re.search(r"/usr/bin/python3 -c '(import json, sys; sys.exit\(json.load\(sys.stdin\).get\(\"registered\"\).*?)'", source).group(1)
        for record, expected in (({"registered": list(modes.MODES)}, 0),
                                 ({"registered": list(modes.MODES)[:-1]}, 1),
                                 ({"registered": list(modes.MODES) + ["640x480"]}, 1),
                                 ({"registered": list(modes.MODES) + ["5120x2880"]}, 1),
                                 ({}, 1), ([], 1)):
            with self.subTest(record=record):
                result = subprocess.run(["/usr/bin/python3", "-c", code], input=json.dumps(record),
                                        text=True, capture_output=True, timeout=3)
                self.assertEqual(result.returncode, expected)
        result = subprocess.run(["/usr/bin/python3", "-c", code], input="not JSON",
                                text=True, capture_output=True, timeout=3)
        self.assertNotEqual(result.returncode, 0)

    def test_helpers_install_digest_and_existing_canvas_verifier_is_upgraded(self):
        installer = (ROOT / "install.sh").read_text()
        digest = (ROOT / "scripts/runtime-source-digest").read_text()
        self.assertIn('"$wine_prefix/compat/uu-display-modes.py"', installer)
        self.assertIn('"$compat_build/uu-display-mode.exe"', installer)
        self.assertIn("scripts/uu-display-modes.py", digest)
        self.assertIn("src/uu_display_mode.c", digest)
        self.assertIn("x11-xserver-utils", installer)
        verifier = (ROOT / "scripts/verify.sh").read_text()
        self.assertIn('inspect "$resolution"', verifier)
        self.assertIn('--relay-pid "$relay_pid"', verifier)
        self.assertIn('elif [[ "$private_geometry" == "$resolution" ]]', verifier)
        self.assertIn("private UU canvas matches the saved relay size", verifier)


if __name__ == "__main__":
    unittest.main()
