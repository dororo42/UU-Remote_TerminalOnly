#!/usr/bin/env python3
"""Compare PE files while ignoring only linker timestamp and checksum fields."""

from __future__ import annotations

import argparse
import hashlib
import struct
from pathlib import Path


class PEError(ValueError):
    """The input is not a structurally valid PE file for this comparison."""


def normalized_bytes(path: Path) -> bytes:
    data = bytearray(path.read_bytes())
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise PEError(f"{path} has no DOS/PE header")
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if pe_offset + 24 + 68 > len(data) or data[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise PEError(f"{path} has an invalid PE header offset")
    optional_header = pe_offset + 24
    magic = struct.unpack_from("<H", data, optional_header)[0]
    if magic not in (0x10B, 0x20B):
        raise PEError(f"{path} has an unsupported PE optional header")

    # COFF TimeDateStamp and OptionalHeader.CheckSum are the only bytes that
    # GNU ld changed between otherwise byte-identical builds.
    data[pe_offset + 8 : pe_offset + 12] = b"\0" * 4
    data[optional_header + 64 : optional_header + 68] = b"\0" * 4
    return bytes(data)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("left", type=Path)
    parser.add_argument("right", type=Path)
    args = parser.parse_args()
    try:
        left = normalized_bytes(args.left)
        right = normalized_bytes(args.right)
    except (OSError, PEError) as error:
        parser.error(str(error))
    if left == right:
        return 0
    print(
        "normalized PE files differ: "
        f"{hashlib.sha256(left).hexdigest()} != "
        f"{hashlib.sha256(right).hexdigest()}"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
