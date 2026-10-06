#!/usr/bin/env python3
"""GitHub runner only: pin SSH host key, back up old release, deploy without builds."""
import os
from pathlib import Path
import re
import shlex
import subprocess
import tarfile
import tempfile

from render import ROOT

host = os.environ['VPS_HOST']
user = os.environ['VPS_USER']
port = os.environ.get('VPS_PORT') or '22'
destination = os.environ['VPS_PATH']
commit = os.environ['DEPLOY_SHA']
if not re.fullmatch(r'[A-Za-z0-9.-]+', host) or not re.fullmatch(r'[a-z_][a-z0-9_-]*', user):
    raise ValueError('Invalid SSH host/user')
if not port.isdigit() or not 1 <= int(port) <= 65535:
    raise ValueError('Invalid SSH port')
if not re.fullmatch(r'/[A-Za-z0-9_./-]+', destination) or '..' in Path(destination).parts or destination == '/':
    raise ValueError('VPS_PATH must be a dedicated absolute path')
if not re.fullmatch(r'[0-9a-f]{40}', commit):
    raise ValueError('Invalid deploy SHA')
with tempfile.TemporaryDirectory() as folder:
    temp = Path(folder)
    key = temp / 'key'
    key.write_text(os.environ['VPS_SSH_KEY'] + '\n')
    key.chmod(0o600)
    known = temp / 'known_hosts'
    known.write_text(os.environ['VPS_KNOWN_HOSTS'] + '\n')
    known.chmod(0o600)
    files = subprocess.run(['git', 'ls-files', '-z'], cwd=ROOT, check=True, capture_output=True).stdout.decode().split('\0')
    archive = temp / 'release.tar'
    with tarfile.open(archive, 'w') as release:
        for name in files:
            if name and (ROOT / name).is_file():
                if name == '.env' or name.startswith(('runtime/', 'backups/', '.state/')):
                    raise ValueError('Secrets/data must never be tracked by Git')
                release.add(ROOT / name, arcname=name, recursive=False)
    ssh = ['ssh', '-i', str(key), '-p', port, '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
           '-o', 'UserKnownHostsFile=' + str(known), user + '@' + host]
    path = shlex.quote(destination)
    # An initial manual deploy provisions .env, DNS, GHCR login and TLS first.
    pre = f'cd {path} && test -s .env && make backup'
    subprocess.run(ssh + [pre], check=True)
    with archive.open('rb') as inp:
        subprocess.run(ssh + [f'tar -xf - -C {path}'], stdin=inp, check=True)
    code = "from pathlib import Path; import re; p=Path('.env'); s=p.read_text(); p.write_text(re.sub(r'^IMAGE_TAG=.*$', 'IMAGE_TAG=sha-" + commit + "', s, flags=re.M)); p.chmod(0o600)"
    command = f'cd {path} && python3 -c {shlex.quote(code)} && make update'
    subprocess.run(ssh + [command], check=True)
print('Deployed tested SHA; previous full backup retained on the server.')
