"""Validated runtime configuration. Standard library; never evaluate .env as shell."""
import ipaddress
import json
import re
import string
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRETS = ('POSTGRES_PASSWORD', 'REGISTRATION_SECRET', 'MACAROON_SECRET', 'FORM_SECRET',
           'LIVEKIT_API_KEY', 'LIVEKIT_API_SECRET', 'TURN_SECRET', 'RTC_AS_TOKEN', 'RTC_HS_TOKEN')


def read_env(path):
    result = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep or not re.fullmatch(r'[A-Z][A-Z0-9_]*', key):
            raise ValueError('Use KEY=value in .env, without export or shell substitutions')
        result[key] = value.strip()
    return result


def load():
    return read_env(ROOT / '.env') | read_env(ROOT / 'versions.env')


def validate(e):
    for key in ('MATRIX_DOMAIN', 'ELEMENT_DOMAIN', 'RTC_DOMAIN'):
        value = e.get(key, '')
        if len(value) > 253 or not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+', value):
            raise ValueError(f'{key}: use a lowercase DNS domain without scheme')
        if value.endswith('.example.com'):
            raise ValueError(f'Set your real {key} first')
    if len({e[k] for k in ('MATRIX_DOMAIN', 'ELEMENT_DOMAIN', 'RTC_DOMAIN')}) != 3:
        raise ValueError('Use three different domains')
    if not ipaddress.IPv4Address(e.get('PUBLIC_IPV4', '')).is_global:
        raise ValueError('PUBLIC_IPV4 must be a public IPv4')
    if e.get('TURN_RELAY_IPV4'):
        ipaddress.IPv4Address(e['TURN_RELAY_IPV4'])
    for key in SECRETS:
        if not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', e.get(key, '')):
            raise ValueError(f'{key}: run make init or use 32–128 URL-safe characters')
    if not re.fullmatch(r'ghcr\.io/[a-z0-9_.-]+/[a-z0-9_./-]+', e.get('IMAGE_PREFIX', '')) or 'owner/repository' in e['IMAGE_PREFIX']:
        raise ValueError('Set IMAGE_PREFIX=ghcr.io/owner/repository, in lowercase')
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}', e.get('IMAGE_TAG', '')):
        raise ValueError('Invalid IMAGE_TAG')
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,40}', e.get('COMPOSE_PROJECT_NAME', '')):
        raise ValueError('Invalid COMPOSE_PROJECT_NAME')
    if e.get('FEDERATION_ENABLED') not in ('true', 'false'):
        raise ValueError('FEDERATION_ENABLED must be true or false')
    for k, low, high in [('MAX_UPLOAD_MB', 1, 200), ('MIN_FREE_DISK_GB', 1, 100)]:
        if not e.get(k, '').isdigit() or not low <= int(e[k]) <= high:
            raise ValueError(f'{k} must be {low}..{high}')
    ports = ('HTTP_PORT', 'HTTPS_PORT', 'LIVEKIT_TCP_PORT', 'LIVEKIT_UDP_START', 'LIVEKIT_UDP_END',
             'TURN_PORT', 'TURN_TLS_PORT', 'TURN_RELAY_MIN', 'TURN_RELAY_MAX')
    for k in ports:
        if not e.get(k, '').isdigit() or not 1 <= int(e[k]) <= 65535:
            raise ValueError(f'Invalid {k}')
    udp_start, udp_end = int(e['LIVEKIT_UDP_START']), int(e['LIVEKIT_UDP_END'])
    relay_start, relay_end = int(e['TURN_RELAY_MIN']), int(e['TURN_RELAY_MAX'])
    if not 0 <= udp_end - udp_start <= 15 or not 99 <= relay_end - relay_start <= 999:
        raise ValueError('Use 1–16 LiveKit UDP ports and 100–1000 TURN relay ports')
    if max(udp_start, relay_start) <= min(udp_end, relay_end):
        raise ValueError('LiveKit and TURN UDP ranges overlap')
    single = [int(e[k]) for k in ('HTTP_PORT', 'HTTPS_PORT', 'LIVEKIT_TCP_PORT', 'TURN_PORT', 'TURN_TLS_PORT')]
    if len(single) != len(set(single)) or any(udp_start <= p <= udp_end or relay_start <= p <= relay_end for p in single):
        raise ValueError('Service ports and media ranges must not overlap')


def write(path, content, mode=0o644):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(mode)


def nginx(e, bootstrap=False):
    m, c, r = (e[k] for k in ('MATRIX_DOMAIN', 'ELEMENT_DOMAIN', 'RTC_DOMAIN'))
    conf = f'''server {{
    listen 80 default_server;
    server_name {m} {c} {r};
    location = /healthz {{ return 200 'ok'; }}
    location ^~ /.well-known/acme-challenge/ {{ root /var/www/acme; }}
    location / {{ {'return 503;' if bootstrap else 'return 301 https://$host$request_uri;'} }}
}}
'''
    if bootstrap:
        return conf
    tls = '''    listen 443 ssl;
    ssl_certificate /etc/letsencrypt/live/matrix/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/matrix/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:TLS:2m;
    add_header Strict-Transport-Security "max-age=31536000" always;
    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy strict-origin-when-cross-origin always;
'''
    client = json.dumps({'m.homeserver': {'base_url': f'https://{m}'}})
    server = json.dumps({'m.server': f'{m}:443'})
    fed = '' if e['FEDERATION_ENABLED'] == 'true' else 'location ^~ /_matrix/federation/ { return 403; }\n    location ^~ /_matrix/key/ { return 403; }'
    proxy = '''        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 180s;
        proxy_send_timeout 180s;
        proxy_buffering off;
'''
    conf += f'''server {{
{tls}    server_name {m};
    client_max_body_size {e['MAX_UPLOAD_MB']}m;
    location = /.well-known/matrix/client {{
        default_type application/json;
        add_header Access-Control-Allow-Origin "*" always;
        return 200 '{client}';
    }}
    location = /.well-known/matrix/server {{ default_type application/json; return 200 '{server}'; }}
    location = /_matrix/federation/v1/openid/userinfo {{
        set $synapse http://synapse:8008;
        proxy_pass $synapse;
{proxy}    }}
    {fed}
    location ^~ /_synapse/admin/ {{ return 404; }}
    location ~ ^/_matrix/(media/(r0|v1|v3)/upload|client/(v1|unstable/org.matrix.msc3916)/media/upload)$ {{
        if (-f /etc/nginx/conf.d/.uploads-blocked) {{ return 507; }}
        set $synapse http://synapse:8008;
        proxy_pass $synapse;
        proxy_request_buffering off;
{proxy}    }}
    location /_matrix/ {{
        set $synapse http://synapse:8008;
        proxy_pass $synapse;
        proxy_request_buffering off;
{proxy}    }}
    location /_synapse/client/ {{
        set $synapse http://synapse:8008;
        proxy_pass $synapse;
{proxy}    }}
    location / {{ return 404; }}
}}
server {{
{tls}    server_name {c};
    root /usr/share/nginx/html;
    add_header X-Frame-Options SAMEORIGIN always;
    location = /config.json {{ add_header Cache-Control "no-store"; }}
    location = /index.html {{ add_header Cache-Control "no-cache"; }}
    location / {{ try_files $uri $uri/ /index.html; }}
}}
server {{
{tls}    server_name {r};
    location = /livekit/sfu {{ return 308 /livekit/sfu/; }}
    location ^~ /livekit/sfu/ {{
        # Set the upstream before rewrite: break skips subsequent set directives.
        set $sfu http://livekit:7880;
        rewrite ^/livekit/sfu/(.*)$ /$1 break;
        proxy_pass $sfu;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
{proxy}    }}
    # Authorization is proxied by Synapse (MSC4512), never exposed directly.
    location / {{ return 404; }}
}}
'''
    return conf


def configure(e, bootstrap=False):
    validate(e)
    marker = ROOT / '.state/server-name'
    if marker.exists() and marker.read_text().strip() != e['MATRIX_DOMAIN']:
        raise ValueError('Cannot change MATRIX_DOMAIN for an initialized server')
    (ROOT / 'runtime').mkdir(exist_ok=True)
    (ROOT / 'runtime').chmod(0o700)
    for d in ('synapse', 'livekit', 'matrixrtc', 'nginx', 'coturn'):
        (ROOT / 'runtime' / d).mkdir(exist_ok=True)
        (ROOT / 'runtime' / d).chmod(0o755)
    (ROOT / 'backups').mkdir(exist_ok=True)
    (ROOT / 'backups').chmod(0o700)
    v = e | {'LISTENER_RESOURCES': '[client, openid, federation]' if e['FEDERATION_ENABLED'] == 'true' else '[client, openid]',
             'FEDERATION_CONFIG': '' if e['FEDERATION_ENABLED'] == 'true' else 'federation_domain_whitelist: []\nsend_federation: false\ntrusted_key_servers: []',
             'TURN_RELAY': e.get('TURN_RELAY_IPV4') or e['PUBLIC_IPV4']}
    for folder, filename in (('synapse', 'homeserver.yaml'), ('livekit', 'livekit.yaml'), ('coturn', 'turnserver.conf')):
        template = (ROOT / 'config' / (filename + '.template')).read_text()
        write(ROOT / 'runtime' / folder / filename, string.Template(template).substitute(v))
    registration = {
        'id': 'matrixrtc', 'as_token': e['RTC_AS_TOKEN'], 'hs_token': e['RTC_HS_TOKEN'],
        'sender_localpart': '_matrixrtc', 'url': None,
        'namespaces': {'users': [{'exclusive': False, 'regex': '.*'}], 'aliases': [], 'rooms': []},
        # Exact keys accepted by Synapse 1.162.0; upstream auth README still shows draft stable names.
        'io.element.msc4502.scopes': ['urn:matrix:client:io.element.msc4502:rooms:is_joined'],
        'io.element.msc4512.proxy_prefix': 'rtc/livekit',
        'io.element.msc4512.proxy_url': 'http://matrixrtc:8080',
    }
    content = json.dumps(registration, indent=2) + '\n'  # JSON is valid YAML.
    write(ROOT / 'runtime/synapse/matrixrtc-registration.yaml', content)
    write(ROOT / 'runtime/matrixrtc/registration.yaml', content)
    write(ROOT / 'runtime/matrixrtc/livekit-key', e['LIVEKIT_API_KEY'] + '\n')
    write(ROOT / 'runtime/matrixrtc/livekit-secret', e['LIVEKIT_API_SECRET'] + '\n')
    write(ROOT / 'runtime/nginx/default.conf', nginx(e, bootstrap))
    write(ROOT / 'runtime/synapse/log.config', (ROOT / 'config/synapse-log.config').read_text())
    element = {
        'default_server_config': {'m.homeserver': {'base_url': f'https://{e["MATRIX_DOMAIN"]}', 'server_name': e['MATRIX_DOMAIN']}},
        'disable_custom_urls': True, 'disable_guests': True, 'brand': 'Element',
        'default_theme': 'dark', 'show_labs_settings': False,
        'features': {'feature_group_calls': True, 'feature_element_call_video_rooms': True},
        'element_call': {'disable': False, 'use_exclusively': True},
        'default_federate': e['FEDERATION_ENABLED'] == 'true',
        'room_directory': {'servers': [e['MATRIX_DOMAIN']]},
        'integrations_ui_url': '', 'integrations_rest_url': '', 'integrations_widgets_urls': [],
    }
    write(ROOT / 'runtime/element.json', json.dumps(element, indent=2) + '\n')
