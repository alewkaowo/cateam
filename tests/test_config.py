import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import render
import manage


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.e = render.read_env(ROOT / '.env.example') | render.read_env(ROOT / 'versions.env')
        self.e.update(PUBLIC_IPV4='8.8.8.8', IMAGE_PREFIX='ghcr.io/test/matrix')
        self.e.update({key: 'a' * 64 for key in render.SECRETS})

    def test_rejects_configuration_injection_and_conflicts(self):
        for key, value in [('MATRIX_DOMAIN', 'bad.com; return 200;'), ('POSTGRES_PASSWORD', 'pass@host'),
                           ('PUBLIC_IPV4', '127.0.0.1'), ('FEDERATION_ENABLED', 'yes'), ('MAX_UPLOAD_MB', '0'),
                           ('TURN_RELAY_MIN', '7882'), ('RTC_DOMAIN', self.e['MATRIX_DOMAIN']), ('IMAGE_TAG', 'tag with spaces')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                render.validate(self.e | {key: value})

    def test_secret_generation_is_idempotent_and_private(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ('.env.example', 'versions.env'):
                shutil.copyfile(ROOT / name, root / name)
            with patch.object(manage, 'ROOT', root):
                manage.init()
                first = (root / '.env').read_text()
                manage.init()
                self.assertEqual(first, (root / '.env').read_text())
                values = render.read_env(root / '.env')
                self.assertEqual(len({values[k] for k in render.SECRETS}), len(render.SECRETS))
                self.assertEqual((root / '.env').stat().st_mode & 0o777, 0o600)

    def test_rtc_wiring_and_federation_switch(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shutil.copytree(ROOT / 'config', root / 'config')
            with patch.object(render, 'ROOT', root):
                render.configure(self.e)
                synapse = (root / 'runtime/synapse/homeserver.yaml').read_text()
                self.assertNotIn('${', synapse)
                self.assertIn('federation_domain_whitelist: []', synapse)
                self.assertIn('msc4512_enabled: true', synapse)
                self.assertIn('url: "wss://rtc.cateam.online/livekit/sfu"', synapse)
                as_config = json.loads((root / 'runtime/synapse/matrixrtc-registration.yaml').read_text())
                self.assertEqual(as_config['io.element.msc4512.proxy_url'], 'http://matrixrtc:8080')
                self.assertIn('urn:matrix:client:io.element.msc4502:rooms:is_joined', as_config['io.element.msc4502.scopes'])
                self.assertIsNone(as_config['url'])
                render.configure(self.e | {'FEDERATION_ENABLED': 'true'})
                self.assertNotIn('federation_domain_whitelist: []', (root / 'runtime/synapse/homeserver.yaml').read_text())
                render.write(root / '.state/server-name', 'old.invalid\n')
                with self.assertRaises(ValueError):
                    render.configure(self.e)

    def test_bootstrap_and_public_api_isolation(self):
        bootstrap = render.nginx(self.e, True)
        self.assertNotIn('ssl_certificate', bootstrap)
        conf = render.nginx(self.e)
        self.assertIn('location ^~ /_synapse/admin/ { return 404; }', conf)
        self.assertNotIn('proxy_pass http://matrixrtc', conf)
        self.assertIn('Connection $connection_upgrade', conf)
        self.assertIn('proxy_request_buffering off', conf)

    def test_restore_rejects_archive_traversal(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'unsafe.tar.gz'
            with tarfile.open(path, 'w:gz') as archive:
                item = tarfile.TarInfo('synapse/../../escape')
                item.size = 1
                archive.addfile(item, io.BytesIO(b'x'))
            with self.assertRaises(ValueError):
                manage.safe_archive(path, {'synapse', 'tls'})

    def test_empty_compose_output_does_not_block_restore(self):
        for output in ('', '\n', '\r\n', '  \n'):
            with self.subTest(output=output), patch.object(manage, 'compose') as compose:
                compose.return_value.stdout = output
                self.assertEqual(manage.running(), [])
        with patch.object(manage, 'compose') as compose:
            compose.return_value.stdout = 'postgres\nsynapse\n'
            self.assertEqual(manage.running(), ['postgres', 'synapse'])

    def test_disk_guard_blocks_and_recovers(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(manage, 'ROOT', Path(folder)):
            with patch.object(manage, 'available_disk', side_effect=ValueError('Low disk')):
                with self.assertRaises(ValueError):
                    manage.monitor(self.e)
            marker = Path(folder) / 'runtime/nginx/.uploads-blocked'
            self.assertTrue(marker.exists())
            with patch.object(manage, 'available_disk'):
                manage.monitor(self.e)
            self.assertFalse(marker.exists())

    @unittest.skipUnless(shutil.which('docker'), 'Docker CLI is required for Compose validation')
    def test_compose_resources_and_named_volumes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'env'
            path.write_text(''.join(f'{k}={v}\n' for k, v in self.e.items()))
            result = subprocess.run(['docker', 'compose', '--env-file', str(path), 'config', '--format', 'json'],
                                    cwd=ROOT, capture_output=True, text=True, check=True)
            c = json.loads(result.stdout)
            services = c['services']
            for name in ('postgres', 'synapse', 'matrixrtc'):
                self.assertFalse(services[name].get('ports'))
            self.assertNotIn('build', services['nginx'])
            self.assertTrue(c['networks']['database']['internal'])
            self.assertTrue(c['networks']['backend']['internal'])
            self.assertEqual(services['coturn']['network_mode'], 'host')
            self.assertFalse(any(s.get('privileged') for s in services.values()))
            memory = sum(int(services[s]['mem_limit']) for s in manage.SERVICES)
            self.assertLess(memory, 4 * 1024**3)
            self.assertGreaterEqual(int(services['synapse']['mem_limit']), 1024**3)
            self.assertGreaterEqual(int(services['livekit']['mem_limit']), 1024**3)
            for name in ('postgres_data', 'synapse_data', 'tls_data', 'turn_tls'):
                self.assertIn(name, c['volumes'])


if __name__ == '__main__':
    unittest.main()
