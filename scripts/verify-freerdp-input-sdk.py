#!/usr/bin/env python3
"""Verify the pinned public input ABI headers and upstream license."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "vendor/freerdp-input-sdk"
MANIFEST_SHA = "d7bd9a5a05d852fe746e5f6041e9dcad30993eba16052fe0dda6c33378f86129"
SOURCE_COMMIT = "a8f1b46b0486b79e986d69530685af144268a9d0"

for path in (ROOT, ROOT / "include", ROOT / "manifest.json"):
    if path.is_symlink():
        raise SystemExit("Refusing symbolic-link SDK input")
raw = (ROOT / "manifest.json").read_bytes()
if hashlib.sha256(raw).hexdigest() != MANIFEST_SHA:
    raise SystemExit("Unreviewed FreeRDP input SDK manifest")
data = json.loads(raw)
if data["source_commit"] != SOURCE_COMMIT or len(data["files"]) != 261:
    raise SystemExit("Wrong SDK ABI source")
paths = list((ROOT / "include").rglob("*"))
if any(path.is_symlink() for path in paths):
    raise SystemExit("Refusing symbolic-link SDK header")
actual = {path.relative_to(ROOT).as_posix() for path in paths if path.is_file()}
if actual != set(data["files"]):
    raise SystemExit("SDK header inventory mismatch")
for name, expected in {**data["files"], "LICENSE": data["LICENSE"]}.items():
    path = ROOT / name
    if path.is_symlink() or not path.is_file():
        raise SystemExit("Invalid SDK source: " + name)
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise SystemExit("SDK source changed: " + name)
print("Pinned a8f1b46 public input SDK headers and license verified")
