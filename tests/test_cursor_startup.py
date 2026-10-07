"""Private cursor startup evidence; no Wine or live bridge processes."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "scripts/uu-remote-bridge").read_text()


def shell_function(name):
    start = SOURCE.index(name + "() {")
    return SOURCE[start:SOURCE.index("\n}\n", start) + 3]


class CursorStartupTests(unittest.TestCase):
    def failure(self, phase, missing=False):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        state = root / "state"
        state.mkdir()
        relay, reader = root / "relay.log", root / "reader.log"
        if not missing:
            relay.write_text("x" * 70000 + "\nrelay stage=preflight error=126\n")
            reader.write_text("reader active\n")
        (state / "cursor-guard-injector.log").write_text("relay injector output\n")
        (state / "cursor-reader-guard-injector.log").write_text("reader injector output\n")
        common = '''set -Eeuo pipefail
state_dir="$CURSOR_TEST_ROOT/state"
cursor_guard_log="$CURSOR_TEST_ROOT/relay.log"
cursor_reader_guard_log="$CURSOR_TEST_ROOT/reader.log"
cursor_guard_setting=on
input_injector_exe=injector.exe
input_route=legacy
public_input_ready=false
input_bridge_windows_path=input.dll
cursor_guard_windows_path=cursor.dll
log() { printf '%s\\n' "$*"; }
sleep() { :; }
'''
        if phase == "reader-readiness":
            # The reader initialization removes stale readiness before its
            # fake injector fails to produce a fresh ready indication.
            common += shell_function("preserve_cursor_guard_failure") + "\n"
            common += shell_function("inject_input_bridge").replace(
                "/opt/wine-stable/bin/wine", "fake_wine") + "\n"
            common += shell_function("initialize_server_compatibility").replace(
                "/opt/wine-stable/bin/wine", "fake_wine") + "\n"
            common += "fake_wine() { :; }\ninitialize_server_compatibility\n"
        else:
            start = SOURCE.index('    if [[ "$cursor_guard_setting" == on ]]; then\n        rm -f "$cursor_guard_log"')
            end = SOURCE.index('\n    relay_window_id="$(', start)
            block = SOURCE[start:end].replace("/opt/wine-stable/bin/wine", "fake_wine")
            common += shell_function("preserve_cursor_guard_failure") + "\n"
            common += '''fake_wine() {
    printf 'relay stage=preflight error=126\\n' >"$cursor_guard_log"
    printf 'UU cursor guard initialization failed\\n' >>"$cursor_guard_log"
}
''' + block
        result = subprocess.run(["bash", "-c", common], capture_output=True, text=True,
                                timeout=5, env=dict(os.environ, CURSOR_TEST_ROOT=str(root)))
        return root, result

    def test_relay_failed_readiness_preserves_both_logs_before_failure(self):
        root, result = self.failure("relay-readiness")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("did not initialize", result.stdout)
        evidence = next((root / "state").glob("cursor-guard-failure.*"))
        self.assertIn("phase=relay-readiness", (evidence / "summary.txt").read_text())
        self.assertRegex((evidence / "summary.txt").read_text(), r"time=\d{4}-\d\d-\d\dT")
        self.assertIn("preflight error=126", (evidence / "relay-guard.log").read_text())
        self.assertEqual((evidence / "reader-guard.log").read_text(), "reader active\n")
        self.assertEqual(evidence.stat().st_mode & 0o777, 0o700)
        for log in evidence.iterdir():
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)

    def test_reader_failed_readiness_records_missing_reader_and_retains_relay_tail(self):
        root, result = self.failure("reader-readiness")
        self.assertEqual(result.returncode, 1, result.stderr)
        evidence = next((root / "state").glob("cursor-guard-failure.*"))
        summary = (evidence / "summary.txt").read_text()
        self.assertIn("phase=reader-readiness", summary)
        self.assertIn("missing=" + str(root / "reader.log"), summary)
        relay = evidence / "relay-guard.log"
        self.assertEqual(relay.stat().st_size, 65536)
        self.assertTrue(relay.read_text().endswith("relay stage=preflight error=126\n"))
        self.assertTrue((evidence / "cursor-reader-guard-injector.log").is_file())

    def test_failed_attempt_evidence_is_not_overwritten_by_a_later_attempt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "relay.log").write_text("first failure\n")
            (root / "reader.log").write_text("reader active\n")
            script = '''set -Eeuo pipefail
state_dir="$CURSOR_TEST_ROOT"
cursor_guard_log="$state_dir/relay.log"
cursor_reader_guard_log="$state_dir/reader.log"
log() { :; }
''' + shell_function("preserve_cursor_guard_failure") + '''
preserve_cursor_guard_failure relay-readiness
printf 'second failure\\n' >"$cursor_guard_log"
preserve_cursor_guard_failure relay-injection
'''
            result = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                                    timeout=5, env=dict(os.environ, CURSOR_TEST_ROOT=temp))
            self.assertEqual(result.returncode, 0, result.stderr)
            attempts = list(root.glob("cursor-guard-failure.*"))
            self.assertEqual(len(attempts), 2)
            self.assertEqual({(attempt / "relay-guard.log").read_text() for attempt in attempts},
                             {"first failure\n", "second failure\n"})


if __name__ == "__main__":
    unittest.main()
