"""Exercise helper socket/child code and clipboard owners on a private Xvfb."""
import ctypes
import errno
import json
import os
from pathlib import Path
import select
import signal
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
MAGIC = 0x58315255
HANDSHAKE = struct.pack("=II64s", MAGIC, 3, b"a" * 64)
HEADER = struct.pack("=IIII", MAGIC, 7, 1, 0)
# Unsupported event verifies framing and replies without injecting any input.
EVENT = struct.pack("=IIiiIHH", 99, 0, 0, 0, 0, 0, 0)

HARNESS = r'''
#define main helper_main
#include "helper.c"
#undef main

static pid_t fixture_owner(volatile sig_atomic_t *owner,
                           volatile sig_atomic_t *status_fd)
{
    int descriptors[2];
    if (pipe(descriptors) != 0) exit(10);
    pid_t child = fork();
    if (child < 0) exit(11);
    if (child == 0) {
        close(descriptors[0]);
        if (owner == &clipboard_owner_pid && getenv("UURB_FIXTURE_EXEC_FAIL")) {
            execl("/dev/null/uu-reaping-missing", "uu-reaping-missing", (char *)NULL);
            _exit(127);
        }
        for (;;) pause();
    }
    close(descriptors[1]);
    *owner = child;
    *status_fd = descriptors[0];
    return child;
}

static void fixture_stop(pid_t child)
{
    pid_t result;
    do { result = waitpid(child, NULL, WNOHANG); }
    while (result < 0 && errno == EINTR);
    if (result != 0) return;
    kill(child, SIGTERM);
    while (waitpid(child, NULL, 0) < 0 && errno == EINTR) {}
}

int main(void)
{
    struct sigaction action = { .sa_handler = handle_signal };
    struct sockaddr_in address = { .sin_family = AF_INET,
                                  .sin_addr.s_addr = htonl(INADDR_LOOPBACK) };
    socklen_t length = sizeof(address);
    x11_api api = {0};
    Display *display = NULL;
    char token[65];
    pid_t clipboard, primary;
    if (getenv("UURB_FIXTURE_REAL_XCLIP")) {
        unsigned long clipboard_baseline, primary_baseline;
        const char text[] = "fixture 中文 café Ω";
        if (!load_x11_api(&api) || !(display = api.open_display(NULL)) ||
            !start_clipboard_owner(&api, display, text, strlen(text),
                                   &clipboard_baseline, &primary_baseline)) return 14;
        clipboard = clipboard_owner_pid;
        primary = primary_owner_pid;
    } else {
        clipboard = fixture_owner(&clipboard_owner_pid, &clipboard_status_fd);
        primary = fixture_owner(&primary_owner_pid, &primary_status_fd);
    }
    if (getenv("UURB_FIXTURE_ALREADY_REAPED")) {
        kill(clipboard, SIGTERM);
        while (waitpid(clipboard, NULL, 0) < 0 && errno == EINTR) {}
    }
    int clipboard_fd = clipboard_status_fd;
    int primary_fd = primary_status_fd;
    pid_t unrelated = fork();
    if (unrelated < 0) return 12;
    if (unrelated == 0) _exit(23);
    if (!display) {
        clipboard_status.next_request_number = 9;
        primary_status.next_request_number = 11;
    }
    memset(token, 'a', 64);
    token[64] = '\0';
    sigemptyset(&action.sa_mask);
    sigaction(SIGTERM, &action, NULL);
    sigaction(SIGINT, &action, NULL);
    signal(SIGPIPE, SIG_IGN);
    listener_fd = create_listener(getenv("UURB_FIXTURE_READY"));
    if (listener_fd < 0 ||
        getsockname(listener_fd, (void *)&address, &length) != 0) return 13;
    printf("{\"port\":%u,\"clipboard\":%d,\"primary\":%d,\"unrelated\":%d}\n",
           ntohs(address.sin_port), clipboard, primary, unrelated);
    fflush(stdout);
    if (getenv("UURB_FIXTURE_STALE_READY")) {
        int queued = socket(AF_INET, SOCK_STREAM, 0);
        if (queued < 0 || connect(queued, (void *)&address, length) != 0 ||
            !wait_input_ready(listener_fd)) return 15;
        int consumed = accept(listener_fd, NULL, NULL);
        if (consumed < 0) return 16;
        bool accepted_blocking = !(fcntl(consumed, F_GETFL) & O_NONBLOCK);
        close(consumed);
        close(queued);
        /* Force stale readiness: the queue just reported readable is empty.
         * Alarm bounds the old blocking-listener negative control. */
        sigaction(SIGALRM, &action, NULL);
        alarm(1);
        uint64_t started = monotonic_milliseconds();
        int empty = accept(listener_fd, NULL, NULL);
        int empty_errno = errno;
        uint64_t elapsed = monotonic_milliseconds() - started;
        alarm(0);
        printf("{\"empty_result\":%d,\"empty_errno\":%d,"
               "\"elapsed_ms\":%llu,\"accepted_blocking\":%d}\n",
               empty, empty_errno, (unsigned long long)elapsed, accepted_blocking);
        fflush(stdout);
        if (empty >= 0) close(empty);
    }
    while (!stop_requested && wait_input_ready(listener_fd)) {
        int client = accept(listener_fd, NULL, NULL);
        if (client < 0 && (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK))
            continue;
        if (client >= 0) {
            active_client_fd = client;
            serve_client(client, token, &api, display, display, 0);
            if (active_client_fd >= 0) close(client);
            active_client_fd = -1;
        }
        break;
    }
    int unrelated_status = 0;
    pid_t waited = waitpid(unrelated, &unrelated_status, 0);
    printf("{\"clipboard_pid\":%d,\"primary_pid\":%d,"
           "\"clipboard_fd_closed\":%d,\"primary_fd_closed\":%d,"
           "\"clipboard_status\":%lu,\"primary_status\":%lu,"
           "\"unrelated_status\":%d,\"stopped\":%d}\n",
           (int)clipboard_owner_pid, (int)primary_owner_pid,
           fcntl(clipboard_fd, F_GETFD) < 0,
           fcntl(primary_fd, F_GETFD) < 0,
           clipboard_status.next_request_number, primary_status.next_request_number,
           waited == unrelated && WIFEXITED(unrelated_status) ?
               WEXITSTATUS(unrelated_status) : -1, (int)stop_requested);
    fflush(stdout);
    /* Fixture-only cleanup also handles handler-cleared PIDs on SIGTERM. */
    fixture_stop(clipboard);
    fixture_stop(primary);
    clipboard_owner_pid = -1;
    primary_owner_pid = -1;
    stop_clipboard_owner();
    if (listener_fd >= 0) close(listener_fd);
    if (display) {
        api.close_display(display);
        unload_x11_api(&api);
    }
    return 0;
}
'''


class X11ChildReapingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="x11-reaping-")
        cls.addClassCleanup(cls.directory.cleanup)
        work = Path(cls.directory.name)
        source = Path(os.environ.get("UURB_X11_REAP_TEST_SOURCE", ROOT / "src/uu_x11_input.c")).read_text()
        # The optional old-source control exercises its actual blocking recv
        # and accept, rather than synthesizing an alternative reaper.
        if "static bool wait_input_ready(" not in source:
            source += "\nstatic bool wait_input_ready(int fd) { (void)fd; return !stop_requested; }\n"
        (work / "helper.c").write_text(source)
        (work / "harness.c").write_text(HARNESS)
        cls.executable = work / "harness"
        subprocess.run(["gcc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-I", str(ROOT / "src"), str(work / "harness.c"),
                        "-ldl", "-o", str(cls.executable)], check=True, capture_output=True)

    def setUp(self):
        self.work = tempfile.TemporaryDirectory(dir=self.directory.name)
        self.addCleanup(self.work.cleanup)
        env = os.environ.copy()
        env["UURB_FIXTURE_READY"] = str(Path(self.work.name) / "ready")
        if self._testMethodName == "test_failed_exec_reaped_while_idle":
            env["UURB_FIXTURE_EXEC_FAIL"] = "1"
        if self._testMethodName == "test_echild_closes_stale_status_fd":
            env["UURB_FIXTURE_ALREADY_REAPED"] = "1"
        if self._testMethodName == "test_stale_readiness_accept_returns_eagain_then_recovers":
            env["UURB_FIXTURE_STALE_READY"] = "1"
        if self._testMethodName == "test_real_xclip_ownership_survives_idle_then_exit_is_reaped":
            self.start_private_xvfb()
            env["DISPLAY"] = self.display_name
            env.pop("XAUTHORITY", None)
            env["UURB_FIXTURE_REAL_XCLIP"] = "1"
        self.process = subprocess.Popen([str(self.executable)], stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, bufsize=0, env=env)
        self.addCleanup(self.cleanup)
        self.connection = None
        self.info = self.read_json()
        self.identities = {name: self.identity(self.info[name])
                           for name in ("clipboard", "primary", "unrelated")}

    def read_json(self):
        self.assertTrue(select.select([self.process.stdout], [], [], 3)[0], "fixture timeout")
        return json.loads(self.process.stdout.readline())

    def start_private_xvfb(self):
        log = (Path(self.work.name) / "xvfb.log").open("w")
        self.addCleanup(log.close)
        self.xvfb = subprocess.Popen(["Xvfb", "-displayfd", "1", "-screen", "0",
                                     "320x240x24", "-nolisten", "tcp", "-extension", "GLX", "-ac"],
                                    stdout=subprocess.PIPE, stderr=log, text=True)
        self.xvfb_identity = self.identity(self.xvfb.pid)
        self.addCleanup(self.stop_private_xvfb)
        self.watchdog = threading.Timer(30, self.stop_private_xvfb)
        self.watchdog.daemon = True
        self.watchdog.start()
        self.addCleanup(self.watchdog.cancel)
        self.assertTrue(select.select([self.xvfb.stdout], [], [], 3)[0])
        number = self.xvfb.stdout.readline().strip()
        self.assertTrue(number.isdigit(), "Xvfb did not allocate an unused display")
        self.display_name = ":" + number
        self.x = ctypes.CDLL("libX11.so.6")
        for name, arguments, result in (
            ("XOpenDisplay", [ctypes.c_char_p], ctypes.c_void_p),
            ("XInternAtom", [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int], ctypes.c_ulong),
            ("XGetSelectionOwner", [ctypes.c_void_p, ctypes.c_ulong], ctypes.c_ulong),
            ("XSync", [ctypes.c_void_p, ctypes.c_int], ctypes.c_int),
            ("XCloseDisplay", [ctypes.c_void_p], ctypes.c_int),
        ):
            getattr(self.x, name).argtypes = arguments
            getattr(self.x, name).restype = result
        self.display = self.x.XOpenDisplay(self.display_name.encode())
        self.assertTrue(self.display)
        self.addCleanup(self.x.XCloseDisplay, self.display)

    def stop_private_xvfb(self):
        current = self.identity(self.xvfb.pid)
        if current and self.xvfb_identity and current[0] == self.xvfb_identity[0]:
            self.xvfb.terminate()
            try:
                self.xvfb.wait(timeout=2)
            except subprocess.TimeoutExpired:
                current = self.identity(self.xvfb.pid)
                if current and current[0] == self.xvfb_identity[0]:
                    self.xvfb.kill()
                self.xvfb.wait(timeout=2)
        if self.xvfb.stdout:
            self.xvfb.stdout.close()

    @staticmethod
    def identity(pid):
        try:
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            return fields[19], fields[0]
        except FileNotFoundError:
            return None

    def cleanup(self):
        if self.connection:
            self.connection.close()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        for name, identity in getattr(self, "identities", {}).items():
            pid = self.info[name]
            current = self.identity(pid)
            if current and identity and current[0] == identity[0] and current[1] != "Z":
                os.kill(pid, signal.SIGKILL)
        self.process.stdout.close()
        self.process.stderr.close()

    def connect(self):
        self.connection = socket.create_connection(("127.0.0.1", self.info["port"]), timeout=2)

    def receive(self, size):
        data = b""
        while len(data) < size:
            piece = self.connection.recv(size - len(data))
            self.assertTrue(piece, "unexpected protocol EOF")
            data += piece
        return data

    def authenticate(self):
        self.connection.sendall(HANDSHAKE)
        self.assertEqual(struct.unpack("=IIII", self.receive(16)), (MAGIC, 0, 1, 0))

    def reap_and_check_live_peer(self, name="clipboard"):
        os.kill(self.info[name], signal.SIGUSR1)
        deadline = time.monotonic() + 1.25
        while self.identity(self.info[name]) is not None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIsNone(self.identity(self.info[name]), "selection child remained unreaped while idle")
        peer = "primary" if name == "clipboard" else "clipboard"
        current = self.identity(self.info[peer])
        self.assertIsNotNone(current)
        self.assertEqual(current[0], self.identities[peer][0])
        self.assertNotEqual(current[1], "Z")

    def finish(self, reaped="clipboard"):
        self.connection.close()
        self.connection = None
        state = self.read_json()
        self.assertEqual(state["unrelated_status"], 23, "synchronous child status was stolen")
        self.assertEqual(state[f"{reaped}_pid"], -1)
        self.assertTrue(state[f"{reaped}_fd_closed"])
        self.assertEqual(state[f"{reaped}_status"], 0)
        peer = "primary" if reaped == "clipboard" else "clipboard"
        self.assertEqual(state[f"{peer}_pid"], self.info[peer])
        self.assertFalse(state[f"{peer}_fd_closed"])
        self.assertEqual(state[f"{peer}_status"], 11 if peer == "primary" else 9)
        self.process.wait(timeout=3)
        self.assertEqual(self.process.returncode, 0)

    def exercise(self, stage, reaped="clipboard"):
        if stage == "accept":
            self.reap_and_check_live_peer(reaped)
            self.connect()
            self.authenticate()
        else:
            self.connect()
            if stage == "handshake":
                self.connection.sendall(HANDSHAKE[:3])
                self.reap_and_check_live_peer(reaped)
                self.connection.sendall(HANDSHAKE[3:])
                self.assertEqual(struct.unpack("=IIII", self.receive(16)), (MAGIC, 0, 1, 0))
            else:
                self.authenticate()
                partial = {"idle": b"", "header": HEADER[:3], "body": HEADER + EVENT[:3]}[stage]
                self.connection.sendall(partial)
                self.reap_and_check_live_peer(reaped)
                self.connection.sendall((HEADER + EVENT)[len(partial):])
                self.assertEqual(struct.unpack("=IIII", self.receive(16)), (MAGIC, 7, 0, 0x2002))
        # A second complete frame proves the same authenticated connection stays usable.
        self.connection.sendall(HEADER + EVENT)
        self.assertEqual(struct.unpack("=IIII", self.receive(16)), (MAGIC, 7, 0, 0x2002))
        self.finish(reaped)

    def test_idle_accept_reaps_only_dead_clipboard_owner(self):
        self.exercise("accept")

    def test_idle_authenticated_recv_reaps_primary_owner(self):
        self.exercise("idle", "primary")

    def test_partial_handshake_retains_bytes(self):
        self.exercise("handshake")

    def test_partial_request_header_retains_bytes(self):
        self.exercise("header")

    def test_partial_request_body_retains_bytes(self):
        self.exercise("body")

    def test_stale_readiness_accept_returns_eagain_then_recovers(self):
        state = self.read_json()
        self.assertEqual(state["empty_result"], -1)
        self.assertIn(state["empty_errno"], (errno.EAGAIN, errno.EWOULDBLOCK))
        self.assertLess(state["elapsed_ms"], 100)
        self.assertTrue(state["accepted_blocking"])
        self.connect()
        self.authenticate()
        self.reap_and_check_live_peer()
        self.connection.sendall(HEADER + EVENT)
        self.assertEqual(struct.unpack("=IIII", self.receive(16)), (MAGIC, 7, 0, 0x2002))
        self.finish()

    def test_real_xclip_ownership_survives_idle_then_exit_is_reaped(self):
        env = {**os.environ, "DISPLAY": self.display_name}
        env.pop("XAUTHORITY", None)

        def owner(selection):
            atom = self.x.XInternAtom(self.display, selection.encode(), 0)
            self.x.XSync(self.display, 0)
            return self.x.XGetSelectionOwner(self.display, atom)

        def text(selection):
            return subprocess.check_output(["xclip", "-selection", selection, "-out"],
                                           env=env, timeout=2).decode()

        original = {name: owner(name) for name in ("CLIPBOARD", "PRIMARY")}
        self.assertTrue(all(original.values()))
        time.sleep(0.8)
        for selection, name in (("CLIPBOARD", "clipboard"), ("PRIMARY", "primary")):
            self.assertEqual(owner(selection), original[selection])
            self.assertEqual(text(selection), "fixture 中文 café Ω")
            current = self.identity(self.info[name])
            self.assertIsNotNone(current)
            self.assertEqual(current[0], self.identities[name][0])
            self.assertNotEqual(current[1], "Z")

        replacement = subprocess.Popen(["xclip", "-selection", "CLIPBOARD", "-in",
                                        "-loops", "0", "-verbose"], env=env,
                                       stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL)
        replacement_identity = self.identity(replacement.pid)

        def stop_replacement():
            current = self.identity(replacement.pid)
            if current and replacement_identity and current[0] == replacement_identity[0]:
                replacement.terminate()
                try:
                    replacement.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    replacement.kill()
                    replacement.wait(timeout=2)

        self.addCleanup(stop_replacement)
        replacement.stdin.write(b"replacement fixture")
        replacement.stdin.close()
        deadline = time.monotonic() + 1.25
        while self.identity(self.info["clipboard"]) is not None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIsNone(self.identity(self.info["clipboard"]), "real xclip remained a zombie during idle accept")
        self.assertNotEqual(owner("CLIPBOARD"), original["CLIPBOARD"])
        self.assertEqual(owner("PRIMARY"), original["PRIMARY"])
        self.assertEqual(text("PRIMARY"), "fixture 中文 café Ω")
        self.assertEqual(text("CLIPBOARD"), "replacement fixture")
        self.connect()
        self.authenticate()
        self.connection.close()
        self.connection = None
        state = self.read_json()
        self.assertEqual(state["clipboard_pid"], -1)
        self.assertTrue(state["clipboard_fd_closed"])
        self.assertEqual(state["clipboard_status"], 0)
        self.assertEqual(state["primary_pid"], self.info["primary"])
        self.assertFalse(state["primary_fd_closed"])
        self.assertEqual(state["unrelated_status"], 23)
        self.process.wait(timeout=3)
        self.assertEqual(self.process.returncode, 0)

    def test_failed_exec_reaped_while_idle(self):
        deadline = time.monotonic() + 1.25
        while self.identity(self.info["clipboard"]) is not None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIsNone(self.identity(self.info["clipboard"]))
        self.connect()
        self.authenticate()
        self.finish()
        self.assertIn("selection=clipboard exit=127", self.process.stderr.read().decode())

    def test_echild_closes_stale_status_fd(self):
        self.connect()
        self.authenticate()
        self.finish()

    def test_idle_live_owners_do_not_spin_or_exit(self):
        def cpu_ticks():
            fields = Path(f"/proc/{self.process.pid}/stat").read_text().rsplit(")", 1)[1].split()
            return int(fields[11]) + int(fields[12])

        before = cpu_ticks()
        time.sleep(0.8)
        seconds = (cpu_ticks() - before) / os.sysconf("SC_CLK_TCK")
        self.assertLess(seconds, 0.1, "idle polling consumed excessive CPU")
        for name in ("clipboard", "primary"):
            identity = self.identity(self.info[name])
            self.assertIsNotNone(identity)
            self.assertEqual(identity[0], self.identities[name][0])
            self.assertNotEqual(identity[1], "Z")
        self.connect()
        self.authenticate()
        self.reap_and_check_live_peer()
        self.finish()

    def exercise_shutdown(self, partial):
        if partial:
            self.connect()
            self.authenticate()
            self.connection.sendall(HEADER[:3])
        time.sleep(0.05)
        started = time.monotonic()
        self.process.terminate()
        state = self.read_json()
        self.process.wait(timeout=2)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(state["stopped"], 1)
        self.assertEqual(state["unrelated_status"], 23)
        self.assertEqual(self.process.returncode, 0)

    def test_sigterm_interrupts_idle_accept(self):
        self.exercise_shutdown(False)

    def test_sigterm_interrupts_partial_request(self):
        self.exercise_shutdown(True)


if __name__ == "__main__":
    unittest.main()
