import hashlib
import hmac
import http.client
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import pyotp
import run
import staff_mfa


class StaffMFATests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "staff-test.sqlite"
        self.origin = "https://reservation.test"
        self.password = "fixture password unique to this test"
        self.secret = pyotp.random_base32()
        salt = bytes.fromhex("ef" * 16)
        hashed = salt.hex() + ":" + staff_mfa.password_hash(self.password, salt).hex()
        self.enterContext(patch.dict(os.environ, {"STAFF_MFA_ENABLED": "1", "STAFF_MFA_ACCOUNTS": json.dumps({
            "owner": {"password_hash": hashed, "totp_secret": self.secret}
        })}))
        self.enterContext(patch.multiple(run, DB=self.path, ADMIN_TOKEN="test-secret", APP_BASE_URL=self.origin))
        run.LOGIN_FAILURES.clear()
        self.addCleanup(run.LOGIN_FAILURES.clear)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), run.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, payload=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        conn.request(method, path, json.dumps(payload) if payload else None, headers or {})
        response = conn.getresponse()
        result = response.status, response.headers, response.read()
        conn.close()
        return result

    def login(self, *, code=None, password=None, origin=None):
        return self.request("POST", "/api/login", {
            "username": "owner", "password": password or self.password,
            "code": code if code is not None else pyotp.TOTP(self.secret).now(),
        }, {"Content-Type": "application/json", "Origin": origin or self.origin})

    def test_valid_authenticator_and_replay(self):
        code = pyotp.TOTP(self.secret).now()
        status, headers, _ = self.login(code=code)
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"]
        self.assertIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertEqual(self.request("GET", "/login", headers={"Cookie": cookie.split(";")[0]})[0], 303)
        self.assertEqual(self.login(code=code)[0], 401)

    def test_requires_both_factors_and_valid_origin(self):
        self.assertEqual(self.login(password=run.STAFF_LOGIN_PIN)[0], 401)
        self.assertEqual(self.login(code="")[0], 401)
        self.assertEqual(self.login(origin="https://untrusted.test")[0], 403)
        self.assertEqual(self.login()[0], 200)

    def test_old_cookie_invalidated_and_login_form_available(self):
        expiry = str(int(time.time()) + 600)
        signature = hmac.new(run.ADMIN_TOKEN.encode(), expiry.encode(), hashlib.sha256).hexdigest()
        status, _, page = self.request("GET", "/login", headers={"Cookie": f"{run.SESSION_COOKIE}={expiry}.{signature}"})
        self.assertEqual(status, 200)
        self.assertIn(b'<fieldset id="staff-mfa">', page)

    def test_bad_configuration_fails_closed(self):
        with patch.dict(os.environ, {"STAFF_MFA_ACCOUNTS": ""}):
            self.assertEqual(self.login()[0], 503)
            self.assertEqual(self.login(password=run.STAFF_LOGIN_PIN)[0], 503)
