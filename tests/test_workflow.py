import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import patch

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="tsukiya-test-import-"))
import run


class WorkflowTest(unittest.TestCase):
    def test_invoice_sms_records_submission_once_and_keeps_payment_status(self):
        rid = self.reservation(email="")
        c = run.con()
        c.execute("UPDATE reservations SET square_invoice_url=? WHERE id=?", ("https://example.com/pay", rid))
        c.commit()
        c.close()
        with patch.object(run, "TWILIO_ACCOUNT_SID", "account"), patch.object(run, "TWILIO_AUTH_TOKEN", "secret"), \
             patch.object(run, "TWILIO_FROM_NUMBER", "+1234567890"), \
             patch.object(run, "send_sms", return_value={"sid": "SM-test", "status": "queued"}) as sms:
            first = run.send_invoice_sms(self.get(rid))
            second = run.send_invoice_sms(self.get(rid))
            self.assertEqual(first["invoice_sms_status"], "QUEUED")
            self.assertEqual(second["status"], "INVOICED")
            self.assertEqual(sms.call_count, 1)
            self.assertTrue(second["invoice_sms_sent_at"])

    def test_invoice_sms_missing_settings_and_failure_are_visible(self):
        rid = self.reservation(email="")
        c = run.con()
        c.execute("UPDATE reservations SET square_invoice_url=? WHERE id=?", ("https://example.com/pay", rid))
        c.commit()
        c.close()
        with patch.object(run, "TWILIO_ACCOUNT_SID", ""):
            self.assertEqual(run.send_invoice_sms(self.get(rid))["invoice_sms_status"], "NOT_CONFIGURED")
        with patch.object(run, "TWILIO_ACCOUNT_SID", "account"), patch.object(run, "TWILIO_AUTH_TOKEN", "secret"), \
             patch.object(run, "TWILIO_FROM_NUMBER", "+1234567890"), \
             patch.object(run, "send_sms", side_effect=TimeoutError) as sms:
            failed = run.send_invoice_sms(self.get(rid))
            run.send_invoice_sms(self.get(rid))
            self.assertEqual(failed["invoice_sms_status"], "ERROR")
            self.assertEqual(failed["status"], "INVOICED")
            self.assertEqual(sms.call_count, 1)

    def test_one_yen_booking_requires_staff_and_ignores_client_amount(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), run.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        payload = {"date": "2026-11-10", "time": "18:00", "seating_area": "COUNTER",
                   "party_size": 2, "request_id": "12345678-1234-1234-1234-123456789abc",
                   "guest_name": "決済テスト", "email": "test@example.com", "phone": "09012345678",
                   "cancellation_policy_accepted": True, "amount": 999999}
        try:
            request = urllib.request.Request(base + "/api/test/reservations",
                data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 401)
            request.add_header("x-admin-token", "test-secret")
            with patch.object(run, "SQUARE_TOKEN", "mock"), patch.object(run, "SQUARE_LOCATION_ID", "mock"), \
                 patch.object(run, "make_invoice", return_value=("c", "o", "i", "https://example.com/pay")) as invoice:
                with urllib.request.urlopen(request) as response:
                    rid = json.load(response)["reservation_id"]
                self.assertEqual(invoice.call_args.args[0]["amount"], 1)
                self.assertEqual(self.get(rid)["amount"], 1)
            request = urllib.request.Request(base + "/book-test", headers={"x-admin-token": "test-secret"})
            with urllib.request.urlopen(request) as response:
                page = response.read().decode()
            self.assertIn("1予約 1円", page)
            self.assertIn("/api/test/reservations", page)
            self.assertNotIn("selected.party_size*60000", page)
        finally:
            server.shutdown()
            server.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = run.DB
        self.old_token = run.ADMIN_TOKEN
        run.DB = Path(self.tmp.name) / "reservations.sqlite"
        run.ADMIN_TOKEN = "test-secret"
        run.LOGIN_FAILURES.clear()
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

    def test_public_hold_expires_after_48_hours_only_if_unpaid(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat()
        recent = run.now_iso()
        c = run.con()
        ids = []
        for status, invoice, created in (("INVOICED", "unpaid", old),
                                         ("INVOICED", "paid", old),
                                         ("INVOICED", "unreachable", old),
                                         ("PENDING", None, old),
                                         ("INVOICED", "recent", recent)):
            ids.append(c.execute(
                "INSERT INTO reservations(source,guest_name,visit_at,party_size,amount,"
                "status,square_invoice_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                ("WEB", "テスト", "2026-11-10T18:00", 2, 120000,
                 status, invoice, created, created)
            ).lastrowid)
        c.commit()
        c.close()
        calls = []

        def square_mock(path, body=None):
            calls.append(path)
            if "unreachable" in path:
                raise RuntimeError("Square unavailable")
            if path.endswith("/cancel"):
                self.assertEqual(body, {"version": 3})
                return {"invoice": {"status": "CANCELED"}}
            return {"invoice": {"status": "PAID" if path.endswith("/paid") else "UNPAID",
                                "version": 3}}

        with patch.object(run, "square", side_effect=square_mock):
            self.assertEqual(run.expire_public_reservations(), 2)
        self.assertEqual([self.get(rid)["status"] for rid in ids],
                         ["CANCELLED", "INVOICED", "INVOICED", "CANCELLED", "INVOICED"])
        self.assertEqual(sum(path.endswith("/cancel") for path in calls), 1)

    def test_public_booking_uses_seasonal_price_and_holds_only_available_seats(self):
        today = datetime.now(timezone(timedelta(hours=9))).date()
        day = next(today + timedelta(days=i) for i in range(1, 365)
                   if run.public_slot_allowed(today + timedelta(days=i), "18:00"))
        server = ThreadingHTTPServer(("127.0.0.1", 0), run.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        old_token, old_location = run.SQUARE_TOKEN, run.SQUARE_LOCATION_ID
        run.SQUARE_TOKEN, run.SQUARE_LOCATION_ID = "test-token", "test-location"
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urllib.request.urlopen(base + "/book") as page:
                self.assertIn("ご予約", page.read().decode())
            with patch.object(run, "make_invoice", return_value=(
                "cust", "order", "invoice", "https://square.example/pay"
            )) as invoice:
                with urllib.request.urlopen(
                    base + f"/api/public/availability?start={day.isoformat()}&party_size=2"
                ) as response:
                    availability = json.load(response)
                self.assertTrue(availability["days"][0]["slots"]["COUNTER"]["18:00"])
                self.assertEqual(availability["price_per_person"], 60000)
                self.assertTrue(availability["days"][0]["slots"]["PRIVATE1"]["18:00"])
                self.assertFalse(availability["days"][0]["slots"]["PRIVATE3"]["18:00"])
                with urllib.request.urlopen(
                    base + f"/api/public/availability?start={day.isoformat()}&party_size=5"
                ) as response:
                    larger = json.load(response)
                self.assertFalse(larger["days"][0]["slots"]["PRIVATE1"]["18:00"])
                self.assertTrue(larger["days"][0]["slots"]["PRIVATE3"]["18:00"])

                body = {"date": day.isoformat(), "time": "18:00", "seating_area": "COUNTER",
                        "party_size": 2, "guest_name": "公開予約テスト", "phone": "09012345678",
                        "email": "public@example.com",
                        "amount": 1,
                        "request_id": "123e4567-e89b-12d3-a456-426614174000",
                        "cancellation_policy_accepted": True}
                without_consent = dict(body, cancellation_policy_accepted=False)
                with self.assertRaises(urllib.error.HTTPError) as err:
                    urllib.request.urlopen(urllib.request.Request(
                        base + "/api/public/reservations", data=json.dumps(without_consent).encode(),
                        headers={"Content-Type": "application/json"}
                    ))
                self.assertEqual(err.exception.code, 400)
                invalid = dict(body, seating_area="PRIVATE3")
                with self.assertRaises(urllib.error.HTTPError) as err:
                    urllib.request.urlopen(urllib.request.Request(
                        base + "/api/public/reservations", data=json.dumps(invalid).encode(),
                        headers={"Content-Type": "application/json"}
                    ))
                self.assertEqual(err.exception.code, 400)
                req = urllib.request.Request(
                    base + "/api/public/reservations", data=json.dumps(body).encode(),
                    headers={"Content-Type": "application/json"}
                )
                first = json.load(urllib.request.urlopen(req))
                second = json.load(urllib.request.urlopen(req))
                self.assertEqual(first, second)
                self.assertEqual(invoice.call_count, 1)
                row = self.get(first["reservation_id"])
                self.assertEqual((row["amount"], row["status"], row["source"]),
                                 (120000, "INVOICED", "WEB"))
                self.assertIsNotNone(row["cancellation_policy_accepted_at"])
                with urllib.request.urlopen(
                    base + f"/api/public/availability?start={day.isoformat()}&party_size=7"
                ) as response:
                    availability = json.load(response)
                self.assertFalse(availability["days"][0]["slots"]["COUNTER"]["18:00"])
                self.assertNotIn("public@example.com", json.dumps(availability))
                event = {"event_id": "public-paid", "type": "invoice.payment_made",
                         "data": {"object": {"invoice": {"id": "invoice", "status": "PAID"}}}}
                with patch.object(run, "square", return_value={"invoice": {"status": "PAID"}}), \
                     patch.object(run, "send_confirmation", return_value=(True, "")):
                    run.process_square_event(event, json.dumps(event).encode())
                self.assertEqual(self.get(first["reservation_id"])["status"], "CONFIRMED")
                self.assertEqual(self.get(first["reservation_id"])["payment_source"], "SQUARE")
        finally:
            run.SQUARE_TOKEN, run.SQUARE_LOCATION_ID = old_token, old_location
            server.shutdown()
            server.server_close()

    def test_partial_payment_does_not_confirm_and_paid_event_is_idempotent(self):
        rid = self.reservation()
        sent = []
        with patch.object(run, "square", return_value={"invoice": {"status": "PAID"}}), \
             patch.object(run, "send_confirmation", side_effect=lambda row: (sent.append(row["id"]) or True, "")):
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

    def test_bank_confirmation_is_not_overwritten_by_delayed_square_event(self):
        rid = self.reservation()
        c = run.con()
        c.execute("UPDATE reservations SET status='CONFIRMED', payment_source='BANK' WHERE id=?", (rid,))
        c.commit()
        c.close()
        event = {"event_id": "late", "type": "invoice.payment_made",
                 "data": {"object": {"invoice": {"id": "inv-test", "status": "PAID"}}}}
        with patch.object(run, "square") as lookup:
            run.process_square_event(event, json.dumps(event).encode())
        lookup.assert_not_called()
        self.assertEqual(self.get(rid)["payment_source"], "BANK")

    def test_staff_can_cancel_only_unpaid_invoice(self):
        rid = self.reservation()
        server = ThreadingHTTPServer(("127.0.0.1", 0), run.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/api/reservations/{rid}/cancel",
            data=b"{}", headers={"x-admin-token": "test-secret"}
        )
        try:
            with patch.object(run, "square", return_value={"invoice": {"status": "PAID"}}):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(req)
                self.assertEqual(error.exception.code, 409)
            self.assertEqual(self.get(rid)["status"], "INVOICED")
            with patch.object(run, "square", side_effect=[
                {"invoice": {"status": "UNPAID", "version": 2}},
                {"invoice": {"status": "CANCELED"}}
            ]) as square:
                self.assertEqual(urllib.request.urlopen(req).status, 200)
                self.assertEqual(square.call_args.args[0], "/v2/invoices/inv-test/cancel")
            self.assertEqual(self.get(rid)["status"], "CANCELLED")
        finally:
            server.shutdown()
            server.server_close()

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

    def test_square_unpaid_list_and_partial_payment_cannot_be_bank_confirmed(self):
        rid = self.reservation(email="")
        with patch.object(run, "square", return_value={"invoice": {"status": "PARTIALLY_PAID", "version": 2}}):
            server = ThreadingHTTPServer(("127.0.0.1", 0), run.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}"
                req = urllib.request.Request(url + "/api/unpaid-invoices",
                                             headers={"x-admin-token": "test-secret"})
                listed = json.load(urllib.request.urlopen(req))
                self.assertEqual(listed[0]["square_status"], "PARTIALLY_PAID")
                req = urllib.request.Request(
                    url + f"/api/reservations/{rid}/confirm-bank-payment",
                    data=json.dumps({"amount": 110000, "reference": "振込明細", "confirmed_by": "担当者"}).encode(),
                    headers={"x-admin-token": "test-secret", "Content-Type": "application/json"},
                )
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    urllib.request.urlopen(req)
                self.assertEqual(rejected.exception.code, 409)
                self.assertEqual(self.get(rid)["status"], "INVOICED")
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

    def test_staff_login_limits_repeated_failures(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), run.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/api/login"
            def attempt(password):
                req = urllib.request.Request(
                    url, data=json.dumps({"password": password}).encode(),
                    headers={"Content-Type": "application/json"},
                )
                try:
                    return urllib.request.urlopen(req).status
                except urllib.error.HTTPError as e:
                    return e.code
            for _ in range(run.LOGIN_MAX_FAILURES):
                self.assertEqual(attempt("wrong"), 401)
            self.assertEqual(attempt("7777"), 429)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
