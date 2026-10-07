#define UNICODE
#define _UNICODE
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <wchar.h>

#define DEFAULT_CURSOR_SIZE 48U
#define MIN_CURSOR_SIZE 24U
#define MAX_CURSOR_SIZE 128U

typedef HCURSOR(WINAPI *set_cursor_fn)(HCURSOR);
typedef BOOL(WINAPI *get_cursor_info_fn)(PCURSORINFO);
typedef BOOL(WINAPI *get_icon_info_fn)(HICON, PICONINFO);

static set_cursor_fn original_set_cursor;
static get_cursor_info_fn original_get_cursor_info;
static get_icon_info_fn original_get_icon_info;
static HCURSOR fallback_cursor;
static BOOL fallback_cursor_owned;
static HINSTANCE guard_instance;
static UINT fallback_cursor_width;
static UINT fallback_cursor_height;
static HANDLE log_file = INVALID_HANDLE_VALUE;
static SRWLOCK log_lock = SRWLOCK_INIT;
static volatile LONG hidden_cursor_count;

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

static void default_log_path(wchar_t *path)
{
    DWORD length;

    length = GetTempPathW(MAX_PATH, path);
    if (length == 0 || length >= MAX_PATH - 20)
        lstrcpynW(path, L"uu-cursor-guard.log", MAX_PATH);
    else
        lstrcatW(path, L"uu-cursor-guard.log");
}

static BOOL adjacent_cursor_path(const wchar_t *filename, wchar_t *path)
{
    DWORD length;
    wchar_t *separator;

    length = GetModuleFileNameW(guard_instance, path, MAX_PATH);
    if (length == 0 || length >= MAX_PATH)
        return FALSE;
    separator = wcsrchr(path, L'\\');
    if (separator == NULL ||
        (size_t)(separator - path + 1) + wcslen(filename) >= MAX_PATH)
        return FALSE;
    lstrcpyW(separator + 1, filename);
    return TRUE;
}

static void open_log(const wchar_t *process_name)
{
    wchar_t path[MAX_PATH];
    wchar_t configuration[MAX_PATH];
    DWORD length = 0;

    /*
     * Wine services do not reliably inherit the bridge environment. Keep the
     * server guard on its process-local temp path even when launch order
     * changes; the relay guard reads its persistent path before the environment.
     */
    if (_wcsicmp(process_name, L"GameViewerServer.exe") != 0) {
        if (_wcsicmp(process_name, L"sdl-freerdp.exe") == 0 &&
            adjacent_cursor_path(L"uu-cursor.ini", configuration)) {
            length = GetPrivateProfileStringW(
                L"Cursor", L"RelayLogPath", L"", path, MAX_PATH,
                configuration);
            if (length >= MAX_PATH - 1 || length < 3 ||
                path[1] != L':' || path[2] != L'\\' ||
                ((path[0] < L'A' || path[0] > L'Z') &&
                 (path[0] < L'a' || path[0] > L'z')))
                length = 0;
        }
        if (length == 0)
            length = GetEnvironmentVariableW(
                L"UURB_CURSOR_GUARD_LOG", path, MAX_PATH);
    }
    if (length == 0 || length >= MAX_PATH)
        default_log_path(path);

    log_file = CreateFileW(path, FILE_APPEND_DATA,
                           FILE_SHARE_READ | FILE_SHARE_WRITE, NULL,
                           OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
}

static UINT configured_cursor_size(void)
{
    wchar_t value[16];
    wchar_t path[MAX_PATH];
    wchar_t *end;
    unsigned long parsed;
    DWORD length;

    /* Wine services rebuild their environment; the adjacent file survives. */
    if (adjacent_cursor_path(L"uu-cursor.ini", path)) {
        length = GetPrivateProfileStringW(
            L"Cursor", L"Size", L"", value,
            sizeof(value) / sizeof(value[0]), path);
        if (length > 0 && length < sizeof(value) / sizeof(value[0])) {
            parsed = wcstoul(value, &end, 10);
            if (end != value && *end == L'\0' &&
                parsed >= MIN_CURSOR_SIZE && parsed <= MAX_CURSOR_SIZE)
                return (UINT)parsed;
        }
    }
    length = GetEnvironmentVariableW(
        L"UURB_CURSOR_SIZE", value, sizeof(value) / sizeof(value[0]));
    if (length == 0 || length >= sizeof(value) / sizeof(value[0]))
        return DEFAULT_CURSOR_SIZE;

    parsed = wcstoul(value, &end, 10);
    if (end == value || *end != L'\0' ||
        parsed < MIN_CURSOR_SIZE || parsed > MAX_CURSOR_SIZE)
        return DEFAULT_CURSOR_SIZE;
    return (UINT)parsed;
}

static BOOL cursor_dimensions(HCURSOR cursor, UINT *width, UINT *height)
{
    ICONINFO icon;
    BITMAP bitmap;
    HBITMAP source;
    LONG bitmap_width;
    LONG bitmap_height;
    BOOL result = FALSE;

    ZeroMemory(&icon, sizeof(icon));
    ZeroMemory(&bitmap, sizeof(bitmap));
    if (!GetIconInfo(cursor, &icon))
        return FALSE;

    source = icon.hbmColor != NULL ? icon.hbmColor : icon.hbmMask;
    if (source != NULL &&
        GetObjectW(source, (int)sizeof(bitmap), &bitmap) != 0) {
        bitmap_width =
            bitmap.bmWidth < 0 ? -bitmap.bmWidth : bitmap.bmWidth;
        bitmap_height =
            bitmap.bmHeight < 0 ? -bitmap.bmHeight : bitmap.bmHeight;
        if (icon.hbmColor == NULL)
            bitmap_height /= 2;
        if (bitmap_width > 0 && bitmap_height > 0) {
            *width = (UINT)bitmap_width;
            *height = (UINT)bitmap_height;
            result = TRUE;
        }
    }

    if (icon.hbmMask != NULL)
        DeleteObject(icon.hbmMask);
    if (icon.hbmColor != NULL)
        DeleteObject(icon.hbmColor);
    return result;
}

static BOOL load_fallback_cursor(void)
{
    HCURSOR base_cursor;
    UINT requested_size;
    wchar_t path[MAX_PATH];

    requested_size = configured_cursor_size();
    if (adjacent_cursor_path(L"uu-cursor.cur", path)) {
        fallback_cursor = (HCURSOR)LoadImageW(
            NULL, path, IMAGE_CURSOR, (int)requested_size,
            (int)requested_size, LR_LOADFROMFILE);
        if (fallback_cursor != NULL) {
            if (cursor_dimensions(fallback_cursor, &fallback_cursor_width,
                                  &fallback_cursor_height) &&
                fallback_cursor_width == requested_size &&
                fallback_cursor_height == requested_size) {
                fallback_cursor_owned = TRUE;
                return TRUE;
            }
            DestroyCursor(fallback_cursor);
            fallback_cursor = NULL;
        }
    }
    base_cursor = LoadCursorW(NULL, IDC_ARROW);
    if (base_cursor == NULL)
        return FALSE;

    fallback_cursor = (HCURSOR)CopyImage(
        base_cursor, IMAGE_CURSOR, (int)requested_size,
        (int)requested_size, LR_DEFAULTCOLOR);
    fallback_cursor_owned = fallback_cursor != NULL;
    if (fallback_cursor == NULL ||
        !cursor_dimensions(fallback_cursor, &fallback_cursor_width,
                           &fallback_cursor_height) ||
        fallback_cursor_width != requested_size ||
        fallback_cursor_height != requested_size) {
        if (fallback_cursor != NULL)
            DestroyCursor(fallback_cursor);
        fallback_cursor = base_cursor;
        fallback_cursor_owned = FALSE;
        if (!cursor_dimensions(fallback_cursor, &fallback_cursor_width,
                               &fallback_cursor_height))
            return FALSE;
    }
    return TRUE;
}

static void write_active_log(const char *guard_name)
{
    char message[128];
    int length;

    length = snprintf(message, sizeof(message),
                      "%s active (cursor %ux%u)\r\n", guard_name,
                      fallback_cursor_width, fallback_cursor_height);
    if (length > 0 && (size_t)length < sizeof(message))
        write_log(message);
}

static HCURSOR WINAPI guarded_set_cursor(HCURSOR cursor)
{
    if (cursor == NULL) {
        cursor = fallback_cursor;
        if (InterlockedIncrement(&hidden_cursor_count) == 1)
            write_log("UU cursor guard replaced a hidden cursor\r\n");
    }

    if (original_set_cursor == NULL)
        return NULL;
    return original_set_cursor(cursor);
}

static BOOL WINAPI guarded_get_cursor_info(PCURSORINFO cursor)
{
    BOOL result;
    ICONINFO icon;

    if (original_get_cursor_info == NULL)
        return FALSE;
    result = original_get_cursor_info(cursor);
    if (result && cursor != NULL) {
        if ((cursor->flags & CURSOR_SHOWING) != 0 &&
            cursor->hCursor != NULL) {
            ZeroMemory(&icon, sizeof(icon));
            if (GetIconInfo(cursor->hCursor, &icon)) {
                if (icon.hbmMask != NULL)
                    DeleteObject(icon.hbmMask);
                if (icon.hbmColor != NULL)
                    DeleteObject(icon.hbmColor);
                return result;
            }
            if (GetLastError() != ERROR_INVALID_CURSOR_HANDLE)
                return result;
        }
        cursor->flags = CURSOR_SHOWING;
        cursor->hCursor = fallback_cursor;
    }
    return result;
}

static BOOL WINAPI guarded_get_icon_info(HICON icon, PICONINFO info)
{
    BOOL result;
    DWORD error;

    if (original_get_icon_info == NULL)
        return FALSE;
    result = original_get_icon_info(icon, info);
    if (result)
        return TRUE;

    error = GetLastError();
    if (error != ERROR_INVALID_CURSOR_HANDLE || fallback_cursor == NULL)
        return FALSE;
    return original_get_icon_info(fallback_cursor, info);
}

static BOOL cursor_reader_self_test(void)
{
    CURSORINFO cursor;
    ICONINFO icon;

    ZeroMemory(&cursor, sizeof(cursor));
    cursor.cbSize = sizeof(cursor);
    if (!guarded_get_cursor_info(&cursor) ||
        cursor.hCursor == NULL ||
        fallback_cursor_width == 0 || fallback_cursor_height == 0 ||
        (cursor.flags & CURSOR_SHOWING) == 0)
        return FALSE;

    ZeroMemory(&icon, sizeof(icon));
    if (!guarded_get_icon_info(cursor.hCursor, &icon))
        return FALSE;
    if (icon.hbmMask != NULL)
        DeleteObject(icon.hbmMask);
    if (icon.hbmColor != NULL)
        DeleteObject(icon.hbmColor);
    return TRUE;
}

typedef struct cursor_guard_import {
    HMODULE module;
    const char *function;
    const char *label;
    uintptr_t replacement;
    PVOID volatile *slot;
    uintptr_t original;
    DWORD protection;
    BOOL writable;
    BOOL changed;
} cursor_guard_import;

static void log_guard_failure(const char *stage, const char *entry, DWORD error)
{
    char message[256];

    snprintf(message, sizeof(message),
             "UU cursor guard stage=%s entry=%s error=%lu\r\n",
             stage, entry, (unsigned long)error);
    write_log(message);
    SetLastError(error);
}

static PVOID volatile *find_import(HMODULE module, const char *function_name)
{
    BYTE *base = (BYTE *)module;
    IMAGE_DOS_HEADER *dos = (IMAGE_DOS_HEADER *)base;
    IMAGE_NT_HEADERS *nt;
    IMAGE_IMPORT_DESCRIPTOR *descriptor;

    if (module == NULL) {
        SetLastError(ERROR_MOD_NOT_FOUND);
        return NULL;
    }
    if (dos->e_magic != IMAGE_DOS_SIGNATURE) {
        SetLastError(ERROR_BAD_EXE_FORMAT);
        return NULL;
    }

    nt = (IMAGE_NT_HEADERS *)(base + dos->e_lfanew);
    if (nt->Signature != IMAGE_NT_SIGNATURE) {
        SetLastError(ERROR_BAD_EXE_FORMAT);
        return NULL;
    }

    descriptor = (IMAGE_IMPORT_DESCRIPTOR *)(
        base + nt->OptionalHeader
                   .DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT]
                   .VirtualAddress);
    if ((BYTE *)descriptor == base) {
        SetLastError(ERROR_PROC_NOT_FOUND);
        return NULL;
    }

    for (; descriptor->Name != 0; descriptor++) {
        const char *imported_dll = (const char *)(base + descriptor->Name);
        IMAGE_THUNK_DATA *names;
        IMAGE_THUNK_DATA *addresses;

        if (_stricmp(imported_dll, "USER32.dll") != 0)
            continue;

        names = descriptor->OriginalFirstThunk != 0
                    ? (IMAGE_THUNK_DATA *)(base + descriptor->OriginalFirstThunk)
                    : (IMAGE_THUNK_DATA *)(base + descriptor->FirstThunk);
        addresses = (IMAGE_THUNK_DATA *)(base + descriptor->FirstThunk);

        for (; names->u1.AddressOfData != 0; names++, addresses++) {
            IMAGE_IMPORT_BY_NAME *import_name;

            if (IMAGE_SNAP_BY_ORDINAL(names->u1.Ordinal))
                continue;
            import_name = (IMAGE_IMPORT_BY_NAME *)(
                base + names->u1.AddressOfData);
            if (strcmp((const char *)import_name->Name, function_name) != 0)
                continue;

            return (PVOID volatile *)&addresses->u1.Function;
        }
    }

    SetLastError(ERROR_PROC_NOT_FOUND);
    return NULL;
}

static BOOL install_guard_imports(cursor_guard_import *imports, size_t count,
                                  BOOL reader)
{
    size_t index;
    DWORD ignored;

    /* Locate every slot and verify originals before changing any IAT. */
    for (index = 0; index < count; index++) {
        imports[index].slot = find_import(imports[index].module,
                                          imports[index].function);
        if (imports[index].slot == NULL) {
            log_guard_failure("preflight", imports[index].label, GetLastError());
            return FALSE;
        }
        imports[index].original = (uintptr_t)*imports[index].slot;
        if (imports[index].original == 0 ||
            imports[index].original == imports[index].replacement) {
            log_guard_failure("preflight-original", imports[index].label,
                              ERROR_INVALID_DATA);
            return FALSE;
        }
    }
    if (reader) {
        if (imports[0].original != imports[2].original ||
            imports[1].original != imports[3].original) {
            log_guard_failure("preflight-original-mismatch", "streamer.dll",
                              ERROR_INVALID_DATA);
            return FALSE;
        }
        original_get_cursor_info = (get_cursor_info_fn)imports[0].original;
        original_get_icon_info = (get_icon_info_fn)imports[1].original;
    } else {
        original_set_cursor = (set_cursor_fn)imports[0].original;
    }
    for (index = 0; index < count; index++) {
        if (!VirtualProtect((void *)imports[index].slot, sizeof(PVOID),
                            PAGE_READWRITE, &imports[index].protection)) {
            log_guard_failure("protect", imports[index].label, GetLastError());
            goto rollback;
        }
        imports[index].writable = TRUE;
    }
    for (index = 0; index < count; index++) {
        InterlockedExchangePointer(imports[index].slot,
                                   (PVOID)imports[index].replacement);
        imports[index].changed = TRUE;
        if (!FlushInstructionCache(GetCurrentProcess(),
                                   (void *)imports[index].slot, sizeof(PVOID))) {
            log_guard_failure("flush", imports[index].label, GetLastError());
            goto rollback;
        }
    }
    if (reader && !cursor_reader_self_test()) {
        DWORD error = GetLastError();

        log_guard_failure("self-test", "reader",
                          error != ERROR_SUCCESS ? error : ERROR_INVALID_DATA);
        goto rollback;
    }
    for (index = count; index > 0; index--) {
        if (!VirtualProtect((void *)imports[index - 1].slot, sizeof(PVOID),
                            imports[index - 1].protection, &ignored)) {
            log_guard_failure("restore-protection", imports[index - 1].label,
                              GetLastError());
            goto rollback;
        }
        imports[index - 1].writable = FALSE;
    }
    return TRUE;

rollback:
    /* Restore IAT entries while writable, then undo protections in reverse. */
    for (index = 0; index < count; index++) {
        if (imports[index].changed && !imports[index].writable) {
            if (!VirtualProtect((void *)imports[index].slot, sizeof(PVOID),
                                PAGE_READWRITE, &ignored)) {
                log_guard_failure("rollback-protect", imports[index].label,
                                  GetLastError());
                continue;
            }
            imports[index].writable = TRUE;
        }
    }
    for (index = count; index > 0; index--) {
        if (imports[index - 1].changed) {
            if (!imports[index - 1].writable) {
                log_guard_failure("rollback-unrestored-import",
                                  imports[index - 1].label, ERROR_ACCESS_DENIED);
                continue;
            }
            InterlockedExchangePointer(imports[index - 1].slot,
                                       (PVOID)imports[index - 1].original);
            if (!FlushInstructionCache(GetCurrentProcess(),
                                       (void *)imports[index - 1].slot,
                                       sizeof(PVOID)))
                log_guard_failure("rollback-flush", imports[index - 1].label,
                                  GetLastError());
        }
    }
    for (index = count; index > 0; index--) {
        if (imports[index - 1].writable &&
            !VirtualProtect((void *)imports[index - 1].slot, sizeof(PVOID),
                            imports[index - 1].protection, &ignored))
            log_guard_failure("rollback-restore-protection",
                              imports[index - 1].label, GetLastError());
    }
    return FALSE;
}

static DWORD WINAPI initialize_guard(void *unused)
{
    HMODULE main_module;
    HMODULE streamer_module;
    HWND relay;
    wchar_t executable[MAX_PATH];
    const wchar_t *process_name;
    wchar_t *separator;

    (void)unused;
    if (GetModuleFileNameW(NULL, executable, MAX_PATH) == 0)
        return 1;
    separator = wcsrchr(executable, L'\\');
    process_name = separator == NULL ? executable : separator + 1;
    open_log(process_name);
    if (!load_fallback_cursor()) {
        log_guard_failure("fallback-load", "uu-cursor.cur/IDC_ARROW",
                          GetLastError());
        write_log("UU cursor guard initialization failed\r\n");
        if (log_file != INVALID_HANDLE_VALUE)
            FlushFileBuffers(log_file);
        return 1;
    }

    main_module = GetModuleHandleW(NULL);
    if (_wcsicmp(process_name, L"sdl-freerdp.exe") == 0) {
        cursor_guard_import imports[] = {
            { .module = main_module, .function = "SetCursor",
              .label = "sdl-freerdp.exe!SetCursor",
              .replacement = (uintptr_t)&guarded_set_cursor }
        };

        if (!install_guard_imports(imports, 1, FALSE)) {
            write_log("UU cursor guard initialization failed\r\n");
            if (log_file != INVALID_HANDLE_VALUE)
                FlushFileBuffers(log_file);
            return 1;
        }

        original_set_cursor(fallback_cursor);
        relay = FindWindowW(NULL, L"Ubuntu-Desktop-Relay");
        if (relay != NULL)
            PostMessageW(relay, WM_SETCURSOR, (WPARAM)relay,
                         MAKELPARAM(HTCLIENT, WM_MOUSEMOVE));
        write_active_log("UU relay cursor guard");
    } else if (_wcsicmp(process_name, L"GameViewerServer.exe") == 0) {
        streamer_module = GetModuleHandleW(L"streamer.dll");
        cursor_guard_import imports[] = {
            { .module = main_module, .function = "GetCursorInfo",
              .label = "GameViewerServer.exe!GetCursorInfo",
              .replacement = (uintptr_t)&guarded_get_cursor_info },
            { .module = main_module, .function = "GetIconInfo",
              .label = "GameViewerServer.exe!GetIconInfo",
              .replacement = (uintptr_t)&guarded_get_icon_info },
            { .module = streamer_module, .function = "GetCursorInfo",
              .label = "streamer.dll!GetCursorInfo",
              .replacement = (uintptr_t)&guarded_get_cursor_info },
            { .module = streamer_module, .function = "GetIconInfo",
              .label = "streamer.dll!GetIconInfo",
              .replacement = (uintptr_t)&guarded_get_icon_info }
        };

        if (!install_guard_imports(imports, 4, TRUE)) {
            write_log("UU cursor reader guard initialization failed\r\n");
            if (log_file != INVALID_HANDLE_VALUE)
                FlushFileBuffers(log_file);
            return 1;
        }
        write_active_log("UU cursor reader guard");
    } else {
        write_log("UU cursor guard initialization failed\r\n");
        if (log_file != INVALID_HANDLE_VALUE)
            FlushFileBuffers(log_file);
        return 1;
    }

    if (log_file != INVALID_HANDLE_VALUE)
        FlushFileBuffers(log_file);
    return 0;
}

BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, LPVOID reserved)
{
    HANDLE thread;

    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        guard_instance = instance;
        DisableThreadLibraryCalls(instance);
        thread = CreateThread(NULL, 0, initialize_guard, NULL, 0, NULL);
        if (thread != NULL)
            CloseHandle(thread);
    } else if (reason == DLL_PROCESS_DETACH) {
        if (fallback_cursor_owned && fallback_cursor != NULL)
            DestroyCursor(fallback_cursor);
        if (log_file != INVALID_HANDLE_VALUE) {
            CloseHandle(log_file);
            log_file = INVALID_HANDLE_VALUE;
        }
    }
    return TRUE;
}
