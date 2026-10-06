#!/usr/bin/env python3
"""Public readiness; optional disposable chat/RTC authorization acceptance checks."""
import base64
import getpass
import hashlib
import hmac
import json
import os
import re
from pathlib import Path
import secrets
import ssl
import sys
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from render import ROOT, load


def describe_http_error(error, method, host, path):
    """Keep endpoint/status and a protocol code; never log body text or queries."""
    detail = 'non-JSON response'
    try:
        body = json.loads(error.read(8192))
        code = body.get('errcode') if isinstance(body, dict) else None
        detail = code if isinstance(code, str) and re.fullmatch(r'M_[A-Z0-9_]{1,80}', code) else 'JSON response'
    except (ValueError, OSError):
        pass
    return f'{method} https://{host}{urlsplit(path).path}: HTTP {error.code} ({detail})'


def main():
    e = load()
    ca = os.environ.get('PROBE_CA_FILE')
    context = ssl.create_default_context(cafile=ca)

    def request(host, path, method='GET', data=None, token=None, raw=False, extra=None):
        headers = dict(extra or {})
        if token:
            headers['Authorization'] = 'Bearer ' + token
        if isinstance(data, dict):
            headers['Content-Type'] = 'application/json'
            data = json.dumps(data).encode()
        elif data is not None:
            headers['Content-Type'] = 'application/octet-stream'
        url = f'https://{host}' + path
        print(f'Probe: {method} https://{host}{urlsplit(path).path}', flush=True)
        try:
            with urlopen(Request(url, data=data, method=method, headers=headers), context=context, timeout=30) as response:
                body = response.read()
                return body if raw else json.loads(body)
        except HTTPError as error:
            error.probe_detail = describe_http_error(error, method, host, path)
            raise

    def api(path, *args, **kwargs):
        return request(e['MATRIX_DOMAIN'], path, *args, **kwargs)

    assert api('/_matrix/client/versions')['versions']
    assert api('/.well-known/matrix/client')['m.homeserver']['base_url'] == f'https://{e["MATRIX_DOMAIN"]}'
    element = request(e['ELEMENT_DOMAIN'], '/config.json')
    assert element['element_call']['use_exclusively']
    assert b'<html' in request(e['ELEMENT_DOMAIN'], '/', raw=True).lower()
    assert request(e['RTC_DOMAIN'], '/livekit/sfu/', raw=True).strip() == b'OK'
    for path, status in [('/_synapse/admin/v1/register', 404), ('/_matrix/federation/v1/version', 403)]:
        if status == 403 and e['FEDERATION_ENABLED'] == 'true':
            continue
        try:
            api(path)
            raise AssertionError(f'{path} must be blocked')
        except HTTPError as err:
            assert err.code == status
    print('HTTPS, Matrix Client API, discovery, Element, SFU readiness, admin/federation isolation: OK')

    statefile = ROOT / 'runtime/ci-state.json'
    if '--persistence' in sys.argv:
        state = json.loads(statefile.read_text())
        event = api(state['event_path'], token=state['token'])
        assert event['content']['body'] == 'Infrastructure acceptance message'
        assert api(state['media_path'], token=state['token'], raw=True) == b'matrix file roundtrip\n'
        print('Stored message, token and uploaded file survive restart: OK')
        return
    if '--rtc' not in sys.argv and '--integration' not in sys.argv:
        return

    if '--integration' in sys.argv:
        # Create three disposable users only in a throwaway CI deployment.
        from manage import compose
        from unittest.mock import patch
        import manage
        password = secrets.token_urlsafe(24)
        names = ['ci_' + secrets.token_hex(6) for _ in range(3)]
        for name in names:
            with patch.object(manage.getpass, 'getpass', return_value=password):
                manage.account(e, name)
    else:
        print('This test creates a private test room and a small file in your account.')
        names = [input('Existing Matrix username: ').strip()]
        password = getpass.getpass('Password: ')
    logins = [api('/_matrix/client/v3/login', 'POST', {
        'type': 'm.login.password', 'identifier': {'type': 'm.id.user', 'user': name}, 'password': password}) for name in names]
    owner = logins[0]['access_token']
    invites = [login['user_id'] for login in logins[1:2]]
    room = api('/_matrix/client/v3/createRoom', 'POST', {'preset': 'private_chat', 'invite': invites,
              'name': 'Infrastructure acceptance test'}, owner)['room_id']
    rp = quote(room, safe='')
    token = owner
    if len(logins) > 1:
        token = logins[1]['access_token']
        api('/_matrix/client/v3/join/' + rp, 'POST', {}, token)
    event = api(f'/_matrix/client/v3/rooms/{rp}/send/m.room.message/check1', 'PUT',
                {'msgtype': 'm.text', 'body': 'Infrastructure acceptance message'}, owner)['event_id']
    for attempt in range(20):
        sync = api('/_matrix/client/v3/sync?timeout=0', token=token)
        events = sync.get('rooms', {}).get('join', {}).get(room, {}).get('timeline', {}).get('events', [])
        if any(ev['event_id'] == event for ev in events):
            break
        time.sleep(0.5)
    else:
        raise AssertionError('Message missing from sync')
    content = b'matrix file roundtrip\n'
    uri = api('/_matrix/media/v3/upload?filename=acceptance.txt', 'POST', content, owner)['content_uri']
    server, mid = uri.removeprefix('mxc://').split('/', 1)
    mediapath = '/_matrix/client/v1/media/download/' + quote(server, safe='') + '/' + quote(mid, safe='')
    assert api(mediapath, token=token, raw=True) == content
    transports = api('/_matrix/client/unstable/org.matrix.msc4143/rtc/transports', token=owner)['rtc_transports']
    assert any(t.get('url') == f'wss://{e["RTC_DOMAIN"]}/livekit/sfu' for t in transports)
    payload = {'server_name': e['MATRIX_DOMAIN'], 'url': f'wss://{e["RTC_DOMAIN"]}/livekit/sfu',
               'room_id': room, 'slot_id': 'm.call',
               'member': {'id': 'acceptance', 'claimed_device_id': logins[0]['device_id']}}
    path = '/_matrix/client/unstable/io.element.msc4195/rtc/livekit/get_token'
    jwt = api(path, 'POST', payload, owner)['jwt']
    parts = jwt.split('.')
    expected = hmac.new(e['LIVEKIT_API_SECRET'].encode(), (parts[0] + '.' + parts[1]).encode(), hashlib.sha256).digest()
    signature = base64.urlsafe_b64decode(parts[2] + '=' * (-len(parts[2]) % 4))
    assert hmac.compare_digest(expected, signature)
    claims = json.loads(base64.urlsafe_b64decode(parts[1] + '=' * (-len(parts[1]) % 4)))
    assert claims['iss'] == e['LIVEKIT_API_KEY'] and claims['video']['roomJoin']
    if len(logins) > 2:
        try:
            api(path, 'POST', payload, logins[2]['access_token'])
            raise AssertionError('Nonmember obtained RTC token')
        except HTTPError as err:
            assert err.code == 403
    # Verify an actual WebSocket upgrade; no media is published by this API test.
    key = base64.b64encode(secrets.token_bytes(16)).decode()
    import socket
    from urllib.parse import urlencode
    with socket.create_connection((e['RTC_DOMAIN'], 443), timeout=15) as connection:
        with context.wrap_socket(connection, server_hostname=e['RTC_DOMAIN']) as secure:
            query = urlencode({'access_token': jwt, 'auto_subscribe': '1', 'sdk': 'js', 'protocol': '15'})
            req = f'GET /livekit/sfu/rtc?{query} HTTP/1.1\r\nHost: {e["RTC_DOMAIN"]}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: {key}\r\n\r\n'
            secure.sendall(req.encode())
            response = secure.recv(4096)
            assert response.split(b'\r\n', 1)[0].startswith(b'HTTP/1.1 101'), 'SFU WebSocket upgrade failed'
    print('Login, private room, message sync, file roundtrip, MSC4519 transport discovery, MSC4195 JWT signature/access control and SFU WebSocket: OK')
    print('Audio/video, screen sharing, E2EE and NAT media connectivity still require real client testing.')
    if '--integration' in sys.argv:
        statefile.write_text(json.dumps({'token': token, 'event_path': f'/_matrix/client/v3/rooms/{rp}/event/{quote(event, safe="")}', 'media_path': mediapath}))
    else:
        for login in logins:
            api('/_matrix/client/v3/logout', 'POST', {}, login['access_token'])


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Never print arbitrary exception messages, response bodies or URL queries.
        detail = getattr(error, 'probe_detail', None)
        print('Probe failed: ' + (detail or type(error).__name__), file=sys.stderr)
        sys.exit(1)
