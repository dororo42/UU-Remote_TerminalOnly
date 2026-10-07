"""Desktop tool isolation tests: no live display, server, or remote connection."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET


REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("desktop_tool", REPO / "scripts/uu-desktop-tool.py")
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


class DesktopToolTests(unittest.TestCase):
    def test_valid_addresses(self):
        for address in ("host.example", "localhost", "127.0.0.1:3390", "192.168.2.3",
                        "[2001:db8::1]:3389", "[::1]", "host-name.example:65535"):
            self.assertEqual(tool.validate_address(address), address)
        self.assertEqual(tool.validate_address("2001:db8::1"), "[2001:db8::1]")

    def test_invalid_addresses_and_option_injection(self):
        for address in ("", "/v:host", "-cert:ignore", "host /p:secret", "host\n/p:secret",
                        "rdp://host", "user@host", "host:0", "host:65536", "host:",
                        "300.1.2.3", "host;touch", "$(touch x)", "host:abc", "[not-ipv6]",
                        "-host", "host..example", "host\\name"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                tool.validate_address(address)

    def test_username_is_one_value_and_control_characters_rejected(self):
        self.assertEqual(tool.validate_username("DOMAIN\\name"), "DOMAIN\\name")
        for username in ("a\nb", "a\0b", "x" * 257):
            with self.assertRaises(ValueError):
                tool.validate_username(username)

    def test_viewer_font_is_local_and_original_environment_unchanged(self):
        with patch.dict(os.environ, FONTCONFIG_FILE="/original"), \
                patch.object(tool, "font_config", return_value="/app-fonts"), \
                patch.object(tool.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertEqual(tool.run_viewer(), 0)
            self.assertEqual(os.environ["FONTCONFIG_FILE"], "/original")
        self.assertEqual(run.call_args.args[0], ["/usr/bin/xtigervncviewer"])
        self.assertEqual(run.call_args.kwargs["env"]["FONTCONFIG_FILE"], "/app-fonts")

    def test_font_asset_contains_verified_noto_assignment_and_default_include(self):
        root = ET.parse(REPO / "desktop/uu-tools-fonts.conf").getroot()
        self.assertEqual(root.find("include").text, "/etc/fonts/fonts.conf")
        self.assertEqual(root.find("match/edit").attrib["mode"], "assign")
        self.assertEqual(root.find("match/edit/string").text, "Noto Sans CJK SC")
        with tempfile.TemporaryDirectory() as directory, patch.object(Path, "home", return_value=Path(directory)):
            self.assertEqual(tool.font_config(), str(REPO / "desktop/uu-tools-fonts.conf"))
            installed = Path(directory) / ".local/share/uu-remote/tools/uu-tools-fonts.conf"
            installed.parent.mkdir(parents=True)
            installed.write_text("installed")
            self.assertEqual(tool.font_config(), str(installed))

    def test_rdp_cancel_does_not_open_terminal_or_connect(self):
        with patch.object(tool, "dialog", return_value=subprocess.CompletedProcess([], 1, "")), \
                patch.object(tool.subprocess, "run") as run:
            self.assertEqual(tool.choose_rdp(), 0)
        run.assert_not_called()

    def test_rdp_form_launches_terminal_with_validated_address_and_no_password(self):
        with patch.object(tool, "dialog", return_value=subprocess.CompletedProcess([], 0, "server.example:3389|DOMAIN\\user\n")), \
                patch.object(tool.shutil, "which", return_value="/usr/bin/xdg-terminal-exec"), \
                patch.object(tool.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertEqual(tool.choose_rdp(), 0)
        arguments = run.call_args.args[0]
        self.assertEqual(arguments[0], "/usr/bin/xdg-terminal-exec")
        self.assertEqual(arguments[-4:], ["rdp-run", "--", "server.example:3389", "DOMAIN\\user"])
        self.assertFalse(any(argument.startswith("/p:") for argument in arguments))

    def test_rdp_form_option_like_username_survives_child_argument_parser(self):
        for username in ("-demo", "--help"):
            with self.subTest(username=username), \
                    patch.object(tool, "dialog", return_value=subprocess.CompletedProcess([], 0, f"server.example|{username}\n")), \
                    patch.object(tool.shutil, "which", return_value="/usr/bin/xdg-terminal-exec"), \
                    patch.object(tool.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as launch:
                self.assertEqual(tool.choose_rdp(), 0)
            arguments = launch.call_args.args[0]
            with patch.object(tool, "run_rdp", return_value=0) as connect:
                self.assertEqual(tool.main(arguments[3:]), 0)
            connect.assert_called_once_with("server.example", username)

    def test_rdp_form_rejects_injection_before_terminal(self):
        with patch.object(tool, "dialog", return_value=subprocess.CompletedProcess([], 0, "host /cert:ignore|user\n")), \
                patch.object(tool.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                tool.choose_rdp()
        run.assert_not_called()

    def test_private_rdp_relay_rejected_before_terminal_and_native_client(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(Path, "home", return_value=Path(directory)), \
                patch.object(tool.socket, "gethostname", return_value="bridge-host"):
            for address in ("127.0.0.1:3390", "127.12.34.56:3390", "localhost:3390",
                            "LOCALHOST.:3390", "[::1]:3390", "bridge-host:3390"):
                with self.subTest(address=address), \
                        patch.object(tool, "dialog", return_value=subprocess.CompletedProcess([], 0, f"{address}|user\n")), \
                        patch.object(tool.subprocess, "run") as run:
                    with self.assertRaisesRegex(ValueError, "UU 内部.*uu-remote open"):
                        tool.choose_rdp()
                    with self.assertRaisesRegex(ValueError, "UU 内部"):
                        tool.run_rdp(address, "user")
                    run.assert_not_called()

    def test_saved_custom_relay_port_guard_and_external_same_port_allowed(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(Path, "home", return_value=Path(directory)), \
                patch.object(tool.socket, "gethostname", return_value="bridge-host"):
            config = Path(directory) / ".config/uu-remote-bridge/environment"
            config.parent.mkdir(parents=True)
            config.write_text("UURB_RDP_PORT=3390\nUURB_RDP_PORT=4400\n")
            with patch.object(tool.subprocess, "run") as run:
                with self.assertRaisesRegex(ValueError, "4400"):
                    tool.run_rdp("[::1]:4400", "user")
                run.assert_not_called()
            for address in ("server.example:4400", "203.0.113.2:4400", "localhost:3390", "localhost:3389"):
                with self.subTest(address=address), \
                        patch.object(tool.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run, \
                        patch.object(tool.sys.stdin, "isatty", return_value=False), \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(tool.run_rdp(address, "user"), 0)
                    self.assertIn("/v:" + address, run.call_args.args[0])

    def test_external_default_relay_port_and_ordinary_local_rdp_still_allowed(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(Path, "home", return_value=Path(directory)):
            for address in ("server.example:3390", "203.0.113.2:3390", "localhost:3389", "[::1]:3389"):
                with self.subTest(address=address), \
                        patch.object(tool, "dialog", return_value=subprocess.CompletedProcess([], 0, f"{address}|user\n")), \
                        patch.object(tool.shutil, "which", return_value="/usr/bin/xdg-terminal-exec"), \
                        patch.object(tool.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
                    self.assertEqual(tool.choose_rdp(), 0)
                    self.assertEqual(run.call_args.args[0][-2], address)

    def test_rdp_native_uses_stdin_default_certificate_validation_and_explicit_target(self):
        with patch.object(tool.subprocess, "run", return_value=subprocess.CompletedProcess([], 4)) as run, \
                patch.object(tool.sys.stdin, "isatty", return_value=False), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tool.run_rdp("server.example", "user"), 4)
        arguments = run.call_args.args[0]
        self.assertIn("/v:server.example", arguments)
        self.assertIn("/from-stdin:force", arguments)
        self.assertFalse(any(argument.startswith(("/p:", "/cert:")) for argument in arguments))
        self.assertNotIn("/v:127.0.0.1:3390", arguments)

    def test_openbox_scales_only_child_and_does_not_start_window_manager(self):
        with patch.object(tool, "dialog", return_value=subprocess.CompletedProcess([], 0)), \
                patch.dict(os.environ, GDK_SCALE="2", GDK_DPI_SCALE="1"), \
                patch.object(tool.subprocess, "run", side_effect=[
                    subprocess.CompletedProcess([], 0, "Xft.dpi:\t192\n"),
                    subprocess.CompletedProcess([], 0)]) as run:
            self.assertEqual(tool.run_openbox(), 0)
            self.assertEqual(os.environ["GDK_SCALE"], "2")
        self.assertEqual(run.call_args.args[0], ["/usr/bin/obconf"])
        env = run.call_args.kwargs["env"]
        self.assertEqual((env["GDK_SCALE"], env["GDK_DPI_SCALE"]), ("1", "0.5"))

    def test_openbox_dpi_normalization_is_adaptive_and_safe_on_unknown(self):
        for resource, expected in (("Xft.dpi: 96\n", "1"), ("Xft.dpi: 384\n", "0.25"),
                                   ("Xft.dpi: 72\n", "1.33333"), ("Xft.dpi: 0\n", "1"),
                                   ("Xft.dpi: 999\n", "1"), ("Xft.dpi: unknown\n", "1"), ("", "1")):
            with self.subTest(resource=resource), \
                    patch.object(tool, "dialog", return_value=subprocess.CompletedProcess([], 0)), \
                    patch.object(tool.subprocess, "run", side_effect=[
                        subprocess.CompletedProcess([], 0, resource), subprocess.CompletedProcess([], 0)]) as run:
                self.assertEqual(tool.run_openbox(), 0)
                self.assertEqual(run.call_args.kwargs["env"]["GDK_DPI_SCALE"], expected)

    def test_openbox_resource_timeout_retains_normal_font_scale(self):
        with patch.object(tool, "dialog", return_value=subprocess.CompletedProcess([], 0)), \
                patch.object(tool.subprocess, "run", side_effect=[
                    subprocess.TimeoutExpired("xrdb", 3), subprocess.CompletedProcess([], 0)]) as run:
            self.assertEqual(tool.run_openbox(), 0)
        self.assertEqual(run.call_args.kwargs["env"]["GDK_DPI_SCALE"], "1")

    def test_x11vnc_config_wait_never_starts_server_and_avoids_conda_wish(self):
        with patch.object(tool, "dialog", return_value=subprocess.CompletedProcess([], 0, "查看 X11VNC 配置\n")), \
                patch.dict(os.environ, PATH="/conda/bin:/usr/bin"), \
                patch.object(tool.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertEqual(tool.run_x11vnc(), 0)
            self.assertEqual(os.environ["PATH"], "/conda/bin:/usr/bin")
        arguments = run.call_args.args[0]
        self.assertEqual(arguments[arguments.index("-gui") + 1], "wait")
        self.assertIn("-norc", arguments)
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["PATH"], "/usr/bin:/bin")
        self.assertEqual(env["X11VNC_FONT_BOLD"], "{Noto Sans CJK SC} -18 bold")

    def test_x11vnc_cancel_or_viewer_choice_does_not_start_server(self):
        with patch.object(tool, "dialog", return_value=subprocess.CompletedProcess([], 1, "")), \
                patch.object(tool.subprocess, "run") as run:
            self.assertEqual(tool.run_x11vnc(), 0)
        run.assert_not_called()
        with patch.object(tool, "dialog", return_value=subprocess.CompletedProcess([], 0, "打开 VNC 查看器\n")), \
                patch.object(tool, "run_viewer", return_value=0) as viewer, \
                patch.object(tool.subprocess, "run") as run:
            self.assertEqual(tool.run_x11vnc(), 0)
        viewer.assert_called_once()
        run.assert_not_called()

    def test_retained_viewer_is_visible_and_old_tool_ids_are_hidden(self):
        viewer = (REPO / "desktop/uu-vnc-viewer.desktop.in").read_text()
        self.assertIn("Name=TigerVNC 查看器\n", viewer)
        self.assertIn('Exec=/usr/bin/python3 "%HOME%/.local/libexec/uu-desktop-tool.py" viewer', viewer)
        self.assertNotIn("NoDisplay=true", viewer)
        self.assertNotIn("Hidden=true", viewer)
        for filename, command in (("xtigervncviewer", "viewer"), ("xfreerdp", "rdp"),
                                  ("obconf", "openbox"), ("x11vnc", "x11vnc")):
            text = (REPO / f"desktop/{filename}.desktop.in").read_text()
            self.assertIn(f'Exec=/usr/bin/python3 "%HOME%/.local/libexec/uu-desktop-tool.py" {command}', text)
            self.assertIn("NoDisplay=true", text)
            self.assertIn("Hidden=true", text)


if __name__ == "__main__":
    unittest.main()
