"""Exercise GNOME monitor fallback with isolated fake busctl/gsettings."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SOURCE = (Path(__file__).resolve().parents[1] / "scripts/uu-remote-bridge").read_text()
HELPERS = SOURCE[SOURCE.index("physical_monitor_state() {"):
                 SOURCE.index("start_desktop_relay() {")]
SUPERVISOR = SOURCE[SOURCE.index("supervise_server() {"):
                    SOURCE.index('\nsupervise_server "$server_pid" &')]


def display_state(*specs):
    logicals = [[0, 0, 1, 0, True, list(specs), {}]] if specs else []
    return json.dumps({"type": "fixture", "data": [1, [], logicals, {}]})


PHYSICAL = ["DP-3", "DEL", "Dell monitor", "1234"]
VIRTUAL = ["Meta-0", "MetaVendor", "Virtual remote monitor", "0"]


class MonitorModeTests(unittest.TestCase):
    def run_helpers(self, state, commands, mode="'mirror-primary'", bus_status=0,
                    set_status=0, marker=False):
        with tempfile.TemporaryDirectory(prefix="uu-monitor-test-") as temp:
            root = Path(temp)
            (root / "mode").write_text(mode)
            (root / "state").write_text(state)
            if marker:
                (root / "monitor-fallback.mode").write_text("'mirror-primary'\n'extend'\n")
            busctl = root / "busctl"
            busctl.write_text('''#!/usr/bin/env bash
cat "$MONITOR_TEST_ROOT/state"
exit "$MONITOR_TEST_BUS_STATUS"
''')
            gsettings = root / "gsettings"
            gsettings.write_text('''#!/usr/bin/env bash
case "$1" in
    get) cat "$MONITOR_TEST_ROOT/mode"; printf '\\n';;
    set)
        [[ "$MONITOR_TEST_SET_STATUS" == 0 ]] || exit "$MONITOR_TEST_SET_STATUS"
        printf '%s\\n' "$4" >>"$MONITOR_TEST_ROOT/calls"
        printf "'%s'" "${4//\\'/}" >"$MONITOR_TEST_ROOT/mode";;
esac
''')
            for command in (busctl, gsettings):
                command.chmod(0o700)
            script = (HELPERS + SUPERVISOR).replace("/usr/bin/busctl", str(busctl))
            script = script.replace("/usr/bin/gsettings", str(gsettings))
            script = '''set -Eeuo pipefail
desktop_bus=unix:path=/fake
state_dir="$MONITOR_TEST_ROOT"
desktop_session_type=wayland
desktop_monitor_watch=false
monitor_fallback_original=""
monitor_fallback_value=""
log() { printf '%s\\n' "$*"; }
''' + script + "\n" + commands
            result = subprocess.run(
                ["bash", "-c", script], capture_output=True, text=True, timeout=10,
                env=dict(os.environ, MONITOR_TEST_ROOT=temp,
                         MONITOR_TEST_BUS_STATUS=str(bus_status),
                         MONITOR_TEST_SET_STATUS=str(set_status)),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = (root / "calls").read_text() if (root / "calls").exists() else ""
            requests = (root / "requests").read_text() if (root / "requests").exists() else ""
            return result.stdout + requests, (root / "mode").read_text(), calls

    def test_present_physical_monitor_retains_mirror(self):
        output, mode, calls = self.run_helpers(
            display_state(PHYSICAL), "physical_monitor_state; configure_desktop_monitor_mode")
        self.assertEqual(output.strip(), "present")
        self.assertEqual(mode, "'mirror-primary'")
        self.assertEqual(calls, "")

    def test_no_active_physical_monitor_enables_fallback_then_restores(self):
        output, mode, calls = self.run_helpers(
            display_state(), "configure_desktop_monitor_mode; restore_desktop_monitor_mode")
        self.assertIn("temporarily sharing", output)
        self.assertIn("Restored the original", output)
        self.assertEqual(mode, "'mirror-primary'")
        self.assertEqual(calls.splitlines(), ["extend", "'mirror-primary'"])

    def test_bridge_virtual_monitor_does_not_count_as_physical(self):
        output, mode, calls = self.run_helpers(
            display_state(VIRTUAL), "physical_monitor_state; configure_desktop_monitor_mode")
        self.assertTrue(output.startswith("absent\n"))
        self.assertEqual(mode, "'extend'")
        self.assertEqual(calls, "extend\n")

    def test_disconnected_monitor_inventory_is_not_an_active_monitor(self):
        state = json.loads(display_state())
        state["data"][1] = [[PHYSICAL, [], {}]]
        output, _, _ = self.run_helpers(json.dumps(state), "physical_monitor_state")
        self.assertEqual(output.strip(), "absent")

    def test_physical_beside_virtual_is_present(self):
        output, mode, calls = self.run_helpers(
            display_state(PHYSICAL, VIRTUAL), "physical_monitor_state; configure_desktop_monitor_mode")
        self.assertEqual(output.strip(), "present")
        self.assertEqual(mode, "'mirror-primary'")
        self.assertEqual(calls, "")

    def test_unknown_query_never_changes_mode(self):
        for state, status in ((display_state(), 1), ("invalid json", 0),
                              ('{"data": [1, [], null, {}]}', 0),
                              ('{"data": [1, [], [[0, 0, 1, 0, true, [], {}]], {}]}', 0)):
            with self.subTest(state=state, status=status):
                output, mode, calls = self.run_helpers(
                    state, "physical_monitor_state; configure_desktop_monitor_mode",
                    bus_status=status)
                self.assertEqual(output.strip(), "unknown")
                self.assertEqual(mode, "'mirror-primary'")
                self.assertEqual(calls, "")

    def test_user_extend_setting_is_preserved(self):
        _, mode, calls = self.run_helpers(
            display_state(), "configure_desktop_monitor_mode; restore_desktop_monitor_mode",
            mode="'extend'")
        self.assertEqual(mode, "'extend'")
        self.assertEqual(calls, "")

    def test_cleanup_preserves_user_changes(self):
        output, mode, calls = self.run_helpers(display_state(), '''
configure_desktop_monitor_mode
printf "'mirror-primary'" >"$MONITOR_TEST_ROOT/mode"
restore_desktop_monitor_mode
[[ ! -e "$state_dir/monitor-fallback.mode" ]]
''')
        self.assertNotIn("Restored the original", output)
        self.assertEqual(mode, "'mirror-primary'")
        self.assertEqual(calls, "extend\n")

    def test_failed_fallback_setting_does_not_schedule_restarts(self):
        output, mode, calls = self.run_helpers(display_state(), '''
configure_desktop_monitor_mode
[[ "$desktop_monitor_watch" == false ]]
[[ -z "$monitor_fallback_original" ]]
[[ ! -e "$state_dir/monitor-fallback.mode" ]]
''', set_status=1)
        self.assertIn("Could not enable", output)
        self.assertEqual(mode, "'mirror-primary'")
        self.assertEqual(calls, "")

    def test_crashed_fallback_is_recovered_with_physical_monitor(self):
        output, mode, calls = self.run_helpers(display_state(PHYSICAL), '''
configure_desktop_monitor_mode
[[ "$desktop_monitor_watch" == true ]]
[[ -z "$monitor_fallback_original" ]]
[[ ! -e "$state_dir/monitor-fallback.mode" ]]
''', mode="'extend'", marker=True)
        self.assertIn("Restored the original", output)
        self.assertEqual(mode, "'mirror-primary'")
        self.assertEqual(calls, "'mirror-primary'\n")

    def test_crashed_fallback_is_reclaimed_without_physical_monitor(self):
        for state in (display_state(), display_state(VIRTUAL), "invalid json"):
            with self.subTest(state=state):
                output, mode, calls = self.run_helpers(state, '''
configure_desktop_monitor_mode
[[ "$desktop_monitor_watch" == true ]]
[[ "$monitor_fallback_original" == "'mirror-primary'" ]]
[[ -e "$state_dir/monitor-fallback.mode" ]]
''', mode="'extend'", marker=True)
                self.assertIn("Recovered ownership", output)
                self.assertEqual(mode, "'extend'")
                self.assertEqual(calls, "")

    def test_crash_before_setting_leaves_mirror_and_clears_marker(self):
        _, mode, calls = self.run_helpers(display_state(PHYSICAL), '''
configure_desktop_monitor_mode
[[ ! -e "$state_dir/monitor-fallback.mode" ]]
''', marker=True)
        self.assertEqual(mode, "'mirror-primary'")
        self.assertEqual(calls, "")

    def test_failed_crash_recovery_restore_does_not_loop(self):
        output, mode, calls = self.run_helpers(display_state(PHYSICAL), '''
configure_desktop_monitor_mode
[[ "$desktop_monitor_watch" == false ]]
[[ "$monitor_fallback_original" == "'mirror-primary'" ]]
[[ -e "$state_dir/monitor-fallback.mode" ]]
''', mode="'extend'", marker=True, set_status=1)
        self.assertIn("without repeated restarts", output)
        self.assertEqual(mode, "'extend'")
        self.assertEqual(calls, "")

    def test_normal_cleanup_clears_persisted_ownership(self):
        _, mode, _ = self.run_helpers(display_state(), '''
configure_desktop_monitor_mode
[[ -e "$state_dir/monitor-fallback.mode" ]]
restore_desktop_monitor_mode
[[ ! -e "$state_dir/monitor-fallback.mode" ]]
''')
        self.assertEqual(mode, "'mirror-primary'")

    def supervise(self, state, fallback=False, mode=None):
        return self.run_helpers(state, '''
desktop_monitor_watch=true
FALLBACK
desktop_relay=rdp
input_route=legacy
public_input_ready=false
grd_fd_restart_threshold=0
network_interface=all
follow_desktop_resolution=off
console_focus_file=/nonexistent-uu-monitor-test
relay_window_id=123
service_name=uu-test.service
state_dir="$MONITOR_TEST_ROOT"
systemctl_user=(fake_systemctl)
fake_systemctl() { printf 'restart-request:%s\\n' "$*" >>"$MONITOR_TEST_ROOT/requests"; }
wine_prefix_process_pid() { printf '123\\n'; }
wine_process_identity() { wine_process_starttime=1; }
sleep_ticks=0
sleep() {
    if [[ "$1" == 1 ]]; then printf 'planned-wait\\n'; exit 0; fi
    sleep_ticks=$((sleep_ticks + 1))
    ((sleep_ticks < 45)) || exit 0
}
supervise_server 123
'''.replace("FALLBACK", '''monitor_fallback_original="'mirror-primary'"
monitor_fallback_value="'extend'"''' if fallback else ""),
            mode=mode or ("'extend'" if fallback else "'mirror-primary'"))

    def test_physical_reappearance_requests_one_planned_restart_and_waits(self):
        output, _, _ = self.supervise(display_state(PHYSICAL), fallback=True)
        self.assertIn("Physical monitor state is present", output)
        self.assertEqual(output.count("restart-request:"), 1)
        self.assertIn("planned-wait", output)
        self.assertNotIn("A bridge process exited", output)

    def test_physical_disappearance_requests_planned_fallback_restart(self):
        output, _, _ = self.supervise(display_state())
        self.assertIn("Physical monitor state is absent", output)
        self.assertIn("planned-wait", output)

    def test_fallback_virtual_monitor_does_not_create_restart_loop(self):
        output, _, _ = self.supervise(display_state(VIRTUAL), fallback=True)
        self.assertNotIn("restart-request:", output)

    def test_unknown_monitor_state_never_requests_restart(self):
        output, _, _ = self.supervise("invalid json")
        self.assertNotIn("restart-request:", output)

    def test_user_mode_change_prevents_monitor_transition(self):
        output, _, _ = self.supervise(display_state(), mode="'extend'")
        self.assertIn("changed externally", output)
        self.assertNotIn("restart-request:", output)

    def test_cleanup_restores_mode_after_grd_is_stopped(self):
        cleanup = SOURCE[SOURCE.index("cleanup() {"):SOURCE.index("\ntrap cleanup EXIT")]
        self.assertLess(cleanup.index('kill -KILL "$pid"'),
                        cleanup.index("restore_desktop_monitor_mode"))


if __name__ == "__main__":
    unittest.main()
