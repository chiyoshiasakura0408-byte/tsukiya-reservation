import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
import calendar_auth as auth


class Handler:
    def __init__(self, authenticated=True):
        self.authenticated = authenticated
        self.headers = {'Origin': 'https://example.com', 'X-Tsukiya-Action': 'calendar-connect'}
    def auth(self):
        return self.authenticated
    def send_json(self, data, status=200, headers=None):
        self.data, self.status, self.response_headers = data, status, headers or {}
    def send_html(self, data, status=200, **kwargs):
        self.data, self.status = data, status
    def redirect(self, path):
        self.data, self.status = path, 303


class AuthTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'test.sqlite'
        self.env = patch.dict(os.environ, {'GOOGLE_CALENDAR_CLIENT_ID': 'test-client',
            'GOOGLE_CALENDAR_CLIENT_SECRET': 'test-secret',
            'GOOGLE_CALENDAR_ID': 'test@group.calendar.google.com'}, clear=True)
        self.env.start()
        auth.PENDING.clear()
    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()
    def start(self):
        h = Handler()
        auth.handle_post(h, '/api/calendar-sync/connect', 'https://example.com')
        query = parse_qs(urlparse(h.data['url']).query)
        h.headers['Cookie'] = h.response_headers['Set-Cookie'].split(';')[0]
        return h, query
    def test_admin_and_origin_required(self):
        for h in (Handler(False), Handler()):
            h.headers['Origin'] = 'https://attacker.example'
            auth.handle_post(h, '/api/calendar-sync/connect', 'https://example.com')
            self.assertIn(h.status, (401, 403))
        self.assertFalse(auth.PENDING)
    def test_offline_pkce_and_no_secret_in_browser(self):
        h, q = self.start()
        self.assertEqual(q['access_type'], ['offline'])
        self.assertEqual(q['code_challenge_method'], ['S256'])
        self.assertEqual(q['redirect_uri'], ['https://example.com/calendar-sync/callback'])
        self.assertNotIn('test-secret', h.data['url'])
    def test_wrong_browser_rejected_before_token_exchange(self):
        h, q = self.start()
        h.headers['Cookie'] = ''
        with patch('urllib.request.urlopen') as network:
            auth.handle_get(h, auth.CALLBACK, {'state':q['state'],'code':['test-code']}, self.db, 'https://example.com')
            network.assert_not_called()
        self.assertEqual(h.status,400)
    def test_wrong_account_never_saved_and_code_single_use(self):
        h, q = self.start()
        tokens = {'access_token':'test-access', 'refresh_token':'test-refresh', 'scope':' '.join(auth.SCOPES)}
        responses = [io.BytesIO(json.dumps(tokens).encode()), io.BytesIO(json.dumps({'email':'other@example.com','email_verified':True}).encode())]
        with patch('urllib.request.urlopen',side_effect=responses):
            auth.handle_get(h, auth.CALLBACK, {'state':q['state'],'code':['test-code']}, self.db, 'https://example.com')
        self.assertEqual(h.status,400)
        self.assertFalse(Path(auth.token_path(self.db)).exists())
        self.assertNotIn('test-code', h.data)
        self.assertNotIn(q['state'][0],auth.PENDING)
    def test_verified_owner_and_private_calendar_required(self):
        h, q = self.start()
        tokens = {'access_token':'test-access', 'refresh_token':'test-refresh', 'scope':' '.join(auth.SCOPES)}
        responses = [io.BytesIO(json.dumps(tokens).encode()), io.BytesIO(json.dumps({'email':auth.EXPECTED_EMAIL,'email_verified':True}).encode())]
        with patch('urllib.request.urlopen',side_effect=responses), patch('calendar_sync.Google.check_calendar') as check:
            auth.handle_get(h, auth.CALLBACK, {'state':q['state'],'code':['test-code']}, self.db, 'https://example.com')
            check.assert_called_once()
        self.assertEqual(h.status,200)
        self.assertEqual(auth.refresh_token(self.db),'test-refresh')
        self.assertEqual(Path(auth.token_path(self.db)).stat().st_mode & 0o777,0o600)
        self.assertNotIn('test-refresh',h.data)
    def test_missing_permissions_do_not_persist(self):
        h, q = self.start()
        responses = [io.BytesIO(json.dumps({'access_token':'test-access','refresh_token':'test-refresh','scope':'openid'}).encode()),io.BytesIO(json.dumps({'email':auth.EXPECTED_EMAIL,'email_verified':True}).encode())]
        with patch('urllib.request.urlopen',side_effect=responses):
            auth.handle_get(h, auth.CALLBACK, {'state':q['state'],'code':['test-code']}, self.db, 'https://example.com')
        self.assertEqual(h.status,400)
        self.assertFalse(Path(auth.token_path(self.db)).exists())


if __name__ == '__main__':
    unittest.main()
