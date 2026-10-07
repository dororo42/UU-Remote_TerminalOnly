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
typedef int32_t LONG;
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
#define ARRAYSIZE(a) (sizeof(a) / sizeof((a)[0]))
#define MAXDWORD UINT32_MAX
#define MAPVK_VK_TO_VSC_EX 4
#define KEYEVENTF_EXTENDEDKEY 1
#define KEYEVENTF_SCANCODE 8
#define VK_INSERT 45
#define ERROR_TIMEOUT 1460
#define ERROR_NOT_READY 21
#define ERROR_BUSY 170
#define ERROR_CONNECTION_ABORTED 1236
#define INPUT_BRIDGE_SEMANTIC_EDIT_WINDOW_MS 2000UL
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
typedef struct { DWORD removable_characters; ULONGLONG updated_ms; WCHAR pending_high_surrogate; BOOL previous_ended_cr; } semantic_edit_state;
typedef enum { PHONE_TEXT_MODE_AUTO, PHONE_TEXT_MODE_KEYS, PHONE_TEXT_MODE_CLIPBOARD } phone_text_mode;
#include "x11_input_protocol.h"
#include "full-input.h"
typedef enum { X11_ROUTE_NOT_USED, X11_ROUTE_SUCCESS, X11_ROUTE_FAILED } x11_route_result;

static BOOL x11_input_semantic_only;
static DWORD text_key_delay_ms, physical_key_delay_ms;
static INPUT received[INPUT_BRIDGE_MAX_TRANSLATED_INPUTS];
static DWORD received_count, public_calls, semantic_calls, fallback_calls;
static DWORD reply_error, reply_count;
static BOOL use_reply;

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
static phone_text_mode configured_phone_text_mode;
static BOOL x11_input_configured = TRUE;
static BOOL live = TRUE, idle = TRUE, lose_live_after_owner;
static ULONGLONG public_call_deadline = 2000;
static LONG x11_sequence = 1;
static DWORD validate_calls, owner_calls, barrier_calls, owner_failure, barrier_failure;
static DWORD validated_after_effect;
static WCHAR owner_text[INPUT_BRIDGE_MAX_INPUTS];
static DWORD owner_units;
static char effects[64];
static DWORD effects_count;
static UINT MapVirtualKeyW(UINT key, UINT mode)
{
    (void)mode;
    if (key == VK_SHIFT) return 0x2a;
    if (key == VK_CONTROL) return 0x1d;
    if (key == VK_MENU) return 0x38;
    return key && key < 256 ? key : 0;
}
static BOOL uurb_rdp_enabled(void) { return TRUE; }
static BOOL public_request_live(void) { return live; }
static ULONGLONG GetTickCount64(void) { return 100; }
static LONG InterlockedCompareExchange(LONG *value, LONG desired, LONG expected)
{
    LONG original = *value;
    if (original == expected) *value = desired;
    return original;
}
static BOOL uurb_rdp_semantic_idle(DWORD *error)
{
    *error = idle ? ERROR_SUCCESS : ERROR_BUSY;
    return idle;
}
static BOOL uurb_rdp_validate(DWORD count, const INPUT *inputs, DWORD *error)
{
    validate_calls++;
    if (effects_count) validated_after_effect++;
    *error = ERROR_NOT_SUPPORTED;
    for (DWORD i = 0; i < count; i++) {
        const INPUT *input = &inputs[i];
        uint32_t required;
        if (input->type == INPUT_MOUSE) {
            required = UI_CAP_MOUSE;
            if (input->mi.dwFlags & ~UINT32_C(0xe9ff)) return FALSE;
        } else if (input->type == INPUT_KEYBOARD) {
            required = (input->ki.dwFlags & KEYEVENTF_UNICODE) ? UI_CAP_UNICODE : UI_CAP_KEY;
            if (input->ki.dwFlags & ~(KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP |
                                      KEYEVENTF_UNICODE | KEYEVENTF_SCANCODE)) return FALSE;
            if (!MapVirtualKeyW(input->ki.wVk, MAPVK_VK_TO_VSC_EX) &&
                !(input->ki.dwFlags & KEYEVENTF_SCANCODE)) return FALSE;
        } else return FALSE;
        if ((UINT32_C(0x3fd) & required) != required) return FALSE;
    }
    *error = ERROR_SUCCESS;
    return TRUE;
}
static DWORD uurb_rdp_send(DWORD count, const INPUT *inputs, DWORD *error)
{
    public_calls++;
    received_count = count;
    memcpy(received, inputs, count * sizeof(*inputs));
    effects[effects_count++] = count == 4 && inputs[0].ki.wVk == VK_SHIFT ? 'P' : 'K';
    *error = use_reply ? reply_error : ERROR_SUCCESS;
    return use_reply ? reply_count : count;
}
static x11_route_result send_x11_events(DWORD count, const uurb_x11_input_event *events,
                                       DWORD *error)
{
    if (events[0].type == UURB_X11_CLIPBOARD_OWNER_ONLY) {
        owner_calls++;
        effects[effects_count++] = 'O';
        for (DWORD i = 0; i < count; i++) {
            if (events[i].type != UURB_X11_CLIPBOARD_OWNER_ONLY) abort();
            owner_text[owner_units++] = (WCHAR)events[i].data;
        }
        if (lose_live_after_owner) live = FALSE;
        *error = owner_failure;
        return owner_failure ? X11_ROUTE_FAILED : X11_ROUTE_SUCCESS;
    }
    if (events[0].type == UURB_X11_CLIPBOARD_BARRIER) {
        barrier_calls++;
        effects[effects_count++] = 'B';
        *error = barrier_failure;
        return barrier_failure ? X11_ROUTE_FAILED : X11_ROUTE_SUCCESS;
    }
    semantic_calls++;
    *error = ERROR_NOT_SUPPORTED;
    return X11_ROUTE_FAILED;
}
static void close_x11_input_socket(void) { }
static x11_route_result send_x11_inputs(DWORD count, const INPUT *inputs,
                                       DWORD *error, BOOL *considered)
{
    (void)count; (void)inputs; (void)error; (void)considered;
    fallback_calls++;
    return X11_ROUTE_FAILED;
}
static BOOL request_relay_focus(DWORD *waited_ms)
{
    *waited_ms = 0;
    fallback_calls++;
    return FALSE;
}
static DWORD GetLastError(void) { return ERROR_NOT_SUPPORTED; }
static void SetLastError(DWORD error) { (void)error; }
static void Sleep(DWORD delay) { (void)delay; fallback_calls++; }
static UINT SendInput(DWORD count, INPUT *inputs, int size)
{
    (void)count; (void)inputs; (void)size;
    fallback_calls++;
    return 0;
}

#include "broker-functions.inc"

#define CHECK(expression) do { if (!(expression)) { \
    fprintf(stderr, "line %u: %s\n", __LINE__, #expression); exit(1); } } while (0)

static void reset(void)
{
    public_calls = semantic_calls = fallback_calls = received_count = 0;
    owner_calls = barrier_calls = validate_calls = validated_after_effect = 0;
    owner_units = effects_count = owner_failure = barrier_failure = 0;
    use_reply = lose_live_after_owner = FALSE;
    configured_phone_text_mode = PHONE_TEXT_MODE_AUTO;
    x11_input_configured = live = idle = TRUE;
    public_call_deadline = 2000;
    memset(owner_text, 0, sizeof(owner_text)); memset(effects, 0, sizeof(effects));
}
static INPUT key(WORD virtual_key, WORD scan, DWORD flags)
{
    INPUT input = {0};
    input.type = INPUT_KEYBOARD;
    input.ki.wVk = virtual_key; input.ki.wScan = scan; input.ki.dwFlags = flags;
    return input;
}
static DWORD dispatch(DWORD count, const INPUT *inputs, DWORD *error)
{
    DWORD characters, physical, wait, clamped;
    BOOL normalized, focus;
    const char *route;
    semantic_edit_state state = {0};
    DWORD result = send_relay_inputs(count, inputs, error, &normalized,
                                    &characters, &physical, &focus, &wait,
                                    &route, &state, &clamped);
    CHECK(fallback_calls == 0 && semantic_calls == 0 && validated_after_effect == 0);
    CHECK(focus && wait == 0 && characters == 0 && physical == 0);
    return result;
}
static void no_effects(void) { CHECK(effects_count == 0 && public_calls == 0 && owner_calls == 0); }
int main(void)
{
    DWORD error;
    INPUT text[] = {
        key(0, 'a', KEYEVENTF_UNICODE), key(0, 'a', KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
        key(0, 'A', KEYEVENTF_UNICODE), key(0, 'A', KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
        key(0, '-', KEYEVENTF_UNICODE), key(0, 0x4e2d, KEYEVENTF_UNICODE),
        key(0, '\n', KEYEVENTF_UNICODE), key(0, 0xd83d, KEYEVENTF_UNICODE),
        key(0, 0xd83d, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
        key(0, 0xde00, KEYEVENTF_UNICODE), key(0, 0xde00, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)
    };
    const WCHAR expected[] = {'a', 'A', '-', 0x4e2d, '\n', 0xd83d, 0xde00};
    reset();
    CHECK(dispatch(11, text, &error) == 11 && error == ERROR_SUCCESS);
    CHECK(owner_units == 7 && !memcmp(owner_text, expected, sizeof(expected)));
    CHECK(owner_calls == 1 && public_calls == 1 && barrier_calls == 1 && validate_calls == 1);
    CHECK(!strcmp(effects, "OPB") && received_count == 4);
    CHECK(received[0].ki.wVk == VK_SHIFT && received[0].ki.dwFlags == 0);
    CHECK(received[1].ki.wVk == VK_INSERT && received[1].ki.dwFlags == 0);
    CHECK(received[2].ki.wVk == VK_INSERT && received[2].ki.dwFlags == KEYEVENTF_KEYUP);
    CHECK(received[3].ki.wVk == VK_SHIFT && received[3].ki.dwFlags == KEYEVENTF_KEYUP);

    INPUT mixed[] = {key('Z', 0, 0), text[0], key('Z', 0, KEYEVENTF_KEYUP), text[3]};
    reset(); CHECK(dispatch(4, mixed, &error) == 4 && !strcmp(effects, "KOPBK"));
    CHECK(owner_units == 1 && validate_calls == 4);

    INPUT invalid[] = {text[0], key('Z', 0, UINT32_C(0x10000))};
    reset(); CHECK(dispatch(2, invalid, &error) == 0); no_effects();
    invalid[1] = key(VK_BACK, 0, UINT32_C(0x10000));
    reset(); CHECK(dispatch(2, invalid, &error) == 0); no_effects();

    invalid[1] = key('Z', 'a', KEYEVENTF_UNICODE);
    reset(); CHECK(dispatch(2, invalid, &error) == 0); no_effects();
    invalid[1] = key(0, 'a', KEYEVENTF_UNICODE | KEYEVENTF_SCANCODE);
    reset(); CHECK(dispatch(2, invalid, &error) == 0); no_effects();

    INPUT malformed[] = {text[0], text[7], text[8], text[9], text[10]};
    for (unsigned i = 1; i < 5; i++) {
        INPUT bad[5]; memcpy(bad, malformed, sizeof(bad)); bad[i].ki.wScan = 0xd83d;
        if (i == 1 || i == 2) bad[i].ki.dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_EXTENDEDKEY;
        reset(); CHECK(dispatch(5, bad, &error) == 0); no_effects();
    }
    reset(); CHECK(dispatch(1, &text[10], &error) == 0); no_effects();

    reset(); CHECK(dispatch(1, &text[1], &error) == 1 && error == ERROR_SUCCESS); no_effects();

    reset(); owner_failure = ERROR_NOT_READY;
    CHECK(dispatch(1, text, &error) == 0 && owner_calls == 1 && public_calls == 0);
    reset(); use_reply = TRUE; reply_count = 2; reply_error = ERROR_WRITE_FAULT;
    CHECK(dispatch(1, text, &error) == 0 && owner_calls == 1 && public_calls == 1 && barrier_calls == 0);
    reset(); barrier_failure = ERROR_TIMEOUT;
    CHECK(dispatch(1, text, &error) == 0 && !strcmp(effects, "OPB"));

    reset(); idle = FALSE; CHECK(dispatch(1, text, &error) == 0 && error == ERROR_BUSY); no_effects();
    reset(); live = FALSE; CHECK(dispatch(1, text, &error) == 0 && error == ERROR_TIMEOUT); no_effects();
    reset(); public_call_deadline = 700;
    CHECK(dispatch(1, text, &error) == 0 && error == ERROR_TIMEOUT); no_effects();
    reset(); lose_live_after_owner = TRUE;
    CHECK(dispatch(1, text, &error) == 0 && owner_calls == 1 && public_calls == 0);

    INPUT modified[] = {key(VK_CONTROL, 0, 0), text[0], key(VK_CONTROL, 0, KEYEVENTF_KEYUP)};
    reset(); CHECK(dispatch(3, modified, &error) == 0 && error == ERROR_BUSY); no_effects();

    reset(); x11_input_configured = FALSE;
    CHECK(dispatch(1, text, &error) == 0 && error == ERROR_NOT_READY); no_effects();

    reset(); configured_phone_text_mode = PHONE_TEXT_MODE_KEYS;
    CHECK(dispatch(1, text, &error) == 1 && public_calls == 1 && owner_calls == 0);
    reset(); INPUT physical = key('Z', 0, 0);
    CHECK(dispatch(1, &physical, &error) == 1 && public_calls == 1 && owner_calls == 0);

    reset(); INPUT null_text = key(0, 0, KEYEVENTF_UNICODE);
    CHECK(dispatch(1, &null_text, &error) == 0); no_effects();
    puts("PASS 12 literal-text/preflight groups; fake transport callbacks only");
    return 0;
}
