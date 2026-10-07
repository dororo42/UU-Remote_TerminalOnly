[English](ssh-and-port-mapping.md) · 简体中文
Home
# SSH and optional UU port mapping
Native OpenSSH is independent of the desktop relay. This guide uses example
peer names and localhost ports; configure your own authorized endpoints.
It does not configure a router, VPN, cloud relay or UU mapping automatically.
## Prepare an SSH endpoint
The destination needs an SSH server and an authorized account. Verify its host
key through a trusted channel and configure key authentication. Keep SSH keys,
hostnames, account names and completed connection records out of Git.
If using UU port mapping, create the required rule in the official controller
and confirm the local forwarding port. Availability and controller ownership
are vendor-managed. Do not accept a takeover or replace another active
controller's mapping without the operator's agreement.
## Add a local alias
The included `uu-ssh` helper creates an SSH alias for an existing mapping; it
does not create that mapping. This example uses `peer`, `remoteuser` and port
`2222` as placeholders:
```bash
uu-ssh add peer --port 2222 --user remoteuser --shell-transport ssh
uu-ssh check peer
uu-ssh peer
```
Use `uu-ssh --help` for the current command surface. `uu-ssh key` prints the
public key to authorize on the peer; never share its private key.
Profile files live under `~/.config/uu-ssh/peers/` and managed SSH fragments
under `~/.ssh/uu-bridge/`. Keep both private.
For a normal SSH endpoint without UU mapping, use your own `~/.ssh/config`
alias and `ssh peer` directly. Do not disable host-key verification to work
around a changing address.
## Terminal and message helpers
`uu-ssh shell peer` uses the explicitly selected shell transport. The vendor
terminal route and mapped SSH are distinct choices; terminal support depends
on the installed UU version and target platform. See
[native Ubuntu terminal](native-ubuntu-terminal.md).
For an authorized UTF-8 message channel over an existing SSH alias, see
private messages over SSH. Messages are stored as data and
are not executed as commands.
## Forward a local service
A standard localhost-only forward can expose an authorized service through SSH:
```bash
ssh -N -o ExitOnForwardFailure=yes \
  -L 15922:127.0.0.1:5922 desktop-host
```
The addresses are loopback endpoints, not a LAN listener. This example is used
by macOS current-desktop access; start that service
and configure `desktop-host` before forwarding it. Close the SSH connection
to remove the forward. Never publish passwords or private mapping identifiers.
See the upstream [OpenSSH manual](https://man.openbsd.org/ssh) and
[SSH configuration reference](https://man.openbsd.org/ssh_config) for other
forwarding options. Obtain UU only from [NetEase](https://uuyc.163.com/).
