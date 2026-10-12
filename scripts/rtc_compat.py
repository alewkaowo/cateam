"""OpenID compatibility for Element Call 0.26, with mandatory room membership."""
import json
import os
from pathlib import Path
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


def call(url, payload=None, token=None):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    data = json.dumps(payload).encode() if payload is not None else None
    with urlopen(Request(url, data=data, headers=headers), timeout=15) as response:
        return json.load(response)


class Denied(Exception):
    pass


def authorize(payload, domain, as_token):
    openid = payload.get('openid_token', {})
    room = payload.get('room_id') or payload.get('room')
    if not isinstance(openid, dict) or openid.get('matrix_server_name') != domain:
        raise Denied()
    token = openid.get('access_token')
    if not isinstance(token, str) or not token or not isinstance(room, str) or not room.startswith('!'):
        raise Denied()
    identity = call('http://synapse:8008/_matrix/federation/v1/openid/userinfo?' + urlencode({'access_token': token}))
    mxid = identity.get('sub')
    if not isinstance(mxid, str) or not mxid.startswith('@') or not mxid.endswith(':' + domain):
        raise Denied()
    member = payload.get('member', {})
    if not isinstance(member, dict) or member.get('claimed_user_id', mxid) != mxid:
        raise Denied()
    membership = call('http://synapse:8008/_matrix/client/unstable/io.element.msc4502/rooms/' +
                      quote(room, safe='') + '/is_joined?' + urlencode({'mxid': mxid}), token=as_token)
    if membership.get('joined') is not True:
        raise Denied()


class Handler(BaseHTTPRequestHandler):
    slots = threading.BoundedSemaphore(4)

    def log_message(self, *args):
        pass  # Never log OpenID tokens or request bodies.

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.reply(200 if self.path == '/health' else 404, {})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_POST(self):
        if self.path not in ('/sfu/get', '/get_token'):
            return self.reply(404, {'errcode': 'M_UNRECOGNIZED'})
        if not self.slots.acquire(blocking=False):
            return self.reply(429, {'errcode': 'M_LIMIT_EXCEEDED'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 16384 or self.headers.get('Transfer-Encoding'):
                return self.reply(400, {'errcode': 'M_BAD_JSON'})
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                return self.reply(400, {'errcode': 'M_BAD_JSON'})
            authorize(payload, self.server.domain, self.server.as_token)
            result = call('http://matrixrtc:8080' + self.path, payload)
            self.reply(200, result)
        except Denied:
            self.reply(403, {'errcode': 'M_FORBIDDEN', 'error': 'Room membership required'})
        except HTTPError as error:
            status = 403 if error.code in (401, 403, 404) else 502
            self.reply(status, {'errcode': 'M_FORBIDDEN' if status == 403 else 'M_UNKNOWN'})
        except (ValueError, TypeError):
            self.reply(400, {'errcode': 'M_BAD_JSON'})
        except Exception:
            self.reply(502, {'errcode': 'M_UNKNOWN'})
        finally:
            self.slots.release()


if __name__ == '__main__':
    server = ThreadingHTTPServer(('0.0.0.0', 8081), Handler)
    server.domain = os.environ['MATRIX_DOMAIN']
    server.as_token = Path('/config/as-token').read_text().strip()
    server.serve_forever()
