"""Real Xvfb/Composite/SHM probes; every window and process belongs to this test."""
from contextlib import contextmanager
import bisect
import ctypes
import json
import os
import re
from pathlib import Path
import select
import shutil
import shlex
import signal
import socket
import struct
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
FLAGS = ["-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-Wpedantic"]


TRAP_BOUNDARIES = r"""
static int manager_test_missing_attributes(MCDisplay *display, MCWindow window,
                                           MCWindowAttributes *attributes)
{
    MCErrorEvent event;
    (void)attributes;
    memset(&event, 0, sizeof(event));
    event.display = display;
    event.serial = scan_candidate_serial;
    event.resourceid = window;
    event.error_code = 3;
    event.request_code = 3;
    trap_private_error(display, &event);
    return 0;
}

void manager_test_trap_boundaries(void)
{
    unsigned long saved_first = error_first, saved_last = error_last;
    unsigned long saved_serial = scan_candidate_serial;
    MCWindow saved_candidate = scan_candidate;
    int saved_error = private_error;
    MCErrorEvent event;
    memset(&event, 0, sizeof(event));
    event.display = private_display;
    event.serial = 123;
    event.resourceid = 456;
    event.error_code = 3;
    event.request_code = 3;
    error_first = 100;
    error_last = 200;
    scan_candidate = 456;
    scan_candidate_serial = 123;
    private_error = 0;
    trap_private_error(private_display, &event);
    assert(private_error == 0);
    private_error = 10;
    trap_private_error(private_display, &event);
    assert(private_error == 10); /* Never erase a preceding real error. */
    for (unsigned int variant = 0; variant < 6; ++variant) {
        MCErrorEvent different = event;
        if (variant == 0) different.serial++;
        if (variant == 1) different.resourceid++;
        if (variant == 2) different.request_code = 14; /* XGetGeometry is not exempt. */
        if (variant == 3) different.error_code = 8;
        if (variant == 4) different.minor_code = 1;
        if (variant == 5) scan_candidate = 0;
        private_error = 0;
        trap_private_error(private_display, &different);
        assert(private_error == different.error_code);
    }
    int (*saved_attributes)(MCDisplay *, MCWindow, MCWindowAttributes *) = api.attributes;
    unsigned int saved_count = window_count, saved_protected_count = scan_protected_count;
    unsigned int saved_nodes = tree_nodes;
    struct gui_window saved_window = windows[0];
    MCWindow saved_protected[2] = {scan_protected[0], scan_protected[1]};
    api.attributes = manager_test_missing_attributes;
    error_first = 0;
    error_last = ULONG_MAX;
    window_count = scan_protected_count = 0;
    for (unsigned int variant = 0; variant < 2; ++variant) {
        private_error = 0;
        tree_nodes = 0;
        assert(!scan_frame(variant == 0 ? main_window : root, root, 0));
        assert(private_error == 3 && scan_candidate == 0);
    }
    scan_protected_count = 2;
    scan_protected[0] = 789;
    scan_protected[1] = 790;
    for (unsigned int variant = 0; variant < 2; ++variant) {
        private_error = 0;
        tree_nodes = 0;
        assert(!scan_frame(scan_protected[variant], root, 0));
        assert(private_error == 3 && scan_candidate == 0);
    }
    scan_protected_count = 0;
    window_count = 1;
    windows[0].client = 987;
    windows[0].frame = 988;
    for (unsigned int variant = 0; variant < 2; ++variant) {
        private_error = 0;
        tree_nodes = 0;
        assert(!scan_frame(variant == 0 ? 987 : 988, root, 0));
        assert(private_error == 3 && scan_candidate == 0);
    }
    window_count = 0;
    private_error = 10;
    tree_nodes = 0;
    assert(!scan_frame(999, root, 0));
    assert(private_error == 10 && scan_candidate == 0);
    api.attributes = saved_attributes;
    window_count = saved_count;
    scan_protected_count = saved_protected_count;
    tree_nodes = saved_nodes;
    windows[0] = saved_window;
    scan_protected[0] = saved_protected[0];
    scan_protected[1] = saved_protected[1];
    error_first = saved_first;
    error_last = saved_last;
    scan_candidate_serial = saved_serial;
    scan_candidate = saved_candidate;
    private_error = saved_error;
}
"""


@contextmanager
def isolated_display(directory, screen="800x600x24", compositor=True):
    directory.mkdir()
    cookie = os.urandom(16)
    name = b"MIT-MAGIC-COOKIE-1"
    authority = directory / "Xauthority"
    authority.write_bytes(struct.pack("!H", 65535) + b"\0\0\0\0" +
                          struct.pack("!H", len(name)) + name + struct.pack("!H", len(cookie)) + cookie)
    authority.chmod(0o600)
    read_fd, write_fd = os.pipe()
    processes = []
    with (directory / "xvfb.log").open("wb") as log:
        try:
            server = subprocess.Popen(["/usr/bin/Xvfb", "-displayfd", str(write_fd),
                                       "-screen", "0", screen, "-nolisten", "tcp",
                                       "-extension", "GLX",
                                       "-auth", str(authority)], pass_fds=(write_fd,),
                                      stdout=log, stderr=log)
            processes.append(server)
            os.close(write_fd)
            write_fd = -1
            if not select.select([read_fd], [], [], 5)[0]:
                raise RuntimeError("Isolated Xvfb did not start")
            number = os.read(read_fd, 64).decode().strip()
            if not number.isdigit():
                raise RuntimeError("Invalid isolated display number: " + repr(number) +
                                   "\n" + (directory / "xvfb.log").read_text()[-8192:])
            # These 2D Composite/SHM probes do not use GLX. Disabling it avoids
            # probing host NVIDIA EGL drivers inside a GPU-free namespace.
            # Display 0 is valid in a private socket namespace; the number
            # comes from this owned server's displayfd, never the host DISPLAY.
            if server.poll() is not None:
                raise RuntimeError("Owned isolated Xvfb exited before authentication")
            # python-xlib does not match FamilyWild entries; give its real
            # Unix connection a FamilyLocal entry using the same private cookie.
            hostname, display_number = socket.gethostname().encode(), number.encode()
            with authority.open("ab") as auth:
                auth.write(struct.pack("!H", 256) + struct.pack("!H", len(hostname)) + hostname +
                           struct.pack("!H", len(display_number)) + display_number +
                           struct.pack("!H", len(name)) + name + struct.pack("!H", len(cookie)) + cookie)
            env = dict(os.environ, DISPLAY=f":{number}", XAUTHORITY=str(authority))
            env.pop("LD_PRELOAD", None)
            for key in tuple(env):
                if key.startswith("UURB_MANAGER_"):
                    del env[key]
            if compositor:
                process = subprocess.Popen(["/usr/bin/xcompmgr", "-n"], env=env,
                                           stdout=log, stderr=log)
                processes.append(process)
                time.sleep(0.15)
                if server.poll() is not None or process.poll() is not None:
                    raise RuntimeError("Isolated compositor failed")
                env["FIXTURE_COMPOSITOR_PID"] = str(process.pid)
            yield env
        finally:
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)
            for process in reversed(processes):
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)


class ManagerCaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="uu-manager-isolation-")
        cls.directory = Path(cls.temp.name)
        cls.module = cls.directory / "uu-manager-capture.so"
        cls.fixture = cls.directory / "fixture"
        cls.negative_module = cls.directory / "negative-module.so"
        cls.boundary_module = cls.directory / "boundary-module.so"
        source = (ROOT / "src/uu_manager_capture.c").read_text()
        exemption = re.search(r"        if \(scan_candidate != 0.*?\n            return 0;", source, re.S)
        if not exemption:
            raise RuntimeError("The exact scoped attributes exemption was not found")
        negative = cls.directory / "negative.c"
        old = source[:exemption.start()] + source[exemption.end():]
        old = old.replace("        private_error = event->error_code;",
                          '        fprintf(stderr, "test-foreign-error resource=%lu code=%u opcode=%u minor=%u serial=%lu candidate=%lu\\n", '
                          'event->resourceid, (unsigned int)event->error_code, (unsigned int)event->request_code, '
                          '(unsigned int)event->minor_code, event->serial, scan_candidate);\n'
                          '        private_error = event->error_code;')
        negative.write_text(old)
        boundary = cls.directory / "boundaries.c"
        boundary.write_text(source.replace("#include <dlfcn.h>", "#include <assert.h>\n#include <dlfcn.h>") + TRAP_BOUNDARIES)
        cls.probes = cls.directory / "render-probe"
        cls.probes.mkdir()
        cls.missing_api = cls.directory / "missing-api"
        cls.missing_api.mkdir()
        commands = [
            ["/usr/bin/cc", *FLAGS, "-fPIC", "-shared", "-I", str(ROOT / "src"),
             str(negative), "-o", str(cls.negative_module), "-ldl", "-pthread"],
            ["/usr/bin/cc", *FLAGS, "-fPIC", "-shared", "-I", str(ROOT / "src"),
             str(boundary), "-o", str(cls.boundary_module), "-ldl", "-pthread"],
            ["/usr/bin/cc", *FLAGS, "-fPIC", "-shared", str(ROOT / "src/uu_manager_capture.c"),
             "-o", str(cls.module), "-ldl", "-pthread"],
            ["/usr/bin/cc", *FLAGS, str(ROOT / "tests/fixtures/manager_capture_fixture.c"),
             "-o", str(cls.fixture), "-ldl"],
            ["/usr/bin/cc", *FLAGS, "-fPIC", "-shared", str(ROOT / "tests/fixtures/manager_capture_render_probe.c"),
             "-o", str(cls.probes / "libXrender.so.1"), "-ldl"],
        ]
        for command in commands:
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            if result.returncode:
                raise RuntimeError(result.stdout + result.stderr)
        result = subprocess.run(["/usr/bin/cc", *FLAGS, "-fPIC", "-shared", "-x", "c", "-",
                                 "-o", str(cls.missing_api / "libXcomposite.so.1")],
                                input="int unavailable_composite_api;\n", capture_output=True,
                                text=True, timeout=30)
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def run_fixture(self, mode, before=None, other=False, libraries=None, settings=None, module=None):
        with tempfile.TemporaryDirectory(prefix="case-", dir=self.directory) as case:
            directory = Path(case)
            ready = directory / "ready"
            if before:
                before(ready)
            large = mode.startswith(("role-", "controller-"))
            with isolated_display(directory / "display", "3840x2160x24" if large else "800x600x24",
                                  compositor=(settings or {}).get("UURB_MANAGER_CAPTURE_ISOLATE_ROOT") != "1") as env:
                env.update(settings or {})
                env["LD_LIBRARY_PATH"] = str(libraries or self.probes)
                env["UURB_MANAGER_CAPTURE_LOG"] = str(directory / "capture.log")
                if mode == "controller-boundary":
                    for arguments in (["--newmode", "1280x720_test", "74.50", "1280", "1344", "1472", "1664",
                                       "720", "723", "728", "748", "-hsync", "+vsync"],
                                      ["--addmode", "screen", "1280x720_test"]):
                        subprocess.run(["/usr/bin/xrandr", *arguments], env=env, check=True,
                                       capture_output=True, timeout=5)
                command = [str(self.fixture), str(module or self.module), mode, str(ready)]
                if other:
                    with isolated_display(directory / "other") as other_env:
                        result = subprocess.run(command + [other_env["DISPLAY"], other_env["XAUTHORITY"]], env=env,
                                                capture_output=True, text=True, timeout=20)
                else:
                    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=20)
            if mode == "foreign-churn":
                created = Path(str(ready) + ".foreign").read_bytes()
                self.assertTrue(created)
                self.assertEqual(len(created) % struct.calcsize("@LL"), 0)
                ranges = list(struct.iter_unpack("@LL", created))
                self.assertTrue(all(last - first == 63 for first, last in ranges))
                if module == self.negative_module:
                    failures = re.findall(r"test-foreign-error resource=(\d+) code=(\d+) opcode=(\d+) minor=(\d+) serial=(\d+) candidate=(\d+)", result.stderr)
                    self.assertTrue(failures)
                    ranges.sort()
                    starts = [first for first, _ in ranges]
                    for resource, code, opcode, minor, serial, candidate in failures:
                        self.assertEqual((code, opcode, minor), ("3", "3", "0"))
                        self.assertEqual(resource, candidate)
                        self.assertGreater(int(serial), 0)
                        interval = bisect.bisect_right(starts, int(resource)) - 1
                        self.assertGreaterEqual(interval, 0)
                        self.assertLessEqual(int(resource), ranges[interval][1])
            marker = ready.read_text() if ready.is_file() and not ready.is_symlink() else None
            permissions = ready.stat().st_mode & 0o777 if marker is not None else None
            log = (directory / "capture.log").read_text() if (directory / "capture.log").exists() else ""
            return result, marker, permissions, log

    def assert_fixture(self, mode, **arguments):
        result, marker, permissions, _ = self.run_fixture(mode, **arguments)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("fixture passed", result.stdout)
        self.assertRegex(marker, r"^00000000-0000-4000-8000-000000000001 [1-9][0-9]* [1-9][0-9]* :[0-9]+\n$")
        self.assertEqual(permissions, 0o600)

    def test_frame_capture_ignores_relay_occlusion_without_polling_focus(self):
        self.assert_fixture("occlusion")

    def test_isolated_manager_stays_off_root_and_routes_input_after_relay_refocus(self):
        self.assert_fixture("isolated-input", settings={"UURB_MANAGER_CAPTURE_ISOLATE_ROOT": "1"})

    def test_isolated_manager_normal_popup_remains_visible_and_clickable(self):
        self.assert_fixture("normal-popup", settings={"UURB_MANAGER_CAPTURE_ISOLATE_ROOT": "1"})

    def test_isolated_manager_capture_survives_move_resize_unmap_and_remap(self):
        self.assert_fixture("lifecycle", settings={"UURB_MANAGER_CAPTURE_ISOLATE_ROOT": "1"})

    def test_foreign_destroyed_tree_candidates_do_not_blank_healthy_owned_pixels(self):
        result, _, _, log = self.run_fixture("foreign-churn")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("normal=90 black=0", result.stdout)
        self.assertNotIn("safe blank", log)

    def test_real_foreign_churn_reproduces_black_pixels_without_exact_exemption(self):
        result, _, _, log = self.run_fixture("foreign-churn", module=self.negative_module,
                                            settings={"FIXTURE_EXPECT_FOREIGN_BLANK": "1"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"normal=\d+ black=[1-9][0-9]*")
        self.assertIn("safe blank", log)

    def test_exact_attributes_scope_preserves_other_errors_and_prior_failure(self):
        self.assert_fixture("trap-boundaries", module=self.boundary_module)

    def test_destroyed_owned_frame_remains_fail_safe_blank(self):
        result, _, _, log = self.run_fixture("owned-frame-destroy")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("safe blank", log)

    def test_actual_getimage_getsubimage_and_mit_shm_preserve_destination(self):
        self.assert_fixture("getters")

    def test_argb_transparent_half_and_opaque_dialog_composition(self):
        self.assert_fixture("alpha")

    def test_normal_top_level_popup_is_included(self):
        self.assert_fixture("normal-popup")

    def test_manager_excludes_independently_titled_outgoing_sessions(self):
        self.assert_fixture("role-manager")

    def test_controller_full_size_associated_argb_and_nested_popup_input(self):
        self.assert_fixture("role-controller")

    def test_controller_survives_manager_compositor_exit(self):
        self.assert_fixture("controller-compositor-exit")

    def test_generic_argb_control_center_and_nested_transient_are_owned(self):
        self.assert_fixture("controller-generic-popup")

    def test_wine_compound_text_titles_use_utf8_ewmh_for_popup_ownership(self):
        self.assert_fixture("controller-generic-popup-utf8")

    def test_overlapping_unicode_controllers_cannot_share_generic_popup(self):
        self.assert_fixture("controller-generic-ambiguous-utf8")

    def test_malformed_ewmh_target_title_fails_closed(self):
        for kind in ("type", "format", "nul", "oversized", "truncated", "utf8", "surrogate"):
            with self.subTest(kind=kind):
                result, marker, _, _ = self.run_fixture("controller-title-invalid-" + kind)
                self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
                self.assertIsNone(marker)

    def test_malformed_generic_popup_title_cannot_fall_back_to_unnamed(self):
        self.assert_fixture("controller-generic-invalid-nul")

    def test_overlapping_controllers_do_not_share_generic_popup_or_nested_dialog(self):
        self.assert_fixture("controller-generic-ambiguous")

    def test_generic_640_popup_cannot_masquerade_as_its_own_controller(self):
        self.assert_fixture("controller-generic-640")

    def test_controller_frame_budget_is_bounded_and_above_manager_limit(self):
        self.assert_fixture("controller-rate")

    def test_invalid_role_or_unbounded_frame_rate_fails_closed(self):
        for settings in ({"UURB_MANAGER_CAPTURE_ROLE": "relay"},
                         {"UURB_MANAGER_CAPTURE_FPS": "61"},
                         {"UURB_MANAGER_CAPTURE_FPS": "0"}):
            with self.subTest(settings=settings):
                result, marker, _, _ = self.run_fixture("role-controller", settings=settings)
                self.assertEqual(result.returncode, 70, result.stderr)
                self.assertIsNone(marker)

    def test_native_controller_normal_close_uses_owned_wm_delete_only(self):
        self.controller_lifecycle("close")

    def test_controller_failure_preserves_remote_session_for_retry(self):
        self.controller_lifecycle("failure")

    def test_controller_guardian_retires_sidecar_after_parent_killed(self):
        self.controller_lifecycle("orphan")

    def test_manager_watcher_auto_opens_controller_and_closes_independently(self):
        self.controller_lifecycle("auto")

    def test_native_controller_fits_canvas_cycle_and_preserves_user_resize(self):
        self.controller_lifecycle("mode-fit")

    def test_controller_rejects_root_outside_input_and_releases_accepted_press(self):
        self.assert_fixture("controller-boundary")

    def test_native_controller_fits_nested_generic_popup_and_clicks_it(self):
        self.controller_lifecycle("popup-fit")

    def test_native_unicode_controller_shows_menus_and_excludes_chinese_lobby(self):
        self.controller_lifecycle("popup-utf8")

    def test_controller_transient_cycles_cannot_enter_popup_layers(self):
        self.assert_fixture("controller-cycle")

    def test_two_generic_argb_popups_cannot_infer_ownership_from_their_cycle(self):
        self.assert_fixture("controller-generic-cycle")

    def test_controller_transient_chain_beyond_eight_is_excluded(self):
        self.assert_fixture("controller-deep")

    def controller_lifecycle(self, scenario):
        popup_fit = scenario in ("popup-fit", "popup-utf8")
        with tempfile.TemporaryDirectory(prefix="native-controller-", dir=self.directory) as case:
            directory = Path(case)
            home = directory / "home"
            runtime = directory / "runtime"
            home.mkdir(); runtime.mkdir()
            processes = []
            session = None

            def await_condition(condition, message, timeout=12):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if condition():
                        return
                    time.sleep(0.05)
                logs = "\n".join(path.read_text(errors="replace")[-4000:]
                                 for path in directory.rglob("*.log"))
                self.fail(message + "\n" + logs)

            def alive(pid):
                path = Path(f"/proc/{pid}/stat")
                try:
                    return path.read_text().rsplit(") ", 1)[1].split()[0] != "Z"
                except (FileNotFoundError, ProcessLookupError):
                    return False

            def command(arguments, environment):
                return subprocess.run(arguments, env=environment, capture_output=True,
                                      text=True, timeout=5)

            with isolated_display(directory / "private", "3840x2160x24") as private, \
                    isolated_display(directory / "native", "3840x2160x24") as native, \
                    (directory / "lifecycle.log").open("wb") as log:
                try:
                    for surface in (private, native):
                        wm_env = dict(surface, HOME=str(home), DBUS_SESSION_BUS_ADDRESS=f"unix:path={directory}/missing")
                        wm_env.pop("SESSION_MANAGER", None)
                        processes.append(subprocess.Popen(["/usr/bin/openbox"], env=wm_env,
                                                          stdout=log, stderr=log))
                    time.sleep(0.3)
                    ids = directory / "windows"
                    app = subprocess.Popen([str(self.fixture), str(self.module),
                                            ("serve-controller-popup-utf8" if scenario == "popup-utf8" else
                                             "serve-controller-popup" if popup_fit else "serve-controller"), str(ids)],
                                           env=private, stdout=log, stderr=log)
                    processes.append(app)
                    await_condition(ids.exists, "Owned controller fixture did not initialize")
                    target, lobby, relay = ids.read_text().splitlines()
                    prefix = home / "prefix"
                    (prefix / "compat").mkdir(parents=True)
                    shutil.copyfile(self.module, prefix / "compat/uu-manager-capture.so")
                    shutil.copyfile(ROOT / "scripts/uu-controller-fit.py", prefix / "compat/uu-controller-fit.py")
                    bridge = runtime / "uu-remote-bridge"
                    bridge.mkdir(mode=0o700)
                    (bridge / "private-display").write_text(private["DISPLAY"] + "\n")
                    shutil.copyfile(private["XAUTHORITY"], bridge / "Xauthority")
                    fonts = home / ".local/share/uu-remote/tools/uu-tools-fonts.conf"
                    fonts.parent.mkdir(parents=True)
                    shutil.copyfile(ROOT / "desktop/uu-tools-fonts.conf", fonts)
                    env = dict(native, HOME=str(home), UURB_WINEPREFIX=str(prefix),
                               XDG_RUNTIME_DIR=str(runtime), XDG_STATE_HOME=str(directory / "state"),
                               DBUS_SESSION_BUS_ADDRESS=f"unix:path={directory}/missing")
                    start = Path(f"/proc/{app.pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]
                    arguments = [str(ROOT / "scripts/uu-remote-console"), "controller-window",
                                 target, str(app.pid), start, lobby]
                    if scenario == "auto":
                        helpers = (ROOT / "scripts/uu-remote-console").read_text().split('case "${1:-open}" in', 1)[0]
                        helpers += "\nscript_path=" + shlex.quote(str(ROOT / "scripts/uu-remote-console"))
                        helpers += '\ndiscover_bridge\n/usr/bin/install -d -m 0700 "$base_state_dir"\n'
                        helpers += f'watch_controller_windows "$$" "$(process_start_time "$$")" {lobby} {app.pid} {start}\n'
                        arguments = ["/usr/bin/bash", "-c", helpers]
                    console = subprocess.Popen(arguments, env=env, stdout=log, stderr=log)
                    processes.append(console)
                    record = runtime / f"uu-remote-console/controller.{target}/window-session"
                    def viewer_ready():
                        nonlocal session
                        if not record.exists():
                            return False
                        candidate = record.read_text().splitlines()
                        if len(candidate) == 10 and candidate[8].isdigit():
                            session = candidate
                            return alive(candidate[8])
                        return False
                    await_condition(viewer_ready, "Native controller never became ready")
                    vnc, viewer = int(session[6]), int(session[8])
                    def native_windows():
                        return command(["/usr/bin/xdotool", "search", "--onlyvisible", "--pid", str(viewer),
                                        "--class", "TigerVNC Viewer"], native).stdout.splitlines()
                    await_condition(native_windows, "Native controller window did not map")
                    viewer_window = native_windows()[-1]
                    vnc_env = Path(f"/proc/{vnc}/environ").read_bytes().split(b"\0")
                    self.assertIn(b"UURB_MANAGER_CAPTURE_ROLE=controller", vnc_env)
                    self.assertIn(b"UURB_MANAGER_CAPTURE_FPS=60", vnc_env)
                    self.assertFalse(any(item.startswith(b"FONTCONFIG_FILE=") for item in vnc_env))
                    viewer_env = Path(f"/proc/{viewer}/environ").read_bytes().split(b"\0")
                    self.assertIn(f"FONTCONFIG_FILE={fonts}".encode(), viewer_env)
                    argv = Path(f"/proc/{vnc}/cmdline").read_bytes().split(b"\0")
                    self.assertEqual(argv[argv.index(b"-scale") + 1], b"1")
                    self.assertEqual(argv[argv.index(b"-sid") + 1], target.encode())
                    for child in (vnc, viewer):
                        for descriptor in Path(f"/proc/{child}/fd").iterdir():
                            try:
                                inherited = os.readlink(descriptor)
                            except FileNotFoundError:
                                continue
                            self.assertNotIn(inherited, [str(record.parent / "window.lock"),
                                                        str(runtime / "uu-remote-console/listener-start.lock")])
                    closed = Path(str(ids) + ".closed")
                    if popup_fit:
                        parent, nested = Path(str(ids) + ".popups").read_text().splitlines()
                        def window_geometry(window):
                            result = command(["/usr/bin/xwininfo", "-id", window], private)
                            self.assertEqual(result.returncode, 0, result.stderr)
                            return [int(re.search(r"\b" + label + r":\s+(-?\d+)", result.stdout).group(1))
                                    for label in ("Absolute upper-left X", "Absolute upper-left Y", "Width", "Height")]
                        main_geometry = window_geometry(target)
                        def fits():
                            x, y, width, height = window_geometry(nested)
                            mx, my, mw, mh = main_geometry
                            return x >= mx and y >= my and x + width <= mx + mw and y + height <= my + mh
                        await_condition(fits, f"Nested owned popup did not fit inside the native viewport: main={main_geometry}, popup={window_geometry(nested)}")
                        popup = window_geometry(nested)
                        # A native viewer click must reach the real nested Qt
                        # window, rather than the relay behind its pixels.
                        px, py = popup[0] - main_geometry[0] + 60, popup[1] - main_geometry[1] + 20
                        def native_pixel(pixel_x=px, pixel_y=py):
                            xlib = ctypes.CDLL("libX11.so.6")
                            xlib.XOpenDisplay.argtypes = [ctypes.c_char_p]
                            xlib.XOpenDisplay.restype = ctypes.c_void_p
                            xlib.XGetImage.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
                                                      ctypes.c_uint, ctypes.c_uint, ctypes.c_ulong, ctypes.c_int]
                            xlib.XGetImage.restype = ctypes.c_void_p
                            xlib.XGetPixel.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
                            xlib.XGetPixel.restype = ctypes.c_ulong
                            xlib.XDestroyImage.argtypes = [ctypes.c_void_p]
                            xlib.XCloseDisplay.argtypes = [ctypes.c_void_p]
                            with patch.dict(os.environ, {"XAUTHORITY": native["XAUTHORITY"]}):
                                connection = xlib.XOpenDisplay(native["DISPLAY"].encode())
                            self.assertTrue(connection)
                            image = None
                            try:
                                image = xlib.XGetImage(connection, int(viewer_window), pixel_x, pixel_y, 1, 1, ctypes.c_ulong(-1), 2)
                                self.assertTrue(image)
                                return xlib.XGetPixel(image, 0, 0) & 0xffffff
                            finally:
                                if image:
                                    xlib.XDestroyImage(image)
                                xlib.XCloseDisplay(connection)
                        await_condition(lambda: native_pixel() == 0xffffff,
                                        f"Nested popup pixels did not reach the real native viewer: pixel={native_pixel():x}, popup={popup}, point={px},{py}")
                        parent_geometry = window_geometry(parent)
                        self.assertGreaterEqual(main_geometry[0] + px, parent_geometry[0])
                        self.assertLess(main_geometry[0] + px, parent_geometry[0] + parent_geometry[2])
                        self.assertGreaterEqual(main_geometry[1] + py, parent_geometry[1])
                        self.assertLess(main_geometry[1] + py, parent_geometry[1] + parent_geometry[3])
                        self.assertEqual(native_pixel(50, 50), 0x00ff00)
                        if scenario == "popup-utf8":
                            lobby_geometry = window_geometry(lobby)
                            self.assertEqual(native_pixel(lobby_geometry[0] - main_geometry[0] + 10,
                                                          lobby_geometry[1] - main_geometry[1] + 10), 0x00ff00)
                        self.assertEqual(native_pixel(parent_geometry[0] - main_geometry[0] + 20,
                                                      parent_geometry[1] - main_geometry[1] + 20), 0x807f00)
                        result = command(["/usr/bin/xdotool", "windowactivate", "--sync", viewer_window,
                                          "mousemove", "--window", viewer_window, str(px), str(py), "click", "1"], native)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        clicked = Path(str(ids) + ".clicked")
                        await_condition(clicked.exists, "Native input did not reach an owned popup; " +
                                        command(["/usr/bin/xdotool", "getmouselocation", "--shell"], private).stdout)
                        self.assertEqual(clicked.read_text().strip(), nested)
                        for popup_window in (nested, parent):
                            result = command(["/usr/bin/xdotool", "windowunmap", popup_window], private)
                            self.assertEqual(result.returncode, 0, result.stderr)
                        await_condition(lambda: native_pixel() == 0x00ff00,
                                        "Unmapped owned popups left stale pixels in the native controller")
                        if scenario == "popup-utf8":
                            from Xlib import display as xdisplay, Xatom
                            script = (ROOT / "scripts/uu-remote-console").read_text()
                            close_code = script.split("<<'PY_CLOSE'\n", 1)[1].split("\nPY_CLOSE", 1)[0]
                            title = "测试的Mac mini"
                            with patch.dict(os.environ, {"XAUTHORITY": private["XAUTHORITY"]}):
                                connection = xdisplay.Display(private["DISPLAY"])
                            try:
                                target_window = connection.create_resource_object("window", int(target))
                                name_atom = connection.intern_atom("_NET_WM_NAME")
                                utf8_atom = connection.intern_atom("UTF8_STRING")
                                invalid = [(Xatom.STRING, title.encode()),
                                           (utf8_atom, title.encode() + b"\0trailing"),
                                           (utf8_atom, b"x" * 1024),
                                           (utf8_atom, b"x" * 5000),
                                           (utf8_atom, b"\xc0\xaf")]
                                for atom_type, value in invalid:
                                    target_window.change_property(name_atom, atom_type, 8, value)
                                    connection.sync()
                                    refusal = command(["/usr/bin/python3", "-c", close_code,
                                                       target, str(app.pid), title], private)
                                    self.assertEqual(refusal.returncode, 1, refusal.stderr)
                                    self.assertFalse(closed.exists())
                                target_window.delete_property(name_atom)
                                connection.sync()
                                refusal = command(["/usr/bin/python3", "-c", close_code,
                                                   target, str(app.pid), title], private)
                                self.assertEqual(refusal.returncode, 1, refusal.stderr)
                                self.assertFalse(closed.exists())
                                target_window.change_property(name_atom, utf8_atom, 8, title.encode())
                                connection.sync()
                            finally:
                                connection.close()
                    if scenario == "mode-fit":
                        def geometry():
                            observed = command(["/usr/bin/python3", str(ROOT / "scripts/uu-controller-fit.py"),
                                                target, str(app.pid), start, "--observe"], private)
                            self.assertEqual(observed.returncode, 0, observed.stderr)
                            return json.loads(observed.stdout)["geometry"]
                        original = geometry()
                        extents_output = command(["/usr/bin/xprop", "-id", target, "_NET_FRAME_EXTENTS"], private)
                        left, right, top, bottom = map(int, re.findall(r"\d+", extents_output.stdout.split("=", 1)[1]))
                        fitted = [left, top, 1280 - left - right, 720 - top - bottom]
                        for options in (["--newmode", "1280x720_test", "74.50", "1280", "1344", "1472", "1664",
                                         "720", "723", "728", "748", "-hsync", "+vsync"],
                                        ["--addmode", "screen", "1280x720_test"]):
                            result = command(["/usr/bin/xrandr", *options], private)
                            self.assertEqual(result.returncode, 0, result.stderr)
                        def resize(mode, expected):
                            result = command(["/usr/bin/xrandr", "--output", "screen", "--mode", mode], private)
                            self.assertEqual(result.returncode, 0, result.stderr)
                            await_condition(lambda: geometry() == expected, "Controller did not fit/restore its canvas")
                            self.assertEqual(record.read_text().splitlines()[6:10:2], [str(vnc), str(viewer)])
                            self.assertTrue(alive(vnc)); self.assertTrue(alive(viewer))
                            time.sleep(0.4)
                            px, py = expected[2] - 72, expected[3] - 68
                            result = command(["/usr/bin/xdotool", "windowactivate", "--sync", viewer_window,
                                              "mousemove", "--window", viewer_window, str(px), str(py), "click", "1"], native)
                            self.assertEqual(result.returncode, 0, result.stderr)
                            def pointer_reached():
                                location = command(["/usr/bin/xdotool", "getmouselocation", "--shell"], private)
                                fields = dict(line.split("=", 1) for line in location.stdout.splitlines())
                                return (abs(int(fields["X"]) - expected[0] - px) <= 2 and
                                        abs(int(fields["Y"]) - expected[1] - py) <= 2)
                            await_condition(pointer_reached, "Native far-corner click did not reach the fitted controller")
                        resize("1280x720_test", fitted)
                        resize("3840x2160", original)
                        result = command(["/usr/bin/xdotool", "windowsize", "--sync", target, "1600", "900"], private)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        time.sleep(1.2)
                        preferred = geometry()
                        self.assertEqual(preferred[2:], [1600, 900])
                        resize("1280x720_test", fitted)
                        resize("3840x2160", preferred)
                    if scenario == "auto":
                        # Closing the lobby's watcher cannot close a valid
                        # independently locked outgoing controller.
                        console.terminate(); self.assertEqual(console.wait(timeout=4), 0)
                        time.sleep(0.6)
                        self.assertTrue(alive(viewer)); self.assertTrue(alive(vnc))
                        self.assertFalse(closed.exists())
                        duplicate = subprocess.Popen(arguments, env=env, stdout=log, stderr=log)
                        processes.append(duplicate)
                        time.sleep(1.1)
                        self.assertEqual(record.read_text().splitlines()[8], str(viewer))
                        duplicate.terminate(); self.assertEqual(duplicate.wait(timeout=4), 0)
                    if scenario == "orphan":
                        console.kill(); console.wait(timeout=3)
                        self.assertTrue(alive(viewer)); self.assertTrue(alive(vnc))
                        self.assertFalse(closed.exists())
                    if scenario == "failure":
                        # A fatal native process cannot be treated as a request
                        # to terminate the original outgoing UU session.
                        os.kill(viewer, signal.SIGKILL)
                    else:
                        # Send the native viewer's close protocol directly;
                        # Alt+F4 can be forwarded through RFB to the source.
                        result = command(["/usr/bin/xdotool", "windowactivate", "--sync", viewer_window], native)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        from Xlib import display as xdisplay, protocol, X
                        with patch.dict(os.environ, {"XAUTHORITY": native["XAUTHORITY"]}):
                            connection = xdisplay.Display(native["DISPLAY"])
                        try:
                            window = connection.create_resource_object("window", int(viewer_window))
                            protocols = connection.intern_atom("WM_PROTOCOLS")
                            delete = connection.intern_atom("WM_DELETE_WINDOW")
                            self.assertIn(delete, list(window.get_full_property(protocols, X.AnyPropertyType).value))
                            event = protocol.event.ClientMessage(
                                window=window, client_type=protocols,
                                data=(32, [delete, X.CurrentTime, 0, 0, 0]))
                            window.send_event(event, event_mask=0)
                            connection.sync()
                        finally:
                            connection.close()
                    await_condition(lambda: not alive(viewer) and not alive(vnc), "Controller sidecars did not retire", 15)
                    if scenario in ("close", "auto", "mode-fit", "popup-fit", "popup-utf8"):
                        await_condition(closed.exists, "Normal viewer close never delivered WM_DELETE_WINDOW")
                        if scenario in ("close", "mode-fit", "popup-fit", "popup-utf8"):
                            self.assertEqual(console.wait(timeout=4), 0)
                    else:
                        if scenario != "orphan":
                            self.assertNotEqual(console.wait(timeout=4), 0)
                        self.assertFalse(closed.exists())
                        self.assertEqual(command(["/usr/bin/xdotool", "getwindowname", target], private).stdout.strip(),
                                         "Fixture Mac mini")
                    await_condition(lambda: not record.exists(), "Owned session record was not retired")
                    self.assertIsNone(app.poll())
                    self.assertEqual(command(["/usr/bin/xdotool", "getwindowname", lobby], private).stdout.strip(),
                                     "网易UU远程" if scenario == "popup-utf8" else "UU Remote")
                    self.assertEqual(command(["/usr/bin/xprop", "-id", relay, "WM_CLASS"], private).returncode, 0)
                finally:
                    # Only PID identities read from our private session record
                    # are sidecars; never search or kill a shared desktop.
                    if session:
                        for index in (0, 8, 6):
                            if session[index].isdigit() and alive(session[index]):
                                identity = Path(f"/proc/{session[index]}/stat").read_text().rsplit(") ", 1)[1].split()[19]
                                if identity == session[index + 1]:
                                    os.kill(int(session[index]), signal.SIGTERM)
                    for process in reversed(processes):
                        if process.poll() is None:
                            process.terminate()
                        try:
                            process.wait(timeout=4)
                        except subprocess.TimeoutExpired:
                            process.kill(); process.wait(timeout=4)

    def test_native_click_recovers_pointer_and_lost_owner_blocks_input(self):
        self.assert_fixture("input")

    def test_cleanup_releases_only_accepted_key_and_button_after_owner_lost(self):
        self.assert_fixture("cleanup-input")

    def test_move_resize_unmap_remap_destroy_clear_old_canvas(self):
        self.assert_fixture("lifecycle")

    def test_changed_pid_or_class_blanks_without_reading_root(self):
        self.assert_fixture("identity")

    def test_nonroot_drawable_is_passed_through(self):
        self.assert_fixture("scope")

    def test_other_display_root_is_passed_through(self):
        # The second fixture display has an independent authority cookie. Make
        # the caller use a fresh connection with that authority in C instead.
        self.assert_fixture("scope", other=True)

    def test_old_ready_marker_cannot_be_overwritten(self):
        result, marker, _, log = self.run_fixture("occlusion", before=lambda path: path.write_text("old marker\n"))
        self.assertEqual(result.returncode, 70, result.stderr)
        self.assertEqual(marker, "old marker\n")
        self.assertIn("readiness handshake failed", log)

    def test_symlink_ready_marker_cannot_be_followed(self):
        def symlink(path):
            target = path.with_name("untouched")
            target.write_text("untouched\n")
            path.symlink_to(target)
        result, marker, _, log = self.run_fixture("occlusion", before=symlink)
        self.assertEqual(result.returncode, 70, result.stderr)
        self.assertIsNone(marker)
        self.assertIn("readiness handshake failed", log)

    def test_missing_composite_api_exits_before_readiness(self):
        result, marker, _, log = self.run_fixture("occlusion", libraries=self.missing_api)
        self.assertEqual(result.returncode, 70, result.stderr)
        self.assertIsNone(marker)
        self.assertIn("initial Composite capture failed", log)


if __name__ == "__main__":
    unittest.main()
