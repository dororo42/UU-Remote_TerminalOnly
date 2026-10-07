"""Check generated IPO flags before the first Ninja build, without compiling."""
import shlex
import subprocess
import sys

commands = subprocess.check_output(['ninja', '-C', sys.argv[1], '-t', 'commands'], text=True)
flags = [item for line in commands.splitlines() for item in shlex.split(line) if item.startswith('-flto')]
assert all(item == '-flto=2' for item in flags), 'Unbounded or unexpected LTO parallelism'
if len(sys.argv) > 2:
    assert flags, 'Expected IPO flags in FreeRDP commands'
