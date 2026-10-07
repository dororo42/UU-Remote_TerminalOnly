"""Exercise console focus helpers with fake X commands, never a live desktop."""
import os
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SOURCE = (Path(__file__).resolve().parents[1] / "scripts/uu-remote-console").read_text()
REPOSITORY = Path(__file__).resolve().parents[1]


class ConsoleFocusTests(unittest.TestCase):
    def test_manager_viewer_requests_raw_on_loopback_without_enabling_remote_resize(self):
        with tempfile.TemporaryDirectory(prefix="uu-manager-viewer-test-") as temp:
            directory = Path(temp)
            viewer = directory / "viewer"
            arguments = directory / "arguments"
            viewer.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$VIEWER_ARGUMENTS"\n')
            viewer.chmod(0o700)
            command = SOURCE.split('    /usr/bin/vncviewer \\\n', 1)[1].split('    # Background + wait', 1)[0]
            script = f'exec 9>/dev/null\n"{viewer}" \\\n' + command + 'wait "$!"\n'
            env = dict(os.environ, state_dir=temp, window_port="5921", VIEWER_ARGUMENTS=str(arguments))
            result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            received = arguments.read_text().splitlines()
            self.assertIn("-PreferredEncoding=Raw", received)
            self.assertIn("-AutoSelect=0", received)
            self.assertIn("-AcceptClipboard=0", received)
            self.assertIn("-SendClipboard=0", received)
            self.assertIn("-SetPrimary=0", received)
            self.assertIn("-SendPrimary=0", received)
            self.assertIn("-RemoteResize=0", received)
            self.assertEqual(received[-1], "127.0.0.1::5921")

    def test_manager_capture_does_not_exchange_desktop_clipboard_or_primary_selection(self):
        with tempfile.TemporaryDirectory(prefix="uu-manager-selection-test-") as temp:
            directory = Path(temp)
            server = directory / "x11vnc"
            arguments = directory / "arguments"
            environment = directory / "environment"
            module = directory / "capture.so"
            subprocess.run(["gcc", "-x", "c", "-shared", "-fPIC", "-o", str(module), "-"],
                           input="int fixture_capture_marker;\n", text=True, capture_output=True, check=True)
            server.write_text('''#!/usr/bin/env bash
printf "%s\\n" "$@" > "$CAPTURE_ARGUMENTS"
printf "%s\\n" "$UURB_MANAGER_WINDOW_ID" "$UURB_MANAGER_EXPECTED_DISPLAY" "$UURB_MANAGER_CAPTURE_LOG" "$LD_PRELOAD" "$UURB_MANAGER_CAPTURE_READY_FILE" "$UURB_MANAGER_CAPTURE_NONCE" > "$CAPTURE_ENVIRONMENT"
''')
            server.chmod(0o700)
            manager = SOURCE.split('open_window() {', 1)[1].split('serve_console() {', 1)[0]
            command = manager.split('    /usr/bin/env -u WAYLAND_DISPLAY XDG_SESSION_TYPE=x11 \\\n', 1)[1].split('    window_vnc_pid=$!', 1)[0]
            command = command.replace('/usr/bin/x11vnc', str(server))
            script = 'exec 9>/dev/null\nLD_PRELOAD="$EXISTING_PRELOAD"\n/usr/bin/env -u WAYLAND_DISPLAY XDG_SESSION_TYPE=x11 \\\n' + command + 'wait "$!"\n'
            script = 'window_poll_options=(-scale_cursor 1)\n' + script
            env = dict(os.environ, state_dir=temp, bridge_display=":99", bridge_xauthority="/none",
                       client_window="100", window_scale="2", window_port="5921",
                       CAPTURE_ARGUMENTS=str(arguments), CAPTURE_ENVIRONMENT=str(environment),
                       manager_capture=str(module), window_capture_ready_file=f"{temp}/capture-ready.test",
                       window_capture_nonce="test-nonce")
            env.pop("LD_PRELOAD", None)
            for previous in ("", str(module)):
                with self.subTest(previous_preload=previous):
                    env["EXISTING_PRELOAD"] = previous
                    result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=5)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(environment.read_text().splitlines(), [
                        "100", ":99", f"{temp}/window-capture.log",
                        str(module) + (":" + previous if previous else ""),
                        f"{temp}/capture-ready.test", "test-nonce",
                    ])
            received = arguments.read_text().splitlines()
            self.assertIn("-nosel", received)
            self.assertIn("-norc", received)
            self.assertIn("-nothreads", received)
            self.assertEqual(received[received.index("-sid") + 1], "100")
            self.assertEqual(received[received.index("-rfbport") + 1], "5921")
            self.assertEqual(received[received.index("-scale_cursor") + 1], "1")
            self.assertNotIn("-scale_cursor", SOURCE.split('serve_console() {', 1)[1])
            self.assertNotIn("-nosel", SOURCE.split('open_window() {', 1)[0])
            self.assertNotIn("-nosel", SOURCE.split('serve_console() {', 1)[1])
            self.assertNotIn("-norc", SOURCE.split('open_window() {', 1)[0])
            self.assertNotIn("-norc", SOURCE.split('serve_console() {', 1)[1])
            self.assertNotIn("UURB_MANAGER_WINDOW_ID=", SOURCE.split('open_window() {', 1)[0])
            self.assertNotIn("LD_PRELOAD=", SOURCE.split('serve_console() {', 1)[1])

    def test_manager_listener_is_insufficient_without_exact_current_capture_handshake(self):
        nonce="6c533be1-0b75-4bc6-854a-ae3c93a33a08"
        cases = {
            "valid": f"{nonce} PID 100 :99\n",
            "stale-pid": f"{nonce} 99999999 100 :99\n",
            "stale-nonce": "old-nonce PID 100 :99\n",
            "wrong-window": f"{nonce} PID 200 :99\n",
            "wrong-display": f"{nonce} PID 100 :20\n",
            "extra-field": f"{nonce} PID 100 :99 extra\n",
            "extra-line": f"{nonce} PID 100 :99\nextra\n",
            "extra-blank-line": f"{nonce} PID 100 :99\n\n",
            "fragment-after-line": f"{nonce} PID 100 :99\nx",
            "truncated": f"{nonce} PID 100",
            "oversized": "x" * 10000,
            "missing": None,
            "linked": f"{nonce} PID 100 :99\n",
        }
        manager=SOURCE.split('open_window() {', 1)[1].split('serve_console() {', 1)[0]
        loop=manager.split('    for _ in {1..80}; do', 1)[1].split('    if [[ "$ready" != true ]]; then', 1)[0]
        for name,content in cases.items():
            with self.subTest(name=name):
                commands=f'''
window_vnc_pid=$$
window_port=5921
window_capture_nonce={nonce}
client_window=100
bridge_display=:99
window_capture_ready_file="$runtime_dir/capture-ready.$window_capture_nonce"
mkdir -p "$runtime_dir"
'''
                if content is not None:
                    # PID is replaced by the test shell, never a real sidecar.
                    commands += 'printf "%s" "$HANDSHAKE" > "$runtime_dir/handshake-content"\n'
                    commands += 'sed "s/PID/$$/g" "$runtime_dir/handshake-content" > "$window_capture_ready_file"\n'
                    if name=="linked":
                        commands += 'mv "$window_capture_ready_file" "$runtime_dir/linked-target"\nln -s "$runtime_dir/linked-target" "$window_capture_ready_file"\n'
                commands += '''
listener_present() { return 0; }
sleep() { :; }
ready=false
for _ in {1..80}; do
''' + loop + '\n[[ "$ready" == "' + ("true" if name=="valid" else "false") + '" ]]\n'
                with patch.dict(os.environ, {"HANDSHAKE": content or ""}):
                    result,_,_=self.run_helpers("none",commands)
                self.assertEqual(result.returncode,0,result.stderr)

    def test_capture_handshake_cleanup_removes_only_this_sessions_marker(self):
        commands='''
focus_client
mkdir -p "$runtime_dir"
window_capture_ready_file="$runtime_dir/capture-ready.this-session"
printf 'owned\\n' > "$window_capture_ready_file"
printf 'other-session\\n' > "$runtime_dir/capture-ready.other-session"
cleanup_window
[[ ! -e "$window_capture_ready_file" ]]
[[ $(cat "$runtime_dir/capture-ready.other-session") == other-session ]]
'''
        result,_,lease=self.run_helpers("rdp",commands)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertFalse(lease)

    def test_missing_manager_capture_fails_before_starting_bridge(self):
        result, calls, lease = self.run_helpers("none", '''
notify_error() { printf '%s\\n' "$1" >&2; }
ensure_bridge() { printf 'unexpected bridge start\\n' >&2; return 0; }
open_window
''')
        self.assertEqual(result.returncode, 1)
        self.assertIn("manager capture module is missing", result.stderr)
        self.assertNotIn("unexpected bridge start", result.stderr)
        self.assertEqual(calls, "")
        self.assertFalse(lease)

    def test_manager_module_build_install_and_digest_are_connected(self):
        with tempfile.TemporaryDirectory(prefix="uu-manager-build-test-") as temp:
            directory = Path(temp)
            compiler = directory / "compiler"
            arguments = directory / "arguments"
            compiler.write_text('''#!/usr/bin/python3
import json, os, sys
with open(os.environ['BUILD_ARGUMENTS'], 'a') as stream:
    stream.write(json.dumps(sys.argv[1:]) + '\\n')
''')
            compiler.chmod(0o700)
            env = dict(os.environ, BUILD_ARGUMENTS=str(arguments))
            for variable in ("MINGW_CC", "MINGW_STRIP", "WINEGCC", "HOST_CC", "HOST_STRIP"):
                env[variable] = str(compiler)
            result = subprocess.run(["bash", str(REPOSITORY / "scripts/build-compat.sh"),
                                     str(directory / "output")], env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            commands = [json.loads(line) for line in arguments.read_text().splitlines()]
            build = next(command for command in commands if str(REPOSITORY / "src/uu_manager_capture.c") in command)
            for argument in ("-shared", "-fPIC", "-std=c11", "-Werror", "-Wpedantic", "-ldl", "-pthread"):
                self.assertIn(argument, build)
            self.assertEqual(build[build.index("-o") + 1], str(directory / "output/uu-manager-capture.so"))
            self.assertTrue(any(str(directory / "output/uu-manager-capture.so") in command
                                and "-o" not in command for command in commands))
            (directory / "output/uu-manager-capture.so").write_bytes(b"isolated module fixture")
            prefix = directory / "prefix"
            (prefix / "compat").mkdir(parents=True)
            installer = (REPOSITORY / "install.sh").read_text()
            command = installer.split('install -m 0755 "$compat_build/uu-manager-capture.so"', 1)[1].split('\ninstall ', 1)[0]
            result = subprocess.run(["bash", "-c", 'install -m 0755 "$compat_build/uu-manager-capture.so"' + command],
                                    env=dict(os.environ, compat_build=str(directory / "output"), wine_prefix=str(prefix)),
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((prefix / "compat/uu-manager-capture.so").read_bytes(), b"isolated module fixture")
            digest = (REPOSITORY / "scripts/runtime-source-digest").read_text()
            self.assertIn("src/uu_manager_capture.c", digest)
            self.assertIn("src/x11_manager_capture_api.h", digest)

    def run_helpers(self, mode, commands, prefix_source="default"):
        with tempfile.TemporaryDirectory(prefix="uu-focus-test-") as temp:
            directory = Path(temp)
            xdo = directory / "xdotool"
            xdo.write_text('''#!/usr/bin/env bash
case "$*" in
  *search*gameviewer*)
    printf '100\\n200\\n'
    [[ "$FOCUS_TEST_MODE" != hidden-with-managed ]] || printf '400\\n';;
  *search*TigerVNC*) printf '300\\n';;
  *getwindowname*300*) printf 'UU Remote - TigerVNC\\n';;
  *getwindowname*200*)
    [[ "$FOCUS_TEST_MODE" != uu-device ]] || { printf 'UU Laptop\\n'; exit; }
    printf 'UU Remote\\n';;
  *getwindowname*) printf 'UU Remote\\n';;
  *search*Ubuntu-Desktop-Relay*)
    [[ "$FOCUS_TEST_MODE" == rdp ]] || exit 1
    printf '101\\n';;
  *search*realvnc-vncviewer*)
    [[ "$FOCUS_TEST_MODE" == vnc ]] || exit 1
    printf '202\\n';;
  *) printf '%s %s\\n' "${DISPLAY:-}" "$*" >> "$FOCUS_TEST_LOG";;
esac
''')
            xprop = directory / "xprop"
            xprop.write_text('''#!/usr/bin/env bash
if [[ "$*" == *100* && ( "$FOCUS_TEST_MODE" != hidden* || -e "$FOCUS_RESUMED" ) ]]; then
    printf 'WM_STATE(WM_STATE): window state: Normal\\n'
elif [[ "$*" == *400* || ( "$FOCUS_TEST_MODE" == uu-device && "$*" == *200* ) ]]; then
    printf 'WM_STATE(WM_STATE): window state: Normal\\n'
else
    printf 'WM_STATE: not found.\\n'
fi
if [[ "$FOCUS_TEST_MODE" == hidden-not-normal ]]; then
    printf '_NET_WM_WINDOW_TYPE(ATOM) = _NET_WM_WINDOW_TYPE_NOTIFICATION\\n'
else
    printf '_NET_WM_WINDOW_TYPE(ATOM) = _NET_WM_WINDOW_TYPE_NORMAL\\n'
fi
''')
            xinfo = directory / "xwininfo"
            xinfo.write_text('''#!/usr/bin/env bash
width=920 height=680 override=no mapped=IsViewable
[[ "$FOCUS_TEST_MODE" != hidden* ]] || mapped=IsUnMapped
[[ ! -e "$FOCUS_RESUMED" ]] || mapped=IsViewable
if [[ "$*" == *200* || "$FOCUS_TEST_MODE" == hidden-small ]]; then
    width=260 height=55
fi
[[ "$FOCUS_TEST_MODE" != hidden-override ]] || override=yes
[[ "$FOCUS_TEST_MODE" != hidden-mapped ]] || mapped=IsViewable
printf '  Width: %s\\n  Height: %s\\n  Map State: %s\\n  Override Redirect State: %s\\n' "$width" "$height" "$mapped" "$override"
''')
            wine = directory / "fake-wine"
            wine.write_text('''#!/usr/bin/env bash
[[ ! -e /proc/$$/fd/9 ]] || exit 90
[[ $(readlink /proc/$$/fd/0) == /dev/null ]] || exit 91
printf 'resume %s %s %s %s %s %s\\n' "$DISPLAY" "$XAUTHORITY" "$WINEPREFIX" "$LIBGL_ALWAYS_SOFTWARE" "$XDG_SESSION_TYPE" "${WAYLAND_DISPLAY:-unset}" >> "$FOCUS_TEST_LOG"
printf 'wine-args %s\\n' "$*" >> "$FOCUS_TEST_LOG"
touch "$FOCUS_RESUMED"
''')
            xdo.chmod(0o700)
            xprop.chmod(0o700)
            xinfo.chmod(0o700)
            wine.chmod(0o700)
            # Source only definitions, substitute private fake X executables,
            # and override discovery so the real X server is never used.
            script = SOURCE.split('case "${1:-open}" in', 1)[0]
            script = script.replace("/usr/bin/xdotool", str(xdo)).replace("/usr/bin/xprop", str(xprop))
            script = script.replace("/usr/bin/xwininfo", str(xinfo))
            script = script.replace("/opt/wine-stable/bin/wine", str(wine))
            script += '\ndiscover_bridge() { bridge_display=:99; bridge_xauthority=/none; }\n'
            script += commands
            env = dict(os.environ, HOME=temp, DISPLAY=":77", XDG_RUNTIME_DIR=temp, XDG_STATE_HOME=temp,
                       FOCUS_TEST_MODE=mode, FOCUS_TEST_LOG=str(directory / "calls"),
                       FOCUS_RESUMED=str(directory / "resumed"), WAYLAND_DISPLAY="wayland-0")
            env.pop("WINEPREFIX", None)
            env.pop("UURB_WINEPREFIX", None)
            prefix = directory / ".local/share/wineprefixes/uu-remote"
            if prefix_source != "default":
                prefix = directory / prefix_source
                config = directory / ".config/uu-remote-bridge"
                config.mkdir(parents=True)
                config.joinpath("environment").write_text(f"UURB_WINEPREFIX={prefix}\n")
                if prefix_source == "wine":
                    env["WINEPREFIX"] = str(prefix)
                    config.joinpath("environment").write_text("UURB_WINEPREFIX=/unused-saved\n")
                elif prefix_source == "override":
                    env["UURB_WINEPREFIX"] = str(prefix)
                    env["WINEPREFIX"] = "/unused-wine"
                    config.joinpath("environment").write_text("UURB_WINEPREFIX=/unused-saved\n")
            executable = prefix / "drive_c/Program Files/Netease/GameViewer/GameViewer.exe"
            executable.parent.mkdir(parents=True)
            executable.touch()
            result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=10)
            calls = (directory / "calls").read_text() if (directory / "calls").exists() else ""
            return result, calls, (directory / "uu-remote-bridge/console-focus").exists()

    def test_restores_rdp(self):
        result, calls, lease = self.run_helpers("rdp", "focus_client; release_client")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("windowactivate 101", calls)
        self.assertFalse(lease)

    def test_device_name_containing_uu_cannot_replace_the_manager_lobby(self):
        result, calls, _ = self.run_helpers("uu-device", "focus_client")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(":99 windowmap 100 windowactivate --sync 100", calls)
        self.assertNotIn("windowactivate --sync 200", calls)

    def test_restores_native_vnc_and_filters_toast(self):
        result, calls, lease = self.run_helpers("vnc", "focus_client; release_client")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("windowmap 100 windowactivate --sync 100", calls)
        self.assertNotIn("200", calls)
        self.assertIn("windowactivate 202", calls)
        self.assertFalse(lease)

    def test_reopens_unmapped_main_window_without_mapping_small_toast(self):
        result, calls, lease = self.run_helpers("hidden", "focus_client")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("windowmap 100 windowactivate --sync 100", calls)
        self.assertIn("resume :99 /none", calls)
        self.assertIn("1 x11 unset", calls)
        self.assertIn("wine-args start /unix", calls)
        self.assertNotIn("200", calls)
        self.assertTrue(lease)

    def test_hidden_resume_uses_configured_prefix_precedence(self):
        for source in ("default", "saved", "wine", "override"):
            with self.subTest(source=source):
                result, calls, _ = self.run_helpers("hidden", "exec 9>/dev/null; focus_client", source)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("resume :99 /none", calls)
                self.assertNotIn("/unused-", calls)
                expected = ".local/share/wineprefixes/uu-remote" if source == "default" else source
                self.assertRegex(calls, rf"resume :99 /none [^\n]+/{expected} 1 x11 unset")

    def test_visible_window_does_not_start_another_application(self):
        result, calls, _ = self.run_helpers("rdp", "focus_client; focus_client")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("resume ", calls)
        self.assertNotIn("wine-args", calls)

    def test_hidden_application_is_resumed_only_once_after_becoming_visible(self):
        result, calls, _ = self.run_helpers("hidden", "focus_client; focus_client")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls.count("resume :99 /none"), 1)

    def test_prefers_managed_window_over_unmapped_fallback(self):
        result, calls, _ = self.run_helpers("hidden-with-managed", "focus_client")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("windowmap 400 windowactivate --sync 400", calls)
        self.assertNotIn("windowmap 100", calls)

    def test_unmapped_small_and_override_windows_are_not_mapped(self):
        for mode in ("hidden-small", "hidden-override", "hidden-not-normal", "hidden-mapped"):
            with self.subTest(mode=mode):
                result, calls, _ = self.run_helpers(mode, "focus_client")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("windowmap", calls)

    def test_reopening_viewer_refocuses_private_client_and_preserves_owner_lease(self):
        commands = '''
mkdir -p "$bridge_runtime_dir"
printf '424242\\n' >"$bridge_focus_file"
activate_existing_window
[[ $(cat "$bridge_focus_file") == 424242 ]]
'''
        result, calls, lease = self.run_helpers("rdp", commands)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(lease)
        self.assertEqual(calls.splitlines(), [
            ":99 windowmap 100 windowactivate --sync 100",
            ":77 windowactivate --sync 300",
        ])

    def test_new_session_claims_lease_and_focuses_private_client(self):
        result, calls, lease = self.run_helpers(
            "rdp", 'focus_client; [[ $(cat "$bridge_focus_file") == "$$" ]]',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(lease)
        self.assertIn(":99 windowmap 100 windowactivate --sync 100", calls)

    def test_reopening_isolated_manager_keeps_relay_focused_without_lease(self):
        result, calls, lease = self.run_helpers("rdp", "activate_existing_window")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(lease)
        self.assertEqual(calls.splitlines(), [
            ":99 windowactivate 101",
            ":77 windowactivate --sync 300",
        ])

    def test_missing_relay_still_releases_lease(self):
        result, calls, lease = self.run_helpers("none", "focus_client; release_client")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("windowactivate 101", calls)
        self.assertNotIn("windowactivate 202", calls)
        self.assertFalse(lease)

    def test_window_cleanup_reaps_its_compositor_and_releases_lease(self):
        unrelated = subprocess.Popen(["sleep", "60"])
        try:
            for ignore_term in (False, True):
                with self.subTest(ignore_term=ignore_term):
                    startup = 'trap "" TERM; ' if ignore_term else ''
                    commands = '''
focus_client
mkdir -p "$runtime_dir"
printf '5923\\n' >"$runtime_dir/window.port"
bash -c 'STARTUPtouch "$1"; exec sleep 60' probe "$runtime_dir/compositor-ready" &
window_compositor_pid=$!
trap 'kill -KILL "$window_compositor_pid" 2>/dev/null || true' EXIT
for _ in {1..100}; do
    [[ -e "$runtime_dir/compositor-ready" ]] && break
    sleep 0.01
done
[[ -e "$runtime_dir/compositor-ready" ]]
window_owner_start=$(process_start_time "$$")
save_window_session
cleanup_window
! kill -0 "$window_compositor_pid" 2>/dev/null
[[ ! -e "$runtime_dir/window.port" ]]
[[ ! -e "$window_session_file" ]]
'''.replace('STARTUP', startup)
                    result, calls, lease = self.run_helpers("rdp", commands)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertFalse(lease)
                    self.assertIn("windowactivate 101", calls)
                    self.assertIsNone(unrelated.poll())
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=3)

    def test_orphaned_window_cleanup_checks_identity_and_live_viewer(self):
        for scenario in ("orphan", "live-viewer", "reused-owner", "reused-pid", "wrong-display", "wrong-auth", "wrong-name"):
            with self.subTest(scenario=scenario):
                commands = '''
mkdir -p "$runtime_dir" "$bridge_runtime_dir"
cat >"$runtime_dir/sidecar.py" <<'PY'
import ctypes
import os
from pathlib import Path
import sys
import time
ctypes.CDLL(None).prctl(15, sys.argv[1].encode(), 0, 0, 0)
Path(sys.argv[2]).write_text(str(os.getpid()))
time.sleep(60)
PY
declare -f process_start_time save_window_session >"$runtime_dir/functions"
cat >"$runtime_dir/owner.sh" <<'SH'
set -Eeuo pipefail
runtime_dir="$1"
window_session_file="$runtime_dir/window-session"
source "$runtime_dir/functions"
bridge_display=:99 bridge_xauthority=/none
window_owner_start=$(process_start_time "$$")
exec 9>"$runtime_dir/window.lock"
flock -n 9
display=:99 auth=/none name=xcompmgr
[[ "$2" != wrong-display ]] || display=:88
[[ "$2" != wrong-auth ]] || auth=/other
[[ "$2" != wrong-name ]] || name=sleep
DISPLAY="$display" XAUTHORITY="$auth" python3 "$runtime_dir/sidecar.py" "$name" "$runtime_dir/compositor.pid" 9>&- &
window_compositor_pid=$!
DISPLAY=:99 XAUTHORITY=/none python3 "$runtime_dir/sidecar.py" x11vnc "$runtime_dir/vnc.pid" 9>&- &
window_vnc_pid=$!
python3 "$runtime_dir/sidecar.py" vncviewer "$runtime_dir/viewer.pid" 9>&- &
window_viewer_pid=$!
for _ in {1..100}; do
    [[ -e "$runtime_dir/compositor.pid" && -e "$runtime_dir/vnc.pid" && -e "$runtime_dir/viewer.pid" ]] && break
    sleep 0.01
done
save_window_session
if [[ "$2" != live-viewer ]]; then
    kill "$window_viewer_pid"
    wait "$window_viewer_pid" || true
fi
touch "$runtime_dir/owner-ready"
wait
SH
DISPLAY=:99 XAUTHORITY=/none python3 "$runtime_dir/sidecar.py" xcompmgr "$runtime_dir/external.pid" &
bash "$runtime_dir/owner.sh" "$runtime_dir" SCENARIO &
owner=$!
trap 'kill -KILL "$owner" 2>/dev/null || true; for file in "$runtime_dir"/*.pid; do kill -KILL "$(cat "$file")" 2>/dev/null || true; done' EXIT
for _ in {1..100}; do
    [[ -e "$runtime_dir/owner-ready" && -e "$runtime_dir/external.pid" ]] && break
    sleep 0.01
done
[[ -e "$runtime_dir/owner-ready" && -e "$runtime_dir/external.pid" ]]
kill -KILL "$owner"
wait "$owner" 2>/dev/null || true
exec 9>"$window_lock_file"
flock -n 9
discover_bridge
if [[ SCENARIO == reused-pid ]]; then
    sed -i '6s/.*/1/' "$window_session_file"
elif [[ SCENARIO == reused-owner ]]; then
    sed -i "1s/.*/$$/;2s/.*/1/" "$window_session_file"
fi
if [[ SCENARIO == live-viewer ]]; then
    ! cleanup_orphaned_window
    kill -0 "$(cat "$runtime_dir/viewer.pid")"
    kill -0 "$(cat "$runtime_dir/compositor.pid")"
    kill -0 "$(cat "$runtime_dir/vnc.pid")"
    [[ -e "$window_session_file" ]]
else
    cleanup_orphaned_window
    [[ ! -e "$window_session_file" ]]
    sleep 0.05
    vnc=$(cat "$runtime_dir/vnc.pid")
    [[ ! -e "/proc/$vnc/stat" || $(awk '{print $3}' "/proc/$vnc/stat") == Z ]]
    compositor=$(cat "$runtime_dir/compositor.pid")
    if [[ SCENARIO == orphan || SCENARIO == reused-owner ]]; then
        [[ ! -e "/proc/$compositor/stat" || $(awk '{print $3}' "/proc/$compositor/stat") == Z ]]
    else
        kill -0 "$compositor"
    fi
fi
kill -0 "$(cat "$runtime_dir/external.pid")"
'''.replace("SCENARIO", scenario)
                result, _, _ = self.run_helpers("rdp", commands)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_cleanup_preserves_another_window_session_record(self):
        commands = '''
mkdir -p "$runtime_dir"
printf '123\\n456\\n' >"$window_session_file"
cleanup_window
[[ $(cat "$window_session_file") == $'123\\n456' ]]
'''
        result, _, _ = self.run_helpers("rdp", commands)
        self.assertEqual(result.returncode, 0, result.stderr)

    def run_stuck_window_takeover(self, isolated=False, live_viewer=False):
        unrelated = subprocess.Popen(["sleep", "60"])
        try:
            commands = '''
mkdir -p "$runtime_dir" "$bridge_runtime_dir"
cat >"$runtime_dir/sidecar.py" <<'PY'
import ctypes
import os
from pathlib import Path
import sys
import time
ctypes.CDLL(None).prctl(15, sys.argv[1].encode(), 0, 0, 0)
Path(sys.argv[2]).write_text(str(os.getpid()))
time.sleep(60)
PY
python3 "$runtime_dir/sidecar.py" xcompmgr "$runtime_dir/external.pid" &
declare -f process_start_time save_window_session >"$runtime_dir/session-functions"
bash -c '
    source "$1/session-functions"
    runtime_dir="$1" window_session_file="$1/window-session"
    bridge_display=:99 bridge_xauthority=/none
    window_owner_start=$(process_start_time "$$")
    exec 9>"$1/window.lock"
    flock -n 9 || exit 1
    printf "%s\\n" "$BASHPID" >"$2"
    DISPLAY=:99 XAUTHORITY=/none python3 "$1/sidecar.py" xcompmgr "$1/compositor.pid" &
    DISPLAY=:99 XAUTHORITY=/none python3 "$1/sidecar.py" x11vnc "$1/vnc.pid" &
    DISPLAY=:99 XAUTHORITY=/none python3 "$1/sidecar.py" vncviewer "$1/viewer.pid" &
    viewer=$!
    for _ in {1..100}; do
        [[ -e "$1/compositor.pid" && -e "$1/vnc.pid" && -e "$1/viewer.pid" ]] && break
        sleep 0.01
    done
    window_compositor_pid=$(cat "$1/compositor.pid")
    window_vnc_pid=$(cat "$1/vnc.pid")
    window_viewer_pid=$viewer
    save_window_session
    if [[ LIVE_VIEWER == false ]]; then
        kill "$viewer"
        wait "$viewer" || true
    fi
    touch "$1/owner-ready"
    wait
' uu-remote-console "$runtime_dir" "$bridge_focus_file" &
owner=$!
trap 'kill -KILL "$owner" 2>/dev/null || true; for file in "$runtime_dir"/*.pid; do kill -KILL "$(cat "$file")" 2>/dev/null || true; done' EXIT
for _ in {1..100}; do
    [[ -e "$runtime_dir/owner-ready" && -e "$runtime_dir/external.pid" ]] && break
    sleep 0.01
done
[[ -e "$runtime_dir/owner-ready" ]]
[[ -e "$runtime_dir/external.pid" ]]
! flock -n "$window_lock_file" true
if [[ ISOLATED == true ]]; then
    rm -f "$bridge_focus_file"
    discover_bridge
fi
if [[ LIVE_VIEWER == true ]]; then
    ! takeover_stuck_window
    kill -0 "$owner"
    kill -0 "$(cat "$runtime_dir/viewer.pid")"
    kill -0 "$(cat "$runtime_dir/vnc.pid")"
    ! flock -n "$window_lock_file" true
    exit 0
fi
takeover_stuck_window
wait "$owner" 2>/dev/null || true
flock -n "$window_lock_file" true
kill -0 "$(cat "$runtime_dir/external.pid")"
for file in "$runtime_dir/compositor.pid" "$runtime_dir/vnc.pid"; do
    pid=$(cat "$file")
    if kill -0 "$pid" 2>/dev/null; then
        [[ $(awk '{print $3}' "/proc/$pid/stat") == Z ]]
    fi
done
'''
            commands = commands.replace("ISOLATED", str(isolated).lower()).replace("LIVE_VIEWER", str(live_viewer).lower())
            result, _, _ = self.run_helpers("rdp", commands)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIsNone(unrelated.poll())
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=3)

    def test_stuck_window_takeover_kills_owned_sidecars_and_releases_lock(self):
        self.run_stuck_window_takeover()

    def test_isolated_stuck_window_takeover_uses_session_when_lease_is_absent(self):
        self.run_stuck_window_takeover(isolated=True)

    def test_isolated_takeover_preserves_a_live_viewer_and_its_sidecars(self):
        self.run_stuck_window_takeover(isolated=True, live_viewer=True)


if __name__ == "__main__":
    unittest.main()
