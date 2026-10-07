#!/usr/bin/env python3
"""Store GNOME's RDP credential using libsecret, with the password on stdin.

The schema and a{sv} value match GNOME Remote Desktop's screen-share backend.
Only the existing GLib/libsecret libraries are required; no Python GI package.
"""

import ctypes as C
import sys


def glib_bindings():
    lib = C.CDLL("libglib-2.0.so.0")
    signatures = {
        "g_variant_type_new": ([C.c_char_p], C.c_void_p),
        "g_variant_type_free": ([C.c_void_p], None),
        "g_variant_new_string": ([C.c_char_p], C.c_void_p),
        "g_variant_new_variant": ([C.c_void_p], C.c_void_p),
        "g_variant_new_dict_entry": ([C.c_void_p, C.c_void_p], C.c_void_p),
        "g_variant_new_array": ([C.c_void_p, C.POINTER(C.c_void_p), C.c_size_t], C.c_void_p),
        "g_variant_ref_sink": ([C.c_void_p], C.c_void_p),
        "g_variant_unref": ([C.c_void_p], None),
        "g_variant_print": ([C.c_void_p, C.c_int], C.c_void_p),
        "g_free": ([C.c_void_p], None),
        "g_error_free": ([C.c_void_p], None),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(lib, name)
        function.argtypes, function.restype = arguments, result
    return lib


def serialize_credentials(lib, username, password):
    if not username or not password or "\0" in username or "\0" in password:
        raise ValueError("Empty credentials or NUL characters are not supported")
    entries = []
    for key, value in (("username", username), ("password", password)):
        entries.append(lib.g_variant_new_dict_entry(
            lib.g_variant_new_string(key.encode("utf-8")),
            lib.g_variant_new_variant(lib.g_variant_new_string(value.encode("utf-8"))),
        ))
    entry_type = lib.g_variant_type_new(b"{sv}")
    try:
        array = (C.c_void_p * len(entries))(*entries)
        value = lib.g_variant_ref_sink(lib.g_variant_new_array(entry_type, array, len(entries)))
    finally:
        lib.g_variant_type_free(entry_type)
    printed = None
    try:
        printed = lib.g_variant_print(value, True)
        return C.string_at(printed)
    finally:
        if printed:
            lib.g_free(printed)
        lib.g_variant_unref(value)


def secret_bindings():
    lib = C.CDLL("libsecret-1.so.0")
    # These functions are variadic; argtypes describe only the fixed arguments.
    lib.secret_schema_new.argtypes = [C.c_char_p, C.c_int]
    lib.secret_schema_new.restype = C.c_void_p
    lib.secret_schema_unref.argtypes = [C.c_void_p]
    lib.secret_schema_unref.restype = None
    lib.secret_password_store_sync.argtypes = [
        C.c_void_p, C.c_char_p, C.c_char_p, C.c_char_p,
        C.c_void_p, C.POINTER(C.c_void_p),
    ]
    lib.secret_password_store_sync.restype = C.c_int
    return lib


def store_credentials(username, password, glib=None, secret=None):
    glib = glib if glib is not None else glib_bindings()
    secret = secret if secret is not None else secret_bindings()
    serialized = serialize_credentials(glib, username, password)
    schema = secret.secret_schema_new(
        b"org.gnome.RemoteDesktop.RdpCredentials", 0, b"credentials", C.c_int(0), None,
    )
    if not schema:
        raise RuntimeError("Could not create the RDP credential schema")
    error = C.c_void_p()
    try:
        if not secret.secret_password_store_sync(
            schema, b"default", b"GNOME Remote Desktop RDP credentials", serialized,
            None, C.byref(error), None,
        ):
            raise RuntimeError("Could not store GNOME RDP credentials")
    finally:
        if error.value:
            glib.g_error_free(error)
        secret.secret_schema_unref(schema)


def main():
    if len(sys.argv) != 2:
        print("Usage: uu-rdp-credentials.py USERNAME < password", file=sys.stderr)
        return 2
    try:
        password = sys.stdin.buffer.read(65537)
        if len(password) > 65536:
            raise ValueError("Credential is too long")
        store_credentials(sys.argv[1], password.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, RuntimeError):
        # Do not expose credential bytes or libsecret's error message in logs.
        print("Could not store GNOME RDP credentials.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
