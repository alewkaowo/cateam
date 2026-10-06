import io
import json
import sys
import unittest
from pathlib import Path
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from probe import describe_http_error


class ProbeDiagnosticsTests(unittest.TestCase):
    def test_http_failure_reports_endpoint_without_credentials(self):
        body = json.dumps({'errcode': 'M_UNKNOWN', 'error': 'Bearer private-token'}).encode()
        error = HTTPError('https://matrix.test/api?access_token=private-token', 500, 'failure', {}, io.BytesIO(body))
        detail = describe_http_error(error, 'POST', 'matrix.test', '/api?access_token=private-token')
        self.assertEqual(detail, 'POST https://matrix.test/api: HTTP 500 (M_UNKNOWN)')
        self.assertNotIn('private-token', detail)

    def test_untrusted_error_bodies_are_not_logged(self):
        for body in (b'<html>private-token</html>', b'{"errcode":"M_UNKNOWN private-token"}', b'[]'):
            with self.subTest(body=body):
                error = HTTPError('https://matrix.test/api', 500, 'failure', {}, io.BytesIO(body))
                detail = describe_http_error(error, 'GET', 'matrix.test', '/api')
                self.assertIn('HTTP 500', detail)
                self.assertNotIn('private-token', detail)


if __name__ == '__main__':
    unittest.main()
