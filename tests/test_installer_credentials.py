import ctypes as C
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('rdp_credentials', ROOT / 'scripts/uu-rdp-credentials.py')
credentials = importlib.util.module_from_spec(spec)
spec.loader.exec_module(credentials)


class InstallerCredentialsTests(unittest.TestCase):
    def test_real_glib_serialization_round_trips_utf8_and_special_characters(self):
        lib = credentials.glib_bindings()
        lib.g_variant_parse.argtypes = [C.c_void_p, C.c_char_p, C.c_void_p, C.c_void_p, C.c_void_p]
        lib.g_variant_parse.restype = C.c_void_p
        lib.g_variant_lookup_value.argtypes = [C.c_void_p, C.c_char_p, C.c_void_p]
        lib.g_variant_lookup_value.restype = C.c_void_p
        lib.g_variant_get_string.argtypes = [C.c_void_p, C.c_void_p]
        lib.g_variant_get_string.restype = C.c_char_p
        for password in ('random-ascii', 'quote\'"\\\n\t中文🙂'):
            user = '用户 name'
            serialized = credentials.serialize_credentials(lib, user, password)
            parsed = lib.g_variant_parse(None, serialized, None, None, None)
            self.assertTrue(parsed)
            try:
                for key, expected in (('username', user), ('password', password)):
                    value = lib.g_variant_lookup_value(parsed, key.encode(), None)
                    self.assertTrue(value)
                    try:
                        self.assertEqual(lib.g_variant_get_string(value, None).decode(), expected)
                    finally:
                        lib.g_variant_unref(value)
            finally:
                lib.g_variant_unref(parsed)

    def test_store_matches_gnome_schema_without_exposing_argv(self):
        class Secret:
            def secret_schema_new(self, *args):
                self.schema_args = args
                return 123
            def secret_schema_unref(self, schema):
                self.released = schema
            def secret_password_store_sync(self, *args):
                self.store_args = args
                return 1
        secret = Secret()
        credentials.store_credentials('user', 'temporary-test-password', secret=secret)
        self.assertEqual(secret.schema_args[0], b'org.gnome.RemoteDesktop.RdpCredentials')
        self.assertEqual(secret.store_args[1], b'default')
        self.assertIsNone(secret.store_args[-1])
        self.assertIn(b'temporary-test-password', secret.store_args[3])
        self.assertEqual(secret.released, 123)
        source = (ROOT / 'install.sh').read_text()
        self.assertNotIn('rdp set-credentials "$bridge_user" "$rdp_password"', source)
        self.assertIn('printf \'%s\' "$rdp_password" | "$python_bin"', source)

    def test_rejected_password_is_not_logged(self):
        password = b'private-test-marker\0bad'
        result = subprocess.run(['/usr/bin/python3', str(ROOT / 'scripts/uu-rdp-credentials.py'), 'user'], input=password, capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(b'private-test-marker', result.stdout + result.stderr)

    def test_installer_prefix_resolution_and_persistence(self):
        source = (ROOT / 'install.sh').read_text()
        prelude = source[source.index('bridge_user='):source.index("wine_bin=")]
        verifier = (ROOT / 'scripts/verify.sh').read_text()
        verify_prelude = verifier[verifier.index('bridge_user='):verifier.index('release_manifest=')]
        persist = next(line for line in source.splitlines() if line.startswith("printf 'UURB_WINEPREFIX="))
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / '.config/uu-remote-bridge'
            config.mkdir(parents=True)
            (config / 'environment').write_text('UURB_WINEPREFIX=' + str(home / 'saved prefix') + '\n')
            base = {k: v for k, v in os.environ.items() if k not in ('WINEPREFIX', 'UURB_WINEPREFIX')}
            base['HOME'] = str(home)
            cases = [({}, home / 'saved prefix'), ({'WINEPREFIX': str(home / 'wine override')}, home / 'wine override'), ({'WINEPREFIX': str(home / 'wine override'), 'UURB_WINEPREFIX': str(home / 'bridge override')}, home / 'bridge override')]
            for overrides, expected in cases:
                out = home / 'persisted'
                out.write_text('')
                script = prelude + '\nenvironment_tmp="$HOME/persisted"\n' + persist
                result = subprocess.run(['bash', '-Eeuo', 'pipefail', '-c', script], env=base | overrides, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(out.read_text(), 'UURB_WINEPREFIX=' + str(expected) + '\n')
                verify_result = subprocess.run(['bash', '-Eeuo', 'pipefail', '-c', verify_prelude + '\nprintf \'%s\\n\' \"$wine_prefix\"'], env=base | overrides, capture_output=True, text=True)
                self.assertEqual(verify_result.returncode, 0, verify_result.stderr)
                self.assertEqual(verify_result.stdout.strip(), str(expected))


if __name__ == '__main__':
    unittest.main()
