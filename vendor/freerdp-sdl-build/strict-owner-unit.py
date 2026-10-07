"""Recompile only the patched owner unit with the reviewed warning boundary."""
from pathlib import Path
import json
import shlex
import subprocess
import sys

root = Path(sys.argv[1]).resolve()
build = root / 'static-variant/freerdp-build'
source = root / 'FreeRDP/client/SDL/SDL3/sdl_context.cpp'
entry = next(item for item in json.loads((build / 'compile_commands.json').read_text()) if Path(item['file']).resolve() == source)
argv = shlex.split(entry['command'])
system_headers = {str(root / 'FreeRDP/winpr/include'), str(build / 'winpr/include')}
argv = [('-isystem' + item[2:]) if item.startswith('-I') and item[2:] in system_headers else item for item in argv]
argv = ['-flto=2' if item in ('-flto=auto', '-flto') else item for item in argv]
argv += ['-Wall', '-Wextra', '-Werror']
subprocess.run(argv, cwd=entry['directory'], check=True, timeout=40)
commands = subprocess.check_output(['ninja', '-C', str(build), '-t', 'commands', 'sdl3-freerdp'], text=True)
links = []
for line in commands.splitlines():
    items = shlex.split(line)
    if items[:2] == [':', '&&']:
        items = items[2:]
    if items[-2:] == ['&&', ':']:
        items = items[:-2]
    if '-o' in items and '-c' not in items and items[items.index('-o') + 1].endswith('/sdl-freerdp.exe'):
        assert not any(item in ('&&', ';', '|') for item in items)
        links.append(items)
assert len(links) == 1, 'Expected one SDL link command'
argv = ['-flto=2' if item in ('-flto=auto', '-flto') else item for item in links[0]]
subprocess.run(argv + ['-Werror'], cwd=build, check=True, timeout=50)
