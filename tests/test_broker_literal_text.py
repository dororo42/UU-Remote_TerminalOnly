"""Exercise actual broker literal-text planning with controlled transport callbacks."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class BrokerLiteralTextTests(unittest.TestCase):
    def test_public_literal_text_preflight_and_failure_order(self):
        source = (ROOT / "src/uu_input_broker.c").read_text()
        ranges = [
            ("static BOOL input_to_x11_event(", "static x11_route_result send_x11_events("),
            ("static BOOL input_is_backspace(const INPUT *input);", "static BOOL append_key_event("),
            ("static BOOL append_key_event(", "static BOOL request_relay_focus(DWORD *waited_ms)\n{"),
        ]
        actual = "\n".join(source[source.index(start):source.index(end, source.index(start))]
                           for start, end in ranges)
        with tempfile.TemporaryDirectory(prefix="uu-broker-literal-text-") as temporary:
            directory = Path(temporary)
            (directory / "broker-functions.inc").write_text(actual)
            binary = directory / "broker-literal-text-tests"
            compiled = subprocess.run(
                ["/usr/bin/cc", "-std=c11", "-Wall", "-Wextra", "-Wpedantic",
                 "-Werror", "-O2", "-I", str(directory), "-I", str(ROOT / "src"),
                 str(ROOT / "tests/fixtures/broker_literal_text_fixture.c"), "-o", str(binary)],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout,
                             "PASS 12 literal-text/preflight groups; fake transport callbacks only\n")
