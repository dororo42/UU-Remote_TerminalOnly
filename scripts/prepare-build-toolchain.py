#!/usr/bin/env python3
"""Download and privately extract the pinned Ubuntu 26.04 reference build tools."""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from urllib.parse import urlparse


REPOSITORY = Path(__file__).resolve().parents[1]
PACKAGES = REPOSITORY / "patches/reference-build-packages.json"
SOURCE_LOCK = REPOSITORY / "vendor/freerdp-sdl-build/source-lock.json"


def check_package(path, package):
    if path.is_symlink() or not path.is_file() or path.stat().st_size != package["bytes"]:
        raise ValueError("Package size or file type mismatch: " + package["filename"])
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != package["sha256"]:
        raise ValueError("Package SHA256 mismatch: " + package["filename"])


def prepare(packages_dir, root, verify_only=False):
    manifest = json.loads(PACKAGES.read_text())
    tools = json.loads(SOURCE_LOCK.read_text())["compiler_build_tools"]
    packages_dir = packages_dir.expanduser().resolve()
    root = root.expanduser().resolve()
    if root == Path("/") or root.is_relative_to(Path("/usr")):
        raise ValueError("Choose a private extraction directory outside /usr")
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise ValueError("Extraction directory must be new or empty")
    packages_dir.mkdir(parents=True, exist_ok=True)
    archives = []
    for package in manifest["packages"]:
        url = urlparse(package["url"])
        filename = package["filename"]
        if (url.scheme != "https" or url.netloc != "archive.ubuntu.com"
                or not url.path.startswith("/ubuntu/pool/")
                or Path(filename).name != filename or not url.path.endswith("/" + filename)):
            raise ValueError("Expected a fixed official Ubuntu package URL")
        archive = packages_dir / filename
        if not archive.exists() and not archive.is_symlink():
            if verify_only:
                raise ValueError("Missing cached package: " + filename)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=packages_dir, suffix=".part", delete=False) as stream:
                    temporary = Path(stream.name)
                    with urllib.request.urlopen(package["url"], timeout=60) as response:
                        redirected = urlparse(response.geturl())
                        if redirected.scheme != "https" or redirected.netloc != "archive.ubuntu.com":
                            raise ValueError("Package download left the official Ubuntu archive")
                        shutil.copyfileobj(response, stream)
                check_package(temporary, package)
                temporary.replace(archive)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        check_package(archive, package)
        archives.append(archive)
    root.mkdir(parents=True, exist_ok=True)
    for archive in archives:
        subprocess.run(["dpkg-deb", "--extract", str(archive), str(root)], check=True)
    for name, expected in tools.items():
        tool = root / name.lstrip("/")
        if not tool.resolve().is_relative_to(root) or not tool.is_file():
            raise ValueError("Missing private tool: " + name)
        with tool.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != expected:
            raise ValueError("Reference tool SHA256 mismatch: " + name)
    print(f"Prepared {len(archives)} packages and verified {len(tools)} reference tools for {manifest['distribution']} amd64.")
    print("Packages: " + str(packages_dir))
    print("Private extraction root: " + str(root))
    print("This prepares reference tools; complete OS dependencies and a fresh build remain separate steps.")
    print("To install these packages manually on Ubuntu 26.04 (changes system packages):")
    print(shlex.join(["sudo", "apt", "install", *map(str, archives)]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packages-dir", type=Path, default=REPOSITORY / "build/reference-toolchain/packages")
    parser.add_argument("--root", type=Path, default=REPOSITORY / "build/reference-toolchain/root",
                        help="new or empty private extraction directory")
    parser.add_argument("--verify-only", action="store_true", help="use cached packages only; never download")
    args = parser.parse_args()
    try:
        prepare(args.packages_dir, args.root, args.verify_only)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print("Toolchain preparation failed: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
