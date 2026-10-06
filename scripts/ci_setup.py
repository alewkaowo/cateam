#!/usr/bin/env python3
"""Only for disposable CI. Generate real random secrets and a test TLS CA."""
import os
import subprocess
from pathlib import Path
from manage import compose, copy_turn_tls, initialize
from render import ROOT, SECRETS, configure, read_env, write
import secrets

e = read_env(ROOT / '.env.example') | read_env(ROOT / 'versions.env')
e.update(MATRIX_DOMAIN='matrix.ci.test', ELEMENT_DOMAIN='chat.ci.test', RTC_DOMAIN='rtc.ci.test',
         PUBLIC_IPV4='8.8.8.8', TURN_RELAY_IPV4='127.0.0.1',
         IMAGE_PREFIX=os.environ['CI_IMAGE_PREFIX'], IMAGE_TAG='ci',
         COMPOSE_PROJECT_NAME='matrix-ci', MIN_FREE_DISK_GB='1',
         COMPOSE_FILE='compose.yaml:runtime/ci-compose.yaml')
for key in SECRETS:
    e[key] = secrets.token_hex(32)
write(ROOT / '.env', ''.join(f'{key}={value}\n' for key, value in e.items()), 0o600)
configure(e)
cert = ROOT / 'runtime/test-ca'
cert.mkdir()
subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '2',
                '-keyout', str(cert / 'privkey.pem'), '-out', str(cert / 'fullchain.pem'),
                '-subj', '/CN=Matrix CI', '-addext', 'basicConstraints=critical,CA:TRUE',
                '-addext', 'subjectAltName=DNS:matrix.ci.test,DNS:chat.ci.test,DNS:rtc.ci.test'],
               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
# Trust this test CA, rather than disabling TLS validation in the authorization service.
write(ROOT / 'runtime/ci-compose.yaml', '''services:
  matrixrtc:
    extra_hosts:
      - "rtc.ci.test:host-gateway"
    volumes:
      - ./runtime/test-ca/fullchain.pem:/etc/ssl/certs/ca-certificates.crt:ro
  livekit:
    extra_hosts:
      - "rtc.ci.test:host-gateway"
''')
compose('run', '--rm', '--no-deps', '-T', '-v', str(cert) + ':/test-ca:ro', 'tools',
        'mkdir -p /tls/live/matrix && cp /test-ca/*.pem /tls/live/matrix/ && chmod 600 /tls/live/matrix/privkey.pem')
initialize(e)
copy_turn_tls()
# Host probe uses verified TLS and the normal production URLs.
subprocess.run(['sudo', 'sh', '-c', 'printf "127.0.0.1 matrix.ci.test chat.ci.test rtc.ci.test\\n" >> /etc/hosts'], check=True)
print('Disposable CI prepared; advertised test IP is not a media acceptance test.')
