"""Exercise real CI certificate generation without Docker or host modifications."""
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import manage
import render


@unittest.skipUnless(shutil.which('openssl'), 'OpenSSL required')
class CITLSTests(unittest.TestCase):
    def test_ca_is_separate_from_server_certificate_and_private_key(self):
        real_run = subprocess.run
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('.env.example', 'versions.env'):
                shutil.copyfile(ROOT / name, root / name)
            (root / 'runtime').mkdir()
            def run(args, **kwargs):
                if args[0] == 'openssl':
                    return real_run(args, **kwargs)
                self.assertEqual(args[0], 'sudo')
            with patch.object(render, 'ROOT', root), patch.object(manage, 'compose') as compose, \
                    patch.object(manage, 'initialize'), patch.object(manage, 'copy_turn_tls'), \
                    patch.object(render, 'configure'), patch.dict(os.environ, CI_IMAGE_PREFIX='ghcr.io/test/matrix'), \
                    patch.object(subprocess, 'run', side_effect=run):
                runpy.run_path(str(ROOT / 'scripts/ci_setup.py'), run_name='__main__')
            cert = root / 'runtime/test-ca'
            leaf = real_run(['openssl', 'x509', '-in', str(cert / 'fullchain.pem'), '-text', '-noout'],
                            check=True, capture_output=True, text=True).stdout
            self.assertIn('CA:FALSE', leaf)
            self.assertIn('TLS Web Server Authentication', leaf)
            for hostname in ('matrix.ci.test', 'chat.ci.test', 'rtc.ci.test'):
                real_run(['openssl', 'verify', '-purpose', 'sslserver', '-verify_hostname', hostname,
                          '-CAfile', str(cert / 'ca.pem'), str(cert / 'fullchain.pem')],
                         check=True, capture_output=True)
            copy_command = compose.call_args.args[-1]
            self.assertNotIn('*.pem', copy_command)
            self.assertNotIn('ca-key.pem', copy_command)
            self.assertIn('test-ca/ca.pem:/etc/ssl/certs/', (root / 'runtime/ci-compose.yaml').read_text())


if __name__ == '__main__':
    unittest.main()
