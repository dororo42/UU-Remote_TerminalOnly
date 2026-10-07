#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

execute=false
display_name="UU-Headless"
resolution="1920x1080"
enable_remote_login=false
public_key_file=""
launch_agent_label="local.uu-headless-betterdisplay"

usage() {
    cat <<'EOF'
usage: bootstrap-headless-macos.sh [options]

Prepare a Mac for monitor-free UU Remote access with one BetterDisplay virtual
screen. The default mode is read-only; pass --execute to make changes.

Options:
  --execute                 install and configure the virtual screen
  --name NAME               virtual-screen name (default: UU-Headless)
  --resolution WIDTHxHEIGHT virtual-screen resolution (default: 1920x1080)
  --enable-remote-login     enable macOS Remote Login through systemsetup
  --public-key-file PATH    install one public key for the current user
  -h, --help                show this help

The script never accepts or stores a password. Remote Login is optional and
may prompt for the current administrator password through sudo.
EOF
}

fail() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

while (($#)); do
    case "$1" in
        --execute)
            execute=true
            shift
            ;;
        --name)
            (($# >= 2)) || fail "--name requires a value"
            display_name="$2"
            shift 2
            ;;
        --resolution)
            (($# >= 2)) || fail "--resolution requires a value"
            resolution="$2"
            shift 2
            ;;
        --enable-remote-login)
            enable_remote_login=true
            shift
            ;;
        --public-key-file)
            (($# >= 2)) || fail "--public-key-file requires a path"
            public_key_file="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            fail "unknown option: $1"
            ;;
    esac
done

[[ "$(uname -s)" == Darwin ]] || fail "this script must run on macOS"
[[ "$display_name" =~ ^[A-Za-z0-9._-]+$ ]] \
    || fail "screen name may contain only letters, numbers, dot, underscore, and dash"
[[ "$resolution" =~ ^[1-9][0-9]{2,4}x[1-9][0-9]{2,4}$ ]] \
    || fail "resolution must use WIDTHxHEIGHT"

product_version="$(sw_vers -productVersion)"
major="${product_version%%.*}"
remainder="${product_version#*.}"
minor="${remainder%%.*}"
if ((major < 13 || (major == 13 && minor < 2))); then
    fail "current BetterDisplay requires macOS 13.2 or newer; found $product_version"
fi

app="/Applications/BetterDisplay.app"
app_cli="$app/Contents/MacOS/BetterDisplay"
launch_agent="$HOME/Library/LaunchAgents/$launch_agent_label.plist"

printf 'macOS=%s\n' "$product_version"
printf 'display-name=%s\n' "$display_name"
printf 'resolution=%s\n' "$resolution"
printf 'betterdisplay=%s\n' \
    "$([[ -x "$app_cli" ]] && printf installed || printf missing)"
printf 'launch-agent=%s\n' \
    "$([[ -f "$launch_agent" ]] && printf installed || printf missing)"

if [[ -n "$public_key_file" ]]; then
    [[ -f "$public_key_file" ]] || fail "public key file does not exist"
    key_lines="$(wc -l <"$public_key_file" | tr -d ' ')"
    [[ "$key_lines" == 1 ]] || fail "public key file must contain exactly one line"
    read -r key_type _ <"$public_key_file"
    case "$key_type" in
        ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256|ecdsa-sha2-nistp384|ecdsa-sha2-nistp521)
            ;;
        *)
            fail "public key file has an unsupported key type"
            ;;
    esac
fi

if [[ "$execute" != true ]]; then
    printf 'dry-run: pass --execute while a temporary display is attached\n'
    exit 0
fi

if [[ ! -x "$app_cli" ]]; then
    command -v brew >/dev/null 2>&1 \
        || fail "Homebrew is required to install BetterDisplay"
    brew install --cask betterdisplay
fi
[[ -x "$app_cli" ]] || fail "BetterDisplay installation did not create $app_cli"

open -gja BetterDisplay
for _ in {1..40}; do
    if pgrep -x BetterDisplay >/dev/null 2>&1; then
        break
    fi
    sleep 0.5
done
pgrep -x BetterDisplay >/dev/null 2>&1 \
    || fail "BetterDisplay did not start in the current GUI session"

help_output="$("$app_cli" help 2>&1)"
grep -q 'VirtualScreen' <<<"$help_output" \
    || fail "installed BetterDisplay CLI does not advertise VirtualScreen"

virtual_screen_exists() {
    local identifiers

    identifiers="$("$app_cli" get -type=VirtualScreen \
        "-name=$display_name" -identifiers 2>/dev/null)" || return 1
    # Match only complete name fields, not UUIDs or similarly named screens.
    printf '%s\n' "$identifiers" | tr '\n' ' ' | \
        grep -Eo '"name"[[:space:]]*:[[:space:]]*"[^"]*"' | \
        sed -E 's/^[^:]*:[[:space:]]*"//; s/"$//' | \
        grep -Fxq -- "$display_name"
}

if ! virtual_screen_exists; then
    "$app_cli" create \
        -type=VirtualScreen \
        "-virtualScreenName=$display_name" \
        -useResolutionList=on \
        "-resolutionList=$resolution" \
        -virtualScreenHiDPI=on \
        -connected=on
    sleep 2
fi

virtual_screen_exists \
    || fail "BetterDisplay did not report the named virtual screen"

mkdir -p "$HOME/Library/LaunchAgents"
temporary_plist="$(mktemp "${TMPDIR:-/tmp}/$launch_agent_label.XXXXXX")"
trap 'rm -f -- "${temporary_plist:-}"' EXIT
cat >"$temporary_plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$launch_agent_label</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/open</string>
        <string>-gja</string>
        <string>BetterDisplay</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
EOF
plutil -lint "$temporary_plist"
install -m 0600 "$temporary_plist" "$launch_agent"
launchctl bootout "gui/$UID/$launch_agent_label" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$UID" "$launch_agent"

if [[ -n "$public_key_file" ]]; then
    mkdir -p "$HOME/.ssh"
    chmod 0700 "$HOME/.ssh"
    touch "$HOME/.ssh/authorized_keys"
    chmod 0600 "$HOME/.ssh/authorized_keys"
    public_key="$(cat "$public_key_file")"
    grep -Fqx "$public_key" "$HOME/.ssh/authorized_keys" \
        || printf '%s\n' "$public_key" >>"$HOME/.ssh/authorized_keys"
fi

if [[ "$enable_remote_login" == true ]]; then
    sudo /usr/sbin/systemsetup -setremotelogin on
fi

printf 'configured=%s\n' "$display_name"
printf 'Before removing the monitor, reboot and verify UU video/input and keyed SSH separately.\n'
