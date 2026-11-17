import json
import os
import tempfile
import threading
import unittest
import urllib.request
import uuid
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import patch

os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='phone-invoice-test-'))
import run

class PhoneInvoiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = patch.multiple(run, DB=Path(self.tmp.name)/'test.sqlite', ADMIN_TOKEN='test-secret',
                                     TWILIO_ACCOUNT_SID='test', TWILIO_AUTH_TOKEN='test', TWILIO_FROM_NUMBER='+8111111111')
        self.config.start()
        run.con().close()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), run.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.body = dict(guest_name='Test guest', phone='09012345678', email='',
                         visit_at='2026-12-05T18:00', party_size=2, seating_area='PRIVATE1',
                         request_id=str(uuid.uuid4()))

    def tearDown(self):
        self.server.shutdown(); self.server.server_close()
        self.config.stop(); self.tmp.cleanup()

    def post(self, path='/api/reservations/phone', body=None, **headers):
        req = urllib.request.Request(f'http://127.0.0.1:{self.server.server_port}'+path,
                data=json.dumps(self.body if body is None else body).encode(),
                headers={'x-admin-token':'test-secret','Content-Type':'application/json',**headers})
        with urllib.request.urlopen(req) as response:
            return json.load(response)

    def test_phone_issues_and_sends_sms_once_for_repeated_request(self):
        with patch.object(run,'make_invoice',return_value=('c','o','i','https://example.com/invoice')) as invoice, \
             patch.object(run,'send_sms',return_value={'sid':'SM-test','status':'queued'}) as sms:
            first=self.post(); second=self.post()
            self.assertEqual(first['id'],second['id'])
            self.assertEqual(first['status'],'INVOICED')
            self.assertEqual(first['amount'],132000)
            self.assertEqual(first['invoice_sms_status'],'QUEUED')
            self.assertTrue(first['invoice_issued_at'])
            invoice.assert_called_once(); sms.assert_called_once()

    def test_email_uses_invoice_delivery_without_sms(self):
        self.body['email']='test@example.com'
        with patch.object(run,'make_invoice',return_value=('c','o','i','https://example.com/invoice')) as invoice, \
             patch.object(run,'send_sms') as sms:
            result=self.post()
            self.assertEqual(result['status'],'INVOICED')
            self.assertEqual(invoice.call_args.args[0]['email'],'test@example.com')
            sms.assert_not_called()

    def test_failure_preserves_booking_and_manual_retry_reuses_id(self):
        with patch.object(run,'make_invoice',side_effect=TimeoutError('timeout')) as invoice:
            first=self.post(); second=self.post()
            self.assertEqual(first['status'],'ERROR')
            self.assertEqual(first['id'],second['id'])
            invoice.assert_called_once()
        with patch.object(run,'make_invoice',return_value=('c','o','i','https://example.com/invoice')), \
             patch.object(run,'send_sms',return_value={'sid':'SM-test','status':'queued'}):
            result=self.post(f"/api/reservations/{first['id']}/send-invoice", {})
            self.assertEqual(result['id'],first['id'])
            self.assertEqual(result['status'],'INVOICED')

    def test_sms_failure_keeps_issued_invoice(self):
        with patch.object(run,'make_invoice',return_value=('c','o','i','https://example.com/invoice')) as invoice, \
             patch.object(run,'send_sms',side_effect=TimeoutError('timeout')) as sms:
            first=self.post(); second=self.post()
            self.assertEqual(first['status'],'INVOICED')
            self.assertEqual(first['invoice_sms_status'],'ERROR')
            self.assertEqual(first['id'],second['id'])
            invoice.assert_called_once(); sms.assert_called_once()

    def test_direct_booking_does_not_invoice(self):
        with patch.object(run,'make_invoice') as invoice, patch.object(run,'send_sms') as sms:
            result=self.post('/api/reservations/direct')
            self.assertEqual(result['status'],'CONFIRMED')
            invoice.assert_not_called(); sms.assert_not_called()

    def test_private3_split_tables_are_staff_only_and_exclude_the_whole_room(self):
        path = '/api/reservations/direct'
        approval = {'X-Tsukiya-Action': 'approve-private3-table'}
        first = {**self.body, 'visit_at': '2099-12-05T18:00', 'seating_area': 'PRIVATE3',
                 'approve_private3_table': True}
        second = {**first, 'guest_name': 'Second guest', 'party_size': 4, 'request_id': str(uuid.uuid4())}

        with self.assertRaises(urllib.error.HTTPError) as missing_approval:
            self.post(path, first)
        self.assertEqual(missing_approval.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as need_two_to_split:
            self.post(path, second, **approval)
        self.assertEqual(need_two_to_split.exception.code, 409)

        with patch.object(run, 'make_invoice') as invoice:
            a = self.post(path, first, **approval)
            b = self.post(path, second, **approval)
            repeated = self.post(path, first, **approval)
            invoice.assert_not_called()
        self.assertEqual((a['status'], a['private3_table'], a['id']), ('CONFIRMED', 1, repeated['id']))
        self.assertEqual((b['status'], b['private3_table'], b['amount']), ('CONFIRMED', 2, 264000))

        third = {**first, 'guest_name': 'Third guest', 'request_id': str(uuid.uuid4())}
        with self.assertRaises(urllib.error.HTTPError) as both_taken:
            self.post(path, third, **approval)
        self.assertEqual(both_taken.exception.code, 409)
        with run.con() as db:
            self.assertFalse(run.availability_check(db, 'PRIVATE3', first['visit_at'], 4)[0])
            self.assertTrue(run.availability_check(db, 'PRIVATE3', '2099-12-05T20:30', 4)[0])

    def test_move_paid_private_reservation_into_split_and_back(self):
        target = {**self.body, 'visit_at': '2099-12-05T18:00'}
        row = self.post('/api/reservations/direct', target)
        with run.con() as db:
            db.execute("UPDATE reservations SET source='WEB_CARD', payment_source='SQUARE', payment_confirmed_at='paid' WHERE id=?", (row['id'],))
        path = f"/api/reservations/{row['id']}/room"
        change = {'seating_area': 'PRIVATE3', 'expected_seating_area': 'PRIVATE1',
                  'expected_visit_at': row['visit_at'], 'expected_party_size': 2, 'approve_private3_table': True}
        with run.con() as db:
            db.execute("INSERT INTO seat_blocks(visit_date,time_text,seating_area,created_at) VALUES('2099-12-05','18:00','PRIVATE3','test')")
        with self.assertRaises(urllib.error.HTTPError):
            self.post(path, change, **{'X-Tsukiya-Action': 'change-time'})
        with run.con() as db:
            db.execute("DELETE FROM seat_blocks")
        self.assertEqual(self.post(path, change, **{'X-Tsukiya-Action': 'change-time'})['private3_table'], 1)
        with run.con() as db:
            saved = db.execute("SELECT * FROM reservations WHERE id=?", (row['id'],)).fetchone()
            self.assertEqual((saved['visit_at'], saved['party_size'], saved['amount'], saved['payment_source']),
                             (row['visit_at'], 2, 132000, 'SQUARE'))
        change.update(seating_area='PRIVATE1', expected_seating_area='PRIVATE3', approve_private3_table=False)
        change['expected_private3_table'] = 1
        self.assertIsNone(self.post(path, change, **{'X-Tsukiya-Action': 'change-time'})['private3_table'])

    def test_move_to_specific_private3_table_checks_entire_visit_and_current_assignment(self):
        row = self.post('/api/reservations/direct', {**self.body, 'visit_at': '2099-12-05T18:00'})
        full_room = self.post('/api/reservations/direct', {
            **self.body, 'guest_name': 'Full room', 'request_id': str(uuid.uuid4()),
            'visit_at': '2099-12-05T20:30', 'seating_area': 'PRIVATE3', 'party_size': 4
        })
        with run.con() as db:
            db.execute("UPDATE reservations SET visit_at='2099-12-05T19:00' WHERE id=?", (row['id'],))
        path = f"/api/reservations/{row['id']}/room"
        headers = {'X-Tsukiya-Action': 'change-time'}
        change = {'seating_area': 'PRIVATE3', 'expected_seating_area': 'PRIVATE1',
                  'expected_visit_at': '2099-12-05T19:00', 'expected_party_size': 2,
                  'approve_private3_table': True, 'private3_table': 2}
        with self.assertRaises(urllib.error.HTTPError) as occupied:
            self.post(path, change, **headers)
        self.assertIn('個室③全体に20:30〜23:00', json.load(occupied.exception)['error'])
        with run.con() as db:
            db.execute("UPDATE reservations SET status='CANCELLED' WHERE id=?", (full_room['id'],))
        table_a = self.post('/api/reservations/direct', {
            **self.body, 'guest_name': 'Table A', 'request_id': str(uuid.uuid4()),
            'visit_at': '2099-12-05T18:00', 'seating_area': 'PRIVATE3', 'approve_private3_table': True
        }, **{'X-Tsukiya-Action': 'approve-private3-table'})
        with self.assertRaises(urllib.error.HTTPError) as table_taken:
            self.post(path, {**change, 'private3_table': 1}, **headers)
        self.assertIn('テーブルAに18:00〜20:30', json.load(table_taken.exception)['error'])
        self.assertEqual(self.post(path, change, **headers)['private3_table'], 2)

        with run.con() as db:
            db.execute("UPDATE reservations SET status='CANCELLED' WHERE id=?", (table_a['id'],))
        change.update(expected_seating_area='PRIVATE3', expected_private3_table=2, private3_table=1)
        self.assertEqual(self.post(path, change, **headers)['private3_table'], 1)
        with self.assertRaises(urllib.error.HTTPError) as stale:
            self.post(path, {**change, 'private3_table': 2}, **headers)
        self.assertIn('予約が更新されています', json.load(stale.exception)['error'])
        change.update(expected_private3_table=1, private3_table=2)
        self.assertEqual(self.post(path, change, **headers)['private3_table'], 2)

    def test_swap_confirmed_private_bookings_checks_both_directions_before_saving(self):
        direct = '/api/reservations/direct'
        start = '2099-12-05T18:00'
        first = self.post(direct, {
            **self.body, 'visit_at': start, 'seating_area': 'PRIVATE3', 'approve_private3_table': True
        }, **{'X-Tsukiya-Action': 'approve-private3-table'})
        second = self.post(direct, {
            **self.body, 'visit_at': start, 'guest_name': 'Other booking',
            'request_id': str(uuid.uuid4()), 'party_size': 4
        })
        fields = ('seating_area', 'private3_table', 'visit_at', 'party_size', 'duration_minutes', 'vip_through')
        payload = {'other_id': second['id'], 'first': {field: first[field] for field in fields},
                   'second': {field: second[field] for field in fields}}
        url = f"/api/reservations/{first['id']}/room-swap"
        headers = {'X-Tsukiya-Action': 'change-time'}
        with run.con() as db:
            db.execute("UPDATE reservations SET source='WEB_CARD',payment_source='SQUARE' WHERE id=?", (second['id'],))
            db.execute("INSERT INTO seat_blocks(visit_date,time_text,seating_area,created_at) VALUES('2099-12-05','18:00','PRIVATE3','test')")
        with self.assertRaises(urllib.error.HTTPError) as blocked:
            self.post(url, payload, **headers)
        self.assertIn('ブロック', json.load(blocked.exception)['error'])
        with run.con() as db:
            saved = db.execute("SELECT seating_area,private3_table FROM reservations ORDER BY id").fetchall()
            self.assertEqual([tuple(row) for row in saved], [('PRIVATE3', 1), ('PRIVATE1', None)])
            db.execute("DELETE FROM seat_blocks")
        self.assertTrue(self.post(url, payload, **headers)['ok'])
        with run.con() as db:
            saved = db.execute("SELECT seating_area,private3_table,visit_at,party_size,amount,payment_source FROM reservations ORDER BY id").fetchall()
            self.assertEqual([tuple(row) for row in saved],
                             [('PRIVATE1', None, start, 2, 132000, None), ('PRIVATE3', 1, start, 4, 264000, 'SQUARE')])
        with self.assertRaises(urllib.error.HTTPError) as stale:
            self.post(url, payload, **headers)
        self.assertIn('予約が更新されています', json.load(stale.exception)['error'])

    def test_swap_preview_uses_free_table_for_overlapping_sittings(self):
        direct = '/api/reservations/direct'
        options = {**self.body, 'visit_at': '2099-12-05T18:00', 'seating_area': 'PRIVATE3'}
        early = self.post(direct, {**options, 'approve_private3_table': True},
                          **{'X-Tsukiya-Action': 'approve-private3-table'})
        late = self.post(direct, {**options, 'visit_at': '2099-12-05T20:30', 'guest_name': 'Late booking',
                                 'party_size': 4, 'request_id': str(uuid.uuid4())})
        middle = self.post(direct, {**options, 'visit_at': '2099-12-05T19:00', 'guest_name': 'Middle booking',
                                   'seating_area': 'PRIVATE1', 'special_end_time': '21:00', 'special_meal_count': 2,
                                   'request_id': str(uuid.uuid4())})
        fields = ('seating_area', 'private3_table', 'visit_at', 'party_size', 'duration_minutes', 'vip_through')
        url = f"/api/reservations/{late['id']}/room-swap"
        headers = {'X-Tsukiya-Action': 'change-time'}
        with run.con() as db:
            db.execute("UPDATE reservations SET party_size=2, amount=132000 WHERE id=?", (late['id'],))
        late['party_size'] = 2
        payload = {'other_id': middle['id'], 'first': {field: late[field] for field in fields},
                   'second': {field: middle[field] for field in fields}}
        with self.assertRaises(urllib.error.HTTPError):
            self.post(url, payload, **headers)
        preview = self.post(url, {**payload, 'preview': True}, **headers)
        destinations = [{'seating_area': 'PRIVATE1', 'private3_table': None},
                        {'seating_area': 'PRIVATE3', 'private3_table': 2}]
        self.assertEqual(preview['destinations'], destinations)
        with run.con() as db:
            seats = db.execute("SELECT seating_area,private3_table FROM reservations ORDER BY id").fetchall()
            self.assertEqual([tuple(row) for row in seats], [('PRIVATE3', 1), ('PRIVATE3', None), ('PRIVATE1', None)])
        self.assertTrue(self.post(url, {**payload, 'expected_destinations': destinations}, **headers)['ok'])
        with run.con() as db:
            seats = db.execute("SELECT id,seating_area,private3_table FROM reservations ORDER BY id").fetchall()
            self.assertEqual([tuple(row) for row in seats],
                             [(early['id'], 'PRIVATE3', 1), (late['id'], 'PRIVATE1', None), (middle['id'], 'PRIVATE3', 2)])

if __name__=='__main__': unittest.main()
