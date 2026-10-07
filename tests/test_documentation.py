from __future__ import annotations

import re
import unittest
from pathlib import Path
from urllib.parse import unquote, urlparse


REPO_DIR = Path(__file__).resolve().parent.parent
MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
# Local-only directories never distributed with the repository.
LOCAL_ONLY_PARTS = frozenset({"implementation-kit", "build", ".git", ".omc", "node_modules"})


class DocumentationTests(unittest.TestCase):
    def test_all_local_markdown_links_resolve(self) -> None:
        failures: list[str] = []
        for document in REPO_DIR.rglob("*.md"):
            if any(part in document.parts for part in LOCAL_ONLY_PARTS):
                continue
            text = document.read_text(encoding="utf-8")
            for raw_target in MARKDOWN_LINK.findall(text):
                target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
                parsed = urlparse(target)
                if parsed.scheme or target.startswith("#"):
                    continue
                relative = re.sub(r":\d+$", "", unquote(parsed.path))
                destination = (document.parent / relative).resolve()
                if any(part in destination.parts for part in LOCAL_ONLY_PARTS):
                    failures.append(
                        f"{document.relative_to(REPO_DIR)} -> {raw_target}"
                        " (points into a local-only directory)"
                    )
                elif not destination.exists():
                    failures.append(
                        f"{document.relative_to(REPO_DIR)} -> {raw_target}"
                    )
        self.assertEqual([], failures)

    def test_readme_states_the_terminal_only_purpose(self) -> None:
        readme = (REPO_DIR / "README.md").read_text(encoding="utf-8")
        for needle in (
            "UU-Remote_TerminalOnly",
            "terminal-only",
            "llmir/uu-remote-ubuntu-plus",
            "GaryOAO/UUWay",
        ):
            self.assertIn(needle, readme)


if __name__ == "__main__":
    unittest.main()
