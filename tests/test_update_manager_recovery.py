"""Updater recovery probes use temporary installations and mocked commands only."""
from pathlib import Path
from dataclasses import asdict, replace
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from uu_update_manager import Config, Manager, UpdateError, process_identity, process_environment


class UpdateRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name) / "home"
        self.home.mkdir()
        self.addCleanup(patch.stopall)
        patch.object(Path, "home", return_value=self.home).start()
        patch.dict(os.environ, {"WINEPREFIX": "", "UURB_WINEPREFIX": "", "XDG_STATE_HOME": str(self.home / ".local/state")}).start()
        patch("uu_update_manager.process_identity", return_value=("123", 45678, 45678, "S")).start()
        patch("uu_update_manager.process_environment", side_effect=lambda pid: {"WINEPREFIX": str(self.manager.wine_prefix())}).start()
        self.manager = Manager(Config(
            path=self.home / "config.json", repository=ROOT,
            state_dir=self.home / "updater", remote="origin", branch="main",
            track="known-good", endpoint="https://example.invalid/latest",
            codex_executable=Path("/nonexistent/codex"), codex_model="test",
            codex_reasoning_effort="medium", codex_timeout_seconds=300,
            codex_max_used_percent=20, idle_minutes=45,
            auto_reinstall_known_good=True, auto_promote_accepted_release=False,
            max_download_bytes=1024 * 1024,
        ))
        patch.object(self.manager, "stop_reinstall_installer").start()

    def settings(self, text):
        path = self.home / ".config/uu-remote-bridge/environment"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def manifest(self, prefix=None, version="test-version"):
        prefix = prefix or self.home / ".local/share/wineprefixes/uu-remote"
        path = prefix / "compat/release-manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": version}))
        return path

    def test_install_timeout_restores_previous_files_and_stops_children(self):
        original = self.home / "runtime-file"
        original.write_text("working runtime")
        checkout = self.home / "checkout"
        checkout.mkdir()
        patch.object(self.manager, "track_checkout", return_value=checkout).start()
        patch.object(self.manager, "runtime_snapshot_paths", return_value=[original]).start()
        patch.object(self.manager, "health", return_value={"healthy": True, "issues": []}).start()
        patch("uu_update_manager.command_output", return_value=subprocess.CompletedProcess([], 0, "secret", "")).start()
        timeout = subprocess.TimeoutExpired("install.sh", 1800)
        process = Mock(pid=45678)
        process.wait.side_effect = [timeout, -signal.SIGKILL]

        def mutate(*args, **kwargs):
            original.write_text("partially replaced runtime")
            raise timeout

        def spawn(*args, **kwargs):
            original.write_text("partially replaced runtime")
            return process

        with patch("uu_update_manager.subprocess.run", side_effect=mutate), patch(
            "uu_update_manager.subprocess.Popen", side_effect=spawn
        ) as popen:
            result = self.manager.reinstall_known_good()
        self.assertEqual(original.read_text(), "working runtime")
        self.assertTrue(result["rolled_back"])
        self.assertEqual(result["returncode"], 124)
        self.assertTrue(result["health"]["healthy"])
        self.assertIn("timed out", result["error"])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.manager.stop_reinstall_installer.assert_called()
        self.assertEqual(self.manager.stop_reinstall_installer.call_args.args[0]["installer_pid"], process.pid)
        self.assertFalse(self.manager.promotion_marker_path.exists())

    def test_install_failures_restore_runtime_and_success_keeps_new_files(self):
        for case in ("spawn-error", "nonzero", "health-error", "unhealthy", "success"):
            with self.subTest(case=case):
                original = self.home / "runtime-file"
                original.write_text("working runtime")
                checkout = self.home / "checkout"
                checkout.mkdir(exist_ok=True)
                process = Mock()
                process.pid = 45678
                process.wait.return_value = 1 if case == "nonzero" else 0

                def spawn(*args, **kwargs):
                    original.write_text("new runtime")
                    if case == "spawn-error":
                        raise OSError("cannot execute installer")
                    return process

                health = {"healthy": True, "issues": []}
                health_results = ([OSError("probe failed"), health] if case == "health-error"
                                  else [{"healthy": False, "issues": ["missing"]}, health]
                                  if case == "unhealthy" else [health, health])
                with patch.object(self.manager, "track_checkout", return_value=checkout), patch.object(
                    self.manager, "runtime_snapshot_paths", return_value=[original]
                ), patch.object(self.manager, "health", side_effect=health_results), patch(
                    "uu_update_manager.command_output", return_value=subprocess.CompletedProcess([], 0, "secret", "")
                ), patch("uu_update_manager.subprocess.Popen", side_effect=spawn) as popen:
                    result = self.manager.reinstall_known_good()
                self.assertEqual(result["rolled_back"], case != "success")
                self.assertEqual(original.read_text(), "new runtime" if case == "success" else "working runtime")
                self.assertEqual(popen.call_args.kwargs["env"]["WINEPREFIX"], str(self.manager.wine_prefix()))

    def test_unconfirmed_installer_termination_keeps_snapshot_and_blocks_restore(self):
        checkout = self.home / "checkout"
        checkout.mkdir()
        process = Mock(pid=45678)
        process.wait.side_effect = subprocess.TimeoutExpired("install.sh", 1800)
        with patch.object(self.manager, "track_checkout", return_value=checkout), patch.object(
            self.manager, "snapshot_live_runtime", return_value=self.home / "snapshot"
        ), patch.object(self.manager, "restore_live_runtime") as restore, patch(
            "uu_update_manager.command_output", return_value=subprocess.CompletedProcess([], 0, "secret", "")
        ), patch("uu_update_manager.subprocess.Popen", return_value=process), patch.object(
            self.manager, "stop_reinstall_installer", side_effect=PermissionError("cannot terminate installer")
        ):
            with self.assertRaisesRegex(UpdateError, "termination is unconfirmed.*snapshot"):
                self.manager.reinstall_known_good()
        restore.assert_not_called()

    def test_unhealthy_reinstall_rollback_preserves_its_durable_recovery_marker(self):
        original = self.home / "runtime-file"
        original.write_text("working runtime")
        checkout = self.home / "checkout"
        checkout.mkdir()
        process = Mock(pid=45678)
        process.wait.return_value = 1
        def spawn(*args, **kwargs):
            original.write_text("partial install")
            return process
        with patch.object(self.manager, "track_checkout", return_value=checkout), patch.object(
            self.manager, "runtime_snapshot_paths", return_value=[original]
        ), patch.object(self.manager, "health", return_value={"healthy": False, "issues": ["missing"]}), patch(
            "uu_update_manager.command_output", return_value=subprocess.CompletedProcess([], 0, "secret", "")
        ), patch("uu_update_manager.subprocess.Popen", side_effect=spawn), patch("uu_update_manager.time.sleep"):
            with self.assertRaisesRegex(UpdateError, "not healthy.*retained marker"):
                self.manager.reinstall_known_good()
        self.assertEqual(original.read_text(), "working runtime")
        self.assertTrue(self.manager.promotion_marker_path.is_file())
        marker = json.loads(self.manager.promotion_marker_path.read_text())
        self.assertTrue((Path(marker["work_dir"]) / "snapshot/manifest.json").is_file())

    def health_responses(self, listener_pid=103, viewer_present=True):
        def output(command, **kwargs):
            if "show" in command:
                return subprocess.CompletedProcess(command, 0, "ActiveState=active\nNRestarts=0\n", "")
            if command[0] == "ss":
                return subprocess.CompletedProcess(command, 0,
                    f'LISTEN 0 32 127.0.0.1:5922 0.0.0.0:* users:(("x11vnc",pid={listener_pid},fd=8))\n', "")
            pattern = command[-1]
            if "GameViewerServer" in pattern:
                return subprocess.CompletedProcess(command, 0, "101\n", "")
            if "x11vnc" in pattern:
                return subprocess.CompletedProcess(command, 0, "103\n", "")
            if "vncviewer" in pattern and viewer_present:
                return subprocess.CompletedProcess(command, 0, "104\n", "")
            return subprocess.CompletedProcess(command, 1, "", "")
        return output

    def vnc_health(self, shared=False, **kwargs):
        self.settings("UURB_DESKTOP_RELAY=vnc\n" + ("UURB_DESKTOP_VNC_PORT=5922\n" if shared else ""))
        self.manifest()
        state = self.home / ".local/state/uu-remote-bridge"
        state.mkdir(parents=True)
        (state / "desktop-x11vnc.log").write_text("PORT=5922\n")
        with patch("uu_update_manager.command_output", side_effect=self.health_responses(**kwargs)) as output:
            health = self.manager.health()
        for call in output.call_args_list:
            self.assertNotIn("sdl-freerdp", " ".join(call.args[0]))
            self.assertNotIn("gnome-remote-desktop", " ".join(call.args[0]))
        return health

    def test_vnc_health_accepts_the_owned_loopback_transport(self):
        self.assertTrue(self.vnc_health()["healthy"])

    def test_vnc_health_accepts_independently_managed_shared_port(self):
        self.assertTrue(self.vnc_health(shared=True)["healthy"])

    def test_vnc_health_rejects_unrelated_listener(self):
        self.assertIn("vnc-listener-owner-mismatch", self.vnc_health(listener_pid=99)["issues"])

    def test_vnc_health_rejects_missing_viewer(self):
        self.assertIn("vnc-relay-missing", self.vnc_health(viewer_present=False)["issues"])

    def test_manifest_and_snapshot_use_saved_prefix_and_environment_override(self):
        saved = self.home / "saved-prefix"
        override = self.home / "override-prefix"
        self.settings(f"UURB_WINEPREFIX={saved}\n")
        self.manifest(saved, "saved")
        self.manifest(override, "override")
        self.assertEqual(self.manager.installed_release()["version"], "saved")
        self.assertIn(saved / "compat", self.manager.runtime_snapshot_paths())
        with patch.dict(os.environ, {"UURB_WINEPREFIX": str(override)}):
            self.assertEqual(self.manager.installed_release()["version"], "override")
            self.assertIn(override / "compat", self.manager.runtime_snapshot_paths())
        with patch.dict(os.environ, {"WINEPREFIX": str(override)}):
            self.assertEqual(self.manager.installed_release()["version"], "override")
        with patch.dict(os.environ, {"WINEPREFIX": str(saved), "UURB_WINEPREFIX": str(override)}):
            self.assertEqual(self.manager.installed_release()["version"], "override")

    def test_outside_home_prefix_fails_before_live_mutation(self):
        outside = Path(self.directory.name) / "external-prefix"
        self.settings(f"UURB_WINEPREFIX={outside}\n")
        with self.assertRaisesRegex(UpdateError, "outside.*home"):
            self.manager.snapshot_live_runtime(self.home / "snapshot-work")

    def test_memory_budget_snapshot_restores_only_exact_managed_dropin(self):
        directory = self.home / ".config/systemd/user/uu-remote-bridge.service.d"
        directory.mkdir(parents=True)
        managed = directory / "10-uu-remote-memory-budget.conf"
        custom = directory / "user-policy.conf"
        managed.write_text("previous managed budget")
        custom.write_text("previous user policy")
        paths = self.manager.runtime_snapshot_paths()
        self.assertIn(managed, paths)
        self.assertNotIn(directory, paths)
        self.assertNotIn(custom, paths)
        snapshot = self.manager.snapshot_live_runtime(self.home / "budget-snapshot")
        managed.write_text("new managed budget")
        custom.write_text("updated user policy")
        with patch("uu_update_manager.command_output", return_value=subprocess.CompletedProcess([], 0)), \
                patch.object(self.manager, "health", return_value={"healthy": True}):
            restored = self.manager.restore_live_runtime(snapshot)
        self.assertTrue(restored["health"]["healthy"])
        self.assertEqual(managed.read_text(), "previous managed budget")
        self.assertEqual(custom.read_text(), "updated user policy")

    def test_terminal_proxy_mirrors_restore_together_in_saved_custom_prefix(self):
        prefix = self.home / "custom prefix"
        self.settings(f"UURB_WINEPREFIX={prefix}\n")
        mirrors = [prefix / "compat/uu-terminal-proxy.exe",
                   prefix / "drive_c/Program Files/Netease/GameViewer/bin/powershell.exe"]
        for path in mirrors:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"old terminal proxy")
        snapshot = self.manager.snapshot_live_runtime(self.home / "terminal-snapshot")
        for path in mirrors:
            path.write_bytes(b"new terminal proxy")
        with patch("uu_update_manager.command_output", return_value=subprocess.CompletedProcess([], 0)), \
                patch.object(self.manager, "health", return_value={"healthy": True}):
            result = self.manager.restore_live_runtime(snapshot)
        self.assertTrue(result["health"]["healthy"])
        for path in mirrors:
            self.assertEqual(path.read_bytes(), b"old terminal proxy")

    def test_recovery_after_maintenance_death_restores_files_and_checks_process_identity(self):
        original = self.home / "runtime-file"
        original.write_text("working runtime")
        work = self.manager.state_dir / "reinstalls/test-interruption"
        patch.object(self.manager, "runtime_snapshot_paths", return_value=[original]).start()
        self.manager.snapshot_live_runtime(work)
        marker = {"schema_version": 1, "kind": "known-good-reinstall", "phase": "installing",
                  "work_dir": str(work), "prefix": str(self.manager.wine_prefix()),
                  "installer_pid": 45678, "installer_starttime": "123", "nonce": "a" * 64}
        for reused in (False, True):
            with self.subTest(pid_reused=reused):
                original.write_text("partial install")
                self.manager.promotion_marker_path.write_text(json.dumps(marker))
                with patch("uu_update_manager.command_output", return_value=subprocess.CompletedProcess([], 0, "", "")), patch.object(
                    self.manager, "health", return_value={"healthy": True, "issues": []}
                ):
                    self.assertTrue(self.manager.recover_interrupted_promotion())
                self.assertEqual(original.read_text(), "working runtime")
                self.assertFalse(self.manager.promotion_marker_path.exists())
                self.manager.stop_reinstall_installer.assert_called_with(marker)

    def test_failed_service_stop_preserves_live_files_snapshot_and_pending_marker(self):
        original = self.home / "runtime-file"
        original.write_text("working runtime")
        work = self.manager.state_dir / "reinstalls/failed-stop"
        patch.object(self.manager, "runtime_snapshot_paths", return_value=[original]).start()
        self.manager.snapshot_live_runtime(work)
        original.write_text("partial install")
        self.manager.promotion_marker_path.write_text(json.dumps({
            "schema_version": 1, "kind": "known-good-reinstall", "phase": "snapshot-ready",
            "work_dir": str(work), "prefix": str(self.manager.wine_prefix()),
        }))
        with patch("uu_update_manager.command_output", return_value=subprocess.CompletedProcess([], 1, "", "stop refused")):
            with self.assertRaisesRegex(UpdateError, "could not stop.*snapshot retained"):
                self.manager.recover_interrupted_promotion()
        self.assertEqual(original.read_text(), "partial install")
        self.assertTrue((work / "snapshot/manifest.json").is_file())
        self.assertTrue(self.manager.promotion_marker_path.is_file())

    def test_health_ignores_server_and_freerdp_from_other_prefixes(self):
        self.manifest()
        def command(command, **kwargs):
            if "show" in command:
                return subprocess.CompletedProcess(command, 0, "ActiveState=active\nNRestarts=0\n", "")
            return subprocess.CompletedProcess(command, 0, "101\n", "")
        with patch("uu_update_manager.command_output", side_effect=command), patch(
            "uu_update_manager.process_environment", return_value={"WINEPREFIX": "/other/prefix"}
        ):
            health = self.manager.health()
        self.assertIn("uu-server-missing", health["issues"])
        self.assertIn("freerdp-relay-missing", health["issues"])

    def test_unhealthy_reinstall_recovery_keeps_marker_snapshot_and_does_not_claim_success(self):
        original = self.home / "runtime-file"
        original.write_text("working runtime")
        work = self.manager.state_dir / "reinstalls/unready-recovery"
        with patch.object(self.manager, "runtime_snapshot_paths", return_value=[original]):
            self.manager.snapshot_live_runtime(work)
            original.write_text("partial install")
            marker = {"schema_version": 1, "kind": "known-good-reinstall", "phase": "snapshot-ready",
                      "work_dir": str(work), "prefix": str(self.manager.wine_prefix())}
            self.manager.promotion_marker_path.write_text(json.dumps(marker))
            with patch("uu_update_manager.command_output", return_value=subprocess.CompletedProcess([], 0, "", "")), patch.object(
                self.manager, "health", return_value={"healthy": False, "issues": ["uu-server-missing"]}
            ), patch.object(self.manager, "write_status") as status, patch("uu_update_manager.time.sleep"):
                with self.assertRaisesRegex(UpdateError, "not healthy.*retained marker"):
                    self.manager.recover_interrupted_promotion()
            status.assert_not_called()
        self.assertEqual(original.read_text(), "working runtime")
        self.assertTrue((work / "snapshot/manifest.json").is_file())
        self.assertEqual(json.loads(self.manager.promotion_marker_path.read_text()), marker)

    def test_promotion_recovery_uses_the_configured_prefix(self):
        prefix = self.home / "custom-prefix"
        self.settings(f"UURB_WINEPREFIX={prefix}\n")
        task_dir = self.manager.tasks_dir / "custom-promotion"
        repo = task_dir / "repo"
        (repo / "scripts").mkdir(parents=True)
        (repo / "scripts/promote-approved-release.py").write_text("fixture only")
        task = {"id": "custom-promotion", "promotion_repo": str(repo)}
        self.manager.save_task(task)
        self.manager.promotion_marker_path.write_text(json.dumps({
            "schema_version": 1, "work_dir": str(task_dir / "promotion"), "prefix": str(prefix),
        }))
        with patch("uu_update_manager.command_output", return_value=subprocess.CompletedProcess([], 0, '{"rolled_back": true}\n', "")) as output:
            self.assertTrue(self.manager.recover_interrupted_promotion())
        arguments = output.call_args.args[0]
        self.assertEqual(arguments[arguments.index("--prefix") + 1], str(prefix))

    def idle_fixture(self, version, suffix=None, recent=False):
        prefix = self.home / "custom-prefix"
        self.settings(f"UURB_WINEPREFIX={prefix}\n")
        self.manifest(prefix, version)
        logs = prefix / "drive_c/Program Files/Netease/GameViewer/log/server/log"
        logs.mkdir(parents=True, exist_ok=True)
        for previous in logs.iterdir():
            previous.unlink()
        if suffix:
            log = logs / f"server_current{suffix}"
            log.write_bytes(b"opaque data; no evidence of a live inbound state")
            stamp = time.time() - (60 if recent else 3600)
            os.utime(log, (stamp, stamp))
        self.manager.config = replace(self.manager.config, auto_promote_accepted_release=True)
        task = {"id": "idle-fixture", "kind": "approved-promotion", "phase": "promotion-queued"}
        return task

    def test_current_or_unknown_state_never_becomes_idle_from_stale_or_missing_logs(self):
        for version, suffix in (("4.42.0.2770", ".slog"), ("4.42.0.2770", ".txt"),
                                ("4.42.0.2770", None), ("unknown", ".txt")):
            with self.subTest(version=version, suffix=suffix):
                task = self.idle_fixture(version, suffix)
                with patch.object(self.manager, "promotion_paths", side_effect=AssertionError("must remain held")):
                    self.manager.run_promotion(task)
                self.assertEqual(task["phase"], "promotion-waiting-idle")
                self.assertEqual(task["idle_block_reason"], "unsupported-live-inbound-state")
                if suffix:
                    logs = self.manager.wine_prefix() / "drive_c/Program Files/Netease/GameViewer/log/server/log"
                    for log in logs.iterdir():
                        log.unlink()

    def test_custom_prefix_recent_slog_vetoes_promotion(self):
        task = self.idle_fixture("4.42.0.2770", ".slog", recent=True)
        with patch.object(self.manager, "promotion_paths", side_effect=AssertionError("active must remain held")):
            self.manager.run_promotion(task)
        self.assertEqual(task["idle_block_reason"], "recent-activity")
        self.assertLess(task["quiet_seconds"], 120)

    def test_known_legacy_idle_txt_rule_still_passes_in_a_custom_prefix(self):
        task = self.idle_fixture("4.33.0.8907", ".txt")
        with patch.object(self.manager, "promotion_paths", side_effect=AssertionError("passed legacy idle gate")):
            with self.assertRaisesRegex(AssertionError, "passed legacy idle gate"):
                self.manager.run_promotion(task)

    def test_quality_helper_is_restored_with_the_installed_runtime(self):
        helper = self.home / ".local/libexec/uu-quality.py"
        helper.parent.mkdir(parents=True)
        helper.write_text("working quality helper")
        snapshot = self.manager.snapshot_live_runtime(self.manager.state_dir / "reinstalls/quality-fixture")
        helper.write_text("partial helper update")
        with patch("uu_update_manager.command_output", return_value=subprocess.CompletedProcess([], 0, "", "")), patch.object(
            self.manager, "health", return_value={"healthy": True, "issues": []}
        ):
            self.manager.restore_live_runtime(snapshot)
        self.assertEqual(helper.read_text(), "working quality helper")

    def test_desktop_tools_and_user_overrides_are_restored_with_the_runtime(self):
        paths = [self.home / ".local/libexec/uu-desktop-tool.py",
                 self.home / ".local/share/uu-remote/tools/uu-tools-fonts.conf",
                 self.home / ".local/share/uu-remote/tools/original-launchers/obconf.desktop"]
        paths += [self.home / f".local/share/applications/{name}.desktop"
                  for name in ("xtigervncviewer", "xfreerdp", "obconf", "x11vnc")]
        for path in paths:
            self.assertTrue(any(path == owned or owned in path.parents
                                for owned in self.manager.runtime_snapshot_paths()))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("previous user file")
        snapshot = self.manager.snapshot_live_runtime(self.manager.state_dir / "reinstalls/desktop-fixture")
        for path in paths:
            path.write_text("partial install")
        with patch("uu_update_manager.command_output", return_value=subprocess.CompletedProcess([], 0, "", "")), patch.object(
            self.manager, "health", return_value={"healthy": True, "issues": []}
        ):
            self.manager.restore_live_runtime(snapshot)
        for path in paths:
            self.assertEqual(path.read_text(), "previous user file")

    def test_manual_promotion_passes_configured_prefix_to_the_transaction(self):
        task = self.idle_fixture("4.42.0.2770", ".slog")
        task_dir = self.manager.tasks_dir / task["id"]
        repo = task_dir / "repo"
        repo.mkdir(parents=True)
        work = task_dir / "promotion"
        work.mkdir()
        (work / "result.json").write_text('{"status": "promoted"}')
        task.update({"source_commit": "a" * 40, "details": {
            "release": {"version": "4.42.0.2770"}, "installer": str(repo / "installer.exe")}})
        with patch.object(self.manager, "promotion_paths", return_value=(task_dir, repo, repo / "helper.py", repo / "manifest.json", work)), patch(
            "uu_update_manager.command_output", return_value=subprocess.CompletedProcess([], 0, "", "")
        ) as output:
            self.manager.run_promotion(task, ignore_idle=True)
        args = output.call_args.args[0]
        self.assertEqual(args[args.index("--prefix") + 1], str(self.manager.wine_prefix()))
        self.assertIn("operator_idle_override_at", task)

    def test_installer_descendants_are_checked_after_leader_exit_and_pid_reuse_is_safe(self):
        marker = {"prefix": str(self.manager.wine_prefix()), "installer_pid": 45678,
                  "installer_starttime": "123", "nonce": "a" * 64}
        for case in ("leader-live", "leader-dead", "pid-reused", "unowned-child", "leader-dead-unowned", "pid-reused-tagged-wrong-prefix"):
            with self.subTest(case=case):
                identities = {
                    45678: ("456" if case.startswith("pid-reused") else "123", 45678, 45678, "S"),
                    45679: ("124", 45678, 45678, "S"),
                }
                if case.startswith("leader-dead"):
                    identities[45678] = None
                environments = {pid: {"WINEPREFIX": marker["prefix"], "UURB_REINSTALL_TRANSACTION": marker["nonce"]}
                                for pid in identities}
                if case.startswith("pid-reused"):
                    environments = {pid: {} for pid in identities}
                if case == "pid-reused-tagged-wrong-prefix":
                    environments[45679] = {"WINEPREFIX": "/wrong/prefix", "UURB_REINSTALL_TRANSACTION": marker["nonce"]}
                    identities[45679] = ("124", 45679, 45679, "S")
                if case in ("unowned-child", "leader-dead-unowned"):
                    environments[45679] = {}

                def kill(descriptor, sig):
                    self.assertEqual(sig, signal.SIGKILL)
                    identities[descriptor] = None

                with patch.object(Path, "iterdir", side_effect=lambda: iter([Path("/proc/45678"), Path("/proc/45679")])), patch(
                    "uu_update_manager.process_identity", side_effect=lambda pid: identities.get(pid)
                ), patch("uu_update_manager.process_environment", side_effect=lambda pid: environments.get(pid, {})), patch(
                    "uu_update_manager.os.pidfd_open", side_effect=lambda pid, flags: pid, create=True
                ), patch("uu_update_manager.signal.pidfd_send_signal", side_effect=kill) as send, patch(
                    "uu_update_manager.os.close"
                ), patch("uu_update_manager.time.sleep"):
                    if case in ("unowned-child", "leader-dead-unowned", "pid-reused-tagged-wrong-prefix"):
                        with self.assertRaisesRegex(UpdateError, "unconfirmed ownership"):
                            Manager.stop_reinstall_installer(self.manager, marker)
                    else:
                        Manager.stop_reinstall_installer(self.manager, marker)
                if case in ("pid-reused", "unowned-child", "leader-dead-unowned", "pid-reused-tagged-wrong-prefix"):
                    send.assert_not_called()
                else:
                    self.assertIn(45679, [call.args[0] for call in send.call_args_list])

    @unittest.skipUnless(hasattr(os, "pidfd_open"), "native pidfd probe requires the system Python")
    def test_nonzero_installer_cannot_leave_a_child_overwriting_the_restored_runtime(self):
        original = self.home / "runtime-file"
        original.write_text("working runtime")
        checkout = self.home / "checkout"
        checkout.mkdir()
        ready = self.home / "child.pid"
        (checkout / "install.sh").write_text('''#!/usr/bin/python3
import os, time
from pathlib import Path
if os.fork() == 0:
    os.setsid()
    Path(os.environ["UURB_TEST_CHILD_PID"]).write_text(str(os.getpid()))
    while True:
        Path(os.environ["UURB_TEST_RUNTIME"]).write_text("child-still-writing")
        time.sleep(0.02)
while not Path(os.environ["UURB_TEST_CHILD_PID"]).exists():
    time.sleep(0.01)
os._exit(1)
''')
        (checkout / "install.sh").chmod(0o700)
        try:
            with patch.object(self.manager, "track_checkout", return_value=checkout), patch.object(
                self.manager, "runtime_snapshot_paths", return_value=[original]
            ), patch.object(self.manager, "health", return_value={"healthy": True, "issues": []}), patch(
                "uu_update_manager.command_output", return_value=subprocess.CompletedProcess([], 0, "secret", "")
            ), patch("uu_update_manager.process_identity", side_effect=process_identity), patch(
                "uu_update_manager.process_environment", side_effect=process_environment
            ), patch.object(self.manager, "stop_reinstall_installer", side_effect=lambda marker: Manager.stop_reinstall_installer(self.manager, marker)), patch.dict(
                os.environ, {"UURB_TEST_CHILD_PID": str(ready), "UURB_TEST_RUNTIME": str(original)}
            ):
                result = self.manager.reinstall_known_good()
            self.assertTrue(result["rolled_back"])
            time.sleep(0.1)
            self.assertEqual(original.read_text(), "working runtime")
            identity = process_identity(int(ready.read_text()))
            self.assertTrue(identity is None or identity[3] == "Z")
        finally:
            if ready.is_file():
                pid = int(ready.read_text())
                identity = process_identity(pid)
                if identity is not None and identity[3] != "Z":
                    descriptor = os.pidfd_open(pid, 0)
                    try:
                        signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                    finally:
                        os.close(descriptor)

    @unittest.skipUnless(hasattr(os, "pidfd_open"), "native pidfd probe requires the system Python")
    def test_sigkill_of_maintenance_leaves_a_durable_recoverable_snapshot(self):
        original = self.home / "runtime-file"
        original.write_text("working runtime")
        checkout = self.home / "checkout"
        checkout.mkdir()
        ready = self.home / "installer.pid"
        (checkout / "install.sh").write_text('''#!/usr/bin/python3
import os, time
from pathlib import Path
Path(os.environ["UURB_TEST_CHILD_PID"]).write_text(str(os.getpid()))
while True:
    Path(os.environ["UURB_TEST_RUNTIME"]).write_text("partial install")
    time.sleep(0.02)
''')
        (checkout / "install.sh").chmod(0o700)
        code = '''
import json, sys, subprocess
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import uu_update_manager as updater
config = json.loads(sys.argv[2])
for key in ("path", "repository", "state_dir", "codex_executable"):
    config[key] = Path(config[key])
manager = updater.Manager(updater.Config(**config))
manager.track_checkout = lambda destination: Path(sys.argv[3])
manager.runtime_snapshot_paths = lambda: [Path(sys.argv[4])]
manager.health = lambda: {"healthy": True, "issues": []}
updater.command_output = lambda *args, **kwargs: subprocess.CompletedProcess([], 0, "secret", "")
manager.reinstall_known_good()
'''
        config = {key: str(value) if isinstance(value, Path) else value for key, value in asdict(self.manager.config).items()}
        maintenance = subprocess.Popen(
            ["/usr/bin/python3", "-c", code, str(ROOT / "scripts"), json.dumps(config), str(checkout), str(original)],
            env={**os.environ, "HOME": str(self.home), "UURB_TEST_CHILD_PID": str(ready), "UURB_TEST_RUNTIME": str(original)},
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 5
            while not ready.is_file() and time.monotonic() < deadline and maintenance.poll() is None:
                time.sleep(0.01)
            self.assertTrue(ready.is_file(), "isolated installer did not start")
            maintenance.kill()
            maintenance.wait(timeout=5)
            self.assertTrue(self.manager.promotion_marker_path.is_file())
            with patch("uu_update_manager.command_output", return_value=subprocess.CompletedProcess([], 0, "", "")), patch.object(
                self.manager, "health", return_value={"healthy": True, "issues": []}
            ), patch.object(self.manager, "runtime_snapshot_paths", return_value=[original]
            ), patch("uu_update_manager.process_identity", side_effect=process_identity), patch(
                "uu_update_manager.process_environment", side_effect=process_environment
            ), patch.object(self.manager, "stop_reinstall_installer", side_effect=lambda marker: Manager.stop_reinstall_installer(self.manager, marker)):
                self.assertTrue(self.manager.recover_interrupted_promotion())
            time.sleep(0.1)
            self.assertEqual(original.read_text(), "working runtime")
            self.assertFalse(self.manager.promotion_marker_path.exists())
        finally:
            if maintenance.poll() is None:
                maintenance.kill()
                maintenance.wait(timeout=5)
            if maintenance.stderr is not None:
                maintenance.stderr.close()
            if ready.is_file():
                pid = int(ready.read_text())
                identity = process_identity(pid)
                if identity is not None and identity[3] != "Z":
                    descriptor = os.pidfd_open(pid, 0)
                    try:
                        signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                    finally:
                        os.close(descriptor)

    def test_malformed_snapshots_are_fully_rejected_before_any_live_file_is_removed(self):
        first = self.home / "first"
        second = self.home / "second"
        sentinel = self.home / "HOME-SENTINEL"
        sentinel.write_text("must survive")
        cases = ({}, {"relative": "", "existed": True}, {"relative": ".", "existed": True},
                 {"relative": "../outside", "existed": True}, {"relative": str(first), "existed": True},
                 {"relative": "unmanaged", "existed": True}, {"relative": "second", "existed": "yes"},
                 "duplicate", "partial", "empty", "missing-file", "source-symlink", "files-symlink")
        with patch.object(self.manager, "runtime_snapshot_paths", return_value=[first, second]):
            for index, case in enumerate(cases):
                with self.subTest(case=case):
                    first.write_text("original first")
                    second.write_text("original second")
                    snapshot = self.manager.snapshot_live_runtime(self.home / f"snapshot-{index}")
                    first.write_text("current first")
                    second.write_text("current second")
                    manifest = json.loads((snapshot / "manifest.json").read_text())
                    if isinstance(case, dict):
                        manifest["entries"][1] = case
                    elif case == "duplicate":
                        manifest["entries"].append(manifest["entries"][0])
                    elif case == "partial":
                        manifest["entries"].pop()
                    elif case == "empty":
                        manifest["entries"] = []
                    elif case == "missing-file":
                        (snapshot / "files/second").unlink()
                    elif case == "source-symlink":
                        (snapshot / "files/second").unlink()
                        (snapshot / "files/second").symlink_to(sentinel)
                    elif case == "files-symlink":
                        (snapshot / "files").rename(snapshot / "real-files")
                        (snapshot / "files").symlink_to(self.home, target_is_directory=True)
                    (snapshot / "manifest.json").write_text(json.dumps(manifest))
                    with patch("uu_update_manager.command_output") as commands:
                        with self.assertRaises(UpdateError):
                            self.manager.restore_live_runtime(snapshot)
                    commands.assert_not_called()
                    self.assertEqual(first.read_text(), "current first")
                    self.assertEqual(second.read_text(), "current second")
                    self.assertEqual(sentinel.read_text(), "must survive")


if __name__ == "__main__":
    unittest.main()
