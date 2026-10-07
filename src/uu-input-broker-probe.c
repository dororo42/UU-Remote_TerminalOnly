#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include "uurb_ready.h"
#include "uurb_rdp_backend.h"

static BOOL broker_peer(HANDLE pipe, DWORD pid, uint64_t creation)
{
    ULONG actual = 0;
    if (!GetNamedPipeServerProcessId(pipe, &actual) || actual != pid)
        return FALSE;
    HANDLE process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
    if (!process)
        return FALSE;
    uint64_t started = uurb_creation(process);
    CloseHandle(process);
    return started && started == creation;
}

static BOOL io(HANDLE pipe, BOOL writing, void *buffer, DWORD bytes,
               DWORD pid, uint64_t started, ULONGLONG deadline)
{
    BYTE *position = buffer;
    while (bytes) {
        DWORD count = 0;
        if (GetTickCount64() >= deadline || !broker_peer(pipe, pid, started))
            return FALSE;
        if (!writing) {
            DWORD available = 0;
            if (!PeekNamedPipe(pipe, NULL, 0, NULL, &available, NULL))
                return FALSE;
            if (!available) {
                Sleep(1);
                continue;
            }
        }
        BOOL ok = writing ? WriteFile(pipe, position, bytes, &count, NULL) :
                            ReadFile(pipe, position, bytes, &count, NULL);
        if (!ok && GetLastError() != ERROR_NO_DATA)
            return FALSE;
        if (!count) {
            Sleep(1);
            continue;
        }
        position += count;
        bytes -= count;
    }
    return GetTickCount64() < deadline;
}

int main(void)
{
    DWORD pid = 0;
    uint64_t started = 0;
    ULONGLONG deadline = GetTickCount64() + 1000;
    if (!uurb_ready_read(L"UURB_FULL_BROKER_READY", "broker", &pid, &started))
        return 1;
    while (!WaitNamedPipeW(L"\\\\.\\pipe\\uurb-input-v1", 20)) {
        DWORD error = GetLastError();
        if (GetTickCount64() >= deadline ||
            (error != ERROR_FILE_NOT_FOUND && error != ERROR_PIPE_BUSY &&
             error != ERROR_SEM_TIMEOUT))
            return 1;
        Sleep(5);
    }
    HANDLE pipe = CreateFileW(L"\\\\.\\pipe\\uurb-input-v1",
                             GENERIC_READ | GENERIC_WRITE, 0, NULL,
                             OPEN_EXISTING, 0, NULL);
    if (pipe == INVALID_HANDLE_VALUE)
        return 1;
    DWORD mode = PIPE_READMODE_BYTE | PIPE_NOWAIT;
    DWORD request[3] = {UURB_RDP_QUERY_MAGIC, 0, 0}, response[2] = {0};
    BOOL ok = SetNamedPipeHandleState(pipe, &mode, NULL, NULL) &&
              io(pipe, TRUE, request, sizeof(request), pid, started, deadline) &&
              io(pipe, FALSE, response, sizeof(response), pid, started, deadline) &&
              response[0] == 1 && response[1] == ERROR_SUCCESS;
    CloseHandle(pipe);
    printf("UURB public input readonly readiness=%s broker=%lu\n",
           ok ? "active" : "unready", (unsigned long)pid);
    return ok ? 0 : 1;
}
