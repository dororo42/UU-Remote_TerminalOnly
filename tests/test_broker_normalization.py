"""Run the actual broker dispatch/translation with controlled Win32 callbacks."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class BrokerNormalizationTests(unittest.TestCase):
    def test_public_keyboard_normalization_and_no_retry(self):
        source = (ROOT / "src/uu_input_broker.c").read_text()
        start = source.index("static BOOL append_key_event(")
        end = source.index("static BOOL request_relay_focus(DWORD *waited_ms)\n{", start)
        with tempfile.TemporaryDirectory(prefix="uu-broker-normalization-") as temporary:
            directory = Path(temporary)
            (directory / "broker-functions.inc").write_text(source[start:end])
            binary = directory / "broker-normalization-tests"
            compiled = subprocess.run(
                ["/usr/bin/cc", "-std=c11", "-Wall", "-Wextra", "-Wpedantic",
                 "-Werror", "-O2", "-I", str(directory),
                 str(ROOT / "tests/fixtures/broker_normalization_fixture.c"),
                 "-o", str(binary)],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True,
                                    timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout,
                             "PASS 7 broker normalization groups; fake Win32/public callbacks only\n")
