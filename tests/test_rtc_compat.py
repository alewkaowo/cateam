import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import rtc_compat


class CompatibilitySecurityTests(unittest.TestCase):
    def setUp(self):
        self.body = {'room': '!room:test.invalid', 'openid_token':
                     {'access_token': 'test-only', 'matrix_server_name': 'test.invalid'}}

    def test_foreign_homeserver_is_rejected_before_network_request(self):
        self.body['openid_token']['matrix_server_name'] = 'foreign.invalid'
        with patch.object(rtc_compat, 'call') as call:
            with self.assertRaises(rtc_compat.Denied):
                rtc_compat.authorize(self.body, 'test.invalid', 'as-test')
            call.assert_not_called()

    def test_openid_identity_does_not_grant_access_to_arbitrary_room(self):
        with patch.object(rtc_compat, 'call', side_effect=[{'sub': '@alice:test.invalid'}, {'joined': False}]):
            with self.assertRaises(rtc_compat.Denied):
                rtc_compat.authorize(self.body, 'test.invalid', 'as-test')

    def test_claimed_identity_cannot_impersonate_another_member(self):
        self.body['member'] = {'claimed_user_id': '@bob:test.invalid'}
        with patch.object(rtc_compat, 'call', return_value={'sub': '@alice:test.invalid'}) as call:
            with self.assertRaises(rtc_compat.Denied):
                rtc_compat.authorize(self.body, 'test.invalid', 'as-test')
            self.assertEqual(call.call_count, 1)

    def test_member_uses_scoped_appservice_token_for_membership(self):
        with patch.object(rtc_compat, 'call', side_effect=[{'sub': '@alice:test.invalid'}, {'joined': True}]) as call:
            rtc_compat.authorize(self.body, 'test.invalid', 'as-test')
            self.assertEqual(call.call_args.kwargs, {'token': 'as-test'})
            self.assertIn('mxid=%40alice%3Atest.invalid', call.call_args.args[0])
