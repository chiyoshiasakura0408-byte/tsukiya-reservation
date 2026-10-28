import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('DATA_DIR', tempfile.mkdtemp())
import run


class RestoreBankTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = patch.object(run, 'DB', Path(self.tmp.name) / 'db')
        self.db.start()
        self.auth = patch.object(run, 'ADMIN_TOKEN', 'test-secret')
        self.auth.start()
        self.origin = patch.object(run, 'APP_BASE_URL', 'https://tsukiya.test')
        self.origin.start()
        c = run.con()
        self.visit = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d') + 'T18:00'
        c.execute("INSERT INTO reservations(id,source,guest_name,visit_at,party_size,amount,seating_area,status,payment_method,cancellation_reason,created_at,updated_at) VALUES(41,'WEB','Test',?,2,88000,'PRIVATE1','CANCELLED','BANK','MANUAL',?,?)", (self.visit, run.now_iso(), run.now_iso()))
        c.commit(); c.close()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), run.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.body = dict(paid=True, amount=88000, expected_visit_at=self.visit,
                         reference='Owner confirms transfer received', confirmed_by='Owner')

    def tearDown(self):
        self.server.shutdown(); self.server.server_close()
        self.origin.stop(); self.auth.stop(); self.db.stop(); self.tmp.cleanup()

    def call(self, data=None, auth=True):
        headers = {'Content-Type': 'application/json', 'Origin':'https://tsukiya.test', 'X-Tsukiya-Action':'restore-bank-payment'}
        if auth:
            headers['x-admin-token'] = 'test-secret'
        req = urllib.request.Request(f'http://127.0.0.1:{self.server.server_port}/api/reservations/41/restore-bank-payment', data=json.dumps(self.body if data is None else data).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def test_auth_and_validation_leave_booking_untouched(self):
        self.assertEqual(self.call(auth=False)[0], 401)
        for change in ({'paid':False}, {'amount':1}, {'reference':''}, {'confirmed_by':''}, {'expected_visit_at':'stale'}):
            self.assertEqual(self.call({**self.body, **change})[0], 409)
        c = run.con()
        self.assertEqual(c.execute('SELECT status FROM reservations').fetchone()[0], 'CANCELLED')
        self.assertEqual(c.execute('SELECT count(*) FROM payment_audit').fetchone()[0], 0)
        c.close()

    def test_restores_original_and_never_invoices_or_notifies(self):
        with patch.object(run, 'make_invoice') as invoice, patch.object(run, 'square') as square, patch.object(run, 'deliver_confirmation') as notification:
            self.assertEqual(self.call()[0], 200)
            self.assertEqual(self.call()[0], 409)
            invoice.assert_not_called(); square.assert_not_called(); notification.assert_not_called()
        c = run.con()
        r = c.execute('SELECT * FROM reservations').fetchone()
        self.assertEqual((r['id'], r['status'], r['payment_source'], r['amount']), (41, 'CONFIRMED', 'BANK', 88000))
        self.assertIsNotNone(r['payment_confirmed_at'])
        self.assertIsNone(r['cancellation_reason'])
        self.assertIsNone(r['square_invoice_id'])
        self.assertEqual(c.execute('SELECT count(*) FROM payment_audit').fetchone()[0], 1)
        self.assertIn('追加請求なし', r['guest_note'])
        c.close()

    def test_rejects_occupied_seat_and_existing_payment(self):
        c = run.con()
        c.execute("INSERT INTO reservations(source,guest_name,visit_at,party_size,amount,seating_area,status,created_at,updated_at) VALUES('DIRECT','Other',?,2,88000,'PRIVATE1','CONFIRMED',?,?)", (self.visit, run.now_iso(), run.now_iso()))
        c.commit(); c.close()
        self.assertEqual(self.call()[0], 409)
        c = run.con()
        c.execute('DELETE FROM reservations WHERE id<>41')
        c.execute("UPDATE reservations SET payment_source='BANK' WHERE id=41")
        c.commit(); c.close()
        self.assertEqual(self.call()[0], 409)

    def test_seat_blocks_remain_enforced(self):
        c = run.con()
        c.execute("INSERT INTO seat_blocks(visit_date,time_text,seating_area,seat_number,created_at) VALUES(?,'18:00','PRIVATE1',0,?)", (self.visit[:10],run.now_iso()))
        c.commit(); c.close()
        self.assertEqual(self.call()[0],409)


if __name__ == '__main__':
    unittest.main()
