"""Exercise actual plugin pipe I/O with controlled transport and clock callbacks."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PluginIOTests(unittest.TestCase):
    def test_ready_partial_retry_cancel_deadline_and_peer(self):
        source = (ROOT / "src/plugin.c").read_text()
        start = source.index("static bool check_stop(")
        end = source.index("static void capability_reply(", start)
        with tempfile.TemporaryDirectory(prefix="uu-plugin-io-") as temporary:
            directory = Path(temporary)
            (directory / "io-functions.inc").write_text(source[start:end])
            binary = directory / "plugin-io-tests"
            compile_result = subprocess.run(
                ["/usr/bin/cc", "-std=c11", "-Wall", "-Wextra", "-Wpedantic",
                 "-Werror", "-O2", "-I", str(directory),
                 str(ROOT / "tests/fixtures/plugin_io_fixture.c"), "-o", str(binary)],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(compile_result.returncode, 0, compile_result.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout,
                             "PASS 15 ready/noData/partial/stop/deadline/peer cases; "
                             "fake callbacks only\n")
