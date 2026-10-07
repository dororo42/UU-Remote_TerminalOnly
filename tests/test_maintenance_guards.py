"""Isolated promotion, autologin ownership, worktree, and Mac CLI guards."""
import os
import json
import contextlib
import hashlib
import importlib.util
import io
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import test_promotion as fixture
import test_patch_tooling as patch_fixture


REPOSITORY = Path(__file__).resolve().parents[1]


class ReferenceToolchainTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "prepare_build_toolchain", REPOSITORY / "scripts/prepare-build-toolchain.py")
        self.toolchain = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.toolchain)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.packages = self.directory / "packages"
        self.packages.mkdir()
        self.root = self.directory / "private root"
        self.archive = self.packages / "tool_1_amd64.deb"
        self.payload = b"fixture package bytes"
        self.tool = b"fixture tool bytes"
        self.url = "https://archive.ubuntu.com/ubuntu/pool/main/t/tool/tool_1_amd64.deb"
        package_manifest = self.directory / "packages.json"
        package_manifest.write_text(json.dumps({"distribution": "Ubuntu 26.04 (resolute)", "packages": [{
            "url": self.url, "filename": self.archive.name, "bytes": len(self.payload),
            "sha256": hashlib.sha256(self.payload).hexdigest()}]}))
        source_lock = self.directory / "source-lock.json"
        source_lock.write_text(json.dumps({"compiler_build_tools": {
            "/usr/bin/tool": hashlib.sha256(self.tool).hexdigest()}}))
        self.toolchain.PACKAGES = package_manifest
        self.toolchain.SOURCE_LOCK = source_lock

    def extract(self, arguments, check):
        self.assertEqual(arguments, ["dpkg-deb", "--extract", str(self.archive), str(self.root)])
        self.assertTrue(check)
        tool = self.root / "usr/lib/tool-real"
        tool.parent.mkdir(parents=True)
        tool.write_bytes(self.tool)
        link = self.root / "usr/bin/tool"
        link.parent.mkdir()
        link.symlink_to("../lib/tool-real")

    def test_exact_cached_and_single_download_prepare_only_private_root(self):
        for cached in (True, False):
            with self.subTest(cached=cached):
                if cached:
                    self.archive.write_bytes(self.payload)
                response = io.BytesIO(self.payload)
                response.geturl = lambda: self.url
                with mock.patch.object(self.toolchain.urllib.request, "urlopen", return_value=response) as download, \
                        mock.patch.object(self.toolchain.subprocess, "run", side_effect=self.extract) as process, \
                        contextlib.redirect_stdout(io.StringIO()) as output:
                    self.toolchain.prepare(self.packages, self.root, verify_only=cached)
                if cached:
                    download.assert_not_called()
                else:
                    download.assert_called_once_with(self.url, timeout=60)
                process.assert_called_once()
                self.assertEqual((self.root / "usr/bin/tool").read_bytes(), self.tool)
                self.assertTrue((self.root / "usr/bin/tool").is_symlink())
                self.assertIn("sudo apt install", output.getvalue())
                self.assertIn("'" + str(self.archive) + "'" if " " in str(self.archive) else str(self.archive), output.getvalue())
                shutil.rmtree(self.root)
                self.archive.unlink()

    def test_bad_package_or_tool_hash_is_refused_without_host_operations(self):
        for bad_input in ("package", "tool"):
            with self.subTest(bad_input=bad_input):
                self.archive.write_bytes(b"invalid package bytes" if bad_input == "package" else self.payload)
                with mock.patch.object(self.toolchain.urllib.request, "urlopen") as download, \
                        mock.patch.object(self.toolchain.subprocess, "run", side_effect=self.extract) as process:
                    if bad_input == "tool":
                        self.tool = b"incorrect tool bytes"
                    with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
                        self.toolchain.prepare(self.packages, self.root, verify_only=True)
                download.assert_not_called()
                self.assertEqual(process.call_count, 0 if bad_input == "package" else 1)

    def test_verify_only_missing_cache_never_downloads_or_extracts(self):
        with mock.patch.object(self.toolchain.urllib.request, "urlopen") as download, \
                mock.patch.object(self.toolchain.subprocess, "run") as process:
            with self.assertRaisesRegex(ValueError, "Missing cached package"):
                self.toolchain.prepare(self.packages, self.root, verify_only=True)
        download.assert_not_called()
        process.assert_not_called()
        self.assertFalse(self.root.exists())


class PrefixConsistencyTests(unittest.TestCase):
    def declarations(self, script):
        source = (REPOSITORY / script).read_text()
        if script == "uninstall.sh":
            return source[source.index('bridge_user='):source.index('purge=false')]
        return source[source.index('manager="$repo_dir/'):source.index('systemctl_user=(')]

    def environment(self, home):
        return dict(os.environ, HOME=str(home), UURB_WINEPREFIX="", WINEPREFIX="",
                    XDG_STATE_HOME=str(home / ".local/state"))

    def test_prefix_sources_and_priority_match_installer(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            settings = home / ".config/uu-remote-bridge/environment"
            settings.parent.mkdir(parents=True)
            saved = home / "saved prefix with spaces"
            wine = home / "Wine override"
            explicit = home / "UU override"
            cases = ((None, {}, home / ".local/share/wineprefixes/uu-remote"),
                     (saved, {}, saved), (saved, {"WINEPREFIX": str(wine)}, wine),
                     (saved, {"WINEPREFIX": str(wine), "UURB_WINEPREFIX": str(explicit)}, explicit))
            for script in ("uninstall.sh", "scripts/upgrade-uu-remote.sh"):
                for stored, overrides, expected in cases:
                    with self.subTest(script=script, expected=expected.name):
                        settings.unlink(missing_ok=True)
                        if stored is not None:
                            settings.write_text(f"UURB_WINEPREFIX=unused\nUURB_WINEPREFIX={stored}\n")
                        command = 'set -Eeuo pipefail\nrepo_dir="$FIXTURE_REPO"\n' + self.declarations(script)
                        command += '\nprintf "%s\\n" "$wine_prefix"\n'
                        environment = self.environment(home)
                        environment.update(overrides, FIXTURE_REPO=str(REPOSITORY))
                        result = subprocess.run(["bash", "-c", command], env=environment,
                                                capture_output=True, text=True)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stdout.strip(), str(expected))

    def test_uninstall_restores_saved_custom_server_not_default(self):
        synthetic = patch_fixture.PatchToolingTests()
        synthetic.setUp()
        source = (REPOSITORY / "uninstall.sh").read_text()
        preflight = source[source.index('server="$uu_bin/'):source.index('if [[ "$dry_run" == true ]]; then')]
        restore_start = source.index('if [[ -f "$server.uu-original" ]]; then', source.index('disable --now'))
        restore = source[restore_start:source.index('\nrm -f \\\n', restore_start)]
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            custom = home / "custom prefix"
            default = home / ".local/share/wineprefixes/uu-remote"
            for prefix in (custom, default):
                (prefix / "compat").mkdir(parents=True)
                (prefix / "compat/release-manifest.json").write_text(json.dumps(synthetic.raw))
                server = prefix / "drive_c/Program Files/Netease/GameViewer/bin/GameViewerServer.exe"
                server.parent.mkdir(parents=True)
                server.write_bytes(synthetic.patched)
                server.with_name(server.name + ".uu-original").write_bytes(synthetic.original)
            settings = home / ".config/uu-remote-bridge/environment"
            settings.parent.mkdir(parents=True)
            settings.write_text(f"UURB_WINEPREFIX={custom}\n")
            command = 'set -Eeuo pipefail\nrepo_dir="$FIXTURE_REPO"\npurge=false\n' + self.declarations("uninstall.sh")
            result = subprocess.run(["bash", "-c", command + preflight + restore],
                                    env=dict(self.environment(home), FIXTURE_REPO=str(REPOSITORY)),
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            relative = "drive_c/Program Files/Netease/GameViewer/bin/GameViewerServer.exe"
            self.assertEqual((custom / relative).read_bytes(), synthetic.original)
            self.assertEqual((default / relative).read_bytes(), synthetic.patched)

    def test_upgrade_child_environment_manifest_and_failure_rollback_share_prefix(self):
        source = (REPOSITORY / "scripts/upgrade-uu-remote.sh").read_text()
        helpers = source[source.index('manifest_field() {'):source.index('print_status() {')]
        backups = source[source.index('backup_paths=('):source.index('\npull_repository\n')]
        refresh_start = source.index('[[ -f "$installed_manifest" ]] || fail \'installed release manifest disappeared\'')
        refresh = source[refresh_start:source.index('runtime_refresh_started=false', refresh_start)]
        for fail_point, tools_existed in (("installer", True), ("installer", False),
                                           ("verifier", True), ("verifier", False)):
            with self.subTest(fail_point=fail_point, tools_existed=tools_existed), tempfile.TemporaryDirectory() as temp:
                home = Path(temp)
                promotion, _ = fixture.PromotionTests().make_fixture(home / "fixture")
                repository = promotion.repository
                for name in ("patch-gameviewer.py", "gameviewer_patchlib.py"):
                    shutil.copyfile(REPOSITORY / "scripts" / name, repository / "scripts" / name)
                custom = home / "custom prefix with spaces"
                default = home / ".local/share/wineprefixes/uu-remote"
                synthetic = patch_fixture.PatchToolingTests()
                synthetic.setUp()
                for prefix, version in ((custom, "custom-version"), (default, "default-version")):
                    (prefix / "compat").mkdir(parents=True)
                    (prefix / "compat/release-manifest.json").write_text(
                        json.dumps(dict(synthetic.raw, version=version)))
                    (repository / f"patches/uu-remote-{version}.json").write_text(
                        json.dumps(dict(synthetic.raw, version=version)))
                    (prefix / "compat/helper").write_bytes(b"old prefix runtime")
                tools = home / ".local/share/uu-remote/tools"
                tool_files = [tools / "uu-manual-plane.py", tools / "uu-full-ready.py"]
                existing = [home / ".local/libexec/uu-desktop-tool.py"]
                missing = [home / ".local/libexec/uu-quality.py",
                           home / ".local/share/applications/uu-quality.desktop"]
                (existing if tools_existed else missing).extend(tool_files)
                managed = existing + missing
                broken_link = home / ".local/libexec/uu-cursor-asset.py"
                for path in existing:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"old desktop tools")
                settings = home / ".config/uu-remote-bridge/environment"
                settings.parent.mkdir(parents=True)
                settings.write_text(f"UURB_WINEPREFIX={custom}\n")
                probe = '''#!/usr/bin/python3
import json, os, pathlib, sys
home = pathlib.Path.home()
kind = pathlib.Path(sys.argv[0]).name
log = home / "calls.jsonl"
previous = log.read_text().splitlines() if log.exists() else []
with log.open("a") as stream:
    stream.write(json.dumps({"kind": kind, "arguments": sys.argv[1:], "UURB_WINEPREFIX": os.environ.get("UURB_WINEPREFIX"), "WINEPREFIX": os.environ.get("WINEPREFIX")}) + "\\n")
if kind == "install.sh":
    prefix = os.environ.get("UURB_WINEPREFIX") or os.environ.get("WINEPREFIX") or (home / ".config/uu-remote-bridge/environment").read_text().strip().split("=", 1)[1]
    (pathlib.Path(prefix) / "compat/helper").write_bytes(b"broken prefix runtime")
    for name in json.loads(os.environ["FIXTURE_MANAGED_PATHS"]):
        path = pathlib.Path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"new desktop tools")
    pathlib.Path(os.environ["FIXTURE_BROKEN_LINK"]).symlink_to(home / "missing-target")
    sys.exit(23 if os.environ["FIXTURE_FAIL_POINT"] == "installer" else 0)
if kind == "verify.sh" and os.environ["FIXTURE_FAIL_POINT"] == "verifier" and any(json.loads(line)["kind"] == "verify.sh" for line in previous):
    sys.exit(24)
'''
                for path in (repository / "install.sh", repository / "scripts/verify.sh",
                             repository / "scripts/uu_update_manager.py"):
                    path.write_text(probe)
                    path.chmod(0o700)
                service = home / "service-stub"
                service.write_text("#!/bin/sh\nexit 0\n")
                service.chmod(0o700)
                config = settings.with_name("updater.json")
                config.write_text("{}")
                command = 'set -Eeuo pipefail\nrepo_dir="$FIXTURE_REPO"\n' + self.declarations("scripts/upgrade-uu-remote.sh")
                command += 'config_file="$HOME/.config/uu-remote-bridge/updater.json"\nsystemctl_user=("$HOME/service-stub")\nlog() { :; }\n'
                command += helpers + backups + '\nrun_live_check\nupdater_command status\n' + refresh
                result = subprocess.run(["bash", "-c", command], capture_output=True, text=True,
                                        env=dict(self.environment(home), FIXTURE_REPO=str(repository),
                                                 FIXTURE_MANAGED_PATHS=json.dumps(list(map(str, managed))),
                                                 FIXTURE_BROKEN_LINK=str(broken_link),
                                                 FIXTURE_FAIL_POINT=fail_point))
                self.assertEqual(result.returncode, 23 if fail_point == "installer" else 24, result.stderr)
                self.assertEqual((custom / "compat/helper").read_bytes(), b"old prefix runtime")
                self.assertEqual((default / "compat/helper").read_bytes(), b"old prefix runtime")
                for path in existing:
                    self.assertEqual(path.read_bytes(), b"old desktop tools")
                for path in missing + [broken_link]:
                    self.assertFalse(path.exists(), path)
                    self.assertFalse(path.is_symlink(), path)
                if not tools_existed:
                    self.assertFalse(tools.exists())
                failed = home / ".local/state/uu-remote-upgrader/latest/failed"
                for path in managed:
                    self.assertEqual((failed / str(path).lstrip("/")).read_bytes(), b"new desktop tools")
                failed_link = failed / str(broken_link).lstrip("/")
                self.assertTrue(failed_link.is_symlink())
                self.assertEqual(os.readlink(failed_link), str(home / "missing-target"))
                self.assertEqual((failed / str(custom).lstrip("/") / "compat/helper").read_bytes(),
                                 b"broken prefix runtime")
                calls = [json.loads(line) for line in (home / "calls.jsonl").read_text().splitlines()]
                for call in calls:
                    self.assertEqual(call["UURB_WINEPREFIX"], str(custom))
                    self.assertEqual(call["WINEPREFIX"], str(custom))
                    if call["kind"] == "install.sh":
                        arguments = call["arguments"]
                        self.assertEqual(arguments[arguments.index("--release-manifest") + 1],
                                         str(repository / "patches/uu-remote-custom-version.json"))
                record = json.loads((home / ".local/state/uu-remote-upgrader/latest/record.json").read_text())
                self.assertEqual(record["installed_version"], "custom-version")


class PromotionCheckoutTests(unittest.TestCase):
    def test_dirty_tracked_installer_is_rejected_before_promotion(self):
        for staged in (False, True):
            with self.subTest(staged=staged), tempfile.TemporaryDirectory() as temp:
                promotion, _ = fixture.PromotionTests().make_fixture(Path(temp))
                (promotion.repository / "install.sh").write_text("#!/bin/sh\nexit 99\n")
                if staged:
                    subprocess.run(["git", "add", "install.sh"], cwd=promotion.repository, check=True)
                acceptance = fixture.promotion_module.load_json(promotion.manifest_path)["acceptance"]
                with self.assertRaisesRegex(fixture.PromotionError, "tracked changes"):
                    promotion.verify_checkout(acceptance)

    def test_clean_pinned_checkout_passes(self):
        with tempfile.TemporaryDirectory() as temp:
            promotion, _ = fixture.PromotionTests().make_fixture(Path(temp))
            acceptance = fixture.promotion_module.load_json(promotion.manifest_path)["acceptance"]
            promotion.verify_checkout(acceptance)

    def test_untracked_source_cannot_override_the_pinned_checkout(self):
        with tempfile.TemporaryDirectory() as temp:
            promotion, _ = fixture.PromotionTests().make_fixture(Path(temp))
            (promotion.repository / "scripts/typing.py").write_text("raise RuntimeError('shadow module')\n")
            acceptance = fixture.promotion_module.load_json(promotion.manifest_path)["acceptance"]
            with self.assertRaisesRegex(fixture.PromotionError, "untracked files"):
                promotion.verify_checkout(acceptance)

    def test_ignored_build_artifacts_do_not_block_pinned_checkout(self):
        with tempfile.TemporaryDirectory() as temp:
            promotion, _ = fixture.PromotionTests().make_fixture(Path(temp))
            (promotion.repository / ".git/info/exclude").write_text("build/\n.omc/\n")
            build = promotion.repository / "build"
            build.mkdir()
            (build / "fixture.dll").touch()
            operational = promotion.repository / ".omc"
            operational.mkdir()
            (operational / "fixture.log").touch()
            acceptance = fixture.promotion_module.load_json(promotion.manifest_path)["acceptance"]
            promotion.verify_checkout(acceptance)

    def runtime_check(self, verify_status=0):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            promotion, prefix = fixture.PromotionTests().make_fixture(root)
            uu_bin = prefix / "drive_c/Program Files/Netease/GameViewer/bin"
            uu_bin.mkdir(parents=True)
            (uu_bin / "GameViewerHealthd.exe.uu-original").write_bytes(b"audited")
            calls = []

            def command(arguments, **kwargs):
                calls.append((arguments, kwargs))
                status = 1 if arguments[0] == "/usr/bin/pgrep" and "sdl-freerdp\\.exe" in arguments else 0
                if "--quick" in arguments:
                    status = verify_status
                return subprocess.CompletedProcess(arguments, status, stdout="123\n", stderr="fixture health failure")

            manifest = SimpleNamespace(server_filename="GameViewerServer.exe",
                                       healthd_filename="GameViewerHealthd.exe",
                                       healthd_original_sha256="audited-hash")
            with mock.patch.object(fixture.promotion_module, "load_manifest", return_value=manifest), \
                 mock.patch.object(fixture.promotion_module, "sha256_file", return_value="audited-hash"), \
                 mock.patch.object(fixture.promotion_module, "command", side_effect=command):
                fixture.Promotion.current_runtime_check(promotion)
            return calls, prefix

    def test_healthy_vnc_relay_does_not_require_freerdp(self):
        with mock.patch.dict(os.environ, UURB_DESKTOP_RELAY="vnc"):
            calls, prefix = self.runtime_check()
        checks = [(args, kwargs) for args, kwargs in calls if "--quick" in args]
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0][1]["env"]["WINEPREFIX"], str(prefix))
        self.assertEqual(checks[0][1]["env"]["UURB_DESKTOP_RELAY"], "vnc")

    def test_vnc_still_requires_complete_bridge_health_verification(self):
        with mock.patch.dict(os.environ, UURB_DESKTOP_RELAY="vnc"):
            with self.assertRaisesRegex(fixture.PromotionError, "complete pre-promotion verification"):
                self.runtime_check(verify_status=1)


class UnattendedOwnershipTests(unittest.TestCase):
    def remember(self, managed_user):
        source = (REPOSITORY / "scripts/configure-unattended.sh").read_text()
        helpers = source[source.index("state_get() {"):source.index("write_encrypted_credential() {")]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "state.ini").touch()
            script = '''set -Eeuo pipefail
bridge_user=current-user
state_file="$OWNERSHIP_TEST_ROOT/state.ini"
gdm_config="$OWNERSHIP_TEST_ROOT/gdm.conf"
sudo() {
    if [[ "$1" == test ]]; then "$@"; return; fi
    if [[ "$1" == crudini && "$2" == --get ]]; then
        [[ -n "$OWNERSHIP_TEST_MANAGED_USER" ]] || return 1
        printf '%s\\n' "$OWNERSHIP_TEST_MANAGED_USER"
        return
    fi
    printf '%s\\n' "$*" >>"$OWNERSHIP_TEST_ROOT/mutations"
}
''' + helpers + '''
remember_gdm_state
sudo crudini --set "$gdm_config" daemon AutomaticLogin "$bridge_user"
'''
            result = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                                    env=dict(os.environ, OWNERSHIP_TEST_ROOT=temp,
                                             OWNERSHIP_TEST_MANAGED_USER=managed_user))
            calls = root / "mutations"
            return result, calls.read_text() if calls.exists() else ""

    def test_existing_other_user_state_blocks_gdm_mutation(self):
        result, mutations = self.remember("other-user")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(mutations, "")
        self.assertIn("other-user", result.stderr)

    def test_missing_managed_user_blocks_gdm_mutation(self):
        result, mutations = self.remember("")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(mutations, "")

    def test_matching_managed_user_can_continue(self):
        result, mutations = self.remember("current-user")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("AutomaticLogin current-user", mutations)

    def test_enable_and_disable_check_owner_before_dependency_or_system_changes(self):
        source = (REPOSITORY / "scripts/configure-unattended.sh").read_text()
        helpers = source[source.index("state_get() {"):source.index("write_encrypted_credential() {")]
        for action, end in (("enable_unattended", "disable_unattended() {"),
                            ("disable_unattended", "show_status() {")):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                (root / "state.ini").touch()
                (root / "gdm.conf").touch()
                function = source[source.index(action + "() {"):source.index(end)]
                script = '''set -Eeuo pipefail
bridge_user=current-user
state_file="$OWNERSHIP_TEST_ROOT/state.ini"
gdm_config="$OWNERSHIP_TEST_ROOT/gdm.conf"
sudo() {
    if [[ "$1" == -v ]]; then return 0; fi
    if [[ "$1" == test ]]; then "$@"; return; fi
    if [[ "$1" == crudini && "$2" == --get ]]; then
        printf 'other-user\\n'; return
    fi
    printf '%s\\n' "$*" >>"$OWNERSHIP_TEST_ROOT/mutations"
}
ensure_dependencies() { printf 'dependencies\\n' >>"$OWNERSHIP_TEST_ROOT/mutations"; }
''' + helpers + function + "\n" + action + "\n"
                result = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                                        env=dict(os.environ, OWNERSHIP_TEST_ROOT=temp))
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertFalse((root / "mutations").exists())


class UpdaterWorktreeTests(unittest.TestCase):
    def test_real_git_worktree_is_accepted_and_invalid_metadata_is_rejected(self):
        source = (REPOSITORY / "scripts/configure-updater.sh").read_text()
        start = source.index('case "$command" in')
        start = source.index('        if ', start)
        gate = source[start:source.index('        codex_executable=', start)]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "repository"
            repository.mkdir()
            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                            "commit", "--allow-empty", "-qm", "fixture"], cwd=repository, check=True)
            worktree = root / "worktree"
            subprocess.run(["git", "worktree", "add", "--detach", "-q", str(worktree)], cwd=repository, check=True)
            invalid = root / "invalid"
            invalid.mkdir()
            (invalid / ".git").write_text("gitdir: /does/not/exist\n")
            for selected, expected in ((repository, 0), (worktree, 0), (invalid, 1)):
                with self.subTest(selected=selected.name):
                    script = 'set -Eeuo pipefail\nrepo_dir="$WORKTREE_TEST_REPO"\n' + gate
                    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                                            env=dict(os.environ, WORKTREE_TEST_REPO=str(selected)))
                    self.assertEqual(result.returncode, expected, result.stderr)

            upgrade = (REPOSITORY / "scripts/upgrade-uu-remote.sh").read_text()
            discovery = upgrade[upgrade.index("discover_repository() {"):upgrade.index('repo_dir="$(discover_repository)"')]
            for selected, expected in ((repository, 0), (worktree, 0), (invalid, 1)):
                scripts = selected / "scripts"
                scripts.mkdir()
                probe = scripts / "discover-fixture.sh"
                probe.write_text('''#!/usr/bin/env bash
set -Eeuo pipefail
repo_override="$FIXTURE_REPO_OVERRIDE"
config_file="$HOME/missing-updater.json"
fail() { printf '%s\\n' "$*" >&2; exit 1; }
''' + discovery + '\ndiscover_repository\n')
                for override in ("", str(selected)):
                    with self.subTest(selected=selected.name, explicit=bool(override)):
                        result = subprocess.run(["bash", str(probe)], capture_output=True, text=True,
                                                env=dict(os.environ, HOME=temp,
                                                         FIXTURE_REPO_OVERRIDE=override))
                        self.assertEqual(result.returncode, expected, result.stderr)
                        if expected == 0:
                            self.assertEqual(result.stdout.strip(), str(selected))


class MacVirtualScreenTests(unittest.TestCase):
    def screen(self, existing):
        source = (REPOSITORY / "scripts/bootstrap-headless-macos.sh").read_text()
        if "virtual_screen_exists() {" in source:
            start = source.index("virtual_screen_exists() {")
        else:
            start = source.index('identifiers="$(')
        logic = source[start:source.index('mkdir -p "$HOME/Library/LaunchAgents"', start)]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cli = root / "BetterDisplay"
            cli.write_text('''#!/usr/bin/env bash
printf '%s\\n' "$*" >>"$MAC_SCREEN_TEST_ROOT/calls"
if [[ "$1" == create ]]; then touch "$MAC_SCREEN_TEST_ROOT/created"; exit 0; fi
if [[ -e "$MAC_SCREEN_TEST_ROOT/created" ]]; then
    printf '{ "name" : "UU", "UUID": "fixture-created" }\\n'
else
    printf '{\\n  "name" : "%s",\\n  "UUID": "UU"\\n}\\n' "$MAC_SCREEN_TEST_EXISTING"
fi
''')
            cli.chmod(0o700)
            script = '''set -Eeuo pipefail
app_cli="$MAC_SCREEN_TEST_ROOT/BetterDisplay"
display_name=UU
resolution=1920x1080
sleep() { :; }
fail() { printf '%s\\n' "$*" >&2; exit 1; }
''' + logic
            result = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                                    env=dict(os.environ, MAC_SCREEN_TEST_ROOT=temp,
                                             MAC_SCREEN_TEST_EXISTING=existing))
            return result, (root / "calls").read_text()

    def test_prefix_name_and_uuid_do_not_hide_missing_exact_screen(self):
        result, calls = self.screen("UU-Headless")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("create -type=VirtualScreen -virtualScreenName=UU", calls)

    def test_exact_existing_screen_is_reused(self):
        result, calls = self.screen("UU")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("create", calls)


if __name__ == "__main__":
    unittest.main()
