"""Absolute fallback injection follows real RandR changes on one connection."""
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import time
import unittest

from test_manager_capture import isolated_display, ROOT, FLAGS


class LiveGeometryTests(unittest.TestCase):
    def test_same_listener_maps_four_real_modes_and_restores_4k(self):
        with tempfile.TemporaryDirectory(prefix="uu-live-input-geometry-") as temporary:
            directory = Path(temporary)
            binary = directory / "uu-x11-input"
            subprocess.run(["/usr/bin/cc", *[flag for flag in FLAGS if flag != "-Wpedantic"], str(ROOT / "src/uu_x11_input.c"),
                            "-o", str(binary), "-ldl"], check=True, capture_output=True, timeout=30)
            with isolated_display(directory / "display", "3840x2160x24") as env, \
                    (directory / "input.log").open("wb") as log:
                modes = {}
                # Real CVT 60 Hz timings; registration and each live apply are
                # checked below, without requiring an extra fixture tool.
                timings = ["1280x720_test 74.50 1280 1344 1472 1664 720 723 728 748 -hsync +vsync",
                           "1920x1080_test 173.00 1920 2048 2248 2576 1080 1083 1088 1120 -hsync +vsync",
                           "2560x1440_test 312.25 2560 2752 3024 3488 1440 1443 1448 1493 -hsync +vsync"]
                for timing in timings:
                    modeline = timing.split()
                    width, height = int(modeline[2]), int(modeline[6])
                    modes[(width, height)] = modeline[0]
                    for arguments in (["--newmode", *modeline], ["--addmode", "screen", modeline[0]]):
                        subprocess.run(["/usr/bin/xrandr", *arguments], env=env, check=True,
                                       capture_output=True, timeout=5)
                ready = directory / "port"
                token = "a" * 64
                server = subprocess.Popen([str(binary), "--ready-file", str(ready)],
                                          env=dict(env, UURB_X11_INPUT_TOKEN=token), stdout=log, stderr=log)
                try:
                    deadline = time.monotonic() + 5
                    while not ready.exists() and server.poll() is None and time.monotonic() < deadline:
                        time.sleep(.02)
                    self.assertTrue(ready.exists(), "Real native listener did not start")
                    with socket.create_connection(("127.0.0.1", int(ready.read_text())), timeout=5) as connection:
                        def receive():
                            response = b""
                            while len(response) < 16:
                                block = connection.recv(16 - len(response))
                                self.assertTrue(block, "Native listener lost its original connection")
                                response += block
                            return struct.unpack("<IIII", response)
                        connection.sendall(struct.pack("<II", 0x58315255, 3) + token.encode())
                        self.assertEqual(receive(), (0x58315255, 0, 1, 0))
                        for sequence, (width, height) in enumerate(((3840, 2160), (1280, 720),
                                                                  (1920, 1080), (2560, 1440), (3840, 2160)), 1):
                            mode = modes.get((width, height), "3840x2160")
                            subprocess.run(["/usr/bin/xrandr", "--output", "screen", "--mode", mode],
                                           env=env, check=True, capture_output=True, timeout=5)
                            connection.sendall(struct.pack("<IIII", 0x58315255, sequence, 1, 0) +
                                               struct.pack("<IIiiIHH", 2, 0x8001, 65535, 65535, 0, 0, 0))
                            self.assertEqual(receive(), (0x58315255, sequence, 1, 0))
                            pointer = subprocess.run(["/usr/bin/xdotool", "getmouselocation", "--shell"],
                                                     env=env, check=True, capture_output=True, text=True, timeout=5)
                            fields = dict(line.split("=", 1) for line in pointer.stdout.splitlines())
                            self.assertEqual((int(fields["X"]), int(fields["Y"])), (width - 1, height - 1))
                            self.assertIsNone(server.poll())
                finally:
                    if server.poll() is None:
                        server.terminate()
                    server.wait(timeout=5)
