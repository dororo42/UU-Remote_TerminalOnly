[English](automated-repair-agent-handoff.md) · 中文（简体）
← English home
# Automated maintenance contract
This document supplies the reusable rules copied into an automated repair
checkout. It contains no host history, account state or completed incident record.
The updater copies it to `build/automated-repair/OPERATIONAL-HANDOFF.md` so a
repair run uses the rules from the same source revision as its task.
## Scope
Work only in the assigned repair checkout. Preserve the selected input track,
saved keyboard timing and working desktop route. Inspect the generated private
context before changing compatibility source or a candidate release manifest.
Permitted work includes source inspection, non-executing binary inspection,
focused tests, documentation changes and draft compatibility manifests.
Report `ready_for_review`, `no_change` or `blocked` using the updater's output
schema. A completed repair run does not authorize deployment.
## Changes requiring separate approval
Do not edit the live Wine prefix, account state, keyring, user configuration,
systemd units or source checkout. Do not run `sudo`, push or publish from a
repair task. Execute an unknown installer only inside the explicit staging
sandbox described in upstream maintenance.
An automated task may not approve its own binary interpretation or mark a new
manifest `approved`. Binary signatures, instruction semantics and complete file
identity require independent review before a maintainer accepts a release.
Do not bypass a failed identity check to make a candidate installable.
## Input and recovery rules
Keyboard events, phone text, mouse input and clipboard paste are different
paths. Check the path affected by a reported problem instead of changing all
input routes or adding global delays. No replay after ambiguity: if delivery
cannot be confirmed, report failure rather than repeat user input.
Keep rollback available before a live update. A healthy user service is not a
substitute for visible desktop, reconnect and controller input checks. A new
release needs a controller check that covers keyboard, phone input, clipboard
and the selected desktop route while retaining login state.
## Further reading
- Upstream maintenance
- [Security](security.md)
- Input behavior tracks
- Automatic updates
- Troubleshooting approach
- Keyboard compatibility checks
- XRDP recovery
Keep installers, proprietary binaries, raw logs, screenshots, credentials,
identifiers and generated task context in private state directories, outside
Git. Publish only portable technical conclusions and sanitized examples.
The original bridge source and maintenance tools are from
[Lachlan Chen's MIT-licensed project](https://github.com/lachlanchen/uu-remote-ubuntu-bridge).
