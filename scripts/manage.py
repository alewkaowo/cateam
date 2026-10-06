#!/usr/bin/env python3
"""Deployment and recovery; no builds, no shell-evaluated configuration."""
import datetime
import fcntl
import getpass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import subprocess
import sys
import tarfile

from render import ROOT, SECRETS, configure, load, read_env, validate, write

SERVICES = ('postgres', 'synapse', 'livekit', 'matrixrtc', 'nginx', 'coturn')


def compose_args():
    return ['docker', 'compose', '--env-file', str(ROOT / '.env'), '--env-file', str(ROOT / 'versions.env')]


def compose(*args, **kw):
    # Explicit values take precedence over stale shell environment.
    env = os.environ | load()
    return subprocess.run(compose_args() + list(args), cwd=ROOT, env=env, check=True, **kw)


def tools(command, **kw):
    return compose('run', '--rm', '--no-deps', '-T', 'tools', command, **kw)


def init():
    path = ROOT / '.env'
    if not path.exists():
        write(path, (ROOT / 'versions.env').read_text() + '\n' + (ROOT / '.env.example').read_text(), 0o600)
    e = read_env(path)
    content = path.read_text()
    for key in SECRETS:
        if not e.get(key):
            value = secrets.token_hex(32)
            if key in e:
                content = re.sub(rf'^{key}=[ \t]*$', f'{key}={value}', content, flags=re.M)
            else:
                content += f'\n{key}={value}\n'
    write(path, content, 0o600)
    print('Secrets generated; edit PUBLIC_IPV4, LETSENCRYPT_EMAIL and IMAGE_PREFIX in .env.')


def available_disk(e):
    minimum = int(e['MIN_FREE_DISK_GB']) * 1024**3
    roots = {ROOT, Path('/var/lib/docker')}
    for root in roots:
        if root.exists() and shutil.disk_usage(root).free < minimum:
            raise ValueError(f'Less than {e["MIN_FREE_DISK_GB"]} GiB free on {root}; operation stopped')


def monitor(e):
    guard = ROOT / 'runtime/nginx/.uploads-blocked'
    try:
        available_disk(e)
    except ValueError:
        write(guard, 'Disk space low; new uploads disabled\n', 0o600)
        raise
    if guard.exists():
        guard.unlink()
    print('Disk reserve sufficient; uploads enabled')


def image_config():
    result = compose('config', '--format', 'json', capture_output=True, text=True)
    return json.loads(result.stdout)


def initialize(e):
    # Environment passed as container env, not as shell-interpolated source.
    compose('run', '--rm', '--no-deps', '-T', '-e', 'EXPECTED_SERVER=' + e['MATRIX_DOMAIN'], 'tools',
            '''set -eu
if [ -f /synapse/.server-name ] && [ "$(cat /synapse/.server-name)" != "$EXPECTED_SERVER" ]; then
  echo 'Existing volume has a different Matrix identity' >&2; exit 1
fi
printf '%s\n' "$EXPECTED_SERVER" > /synapse/.server-name
chown 991:991 /synapse
chmod 750 /synapse
''')
    write(ROOT / '.state/server-name', e['MATRIX_DOMAIN'] + '\n', 0o600)


def certificate_exists():
    result = subprocess.run(compose_args() + ['run', '--rm', '--no-deps', '-T', 'tools',
        'test -s /tls/live/matrix/fullchain.pem && test -s /tls/live/matrix/privkey.pem'],
        cwd=ROOT, env=os.environ | load(), stdout=subprocess.DEVNULL)
    return result.returncode == 0


def copy_turn_tls():
    tools('''set -eu
cp /tls/live/matrix/fullchain.pem /turn-tls/fullchain.pem.new
cp /tls/live/matrix/privkey.pem /turn-tls/privkey.pem.new
chown 10001:10001 /turn-tls/*.new
chmod 600 /turn-tls/*.new
chmod 755 /turn-tls
mv /turn-tls/fullchain.pem.new /turn-tls/fullchain.pem
mv /turn-tls/privkey.pem.new /turn-tls/privkey.pem
''')


def cert(e):
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', e.get('LETSENCRYPT_EMAIL', '')):
        raise ValueError('Set LETSENCRYPT_EMAIL to a working email address')
    configure(e, bootstrap=True)
    compose('up', '-d', '--no-deps', 'nginx')
    compose('exec', '-T', 'nginx', 'nginx', '-t')
    compose('exec', '-T', 'nginx', 'nginx', '-s', 'reload')
    args = ['run', '--rm', '--no-deps', '-T', 'certbot', 'certonly', '--webroot', '-w', '/var/www/acme',
            '--cert-name', 'matrix', '--email', e['LETSENCRYPT_EMAIL'], '--agree-tos', '--non-interactive']
    for key in ('MATRIX_DOMAIN', 'ELEMENT_DOMAIN', 'RTC_DOMAIN'):
        args += ['-d', e[key]]
    compose(*args)
    copy_turn_tls()
    configure(e)
    compose('exec', '-T', 'nginx', 'nginx', '-t')
    compose('exec', '-T', 'nginx', 'nginx', '-s', 'reload')


def deploy(e, pull=True):
    available_disk(e)
    configure(e)
    compose('config', '--quiet')
    if pull:
        compose('pull', *SERVICES, 'certbot', 'tools')
    initialize(e)
    if not certificate_exists():
        cert(e)
    else:
        copy_turn_tls()
    configure(e)
    compose('up', '-d', '--wait', '--wait-timeout', '240', *SERVICES)
    compose('exec', '-T', 'nginx', 'nginx', '-t')
    compose('exec', '-T', 'nginx', 'nginx', '-s', 'reload')
    # A restored certificate may be expired. Renew before the public TLS probe.
    if pull:
        renew(e)
    subprocess.run([sys.executable, str(ROOT / 'scripts/probe.py')], check=True)
    print('Deployed. Test real calls using docs/acceptance.md before inviting everyone.')


def renew(e):
    compose('run', '--rm', '--no-deps', '-T', 'certbot', 'renew', '--non-interactive', '--webroot', '-w', '/var/www/acme')
    before = tools('sha256sum /turn-tls/fullchain.pem', capture_output=True, text=True).stdout
    copy_turn_tls()
    after = tools('sha256sum /turn-tls/fullchain.pem', capture_output=True, text=True).stdout
    compose('exec', '-T', 'nginx', 'nginx', '-t')
    compose('exec', '-T', 'nginx', 'nginx', '-s', 'reload')
    if before != after:
        compose('restart', 'coturn')
        print('New TLS certificate applied; TURN allocations may briefly reconnect.')


def account(e, name, admin=False):
    if not re.fullmatch(r'[a-z0-9._=-]{1,100}', name):
        raise ValueError('Use ACCOUNT=alice (lowercase localpart)')
    password = getpass.getpass('Password: ')
    if len(password) < 12 or password != getpass.getpass('Repeat password: '):
        raise ValueError('Passwords must match and have at least 12 characters')
    # Interactive registration CLI avoids secrets in argv; API runs only inside Synapse.
    code = '''import hashlib,hmac,json,sys,urllib.request,yaml
p=json.load(sys.stdin)
s=yaml.safe_load(open('/config/homeserver.yaml'))['registration_shared_secret']
u='http://127.0.0.1:8008/_synapse/admin/v1/register'
n=json.load(urllib.request.urlopen(u))['nonce']
b='\\x00'.join([n,p['username'],p['password'],'admin' if p['admin'] else 'notadmin'])
p['nonce']=n;p['mac']=hmac.new(s.encode(),b.encode(),hashlib.sha1).hexdigest()
r=urllib.request.Request(u,data=json.dumps(p).encode(),headers={'Content-Type':'application/json'})
with urllib.request.urlopen(r) as response: assert response.status==200
'''
    compose('exec', '-T', 'synapse', 'python', '-c', code,
            input=json.dumps({'username': name, 'password': password, 'admin': admin}), text=True,
            stdout=subprocess.DEVNULL)
    print(f'Created @{name}:{e["MATRIX_DOMAIN"]}')


def running():
    return compose('ps', '--status', 'running', '--services', capture_output=True, text=True).stdout.split()


def backup(e):
    available_disk(e)
    if not {'postgres', 'synapse'}.issubset(running()):
        raise ValueError('Start PostgreSQL and Synapse before backup')
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    folder = ROOT / 'backups' / stamp
    folder.mkdir(mode=0o700)
    cfg = image_config()
    manifest = {'server_name': e['MATRIX_DOMAIN'], 'created_utc': stamp,
                'images': {k: cfg['services'][k]['image'] for k in SERVICES}, 'format': 1}
    # Conservatively reserve uncompressed source volume size + DB size + operating margin.
    size = tools('du -sk /synapse /tls /turn-tls', capture_output=True, text=True).stdout
    required = sum(int(line.split()[0]) * 1024 for line in size.splitlines())
    dbsize = compose('exec', '-T', 'postgres', 'psql', '-U', 'synapse', '-d', 'synapse', '-Atc',
                     "SELECT pg_database_size('synapse')", capture_output=True, text=True).stdout.strip()
    if shutil.disk_usage(folder).free < required + int(dbsize) + int(e['MIN_FREE_DISK_GB']) * 1024**3:
        raise ValueError('Backup needs more disk; mount backups/ on external storage')
    compose('stop', 'synapse')
    try:
        with (folder / 'postgres.dump').open('wb') as out:
            compose('exec', '-T', 'postgres', 'pg_dump', '-U', 'synapse', '-d', 'synapse', '-Fc', stdout=out)
        tools(f'tar -czf /backup/{stamp}/volumes.tar.gz -C / synapse tls turn-tls acme && chmod 600 /backup/{stamp}/volumes.tar.gz && chown {os.getuid()}:{os.getgid()} /backup/{stamp}/volumes.tar.gz')
        with tarfile.open(folder / 'config.tar.gz', 'w:gz') as archive:
            for path in ('.env', 'versions.env', 'runtime', '.state/server-name'):
                if (ROOT / path).exists():
                    archive.add(ROOT / path, arcname=path)
        write(folder / 'manifest.json', json.dumps(manifest, indent=2) + '\n', 0o600)
        checksums = ''.join(f'{hashlib.file_digest(p.open("rb"), "sha256").hexdigest()}  {p.name}\n'
                            for p in sorted(folder.iterdir()) if p.is_file())
        write(folder / 'SHA256SUMS', checksums, 0o600)
        write(folder / 'COMPLETE', 'Complete backup\n', 0o600)
    finally:
        compose('up', '-d', '--wait', '--wait-timeout', '240', 'synapse')
    print(f'Backup: {folder}. Contains secrets; encrypt and copy off-server.')


def safe_archive(path, prefixes):
    with tarfile.open(path, 'r:gz') as archive:
        for member in archive.getmembers():
            parts = PurePosixPath(member.name).parts
            if not parts or parts[0] not in prefixes or member.name.startswith('/') or '..' in parts:
                raise ValueError('Unsafe archive path')
            if member.isdev() or member.isfifo():
                raise ValueError('Special files are not accepted in backups')
            if member.issym() or member.islnk():
                import posixpath
                target = posixpath.normpath(posixpath.join(posixpath.dirname(member.name), member.linkname))
                if target.startswith('/') or target.split('/')[0] not in prefixes or target.startswith('../'):
                    raise ValueError('Unsafe archive link')


def restore(e, source, pull=True):
    folder = Path(source).expanduser().resolve()
    if not (folder / 'COMPLETE').exists():
        raise ValueError('Use a complete backup directory')
    expected_files = {'postgres.dump', 'volumes.tar.gz', 'config.tar.gz', 'manifest.json'}
    seen = set()
    for line in (folder / 'SHA256SUMS').read_text().splitlines():
        digest, name = line.split('  ', 1)
        if name not in expected_files or name in seen:
            raise ValueError('Unexpected backup filename')
        seen.add(name)
        with (folder / name).open('rb') as file:
            if hashlib.file_digest(file, 'sha256').hexdigest() != digest:
                raise ValueError(f'Checksum mismatch: {name}')
    if seen != expected_files:
        raise ValueError('Backup checksum list is incomplete')
    if running():
        raise ValueError('Restore requires a stopped installation. Use a new empty deployment.')
    manifest = json.loads((folder / 'manifest.json').read_text())
    if manifest.get('format') != 1:
        raise ValueError('Unsupported backup format')
    if manifest['server_name'] != e['MATRIX_DOMAIN']:
        raise ValueError('Restore must preserve the Matrix domain')
    for filename, prefixes in [('config.tar.gz', {'.env', 'versions.env', 'runtime', '.state'}),
                               ('volumes.tar.gz', {'synapse', 'tls', 'turn-tls', 'acme'})]:
        safe_archive(folder / filename, prefixes)
    tools('test -z "$(ls -A /synapse)" && test -z "$(ls -A /tls)" && test -z "$(ls -A /turn-tls)"')
    # Restore original versions/config first, then operate only on matching images.
    with tarfile.open(folder / 'config.tar.gz') as archive:
        archive.extractall(ROOT, filter='data')
    # Preserve the destination's project name so recovery can use fresh named volumes.
    envpath = ROOT / '.env'
    content = re.sub(r'^COMPOSE_PROJECT_NAME=.*$', 'COMPOSE_PROJECT_NAME=' + e['COMPOSE_PROJECT_NAME'],
                     envpath.read_text(), flags=re.M)
    write(envpath, content, 0o600)
    restored = load()
    validate(restored)
    configured = image_config()
    if manifest['images'] != {name: configured['services'][name]['image'] for name in SERVICES}:
        raise ValueError('Backup manifest does not match restored image versions')
    if pull:
        compose('pull', *SERVICES, 'tools')
    compose('up', '-d', '--wait', 'postgres')
    count = compose('exec', '-T', 'postgres', 'psql', '-U', 'synapse', '-d', 'synapse', '-Atc',
                    "SELECT count(*) FROM pg_tables WHERE schemaname='public'", capture_output=True, text=True).stdout.strip()
    if count != '0':
        raise ValueError('Refusing to restore into a nonempty PostgreSQL database')
    # Mount the supplied backup read-only; avoid copying it onto the small VPS disk.
    compose('run', '--rm', '--no-deps', '-T', '-v', str(folder) + ':/restore:ro', 'tools',
            'tar -xzf /restore/volumes.tar.gz -C / && chown 991:991 /synapse && chmod 750 /synapse')
    with (folder / 'postgres.dump').open('rb') as inp:
        compose('exec', '-T', 'postgres', 'pg_restore', '-U', 'synapse', '-d', 'synapse', '--exit-on-error', stdin=inp)
    deploy(restored, pull=pull)


def doctor(e):
    available_disk(e)
    compose('ps')
    subprocess.run(['docker', 'stats', '--no-stream'], check=True)
    subprocess.run(['docker', 'system', 'df'], check=True)
    subprocess.run(['df', '-h', str(ROOT)], check=True)
    subprocess.run(['free', '-h'], check=False)
    if 'postgres' in running():
        compose('exec', '-T', 'postgres', 'psql', '-U', 'synapse', '-d', 'synapse', '-c',
                "SELECT pg_size_pretty(pg_database_size('synapse')) AS database_size;")


def main():
    os.umask(0o077)
    action = sys.argv[1] if len(sys.argv) > 1 else 'help'
    if action == 'init':
        init()
        return
    e = load()
    if action == 'compose':
        args = sys.argv[2:]
        if any(a in ('build', '--build') for a in args):
            raise ValueError('Builds on VPS are forbidden; use GitHub Actions')
        compose(*args)
        return
    validate(e)
    if action == 'configure':
        configure(e)
    elif action == 'config':
        compose('config', '--quiet')
        print('Compose config: OK')
    elif action == 'pull':
        available_disk(e)
        compose('pull', *SERVICES, 'certbot', 'tools')
    elif action in ('deploy', 'update'):
        deploy(e)
    elif action == 'cert':
        cert(e)
        if 'coturn' in running():
            compose('restart', 'coturn')
    elif action == 'renew':
        renew(e)
    elif action in ('user', 'admin'):
        account(e, sys.argv[2] if len(sys.argv) > 2 else '', action == 'admin')
    elif action == 'backup':
        backup(e)
    elif action == 'restore':
        restore(e, sys.argv[2] if len(sys.argv) > 2 else '')
    elif action in ('status', 'doctor'):
        doctor(e)
    elif action == 'monitor':
        monitor(e)
    elif action == 'prune':
        # Only dangling layers older than a week, never volumes or tagged rollback images.
        subprocess.run(['docker', 'image', 'prune', '-f', '--filter', 'until=168h'], check=True)
    else:
        raise ValueError('Unknown command')


if __name__ == '__main__':
    try:
        (ROOT / '.state').mkdir(exist_ok=True)
        (ROOT / '.state').chmod(0o700)
        with (ROOT / '.state/operation.lock').open('a') as lock:
            readonly = len(sys.argv) > 1 and (sys.argv[1] in ('status', 'doctor', 'config') or
                       sys.argv[1] == 'compose' and len(sys.argv) > 2 and sys.argv[2] in ('logs', 'ps', 'config', 'images'))
            if not readonly:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ValueError('Another deployment/backup/renewal is running')
            main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f'Error: {error}', file=sys.stderr)
        sys.exit(1)
