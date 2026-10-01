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
    def test_seat_blocks_and_private_auto_assignment(self):
        day = "2026-11-10"
        server = ThreadingHTTPServer(("127.0.0.1", 0), run.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        headers = {"x-admin-token": "test-secret", "Content-Type": "application/json"}
        def post(path, data):
            return json.load(urllib.request.urlopen(urllib.request.Request(
                base + path, data=json.dumps(data).encode(), headers=headers)))
        try:
            block = post("/api/seat-blocks", {"date": day, "blocks": [
                {"time": "18:00", "seating_area": "COUNTER", "seat_number": 8},
                {"time": "18:00", "seating_area": "PRIVATE3", "seat_number": 0}]})
            self.assertEqual(block["created"], 2)
            c = run.con()
            self.assertFalse(run.availability_check(c, "COUNTER", day+"T18:00", 8, 1)[0])
            self.assertFalse(run.availability_check(c, "PRIVATE", day+"T18:00", 5)[0])
            self.assertTrue(run.availability_check(c, "PRIVATE", day+"T18:00", 4)[0])
            c.close()
            body = {"guest_name": "電話のお客様", "phone": "09012345678",
                    "visit_at": day+"T18:00", "party_size": 4,
                    "seating_area": "PRIVATE", "guest_note": "誕生日のお祝い",
                    "celebration_items": ["ホールケーキ"], "plate_message": "おめでとう"}
            first = post("/api/reservations/phone", body)
            self.assertEqual(self.get(first["id"])["seating_area"], "PRIVATE1")
            self.assertEqual(self.get(first["id"])["plate_message"], "おめでとう")
            second = post("/api/reservations/phone", body)
            self.assertEqual(self.get(second["id"])["seating_area"], "PRIVATE2")
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                post("/api/reservations/phone", body)
            self.assertEqual(rejected.exception.code, 409)
            listed = json.load(urllib.request.urlopen(urllib.request.Request(
                base + "/api/seat-blocks?date=" + day, headers=headers)))
            room_block = next(b for b in listed if b["seating_area"] == "PRIVATE3")
            self.assertEqual(post("/api/seat-blocks/remove", {"id": room_block["id"]})["removed"], 1)
            body["party_size"] = 5
            third = post("/api/reservations/phone", body)
            self.assertEqual(self.get(third["id"])["seating_area"], "PRIVATE3")
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                post("/api/seat-blocks", {"date": day, "blocks": [
                    {"time": "18:00", "seating_area": "PRIVATE3", "seat_number": 0}]})
            self.assertEqual(rejected.exception.code, 400)
        finally:
            server.shutdown(); server.server_close()

    def test_phone_booking_uses_server_course_and_price(self):
        day = "2026-11-10"
        server = ThreadingHTTPServer(("127.0.0.1", 0), run.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            body = {"guest_name": "電話のお客様", "phone": "09012345678",
                    "visit_at": day + "T18:00", "party_size": 3,
                    "seating_area": "COUNTER", "counter_round": 1,
                    "amount": 1, "course_name": "改ざんしたコース"}
            req = urllib.request.Request(base + "/api/reservations/phone",
                                         data=json.dumps(body).encode(),
                                         headers={"x-admin-token": "test-secret", "Content-Type": "application/json"})
            result = json.load(urllib.request.urlopen(req))
            row = self.get(result["id"])
            self.assertEqual((row["course_name"], row["amount"]),
                             (run.PUBLIC_COURSES["matsuba-seko"][0], 180000))
            body["visit_at"] = "2027-04-01T18:00"
            req = urllib.request.Request(base + "/api/reservations/phone",
                                         data=json.dumps(body).encode(),
                                         headers={"x-admin-token": "test-secret", "Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(req)
            self.assertEqual(error.exception.code, 400)
        finally:
            server.shutdown(); server.server_close()

    def test_unpaid_reminder_once_and_phone_expiry_after_48_hours(self):
        rid = self.reservation(email="")
        issued = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        c = run.con()
        c.execute("UPDATE reservations SET square_invoice_url=?,invoice_issued_at=? WHERE id=?",
                  ("https://squareup.com/pay-test", issued, rid))
        c.commit(); c.close()
        with patch.multiple(run, TWILIO_ACCOUNT_SID="account", TWILIO_AUTH_TOKEN="token",
                            TWILIO_FROM_NUMBER="+8112345678"), \
             patch.object(run, "send_sms", return_value={"sid": "SM-reminder", "status": "queued"}) as sms:
            self.assertFalse(run.send_payment_reminder(self.get(rid), "PAID"))
            self.assertTrue(run.send_payment_reminder(self.get(rid), "UNPAID"))
            self.assertFalse(run.send_payment_reminder(self.get(rid), "UNPAID"))
            self.assertEqual(sms.call_count, 1)
            self.assertIn("取り消し", sms.call_args.kwargs["body"])
        self.assertEqual(self.get(rid)["reminder_status"], "SENT")
        c = run.con()
        c.execute("UPDATE reservations SET invoice_issued_at=? WHERE id=?",
                  ((datetime.now(timezone.utc) - timedelta(hours=49)).isoformat(), rid))
        c.commit(); c.close()
        with patch.object(run, "square", side_effect=[{"invoice": {"status": "UNPAID", "version": 1}},
                                                      {"invoice": {"status": "CANCELED"}}]):
            self.assertEqual(run.expire_public_reservations(), 1)
        self.assertEqual(self.get(rid)["status"], "CANCELLED")

    def test_staff_new_booking_acknowledgement_is_persistent_and_scoped(self):
        first, second = self.reservation(), self.reservation()
        server = ThreadingHTTPServer(("127.0.0.1", 0), run.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        headers = {"x-admin-token": "test-secret", "Content-Type": "application/json"}
        try:
            request = urllib.request.Request(base + "/api/reservations", headers=headers)
            self.assertEqual(sum(not row["staff_seen_at"] for row in json.load(urllib.request.urlopen(request))), 2)
            request = urllib.request.Request(base + "/api/reservations/mark-read",
                                            data=json.dumps({"ids": [first]}).encode(), headers=headers)
            self.assertEqual(json.load(urllib.request.urlopen(request))["marked"], 1)
            self.assertIsNotNone(self.get(first)["staff_seen_at"])
            self.assertIsNone(self.get(second)["staff_seen_at"])
            self.assertEqual(json.load(urllib.request.urlopen(request))["marked"], 0)
        finally:
            server.shutdown(); server.server_close()

    def test_full_slot_suggests_nearby_seats_and_dates(self):
        day = (datetime.now(timezone(timedelta(hours=9))) + timedelta(days=10)).date().isoformat()
        c = run.con()
        ts = run.now_iso()
        c.execute("INSERT INTO reservations(source,guest_name,visit_at,party_size,amount,seating_area,counter_round,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                  ("PHONE", "満席", day + "T18:00", 8, 10000, "COUNTER", 1, "CONFIRMED", ts, ts))
        c.commit()
        try:
            candidates = run.nearby_availability(c, day + "T18:00", "COUNTER", 2)
            self.assertTrue(candidates)
            self.assertNotIn({"visit_at": day + "T18:00", "seating_area": "COUNTER", "counter_round": 1}, candidates)
            self.assertTrue(any(x["visit_at"].startswith(day) and x["seating_area"] != "COUNTER" for x in candidates))
        finally:
            c.close()

    def test_crab_preview_and_mobile_video_ranges(self):
        server=ThreadingHTTPServer(("127.0.0.1",0),run.Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        base=f"http://127.0.0.1:{server.server_port}"
        try:
            with urllib.request.urlopen(base+"/loading-test") as response:
                page=response.read().decode()
            self.assertIn("/assets/crab-3.mp4",page)
            self.assertIn("/crab-loader.js",page)
            with urllib.request.urlopen(base+"/book") as response:booking=response.read().decode()
            self.assertNotIn('<script src="/crab-loader.js">',booking)
            self.assertIn("data:image/jpeg;base64,",booking)
            self.assertIn("MutationObserver",booking)
            request=urllib.request.Request(base+"/assets/crab-1.mp4",headers={"Range":"bytes=0-31"})
            with urllib.request.urlopen(request) as response:
                self.assertEqual(response.status,206)
                self.assertEqual(len(response.read()),32)
                self.assertTrue(response.headers["Content-Range"].startswith("bytes 0-31/"))
            request=urllib.request.Request(base+"/assets/crab-1.mp4",headers={"Range":"bytes=999999999-"})
            with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(request)
            self.assertEqual(error.exception.code,416)
        finally:server.shutdown();server.server_close()

    def test_reconcile_recovers_missing_payment_event(self):
        rid=self.reservation(email="")
        with patch.object(run,"square",return_value={"invoice":{"id":"inv-test","status":"PAID"}}), patch.object(run,"send_confirmation_sms",side_effect=lambda r:r):
            run.reconcile_reservations()
            run.reconcile_reservations()
        self.assertEqual(self.get(rid)["status"],"CONFIRMED")
        self.assertEqual(self.get(rid)["payment_source"],"SQUARE")

    def test_reconcile_does_not_confirm_when_square_is_unavailable(self):
        rid=self.reservation(email="")
        with patch.object(run,"square",side_effect=TimeoutError()):
            run.reconcile_reservations()
        self.assertEqual(self.get(rid)["status"],"INVOICED")

    def test_email_ambiguous_failure_is_not_sent_twice(self):
        rid=self.reservation()
        c=run.con();c.execute("UPDATE reservations SET status='CONFIRMED' WHERE id=?",(rid,));c.commit();c.close()
        with patch.multiple(run,SMTP_HOST="mock",SMTP_USER="mock",SMTP_PASS="mock",MAIL_FROM="test@example.com"),patch.object(run,"send_confirmation",return_value=(False,"timeout")) as mail:
            run.deliver_confirmation(self.get(rid));run.deliver_confirmation(self.get(rid))
            self.assertEqual(mail.call_count,1)
            self.assertEqual(self.get(rid)["confirmation_email_status"],"ERROR")

    def test_confirmation_sms_only_confirmed_and_once(self):
        rid = self.reservation(email="")
        with patch.object(run, "TWILIO_ACCOUNT_SID", "account"), patch.object(run, "TWILIO_AUTH_TOKEN", "secret"), patch.object(run, "TWILIO_FROM_NUMBER", "TSUKIYA"), patch.object(run, "send_sms", return_value={"sid":"SM-confirm","status":"queued"}) as sms:
            run.send_confirmation_sms(self.get(rid))
            sms.assert_not_called()
            c=run.con(); c.execute("UPDATE reservations SET status='CONFIRMED' WHERE id=?", (rid,)); c.commit(); c.close()
            run.send_confirmation_sms(self.get(rid))
            result=run.send_confirmation_sms(self.get(rid))
            self.assertEqual(sms.call_count,1)
            self.assertEqual(result["confirmation_sms_status"],"QUEUED")
            self.assertIn("ご予約を確定",sms.call_args.kwargs["body"])

    def test_confirmation_sms_failure_preserves_confirmation_and_no_duplicate(self):
        rid=self.reservation(email="")
        c=run.con(); c.execute("UPDATE reservations SET status='CONFIRMED' WHERE id=?",(rid,)); c.commit(); c.close()
        with patch.object(run,"TWILIO_ACCOUNT_SID","account"), patch.object(run,"TWILIO_AUTH_TOKEN","secret"), patch.object(run,"TWILIO_FROM_NUMBER","TSUKIYA"), patch.object(run,"send_sms",side_effect=TimeoutError()) as sms:
            run.send_confirmation_sms(self.get(rid))
            result=run.send_confirmation_sms(self.get(rid))
            self.assertEqual(sms.call_count,1)
            self.assertEqual(result["status"],"CONFIRMED")
            self.assertEqual(result["confirmation_sms_status"],"ERROR")

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

    def test_customer_profiles_link_bookings_and_keep_visit_notes_scoped(self):
        first = self.reservation()
        second = self.reservation()
        c = run.con()
        ts = run.now_iso()
        other = c.execute("""INSERT INTO reservations(source,guest_name,phone,email,visit_at,party_size,
            amount,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            ("PHONE", "別のお客様", "09012345678", "", "2026-11-10T18:00", 2, 120000,
             "CONFIRMED", ts, ts)).lastrowid
        past = (datetime.now(timezone(timedelta(hours=9))) - timedelta(days=1)).strftime("%Y-%m-%dT18:00")
        c.execute("UPDATE reservations SET status='CONFIRMED',visit_at=? WHERE id=?", (past, first))
        c.commit(); c.close()
        server = ThreadingHTTPServer(("127.0.0.1", 0), run.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        headers = {"x-admin-token": "test-secret", "Content-Type": "application/json"}
        def get(path):
            return json.load(urllib.request.urlopen(urllib.request.Request(base + path, headers=headers)))
        def post(path, body):
            req = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers=headers)
            return json.load(urllib.request.urlopen(req))
        try:
            listing = get("/api/customers")
            self.assertEqual(len(listing), 2)
            customer_id = self.get(first)["customer_id"]
            self.assertEqual(next(x for x in listing if x["id"] == customer_id)["visit_count"], 1)
            self.assertEqual(customer_id, self.get(second)["customer_id"])
            self.assertNotEqual(customer_id, self.get(other)["customer_id"])
            self.assertEqual(len(get(f"/api/customers/{customer_id}")["visits"]), 2)
            post(f"/api/customers/{customer_id}", {"company_name": "株式会社つきや", "receipt_name": "つきや"})
            post(f"/api/customers/{customer_id}/visits/{first}",
                 {"visit_note": "蟹みそを好む", "companions": "佐藤様"})
            profile = get(f"/api/customers/{customer_id}")
            self.assertEqual(profile["customer"]["company_name"], "株式会社つきや")
            self.assertEqual(next(v for v in profile["visits"] if v["id"] == first)["companions"], "佐藤様")
            self.assertFalse(next(v for v in profile["visits"] if v["id"] == second)["visit_note"])
            with self.assertRaises(urllib.error.HTTPError) as error:
                post(f"/api/customers/{customer_id}/visits/{other}", {"visit_note": "不正な編集"})
            self.assertEqual(error.exception.code, 404)
        finally:
            server.shutdown(); server.server_close()

    def test_two_matsuba_courses_keep_distinct_periods_and_names(self):
        from datetime import date
        self.assertTrue(run.public_slot_allowed(date(2026, 11, 10), "18:00", "matsuba-seko"))
        self.assertTrue(run.public_slot_allowed(date(2026, 12, 31), "18:00", "matsuba-seko"))
        self.assertFalse(run.public_slot_allowed(date(2027, 1, 1), "18:00", "matsuba-seko"))
        self.assertTrue(run.public_slot_allowed(date(2027, 1, 1), "18:00", "matsuba-fukahire"))
        self.assertTrue(run.public_slot_allowed(date(2027, 3, 20), "18:00", "matsuba-fukahire"))
        self.assertFalse(run.public_slot_allowed(date(2026, 12, 31), "18:00", "matsuba-fukahire"))
        self.assertFalse(run.public_slot_allowed(date(2027, 3, 21), "18:00", "matsuba-fukahire"))
        self.assertEqual(run.PUBLIC_COURSES["matsuba-seko"][1], 60000)
        self.assertEqual(run.PUBLIC_COURSES["matsuba-fukahire"][1], 60000)

    def test_english_booking_creates_english_invoice_content(self):
        rid = self.reservation()
        c = run.con()
        c.execute("UPDATE reservations SET booking_language='en' WHERE id=?", (rid,))
        c.commit()
        row = c.execute("SELECT * FROM reservations WHERE id=?", (rid,)).fetchone()
        c.close()
        requests = []
        def square_mock(path, body=None):
            requests.append((path, body))
            if path == "/v2/customers":
                return {"customer": {"id": "customer-test"}}
            if path == "/v2/orders":
                return {"order": {"id": "order-test"}}
            if path == "/v2/invoices":
                return {"invoice": {"id": "invoice-test", "version": 1}}
            return {"invoice": {"id": "invoice-test", "public_url": "https://square.example/pay"}}
        with patch.object(run, "SQUARE_LOCATION_ID", "location-test"), \
             patch.object(run, "SQUARE_EN_LOCATION_ID", "english-location"), \
             patch.object(run, "square", side_effect=square_mock):
            run.make_invoice(row)
        self.assertEqual(requests[1][1]["order"]["location_id"], "english-location")
        self.assertEqual(requests[2][1]["invoice"]["location_id"], "english-location")
        self.assertEqual(requests[1][1]["order"]["line_items"][0]["name"], "Crab omakase course")
        self.assertIn("Nishitenma Tsukiya", requests[2][1]["invoice"]["title"])
        self.assertIn("Full prepayment", requests[2][1]["invoice"]["description"])

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
                "status,square_invoice_id,created_at,updated_at,reminder_status) VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("WEB", "テスト", "2026-11-10T18:00", 2, 120000,
                 status, invoice, created, created, "SKIPPED_LEGACY" if invoice else None)
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
                        "guest_note": "花束の予算は1万円", "celebration_items": ["花束"],
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
                self.assertEqual((row["guest_note"], row["celebration_items"]),
                                 ("花束の予算は1万円", "花束"))
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
                     patch.multiple(run, SMTP_HOST="mock", SMTP_USER="mock", SMTP_PASS="mock", MAIL_FROM="test@example.com"), \
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
             patch.multiple(run, SMTP_HOST="mock", SMTP_USER="mock", SMTP_PASS="mock", MAIL_FROM="test@example.com"), \
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
