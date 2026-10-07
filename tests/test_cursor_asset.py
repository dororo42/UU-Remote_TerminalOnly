"""Validate Windows cursor encoding and safe desktop-theme fallback."""
import importlib.util
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch


REPOSITORY = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "cursor_asset", REPOSITORY / "scripts/uu-cursor-asset.py")
CURSOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CURSOR)


class CursorAssetTests(unittest.TestCase):
    def test_cur_header_bitmap_alpha_orientation_and_hotspot(self):
        encoded = CURSOR.encode_cursor(2, 2, 1, 0,
                                       [0, 0xFFFF0000, 0x80402010, 0xFF0000FF])
        self.assertEqual(struct.unpack_from("<HHH", encoded), (0, 2, 1))
        directory = struct.unpack_from("<BBBBHHII", encoded, 6)
        self.assertEqual(directory[:6], (2, 2, 0, 0, 1, 0))
        self.assertEqual(directory[6:], (64, 22))
        bitmap = struct.unpack_from("<IiiHHIIiiII", encoded, 22)
        self.assertEqual(bitmap[:7], (40, 2, 4, 1, 32, 0, 24))
        # Bottom row is first, and partial alpha is converted from premultiplied.
        self.assertEqual(encoded[62:78], bytes([
            32, 64, 128, 128, 255, 0, 0, 255,
            0, 0, 0, 0, 0, 0, 255, 255]))
        self.assertEqual(encoded[78:], bytes([0, 0, 0, 0, 128, 0, 0, 0]))

    def test_scaling_has_exact_dimensions_and_preserves_hotspot(self):
        encoded = CURSOR.encode_cursor(2, 2, 1, 1, [0xFFFFFFFF] * 4, size=24)
        directory = struct.unpack_from("<BBBBHHII", encoded, 6)
        self.assertEqual(directory[:6], (24, 24, 0, 0, 12, 12))
        self.assertEqual(struct.unpack_from("<Iii", encoded, 22), (40, 24, 48))
        self.assertEqual(encoded[62:62 + 24 * 24 * 4], bytes([255] * (24 * 24 * 4)))

    def test_scaling_transparent_edges_preserves_color_without_dark_halo(self):
        encoded = CURSOR.encode_cursor(2, 1, 0, 0, [0, 0xFFFF0000], size=24)
        pixels = encoded[62:62 + 24 * 24 * 4]
        translucent = [pixels[index:index + 4] for index in range(0, len(pixels), 4)
                       if 0 < pixels[index + 3] < 255]
        self.assertTrue(translucent)
        for pixel in translucent:
            self.assertEqual(pixel[:3], bytes([0, 0, 255]))

    def test_invalid_dimensions_hotspot_and_pixels_are_rejected(self):
        for args in ((0, 2, 0, 0, []), (257, 1, 0, 0, [0] * 257),
                     (2, 2, 2, 0, [0] * 4), (2, 2, 0, -1, [0] * 4),
                     (2, 2, 0, 0, [0])):
            with self.subTest(args=args[:4]):
                with self.assertRaises(ValueError):
                    CURSOR.encode_cursor(*args)

    def test_cursor_is_published_atomically_with_private_permissions(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "uu-cursor.cur"
            output.write_bytes(b"old cursor")
            payload = CURSOR.encode_cursor(1, 1, 0, 0, [0xFFFFFFFF])
            with patch.object(CURSOR, "load_cursor", return_value=payload):
                self.assertTrue(CURSOR.write_cursor(output, "theme", 24))
            self.assertEqual(output.read_bytes(), payload)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(Path(temp).iterdir()), [output])

    def test_missing_library_or_theme_removes_stale_cursor_for_builtin_fallback(self):
        for error in (OSError("libXcursor missing"), ValueError("theme unavailable")):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as temp:
                output = Path(temp) / "uu-cursor.cur"
                output.write_bytes(b"stale cursor")
                with patch.object(CURSOR, "load_cursor", side_effect=error):
                    self.assertFalse(CURSOR.write_cursor(output, "theme", 24))
                self.assertFalse(output.exists())
                self.assertEqual(list(Path(temp).iterdir()), [])

    def test_real_libxcursor_exports_default_pointer_without_display(self):
        encoded = CURSOR.load_cursor("", 24)
        self.assertEqual(struct.unpack_from("<HHH", encoded), (0, 2, 1))
        self.assertEqual(struct.unpack_from("<BBBB", encoded, 6), (24, 24, 0, 0))
        alpha = encoded[65:62 + 24 * 24 * 4:4]
        self.assertIn(0, alpha)
        self.assertIn(255, alpha)

    def test_bridge_persists_resolved_service_size_and_keeps_running_on_asset_failure(self):
        source = (REPOSITORY / "scripts/uu-remote-bridge").read_text()
        helper = source[source.index("resolve_cursor_size() {"):source.index("\ncleanup() {")]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            compat = root / "compat"
            compat.mkdir()
            (compat / "uu-cursor.cur").write_bytes(b"stale")
            settings = root / "gsettings"
            settings.write_text("#!/bin/sh\nprintf \"'theme-name'\\n\"\n")
            settings.chmod(0o700)
            export = root / "export.py"
            export.write_text("import sys\nsys.exit(1)\n")
            helper = helper.replace("/usr/bin/gsettings", str(settings))
            helper = helper.replace('"$HOME/.local/libexec/uu-cursor-asset.py"', str(export))
            script = '''set -Eeuo pipefail
cursor_size_setting=24
desktop_display=""
desktop_bus=unix:path=/fake
compat_dir="$CURSOR_TEST_COMPAT"
UURB_CURSOR_GUARD_LOG='C:\\users\\user\\Temp\\uu-cursor-guard.log'
log() { printf '%s\\n' "$*"; }
''' + helper + '\nresolve_cursor_size\n'
            result = subprocess.run(["bash", "-c", script], capture_output=True,
                                    text=True, timeout=10,
                                    env=dict(os.environ, CURSOR_TEST_COMPAT=str(compat)))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((compat / "uu-cursor.ini").read_text(),
                             "[Cursor]\nSize=24\nRelayLogPath=C:\\users\\user\\Temp\\uu-cursor-guard.log\n")
            self.assertEqual((compat / "uu-cursor.ini").stat().st_mode & 0o777, 0o600)
            self.assertFalse((compat / "uu-cursor.cur").exists())
            self.assertIn("built-in cursor fallback", result.stdout)
            self.assertEqual(list(compat.iterdir()), [compat / "uu-cursor.ini"])

    def test_helper_is_installed_and_covered_by_runtime_digest(self):
        installer = (REPOSITORY / "install.sh").read_text()
        digest = (REPOSITORY / "scripts/runtime-source-digest").read_text()
        self.assertIn('"$repo_dir/scripts/uu-cursor-asset.py"', installer)
        self.assertIn('"$HOME/.local/libexec/uu-cursor-asset.py"', installer)
        self.assertIn("scripts/uu-cursor-asset.py", digest)


if __name__ == "__main__":
    unittest.main()
