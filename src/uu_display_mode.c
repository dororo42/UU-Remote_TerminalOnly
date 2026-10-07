#define UNICODE
#define _UNICODE
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

static BOOL current_mode(DEVMODEW *mode)
{
    ZeroMemory(mode, sizeof(*mode));
    mode->dmSize = sizeof(*mode);
    return EnumDisplaySettingsExW(NULL, ENUM_CURRENT_SETTINGS, mode, 0);
}

static BOOL relay_matches(HWND relay, DWORD width, DWORD height)
{
    RECT rectangle;
    return IsWindow(relay) && GetWindowRect(relay, &rectangle) &&
           rectangle.left == 0 && rectangle.top == 0 &&
           rectangle.right == (LONG)width && rectangle.bottom == (LONG)height &&
           GetSystemMetrics(SM_CXSCREEN) == (int)width &&
           GetSystemMetrics(SM_CYSCREEN) == (int)height;
}

static BOOL wait_mode(HWND relay, DWORD width, DWORD height)
{
    ULONGLONG deadline = GetTickCount64() + 5000;
    do {
        DEVMODEW current;
        if (current_mode(&current) && current.dmPelsWidth == width &&
            current.dmPelsHeight == height && relay_matches(relay, width, height))
            return TRUE;
        Sleep(20);
    } while (GetTickCount64() < deadline);
    return FALSE;
}

int main(int argc, char **argv)
{
    DWORD width, height;
    DEVMODEW before, selected;
    HWND relay;
    wchar_t class_name[64];
    int consumed = 0;
    BOOL found = FALSE;
    setvbuf(stdout, NULL, _IONBF, 0);
    if (argc != 2 || sscanf(argv[1], "%lux%lu%n", &width, &height, &consumed) != 2 ||
        argv[1][consumed] != '\0' ||
        !((width == 1280 && height == 720) || (width == 1920 && height == 1080) ||
          (width == 2560 && height == 1440) || (width == 3840 && height == 2160) ||
          (width == 5120 && height == 2880)))
        return 2;
    relay = FindWindowW(NULL, L"Ubuntu-Desktop-Relay");
    if (relay == NULL || !GetClassNameW(relay, class_name, 64) ||
        wcscmp(class_name, L"SDL_app") != 0 || !current_mode(&before)) {
        fputs("Verified fullscreen SDL relay is unavailable.\n", stderr);
        return 3;
    }
    for (DWORD index = 0; index < 512; ++index) {
        ZeroMemory(&selected, sizeof(selected));
        selected.dmSize = sizeof(selected);
        if (!EnumDisplaySettingsExW(NULL, index, &selected, 0))
            break;
        if (selected.dmPelsWidth == width && selected.dmPelsHeight == height &&
            selected.dmBitsPerPel == 32 && selected.dmDisplayFrequency == 60) {
            found = TRUE;
            break;
        }
    }
    if (!found) {
        fputs("Requested genuine 32bpp/60Hz display mode is unavailable.\n", stderr);
        return 3;
    }
    LONG result = ChangeDisplaySettingsExW(NULL, &selected, NULL, 0, NULL);
    if (result == DISP_CHANGE_SUCCESSFUL && wait_mode(relay, width, height)) {
        printf("{\"resolution\":\"%lux%lu\",\"fullscreen\":true}\n", width, height);
        return 0;
    }
    fprintf(stderr, "Display change failed fullscreen validation (CDS=%ld); restoring prior live mode.\n", result);
    result = ChangeDisplaySettingsExW(NULL, &before, NULL, 0, NULL);
    if (result != DISP_CHANGE_SUCCESSFUL ||
        !wait_mode(relay, before.dmPelsWidth, before.dmPelsHeight)) {
        fputs("Prior live mode recovery is not confirmed.\n", stderr);
        return 5;
    }
    fputs("Prior live mode restored.\n", stderr);
    return 4;
}
