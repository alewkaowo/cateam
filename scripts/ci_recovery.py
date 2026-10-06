#!/usr/bin/env python3
"""Exercise the actual recovery implementation with fresh CI volumes."""
import re
import subprocess
import sys
from manage import backup, compose, restore
from render import ROOT, load, write

original = load()
backup(original)
folder = sorted((ROOT / 'backups').glob('*/COMPLETE'))[-1].parent
compose('down')
path = ROOT / '.env'
write(path, re.sub(r'^COMPOSE_PROJECT_NAME=.*$', 'COMPOSE_PROJECT_NAME=matrix-ci-restore', path.read_text(), flags=re.M), 0o600)
try:
    restore(load(), str(folder), pull=False)  # CI uses local, not-yet-published tested web image.
    subprocess.run([sys.executable, str(ROOT / 'scripts/probe.py'), '--persistence'], check=True)
finally:
    compose('down', '-v')
    write(path, re.sub(r'^COMPOSE_PROJECT_NAME=.*$', 'COMPOSE_PROJECT_NAME=matrix-ci', path.read_text(), flags=re.M), 0o600)
print('Actual backup/restore to new named volumes: OK')
