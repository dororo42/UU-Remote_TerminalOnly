"""Probe host shell actions without invoking the real GNOME session."""
import ctypes
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest

from test_wine_hook_safety import COMMON, function

ROOT = Path(__file__).resolve().parents[1]

WINDOWS = r'''
#include <wctype.h>
#define ERROR_TIMEOUT 1460
#define ERROR_NOT_READY 21
#define ERROR_NO_DATA 232
#define PIPE_WAIT 0
#define PIPE_NOWAIT 1
#define INPUT_BRIDGE_PIPE L"private-fixture"
#define UURB_INPUT_HOST_ACTION_MAGIC 0x41425555
#define UURB_HOST_ACTION_SHOW_DESKTOP 1
#define UURB_HOST_ACTION_SHOW_WINDOWS 2
#define _wcsicmp fixture_wcsicmp
typedef intptr_t INT_PTR;
typedef uint32_t ULONG;
#define PROCESS_QUERY_LIMITED_INFORMATION 0x1000
typedef const wchar_t *LPCWSTR;
typedef HINSTANCE (*shell_execute_fn)(HWND,LPCWSTR,LPCWSTR,LPCWSTR,LPCWSTR,int);
typedef struct {DWORD magic,count,input_size;} input_bridge_request;
typedef struct {DWORD result,error;} input_bridge_response;
static HANDLE broker_pipe=INVALID_HANDLE_VALUE;
static CRITICAL_SECTION broker_lock;
static BOOL broker_lock_initialized=TRUE;
static shell_execute_fn original_shell_execute;
static ULONGLONG clock_ms;
static int scenario,requests,transfers,reads,mode_changes,passthrough;
static BOOL public_config_ready=TRUE;
static DWORD public_broker_pid=42;
static uint64_t public_broker_start=1234;
static BOOL uurb_ready_read(LPCWSTR name,const char *role,DWORD *pid,uint64_t *start) {
 assert(!wcscmp(name,L"UURB_FULL_BROKER_READY") && !strcmp(role,"broker"));
 *pid=42;*start=1234;return TRUE;
}
static BOOL GetNamedPipeServerProcessId(HANDLE handle,ULONG *pid) {
 assert(handle==(HANDLE)1);*pid=scenario==8?43:42;return TRUE;
}
static HANDLE OpenProcess(DWORD access,BOOL inherit,DWORD pid) {
 assert(access==PROCESS_QUERY_LIMITED_INFORMATION && !inherit && pid==42);return (HANDLE)1;
}
static uint64_t uurb_creation(HANDLE process) {
 assert(process==(HANDLE)1);return scenario==9?1235:1234;
}
static DWORD observed_action, observed_wait;
static unsigned wait_calls;
static int fixture_wcsicmp(const wchar_t *a,const wchar_t *b) {
 while (*a && *b && towlower(*a)==towlower(*b)) {a++;b++;}
 return towlower(*a)-towlower(*b);
}
static void Sleep(DWORD delay) {clock_ms+=delay;}
static BOOL TryEnterCriticalSection(CRITICAL_SECTION *p) {
 (void)p;
 if(scenario==6) return clock_ms>=3400;
 if(scenario==7) return clock_ms>=3500;
 return scenario!=4;
}
static BOOL WaitNamedPipeW(LPCWSTR name,DWORD timeout) {
 (void)name;observed_wait=timeout;wait_calls++;
 if(scenario==6) {clock_ms+=timeout;SetLastError(ERROR_TIMEOUT);return FALSE;}
 return TRUE;
}
static HANDLE CreateFileW(LPCWSTR p,DWORD a,DWORD s,void *sec,DWORD c,DWORD f,HANDLE t) {
 (void)p;(void)a;(void)s;(void)sec;(void)c;(void)f;(void)t;return (HANDLE)1;
}
static BOOL SetNamedPipeHandleState(HANDLE p,DWORD *mode,void *a,void *b) {
 (void)p;(void)a;(void)b;mode_changes++;
 if (scenario==5 && *mode==PIPE_WAIT) {SetLastError(ERROR_ACCESS_DENIED);return FALSE;}
 return TRUE;
}
static BOOL WriteFile(HANDLE h,const void *b,DWORD n,DWORD *written,void *o) {
 (void)h;(void)o;transfers++;
 if (scenario==2) {
  if (transfers==1) {*written=1;return TRUE;}
  *written=0;SetLastError(ERROR_BROKEN_PIPE);return FALSE;
 }
 if(n==sizeof(input_bridge_request)) {
  const input_bridge_request *r=b;
  assert(r->magic==UURB_INPUT_HOST_ACTION_MAGIC && r->count==1 && r->input_size==4);requests++;
 } else {assert(n==4);observed_action=*(const DWORD *)b;}
 *written=n;return TRUE;
}
static BOOL ReadFile(HANDLE h,void *b,DWORD n,DWORD *got,void *o) {
 (void)h;(void)o;reads++;
 if(scenario==1) {*got=0;SetLastError(ERROR_NO_DATA);return FALSE;}
 input_bridge_response r={1,0};assert(n==sizeof(r));memcpy(b,&r,n);*got=n;return TRUE;
}
static HINSTANCE original_shell(HWND w,LPCWSTR o,LPCWSTR f,LPCWSTR p,LPCWSTR d,int s) {
 (void)w;(void)o;(void)f;(void)p;(void)d;(void)s;passthrough++;SetLastError(777);return (HINSTANCE)(INT_PTR)99;
}
'''


class WindowsHostActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="uu-shell-action-windows-")
        cls.addClassCleanup(cls.tmp.cleanup)
        source = (ROOT / "src/uu_input_bridge.c").read_text()
        common = COMMON.replace("static ULONGLONG GetTickCount64(void) { return 0; }", "")
        names = ("public_rdp_route", "public_broker_peer", "disconnect_broker", "connect_broker", "broker_action_io",
                 "send_host_action", "shell_host_action", "bridged_shell_execute", "patch_import")
        code = common + WINDOWS + "\nstatic ULONGLONG GetTickCount64(void) {return clock_ms;}\n"
        code += "\n".join(function(source, name) for name in names)
        code += r'''
static void observe_publication(uintptr_t value) {
 if(value==(uintptr_t)&bridged_shell_execute && !original_shell_execute) publication_failures++;
}
int main(int argc,char **argv) {
 assert(argc==2);original_shell_execute=original_shell;
 const wchar_t *desktop=L"shell:::{3080F90D-D7AD-11D9-BD98-0000947B0257}";
 const wchar_t *windows=L"shell:::{3080F90E-D7AD-11D9-BD98-0000947B0257}";
 if(!strcmp(argv[1],"allow")) {
  assert((INT_PTR)bridged_shell_execute(NULL,L"open",L"explorer.exe",desktop,NULL,1)==33);
  assert(requests==1 && observed_action==1 && !passthrough && mode_changes==2);
  assert((INT_PTR)bridged_shell_execute(NULL,L"OPEN",L"EXPLORER.EXE",windows,L"",1)==33);
  assert(requests==2 && observed_action==2);
 } else if(!strcmp(argv[1],"reject")) {
  const wchar_t *bad[]={L"https://example.invalid/",L"shell:::{00000000-0000-0000-0000-000000000000}",
    L"shell:::{3080F90D-D7AD-11D9-BD98-0000947B0257} extra",L"\"shell:::{3080F90D-D7AD-11D9-BD98-0000947B0257}\""};
  for(unsigned i=0;i<4;i++) assert((INT_PTR)bridged_shell_execute(NULL,L"open",L"explorer.exe",bad[i],NULL,1)==99);
  assert((INT_PTR)bridged_shell_execute(NULL,L"runas",L"explorer.exe",desktop,NULL,1)==99);
  assert((INT_PTR)bridged_shell_execute(NULL,L"open",L"C:\\Windows\\explorer.exe",desktop,NULL,1)==99);
  assert((INT_PTR)bridged_shell_execute(NULL,L"open",L"explorer.exe",desktop,L"C:\\",1)==99);
  assert((INT_PTR)bridged_shell_execute(NULL,NULL,L"explorer.exe",desktop,NULL,1)==99);
  assert((INT_PTR)bridged_shell_execute(NULL,L"open",NULL,desktop,NULL,1)==99);
  assert(!requests && passthrough==9 && GetLastError()==777);
 } else if(!strcmp(argv[1],"config-rejected") || !strcmp(argv[1],"foreign-peer") || !strcmp(argv[1],"reused-peer")) {
  public_config_ready=strcmp(argv[1],"config-rejected")!=0;
  scenario=!strcmp(argv[1],"foreign-peer")?8:9;
  assert((INT_PTR)bridged_shell_execute(NULL,L"open",L"explorer.exe",desktop,NULL,1)<=32);
  assert(!requests && !transfers && !passthrough && broker_pipe==INVALID_HANDLE_VALUE);
 } else if(!strcmp(argv[1],"ordinary-connect")) {
  clock_ms=3400;assert(connect_broker(500));assert(observed_wait==500 && wait_calls==1);
 } else if(!strcmp(argv[1],"publish")) {
  const char *names[]={"ShellExecuteW"};uintptr_t values[]={(uintptr_t)original_shell};
  imports(names,values,1);strcpy((char *)module_data+512,"SHELL32.dll");original_shell_execute=NULL;
  assert(patch_import(module_data,"SHELL32.dll","ShellExecuteW",(uintptr_t)bridged_shell_execute,&original_shell_execute));
  assert(!publication_failures && original_shell_execute==original_shell);
 } else {
  scenario=!strcmp(argv[1],"lost-ack")?1:!strcmp(argv[1],"partial")?2:!strcmp(argv[1],"busy")?4:!strcmp(argv[1],"connect-deadline")?6:!strcmp(argv[1],"expired-lock")?7:5;
  INT_PTR result=(INT_PTR)bridged_shell_execute(NULL,L"open",L"explorer.exe",desktop,NULL,1);
  assert(!passthrough);
  if(scenario==5) assert(result==33 && GetLastError()==0 && broker_pipe==INVALID_HANDLE_VALUE && requests==1);
  else assert(result<=32 && broker_pipe==INVALID_HANDLE_VALUE);
  if(scenario==1) assert(requests==1 && transfers==2 && clock_ms>=3500 && clock_ms<3505);
  if(scenario==2) assert(transfers==2 && requests==0);
  if(scenario==4) assert(!requests && clock_ms>=3500 && clock_ms<3505);
  if(scenario==6) assert(!requests && observed_wait==100 && wait_calls==1 && clock_ms==3500);
  if(scenario==7) assert(!requests && !wait_calls && clock_ms==3500);
 }
 return 0;
}
'''
        path = Path(cls.tmp.name) / "probe.c"
        path.write_text(code)
        cls.exe = path.with_suffix("")
        result = subprocess.run(["gcc", "-std=c11", "-O0", "-Wall", "-Wextra", "-Werror",
                        "-Wno-unused-function", "-Wno-unused-variable", "-Wno-unused-parameter",
                        str(path), "-o", str(cls.exe)], capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)

    def probe(self, name):
        result = subprocess.run([str(self.exe), name], capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_exact_audited_operations_are_confirmed(self):
        self.probe("allow")

    def test_urls_quoting_unknown_actions_and_paths_pass_through(self):
        self.probe("reject")

    def test_original_is_ready_before_shell_iat_publication(self):
        self.probe("publish")

    def test_lost_ack_is_bounded_and_never_replayed(self):
        self.probe("lost-ack")

    def test_partial_frame_is_never_replayed(self):
        self.probe("partial")

    def test_busy_input_lock_has_a_deadline(self):
        self.probe("busy")

    def test_connection_wait_uses_only_remaining_action_budget(self):
        self.probe("connect-deadline")

    def test_expired_lock_budget_never_connects_with_zero_wait(self):
        self.probe("expired-lock")

    def test_ordinary_input_connection_retains_500_ms_wait(self):
        self.probe("ordinary-connect")

    def test_confirmed_action_survives_pipe_mode_cleanup_failure(self):
        self.probe("cleanup")

    def test_public_host_action_rejects_missing_config_before_io(self):
        self.probe("config-rejected")

    def test_public_host_action_rejects_foreign_or_reused_broker_peer(self):
        for case in ("foreign-peer", "reused-peer"):
            with self.subTest(case=case):
                self.probe(case)


class ClientMessage(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong),
                ("send_event", ctypes.c_int), ("display", ctypes.c_void_p),
                ("window", ctypes.c_ulong), ("message_type", ctypes.c_ulong),
                ("format", ctypes.c_int), ("data", ctypes.c_long * 5)]


class XEvent(ctypes.Union):
    _fields_ = [("client", ClientMessage), ("padding", ctypes.c_long * 24)]


class PrivateDesktop:
    def __init__(self, path, acknowledge=True):
        self.path = path
        self.acknowledge = acknowledge
        self.requests = 0
        self.value = 0
        self.stopping = threading.Event()
        self.log = (path / "xvfb.log").open("w")
        self.process = subprocess.Popen(["Xvfb", "-displayfd", "1", "-screen", "0", "320x240x24", "-nolisten", "tcp", "-extension", "GLX", "-ac"],
                                        stdout=subprocess.PIPE, stderr=self.log, text=True)
        self.display_name = ":" + self.process.stdout.readline().strip()
        self.x = ctypes.CDLL("libX11.so.6")
        signatures = {
            "XOpenDisplay": ([ctypes.c_char_p], ctypes.c_void_p),
            "XDefaultRootWindow": ([ctypes.c_void_p], ctypes.c_ulong),
            "XInternAtom": ([ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int], ctypes.c_ulong),
            "XChangeProperty": ([ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_void_p, ctypes.c_int], ctypes.c_int),
            "XSelectInput": ([ctypes.c_void_p, ctypes.c_ulong, ctypes.c_long], ctypes.c_int),
            "XSync": ([ctypes.c_void_p, ctypes.c_int], ctypes.c_int),
            "XPending": ([ctypes.c_void_p], ctypes.c_int),
            "XNextEvent": ([ctypes.c_void_p, ctypes.POINTER(XEvent)], ctypes.c_int),
            "XCloseDisplay": ([ctypes.c_void_p], ctypes.c_int),
        }
        for name, (args, result) in signatures.items():
            getattr(self.x, name).argtypes = args
            getattr(self.x, name).restype = result
        self.display = self.x.XOpenDisplay(self.display_name.encode())
        if not self.display:
            raise RuntimeError("Private Xvfb did not open")
        self.root = self.x.XDefaultRootWindow(self.display)
        self.property = self.x.XInternAtom(self.display, b"_NET_SHOWING_DESKTOP", 0)
        supported = self.x.XInternAtom(self.display, b"_NET_SUPPORTED", 0)
        data = ctypes.c_ulong(self.property)
        self.x.XChangeProperty(self.display, self.root, supported, 4, 32, 0, ctypes.byref(data), 1)
        self.set_value(0)
        self.x.XSelectInput(self.display, self.root, (1 << 19) | (1 << 20))
        self.x.XSync(self.display, 0)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def set_value(self, value):
        data = ctypes.c_ulong(value)
        self.x.XChangeProperty(self.display, self.root, self.property, 6, 32, 0, ctypes.byref(data), 1)
        self.x.XSync(self.display, 0)
        self.value = value

    def serve(self):
        while not self.stopping.is_set():
            while self.x.XPending(self.display):
                event = XEvent()
                self.x.XNextEvent(self.display, ctypes.byref(event))
                if event.client.type == 33 and event.client.message_type == self.property:
                    self.requests += 1
                    if self.acknowledge:
                        self.set_value(event.client.data[0])
            self.stopping.wait(.002)

    def close(self):
        self.stopping.set()
        self.thread.join(timeout=2)
        self.x.XCloseDisplay(self.display)
        self.process.terminate()
        self.process.wait(timeout=3)
        self.process.stdout.close()
        self.log.close()


@unittest.skipUnless(shutil.which("Xvfb") and shutil.which("gcc"), "isolated Xvfb / compiler required")
class NativeHostActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="uu-shell-action-native-")
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.path = Path(cls.tmp.name)
        fake = cls.path / "fake-gdbus"
        fake.write_text('''#!/usr/bin/python3
import os,sys,time
from pathlib import Path
if os.environ.get("UURB_FIXTURE_STALL") == "1": time.sleep(5)
a=sys.argv[1:]
assert a[:8] == ["call","--session","--timeout","1","--dest","org.gnome.Shell","--object-path","/org/gnome/Shell"]
assert a[8] == "--method" and a[10:12] == ["org.gnome.Shell","OverviewActive"]
p=Path(os.environ["UURB_FIXTURE_OVERVIEW"])
assert os.environ["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/private-fixture-no-gnome-bus"
if a[9] == "org.freedesktop.DBus.Properties.Get":
 print("(<"+p.read_text()+">,)")
else:
 assert a[9] == "org.freedesktop.DBus.Properties.Set" and a[12] in ["<true>","<false>"]
 if os.environ.get("UURB_FIXTURE_IGNORE_SET") != "1": p.write_text(a[12][1:-1])
 print("()")
''')
        fake.chmod(0o700)
        source = (ROOT / "src/uu_x11_input.c").read_text().replace('"/usr/bin/gdbus"', '"' + str(fake) + '"')
        copied = cls.path / "helper.c"
        copied.write_text(source)
        cls.exe = cls.path / "helper"
        result = subprocess.run(["gcc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-I", str(ROOT / "src"),
                                 str(copied), "-ldl", "-o", str(cls.exe)], capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)

    def setUp(self):
        self.work = tempfile.TemporaryDirectory(dir=self.path)
        self.addCleanup(self.work.cleanup)
        self.work_path = Path(self.work.name)
        self.desktop = PrivateDesktop(self.work_path)
        self.addCleanup(self.desktop.close)
        self.overview = self.work_path / "overview"
        self.overview.write_text("false")
        self.env = {**os.environ, "DISPLAY": self.desktop.display_name,
                    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/private-fixture-no-gnome-bus",
                    "UURB_X11_INPUT_TOKEN": "a" * 64,
                    "UURB_FIXTURE_OVERVIEW": str(self.overview)}

    def connect(self, token="a" * 64, extra=(), env=None):
        ready = self.work_path / "ready"
        log = (self.work_path / "helper.log").open("w")
        self.addCleanup(log.close)
        helper = subprocess.Popen([str(self.exe), "--ready-file", str(ready), *extra],
                                  env=env or self.env, stdout=log, stderr=log)
        def stop():
            if helper.poll() is None:
                helper.terminate()
            helper.wait(timeout=3)
        self.addCleanup(stop)
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            self.assertIsNone(helper.poll(), (self.work_path / "helper.log").read_text())
            time.sleep(.01)
        self.assertTrue(ready.exists())
        sock = socket.create_connection(("127.0.0.1", int(ready.read_text())), timeout=3)
        self.addCleanup(sock.close)
        sock.sendall(struct.pack("=II64s", 0x58315255, 3, token.encode()))
        return sock

    def action(self, sock, action, flags=0, count=1):
        sock.sendall(struct.pack("=IIII", 0x58315255, 1, count, 0))
        sock.sendall(struct.pack("=IIiiIHH", 4, flags, 0, 0, action, 0, 0) * count)
        data = b""
        while len(data) < 16:
            data += sock.recv(16 - len(data))
        return struct.unpack("=IIII", data)[2:]

    def authorized(self, **kwargs):
        sock = self.connect(**kwargs)
        self.assertEqual(sock.recv(16), struct.pack("=IIII", 0x58315255, 0, 1, 0))
        return sock

    def test_authenticated_desktop_toggle_confirms_actual_property(self):
        sock = self.authorized()
        self.assertEqual(self.action(sock, 1), (1, 0))
        self.assertEqual(self.desktop.value, 1)
        self.assertEqual(self.action(sock, 1), (1, 0))
        self.assertEqual(self.desktop.value, 0)
        self.assertEqual(self.desktop.requests, 2)

    def test_overview_toggles_only_fixed_dbus_property(self):
        sock = self.authorized()
        self.assertEqual(self.action(sock, 2), (1, 0))
        self.assertEqual(self.overview.read_text(), "true")
        self.assertEqual(self.action(sock, 2), (1, 0))
        self.assertEqual(self.overview.read_text(), "false")
        self.assertEqual(self.desktop.requests, 0)

    def test_unknown_and_malformed_actions_have_no_side_effect(self):
        sock = self.authorized()
        for kwargs in ({"action": 99}, {"action": 1, "flags": 1}, {"action": 1, "count": 2}):
            self.assertEqual(self.action(sock, **kwargs), (0, 0x2002))
        self.assertEqual(self.desktop.requests, 0)
        self.assertEqual(self.overview.read_text(), "false")

    def test_unauthorized_client_cannot_dispatch(self):
        sock = self.connect(token="b" * 64)
        self.assertEqual(sock.recv(16), b"")
        self.assertEqual(self.desktop.requests, 0)
        self.assertEqual(self.overview.read_text(), "false")

    def test_missing_desktop_ack_is_failure_without_replay(self):
        self.desktop.acknowledge = False
        sock = self.authorized()
        start = time.monotonic()
        self.assertEqual(self.action(sock, 1), (0, 0x2003))
        self.assertLess(time.monotonic() - start, 1)
        self.assertEqual(self.desktop.requests, 1)

    def test_successful_dbus_exit_without_property_change_is_failure(self):
        sock = self.authorized(env={**self.env, "UURB_FIXTURE_IGNORE_SET": "1"})
        self.assertEqual(self.action(sock, 2), (0, 0x2003))
        self.assertEqual(self.overview.read_text(), "false")

    def test_stalled_dbus_child_is_bounded_and_reaped(self):
        sock = self.authorized(env={**self.env, "UURB_FIXTURE_STALL": "1"})
        start = time.monotonic()
        self.assertEqual(self.action(sock, 2), (0, 0x2003))
        self.assertLess(time.monotonic() - start, 1.2)

    def test_desktop_action_uses_physical_target_not_paste_display(self):
        other_path = self.work_path / "other"
        other_path.mkdir()
        other = PrivateDesktop(other_path)
        self.addCleanup(other.close)
        auth = self.work_path / "authority"
        auth.write_bytes(b"")
        sock = self.authorized(extra=("--inject-display", other.display_name, "--inject-xauthority", str(auth)))
        self.assertEqual(self.action(sock, 1), (1, 0))
        self.assertEqual(self.desktop.value, 1)
        self.assertEqual(other.requests, 0)
        self.assertEqual(other.value, 0)
