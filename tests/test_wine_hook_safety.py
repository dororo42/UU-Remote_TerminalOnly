"""Deterministic native probes of Windows hook logic, without a Wine session."""
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def function(source, name):
    match = re.search(r"^static [^\n]*\b" + name + r"\([^;{]*\)\s*\{", source, re.M)
    if match is None:
        raise ValueError("Function definition not found: " + name)
    start = source.index("{", match.start())
    depth = 1
    end = start + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[match.start():end] + "\n"


COMMON = r'''
#include <assert.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include <strings.h>
#include <stdarg.h>
#include <wchar.h>
#include <stdlib.h>
#define WINAPI
#define TRUE 1
#define FALSE 0
#define MAX_PATH 260
#define ERROR_SUCCESS 0
#define ERROR_ACCESS_DENIED 5
#define ERROR_INVALID_DATA 13
#define ERROR_BROKEN_PIPE 109
#define ERROR_INSUFFICIENT_BUFFER 122
#define ERROR_MOD_NOT_FOUND 126
#define ERROR_PROC_NOT_FOUND 127
#define ERROR_BAD_EXE_FORMAT 193
#define ERROR_INVALID_CURSOR_HANDLE 1402
#define CURSOR_SHOWING 1
#define INVALID_HANDLE_VALUE ((void *)(intptr_t)-1)
#define GENERIC_READ 1
#define GENERIC_WRITE 2
#define OPEN_EXISTING 3
#define PAGE_READWRITE 4
#define IMAGE_DOS_SIGNATURE 0x5a4d
#define IMAGE_NT_SIGNATURE 0x4550
#define IMAGE_DIRECTORY_ENTRY_IMPORT 1
#define IMAGE_SNAP_BY_ORDINAL(value) ((value) >> 63)
#define ZeroMemory(p,n) memset(p,0,n)
#define _stricmp strcasecmp
#define WM_SETCURSOR 32
#define WM_MOUSEMOVE 512
#define HTCLIENT 1
#define MAKELPARAM(a,b) ((a) | ((b) << 16))
typedef uint8_t BYTE;
typedef uint16_t WORD;
typedef uint32_t DWORD;
typedef int32_t LONG;
typedef unsigned int UINT;
typedef int BOOL;
typedef uint64_t ULONGLONG;
typedef void *HANDLE, *HMODULE, *HINSTANCE, *HWND, *HCURSOR, *HICON, *HBITMAP, *PVOID;
typedef uintptr_t WPARAM;
typedef int CRITICAL_SECTION;
static DWORD last_error;
static DWORD GetLastError(void) { return last_error; }
static void SetLastError(DWORD value) { last_error = value; }
static LONG InterlockedIncrement(volatile LONG *p) { return ++*p; }
static void *InterlockedExchangePointer(void *volatile *p, void *value)
{ void *old = *p; *p = value; return old; }
static char guard_log[4096];
static void write_log(const char *message) { strncat(guard_log, message, sizeof(guard_log) - strlen(guard_log) - 1); }
static void flush_log(void) {}
static void FlushFileBuffers(HANDLE h) { (void)h; }
static BOOL CloseHandle(HANDLE h) { (void)h; return TRUE; }
static void InitializeCriticalSection(CRITICAL_SECTION *p) { *p = 1; }
static void EnterCriticalSection(CRITICAL_SECTION *p) { (void)p; }
static void LeaveCriticalSection(CRITICAL_SECTION *p) { (void)p; }
static ULONGLONG GetTickCount64(void) { return 0; }
static int _snprintf(char *buffer, size_t size, const char *format, ...)
{ (void)format; if (size) buffer[0] = 0; return 0; }
typedef struct { WORD e_magic; DWORD e_lfanew; } IMAGE_DOS_HEADER;
typedef struct { DWORD VirtualAddress; } DATA_DIRECTORY;
typedef struct { DWORD Signature; struct { DATA_DIRECTORY DataDirectory[16]; } OptionalHeader; } IMAGE_NT_HEADERS;
typedef struct { DWORD OriginalFirstThunk, Name, FirstThunk; } IMAGE_IMPORT_DESCRIPTOR;
typedef struct { union { uint64_t AddressOfData, Function, Ordinal; } u1; } IMAGE_THUNK_DATA;
typedef struct { WORD Hint; char Name[60]; } IMAGE_IMPORT_BY_NAME;
static BYTE module_data[4096];
static BYTE streamer_data[4096];
static BOOL streamer_loaded = TRUE;
static int publication_failures;
static int protect_calls, protect_fail_at, protect_fail_again_at, flush_calls, flush_fail_at;
static HMODULE GetModuleHandleW(const wchar_t *name)
{ return name != NULL && !wcscmp(name, L"streamer.dll") ? (streamer_loaded ? streamer_data : NULL) : module_data; }
static HANDLE GetCurrentProcess(void) { return NULL; }
static BOOL VirtualProtect(void *address, size_t size, DWORD mode, DWORD *old)
{
    (void)address; (void)size; (void)mode; ++protect_calls;
    if (protect_calls == protect_fail_at || protect_calls == protect_fail_again_at) { SetLastError(ERROR_ACCESS_DENIED); return FALSE; }
    *old = 0; return TRUE;
}
static void observe_publication(uintptr_t value);
static BOOL FlushInstructionCache(HANDLE h, void *address, size_t size)
{
    (void)h; (void)size; ++flush_calls; observe_publication(*(uintptr_t *)address);
    if (flush_calls == flush_fail_at) { SetLastError(ERROR_ACCESS_DENIED); return FALSE; }
    return TRUE;
}
static void imports(const char **names, const uintptr_t *values, int count)
{
    memset(module_data, 0, sizeof(module_data));
    IMAGE_DOS_HEADER *dos = (void *)module_data;
    dos->e_magic = IMAGE_DOS_SIGNATURE; dos->e_lfanew = 64;
    IMAGE_NT_HEADERS *nt = (void *)(module_data + 64);
    nt->Signature = IMAGE_NT_SIGNATURE;
    nt->OptionalHeader.DataDirectory[1].VirtualAddress = 256;
    IMAGE_IMPORT_DESCRIPTOR *descriptor = (void *)(module_data + 256);
    descriptor->Name = 512; descriptor->OriginalFirstThunk = 768; descriptor->FirstThunk = 1024;
    strcpy((char *)module_data + 512, "USER32.dll");
    IMAGE_THUNK_DATA *thunks = (void *)(module_data + 768);
    IMAGE_THUNK_DATA *addresses = (void *)(module_data + 1024);
    for (int i = 0; i < count; ++i) {
        thunks[i].u1.AddressOfData = 1280 + i * 64;
        IMAGE_IMPORT_BY_NAME *entry = (void *)(module_data + thunks[i].u1.AddressOfData);
        strcpy(entry->Name, names[i]); addresses[i].u1.Function = values[i];
    }
    memcpy(streamer_data, module_data, sizeof(module_data));
}
'''


INPUT = r'''
#define INPUT_MOUSE 0
#define INPUT_KEYBOARD 1
#define KEYEVENTF_UNICODE 4
#define INPUT_BRIDGE_MAGIC 0x42525555UL
#define INPUT_BRIDGE_MAX_INPUTS 2048UL
#define INPUT_BRIDGE_PIPE L"probe"
typedef struct { LONG dx, dy; DWORD mouseData, dwFlags, time; uintptr_t dwExtraInfo; } MOUSEINPUT;
typedef struct { WORD wVk, wScan; DWORD dwFlags, time; uintptr_t dwExtraInfo; } KEYBDINPUT;
typedef struct { DWORD type; union { MOUSEINPUT mi; KEYBDINPUT ki; }; } INPUT, *LPINPUT;
typedef UINT (*send_input_fn)(UINT, LPINPUT, int);
typedef struct { DWORD magic, count, input_size; } input_bridge_request;
typedef struct { DWORD result, error; } input_bridge_response;
static send_input_fn original_send_input;
static void *original_shell_execute;
static void bridged_shell_execute(void) {}
static HANDLE broker_pipe = INVALID_HANDLE_VALUE;
static CRITICAL_SECTION broker_lock;
static BOOL broker_lock_initialized = TRUE;
static volatile LONG input_call_count, keyboard_call_count, mouse_call_count, other_call_count, text_call_count;
static int mode, writes, connections, requests, reads;
static int direct_calls, foreground_calls;
static UINT broker_count, direct_result, response_result = UINT32_MAX;
static LONG first_broker_dx;
static BOOL WaitNamedPipeW(const wchar_t *name, DWORD timeout)
{ (void)name; (void)timeout; ++connections; return TRUE; }
static HANDLE CreateFileW(const wchar_t *name, DWORD access, DWORD share, void *security, DWORD create, DWORD flags, HANDLE template)
{ (void)name; (void)access; (void)share; (void)security; (void)create; (void)flags; (void)template; return (HANDLE)1; }
static BOOL WriteFile(HANDLE h, const void *buffer, DWORD size, DWORD *written, void *overlap)
{
    (void)h; (void)overlap; ++writes;
    if ((mode == 2 && writes == 1) || (mode == 3 && writes == 2)) {
        *written = 0; SetLastError(ERROR_BROKEN_PIPE); return FALSE;
    }
    if (mode == 3 && writes == 1) { *written = 1; return TRUE; }
    if (size == sizeof(input_bridge_request)) {
        ++requests; broker_count = ((const input_bridge_request *)buffer)->count;
    } else if (size >= sizeof(INPUT)) {
        first_broker_dx = ((const INPUT *)buffer)[0].mi.dx;
    }
    *written = size; return TRUE;
}
static BOOL ReadFile(HANDLE h, void *buffer, DWORD size, DWORD *received, void *overlap)
{
    (void)h; (void)overlap; ++reads;
    if (mode == 1 && reads == 1) { *received = 0; SetLastError(ERROR_BROKEN_PIPE); return FALSE; }
    input_bridge_response response = { response_result == UINT32_MAX ? broker_count : response_result, ERROR_SUCCESS };
    memcpy(buffer, &response, size); *received = size; return TRUE;
}
static HWND find_relay_window(void) { return NULL; }
static BOOL SetForegroundWindow(HWND window) { (void)window; ++foreground_calls; return TRUE; }
static UINT direct_send_input(UINT count, LPINPUT inputs, int size)
{ (void)count; (void)inputs; (void)size; ++direct_calls; return direct_result; }
static void open_log(void) {}
static void safe_evt_open_publisher_metadata(void) {}
'''

INPUT_DEPENDENCIES = r'''
#include "uurb_rdp_state.h"
#include "uurb_public_config.h"
#define ERROR_NOT_READY 21
#define ERROR_TIMEOUT 1460
#define ERROR_NO_DATA 232
#define ERROR_INVALID_PARAMETER 87
#define PIPE_READMODE_BYTE 0
#define PIPE_NOWAIT 1
#define PROCESS_QUERY_LIMITED_INFORMATION 0x1000
#define VK_CAPITAL 20
#define VK_NUMLOCK 144
#define VK_SCROLL 145
#define CP_UTF8 65001
#define MB_ERR_INVALID_CHARS 8
#define FILE_SHARE_READ 1
#define FILE_FLAG_OPEN_REPARSE_POINT 0x200000
#define FILE_TYPE_DISK 1
#define FILE_ATTRIBUTE_REPARSE_POINT 0x400
#define FILE_ATTRIBUTE_DIRECTORY 0x10
typedef uint32_t ULONG;
typedef int16_t SHORT;
typedef BYTE *PBYTE;
typedef struct { LONG x,y; } POINT,*LPPOINT;
typedef BOOL (*get_cursor_pos_fn)(LPPOINT);
typedef SHORT (*get_key_state_fn)(int);
typedef BOOL (*get_keyboard_state_fn)(PBYTE);
static get_cursor_pos_fn original_get_cursor_pos;
static get_key_state_fn original_get_key_state,original_get_async_key_state;
static get_keyboard_state_fn original_get_keyboard_state;
static HINSTANCE bridge_instance=(HINSTANCE)77;
static BOOL public_config_ready=TRUE;
static DWORD public_broker_pid=42;
static uint64_t public_broker_start=1234;
typedef struct { DWORD dwLowDateTime,dwHighDateTime; } FILETIME;
typedef struct {
 DWORD dwFileAttributes,nFileSizeHigh,nFileSizeLow,nFileIndexHigh,nFileIndexLow;
 FILETIME ftLastWriteTime;
} BY_HANDLE_FILE_INFORMATION;
static BOOL TryEnterCriticalSection(CRITICAL_SECTION *p) {(void)p;return TRUE;}
static void Sleep(DWORD delay) {(void)delay;assert(!"unexpected idle I/O in fixture");}
static BOOL SetNamedPipeHandleState(HANDLE p,DWORD *mode,void *a,void *b) {
 (void)p;(void)mode;(void)a;(void)b;return TRUE;
}
static DWORD GetEnvironmentVariableA(const char *name,char *out,DWORD size) {
 assert(!strcmp(name,"UURB_INPUT_ROUTE"));(void)out;(void)size;return 0;
}
static BOOL uurb_ready_read(const wchar_t *name,const char *role,DWORD *pid,uint64_t *start) {
 assert(!wcscmp(name,L"UURB_FULL_BROKER_READY") && !strcmp(role,"broker"));
 *pid=42;*start=1234;return TRUE;
}
static BOOL GetNamedPipeServerProcessId(HANDLE h,ULONG *pid) {
 assert(h==(HANDLE)1);*pid=42;return TRUE;
}
static HANDLE OpenProcess(DWORD access,BOOL inherit,DWORD pid) {
 assert(access==PROCESS_QUERY_LIMITED_INFORMATION && !inherit && pid==42);return (HANDLE)1;
}
static uint64_t uurb_creation(HANDLE h) {(void)h;return 1234;}
static DWORD GetCurrentProcessId(void) {return 99;}
static DWORD GetModuleFileNameW(HMODULE module,wchar_t *out,DWORD size) {
 (void)module;assert(size==MAX_PATH);wcscpy(out,L"Z:\\untrusted\\uu-input-bridge.dll");return (DWORD)wcslen(out);
}
static int _wcsicmp(const wchar_t *a,const wchar_t *b) {return wcscmp(a,b);}
static int MultiByteToWideChar(UINT page,DWORD flags,const char *text,int n,wchar_t *out,int size) {
 (void)page;(void)flags;(void)text;(void)n;(void)out;(void)size;assert(!"invalid module path must reject first");return 0;
}
static DWORD GetFileType(HANDLE h) {(void)h;assert(!"invalid module path must reject first");return 0;}
static BOOL GetFileInformationByHandle(HANDLE h,BY_HANDLE_FILE_INFORMATION *out) {
 (void)h;(void)out;assert(!"invalid module path must reject first");return FALSE;
}
static BOOL SetEnvironmentVariableA(const char *name,const char *value) {
 (void)name;(void)value;assert(!"invalid module path must reject before environment publication");return FALSE;
}
static BOOL SetEnvironmentVariableW(const wchar_t *name,const wchar_t *value) {
 (void)name;(void)value;assert(!"invalid module path must reject before environment publication");return FALSE;
}
'''


CURSOR = r'''
typedef struct { DWORD cbSize, flags; HCURSOR hCursor; } CURSORINFO, *PCURSORINFO;
typedef struct { BOOL fIcon; DWORD xHotspot, yHotspot; HBITMAP hbmMask, hbmColor; } ICONINFO, *PICONINFO;
typedef HCURSOR (*set_cursor_fn)(HCURSOR);
typedef BOOL (*get_cursor_info_fn)(PCURSORINFO);
typedef BOOL (*get_icon_info_fn)(HICON, PICONINFO);
static set_cursor_fn original_set_cursor;
static get_cursor_info_fn original_get_cursor_info;
static get_icon_info_fn original_get_icon_info;
static HCURSOR fallback_cursor = (HCURSOR)1;
static UINT fallback_cursor_width = 48, fallback_cursor_height = 48;
static HANDLE log_file = INVALID_HANDLE_VALUE;
static volatile LONG hidden_cursor_count;
static DWORD cursor_flags = CURSOR_SHOWING, icon_error;
static HCURSOR real_cursor = (HCURSOR)2;
static int deleted_bitmaps;
static BOOL relay_process;
static BOOL cursor_self_test_fails;
static HCURSOR base_set_cursor(HCURSOR cursor) { return cursor; }
static BOOL base_get_cursor_info(PCURSORINFO cursor)
{ cursor->flags = cursor_flags; cursor->hCursor = real_cursor; return !cursor_self_test_fails; }
static BOOL base_get_icon_info(HICON icon, PICONINFO info)
{
    if (icon != fallback_cursor && icon_error) { SetLastError(icon_error); return FALSE; }
    info->hbmMask = (HBITMAP)3; info->hbmColor = (HBITMAP)4; return TRUE;
}
static BOOL GetIconInfo(HICON icon, PICONINFO info) { return base_get_icon_info(icon, info); }
static BOOL DeleteObject(HBITMAP bitmap) { (void)bitmap; ++deleted_bitmaps; return TRUE; }
static int _wcsicmp(const wchar_t *left, const wchar_t *right) { return wcscmp(left, right); }
static BOOL load_fallback_cursor(void) { return TRUE; }
static void open_log(const wchar_t *process) { (void)process; }
static void write_active_log(const char *name) { (void)name; }
static DWORD GetModuleFileNameW(HMODULE module, wchar_t *path, DWORD size)
{ (void)module; (void)size; wcscpy(path, relay_process ? L"sdl-freerdp.exe" : L"GameViewerServer.exe"); return (DWORD)wcslen(path); }
static HWND FindWindowW(const wchar_t *class_name, const wchar_t *title)
{ (void)class_name; (void)title; return NULL; }
static BOOL PostMessageW(HWND window, UINT message, WPARAM word, uintptr_t value)
{ (void)window; (void)message; (void)word; (void)value; return TRUE; }
'''


RESOURCES = r'''
#define DEFAULT_CURSOR_SIZE 48U
#define MIN_CURSOR_SIZE 24U
#define MAX_CURSOR_SIZE 128U
#define IDC_ARROW L"arrow"
#define IMAGE_CURSOR 2
#define LR_LOADFROMFILE 16
#define LR_DEFAULTCOLOR 0
typedef struct { BOOL fIcon; DWORD xHotspot, yHotspot; HBITMAP hbmMask, hbmColor; } ICONINFO;
typedef struct { LONG bmWidth, bmHeight; } BITMAP;
static HINSTANCE guard_instance;
static HCURSOR fallback_cursor;
static BOOL fallback_cursor_owned;
static UINT fallback_cursor_width, fallback_cursor_height;
static const wchar_t *ini = L"24", *environment_size = L"48";
static int file_width = 24, loaded_files, copied_cursors, destroyed_cursors;
static BOOL copy_failed;
static DWORD GetModuleFileNameW(HMODULE module, wchar_t *path, DWORD size)
{ (void)module; (void)size; wcscpy(path, L"C:\\compat\\uu-cursor-guard.dll"); return (DWORD)wcslen(path); }
static void lstrcpyW(wchar_t *target, const wchar_t *source) { wcscpy(target, source); }
static DWORD GetPrivateProfileStringW(const wchar_t *section, const wchar_t *key, const wchar_t *empty, wchar_t *value, DWORD size, const wchar_t *path)
{
    (void)empty;
    assert(!wcscmp(section, L"Cursor") && !wcscmp(key, L"Size"));
    assert(!wcscmp(path, L"C:\\compat\\uu-cursor.ini"));
    wcsncpy(value, ini, size); value[size - 1] = 0; return (DWORD)wcslen(value);
}
static DWORD GetEnvironmentVariableW(const wchar_t *name, wchar_t *value, DWORD size)
{ (void)name; wcsncpy(value, environment_size, size); value[size - 1] = 0; return (DWORD)wcslen(value); }
static HCURSOR LoadCursorW(HMODULE module, const wchar_t *name)
{ (void)module; (void)name; return (HCURSOR)1; }
static HANDLE LoadImageW(HMODULE module, const wchar_t *path, UINT type, int width, int height, UINT flags)
{
    (void)module; assert(!wcscmp(path, L"C:\\compat\\uu-cursor.cur"));
    assert(type == IMAGE_CURSOR && width == 24 && height == 24 && flags == LR_LOADFROMFILE);
    ++loaded_files; return file_width ? (HANDLE)5 : NULL;
}
static HANDLE CopyImage(HANDLE base, UINT type, int width, int height, UINT flags)
{ (void)base; (void)type; (void)flags; assert(width == 24 && height == 24); ++copied_cursors; return copy_failed ? NULL : (HANDLE)6; }
static BOOL DestroyCursor(HCURSOR cursor) { (void)cursor; ++destroyed_cursors; return TRUE; }
static BOOL GetIconInfo(HCURSOR cursor, ICONINFO *icon)
{ icon->hbmColor = (HBITMAP)((uintptr_t)cursor + 100); icon->hbmMask = NULL; return TRUE; }
static int GetObjectW(HBITMAP bitmap, int size, BITMAP *result)
{ (void)size; result->bmWidth = (uintptr_t)bitmap == 105 ? file_width : 24; result->bmHeight = result->bmWidth; return sizeof(*result); }
static BOOL DeleteObject(HBITMAP bitmap) { (void)bitmap; return TRUE; }
static void observe_publication(uintptr_t value) { (void)value; }
'''


LOGGING = r'''
#define FILE_APPEND_DATA 4
#define FILE_SHARE_READ 1
#define FILE_SHARE_WRITE 2
#define OPEN_ALWAYS 4
#define FILE_ATTRIBUTE_NORMAL 0
static HINSTANCE guard_instance;
static HANDLE log_file = INVALID_HANDLE_VALUE;
static const wchar_t *ini_log = L"C:\\users\\fixture\\Temp\\uu-cursor-guard.log";
static const wchar_t *environment_log = L"C:\\wrong\\uu-cursor-guard.log";
static wchar_t opened_path[MAX_PATH];
static int ini_calls, environment_calls;
static BOOL truncated_ini;
static int _wcsicmp(const wchar_t *a, const wchar_t *b) { return wcscmp(a,b); }
static DWORD GetModuleFileNameW(HMODULE module, wchar_t *path, DWORD size)
{ (void)module; (void)size; wcscpy(path,L"C:\\compat\\uu-cursor-guard.dll"); return wcslen(path); }
static DWORD GetPrivateProfileStringW(const wchar_t *section, const wchar_t *key,
                                     const wchar_t *empty, wchar_t *value, DWORD size, const wchar_t *path)
{
    (void)empty; ++ini_calls;
    assert(!wcscmp(section,L"Cursor") && !wcscmp(key,L"RelayLogPath"));
    assert(!wcscmp(path,L"C:\\compat\\uu-cursor.ini"));
    wcsncpy(value,ini_log,size); value[size-1]=0;
    return truncated_ini ? size-1 : (DWORD)wcslen(value);
}
static DWORD GetEnvironmentVariableW(const wchar_t *name, wchar_t *value, DWORD size)
{ ++environment_calls; assert(!wcscmp(name,L"UURB_CURSOR_GUARD_LOG")); wcsncpy(value,environment_log,size); value[size-1]=0; return wcslen(value); }
static DWORD GetTempPathW(DWORD size, wchar_t *value)
{ wcsncpy(value,L"C:\\users\\fixture\\AppData\\Local\\Temp\\",size); return wcslen(value); }
static void lstrcpyW(wchar_t *target, const wchar_t *source) { wcscpy(target,source); }
static void lstrcpynW(wchar_t *target, const wchar_t *source, int size)
{ wcsncpy(target,source,size); target[size-1]=0; }
static void lstrcatW(wchar_t *target, const wchar_t *source) { wcscat(target,source); }
static HANDLE CreateFileW(const wchar_t *path, DWORD access, DWORD share,
                          void *security, DWORD creation, DWORD flags, HANDLE template)
{ (void)access; (void)share; (void)security; (void)creation; (void)flags; (void)template; wcscpy(opened_path,path); return (HANDLE)1; }
static void observe_publication(uintptr_t value) { (void)value; }
'''


class WineHookSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="uu-hook-safety-")
        cls.addClassCleanup(cls.directory.cleanup)
        # Partial direct results and pre-send retry are the installed legacy
        # DLL contract. The mandatory public DLL is tested separately below.
        input_functions = [
            "public_rdp_route", "public_broker_peer", "write_all", "read_all", "disconnect_broker",
            "connect_broker", "broker_action_io", "send_public_broker", "source_snapshot",
            "public_get_cursor_pos", "source_key", "public_get_key_state",
            "public_get_async_key_state", "public_get_keyboard_state", "send_through_broker",
            "contains_unicode_keyboard", "contains_input_type", "bridged_send_input", "patch_import", "initialize_bridge",
        ]
        cls.input_exe = cls.compile_probe("input", INPUT + INPUT_DEPENDENCIES, "uu_input_bridge_legacy.c", input_functions, r'''
static void observe_publication(uintptr_t value)
{ if (value == (uintptr_t)&bridged_send_input && original_send_input == NULL) ++publication_failures; }
int main(int argc, char **argv)
{
    assert(argc == 2);
    if (!strcmp(argv[1], "publish")) {
        const char *names[] = { "SendInput" }; uintptr_t values[] = { (uintptr_t)&direct_send_input };
        imports(names, values, 1); initialize_bridge(NULL); assert(publication_failures == 0); return 0;
    }
    original_send_input = direct_send_input;
    INPUT inputs[3] = {0}; for (int i = 0; i < 3; ++i) inputs[i].mi.dx = 11 * (i + 1);
    if (!strcmp(argv[1], "partial")) {
        direct_result = 1; assert(bridged_send_input(3, inputs, sizeof(INPUT)) == 3);
        assert(broker_count == 2 && first_broker_dx == 22);
    } else if (!strcmp(argv[1], "partial-broker")) {
        direct_result = 1; response_result = 1;
        assert(bridged_send_input(3, inputs, sizeof(INPUT)) == 2);
    } else {
        DWORD error; mode = !strcmp(argv[1], "ack") ? 1 : (!strcmp(argv[1], "before-write") ? 2 : 3);
        UINT result = send_through_broker(3, inputs, sizeof(INPUT), &error);
        if (mode == 2) { assert(result == 3 && connections == 2 && requests == 1); }
        else if (mode == 1) { assert(result == 0 && connections == 1 && requests == 1); }
        else { assert(result == 0 && connections == 1 && writes == 2); }
    }
    return 0;
}
''')
        cls.public_exe = cls.compile_probe("public-input", INPUT + INPUT_DEPENDENCIES,
                                          "uu_input_bridge.c",
                                          ["config_wide_path", "load_public_config", *input_functions], r'''
static void observe_publication(uintptr_t value)
{ if(value==(uintptr_t)&bridged_send_input && !original_send_input) publication_failures++; }
int main(int argc,char **argv) {
 assert(argc==2);
 original_send_input=direct_send_input;
 INPUT inputs[3]={0};
 if(!strcmp(argv[1],"config-rejected")) {
  const char *names[]={"SendInput"};uintptr_t values[]={(uintptr_t)&direct_send_input};
  imports(names,values,1);original_send_input=NULL;
  assert(initialize_bridge(NULL)==ERROR_INVALID_DATA);
  assert(!public_config_ready && !publication_failures);
  assert(strstr(guard_log,"public session config rejected; input disabled"));
  assert(bridged_send_input(3,inputs,sizeof(INPUT))==0);
  POINT point={7,9};BYTE keys[256];memset(keys,0xa5,sizeof(keys));
  assert(!public_get_cursor_pos(&point) && GetLastError()==ERROR_NOT_READY);
  assert(point.x==7 && point.y==9);
  assert(!public_get_keyboard_state(keys) && GetLastError()==ERROR_NOT_READY);
  for(unsigned i=0;i<sizeof(keys);i++) assert(keys[i]==0xa5);
  assert(public_get_key_state(VK_CAPITAL)==0 && GetLastError()==ERROR_NOT_READY);
  assert(public_get_async_key_state(VK_NUMLOCK)==0 && GetLastError()==ERROR_NOT_READY);
  assert(!writes && !connections && !direct_calls && !foreground_calls);
  return 0;
 }
 if(!strcmp(argv[1],"no-direct")) {
  direct_result=1;
  assert(bridged_send_input(3,inputs,sizeof(INPUT))==3);
  assert(broker_count==3 && requests==1 && !direct_calls && !foreground_calls);
 } else {
  mode=!strcmp(argv[1],"lost-ack")?1:!strcmp(argv[1],"before-write")?2:3;
  DWORD error=0;
  assert(send_through_broker(3,inputs,sizeof(INPUT),&error)==0);
  assert(connections==1 && broker_pipe==INVALID_HANDLE_VALUE && !direct_calls);
  if(mode==1) assert(requests==1 && writes==2 && reads==1);
  if(mode==2) assert(writes==1 && requests==0);
  if(mode==3) assert(writes==2 && requests==0);
 }
 return 0;
}
''')
        cls.cursor_exe = cls.compile_probe("cursor", CURSOR, "uu_cursor_guard.c", [
            "guarded_set_cursor", "guarded_get_cursor_info", "guarded_get_icon_info",
            "cursor_reader_self_test", "log_guard_failure", "find_import",
            "install_guard_imports", "initialize_guard",
        ], r'''
static void observe_publication(uintptr_t value)
{
    if (value == (uintptr_t)&guarded_get_cursor_info && original_get_cursor_info == NULL) ++publication_failures;
    if (value == (uintptr_t)&guarded_get_icon_info && original_get_icon_info == NULL) ++publication_failures;
    if (value == (uintptr_t)&guarded_set_cursor && original_set_cursor == NULL) ++publication_failures;
}
int main(int argc, char **argv)
{
    assert(argc == 2);
    if (!strncmp(argv[1], "protect-failure-", 16) || !strncmp(argv[1], "flush-failure-", 14) ||
        !strcmp(argv[1], "reader-self-test-failure") || !strcmp(argv[1], "missing-streamer-import") ||
        !strcmp(argv[1], "missing-streamer") || !strcmp(argv[1], "mismatched-original") ||
        !strcmp(argv[1], "double-protect-failure") || !strcmp(argv[1], "unrestorable-slot")) {
        const char *names[] = { "GetCursorInfo", "GetIconInfo" };
        uintptr_t values[] = { (uintptr_t)&base_get_cursor_info, (uintptr_t)&base_get_icon_info };
        imports(names, values, 2);
        if (!strncmp(argv[1], "protect-failure-", 16)) protect_fail_at = atoi(argv[1] + 16);
        if (!strncmp(argv[1], "flush-failure-", 14)) flush_fail_at = atoi(argv[1] + 14);
        if (!strcmp(argv[1], "double-protect-failure")) { protect_fail_at = 5; protect_fail_again_at = 9; }
        if (!strcmp(argv[1], "unrestorable-slot")) { protect_fail_at = 7; protect_fail_again_at = 9; }
        if (!strcmp(argv[1], "reader-self-test-failure")) cursor_self_test_fails = TRUE;
        if (!strcmp(argv[1], "missing-streamer-import")) ((IMAGE_THUNK_DATA *)(streamer_data + 768))[1].u1.AddressOfData = 0;
        IMAGE_THUNK_DATA *main_iat = (void *)(module_data + 1024);
        IMAGE_THUNK_DATA *streamer_iat = (void *)(streamer_data + 1024);
        if (!strcmp(argv[1], "missing-streamer")) streamer_loaded = FALSE;
        if (!strcmp(argv[1], "mismatched-original")) streamer_iat[1].u1.Function = (uintptr_t)&base_set_cursor;
        uintptr_t streamer_originals[] = { streamer_iat[0].u1.Function, streamer_iat[1].u1.Function };
        assert(initialize_guard(NULL) != 0);
        assert(strstr(guard_log, "stage=") != NULL && strstr(guard_log, "entry=") != NULL);
        if (!strcmp(argv[1], "unrestorable-slot")) {
            assert(strstr(guard_log, "stage=rollback-unrestored-import entry=streamer.dll!GetIconInfo") != NULL);
            assert(strstr(guard_log, "UU cursor reader guard initialization failed") != NULL);
            assert(strstr(guard_log, "guard active") == NULL);
            assert(main_iat[0].u1.Function == values[0] && main_iat[1].u1.Function == values[1]);
            assert(streamer_iat[0].u1.Function == streamer_originals[0]);
            assert(streamer_iat[1].u1.Function == (uintptr_t)&guarded_get_icon_info);
            return 0;
        }
        for (int i = 0; i < 2; ++i) {
            assert(main_iat[i].u1.Function == values[i]);
            assert(streamer_iat[i].u1.Function == streamer_originals[i]);
        }
        return 0;
    }
    if (!strcmp(argv[1], "relay-publish")) {
        relay_process = TRUE;
        const char *names[] = { "SetCursor" }; uintptr_t values[] = { (uintptr_t)&base_set_cursor };
        imports(names, values, 1); assert(initialize_guard(NULL) == 0); assert(publication_failures == 0); return 0;
    }
    if (!strcmp(argv[1], "publish")) {
        const char *names[] = { "GetCursorInfo", "GetIconInfo" };
        uintptr_t values[] = { (uintptr_t)&base_get_cursor_info, (uintptr_t)&base_get_icon_info };
        imports(names, values, 2); assert(initialize_guard(NULL) == 0); assert(publication_failures == 0); return 0;
    }
    original_get_cursor_info = base_get_cursor_info; original_get_icon_info = base_get_icon_info;
    if (!strcmp(argv[1], "hidden")) cursor_flags = 0;
    if (!strcmp(argv[1], "null")) real_cursor = NULL;
    if (!strcmp(argv[1], "invalid")) icon_error = ERROR_INVALID_CURSOR_HANDLE;
    if (!strcmp(argv[1], "other-error")) icon_error = ERROR_ACCESS_DENIED;
    CURSORINFO cursor = {0}; assert(guarded_get_cursor_info(&cursor));
    BOOL fallback = !strcmp(argv[1], "hidden") || !strcmp(argv[1], "null") || !strcmp(argv[1], "invalid");
    assert(cursor.hCursor == (fallback ? fallback_cursor : real_cursor));
    assert(cursor.flags == CURSOR_SHOWING);
    if (!strcmp(argv[1], "valid")) assert(deleted_bitmaps == 2);
    return 0;
}
''')
        resource_functions = ["adjacent_cursor_path", "configured_cursor_size", "cursor_dimensions", "load_fallback_cursor"]
        cls.resource_exe = cls.compile_probe("resources", RESOURCES, "uu_cursor_guard.c", resource_functions, r'''
int main(int argc, char **argv)
{
    assert(argc == 2);
    if (!strcmp(argv[1], "ini-priority")) assert(configured_cursor_size() == 24);
    else if (!strcmp(argv[1], "no-environment")) { environment_size = L""; assert(configured_cursor_size() == 24); }
    else if (!strcmp(argv[1], "bad-ini")) { ini = L"23"; environment_size = L"32"; assert(configured_cursor_size() == 32); }
    else if (!strcmp(argv[1], "missing-ini")) { ini = L""; environment_size = L"32"; assert(configured_cursor_size() == 32); }
    else if (!strcmp(argv[1], "bad-all")) { ini = L"bad"; environment_size = L"bad"; assert(configured_cursor_size() == 48); }
    else {
        environment_size = L"24";
        if (!strcmp(argv[1], "bad-resource")) file_width = 16;
        if (!strcmp(argv[1], "missing-resource")) file_width = 0;
        if (!strcmp(argv[1], "missing-all")) { file_width = 0; copy_failed = TRUE; }
        assert(load_fallback_cursor()); assert(fallback_cursor_width == 24 && fallback_cursor_height == 24);
        assert(loaded_files == 1);
        if (copy_failed) { assert(fallback_cursor == (HCURSOR)1 && !fallback_cursor_owned); return 0; }
        assert(fallback_cursor_owned);
        if (file_width == 24) { assert(fallback_cursor == (HCURSOR)5 && copied_cursors == 0); }
        else { assert(fallback_cursor == (HCURSOR)6 && copied_cursors == 1); }
        if (file_width == 16) assert(destroyed_cursors == 1);
    }
    return 0;
}
''')
        cls.logging_exe = cls.compile_probe("logging", LOGGING, "uu_cursor_guard.c",
                                           ["adjacent_cursor_path", "default_log_path", "open_log"], r'''
int main(int argc, char **argv)
{
    assert(argc==2);
    if (!strcmp(argv[1],"missing-environment")) environment_log=L"";
    if (!strcmp(argv[1],"reader")) {
        open_log(L"GameViewerServer.exe");
        assert(!wcscmp(opened_path,L"C:\\users\\fixture\\AppData\\Local\\Temp\\uu-cursor-guard.log"));
        assert(ini_calls==0 && environment_calls==0); return 0;
    }
    if (!strcmp(argv[1],"missing-ini")) ini_log=L"";
    if (!strcmp(argv[1],"relative-ini")) ini_log=L"uu-cursor-guard.log";
    if (!strcmp(argv[1],"truncated-ini")) truncated_ini=TRUE;
    open_log(L"sdl-freerdp.exe"); assert(ini_calls==1);
    if (ini_log[0]==L'\0' || ini_log[1]!=L':' || truncated_ini) {
        assert(!wcscmp(opened_path,environment_log)); assert(environment_calls==1);
    } else {
        assert(!wcscmp(opened_path,ini_log)); assert(environment_calls==0);
    }
    return 0;
}
''')

    @classmethod
    def compile_probe(cls, name, definitions, source_name, functions, main):
        source = (ROOT / "src" / source_name).read_text()
        if "install_guard_imports" in functions:
            definitions += re.search(r"typedef struct cursor_guard_import \{.*?\} cursor_guard_import;", source, re.S).group()
        code = COMMON + definitions + "\n".join(function(source, item) for item in functions) + main
        path = Path(cls.directory.name) / f"{name}.c"
        path.write_text(code)
        executable = path.with_suffix("")
        result = subprocess.run(["gcc", "-std=c11", "-O0", "-Wall", "-Wextra", "-Werror",
                        "-Wno-unused-function", "-Wno-unused-variable", "-Wno-unused-parameter",
                        "-I", str(ROOT / "src"), str(path), "-o", str(executable)],
                        capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        return executable

    def probe(self, executable, case):
        result = subprocess.run([str(executable), case], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_input_original_is_ready_at_iat_publication(self):
        self.probe(self.input_exe, "publish")

    def test_relay_log_path_survives_missing_environment_and_preserves_reader_path(self):
        for case in ("ini-priority", "missing-environment", "reader", "missing-ini", "relative-ini", "truncated-ini"):
            with self.subTest(case=case):
                self.probe(self.logging_exe, case)

    def test_partial_direct_input_only_sends_the_suffix(self):
        self.probe(self.input_exe, "partial")

    def test_partial_broker_result_is_added_to_direct_result(self):
        self.probe(self.input_exe, "partial-broker")

    def test_lost_ack_does_not_replay_an_executed_request(self):
        self.probe(self.input_exe, "ack")

    def test_retry_is_allowed_before_any_request_byte_was_sent(self):
        self.probe(self.input_exe, "before-write")

    def test_partial_request_is_not_replayed(self):
        self.probe(self.input_exe, "partial-write")

    def test_public_input_never_uses_direct_wine_or_replays_any_failure(self):
        for case in ("no-direct", "lost-ack", "before-write", "partial-write"):
            with self.subTest(case=case):
                self.probe(self.public_exe, case)

    def test_public_untrusted_module_rejects_config_and_keeps_hook_fail_closed(self):
        self.probe(self.public_exe, "config-rejected")

    def test_cursor_originals_are_ready_at_iat_publication(self):
        self.probe(self.cursor_exe, "publish")
        self.probe(self.cursor_exe, "relay-publish")

    def test_reader_initialization_failure_restores_all_imports(self):
        cases = [f"protect-failure-{index}" for index in range(1, 9)]
        cases += [f"flush-failure-{index}" for index in range(1, 5)]
        cases += ["reader-self-test-failure", "missing-streamer-import", "missing-streamer", "mismatched-original"]
        cases += ["double-protect-failure"]
        for case in cases:
            with self.subTest(case=case):
                self.probe(self.cursor_exe, case)

    def test_cursor_shapes_are_preserved_unless_missing_or_invalid(self):
        for case in ("valid", "hidden", "null", "invalid", "other-error"):
            with self.subTest(case=case):
                self.probe(self.cursor_exe, case)

    def test_rollback_restores_other_slots_when_one_remains_inaccessible(self):
        self.probe(self.cursor_exe, "unrestorable-slot")

    def test_cursor_size_file_overrides_rebuilt_service_environment(self):
        for case in ("ini-priority", "no-environment", "bad-ini", "missing-ini", "bad-all"):
            with self.subTest(case=case):
                self.probe(self.resource_exe, case)

    def test_adjacent_cursor_resource_is_validated_and_falls_back(self):
        for case in ("resource", "bad-resource", "missing-resource", "missing-all"):
            with self.subTest(case=case):
                self.probe(self.resource_exe, case)
