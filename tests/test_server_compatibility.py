"""Isolated tests for prefix selection and cursor reader reinitialization."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


REPOSITORY = Path(__file__).resolve().parents[1]
SOURCE = (REPOSITORY / "scripts/uu-remote-bridge").read_text()
INITIALIZER = SOURCE[SOURCE.index("preserve_cursor_guard_failure() {"):
                     SOURCE.index('\nif [[ "$cursor_guard_setting" == on ]]; then',
                                  SOURCE.index("initialize_server_compatibility() {"))]
SELECTOR = SOURCE[SOURCE.index("wine_prefix_process_pid() {"):
                  SOURCE.index("wine_process_identity() {")]
IDENTITY = SOURCE[SOURCE.index("wine_process_identity() {"):
                  SOURCE.index("refresh_manager_environment() {")]
SUPERVISOR = SOURCE[SOURCE.index("supervise_server() {"):
                    SOURCE.index('\nsupervise_server "$server_pid" &')]


class ServerCompatibilityTests(unittest.TestCase):
    def test_process_identity_rejects_zombies_dead_and_invalid_starttimes(self):
        for state, starttime, expected in (("S", "100", 0), ("Z", "100", 1),
                                          ("X", "100", 1), ("x", "100", 1),
                                          ("S", "invalid", 1)):
            with self.subTest(state=state, starttime=starttime), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                (root / "123").mkdir()
                fields = [state] + ["0"] * 18 + [starttime]
                (root / "123/stat").write_text("123 (name with ) spaces) " + " ".join(fields) + "\n")
                helper = IDENTITY.replace('"/proc/$pid/stat"', '"$IDENTITY_TEST_ROOT/$pid/stat"')
                script = 'set -Eeuo pipefail\nkill() { return 0; }\n' + helper
                script += '\nwine_process_identity 123\nprintf "%s\\n" "$wine_process_starttime"\n'
                result = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                                        env=dict(os.environ, IDENTITY_TEST_ROOT=temp))
                self.assertEqual(result.returncode, expected, result.stderr)
                if not expected:
                    self.assertEqual(result.stdout.strip(), starttime)

    def test_live_process_identity_uses_proc_starttime(self):
        result = subprocess.run(["bash", "-c", IDENTITY +
                                 '\nwine_process_identity $$\nprintf "%s\\n" "$wine_process_starttime"'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.strip().isdigit())

    def supervise_case(self, scenario):
        with tempfile.TemporaryDirectory() as temp:
            setup = '''set -Eeuo pipefail
identity_checks=0
wine_process_identity() {
    identity_checks=$((identity_checks+1))
    wine_process_starttime=100
    if [[ "$SUPERVISOR_TEST_SCENARIO" == absent && "$identity_checks" != 1 ]]; then
        return 1
    fi
    if [[ "$SUPERVISOR_TEST_SCENARIO" == reused && "$identity_checks" != 1 ]]; then
        wine_process_starttime=200
    fi
}
wine_prefix_process_pid() {
    printf 'discovery\\n' >>"$SUPERVISOR_TEST_ROOT/calls"
    [[ "$SUPERVISOR_TEST_SCENARIO" != absent ]] || return 1
    printf '123\\n'
}
initialize_server_compatibility() { printf 'initialize\\n' >>"$SUPERVISOR_TEST_ROOT/calls"; }
bootstrap_account() { :; }
log() { printf '%s\\n' "$*"; }
desktop_relay=rdp
input_route=legacy
public_input_ready=false
grd_fd_restart_threshold=0
network_interface=all
follow_desktop_resolution=off
desktop_monitor_watch=false
console_focus_file=/nonexistent-uu-supervisor-test
relay_window_id=123
sleep_ticks=0
sleep() {
    sleep_ticks=$((sleep_ticks+1))
    ((sleep_ticks < 45)) || exit 0
}
'''
            script = setup + SUPERVISOR.replace("/usr/bin/xdotool", "/usr/bin/true")
            script += "\nsupervise_server 123\n"
            result = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                                    env=dict(os.environ, SUPERVISOR_TEST_ROOT=temp,
                                             SUPERVISOR_TEST_SCENARIO=scenario))
            calls = Path(temp) / "calls"
            return result, calls.read_text().splitlines() if calls.exists() else []

    def test_stable_process_uses_identity_fastpath_without_discovery(self):
        result, calls = self.supervise_case("stable")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [])

    def test_reused_pid_is_rediscovered_and_reinitialized(self):
        result, calls = self.supervise_case("reused")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ["discovery", "initialize"])

    def test_absent_process_retains_original_ten_second_detection(self):
        result, calls = self.supervise_case("absent")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(calls, ["discovery"] * 40)
        self.assertIn("UU server was absent for 10 seconds", result.stdout)

    def test_prefix_selection_ignores_foreign_wine_server(self):
        script = '''set -Eeuo pipefail
WINEPREFIX=/selected-prefix
pgrep() { printf '111\\n222\\n333\\n'; }
process_environment_value() {
    case "$2" in
        111) printf '/foreign-prefix\\n';;
        222) return 1;;
        333) printf '/selected-prefix\\n';;
    esac
}
''' + SELECTOR + "\nwine_prefix_process_pid 'GameViewerServer\\.exe'\n"
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "333")

    def test_foreign_or_missing_prefix_is_not_ready(self):
        script = '''set -Eeuo pipefail
WINEPREFIX=/selected-prefix
pgrep() { printf '111\\n'; }
process_environment_value() { printf '/foreign-prefix\\n'; }
''' + SELECTOR + "\nwine_prefix_process_pid 'GameViewerServer\\.exe'\n"
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")

    def initialize(self, mode="on", reader="active", repeat=False,
                   route="legacy", public_ready=False):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            wine = root / "wine"
            wine.write_text('''#!/usr/bin/env bash
printf '%s\\n' "$*" >>"$INIT_TEST_ROOT/calls"
if [[ "$2" == cursor.dll ]]; then
    if [[ "$INIT_TEST_READER" == active ]]; then
        printf 'UU cursor reader guard active (cursor 24x24)\\n' >"$INIT_TEST_ROOT/reader.log"
    elif [[ "$INIT_TEST_READER" == failed ]]; then
        printf 'UU cursor reader guard initialization failed\\n' >"$INIT_TEST_ROOT/reader.log"
    fi
fi
''')
            wine.chmod(0o700)
            (root / "reader.log").write_text("UU cursor reader guard active (stale)\n")
            script = '''set -Eeuo pipefail
state_dir="$INIT_TEST_ROOT"
input_injector_exe=injector.exe
input_route="$INIT_TEST_ROUTE"
public_input_ready="$INIT_TEST_PUBLIC_READY"
input_bridge_windows_path=input.dll
cursor_guard_windows_path=cursor.dll
cursor_guard_log="$INIT_TEST_ROOT/relay.log"
cursor_reader_guard_log="$INIT_TEST_ROOT/reader.log"
cursor_guard_setting="$INIT_TEST_MODE"
log() { printf '%s\\n' "$*"; }
sleep() { :; }
''' + INITIALIZER.replace("/opt/wine-stable/bin/wine", str(wine))
            script += "\ninitialize_server_compatibility\n"
            if repeat:
                script += "initialize_server_compatibility\n"
            result = subprocess.run(
                ["bash", "-c", script], capture_output=True, text=True, timeout=10,
                env=dict(os.environ, INIT_TEST_ROOT=temp, INIT_TEST_MODE=mode,
                         INIT_TEST_READER=reader, INIT_TEST_ROUTE=route,
                         INIT_TEST_PUBLIC_READY="true" if public_ready else "false"))
            return result, (root / "calls").read_text().splitlines()

    def test_start_and_replacement_both_inject_and_wait_for_reader_guard(self):
        result, calls = self.initialize(repeat=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ["injector.exe input.dll",
                                "injector.exe cursor.dll GameViewerServer.exe"] * 2)

    def test_failed_initialization_is_reported(self):
        result, _ = self.initialize(reader="failed")
        self.assertEqual(result.returncode, 1)
        self.assertIn("did not initialize", result.stdout)

    def test_stale_ready_log_cannot_hide_missing_new_initialization(self):
        result, _ = self.initialize(reader="missing")
        self.assertEqual(result.returncode, 1)
        self.assertIn("did not initialize", result.stdout)

    def test_disabled_guard_only_injects_input_bridge(self):
        result, calls = self.initialize(mode="off")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ["injector.exe input.dll"])

    def test_public_input_waits_for_ready_without_deferring_cursor_guard(self):
        result, calls = self.initialize(route="rdp-public")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ["injector.exe cursor.dll GameViewerServer.exe"])

    def test_ready_public_input_injects_before_the_cursor_guard(self):
        result, calls = self.initialize(route="rdp-public", public_ready=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ["injector.exe input.dll",
                                "injector.exe cursor.dll GameViewerServer.exe"])


class UninstallAuditTests(unittest.TestCase):
    def check_devcon(self, version, known_hash):
        source = (REPOSITORY / "uninstall.sh").read_text()
        validation = source[source.index('release_version="$(manifest_field version)"'):
                            source.index('if [[ -e "$terminal_proxy" ]]')]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "drivers").mkdir()
            (root / "drivers/devcon.exe.uu-original").touch()
            (root / "drivers/devcon.exe").touch()
            script = '''set -Eeuo pipefail
uu_bin="$AUDIT_TEST_ROOT"
manifest_field() { printf '%s\\n' "$AUDIT_TEST_VERSION"; }
sha256sum() { printf '%s  %s\\n' "$AUDIT_TEST_HASH" "$1"; }
''' + validation
            return subprocess.run(
                ["bash", "-c", script], capture_output=True, text=True, timeout=5,
                env=dict(os.environ, AUDIT_TEST_ROOT=temp, AUDIT_TEST_VERSION=version,
                         AUDIT_TEST_HASH=known_hash))

    def test_audited_442_backup_and_live_helper_are_accepted(self):
        result = self.check_devcon("4.42.0.2770",
                                  "46731d6ea59dd9b63ad641c79646bb5ff64e1b877a1226536e3fe34d1ab4ee10")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unknown_hash_or_release_is_rejected(self):
        for version, known_hash in (("4.42.0.2770", "unknown"),
                                   ("9.99.0.1", "46731d6ea59dd9b63ad641c79646bb5ff64e1b877a1226536e3fe34d1ab4ee10")):
            with self.subTest(version=version, hash=known_hash):
                result = self.check_devcon(version, known_hash)
                self.assertEqual(result.returncode, 1)
                self.assertIn("unknown devcon.exe backup", result.stderr)


class VerifierPrefixTests(unittest.TestCase):
    def verify_selection(self, own_present):
        source = (REPOSITORY / "scripts/verify.sh").read_text()
        helper = source[source.index("wine_prefix_process_pid() {"):
                        source.index("normalized_x_display() {")]
        script = '''set -Eeuo pipefail
wine_prefix=/selected-prefix
pgrep() { printf '111\\n222\\n333\\n'; }
process_environment_value() {
    if [[ "$VERIFY_TEST_OWN_PRESENT" == yes && "$2" == 222 ]]; then
        printf '/selected-prefix\\n'
    else
        printf '/foreign-prefix\\n'
    fi
}
''' + helper + '''
server_pid="$(wine_prefix_process_pid 'GameViewerServer\\.exe' || true)"
relay_pid="$(wine_prefix_process_pid 'sdl-freerdp.exe' -x || true)"
printf 'server=%s relay=%s\\n' "$server_pid" "$relay_pid"
[[ -n "$server_pid" && -n "$relay_pid" ]]
'''
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                              env=dict(os.environ, VERIFY_TEST_OWN_PRESENT="yes" if own_present else "no"))

    def test_foreign_prefix_cannot_hide_missing_own_server_or_relay(self):
        result = self.verify_selection(False)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout.strip(), "server= relay=")

    def test_foreign_oldest_server_and_newest_relay_are_excluded(self):
        result = self.verify_selection(True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "server=222 relay=222")


if __name__ == "__main__":
    unittest.main()
