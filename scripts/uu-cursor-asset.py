#!/usr/bin/env python3
"""Export a themed Xcursor pointer as a Windows alpha cursor."""
import argparse
import ctypes
import os
from pathlib import Path
import struct
import sys
import tempfile


class XcursorImage(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint) for name in
                ("version", "size", "width", "height", "xhot", "yhot", "delay")]
    _fields_.append(("pixels", ctypes.POINTER(ctypes.c_uint32)))


def encode_cursor(width, height, xhot, yhot, pixels, size=None):
    if not (0 < width <= 256 and 0 < height <= 256 and
            0 <= xhot < width and 0 <= yhot < height and
            len(pixels) == width * height):
        raise ValueError("invalid Xcursor dimensions or hotspot")
    output_width = size or width
    output_height = size or height
    scale = size / max(width, height) if size else 1
    scaled_width = max(1, round(width * scale))
    scaled_height = max(1, round(height * scale))
    output_xhot = min(scaled_width - 1, round(xhot * scale))
    output_yhot = min(scaled_height - 1, round(yhot * scale))
    xor = bytearray()
    mask_stride = ((output_width + 31) // 32) * 4
    mask = bytearray(mask_stride * output_height)
    for output_y in range(output_height - 1, -1, -1):
        for output_x in range(output_width):
            channels = [0, 0, 0, 0]
            if output_x < scaled_width and output_y < scaled_height:
                source_x = max(0, min(width - 1, (output_x + 0.5) / scale - 0.5))
                source_y = max(0, min(height - 1, (output_y + 0.5) / scale - 0.5))
                x0, y0 = int(source_x), int(source_y)
                dx, dy = source_x - x0, source_y - y0
                for x, y, weight in (
                    (x0, y0, (1 - dx) * (1 - dy)),
                    (min(x0 + 1, width - 1), y0, dx * (1 - dy)),
                    (x0, min(y0 + 1, height - 1), (1 - dx) * dy),
                    (min(x0 + 1, width - 1), min(y0 + 1, height - 1), dx * dy),
                ):
                    pixel = pixels[y * width + x]
                    for channel in range(4):
                        channels[channel] += ((pixel >> (channel * 8)) & 255) * weight
                channels = [round(value) for value in channels]
            alpha = channels[3]
            # Xcursor uses premultiplied ARGB; Windows CUR stores straight BGRA.
            if alpha:
                channels[:3] = [min(255, round(value * 255 / alpha))
                                for value in channels[:3]]
            else:
                channels[:3] = [0, 0, 0]
                row = output_height - 1 - output_y
                mask[row * mask_stride + output_x // 8] |= 1 << (7 - output_x % 8)
            xor.extend(channels)
    bitmap = struct.pack("<IiiHHIIiiII", 40, output_width, output_height * 2,
                         1, 32, 0, len(xor) + len(mask), 0, 0, 0, 0)
    image = bitmap + xor + mask
    directory = struct.pack("<BBBBHHII", output_width % 256, output_height % 256,
                            0, 0, output_xhot, output_yhot, len(image), 22)
    return struct.pack("<HHH", 0, 2, 1) + directory + image


def load_cursor(theme, size):
    library = ctypes.CDLL("libXcursor.so.1")
    library.XcursorLibraryLoadImage.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
    library.XcursorLibraryLoadImage.restype = ctypes.POINTER(XcursorImage)
    library.XcursorImageDestroy.argtypes = [ctypes.POINTER(XcursorImage)]
    library.XcursorImageDestroy.restype = None
    image = library.XcursorLibraryLoadImage(b"left_ptr", theme.encode() if theme else None, size)
    if not image:
        raise ValueError("the desktop cursor theme has no left_ptr image")
    try:
        data = image.contents
        if not (0 < data.width <= 256 and 0 < data.height <= 256 and data.pixels):
            raise ValueError("invalid Xcursor image")
        pixels = list(data.pixels[:data.width * data.height])
        return encode_cursor(data.width, data.height, data.xhot, data.yhot, pixels, size)
    finally:
        library.XcursorImageDestroy(image)


def write_cursor(output, theme, size):
    temporary = None
    try:
        cursor = load_cursor(theme, size)
        descriptor, temporary = tempfile.mkstemp(prefix=".uu-cursor.", dir=output.parent)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(cursor)
        os.replace(temporary, output)
        return True
    except (OSError, ValueError, AttributeError) as error:
        # Never leave a stale theme/size from a previous bridge invocation.
        output.unlink(missing_ok=True)
        print(f"Desktop cursor asset unavailable; using the built-in cursor: {error}",
              file=sys.stderr)
        return False
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--theme", default="")
    parser.add_argument("--size", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if not 24 <= args.size <= 128:
        parser.error("cursor size must be between 24 and 128")
    return 0 if write_cursor(args.output, args.theme, args.size) else 1


if __name__ == "__main__":
    sys.exit(main())
