#!/usr/bin/env bash

set -Eeuo pipefail
# SDK headers and explicit build flags must not be replaced by ambient search paths.
unset CFLAGS CPPFLAGS LDFLAGS CPATH C_INCLUDE_PATH LIBRARY_PATH CXXFLAGS \
    OBJC_INCLUDE_PATH COMPILER_PATH GCC_EXEC_PREFIX ASFLAGS
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-1}"

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
output_dir="${1:-$repo_dir/build/compat}"
cc="${MINGW_CC:-x86_64-w64-mingw32-gcc}"
strip="${MINGW_STRIP:-x86_64-w64-mingw32-strip}"
winegcc="${WINEGCC:-/opt/wine-stable/bin/winegcc}"
host_cc="${HOST_CC:-gcc}"
host_strip="${HOST_STRIP:-strip}"
common=(-std=c11 -O2 -Wall -Wextra -Werror)
pe_link=(-Wl,--no-insert-timestamp)

for command in "$cc" "$strip" "$winegcc" "$host_cc" "$host_strip"; do
    if ! command -v "$command" >/dev/null 2>&1; then
        printf 'missing build tool: %s\n' "$command" >&2
        exit 1
    fi
done

python3 "$repo_dir/scripts/verify-freerdp-input-sdk.py"
input_sdk="$repo_dir/vendor/freerdp-input-sdk/include"
input_flags=(-D__STDC_NO_THREADS__=1 -D_WIN32_WINNT=0x0601 -Wpedantic -isystem "$input_sdk")
mkdir -p "$output_dir"

"$cc" "${common[@]}" "${input_flags[@]}" "${pe_link[@]}" -shared \
    -o "$output_dir/uu-input-bridge.dll" \
    "$repo_dir/src/uu_input_bridge_legacy.c" "$repo_dir/src/uurb_ready.c" -luser32
"$cc" "${common[@]}" "${input_flags[@]}" "${pe_link[@]}" -shared \
    -o "$output_dir/uu-input-bridge-public.dll" \
    "$repo_dir/src/uu_input_bridge.c" "$repo_dir/src/uurb_ready.c" -luser32
"$cc" "${common[@]}" "${pe_link[@]}" -shared \
    -o "$output_dir/uu-cursor-guard.dll" \
    "$repo_dir/src/uu_cursor_guard.c" -luser32 -lgdi32
"$cc" "${common[@]}" "${input_flags[@]}" "${pe_link[@]}" -municode -mwindows \
    -o "$output_dir/uu-input-broker.exe" \
    "$repo_dir/src/uu_input_broker.c" "$repo_dir/src/uurb_rdp_backend.c" \
    "$repo_dir/src/uurb_ready.c" "$repo_dir/src/full-input.c" -luser32 -lws2_32
"$cc" "${common[@]}" "${input_flags[@]}" "${pe_link[@]}" -shared \
    -o "$output_dir/uurb-full-input-client.dll" \
    "$repo_dir/src/plugin.c" "$repo_dir/src/freerdp-adapter.c" \
    "$repo_dir/src/full-input.c" "$repo_dir/src/uurb_ready.c" -lbcrypt
"$cc" "${common[@]}" "${input_flags[@]}" "${pe_link[@]}" \
    -o "$output_dir/uu-input-broker-probe.exe" \
    "$repo_dir/src/uu-input-broker-probe.c" "$repo_dir/src/uurb_ready.c"
"$cc" "${common[@]}" "${input_flags[@]}" -M \
    "$repo_dir/src/plugin.c" "$repo_dir/src/freerdp-adapter.c" \
    >"$output_dir/full-input-header-dependencies.txt"
if grep -Fq 'freerdp/build-config.h' "$output_dir/full-input-header-dependencies.txt" || \
   grep -Fq 'winpr/build-config.h' "$output_dir/full-input-header-dependencies.txt"; then
    printf 'Unexpected build-config dependency in public input SDK.\n' >&2
    exit 1
fi
"$cc" "${common[@]}" "${pe_link[@]}" -municode \
    -o "$output_dir/uu-injector.exe" \
    "$repo_dir/src/uu_injector.c"
"$cc" "${common[@]}" "${pe_link[@]}" -municode \
    -o "$output_dir/uu-service-control.exe" \
    "$repo_dir/src/uu_service_control.c" -ladvapi32
"$cc" "${common[@]}" "${pe_link[@]}" \
    -o "$output_dir/uu-display-mode.exe" \
    "$repo_dir/src/uu_display_mode.c" -luser32
"$cc" "${common[@]}" "${pe_link[@]}" -municode \
    -I "$repo_dir/src" \
    -o "$output_dir/uu-wine-clipboard-bridge.exe" \
    "$repo_dir/src/uu_wine_clipboard_bridge.c" -lws2_32
"$cc" "${common[@]}" "${pe_link[@]}" -I "$repo_dir/src" \
    -o "$output_dir/uu-terminal-proxy.exe" \
    "$repo_dir/src/uu_terminal_proxy.c" -lws2_32
"$cc" "${common[@]}" "${pe_link[@]}" -mwindows \
    -o "$output_dir/uu-healthd-stub.exe" \
    "$repo_dir/src/winlogon.c"
"$cc" "${common[@]}" "${pe_link[@]}" -shared \
    -o "$output_dir/winpr-sspi-shim.dll" \
    "$repo_dir/src/winpr_sspi_shim.c"
"$host_cc" "${common[@]}" -fPIC -shared \
    -o "$output_dir/uu-network-filter.so" \
    "$repo_dir/src/uu_network_filter.c" -ldl -pthread
"$host_cc" "${common[@]}" -Wpedantic -fPIC -shared \
    -o "$output_dir/uu-manager-capture.so" \
    "$repo_dir/src/uu_manager_capture.c" -ldl -pthread
"$host_cc" "${common[@]}" -I "$repo_dir/src" \
    -o "$output_dir/uu-x11-input" \
    "$repo_dir/src/uu_x11_input.c" -ldl
"$host_cc" "${common[@]}" -I "$repo_dir/src" \
    -o "$output_dir/uu-x11-clipboard" \
    "$repo_dir/src/uu_x11_clipboard.c" -ldl
"$host_cc" "${common[@]}" -I "$repo_dir/src" \
    -o "$output_dir/uu-terminal-bridge" \
    "$repo_dir/src/uu_terminal_bridge.c" -lutil

"$strip" \
    "$output_dir/uu-cursor-guard.dll" \
    "$output_dir/uu-display-mode.exe" \
    "$output_dir/uu-input-bridge.dll" \
    "$output_dir/uu-input-bridge-public.dll" \
    "$output_dir/uu-input-broker.exe" \
    "$output_dir/uurb-full-input-client.dll" \
    "$output_dir/uu-input-broker-probe.exe" \
    "$output_dir/uu-injector.exe" \
    "$output_dir/uu-service-control.exe" \
    "$output_dir/uu-wine-clipboard-bridge.exe" \
    "$output_dir/uu-terminal-proxy.exe" \
    "$output_dir/uu-healthd-stub.exe" \
    "$output_dir/winpr-sspi-shim.dll"
"$host_strip" \
    "$output_dir/uu-network-filter.so" \
    "$output_dir/uu-manager-capture.so" \
    "$output_dir/uu-x11-input" \
    "$output_dir/uu-x11-clipboard" \
    "$output_dir/uu-terminal-bridge"

rm -f "$output_dir/winlogon.exe" "$output_dir/winlogon.exe.so"
"$winegcc" -O2 -mwindows -o "$output_dir/winlogon.exe" \
    "$repo_dir/src/winlogon.c"
"$host_strip" "$output_dir/winlogon.exe.so"

printf 'compatibility tools built in %s\n' "$output_dir"
