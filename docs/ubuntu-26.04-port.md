[English](ubuntu-26.04-port.md) · العربية · Deutsch · Español · Français · 日本語 · 한국어 · Русский · Tiếng Việt · 简体中文 · 繁體中文
Home
# Ubuntu 26.04 and GNOME 50
Plus extends [Lachlan Chen's MIT-licensed bridge](https://github.com/lachlanchen/uu-remote-ubuntu-bridge) to x86-64 Ubuntu 26.04 / GNOME 50 and retains Ubuntu 24.04 / GNOME 46. Wine isolation, audited UU manifests, the input broker, desktop relay and supervised user service remain the foundation.
Installing the relay requires the exact reviewed toolchain or an existing verified output/cache; ordinary APT packages do not automatically provide that toolchain. See [build prerequisites and cache reuse](source-build.md).
## Platform choices
| Component | Upstream reference | Plus |
| --- | --- | --- |
| Ubuntu installer | 24.04 | 24.04 and 26.04; other versions require `UURB_ALLOW_UNVALIDATED_UBUNTU=1` |
| Default Windows UU | 4.33.0.8907 | Approved 4.42.0.2770; older approved manifests remain selectable |
| libei | Isolated 1.2.1 backport | System library when the fix is present, otherwise the retained backport |
| Windows relay | Pinned nightly SDL client and matching WinPR | Patched FreeRDP/SDL built from pinned source; see [source build](source-build.md) |
| Source CI | Ubuntu 24.04 | Workflow targets 24.04 and 26.04; hosted results are checked per run |
The observed 26.04 installation uses GNOME Remote Desktop 50.2 and system libei 1.5.0. Older libraries can use backport commit `ee27dd5c92e4e9496a36ca2d4112049fe02d2269`. `UURB_LIBEI_MODE=system|backport` records the choice, and `verify.sh` checks the library loaded by the RDP process. Wine comes from WineHQ stable.
## Desktop behavior
Four canvas presets, full-desktop fitting and pointer mapping are available through the GUI and CLI. The first installation chooses 1080p; upgrades preserve saved settings. A saved-size change reconnects the relay with rollback protection; reapplying a saved preset can restore its live RDP canvas. The user confirmed normal reconnect and a native resolution menu ending at 4K.
Fresh RDP installations use `rdp-public` input through FreeRDP's public APIs. Upgrades retain the saved route. Committed Unicode text and physical keys use distinct operations. Management windows and owned popups have a separate capture path; closing their viewer restores relay focus while the manager stays mapped. Desktop clipboard exchange remains active, while manager clipboard exchange is disabled.
| Feature | Current result |
| --- | --- |
| Direct phone and Mac Chinese input | User confirmed normal input during Plus integration |
| Ordinary computer text copy/paste | User confirmed normal behavior |
| Mac UU update and reconnect | Input, paste and reconnect remained normal; 4K felt similar |
| Manager overlap and focus | User reported resolved; selected window/popup checks passed |
| Android desktop/overview actions | Backend toggles work in both directions; Android buttons pending |
| Optional cursor guard | Theme fallback and selected cursor checks work; full controller shape set pending |
| Physical monitor off/on and virtual output | Pending on this host |
| Phone → ToDesk → Mac → UU text | Repeated lowercase `a` remains unresolved |
Desktop launchers provide scoped CJK fonts, DPI and credential handling for VNC, FreeRDP and Openbox tools. Dock settings belong to GNOME. Ubuntu's outgoing Wine-controller quality limits are separate from inbound Ubuntu streaming. A canvas request also does not force the physical source desktop to that resolution. See quality, [architecture](architecture.md) and comparison.
## Updates and upstream changes
Keep the Plus repository as `origin`, with the original bridge as another remote:
```bash
git remote add upstream https://github.com/lachlanchen/uu-remote-ubuntu-bridge.git
git fetch upstream
```
Review upstream changes in a development branch. After changing runtime scripts or native components, reinstall and check:
```bash
./install.sh --skip-packages --skip-account-login
./scripts/verify.sh --quick
```
Installation briefly interrupts the connection. The runtime digest reports changed source until installation is updated. Reusable upgrades describe runtime recovery; preset rollback covers canvas configuration.
An unknown UU installer hash stops automatic patching. A new release needs semantic review, an approved manifest and runtime checks before promotion; see upstream maintenance. The repository distributes MIT bridge source and manifests. UU and dependencies keep their own licenses.
