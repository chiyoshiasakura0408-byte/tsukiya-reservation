import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import patch

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="tsukiya-test-import-"))
import run


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = run.DB
        self.old_token = run.ADMIN_TOKEN
        run.DB = Path(self.tmp.name) / "reservations.sqlite"
        run.ADMIN_TOKEN = "test-secret"
        run.con().close()

    def tearDown(self):
        run.DB = self.old_db
        run.ADMIN_TOKEN = self.old_token
        self.tmp.cleanup()

    def reservation(self, invoice_id="inv-test", email="guest@example.com"):
        c = run.con()
        ts = run.now_iso()
        cur = c.execute(
            "INSERT INTO reservations(source,guest_name,phone,email,visit_at,party_size,"
            "course_name,amount,seating_area,counter_round,duration_minutes,status,"
            "square_invoice_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("PHONE", "ゲスト", "09012345678", email, "2026-10-01T18:00", 2,
             "松葉蟹おまかせコース", 110000, "COUNTER", 1, 150, "INVOICED",
             invoice_id, ts, ts),
        )
        c.commit()
        rid = cur.lastrowid
        c.close()
        return rid

    def get(self, rid):
        c = run.con()
        row = dict(c.execute("SELECT * FROM reservations WHERE id=?", (rid,)).fetchone())
        c.close()
        return row

    def test_partial_payment_does_not_confirm_and_paid_event_is_idempotent(self):
        rid = self.reservation()
        sent = []
        with patch.object(run, "send_confirmation", side_effect=lambda row: (sent.append(row["id"]) or True, "")):
            def event(eid, status):
                data = {"event_id": eid, "type": "invoice.payment_made",
                        "data": {"object": {"invoice": {"id": "inv-test", "status": status}}}}
                return run.process_square_event(data, json.dumps(data).encode())

            self.assertFalse(event("partial", "PARTIALLY_PAID"))
            self.assertEqual(self.get(rid)["status"], "INVOICED")
            self.assertFalse(event("full", "PAID"))
            self.assertEqual(self.get(rid)["status"], "CONFIRMED")
            self.assertEqual(self.get(rid)["payment_source"], "SQUARE")
            self.assertTrue(self.get(rid)["confirmation_sent_at"])
            self.assertTrue(event("full", "PAID"))
            self.assertEqual(sent, [rid])

    def test_bank_reconciliation_cancels_invoice_before_confirmation(self):
        rid = self.reservation(email="")
        calls = []

        def square(path, body=None):
            calls.append((path, body))
            if body is None:
                return {"invoice": {"status": "UNPAID", "version": 3}}
            return {"invoice": {"status": "CANCELED"}}

        with patch.object(run, "square", side_effect=square):
            server = ThreadingHTTPServer(("127.0.0.1", 0), run.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/api/reservations/{rid}/confirm-bank-payment"
                req = urllib.request.Request(
                    url,
                    data=json.dumps({"amount": 110000, "reference": "10/1 アサクラ",
                                     "confirmed_by": "担当者"}).encode(),
                    headers={"x-admin-token": "test-secret", "Content-Type": "application/json"},
                )
                self.assertEqual(urllib.request.urlopen(req).status, 200)
                self.assertEqual(self.get(rid)["payment_source"], "BANK")
                self.assertEqual(calls[-1][1], {"version": 3})
                with self.assertRaises(urllib.error.HTTPError) as second:
                    urllib.request.urlopen(req)
                self.assertEqual(second.exception.code, 409)
                c = run.con()
                self.assertEqual(c.execute("SELECT count(*) FROM payment_audit").fetchone()[0], 1)
                c.close()
            finally:
                server.shutdown()
                server.server_close()

    def test_failed_invoice_keeps_seat_held(self):
        rid = self.reservation()
        c = run.con()
        c.execute("UPDATE reservations SET status='ERROR' WHERE id=?", (rid,))
        c.commit()
        ok, message = run.availability_check(c, "COUNTER", "2026-10-01T18:00", 7, 1)
        c.close()
        self.assertFalse(ok)
        self.assertIn("残り6席", message)

    def test_staff_pin_logs_in_without_exposing_admin_token(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), run.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}"
            def login(password):
                return urllib.request.Request(
                    url + "/api/login", data=json.dumps({"password": password}).encode(),
                    headers={"Content-Type": "application/json"},
                )
            with self.assertRaises(urllib.error.HTTPError) as wrong:
                urllib.request.urlopen(login("test-secret"))
            self.assertEqual(wrong.exception.code, 401)
            response = urllib.request.urlopen(login("7777"))
            cookie = response.headers["Set-Cookie"].split(";", 1)[0]
            self.assertNotIn("test-secret", cookie)
            rows = urllib.request.Request(url + "/api/reservations", headers={"Cookie": cookie})
            self.assertEqual(urllib.request.urlopen(rows).status, 200)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
