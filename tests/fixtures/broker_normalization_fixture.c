#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef uint32_t DWORD;
typedef uint64_t ULONGLONG;
typedef uint16_t WORD;
typedef uint16_t WCHAR;
typedef uint8_t BYTE;
typedef int16_t SHORT;
typedef unsigned UINT;
typedef int BOOL;
#define TRUE 1
#define FALSE 0
#define LOBYTE(v) ((BYTE)(v))
#define HIBYTE(v) ((BYTE)((v) >> 8))
#define ZeroMemory(p, n) memset(p, 0, n)
#define INPUT_MOUSE 0
#define INPUT_KEYBOARD 1
#define KEYEVENTF_KEYUP 2
#define KEYEVENTF_UNICODE 4
#define VK_BACK 8
#define VK_TAB 9
#define VK_RETURN 13
#define VK_SHIFT 16
#define VK_CONTROL 17
#define VK_MENU 18
#define ERROR_SUCCESS 0
#define ERROR_WRITE_FAULT 29
#define ERROR_NOT_SUPPORTED 50
#define ERROR_INVALID_PARAMETER 87
#define ERROR_NO_UNICODE_TRANSLATION 1113
#define INPUT_BRIDGE_MAX_INPUTS 2048UL
#define INPUT_BRIDGE_MAX_TRANSLATED_INPUTS (INPUT_BRIDGE_MAX_INPUTS * 8UL)
#define INPUT_BRIDGE_MAX_SEGMENTS (INPUT_BRIDGE_MAX_INPUTS + 1UL)

typedef struct {
    DWORD type;
    struct { WORD wVk, wScan; DWORD dwFlags, time; uintptr_t dwExtraInfo; } ki;
    struct { int32_t dx, dy; DWORD mouseData, dwFlags; } mi;
} INPUT;
typedef struct { DWORD offset, count; BOOL text; } input_segment;
typedef struct { DWORD removable_characters; ULONGLONG updated_ms; } semantic_edit_state;
typedef enum { X11_ROUTE_NOT_USED, X11_ROUTE_SUCCESS, X11_ROUTE_FAILED } x11_route_result;

static BOOL x11_input_semantic_only;
static DWORD text_key_delay_ms, physical_key_delay_ms;
static INPUT received[INPUT_BRIDGE_MAX_TRANSLATED_INPUTS];
static DWORD received_count, public_calls, semantic_calls, fallback_calls;
static DWORD reply_error, reply_count;
static BOOL use_reply, use_semantic;

static SHORT VkKeyScanW(WCHAR character)
{
    if (character >= 'a' && character <= 'z')
        return (SHORT)(character - 'a' + 'A');
    if (character >= 'A' && character <= 'Z')
        return (SHORT)(0x100 | character);
    if (character >= '0' && character <= '9')
        return (SHORT)character;
    if (character == '-')
        return 0xbd;
    return (SHORT)-1;
}
static BOOL uurb_rdp_enabled(void) { return TRUE; }
static DWORD uurb_rdp_send(DWORD count, const INPUT *inputs, DWORD *error)
{
    public_calls++;
    received_count = count;
    memcpy(received, inputs, count * sizeof(*inputs));
    /* Model the actual peer's physical-key capability, with Unicode absent.
     * Validation must finish before any modeled wrapper effects. */
    if (count > INPUT_BRIDGE_MAX_INPUTS) {
        *error = ERROR_INVALID_PARAMETER;
        return 0;
    }
    for (DWORD index = 0; index < count; index++) {
        if (inputs[index].type == INPUT_KEYBOARD &&
            (inputs[index].ki.dwFlags & KEYEVENTF_UNICODE) != 0) {
            *error = ERROR_NOT_SUPPORTED;
            return 0;
        }
    }
    *error = use_reply ? reply_error : ERROR_SUCCESS;
    return use_reply ? reply_count : count;
}
static BOOL phone_text_uses_clipboard(DWORD count, const INPUT *inputs)
{
    (void)count; (void)inputs;
    return use_semantic;
}
static x11_route_result send_x11_clipboard_text(
    DWORD count, const INPUT *inputs, DWORD *error, BOOL *considered,
    semantic_edit_state *edit_state, DWORD *clamped_edits)
{
    (void)count; (void)inputs; (void)edit_state; (void)clamped_edits;
    semantic_calls++;
    *considered = TRUE;
    *error = ERROR_SUCCESS;
    return X11_ROUTE_SUCCESS;
}
static x11_route_result send_x11_inputs(DWORD count, const INPUT *inputs,
                                       DWORD *error, BOOL *considered)
{
    (void)count; (void)inputs; (void)error; (void)considered;
    fallback_calls++;
    return X11_ROUTE_NOT_USED;
}
static BOOL request_relay_focus(DWORD *waited_ms)
{
    *waited_ms = 0;
    fallback_calls++;
    return FALSE;
}
static DWORD GetLastError(void) { return ERROR_NOT_SUPPORTED; }
static void SetLastError(DWORD error) { (void)error; }
static ULONGLONG GetTickCount64(void) { return 100; }
static void Sleep(DWORD delay) { (void)delay; fallback_calls++; }
static UINT SendInput(DWORD count, INPUT *inputs, int size)
{
    (void)count; (void)inputs; (void)size;
    fallback_calls++;
    return 0;
}
static void reset_semantic_edit_state(semantic_edit_state *state) { (void)state; }
static void expire_semantic_edit_state(semantic_edit_state *state, ULONGLONG now)
{ (void)state; (void)now; }
static void credit_semantic_text(semantic_edit_state *state, DWORD count, const INPUT *inputs)
{ (void)state; (void)count; (void)inputs; }

#include "broker-functions.inc"

#define CHECK(expression) do { if (!(expression)) { \
    fprintf(stderr, "line %u: %s\n", __LINE__, #expression); exit(1); } } while (0)

static void reset(void)
{
    public_calls = semantic_calls = fallback_calls = received_count = 0;
    reply_error = ERROR_SUCCESS;
    reply_count = 0;
    use_reply = use_semantic = FALSE;
    memset(received, 0, sizeof(received));
}
static INPUT key(WORD virtual_key, WORD scan, DWORD flags)
{
    INPUT input = {0};
    input.type = INPUT_KEYBOARD;
    input.ki.wVk = virtual_key;
    input.ki.wScan = scan;
    input.ki.dwFlags = flags;
    return input;
}
static DWORD dispatch(DWORD count, const INPUT *inputs, DWORD *error, BOOL *normalized)
{
    DWORD characters, physical, wait, clamped;
    BOOL focus;
    const char *route;
    semantic_edit_state state = {0};
    DWORD result = send_relay_inputs(count, inputs, error, normalized,
                                    &characters, &physical, &focus, &wait,
                                    &route, &state, &clamped);
    CHECK(fallback_calls == 0);
    CHECK(focus && wait == 0 && characters == 0 && physical == 0);
    CHECK(strcmp(route, use_semantic ? "rdp-public-owner-barrier" : "rdp-public-wrapper") == 0 ||
          public_calls == 0);
    return result;
}
static void check_key(DWORD index, WORD virtual_key, DWORD flags)
{
    CHECK(received[index].type == INPUT_KEYBOARD);
    CHECK(received[index].ki.wVk == virtual_key);
    CHECK(received[index].ki.wScan == 0);
    CHECK(received[index].ki.dwFlags == flags);
}
int main(void)
{
    DWORD error;
    BOOL normalized;
    INPUT letters[] = {
        key(0, 'a', KEYEVENTF_UNICODE), key(0, 'a', KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
        key(0, 'A', KEYEVENTF_UNICODE), key(0, 'A', KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
        key(0, '-', KEYEVENTF_UNICODE), key(0, '-', KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)
    };
    reset();
    CHECK(dispatch(6, letters, &error, &normalized) == 6);
    CHECK(normalized && error == ERROR_SUCCESS && public_calls == 1 && received_count == 8);
    check_key(0, 'A', 0); check_key(1, 'A', KEYEVENTF_KEYUP);
    check_key(2, VK_SHIFT, 0); check_key(3, 'A', 0);
    check_key(4, 'A', KEYEVENTF_KEYUP); check_key(5, VK_SHIFT, KEYEVENTF_KEYUP);
    check_key(6, 0xbd, 0); check_key(7, 0xbd, KEYEVENTF_KEYUP);

    INPUT physical[] = {key(VK_CONTROL, 0, 0), key('V', 0, 0), key('V', 0, KEYEVENTF_KEYUP)};
    physical[0].ki.dwExtraInfo = 19;
    reset();
    CHECK(dispatch(3, physical, &error, &normalized) == 3);
    CHECK(!normalized && received_count == 3 && !memcmp(received, physical, sizeof(physical)));
    use_reply = TRUE; reply_count = 1; reply_error = ERROR_WRITE_FAULT;
    CHECK(dispatch(3, physical, &error, &normalized) == 1);
    CHECK(error == ERROR_WRITE_FAULT && public_calls == 2);

    INPUT mixed[] = {physical[0], letters[2], letters[3], key(VK_CONTROL, 0, KEYEVENTF_KEYUP)};
    reset();
    CHECK(dispatch(4, mixed, &error, &normalized) == 4);
    CHECK(normalized && received_count == 6 && public_calls == 1);
    CHECK(!memcmp(&received[0], &mixed[0], sizeof(INPUT)));
    check_key(1, VK_SHIFT, 0); check_key(2, 'A', 0);
    check_key(3, 'A', KEYEVENTF_KEYUP); check_key(4, VK_SHIFT, KEYEVENTF_KEYUP);
    CHECK(!memcmp(&received[5], &mixed[3], sizeof(INPUT)));

    reset(); use_reply = TRUE; reply_count = 3; reply_error = ERROR_WRITE_FAULT;
    CHECK(dispatch(6, letters, &error, &normalized) == 0);
    CHECK(error == ERROR_WRITE_FAULT && public_calls == 1 && semantic_calls == 0);
    reset(); use_reply = TRUE; reply_count = 8; reply_error = ERROR_WRITE_FAULT;
    CHECK(dispatch(6, letters, &error, &normalized) == 0 && public_calls == 1);

    INPUT unsupported[] = {letters[0], letters[1], key(0, 0x4e2d, KEYEVENTF_UNICODE)};
    reset();
    CHECK(dispatch(3, unsupported, &error, &normalized) == 0);
    CHECK(error == ERROR_NO_UNICODE_TRANSLATION && public_calls == 0);
    reset(); use_semantic = TRUE;
    CHECK(dispatch(3, unsupported, &error, &normalized) == 3);
    CHECK(error == ERROR_SUCCESS && semantic_calls == 1 && public_calls == 0);

    INPUT release = key(0, 'A', KEYEVENTF_UNICODE | KEYEVENTF_KEYUP);
    reset();
    CHECK(dispatch(1, &release, &error, &normalized) == 1);
    CHECK(normalized && error == ERROR_SUCCESS && public_calls == 0);

    INPUT *large = calloc(INPUT_BRIDGE_MAX_INPUTS, sizeof(*large));
    CHECK(large != NULL);
    for (DWORD index = 0; index < INPUT_BRIDGE_MAX_INPUTS; index++)
        large[index] = letters[2];
    reset();
    CHECK(dispatch(INPUT_BRIDGE_MAX_INPUTS, large, &error, &normalized) == 0);
    CHECK(error == ERROR_INVALID_PARAMETER && public_calls == 1 && received_count == 8192);
    free(large);
    puts("PASS 7 broker normalization groups; fake Win32/public callbacks only");
    return 0;
}
