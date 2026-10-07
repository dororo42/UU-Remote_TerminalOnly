#!/usr/bin/env python3
"""Validate against repository-reviewed source and PE pins, never runtime manifests."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import struct

RECIPE_FILES = (
    "bootstrap.sh", "build-sdl.sh", "build-freerdp.sh", "strict-owner-unit.py",
    "toolchain.cmake", "lto-cap.cmake", "check-lto.py", "mingw.ini",
    "source-lock.json", "freerdp-sdl-owner-refresh.patch",
)
PE_FILES = (
    "sdl-freerdp.exe", "libfreerdp3.dll", "libfreerdp-client3.dll", "libwinpr3.dll",
    "libcrypto-3-x64.dll", "libssl-3-x64.dll", "libcjson-1.dll", "liburiparser-1.dll",
    "libgcc_s_seh-1.dll", "libstdc++-6.dll", "libwinpthread-1.dll",
    "winpr-sspi-shim.dll", "ossl-modules/legacy.dll",
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def plain_file(path):
    require(path.is_file() and not any(p.is_symlink() for p in (path, *path.parents)),
            "Missing or symbolic-link input: " + str(path))
    return path


def sha(path):
    with plain_file(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def pins(repo):
    profile_path = repo / "patches/freerdp-sdl-product.json"
    profile = json.loads(plain_file(profile_path).read_text())
    require(profile.get("schema") == 1 and
            profile.get("approval") == "APPROVED_SOURCE_BUILD_CLOSURE",
            "FreeRDP product closure has not been independently approved")
    require(profile.get("source_commit") == "a8f1b46b0486b79e986d69530685af144268a9d0",
            "Unexpected FreeRDP source revision")
    recipe = repo / "vendor/freerdp-sdl-build"
    require(set(profile["recipe_files"]) == set(RECIPE_FILES), "Incomplete recipe pins")
    require(set(profile["files"]) == set(PE_FILES), "Incomplete thirteen-PE product pins")
    for name in RECIPE_FILES:
        expected = profile["recipe_files"][name]
        require(isinstance(expected, str) and re.fullmatch(r"[0-9a-f]{64}", expected),
                "Invalid recipe pin: " + name)
        require(sha(recipe / name) == expected, "Recipe identity mismatch: " + name)
    lock = json.loads((recipe / "source-lock.json").read_text())
    require(profile["source_lock_sha256"] == sha(recipe / "source-lock.json"),
            "Source lock identity mismatch")
    require(lock["git_sources"]["FreeRDP"]["commit"] == profile["source_commit"],
            "Source lock revision mismatch")
    for key in ("source", "build_recipe"):
        name = lock["compat_shim"][key]
        require(sha(repo / name) == lock["compat_shim"][key + "_sha256"],
                "Shim source/build identity mismatch: " + name)
    for name, pin in profile["files"].items():
        require(isinstance(pin, dict) and
                re.fullmatch(r"[0-9a-f]{64}", str(pin.get("sha256", ""))) and
                isinstance(pin.get("size"), int) and 64 < pin["size"] < 64 * 1024 * 1024,
                "Invalid PE pin: " + name)
    lines = "".join(profile["recipe_files"][name] + "  " + name + "\n"
                    for name in RECIPE_FILES)
    digest = hashlib.sha256(lines.encode()).hexdigest()
    return profile, sha(profile_path), digest


def validate(repo, output, mode):
    profile, profile_sha, recipe_digest = pins(repo)
    if mode == "profile":
        return
    if mode == "list":
        print("\n".join(PE_FILES))
        return
    require(output is not None, "Runtime directory required")
    for name in PE_FILES:
        path = plain_file(output / name)
        require(path.stat().st_size == profile["files"][name]["size"] and
                sha(path) == profile["files"][name]["sha256"], "PE hash mismatch: " + name)
        with path.open("rb") as stream:
            header = stream.read(64)
            require(header[:2] == b"MZ", "Missing PE header: " + name)
            offset = struct.unpack_from("<I", header, 60)[0]
            require(offset <= path.stat().st_size - 6, "Invalid PE offset: " + name)
            stream.seek(offset)
            require(stream.read(6) == b"PE\0\0\x64\x86", "Not AMD64 PE: " + name)
    require(plain_file(output / ".build-recipe").read_text().strip() == recipe_digest,
            "Build recipe receipt mismatch")
    if mode == "runtime":
        receipt = json.loads(plain_file(output / ".source-provenance.json").read_text())
        require(receipt == {"schema": 1, "profile_sha256": profile_sha,
                           "source_lock_sha256": profile["source_lock_sha256"],
                           "recipe_digest": recipe_digest}, "Source provenance binding mismatch")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("profile", "list", "build", "runtime"), default="runtime")
    parser.add_argument("directory", type=Path, nargs="?")
    args = parser.parse_args()
    repo = Path(__file__).absolute().parent.parent
    try:
        validate(repo, args.directory.absolute() if args.directory else None, args.mode)
    except (ValueError, KeyError, TypeError, OSError, json.JSONDecodeError) as error:
        raise SystemExit("FreeRDP product verification failed: " + str(error)) from error


if __name__ == "__main__":
    main()
