"""Execute actual installer launcher blocks in temporary homes only."""
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
from uu_update_manager import Manager


def embedded(filename, marker):
    text = (REPO / filename).read_text()
    match = re.search(r"<<'" + marker + r"'\n(.*?)\n" + marker + r"(?:\n|$)", text, re.S)
    if not match:
        raise AssertionError(f"Missing embedded block: {marker}")
    return match.group(1)


class DesktopLauncherInstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.applications = self.home / '.local/share/applications'
        self.applications.mkdir(parents=True)
        (self.home / 'Desktop').mkdir()
        self.backups = self.home / '.local/share/uu-remote/tools/launcher-backups'

    def execute(self, filename, marker, *arguments, prefix=''):
        return subprocess.run([sys.executable, '-c', prefix + embedded(filename, marker),
                               *map(str, arguments)], capture_output=True, text=True)

    def preflight(self):
        return self.execute('install.sh', 'PY_LAUNCHER_PREFLIGHT', self.home)

    def main_entry(self, prefix=''):
        return self.execute('install.sh', 'PY_MAIN_LAUNCHER', REPO / 'desktop/uu-remote.desktop.in',
                            self.applications / 'uu-remote.desktop', self.home / '.local/bin/uu-remote',
                            self.home, prefix=prefix)

    def tool_entries(self, prefix=''):
        prefix = ("from pathlib import Path\n"
                  "real_is_file = Path.is_file\n"
                  "Path.is_file = lambda path: str(path) in ('/usr/bin/xtigervncviewer', '/usr/bin/x11vnc', "
                  "'/usr/bin/xfreerdp', '/usr/bin/obconf') or real_is_file(path)\n" + prefix)
        return self.execute('install.sh', 'PY_TOOL_LAUNCHERS', REPO / 'desktop', self.home, prefix=prefix)

    def remove(self):
        return self.execute('uninstall.sh', 'PY_REMOVE_LAUNCHERS', self.home)

    def assert_ok(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_preflight_all_launcher_symlinks_and_dangling_links_refused_before_stop(self):
        source = (REPO / 'install.sh').read_text()
        self.assertLess(source.index("<<'PY_LAUNCHER_PREFLIGHT'"), source.index('"${systemctl_user[@]}" stop uu-remote-bridge.service'))
        entries = [self.applications / (name + '.desktop') for name in
                   ('uu-remote', 'uu-quality', 'uu-vnc-viewer', 'xtigervncviewer', 'x11vnc', 'xfreerdp', 'obconf')]
        entries.append(self.home / 'Desktop/UU Remote.desktop')
        self.backups.mkdir(parents=True)
        entries += [self.backups / entry.name for entry in entries]
        target = self.home / 'untouched'
        target.write_text('original target')
        for entry in entries:
            for destination in (target, self.home / 'missing-target'):
                with self.subTest(entry=entry.name, destination=destination.name):
                    entry.symlink_to(destination)
                    result = self.preflight()
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('symbolic-link', result.stderr)
                    self.assertEqual(target.read_text(), 'original target')
                    self.assertTrue(entry.is_symlink())
                    entry.unlink()

    def test_main_and_tool_blocks_refuse_existing_links_without_writing_targets(self):
        target = self.home / 'target'
        target.write_text('untouched')
        main = self.applications / 'uu-remote.desktop'
        main.symlink_to(target)
        self.assertNotEqual(self.main_entry().returncode, 0)
        main.unlink()
        for name in ('uu-vnc-viewer', 'xtigervncviewer'):
            viewer = self.applications / (name + '.desktop')
            viewer.symlink_to(target)
            self.assertNotEqual(self.tool_entries().returncode, 0)
            viewer.unlink()
        self.assertEqual(target.read_text(), 'untouched')

    def test_installed_tool_visibility_matches_gio_and_keeps_font_wrapper(self):
        from gi.repository import Gio

        self.assert_ok(self.tool_entries())
        entry = self.applications / 'uu-vnc-viewer.desktop'
        viewer = Gio.DesktopAppInfo.new_from_filename(str(entry))
        self.assertEqual(viewer.get_name(), 'TigerVNC 查看器')
        self.assertTrue(viewer.should_show())
        self.assertFalse(viewer.get_is_hidden())
        self.assertFalse(viewer.get_nodisplay())
        self.assertIn(f'Exec=/usr/bin/python3 "{self.home}/.local/libexec/uu-desktop-tool.py" viewer\n', entry.read_text())
        self.assertIn('desktop/uu-vnc-viewer.desktop.in', (REPO / 'scripts/runtime-source-digest').read_text())
        for name in ('xtigervncviewer', 'x11vnc', 'xfreerdp', 'obconf'):
            with self.subTest(name=name):
                app = Gio.DesktopAppInfo.new_from_filename(str(self.applications / (name + '.desktop')))
                self.assertTrue(app.get_is_hidden())
                self.assertTrue(app.get_nodisplay())
                self.assertFalse(app.should_show())

    def test_optional_user_launchers_including_marker_text_remain_untouched(self):
        entries = [self.applications / (name + '.desktop') for name in ('x11vnc', 'xfreerdp', 'obconf')]
        for entry in entries:
            entry.write_text('[Desktop Entry]\nName=My custom X-UURB-Managed=true ' + entry.name +
                             '\n# X-UURB-Managed=true\n')
        original = {entry: entry.read_text() for entry in entries}
        for _ in range(2):
            self.assert_ok(self.tool_entries())
            for entry in entries:
                self.assertEqual(entry.read_text(), original[entry])
                self.assertFalse((self.backups / entry.name).exists())
        self.assert_ok(self.remove())
        for entry in entries:
            self.assertEqual(entry.read_text(), original[entry])

    def test_automatic_packages_keep_runtime_tools_and_omit_optional_native_rdp(self):
        source = (REPO / 'install.sh').read_text()
        packages = re.search(r'^install_packages\(\) \{\n(.*?)^\}', source, re.M | re.S).group(1)
        self.assertNotIn('freerdp3-x11', packages)
        for package in ('openbox', 'tigervnc-viewer', 'x11vnc'):
            self.assertIn(package, packages)

    def test_quality_entry_is_searchable_and_runs_existing_quality_gui(self):
        self.assert_ok(self.main_entry())
        entry = self.applications / 'uu-quality.desktop'
        rendered = entry.read_text()
        self.assertIn('Name=UU Remote 画质与分辨率\n', rendered)
        self.assertIn(f'Exec={self.home}/.local/bin/uu-remote quality gui\n', rendered)
        self.assertIn('画质;分辨率;', rendered)
        self.assertNotIn('@EXEC', rendered)
        self.assertNotIn('NoDisplay=true', rendered)
        self.assertIn('X-UURB-Managed=true', rendered)
        self.assertEqual(entry.stat().st_mode & 0o777, 0o644)
        self.assertIn('desktop/uu-quality.desktop.in', (REPO / 'scripts/runtime-source-digest').read_text())

    def test_runtime_snapshot_covers_quality_and_both_viewer_ids_with_backups(self):
        entries = [self.applications / (name + '.desktop') for name in
                   ('uu-quality', 'uu-vnc-viewer', 'xtigervncviewer')]
        original = {entry: 'original ' + entry.name for entry in entries}
        for entry in entries:
            entry.write_text(original[entry])
        self.assert_ok(self.main_entry())
        self.assert_ok(self.tool_entries())
        manager = Manager.__new__(Manager)
        with patch.object(Path, 'home', return_value=self.home), \
                patch.object(Manager, 'wine_prefix', return_value=self.home / 'wine'), \
                patch('uu_update_manager.command_output', return_value=subprocess.CompletedProcess([], 0)), \
                patch.object(Manager, 'health', return_value={'healthy': True}):
            self.assertIn(self.backups.parent, manager.runtime_snapshot_paths())
            snapshot = manager.snapshot_live_runtime(self.home / 'work')
            manager.validated_snapshot_entries(snapshot)
            for entry in entries:
                self.assertIn(entry, manager.runtime_snapshot_paths())
                saved = snapshot / 'files' / entry.relative_to(self.home)
                self.assertEqual(saved.read_text(), entry.read_text())
                backup = snapshot / 'files' / (self.backups / entry.name).relative_to(self.home)
                self.assertEqual(backup.read_text(), original[entry])
                entry.write_text('new deployment')
                (self.backups / entry.name).write_text('changed backup')
            self.assertTrue(manager.restore_live_runtime(snapshot)['health']['healthy'])
            for entry in entries:
                self.assertEqual(entry.read_text(), (snapshot / 'files' / entry.relative_to(self.home)).read_text())
                self.assertEqual((self.backups / entry.name).read_text(), original[entry])

    def test_runtime_restore_removes_viewer_ids_absent_before_install(self):
        manager = Manager.__new__(Manager)
        with patch.object(Path, 'home', return_value=self.home), \
                patch.object(Manager, 'wine_prefix', return_value=self.home / 'wine'), \
                patch('uu_update_manager.command_output', return_value=subprocess.CompletedProcess([], 0)), \
                patch.object(Manager, 'health', return_value={'healthy': True}):
            snapshot = manager.snapshot_live_runtime(self.home / 'work')
            self.assert_ok(self.tool_entries())
            for name in ('uu-vnc-viewer', 'xtigervncviewer'):
                self.assertTrue((self.applications / (name + '.desktop')).exists())
            self.assertTrue(manager.restore_live_runtime(snapshot)['health']['healthy'])
            for name in ('uu-vnc-viewer', 'xtigervncviewer'):
                self.assertFalse((self.applications / (name + '.desktop')).exists())

    def test_atomic_publication_never_follows_symlink_created_after_initial_check(self):
        target = self.home / 'target'
        target.write_text('untouched')
        # Introduce a link at the publication boundary, after the block's preflight.
        prefix = '''import os
from pathlib import Path
real_replace = os.replace
def late_link(source, destination):
    destination = Path(destination)
    destination.unlink(missing_ok=True)
    destination.symlink_to(Path(__import__('sys').argv[-1]) / 'target')
    return real_replace(source, destination)
os.replace = late_link
'''
        for run, entry in ((self.main_entry, self.applications / 'uu-remote.desktop'),
                           (self.tool_entries, self.applications / 'uu-vnc-viewer.desktop'),
                           (self.tool_entries, self.applications / 'xtigervncviewer.desktop')):
            with self.subTest(entry=entry.name):
                self.assert_ok(run(prefix=prefix))
                self.assertFalse(entry.is_symlink())
                self.assertEqual(entry.stat().st_mode & 0o777, 0o644)
                self.assertEqual(target.read_text(), 'untouched')
        self.assertEqual(list(self.applications.glob('.uu-launcher-*')), [])

    def test_desktop_shortcut_executable_while_application_entry_is_not(self):
        self.assert_ok(self.main_entry())
        shortcut = self.home / 'Desktop/UU Remote.desktop'
        application = self.applications / 'uu-remote.desktop'
        self.assertTrue(shortcut.stat().st_mode & 0o111)
        self.assertEqual(shortcut.stat().st_mode & 0o777, 0o755)
        self.assertEqual(application.stat().st_mode & 0o777, 0o644)

    def test_user_launchers_backed_up_once_and_owned_uninstall_restores_originals(self):
        entries = [self.applications / 'uu-remote.desktop', self.applications / 'uu-quality.desktop',
                   self.applications / 'uu-vnc-viewer.desktop',
                   self.applications / 'xtigervncviewer.desktop',
                   self.home / 'Desktop/UU Remote.desktop']
        for entry in entries:
            entry.write_text('[Desktop Entry]\nName=My original X-UURB-Managed=true ' + entry.name + '\n')
        entries[-1].chmod(0o755)
        original = {entry: entry.read_text() for entry in entries}
        for _ in range(2):
            self.assert_ok(self.preflight())
            self.assert_ok(self.main_entry())
            self.assert_ok(self.tool_entries())
            for entry in entries:
                self.assertIn('X-UURB-Managed=true', entry.read_text())
                self.assertEqual((self.backups / entry.name).read_text(), original[entry])
        self.assert_ok(self.remove())
        for entry in entries:
            self.assertEqual(entry.read_text(), original[entry])
        self.assertEqual(entries[-1].stat().st_mode & 0o777, 0o755)
        self.assertFalse(self.backups.exists())

    def test_user_replaced_launcher_is_preserved_with_backup_on_uninstall(self):
        for name, install in (('uu-vnc-viewer', self.tool_entries), ('xtigervncviewer', self.tool_entries),
                              ('uu-quality', self.main_entry)):
            with self.subTest(name=name):
                entry = self.applications / (name + '.desktop')
                entry.write_text('original user entry')
                self.assert_ok(install())
                entry.write_text('later user replacement')
                self.assert_ok(self.remove())
                self.assertEqual(entry.read_text(), 'later user replacement')
                self.assertEqual((self.backups / entry.name).read_text(), 'original user entry')

    def test_uninstall_skips_launcher_and_backup_links_without_touching_targets(self):
        self.assert_ok(self.main_entry())
        self.assert_ok(self.tool_entries())
        target = self.home / 'target'
        target.write_text('X-UURB-Managed=true\nexternal target')
        entries = [self.applications / (name + '.desktop') for name in ('uu-vnc-viewer', 'xtigervncviewer')]
        for entry in entries:
            entry.unlink()
            entry.symlink_to(target)
        self.backups.mkdir(parents=True, exist_ok=True)
        backup = self.backups / 'uu-remote.desktop'
        backup.symlink_to(target)
        self.assert_ok(self.remove())
        for entry in entries:
            self.assertTrue(entry.is_symlink())
        self.assertTrue(backup.is_symlink())
        self.assertEqual(target.read_text(), 'X-UURB-Managed=true\nexternal target')
        self.assertTrue((self.applications / 'uu-remote.desktop').exists())

    def test_uninstall_symlinked_backup_directory_is_not_traversed_or_removed(self):
        self.assert_ok(self.tool_entries())
        target = self.home / 'external backups'
        target.mkdir()
        (target / 'xtigervncviewer.desktop').write_text('external backup')
        self.backups.parent.mkdir(parents=True, exist_ok=True)
        self.backups.symlink_to(target)
        self.assert_ok(self.remove())
        self.assertTrue(self.backups.is_symlink())
        self.assertEqual((target / 'xtigervncviewer.desktop').read_text(), 'external backup')


if __name__ == '__main__':
    unittest.main()
