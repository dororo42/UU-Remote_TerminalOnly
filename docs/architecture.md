[English](architecture.md) · العربية · Español · Français · 日本語 · 한국어 · Tiếng Việt · 中文（简体） · 中文（繁體） · Deutsch · Русский
← English home
# Architecture
Purple arrows carry the desktop image toward the controller; orange arrows
carry keyboard, mouse and text toward Ubuntu. The lower management branch
opens UU's own windows locally, with viewer input returning to those windows.
The RDP server, input plugin and text-helper paths are detailed below.
## Two desktops and two directions
UU Remote's Windows host runs in a dedicated Wine prefix. Its Windows kernel
input driver cannot control a native Ubuntu GNOME session. The bridge instead
presents a relay window on a private X11 display and translates UU's user-mode
input into the selected Ubuntu session.
The logged-in Ubuntu desktop and the private Wine canvas are separate. Pixels
travel from Ubuntu through the relay to the remote UU controller; keyboard,
mouse and committed text travel in the opposite direction. The local management
viewer is a separate branch for UU's own windows, rather than another hop in
the shared-desktop stream.
The current source combines the UU 4.42 manifest with a pinned FreeRDP/SDL
build. Existing installations retain their saved settings.
## Defaults and supported branches
A new installation uses these settings; updates preserve saved configuration.
| Setting | New-install default | Other supported choice |
| --- | --- | --- |
| Desktop relay | `rdp` | Explicit `vnc` for an X11/XRDP desktop |
| Input route | `rdp-public` when using RDP | `legacy`; required by VNC |
| Desktop target | `auto` | `physical`, `xrdp`, or an exact X display |
| Private canvas | 1920 × 1080 | Normal presets: 720p, 1080p, 1440p and 4K |
| Follow desktop resolution | `off` | Opt-in `on` |
| Legacy physical-key route | `rdp` (through the selected relay) | Direct `x11`, or `auto` on X11 |
| Phone text | `auto` | `keys` or `clipboard` |
The installer's route selection is in [`install.sh`](../install.sh#L358).
The launcher's fallback when invoked without an installed route is `legacy`,
not the installer's new RDP default. `rdp-public` requires the RDP relay; VNC
requires an accessible X11/XRDP desktop and cannot substitute for a Wayland
session. These constraints are enforced before startup.
See [`uu-remote-bridge`](../scripts/uu-remote-bridge#L6) for setting validation
and [`runtime-settings.sh`](../scripts/runtime-settings.sh) for the fresh-install
versus upgrade phone-key pacing rule. Relay selection does not change UU's
controller-side encoding quality, FPS selection or bitrate ceiling.
## Desktop pixels: Ubuntu to the controller
Static PNG ·
Editable SVG ·
SVG animation ·
简体中文动图 ·
简体中文 SVG
The default RDP path is:
```text
Selected logged-in GNOME desktop
    -> GNOME Remote Desktop on that session's D-Bus
    -> loopback RDP connection
    -> pinned Windows SDL FreeRDP client: Ubuntu-Desktop-Relay
    -> private X11 canvas (only the bound SDL window is composed for rdp-public)
    -> UU GameViewerServer capture, encoding and UU transport
    -> remote phone, macOS or Windows UU controller
```
The launcher discovers the selected GNOME Shell's display, session and D-Bus.
An explicit target waits for that desktop rather than substituting another
session. It starts GNOME Remote Desktop on the selected bus, including the
private bus used by XRDP. FreeRDP connects to the configured loopback endpoint
(default `127.0.0.1:3390`), pins its configured TLS certificate and reads the
GNOME credential from standard input. This credential is separate from the UU
account. See [`start_desktop_relay`](../scripts/uu-remote-bridge#L1010) and the
[SDL launch](../scripts/uu-remote-bridge#L1758).
Xvfb and Openbox provide the private canvas. Xvfb uses an Xauthority cookie and
`-nolisten tcp`. The launcher selects a free display starting at `:20`, or uses
a validated configured display. The standard RDP modes have a maximum 4K
private screen, with genuine 720p, 1080p, 1440p and 4K modes registered before
Wine starts. See [private display setup](../scripts/uu-remote-bridge#L1437).
For `rdp-public`, [`uu-manual-plane.py`](../scripts/uu-manual-plane.py#L155)
owns XComposite redirection and paints only the bound SDL window's pixmap onto
the private root. Management windows remain mapped but are excluded from this
root image.
The optional VNC path replaces GNOME RDP and SDL for an X11/XRDP target:
```text
Selected X11 desktop -> loopback x11vnc -> private fullscreen VNC viewer
    -> UU capture and transport -> remote UU controller
```
It can use an independently managed, validated loopback x11vnc service or start
one owned by the bridge. The private viewer fits the desktop image without
resizing the source monitor. The VNC branch uses `legacy` input. See
[VNC server setup](../scripts/uu-remote-bridge#L1040) and
[VNC viewer setup](../scripts/uu-remote-bridge#L1844).
## Ordinary input: controller to Ubuntu
Version-specific, reviewed UU patches select its existing user-mode
`SendInput` path instead of its Windows kernel HID driver. The current
[4.42 manifest](../patches/uu-remote-4.42.0.2770.json) has its own audited edits;
older manifests cover their respective releases.
For `rdp-public`, the desktop input path is:
```text
Remote controller keyboard, pointer, buttons and wheel
    -> UU GameViewerServer SendInput import hook
    -> local input-broker named pipe
    -> uu-input-broker.exe / uurb_rdp_backend
    -> local named pipe to uurb-full-input inside SDL FreeRDP
    -> FreeRDP's public keyboard/mouse input functions
    -> existing RDP connection -> GNOME Remote Desktop -> selected desktop
```
The plugin is loaded by `/dvc:uurb-full-input`, but the broker-to-plugin link
is a local named pipe. It does not require a new server-side input channel.
The backend binds the active session and geometry; the plugin dispatches on
FreeRDP's event-loop timer. This desktop route does not focus a Wine window or
fall back to foreground `SendInput` after a partial or ambiguous attempt.
See [`uu_input_bridge.c`](../src/uu_input_bridge.c#L688),
[`uu_input_broker.c`](../src/uu_input_broker.c#L1081),
[`plugin.c`](../src/plugin.c#L200) and
[`freerdp-adapter.c`](../src/freerdp-adapter.c#L48).
The `legacy` hook first attempts Wine `SendInput` for ordinary keyboard/mouse
arrays and sends an unaccepted remainder to the broker. Unicode arrays go to
the broker directly. The broker focuses the private relay, and input reaches
Ubuntu through that relay's RDP or VNC connection. On an X11 target, opt-in
`--keyboard-route x11` or `auto` can send translated keyboard/mouse arrays
directly through the authenticated native XTEST helper. An unavailable helper
before injection can retain the relay route; an ambiguous failure after
injection is not replayed. See the
[legacy hook](../src/uu_input_bridge_legacy.c#L595) and
[broker's direct-X11/relay selection](../src/uu_input_broker.c#L1101).
## Committed Unicode text and copy/paste
Physical keys and committed IME text are different inputs. UU's phone IME
submits `KEYEVENTF_UNICODE` batches. In adaptive mode, the `legacy` broker
converts representable text into ordinary key chords; CJK, newline, tab and
other text needing literal semantics use the native text helper instead.
For `rdp-public`, adaptive Unicode commits use literal clipboard text even
when the characters are ASCII, so Caps Lock or a keyboard layout does not
change the committed text. Explicit `keys` mode selects key translation.
See [text selection](../src/uu_input_broker.c#L454).
Semantic text has two coordinated operations:
1. The broker sends bounded text to `uu-x11-input` over an authenticated
   loopback connection. The helper converts it to UTF-8 and gives `xclip`
   ownership of `CLIPBOARD` and `PRIMARY` on the selected desktop's X11 or
   Xwayland display, verifying the new owners before paste.
2. With `rdp-public`, the broker sends a Shift+Insert chord through the
   plugin/RDP input route, then checks an owner/selection-request barrier.
   With the legacy RDP split helper, clipboard ownership stays on the source
   desktop while only the paste chord is injected on the private SDL display;
   SDL/RDP delivers that chord to GNOME. Direct X11 uses the target display
   for both ownership and the paste chord.
The barrier confirms a bounded selection transaction, not that a particular
application received or displayed the text. A target application must support
paste and have input focus. See
[`send_semantic_segment`](../src/uu_input_broker.c#L641),
[helper display selection](../scripts/uu-remote-bridge#L1182) and
[selection transactions](../src/uu_x11_input.c#L1000).
Ordinary copy/paste is separate from phone IME commits. The RDP relay keeps its
normal `cliprdr` clipboard channel enabled. The pinned FreeRDP/SDL source
patches update remote clipboard caches and sequence/owner handling; their
inputs and build provenance are documented in [source-build.md](source-build.md).
The VNC branch also has an optional, strictly one-way text companion:
```text
Controller's UU clipboard -> GameViewer CF_UNICODETEXT
    -> uu-wine-clipboard-bridge -> authenticated loopback helper
    -> selected desktop CLIPBOARD and PRIMARY
```
It baselines the clipboard sequence at startup and requires the clipboard owner
to be GameViewer before and after reading. It neither reads the host clipboard
back nor injects a paste key, and therefore is not a bidirectional sync service.
It is started only for the explicit VNC/X11 relay. See
[companion startup](../scripts/uu-remote-bridge#L1266),
[owner checks](../src/uu_wine_clipboard_bridge.c#L195) and
[native clipboard ownership](../src/uu_x11_clipboard.c#L329).
## Local management and controller windows
`uu-remote open` presents UU's GameViewer management window on the logged-in
Ubuntu desktop through a local TigerVNC viewer. All Wine windows stay on the
private display and in the dedicated prefix.
```text
GameViewer management window + associated, same-owner popups/modals
    -> XComposite pixmaps / uu-manager-capture.so
    -> window-scoped loopback x11vnc -> local TigerVNC viewer
Local viewer keyboard/mouse
    -> x11vnc -> capture helper's owned-window hit/focus handling
    -> corresponding GameViewer window or popup
```
This branch does not expose the private desktop root and is not a serial hop
between the relay and the remote controller. The helper composes associated
popup windows, revalidates their owner and geometry, and routes local input to
them. Override-redirect menus retain their focus/grab; pointer motion in an
isolated manager does not continually refocus it. See
[window binding and capture](../scripts/uu-remote-console#L1121),
[popup association](../src/uu_manager_capture.c#L464) and
[input routing](../src/uu_manager_capture.c#L1090).
With `rdp-public`, the manual composition owner keeps management off the root.
With `legacy`, capture redirects only the owned manager frames off the root
while the relay supervisor remains active. The management VNC viewer disables
clipboard exchange and remote resizing, keeping those selections separate from
the desktop clipboard. A session lock reuses an existing local viewer; closing
it reaps its sidecars and removes its session record. The UU window stays
mapped; cleanup releases the management focus marker and restores relay focus
where applicable. It does not minimize the manager during layered-window
replacement. See [viewer options](../scripts/uu-remote-console#L1193),
[sidecar cleanup](../scripts/uu-remote-console#L622) and
[focus release](../scripts/uu-remote-console#L299).
When Ubuntu is used as a UU controller for another machine, a separately bound
controller window uses the same window-capture/viewer mechanism. Its incoming
remote picture and outgoing controls belong to that UU controller session;
this is separate from sharing Ubuntu's own desktop. The full-root noVNC console
is an explicit diagnostic command, not the ordinary management launcher.
## Display changes, compatibility and lifecycle
Canvas controls change the private relay size rather than the physical
monitor's resolution. The quality selector keeps four normal presets, verifies
the saved and live canvas, and arms independent recovery before a change.
A failed size trial restores the prior configuration and restarts the bridge
when needed; if recovery cannot be confirmed, the command reports that failure.
It does not claim that rollback always succeeds. Fixed-canvas selection requires
resolution following to be off. See
[quality recovery](../scripts/uu-quality.py#L329),
[mode registration and inspection](../scripts/uu-display-modes.py#L166) and the
quality guide.
GNOME Remote Desktop performs the RDP integration into GNOME. On Wayland its
native input stack uses the system's compositor integration; the bridge's
public input adapter calls FreeRDP, not libei directly. Compatibility support
can put an isolated older libei keymap-FD backport on the supervised GNOME RDP
process's library path when configured and present. Ubuntu 26.04's system
library includes that upstream fix, so this backport is not a universal input
path and does not replace the system library. See
[backport selection](../scripts/uu-remote-bridge#L199) and
[GNOME process environment](../scripts/uu-remote-bridge#L1121).
The SDL client is built from pinned upstream source.
`winpr-sspi-shim.dll` forwards WinPR SSPI operations and normalizes
Wine/WinPR private handle representations for authentication. UU compatibility
also supplies an active-session token source and an event-log API failure shape
that Wine otherwise lacks. These helpers do not grant Unix privileges.
The systemd user service owns the bridge process group. The launcher waits for
critical children and requests a full relay restart if one exits; an inner
supervisor rebinds the input hook when UU restarts. Cleanup stops owned native
helpers and Wine processes within the dedicated prefix, reaps clipboard owners,
releases capture ownership and removes session markers. It restores any
previously active native GNOME desktop-sharing service. Other Wine prefixes
and the selected desktop's application session are not cleanup targets. See
[supervised children](../scripts/uu-remote-bridge#L2207),
[cleanup](../scripts/uu-remote-bridge#L478) and the
[user unit](../systemd/uu-remote-bridge.service).
For a Wayland session without an active monitor, the RDP branch can temporarily
own a GNOME virtual-monitor fallback and restore the sharing mode when a
physical monitor returns or the bridge stops. This is separate from changing
the relay canvas.
UU's optional terminal channel uses a Windows stdio proxy and an authenticated
loopback native `forkpty` helper to run the current user's login shell. It is
separate from desktop pixels and input and does not require an SSH listener.
See [native Ubuntu terminal](native-ubuntu-terminal.md). Unattended startup
and account/keyring handling are described in
[unattended-startup.md](unattended-startup.md).
