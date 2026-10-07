import http.client
import threading
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer

import run


class SecurityBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.auth = patch.object(run, "ADMIN_TOKEN", "test-only-secret")
        self.auth.start()
        run.LOGIN_FAILURES.clear()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), run.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.auth.stop()
        run.LOGIN_FAILURES.clear()

    def request(self, headers, body=b"{}"):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        conn.putrequest("POST", "/api/login")
        for key, value in headers:
            conn.putheader(key, value)
        conn.endheaders(body)
        response = conn.getresponse()
        status, result_headers = response.status, response.headers
        response.read()
        conn.close()
        return status, result_headers

    def test_rejects_invalid_framing_before_body_read(self):
        for headers in [
            [("Content-Length", "-1")],
            [("Content-Length", "no")],
            [("Content-Length", "2"), ("Content-Length", "2")],
            [("Transfer-Encoding", "chunked")],
        ]:
            with self.subTest(headers=headers):
                self.assertEqual(self.request(headers)[0], 400)

    def test_rejects_oversized_login_without_waiting_for_payload(self):
        self.assertEqual(self.request([("Content-Length", "4097")], b"")[0], 413)

    def test_malformed_login_is_controlled_and_not_cacheable(self):
        for body in [b"[]", b"null", b"{", b'{"password":123}']:
            status, headers = self.request([("Content-Length", str(len(body)))], body)
            self.assertEqual(status, 400)
            self.assertIn("no-store", headers.get("Cache-Control"))
            self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")

    def test_unicode_password_is_rejected_and_counted(self):
        body = '{"password":"不正入力"}'.encode()
        status, _ = self.request([("Content-Length", str(len(body)))], body)
        self.assertEqual(status, 401)
        self.assertEqual(len(run.LOGIN_FAILURES["127.0.0.1"]), 1)


if __name__ == "__main__":
    unittest.main()
