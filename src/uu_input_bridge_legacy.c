#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <winevt.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "x11_input_protocol.h"
#include "uurb_ready.h"
#include "uurb_rdp_state.h"

typedef UINT(WINAPI *send_input_fn)(UINT, LPINPUT, int);
typedef HINSTANCE(WINAPI *shell_execute_fn)(HWND, LPCWSTR, LPCWSTR,
                                            LPCWSTR, LPCWSTR, int);

#define INPUT_BRIDGE_MAGIC 0x42525555UL
#define INPUT_BRIDGE_MAX_INPUTS 2048UL
#define INPUT_BRIDGE_PIPE L"\\\\.\\pipe\\uurb-input-v1"

typedef struct input_bridge_request {
    DWORD magic;
    DWORD count;
    DWORD input_size;
} input_bridge_request;

typedef struct input_bridge_response {
    DWORD result;
    DWORD error;
} input_bridge_response;

static send_input_fn original_send_input;
static shell_execute_fn original_shell_execute;
static HANDLE log_file = INVALID_HANDLE_VALUE;
static SRWLOCK log_lock = SRWLOCK_INIT;
static HANDLE broker_pipe = INVALID_HANDLE_VALUE;
static CRITICAL_SECTION broker_lock;
static BOOL broker_lock_initialized;
static volatile LONG input_call_count;
static volatile LONG keyboard_call_count;
static volatile LONG mouse_call_count;
static volatile LONG other_call_count;
static volatile LONG text_call_count;

static EVT_HANDLE WINAPI safe_evt_open_publisher_metadata(
    EVT_HANDLE session, LPCWSTR publisher_identity, LPCWSTR log_file_path,
    LCID locale, DWORD flags)
{
    (void)session;
    (void)publisher_identity;
    (void)log_file_path;
    (void)locale;
    (void)flags;

    SetLastError(ERROR_EVT_PUBLISHER_METADATA_NOT_FOUND);
    return NULL;
}

static void write_log(const char *message)
{
    DWORD written;
    DWORD error = GetLastError();

    AcquireSRWLockExclusive(&log_lock);
    if (log_file != INVALID_HANDLE_VALUE)
        WriteFile(log_file, message, (DWORD)strlen(message), &written, NULL);
    ReleaseSRWLockExclusive(&log_lock);
    SetLastError(error);
}

static void flush_log(void)
{
    if (log_file != INVALID_HANDLE_VALUE)
        FlushFileBuffers(log_file);
}

static void open_log(void)
{
    wchar_t path[MAX_PATH];
    DWORD length;

    length = GetEnvironmentVariableW(L"UU_INPUT_BRIDGE_LOG", path, MAX_PATH);
    if (length == 0 || length >= MAX_PATH) {
        length = GetTempPathW(MAX_PATH, path);
        if (length == 0 || length >= MAX_PATH - 20)
            lstrcpynW(path, L"uu-input-bridge.log", MAX_PATH);
        else
            lstrcatW(path, L"uu-input-bridge.log");
    }

    log_file = CreateFileW(path, FILE_APPEND_DATA,
                           FILE_SHARE_READ | FILE_SHARE_WRITE, NULL,
                           OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
}

static HWND find_relay_window(void)
{
    return FindWindowW(NULL, L"Ubuntu-Desktop-Relay");
}

static BOOL write_all(HANDLE handle, const void *buffer, DWORD size,
                      DWORD *sent)
{
    const BYTE *position = (const BYTE *)buffer;

    while (size > 0) {
        DWORD written = 0;
        BOOL success;

        success = WriteFile(handle, position, size, &written, NULL);
        *sent += written;
        if (!success || written == 0)
            return FALSE;
        position += written;
        size -= written;
    }

    return TRUE;
}

static BOOL read_all(HANDLE handle, void *buffer, DWORD size)
{
    BYTE *position = (BYTE *)buffer;

    while (size > 0) {
        DWORD received = 0;

        if (!ReadFile(handle, position, size, &received, NULL) || received == 0)
            return FALSE;
        position += received;
        size -= received;
    }

    return TRUE;
}

static void disconnect_broker(void)
{
    if (broker_pipe != INVALID_HANDLE_VALUE) {
        CloseHandle(broker_pipe);
        broker_pipe = INVALID_HANDLE_VALUE;
    }
}

static BOOL public_rdp_route(void);
static BOOL public_broker_peer(void)
{
    DWORD expected = 0;
    uint64_t creation = 0;
    ULONG actual = 0;
    if (!uurb_ready_read(L"UURB_FULL_BROKER_READY", "broker", &expected,
                         &creation) ||
        !GetNamedPipeServerProcessId(broker_pipe, &actual) || actual != expected)
        return FALSE;
    HANDLE process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, actual);
    if (!process)
        return FALSE;
    uint64_t started = uurb_creation(process);
    CloseHandle(process);
    return started && started == creation;
}

static BOOL connect_broker(DWORD wait_ms)
{
    if (broker_pipe != INVALID_HANDLE_VALUE) {
        if (!public_rdp_route() || public_broker_peer())
            return TRUE;
        disconnect_broker();
        return FALSE;
    }

    if (!WaitNamedPipeW(INPUT_BRIDGE_PIPE, wait_ms))
        return FALSE;

    broker_pipe = CreateFileW(INPUT_BRIDGE_PIPE, GENERIC_READ | GENERIC_WRITE,
                              0, NULL, OPEN_EXISTING, 0, NULL);
    if (broker_pipe == INVALID_HANDLE_VALUE)
        return FALSE;
    if (public_rdp_route() && !public_broker_peer()) {
        disconnect_broker();
        return FALSE;
    }
    return TRUE;
}

static BOOL public_rdp_route(void);
static BOOL broker_action_io(BOOL, void *, DWORD, DWORD *, ULONGLONG);
static UINT send_public_broker(UINT count, const INPUT *inputs, int size,
                               DWORD *error)
{
    DWORD timeout = 750;
    if (inputs && count <= INPUT_BRIDGE_MAX_INPUTS) {
        for (UINT i = 0; i < count; i++) {
            if (inputs[i].type == INPUT_KEYBOARD &&
                (inputs[i].ki.dwFlags & KEYEVENTF_UNICODE)) {
                timeout = 5000; /* Bounded native owner + selection barrier. */
                break;
            }
        }
    }
    ULONGLONG deadline = GetTickCount64() + timeout;
    input_bridge_request request = {INPUT_BRIDGE_MAGIC, count, (DWORD)size};
    input_bridge_response response = {0};
    DWORD sent = 0;
    DWORD mode = PIPE_READMODE_BYTE | PIPE_NOWAIT;
    *error = ERROR_INVALID_PARAMETER;
    if (!broker_lock_initialized || !count || count > INPUT_BRIDGE_MAX_INPUTS ||
        !inputs || size != (int)sizeof(INPUT))
        return 0;
    while (!TryEnterCriticalSection(&broker_lock)) {
        if (GetTickCount64() >= deadline) {
            *error = ERROR_TIMEOUT;
            return 0;
        }
        Sleep(1);
    }
    UINT result = 0;
    ULONGLONG now = GetTickCount64();
    if (now < deadline && connect_broker((DWORD)(deadline - now)) &&
        SetNamedPipeHandleState(broker_pipe, &mode, NULL, NULL) &&
        broker_action_io(TRUE, &request, sizeof(request), &sent, deadline) &&
        broker_action_io(TRUE, (void *)inputs, count * sizeof(INPUT), &sent,
                         deadline) &&
        broker_action_io(FALSE, &response, sizeof(response), &sent, deadline) &&
        response.result <= count) {
        result = response.result;
        *error = response.error;
    } else {
        *error = GetLastError();
        if (*error == ERROR_SUCCESS)
            *error = ERROR_INVALID_DATA;
        disconnect_broker();
    }
    /* No RPC retry: a lost reply may already have caused RDP input effects. */
    LeaveCriticalSection(&broker_lock);
    return result;
}

static BOOL source_snapshot(uurb_rdp_state *state)
{
    ULONGLONG deadline = GetTickCount64() + 150;
    input_bridge_request request = {UURB_RDP_STATE_MAGIC, 0, sizeof(*state)};
    input_bridge_response response = {0};
    DWORD transferred = 0, mode = PIPE_READMODE_BYTE | PIPE_NOWAIT;
    if (!broker_lock_initialized)
        return FALSE;
    while (!TryEnterCriticalSection(&broker_lock)) {
        if (GetTickCount64() >= deadline)
            return FALSE;
        Sleep(1);
    }
    ULONGLONG now = GetTickCount64();
    BOOL ok = now < deadline && connect_broker((DWORD)(deadline - now)) &&
        SetNamedPipeHandleState(broker_pipe, &mode, NULL, NULL) &&
        broker_action_io(TRUE, &request, sizeof(request), &transferred, deadline) &&
        broker_action_io(FALSE, &response, sizeof(response), &transferred, deadline) &&
        broker_action_io(FALSE, state, sizeof(*state), &transferred, deadline) &&
        response.result == 1 && response.error == ERROR_SUCCESS &&
        state->version == UURB_RDP_STATE_VERSION && state->generation && state->epoch;
    if (!ok)
        disconnect_broker();
    LeaveCriticalSection(&broker_lock);
    return ok;
}

typedef BOOL (WINAPI *get_cursor_pos_fn)(LPPOINT);
typedef SHORT (WINAPI *get_key_state_fn)(int);
static get_cursor_pos_fn original_get_cursor_pos;
static get_key_state_fn original_get_key_state, original_get_async_key_state;
typedef BOOL (WINAPI *get_keyboard_state_fn)(PBYTE);
static get_keyboard_state_fn original_get_keyboard_state;
static BOOL WINAPI public_get_cursor_pos(LPPOINT point)
{
    if (!public_rdp_route())
        return original_get_cursor_pos(point);
    uurb_rdp_state state;
    if (!point || !source_snapshot(&state) || !state.position_known) {
        SetLastError(ERROR_NOT_READY);
        return FALSE;
    }
    point->x = state.x;
    point->y = state.y;
    return TRUE;
}
static SHORT source_key(int vk)
{
    uurb_rdp_state state;
    /* Toggle bits and the Async transition bit require a real witness. */
    if (vk < 0 || vk > 255 || vk == VK_CAPITAL || vk == VK_NUMLOCK ||
        vk == VK_SCROLL || !source_snapshot(&state) || !state.known[vk]) {
        SetLastError(ERROR_NOT_READY);
        return 0;
    }
    return state.keys[vk] ? (SHORT)0x8000 : 0;
}
static BOOL WINAPI public_get_keyboard_state(PBYTE keys)
{
    if (!public_rdp_route())
        return original_get_keyboard_state(keys);
    uurb_rdp_state state;
    if (!keys || !source_snapshot(&state)) {
        SetLastError(ERROR_NOT_READY);
        return FALSE;
    }
    /* Whole Win32 keyboard state includes toggle and unobserved keys. No
     * synthetic initial zero/toggle bits are asserted as known. */
    for (unsigned i = 0; i < 256; i++) {
        if (!state.known[i] || i == VK_CAPITAL || i == VK_NUMLOCK || i == VK_SCROLL) {
            SetLastError(ERROR_NOT_READY);
            return FALSE;
        }
    }
    memcpy(keys, state.keys, 256);
    return TRUE;
}
static SHORT WINAPI public_get_key_state(int vk)
{
    return public_rdp_route() ? source_key(vk) : original_get_key_state(vk);
}
static SHORT WINAPI public_get_async_key_state(int vk)
{
    return public_rdp_route() ? source_key(vk) : original_get_async_key_state(vk);
}

static UINT send_through_broker(UINT count, const INPUT *inputs, int size,
                                DWORD *broker_error)
{
    input_bridge_request request;
    input_bridge_response response;
    UINT result = 0;
    int attempt;

    if (public_rdp_route())
        return send_public_broker(count, inputs, size, broker_error);
    *broker_error = ERROR_ACCESS_DENIED;
    if (!broker_lock_initialized || count == 0 || inputs == NULL ||
        size != (int)sizeof(INPUT))
        return 0;
    if (count > INPUT_BRIDGE_MAX_INPUTS) {
        *broker_error = ERROR_INSUFFICIENT_BUFFER;
        return 0;
    }

    request.magic = INPUT_BRIDGE_MAGIC;
    request.count = count;
    request.input_size = (DWORD)size;

    /*
     * UU's phone dictation grows a provisional composition into one SendInput
     * array. Preserve that complete call so a semantic clipboard paste cannot
     * observe only the final fragment. The bounded protocol allows 1,024
     * UTF-16 key pairs, comfortably above the live 332-record failure while
     * keeping every helper allocation finite.
     */
    EnterCriticalSection(&broker_lock);
    for (attempt = 0; attempt < 2; attempt++) {
        DWORD sent = 0;

        if (connect_broker(500) &&
            write_all(broker_pipe, &request, sizeof(request), &sent) &&
            write_all(broker_pipe, inputs, count * sizeof(INPUT), &sent) &&
            read_all(broker_pipe, &response, sizeof(response))) {
            result = response.result;
            *broker_error = response.error;
            break;
        }
        *broker_error = GetLastError();
        disconnect_broker();
        /* An executed request with a lost acknowledgement must not replay. */
        if (sent != 0)
            break;
    }
    LeaveCriticalSection(&broker_lock);
    return result;
}

static BOOL broker_action_io(BOOL writing, void *buffer, DWORD size,
                              DWORD *sent, ULONGLONG deadline)
{
    BYTE *position = buffer;

    while (size != 0) {
        DWORD transferred = 0;
        BOOL success;

        if (GetTickCount64() >= deadline ||
            (public_rdp_route() && !public_broker_peer())) {
            SetLastError(ERROR_TIMEOUT);
            return FALSE;
        }
        if (writing) {
            success = WriteFile(broker_pipe, position, size, &transferred,
                                NULL);
            *sent += transferred;
        } else {
            success = ReadFile(broker_pipe, position, size, &transferred,
                               NULL);
        }
        if (!success && GetLastError() != ERROR_NO_DATA)
            return FALSE;
        if (transferred > size) {
            SetLastError(ERROR_INVALID_DATA);
            return FALSE;
        }
        position += transferred;
        size -= transferred;
        if (transferred == 0)
            Sleep(2);
    }
    return TRUE;
}

static BOOL send_host_action(DWORD action, DWORD *error)
{
    input_bridge_request request = {UURB_INPUT_HOST_ACTION_MAGIC, 1,
                                    sizeof(DWORD)};
    input_bridge_response response = {0};
    ULONGLONG deadline = GetTickCount64() + 3500;
    DWORD mode = PIPE_NOWAIT;
    DWORD sent = 0;
    DWORD connect_wait;
    ULONGLONG now;
    BOOL success = FALSE;

    *error = ERROR_NOT_READY;
    if (!broker_lock_initialized)
        return FALSE;
    while (!TryEnterCriticalSection(&broker_lock)) {
        if (GetTickCount64() >= deadline) {
            *error = ERROR_TIMEOUT;
            return FALSE;
        }
        Sleep(2);
    }
    /* Use the existing serial connection. A second client would wait behind
     * the input client's persistent connection. Nonblocking pipe mode keeps
     * this action's acknowledgment bounded even if the broker stalls. */
    now = GetTickCount64();
    if (now >= deadline) {
        *error = ERROR_TIMEOUT;
        goto done;
    }
    connect_wait = (DWORD)(deadline - now);
    if (connect_wait > 500)
        connect_wait = 500;
    if (!connect_broker(connect_wait) ||
        !SetNamedPipeHandleState(broker_pipe, &mode, NULL, NULL)) {
        *error = GetLastError();
        goto done;
    }
    if (!broker_action_io(TRUE, &request, sizeof(request), &sent, deadline) ||
        !broker_action_io(TRUE, &action, sizeof(action), &sent, deadline) ||
        !broker_action_io(FALSE, &response, sizeof(response), &sent,
                          deadline)) {
        *error = GetLastError();
        goto done;
    }
    if (response.result != 1 || response.error != ERROR_SUCCESS) {
        *error = response.error ? response.error : ERROR_INVALID_DATA;
        goto done;
    }
    mode = PIPE_WAIT;
    if (!SetNamedPipeHandleState(broker_pipe, &mode, NULL, NULL)) {
        /* The action is already confirmed. Reconnect for later input rather
         * than misreporting this completed toggle or retrying it. */
        disconnect_broker();
    }
    *error = ERROR_SUCCESS;
    success = TRUE;
done:
    /* Never replay, including an executed action with a lost acknowledgment.
     * Closing also discards any late response before ordinary input resumes. */
    if (!success)
        disconnect_broker();
    LeaveCriticalSection(&broker_lock);
    return success;
}

static DWORD shell_host_action(LPCWSTR operation, LPCWSTR file,
                                LPCWSTR parameters, LPCWSTR directory)
{
    if (!operation || !file || !parameters ||
        (directory && directory[0] != L'\0') ||
        _wcsicmp(operation, L"open") != 0 ||
        _wcsicmp(file, L"explorer.exe") != 0)
        return 0;
    if (_wcsicmp(parameters,
                 L"shell:::{3080F90D-D7AD-11D9-BD98-0000947B0257}") == 0)
        return UURB_HOST_ACTION_SHOW_DESKTOP;
    if (_wcsicmp(parameters,
                 L"shell:::{3080F90E-D7AD-11D9-BD98-0000947B0257}") == 0)
        return UURB_HOST_ACTION_SHOW_WINDOWS;
    return 0;
}

static HINSTANCE WINAPI bridged_shell_execute(HWND window, LPCWSTR operation,
                                               LPCWSTR file,
                                               LPCWSTR parameters,
                                               LPCWSTR directory, int show)
{
    DWORD action = shell_host_action(operation, file, parameters, directory);
    DWORD error;
    BOOL success;
    char line[128];

    if (action == 0)
        return original_shell_execute(window, operation, file, parameters,
                                      directory, show);
    success = send_host_action(action, &error);
    _snprintf(line, sizeof(line), "UU host shell action=%s result=%s error=%lu\r\n",
              action == UURB_HOST_ACTION_SHOW_DESKTOP ? "show-desktop" :
                                                       "show-windows",
              success ? "confirmed" : "failed", (unsigned long)error);
    line[sizeof(line) - 1] = '\0';
    write_log(line);
    SetLastError(error);
    /* ShellExecute success is a non-handle value greater than 32. A failed
     * native action is not reported as Wine's successful Explorer launch. */
    return (HINSTANCE)(INT_PTR)(success ? 33 : 31);
}

static BOOL contains_unicode_keyboard(UINT count, const INPUT *inputs, int size)
{
    UINT index;

    if (count == 0 || inputs == NULL || size != (int)sizeof(INPUT))
        return FALSE;

    for (index = 0; index < count; index++) {
        if (inputs[index].type == INPUT_KEYBOARD &&
            (inputs[index].ki.dwFlags & KEYEVENTF_UNICODE) != 0)
            return TRUE;
    }

    return FALSE;
}

static BOOL contains_input_type(UINT count, const INPUT *inputs, int size,
                                DWORD type)
{
    UINT index;

    if (count == 0 || inputs == NULL || size != (int)sizeof(INPUT))
        return FALSE;
    for (index = 0; index < count; index++) {
        if (inputs[index].type == type)
            return TRUE;
    }
    return FALSE;
}

static BOOL public_rdp_route(void)
{
    char value[32];
    DWORD length = GetEnvironmentVariableA("UURB_INPUT_ROUTE", value,
                                           sizeof(value));
    return length > 0 && length < sizeof(value) &&
           strcmp(value, "rdp-public") == 0;
}

static UINT WINAPI bridged_send_input(UINT count, LPINPUT inputs, int size)
{
    char line[512];
    HWND relay;
    UINT result;
    DWORD error;
    LONG call_number;
    DWORD first_type = UINT32_MAX;
    DWORD first_flags = 0;
    BOOL used_broker = FALSE;
    BOOL unicode_keyboard;
    BOOL physical_keyboard;
    BOOL mouse_input;
    const char *category;
    LONG category_call_number;
    ULONGLONG started_ms;
    ULONGLONG direct_started_ms;
    ULONGLONG broker_started_ms;
    DWORD direct_ms = 0;
    DWORD broker_ms = 0;

    started_ms = GetTickCount64();

    relay = public_rdp_route() ? NULL : find_relay_window();
    if (relay != NULL)
        SetForegroundWindow(relay);

    if (count > 0 && inputs != NULL && size == (int)sizeof(INPUT)) {
        first_type = inputs[0].type;
        if (first_type == INPUT_MOUSE)
            first_flags = inputs[0].mi.dwFlags;
        else if (first_type == INPUT_KEYBOARD)
            first_flags = inputs[0].ki.dwFlags;
    }

    unicode_keyboard = contains_unicode_keyboard(count, inputs, size);
    physical_keyboard = !unicode_keyboard &&
                        contains_input_type(count, inputs, size,
                                            INPUT_KEYBOARD);
    mouse_input = !unicode_keyboard && !physical_keyboard &&
                  contains_input_type(count, inputs, size, INPUT_MOUSE);
    if (unicode_keyboard || public_rdp_route()) {
        broker_started_ms = GetTickCount64();
        result = send_through_broker(count, inputs, size, &error);
        broker_ms = (DWORD)(GetTickCount64() - broker_started_ms);
        used_broker = TRUE;
        SetLastError(error);
    } else {
        direct_started_ms = GetTickCount64();
        SetLastError(ERROR_SUCCESS);
        result = original_send_input(count, inputs, size);
        error = GetLastError();
        direct_ms = (DWORD)(GetTickCount64() - direct_started_ms);
        if (result < count && inputs != NULL && size == (int)sizeof(INPUT)) {
            broker_started_ms = GetTickCount64();
            result += send_through_broker(count - result, inputs + result,
                                          size, &error);
            broker_ms = (DWORD)(GetTickCount64() - broker_started_ms);
            used_broker = TRUE;
            SetLastError(error);
        }
    }
    call_number = InterlockedIncrement(&input_call_count);
    if (unicode_keyboard) {
        category = "text";
        category_call_number = InterlockedIncrement(&text_call_count);
    } else if (physical_keyboard) {
        category = "keyboard";
        category_call_number = InterlockedIncrement(&keyboard_call_count);
    } else if (mouse_input) {
        category = "mouse";
        category_call_number = InterlockedIncrement(&mouse_call_count);
    } else {
        category = "other";
        category_call_number = InterlockedIncrement(&other_call_count);
    }

    if ((unicode_keyboard && category_call_number <= 256) ||
        (physical_keyboard && category_call_number <= 256) ||
        (mouse_input && category_call_number <= 32) ||
        (!unicode_keyboard && !physical_keyboard && !mouse_input &&
         category_call_number <= 64) ||
        result != count) {
        _snprintf(line, sizeof(line),
                  "call=%ld category=%s category-call=%ld count=%lu type=%lu flags=0x%08lx route=%s direct-ms=%lu broker-ms=%lu total-ms=%lu result=%lu error=%lu\r\n",
                  call_number, category, category_call_number,
                  (unsigned long)count,
                  (unsigned long)first_type, (unsigned long)first_flags,
                  used_broker ? "broker" : "direct",
                  (unsigned long)direct_ms, (unsigned long)broker_ms,
                  (unsigned long)(GetTickCount64() - started_ms),
                  (unsigned long)result, (unsigned long)error);
        line[sizeof(line) - 1] = '\0';
        write_log(line);
        if (result != count)
            flush_log();
    }

    return result;
}

static BOOL patch_import(HMODULE module, const char *dll_name,
                         const char *function_name, uintptr_t replacement,
                         void *original)
{
    BYTE *base = (BYTE *)module;
    IMAGE_DOS_HEADER *dos = (IMAGE_DOS_HEADER *)base;
    IMAGE_NT_HEADERS *nt;
    IMAGE_IMPORT_DESCRIPTOR *descriptor;

    if (dos->e_magic != IMAGE_DOS_SIGNATURE)
        return FALSE;

    nt = (IMAGE_NT_HEADERS *)(base + dos->e_lfanew);
    if (nt->Signature != IMAGE_NT_SIGNATURE)
        return FALSE;

    descriptor = (IMAGE_IMPORT_DESCRIPTOR *)(
        base + nt->OptionalHeader
                   .DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT]
                   .VirtualAddress);

    if ((BYTE *)descriptor == base)
        return FALSE;

    for (; descriptor->Name != 0; descriptor++) {
        const char *imported_dll = (const char *)(base + descriptor->Name);
        IMAGE_THUNK_DATA *names;
        IMAGE_THUNK_DATA *addresses;

        if (_stricmp(imported_dll, dll_name) != 0)
            continue;

        names = descriptor->OriginalFirstThunk != 0
                    ? (IMAGE_THUNK_DATA *)(base + descriptor->OriginalFirstThunk)
                    : (IMAGE_THUNK_DATA *)(base + descriptor->FirstThunk);
        addresses = (IMAGE_THUNK_DATA *)(base + descriptor->FirstThunk);

        for (; names->u1.AddressOfData != 0; names++, addresses++) {
            IMAGE_IMPORT_BY_NAME *import_name;
            DWORD old_protection;

            if (IMAGE_SNAP_BY_ORDINAL(names->u1.Ordinal))
                continue;

            import_name = (IMAGE_IMPORT_BY_NAME *)(
                base + names->u1.AddressOfData);
            if (strcmp((const char *)import_name->Name, function_name) != 0)
                continue;

            if (!VirtualProtect(&addresses->u1.Function,
                                sizeof(addresses->u1.Function),
                                PAGE_READWRITE, &old_protection))
                return FALSE;

            if (original != NULL) {
                uintptr_t address = (uintptr_t)addresses->u1.Function;

                memcpy(original, &address, sizeof(address));
            }
            /* Publish only after the hook's original function is available. */
            InterlockedExchangePointer(
                (PVOID volatile *)&addresses->u1.Function, (PVOID)replacement);
            FlushInstructionCache(GetCurrentProcess(),
                                  &addresses->u1.Function,
                                  sizeof(addresses->u1.Function));
            VirtualProtect(&addresses->u1.Function,
                           sizeof(addresses->u1.Function), old_protection,
                           &old_protection);
            return TRUE;
        }
    }

    return FALSE;
}

static DWORD WINAPI initialize_bridge(void *unused)
{
    BOOL input_patched;
    BOOL event_log_patched;
    BOOL shell_patched;

    (void)unused;
    open_log();
    InitializeCriticalSection(&broker_lock);
    broker_lock_initialized = TRUE;
    input_patched = patch_import(
        GetModuleHandleW(NULL), "USER32.dll", "SendInput",
        (uintptr_t)&bridged_send_input, &original_send_input);
    event_log_patched = patch_import(
        GetModuleHandleW(NULL), "wevtapi.dll", "EvtOpenPublisherMetadata",
        (uintptr_t)&safe_evt_open_publisher_metadata, NULL);
    shell_patched = patch_import(
        GetModuleHandleW(NULL), "SHELL32.dll", "ShellExecuteW",
        (uintptr_t)&bridged_shell_execute, &original_shell_execute);

    if (public_rdp_route()) {
        BOOL position = patch_import(GetModuleHandleW(NULL), "USER32.dll",
            "GetCursorPos", (uintptr_t)&public_get_cursor_pos, &original_get_cursor_pos);
        BOOL key = patch_import(GetModuleHandleW(NULL), "USER32.dll",
            "GetKeyState", (uintptr_t)&public_get_key_state, &original_get_key_state);
        BOOL async_key = patch_import(GetModuleHandleW(NULL), "USER32.dll",
            "GetAsyncKeyState", (uintptr_t)&public_get_async_key_state, &original_get_async_key_state);
        BOOL keyboard = patch_import(GetModuleHandleW(NULL), "USER32.dll",
            "GetKeyboardState", (uintptr_t)&public_get_keyboard_state, &original_get_keyboard_state);
        write_log(keyboard ? "UU whole keyboard state hook active; unknown/toggle witness returns NOT_READY\r\n" :
                            "UU target has no direct GetKeyboardState import\r\n");
        write_log(position && key && async_key ?
            "UU public input LOCAL wrapper state hooks active; remote/cursor-shape witness pending\r\n" :
            "UU public input state import hook incomplete\r\n");
    }

    write_log(input_patched ? "UU SendInput bridge active\r\n"
                            : "UU bridge could not find SendInput import\r\n");
    write_log(event_log_patched
                  ? "UU Wine event-log compatibility active\r\n"
                  : "UU bridge could not find event-log import\r\n");
    write_log(shell_patched ? "UU native host shell actions active\r\n" :
                             "UU bridge could not find ShellExecuteW import\r\n");
    flush_log();
    return input_patched && event_log_patched ? 0 : 1;
}

BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, LPVOID reserved)
{
    HANDLE thread;

    (void)reserved;

    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(instance);
        thread = CreateThread(NULL, 0, initialize_bridge, NULL, 0, NULL);
        if (thread != NULL)
            CloseHandle(thread);
    } else if (reason == DLL_PROCESS_DETACH) {
        disconnect_broker();
        if (broker_lock_initialized) {
            DeleteCriticalSection(&broker_lock);
            broker_lock_initialized = FALSE;
        }
        if (log_file != INVALID_HANDLE_VALUE) {
            CloseHandle(log_file);
            log_file = INVALID_HANDLE_VALUE;
        }
    }

    return TRUE;
}
