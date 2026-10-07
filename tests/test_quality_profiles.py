"""Isolated quality controls; never restart a real bridge or modify UU settings."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import select
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("uu_quality", REPO / "scripts/uu-quality.py")
quality = importlib.util.module_from_spec(spec)
spec.loader.exec_module(quality)
WAIT_READY = quality.wait_ready


class QualityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.config = self.home / ".config/uu-remote-bridge/environment"
        self.config.parent.mkdir(parents=True)
        self.original = (b"# preserve comments\nUURB_RESOLUTION=1920x1080\n"
                         b"UURB_WINEPREFIX=/custom/prefix\nUURB_CURSOR_SIZE=24\n"
                         b"UURB_KEYBOARD_ROUTE=rdp\nUURB_PRIVATE_TOKEN=do-not-display\n")
        self.config.write_bytes(self.original)
        self.home_patch = patch.object(Path, "home", return_value=self.home)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.ready_patch = patch.object(quality, "wait_ready", return_value=None)
        self.ready = self.ready_patch.start()
        self.addCleanup(self.ready_patch.stop)
        self.timer_patch = patch.object(quality, "timer_control", return_value=None)
        self.timer = self.timer_patch.start()
        self.addCleanup(self.timer_patch.stop)

    def run_main(self, *arguments):
        output, error = io.StringIO(), io.StringIO()
        with patch.object(Path, "home", return_value=self.home), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            result = quality.main(arguments)
        return result, output.getvalue(), error.getvalue()

    def test_profiles_have_exact_canvas_sizes_and_no_fps_promises(self):
        result, output, _ = self.run_main("list")
        self.assertEqual(result, 0)
        for size in ("1280x720", "1920x1080", "2560x1440", "3840x2160"):
            self.assertIn(size, output)
        self.assertNotIn("5120x2880", output)
        self.assertNotIn("2880p", output)
        self.assertNotIn("60fps", output)

    def test_apply_preserves_other_bytes_restarts_once_and_sets_private_mode(self):
        with patch.object(quality, "systemctl") as systemctl:
            self.assertEqual(self.run_main("apply", "1440p")[0], 0)
        self.assertEqual(self.config.read_bytes(), self.original.replace(b"1920x1080", b"2560x1440"))
        systemctl.assert_called_once_with("restart", "uu-remote-bridge.service")
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)

    def test_removed_5k_profile_is_refused_without_changing_saved_4k(self):
        original_4k = self.original.replace(b"1920x1080", b"3840x2160")
        self.config.write_bytes(original_4k)
        with patch.object(quality, "systemctl") as restart:
            with self.assertRaises(SystemExit) as error:
                self.run_main("apply", "2880p")
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(self.config.read_bytes(), original_4k)
        restart.assert_not_called()
        self.timer.assert_not_called()

    def test_same_profile_does_not_restart(self):
        with patch.object(quality, "systemctl") as systemctl:
            self.assertEqual(self.run_main("apply", "1080p")[0], 0)
        systemctl.assert_not_called()

    def test_saved_profile_unknown_live_canvas_reports_no_live_change(self):
        with patch.object(quality, "canvas_status", return_value=None), patch.object(quality, "systemctl") as restart:
            result, output, _ = self.run_main("apply", "1080p")
        self.assertEqual(result, 0)
        self.assertIn("当前UU画布无法验证，未更改实时分辨率", output)
        self.assertNotIn("无需重新连接", output)
        restart.assert_not_called()
        self.timer.assert_not_called()

    def test_same_saved_profile_restores_different_live_canvas_under_guard_without_restart(self):
        with patch.object(quality, "canvas_status", return_value={"live": "1280x720"}), patch.object(
            quality, "canvas_command", return_value={"live": "1920x1080"}) as restore, patch.object(quality, "systemctl") as restart:
            result, output, _ = self.run_main("apply", "1080p")
        self.assertEqual(result, 0)
        self.assertIn("当前UU画布已恢复为 1920x1080", output)
        restore.assert_called_once_with(self.config, "restore")
        restart.assert_not_called()
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertEqual(self.timer.call_count, 2)
        self.assertEqual(self.ready.call_count, 2)
        self.assertFalse((self.config.parent / "quality-guard-active").exists())

    def test_failed_live_restore_recovers_saved_startup_and_keeps_configuration(self):
        with patch.object(quality, "canvas_status", return_value={"live": "1280x720"}), patch.object(
            quality, "canvas_command", side_effect=RuntimeError("CDS failed")), patch.object(quality, "systemctl") as restart:
            result, _, error = self.run_main("apply", "1080p")
        self.assertEqual(result, 1)
        self.assertIn("saved startup resolution restored", error)
        self.assertEqual(self.config.read_bytes(), self.original)
        restart.assert_called_once_with("restart", "uu-remote-bridge.service")
        self.assertEqual(self.timer.call_count, 2)

    def test_live_restore_never_runs_without_armed_recovery(self):
        self.timer.side_effect = RuntimeError("timer unavailable")
        with patch.object(quality, "canvas_status", return_value={"live": "1280x720"}), patch.object(
            quality, "canvas_command") as restore, patch.object(quality, "systemctl") as restart:
            self.assertEqual(self.run_main("apply", "1080p")[0], 1)
        restore.assert_not_called()
        restart.assert_not_called()
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_status_distinguishes_saved_source_from_live_canvas(self):
        with patch.object(quality, "canvas_status", return_value={"live": "1280x720"}):
            result, output, _ = self.run_main("status")
        self.assertEqual(result, 0)
        self.assertIn("保存的启动尺寸（RDP请求）：1920x1080", output)
        self.assertIn("当前UU画布：1280x720", output)

    def test_canvas_status_rejects_invalid_helper_records_without_claiming_live_size(self):
        prefix = self.home / "prefix"
        helper = prefix / "compat/uu-display-modes.py"
        helper.parent.mkdir(parents=True)
        helper.touch()
        runtime = self.home / "runtime"
        state = runtime / "uu-remote-bridge"
        state.mkdir(parents=True)
        (state / "private-display").write_text(":123\n")
        (state / "Xauthority").touch()
        self.config.write_bytes(self.original.replace(b"/custom/prefix", os.fsencode(prefix)))
        for record in ([], {"startup": "1920x1080", "live": "1280x720", "fullscreen": False},
                       {"startup": "3840x2160", "live": "1920x1080", "fullscreen": True}):
            response = subprocess.CompletedProcess([], 0, json.dumps(record), "")
            with self.subTest(record=record), patch.dict(os.environ, XDG_RUNTIME_DIR=str(runtime)), patch.object(
                quality.subprocess, "run", return_value=response
            ):
                self.assertIsNone(quality.canvas_status(self.config))

    def test_successful_restart_with_unready_bridge_is_a_failure(self):
        with patch.object(quality, "systemctl"), patch.object(
            quality, "wait_ready", side_effect=[None, RuntimeError("not ready"), None]
        ):
            result, _, _ = self.run_main("apply", "1440p")
        self.assertEqual(result, 1)
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_gui_failure_is_visible_without_a_terminal(self):
        responses = [subprocess.CompletedProcess([], 0, "1440p\n"), subprocess.CompletedProcess([], 0, "")]
        with patch.dict(os.environ, DISPLAY=":fake"), patch.object(quality.shutil, "which", return_value="/usr/bin/zenity"), patch.object(
            quality.subprocess, "run", side_effect=responses
        ) as run, patch.object(quality, "systemctl", side_effect=RuntimeError("restart failed")):
            self.assertEqual(self.run_main("gui")[0], 1)
        self.assertTrue(any("--error" in call.args[0] for call in run.call_args_list))

    def test_following_resolution_is_preserved_and_fixed_selection_refused(self):
        self.config.write_bytes(self.original + b"UURB_FOLLOW_DESKTOP_RESOLUTION=on\n")
        before = self.config.read_bytes()
        with patch.object(quality, "systemctl") as systemctl:
            self.assertEqual(self.run_main("apply", "720p")[0], 1)
        self.assertEqual(self.config.read_bytes(), before)
        systemctl.assert_not_called()

    def test_failed_restart_restores_original_and_restarts_recovery_once(self):
        with patch.object(quality, "systemctl", side_effect=[RuntimeError("failed"), None]) as systemctl:
            result, _, error = self.run_main("apply", "2160p")
        self.assertEqual(result, 1)
        self.assertIn("restored", error)
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertEqual(systemctl.call_count, 2)

    def test_failed_recovery_is_reported_and_preserves_original(self):
        with patch.object(quality, "systemctl", side_effect=RuntimeError("failed")):
            self.assertEqual(self.run_main("apply", "2160p")[0], 1)
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_unready_baseline_refuses_the_trial_before_arming_or_restarting(self):
        self.ready.side_effect = RuntimeError("baseline unready")
        with patch.object(quality, "systemctl") as restart:
            result, _, error = self.run_main("apply", "1440p")
        self.assertEqual(result, 1)
        self.assertIn("Previous configuration is not ready", error)
        self.assertEqual(self.config.read_bytes(), self.original)
        self.timer.assert_not_called()
        restart.assert_not_called()

    def test_timer_is_armed_before_mutation_and_cancelled_only_after_readiness(self):
        def timer(command):
            active = self.config.parent / "quality-guard-active"
            guard = Path(active.read_text().strip())
            if command[0] == "/usr/bin/systemd-run":
                self.assertEqual(self.config.read_bytes(), self.original)
                self.assertIn("--on-active=90s", command)
                self.assertIn("--property=Restart=on-failure", command)
                self.assertIn("--property=RestartSec=5s", command)
                self.assertIn("--property=StartLimitIntervalSec=0", command)
                self.assertEqual(command[-4:], [str(guard / "recovery.py"), "guard", "rollback", str(guard)])
                self.assertEqual((guard / "recovery.py").read_bytes(), (REPO / "scripts/uu-quality.py").read_bytes())
                self.assertEqual((guard / "original.environment").read_bytes(), self.original)
                for private in (guard, guard / "record.json", guard / "original.environment",
                                guard / "expected.environment", guard / "recovery.py"):
                    self.assertEqual(private.stat().st_mode & 0o777, 0o700 if private == guard else 0o600)
                self.assertEqual(self.ready.call_count, 1)
            else:
                self.assertEqual(self.ready.call_count, 2)
                self.assertEqual(json.loads((guard / "record.json").read_text())["state"], "committed")
        self.timer.side_effect = timer
        with patch.object(quality, "systemctl"):
            self.assertEqual(self.run_main("apply", "1440p")[0], 0)
        self.assertEqual(self.timer.call_count, 2)
        self.assertFalse((self.config.parent / "quality-guard-active").exists())

    def test_timer_scheduling_failure_never_changes_or_restarts_the_bridge(self):
        self.timer.side_effect = RuntimeError("timer scheduling failed")
        with patch.object(quality, "systemctl") as restart:
            self.assertEqual(self.run_main("apply", "1440p")[0], 1)
        self.assertEqual(self.config.read_bytes(), self.original)
        restart.assert_not_called()
        self.assertFalse((self.config.parent / "quality-guard-active").exists())
        record = next((self.config.parent / "quality-guards").glob("*/record.json"))
        self.assertEqual(json.loads(record.read_text())["state"], "aborted")

    def test_unready_recovery_keeps_private_guard_armed_and_reports_no_success(self):
        self.ready.side_effect = [None, RuntimeError("target unready"), RuntimeError("recovery unready")]
        with patch.object(quality, "systemctl") as restart:
            result, output, error = self.run_main("apply", "1440p")
        self.assertEqual(result, 1)
        self.assertEqual(output, "")
        self.assertIn("recovery is not confirmed", error)
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertEqual(restart.call_count, 2)
        self.assertEqual(self.timer.call_count, 1)
        guard = Path((self.config.parent / "quality-guard-active").read_text().strip())
        self.assertEqual(json.loads((guard / "record.json").read_text())["state"], "restoring")

    def test_public_guard_commit_requires_readiness_and_rollback_preserves_external_changes(self):
        expected = self.original.replace(b"1920x1080", b"2560x1440")
        guard = quality.arm_guard(self.config, expected)
        quality.atomic_write(self.config, expected)
        self.ready.side_effect = RuntimeError("trial unready")
        with self.assertRaisesRegex(RuntimeError, "trial unready"):
            quality.commit_guard(guard)
        self.assertEqual(self.timer.call_count, 1)
        external = expected + b"UURB_EXTERNAL_SETTING=preserve\n"
        self.config.write_bytes(external)
        with patch.object(quality, "systemctl") as restart:
            with self.assertRaisesRegex(RuntimeError, "externally"):
                quality.rollback_guard(guard)
        restart.assert_not_called()
        self.assertEqual(self.config.read_bytes(), external)
        self.assertTrue((self.config.parent / "quality-guard-active").is_file())

    def test_independent_recovery_survives_sigkill_of_the_quality_caller(self):
        # A private scheduler runs the copied callback in a separate session,
        # replacing systemd only; neither fixture touches a real user service.
        runner = self.home / "isolated-recovery.py"
        runner.write_text('''import importlib.util, pathlib, sys, time
spec = importlib.util.spec_from_file_location("isolated_quality", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.wait_ready = lambda *a, **k: None
module.systemctl = lambda *a: None
module.timer_control = lambda *a: None
while not pathlib.Path(sys.argv[3]).exists(): time.sleep(.01)
assert module.rollback_guard(pathlib.Path(sys.argv[2]))
pathlib.Path(sys.argv[4]).write_text("recovered")
''')
        caller = self.home / "isolated-caller.py"
        caller.write_text('''import importlib.util, os, pathlib, subprocess, sys, time
spec = importlib.util.spec_from_file_location("isolated_quality", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.wait_ready = lambda *a, **k: None
def schedule(command):
    process = subprocess.Popen([sys.executable, sys.argv[2], command[-4], command[-1],
                                sys.argv[3], sys.argv[4]], start_new_session=True)
    pathlib.Path(sys.argv[5]).write_text(str(process.pid))
module.timer_control = schedule
config = pathlib.Path.home() / ".config/uu-remote-bridge/environment"
target = module.read_config(config).replace(b"1920x1080", b"2560x1440")
guard = module.arm_guard(config, target)
module.atomic_write(config, target)
pathlib.Path(sys.argv[6]).write_text(str(guard))
while True: time.sleep(1)
''')
        trigger, done, timer_pid, token = [self.home / name for name in ("trigger", "done", "timer-pid", "token")]
        process = subprocess.Popen(["/usr/bin/python3", str(caller), str(REPO / "scripts/uu-quality.py"),
                                    str(runner), str(trigger), str(done), str(timer_pid), str(token)],
                                   env=dict(os.environ, HOME=str(self.home)))
        try:
            deadline = time.monotonic() + 5
            while not token.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(token.is_file(), "isolated caller did not arm its recovery")
            self.assertIn(b"2560x1440", self.config.read_bytes())
            process.kill()
            self.assertLess(process.wait(timeout=3), 0)
            self.assertTrue(Path(f"/proc/{timer_pid.read_text().strip()}").exists())
            trigger.touch()
            deadline = time.monotonic() + 5
            while not done.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(done.is_file(), "independent recovery did not finish")
            self.assertEqual(self.config.read_bytes(), self.original)
            guard = Path(token.read_text())
            self.assertEqual(json.loads((guard / "record.json").read_text())["state"], "rolled-back")
            self.assertFalse((self.config.parent / "quality-guard-active").exists())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
            trigger.touch()
            if timer_pid.exists() and not done.exists():
                try:
                    os.kill(int(timer_pid.read_text()), 9)
                except ProcessLookupError:
                    pass

    def test_concurrent_timer_and_caller_recovery_restart_only_once(self):
        expected = self.original.replace(b"1920x1080", b"2560x1440")
        guard = quality.arm_guard(self.config, expected)
        quality.atomic_write(self.config, expected)
        restarting, release = threading.Event(), threading.Event()
        def restart(*args):
            restarting.set()
            self.assertTrue(release.wait(3), "isolated recovery was not released")
        with patch.object(quality, "systemctl", side_effect=restart) as command, ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(quality.rollback_guard, guard)
            self.assertTrue(restarting.wait(3))
            second = workers.submit(quality.rollback_guard, guard)
            try:
                self.assertFalse(second.done())
            finally:
                release.set()
            self.assertTrue(first.result(timeout=3))
            self.assertTrue(second.result(timeout=3))
        command.assert_called_once_with("restart", "uu-remote-bridge.service")
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_independent_retry_recovers_after_an_unready_restore_and_cleans_failed_timer_stop(self):
        expected = self.original.replace(b"1920x1080", b"2560x1440")
        guard = quality.arm_guard(self.config, expected)
        quality.atomic_write(self.config, expected)
        self.ready.side_effect = [RuntimeError("recovery unready"), None, None]
        self.timer.side_effect = [RuntimeError("stop timer failed"), None]
        with patch.object(quality, "systemctl") as restart:
            with self.assertRaisesRegex(RuntimeError, "recovery unready"):
                quality.rollback_guard(guard)
            self.assertEqual(json.loads((guard / "record.json").read_text())["state"], "restoring")
            with self.assertRaisesRegex(RuntimeError, "stop timer failed"):
                quality.rollback_guard(guard)
            self.assertTrue((self.config.parent / "quality-guard-active").is_file())
            self.assertTrue(quality.rollback_guard(guard))
        self.assertEqual(restart.call_count, 2)
        self.assertFalse((self.config.parent / "quality-guard-active").exists())
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_committed_guard_retries_timer_cleanup_without_restoring_or_restarting(self):
        expected = self.original.replace(b"1920x1080", b"2560x1440")
        guard = quality.arm_guard(self.config, expected)
        quality.atomic_write(self.config, expected)
        self.timer.side_effect = [RuntimeError("timer stop failed"), None, None]
        with self.assertRaisesRegex(RuntimeError, "timer stop failed"):
            quality.commit_guard(guard)
        active = self.config.parent / "quality-guard-active"
        self.assertTrue(active.exists())
        self.assertEqual(json.loads((guard / "record.json").read_text())["state"], "committed")
        with patch.object(quality, "systemctl") as restart:
            self.assertFalse(quality.rollback_guard(guard))
        restart.assert_not_called()
        self.assertEqual(self.config.read_bytes(), expected)
        self.assertFalse(active.exists())
        self.assertTrue(quality.arm_guard(self.config, expected).exists())

    def test_old_committed_callback_preserves_a_new_guard_and_external_configuration(self):
        expected = self.original.replace(b"1920x1080", b"2560x1440")
        guard = quality.arm_guard(self.config, expected)
        quality.atomic_write(self.config, expected)
        quality.commit_guard(guard)
        newer = quality.arm_guard(self.config, expected + b"# newer target\n")
        active = self.config.parent / "quality-guard-active"
        external = expected + b"# external modification\n"
        self.config.write_bytes(external)
        with patch.object(quality, "systemctl") as restart:
            self.assertFalse(quality.rollback_guard(guard))
        self.assertEqual(active.read_text().strip(), str(newer))
        self.assertEqual(self.config.read_bytes(), external)
        restart.assert_not_called()

    def test_sigkill_after_commit_record_leaves_cleanup_for_the_independent_callback(self):
        token, ready = self.home / "committed-token", self.home / "committed-ready"
        code = '''import importlib.util, pathlib, sys, time
spec=importlib.util.spec_from_file_location("isolated_quality",sys.argv[1])
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
module.wait_ready=lambda *a,**k: None
def timer(command):
    if command[0].endswith("systemctl"):
        pathlib.Path(sys.argv[3]).touch()
        while True: time.sleep(1)
module.timer_control=timer
config=pathlib.Path.home()/".config/uu-remote-bridge/environment"
target=module.read_config(config).replace(b"1920x1080",b"2560x1440")
guard=module.arm_guard(config,target)
pathlib.Path(sys.argv[2]).write_text(str(guard))
module.atomic_write(config,target)
module.commit_guard(guard)
'''
        process = subprocess.Popen(["/usr/bin/python3", "-c", code, str(REPO / "scripts/uu-quality.py"),
                                    str(token), str(ready)], env=dict(os.environ, HOME=str(self.home)))
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(ready.exists(), "isolated caller did not persist its commit")
            process.kill()
            self.assertLess(process.wait(timeout=3), 0)
            guard = Path(token.read_text())
            self.assertEqual(json.loads((guard / "record.json").read_text())["state"], "committed")
            with patch.object(quality, "systemctl") as restart:
                self.assertFalse(quality.rollback_guard(guard))
            restart.assert_not_called()
            self.assertFalse((self.config.parent / "quality-guard-active").exists())
            self.assertEqual(self.config.read_bytes(), self.original.replace(b"1920x1080", b"2560x1440"))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)

    def test_process_data_rejects_processes_that_left_the_own_service_cgroup(self):
        def read(path, *args, **kwargs):
            self.assertEqual(str(path), "/proc/12345/cgroup")
            return "0::/foreign/uu-remote-bridge.service\n"
        with patch.object(Path, "read_text", read), patch.object(Path, "read_bytes") as read_bytes:
            with self.assertRaisesRegex(ValueError, "left the bridge cgroup"):
                quality.process_data("12345", "/isolated/uu-remote-bridge.service")
        read_bytes.assert_not_called()

    def test_namespace_listener_fallback_requires_a_socket_owned_by_the_target_pid(self):
        code = "import socket,sys; s=socket.socket(); s.bind(('127.0.0.1',0)); s.listen(); print(s.getsockname()[1],flush=True); sys.stdin.readline()"
        process = subprocess.Popen(["/usr/bin/python3", "-c", code], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertTrue(select.select([process.stdout], [], [], 3)[0], "isolated listener did not start")
            port = int(process.stdout.readline().strip())
            with patch.object(quality, "user_command", return_value=subprocess.CompletedProcess([], 0, "", "")):
                self.assertFalse(quality.listener_ready(os.getpid(), port))
                self.assertTrue(quality.listener_ready(process.pid, port))
        finally:
            process.communicate("done\n", timeout=3)

    def health_fixture(self, relay="rdp", main="10", prefix="/custom/prefix", ipc=True, owner="13"):
        cgroup = "/isolated/uu-remote-bridge.service"
        display = {"DISPLAY": ":20", "WINEPREFIX": prefix}
        processes = {"10": (["bash", "uu-remote-bridge"], {}),
                     "11": (["C:\\UU\\GameViewerServer.exe"], dict(display))}
        if relay == "rdp":
            processes.update({"12": (["C:\\FreeRDP\\sdl-freerdp.exe", "/v:127.0.0.1:3390"], dict(display)),
                              "13": (["/usr/libexec/gnome-remote-desktop-daemon", "--rdp-port", "3390"], {})})
            port = 3390
        else:
            processes.update({"12": (["/usr/bin/vncviewer", "127.0.0.1:22"], {"DISPLAY": ":20"}),
                              "13": (["/usr/bin/x11vnc", "-rfbport", "5922"], {})})
            self.config.write_bytes(self.original + b"UURB_DESKTOP_RELAY=vnc\nUURB_DESKTOP_VNC_PORT=5922\n")
            port = 5922
        original_read = Path.read_text
        def read(path, *args, **kwargs):
            if str(path) == "/sys/fs/cgroup" + cgroup + "/cgroup.procs":
                return "\n".join(processes) + "\n"
            return original_read(path, *args, **kwargs)
        def process(pid, group=None):
            self.assertEqual(group, cgroup)
            return processes[pid]
        def command(args, **kwargs):
            if args[0] == "/usr/bin/systemctl":
                return subprocess.CompletedProcess(args, 0,
                    f"ActiveState=active\nMainPID={main}\nControlGroup={cgroup}\n", "")
            if args[0] == "/usr/bin/ss":
                self.assertEqual(kwargs["timeout"], 3)
                return subprocess.CompletedProcess(args, 0,
                    f'LISTEN 0 32 127.0.0.1:{port} 0.0.0.0:* users:(("fixture",pid={owner},fd=3))\n', "")
            self.assertEqual(args, [str(self.home / ".local/bin/uu-agent"), "status"])
            self.assertEqual(kwargs["timeout"], 6)
            return subprocess.CompletedProcess(args, 0, json.dumps({"success": ipc}), "")
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(Path, "read_text", read))
        stack.enter_context(patch.object(quality, "process_data", side_effect=process))
        stack.enter_context(patch.object(quality, "user_command", side_effect=command))
        self.addCleanup(stack.close)
        return processes, stack

    def test_ready_requires_own_cgroup_prefix_rdp_relay_listener_and_successful_ipc(self):
        processes, stack = self.health_fixture()
        self.assertTrue(quality.bridge_ready(self.config))
        for pid, change in (("11", {"WINEPREFIX": "/other-prefix"}),
                            ("12", {"WINEPREFIX": "/other-prefix"}),
                            ("12", {"DISPLAY": ":99"})):
            args, env = processes[pid]
            processes[pid] = (args, dict(env, **change))
            self.assertFalse(quality.bridge_ready(self.config))
            processes[pid] = (args, env)
        args, env = processes.pop("12")
        self.assertFalse(quality.bridge_ready(self.config))
        processes["12"] = (args, env)
        processes["13"][0][-1] = "3389"
        self.assertFalse(quality.bridge_ready(self.config))
        stack.close()

    def test_successful_ipc_alone_wrong_listener_or_missing_service_owner_is_not_ready(self):
        for parameters in ({"main": "99"}, {"owner": "99"}, {"ipc": False}):
            with self.subTest(parameters=parameters):
                _, stack = self.health_fixture(**parameters)
                self.assertFalse(quality.bridge_ready(self.config))
                stack.close()

    def test_vnc_ready_checks_matching_loopback_viewer_and_listener_without_grd(self):
        processes, stack = self.health_fixture(relay="vnc")
        self.assertTrue(quality.bridge_ready(self.config))
        processes["12"][0][-1] = "127.0.0.1:23"
        self.assertFalse(quality.bridge_ready(self.config))
        processes["12"][0][-1] = "127.0.0.1:22"
        processes["13"][0][-1] = "5923"
        self.assertFalse(quality.bridge_ready(self.config))
        stack.close()

    def test_wait_ready_has_a_deadline_even_when_ipc_times_out_or_json_is_invalid(self):
        for failure in (subprocess.TimeoutExpired("existing-ipc", 6), ValueError("invalid JSON")):
            with self.subTest(failure=failure), patch.object(quality, "bridge_ready", side_effect=failure), patch.object(
                quality.time, "monotonic", side_effect=[0, 0, 46]
            ), patch.object(quality.time, "sleep") as sleep:
                with self.assertRaisesRegex(RuntimeError, "not ready"):
                    WAIT_READY(self.config)
                sleep.assert_called_once_with(.5)

    def test_external_change_during_failed_restart_is_not_rolled_back(self):
        changed = self.original + b"UURB_NEW_SETTING=preserve\n"
        def fail_restart(*args):
            self.config.write_bytes(changed)
            raise RuntimeError("failed")
        with patch.object(quality, "systemctl", side_effect=fail_restart) as systemctl:
            result, _, error = self.run_main("apply", "1440p")
        self.assertEqual(result, 1)
        self.assertIn("externally", error)
        self.assertEqual(self.config.read_bytes(), changed)
        self.assertEqual(systemctl.call_count, 1)

    def test_compare_before_write_rejects_external_update(self):
        with patch.object(quality, "read_config", side_effect=[self.original, self.original + b"# newer\n"]), \
                patch.object(quality, "systemctl") as systemctl:
            result, _, error = self.run_main("apply", "1440p")
        self.assertEqual(result, 1)
        self.assertIn("changed", error)
        self.assertEqual(self.config.read_bytes(), self.original)
        systemctl.assert_not_called()

    def test_missing_resolution_appends_without_losing_final_line(self):
        self.config.write_bytes(b"UURB_CURSOR_SIZE=24")
        with patch.object(quality, "systemctl"):
            self.assertEqual(self.run_main("apply", "720p")[0], 0)
        self.assertEqual(self.config.read_bytes(), b"UURB_CURSOR_SIZE=24\nUURB_RESOLUTION=1280x720\n")

    def test_duplicate_resolution_entries_are_all_updated(self):
        self.config.write_bytes(self.original + b"UURB_RESOLUTION=1280x720\n")
        with patch.object(quality, "systemctl"):
            self.assertEqual(self.run_main("apply", "1440p")[0], 0)
        self.assertEqual(self.config.read_bytes().count(b"UURB_RESOLUTION=2560x1440"), 2)

    def test_status_does_not_reveal_unrelated_settings_or_claim_live_fps(self):
        result, output, _ = self.run_main("status")
        self.assertEqual(result, 0)
        self.assertIn("1920x1080", output)
        self.assertIn("未知", output)
        self.assertNotIn("do-not-display", output)
        self.assertNotIn("60", output)

    def test_bitrate_is_explicit_and_uses_only_validated_numeric_argument(self):
        for number in (0, 1, 500):
            response = ("Bitrate limit reset to default (no limit)\n" if number == 0 else
                        f"Bitrate limit set to {number} Mbps\n")
            completed = subprocess.CompletedProcess([], 0, response, "Wine diagnostic noise")
            with self.subTest(number=number), \
                    patch.object(quality.subprocess, "run", return_value=completed) as run:
                self.assertEqual(self.run_main("bitrate", str(number))[0], 0)
                run.assert_called_once_with([str(self.home / ".local/bin/uu-agent"), "cli",
                                             "--set-bitrate-limit", str(number)],
                                            capture_output=True, text=True)
        for number in (-1, 501):
            with self.assertRaises(argparse.ArgumentTypeError):
                quality.bitrate(str(number))

    def test_bitrate_requires_success_returncode_and_exact_numeric_confirmation(self):
        cases = ((0, '{"success":false}\n'), (0, ""),
                 (0, "Bitrate limit set to 21 Mbps\n"),
                 (0, "Bitrate limit reset to default (no limit)\n"),
                 (0, "ERROR\nBitrate limit set to 20 Mbps\n"),
                 (1, "Bitrate limit set to 20 Mbps\n"))
        for returncode, output in cases:
            with self.subTest(returncode=returncode, output=output), \
                    patch.object(quality.subprocess, "run", return_value=
                                 subprocess.CompletedProcess([], returncode, output, "private diagnostic")):
                result, stdout, stderr = self.run_main("bitrate", "20")
            self.assertEqual(result, 1)
            self.assertEqual(stdout, "")
            self.assertIn("did not confirm", stderr)
            self.assertNotIn("private diagnostic", stderr)

    def test_actual_positive_bitrate_protocol_accepts_twenty_without_fps_claim(self):
        with patch.object(quality.subprocess, "run", return_value=
                          subprocess.CompletedProcess([], 0, "Bitrate limit set to 20 Mbps\n", "")):
            result, output, _ = self.run_main("bitrate", "20")
        self.assertEqual(result, 0)
        self.assertIn("已接受", output)
        self.assertIn("不保证实际码率或帧率", output)

    def test_guide_has_real_native_menu_sources_and_bridge_limit(self):
        _, output, _ = self.run_main("guide")
        self.assertIn("【控制中心】→【画质】", output)
        self.assertIn("【操作】→【显示】", output)
        self.assertIn("720p、1080p、1440p 和 4K", output)
        self.assertNotIn("5K", output)
        self.assertIn("实际帧率未测定", output)
        self.assertNotIn("最高支持 4K", output)
        self.assertIn("uuyc.163.com/help/superscreen.html", output)

    def test_symlink_config_is_refused(self):
        self.config.unlink()
        target = self.home / "target"
        target.write_bytes(self.original)
        self.config.symlink_to(target)
        with patch.object(quality, "systemctl") as systemctl:
            self.assertEqual(self.run_main("apply", "720p")[0], 1)
        self.assertEqual(target.read_bytes(), self.original)
        systemctl.assert_not_called()

    def test_gui_has_four_presets_and_cancel_preserves_saved_4k(self):
        original_4k = self.original.replace(b"1920x1080", b"3840x2160")
        self.config.write_bytes(original_4k)
        with patch.dict(os.environ, DISPLAY=":fake"), \
                patch.object(quality.shutil, "which", return_value="/usr/bin/zenity"), \
                patch.object(quality.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "")) as run, \
                patch.object(quality, "systemctl") as systemctl:
            self.assertEqual(self.run_main("gui")[0], 0)
        dialog = run.call_args_list[-1].args[0]
        for profile in ("720p", "1080p", "1440p", "2160p"):
            self.assertIn(profile, dialog)
        self.assertNotIn("2880p", dialog)
        self.assertNotIn("5120x2880", dialog)
        self.assertEqual(self.config.read_bytes(), original_4k)
        systemctl.assert_not_called()

    def test_gui_help_cancel_returns_to_selection_without_modifying_configuration(self):
        results = [subprocess.CompletedProcess([], 0, "画质/帧率帮助\n"),
                   subprocess.CompletedProcess([], 1, ""),
                   subprocess.CompletedProcess([], 1, "")]
        with patch.dict(os.environ, DISPLAY=":fake"), \
                patch.object(quality.shutil, "which", return_value="/usr/bin/zenity"), \
                patch.object(quality.subprocess, "run", side_effect=results) as run, \
                patch.object(quality, "systemctl") as systemctl:
            self.assertEqual(self.run_main("gui")[0], 0)
        self.assertEqual(run.call_count, 3)
        self.assertIn("--title=UU远程 · 画质与分辨率", run.call_args_list[0].args[0])
        self.assertIn("--extra-button=画质/帧率帮助", run.call_args_list[0].args[0])
        self.assertIn("--info", run.call_args_list[1].args[0])
        self.assertIn("--list", run.call_args_list[2].args[0])
        self.assertEqual(self.config.read_bytes(), self.original)
        systemctl.assert_not_called()

    def test_gui_help_then_profile_applies_only_after_selection(self):
        results = [subprocess.CompletedProcess([], 0, "画质/帧率帮助\n"),
                   subprocess.CompletedProcess([], 0, ""),
                   subprocess.CompletedProcess([], 0, "1440p\n"),
                   subprocess.CompletedProcess([], 0, "")]
        with patch.dict(os.environ, DISPLAY=":fake"), \
                patch.object(quality.shutil, "which", return_value="/usr/bin/zenity"), \
                patch.object(quality.subprocess, "run", side_effect=results), \
                patch.object(quality, "systemctl") as systemctl:
            self.assertEqual(self.run_main("gui")[0], 0)
        self.assertEqual(self.config.read_bytes(), self.original.replace(b"1920x1080", b"2560x1440"))
        systemctl.assert_called_once_with("restart", "uu-remote-bridge.service")

    def test_wrapper_prefix_precedence_and_quality_argument_passthrough(self):
        helper = self.home / ".local/libexec/uu-quality.py"
        helper.parent.mkdir(parents=True)
        helper.write_text("import os,sys\nprint(os.environ['WINEPREFIX'])\nprint(repr(sys.argv[1:]))\n")
        env = dict(os.environ, HOME=str(self.home))
        env.pop("UURB_WINEPREFIX", None)
        env.pop("WINEPREFIX", None)
        cases = (({}, "/custom/prefix"), ({"WINEPREFIX": "/wine"}, "/wine"),
                 ({"WINEPREFIX": "/wine", "UURB_WINEPREFIX": "/uurb"}, "/uurb"))
        for overrides, expected in cases:
            result = subprocess.run(["bash", str(REPO / "scripts/uu-remote"), "quality", "apply", "720p"],
                                    env=dict(env, **overrides), capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), [expected, "['apply', '720p']"])


if __name__ == "__main__":
    unittest.main()
