"""Run the real installer budget renderer against private unit directories."""
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = (ROOT / "install.sh").read_text()
RENDERER = INSTALLER.split("<<'PY_MEMORY_BUDGET'", 1)[1].split("\nPY_MEMORY_BUDGET", 1)[0]
MANAGED = "10-uu-remote-memory-budget.conf"
GIB_KIB = 1024 * 1024


class InstallerMemoryBudgetTests(unittest.TestCase):
    def render(self, directory, ram, mode="apply"):
        return subprocess.run(["/usr/bin/python3", "-", str(directory), str(ram), mode],
                              input=RENDERER, capture_output=True, text=True, timeout=5)

    def test_small_hosts_keep_baseline_without_creating_a_larger_budget(self):
        for ram in (4 * GIB_KIB, 8 * GIB_KIB, 16 * GIB_KIB - 1):
            with self.subTest(ram=ram), tempfile.TemporaryDirectory() as temp:
                result = self.render(Path(temp), ram)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse((Path(temp) / "uu-remote-bridge.service.d").exists())

    def test_large_hosts_get_finite_soft_and_hard_headroom(self):
        for ram in (16 * GIB_KIB, 247 * GIB_KIB):
            with self.subTest(ram=ram), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp) / "uu-remote-bridge.service.d"
                result = self.render(Path(temp), ram)
                self.assertEqual(result.returncode, 0, result.stderr)
                budget = directory / MANAGED
                self.assertIn("MemoryHigh=6G\nMemoryMax=8G\n", budget.read_text())
                self.assertEqual(budget.stat().st_mode & 0o777, 0o644)
                self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
                self.assertEqual(self.render(Path(temp), ram).returncode, 0)
                self.assertEqual(sorted(path.name for path in directory.iterdir()), [MANAGED])

    def test_move_to_small_host_removes_only_exact_installer_owned_policy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(self.render(root, 32 * GIB_KIB).returncode, 0)
            directory = root / "uu-remote-bridge.service.d"
            custom = directory / "other.conf"
            custom.write_text("[Service]\nEnvironment=USER_SETTING=yes\n")
            result = self.render(root, 8 * GIB_KIB)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((directory / MANAGED).exists())
            self.assertEqual(custom.read_text(), "[Service]\nEnvironment=USER_SETTING=yes\n")

    def test_user_memory_policy_wins_even_with_an_earlier_filename(self):
        for existing in (False, True):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                if existing:
                    self.assertEqual(self.render(root, 32 * GIB_KIB).returncode, 0)
                directory = root / "uu-remote-bridge.service.d"
                directory.mkdir(exist_ok=True)
                custom = directory / "00-user-policy.conf"
                content = "[Service]\nMemoryHigh=5G\nMemoryMax=7G\n"
                custom.write_text(content)
                result = self.render(root, 32 * GIB_KIB)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(custom.read_text(), content)
                self.assertFalse((directory / MANAGED).exists())

    def test_customize_managed_filename_is_never_overwritten_or_removed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(self.render(root, 32 * GIB_KIB).returncode, 0)
            budget = root / "uu-remote-bridge.service.d" / MANAGED
            content = budget.read_text().replace("MemoryMax=8G", "MemoryMax=7G")
            budget.write_text(content)
            for ram in (8 * GIB_KIB, 32 * GIB_KIB):
                result = self.render(root, ram)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(budget.read_text(), content)

    def test_symbolic_link_paths_are_rejected_without_touching_targets(self):
        for component in ("root", "directory", "file"):
            with self.subTest(component=component), tempfile.TemporaryDirectory() as temp:
                base = Path(temp)
                root, target = base / "units", base / "target"
                root.mkdir(); target.mkdir()
                marker = target / "marker"
                marker.write_text("unchanged")
                if component == "root":
                    root.rmdir(); root.symlink_to(target, target_is_directory=True)
                elif component == "directory":
                    (root / "uu-remote-bridge.service.d").symlink_to(target, target_is_directory=True)
                else:
                    (root / "uu-remote-bridge.service.d").mkdir()
                    (root / "uu-remote-bridge.service.d" / MANAGED).symlink_to(marker)
                result = self.render(root, 32 * GIB_KIB)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(marker.read_text(), "unchanged")
                self.assertEqual(sorted(path.name for path in target.iterdir()), ["marker"])

    def test_invalid_host_ram_cannot_modify_an_existing_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(self.render(root, 32 * GIB_KIB).returncode, 0)
            budget = root / "uu-remote-bridge.service.d" / MANAGED
            original = budget.read_bytes()
            for ram in ("", "invalid", "0", "-1"):
                result = self.render(root, ram)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(budget.read_bytes(), original)

    def test_baseline_retains_swap_thread_and_oom_restart_protection(self):
        unit = (ROOT / "systemd/uu-remote-bridge.service").read_text()
        for setting in ("MemoryHigh=3G", "MemoryMax=4G", "MemorySwapMax=2G",
                        "TasksMax=1024", "OOMPolicy=stop", "Restart=always"):
            self.assertIn(setting, unit)
        self.assertNotIn("infinity", RENDERER)

    def test_preflight_does_not_change_policy_and_precedes_bridge_stop(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = self.render(root, 32 * GIB_KIB, "preflight")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((root / "uu-remote-bridge.service.d").exists())
            self.assertEqual(self.render(root, 32 * GIB_KIB).returncode, 0)
            budget = root / "uu-remote-bridge.service.d" / MANAGED
            content = budget.read_bytes()
            self.assertEqual(self.render(root, 8 * GIB_KIB, "preflight").returncode, 0)
            self.assertEqual(budget.read_bytes(), content)
        self.assertLess(INSTALLER.index("    manage_memory_budget preflight"),
                        INSTALLER.index('"${systemctl_user[@]}" stop uu-remote-bridge.service'))

    def test_uninstall_removes_only_unchanged_managed_budget(self):
        uninstaller = (ROOT / "uninstall.sh").read_text()
        remover = uninstaller.split("<<'PY_REMOVE_MEMORY_BUDGET'", 1)[1].split("\nPY_REMOVE_MEMORY_BUDGET", 1)[0]
        for customized in (False, True):
            with self.subTest(customized=customized), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                self.assertEqual(self.render(root, 32 * GIB_KIB).returncode, 0)
                directory = root / "uu-remote-bridge.service.d"
                budget = directory / MANAGED
                custom = directory / "user.conf"
                custom.write_text("[Service]\nEnvironment=USER_SETTING=yes\n")
                if customized:
                    budget.write_text(budget.read_text() + "# user customized\n")
                result = subprocess.run(["/usr/bin/python3", "-", str(root)], input=remover,
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(budget.exists(), customized)
                self.assertEqual(custom.read_text(), "[Service]\nEnvironment=USER_SETTING=yes\n")


if __name__ == "__main__":
    unittest.main()
