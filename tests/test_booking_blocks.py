import contextlib
import http.client
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import booking_blocks as blocks
import run


class BookingBlocksTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(run, 'DB', Path(self.tmp.name) / 'test.sqlite')
        self.db_patch.start()
        self.c = run.con()
        self.c.execute("INSERT INTO customers(id,match_key,name,phone,email,created_at,updated_at) VALUES(1,'one','対象顧客','090-1234-5678','Guest@Example.com','now','now')")
        self.c.execute("INSERT INTO customers(id,match_key,name,phone,email,created_at,updated_at) VALUES(2,'two','対象顧客','080-9999-9999','other@example.com','now','now')")
        self.c.commit()

    def tearDown(self):
        self.c.close()
        self.db_patch.stop()
        self.tmp.cleanup()

    def block(self):
        with self.c:
            return blocks.set_state(self.c, 1, {'active': True, 'reason': '無断キャンセル確認', 'version': 0})

    def test_default_is_allowed_and_migration_is_idempotent(self):
        blocks.schema(self.c)
        blocks.ensure_allowed(self.c, '09012345678', 'guest@example.com', 1)
        self.assertFalse(blocks.state(self.c, 1)['active'])

    def test_normalized_phone_and_email_block_even_with_changed_name(self):
        self.block()
        for number in ['09012345678', '+81 90 1234 5678', '００８１ ９０ １２３４ ５６７８']:
            with self.assertRaises(blocks.BookingBlocked):
                blocks.ensure_allowed(self.c, number)
        with self.assertRaises(blocks.BookingBlocked):
            blocks.ensure_allowed(self.c, email=' GUEST@EXAMPLE.COM ')

    def test_same_name_is_not_a_match(self):
        self.block()
        blocks.ensure_allowed(self.c, '08099999999', 'other@example.com', 2)
        blocks.ensure_allowed(self.c)

    def test_customer_id_blocks_without_contact(self):
        self.block()
        with self.assertRaises(blocks.BookingBlocked):
            blocks.ensure_allowed(self.c, customer_id=1)

    def test_release_restores_booking_and_keeps_audit(self):
        self.block()
        with self.c:
            state = blocks.set_state(self.c, 1, {'active': False, 'reason': '朝倉確認済み', 'version': 1})
        blocks.ensure_allowed(self.c, '09012345678', 'guest@example.com', 1)
        self.assertEqual([h['active'] for h in state['history']], [0, 1])
        self.assertEqual(state['version'], 2)

    def test_reason_and_version_are_required(self):
        for data in [None, {}, {'active': True, 'reason': '', 'version': 0}, {'active': '1', 'reason': '理由', 'version': 0}, {'active': True, 'reason': '理由', 'version': True}]:
            with self.assertRaises(ValueError):
                blocks.set_state(self.c, 1, data)
        self.block()
        with self.assertRaisesRegex(ValueError, '更新'):
            blocks.set_state(self.c, 1, {'active': False, 'reason': '理由', 'version': 0})
        self.assertTrue(blocks.state(self.c, 1)['active'])

    def test_existing_reservations_and_history_are_preserved(self):
        self.c.execute("INSERT INTO reservations(source,guest_name,phone,email,visit_at,party_size,course_name,amount,status,customer_id,created_at,updated_at) VALUES('DIRECT','対象顧客','08011112222','old@example.com','2026-12-10T18:00',2,'コース',132000,'CONFIRMED',1,'now','now')")
        self.c.commit()
        self.block()
        with self.assertRaises(blocks.BookingBlocked):
            blocks.ensure_allowed(self.c, '08011112222')
        self.assertEqual(self.c.execute('SELECT status FROM reservations').fetchone()[0], 'CONFIRMED')

    def test_profile_edits_do_not_erase_restricted_contacts(self):
        self.block()
        with self.c:
            blocks.retain_contacts(self.c, self.c.execute('SELECT * FROM customers WHERE id=1').fetchone())
            self.c.execute("UPDATE customers SET phone='08011112222',email='new@example.com' WHERE id=1")
        with self.c:
            blocks.retain_contacts(self.c, self.c.execute('SELECT * FROM customers WHERE id=1').fetchone())
            self.c.execute("UPDATE customers SET phone='',email='' WHERE id=1")
        for phone in ['09012345678', '08011112222']:
            with self.assertRaises(blocks.BookingBlocked):
                blocks.ensure_allowed(self.c, phone)

    def test_flags_and_persistence(self):
        self.block()
        rows = blocks.annotate(self.c, [{'id': 1}, {'id': 2}])
        self.assertEqual([r['booking_blocked'] for r in rows], [True, False])
        other = run.con()
        try:
            self.assertTrue(blocks.state(other, 1)['active'])
        finally:
            other.close()

    def test_square_import_does_not_create_local_booking(self):
        self.block()
        with patch.object(run, 'square', return_value={'customer': {'given_name': '別名', 'phone_number': '+819012345678', 'email_address': 'guest@example.com'}}):
            run.import_booking({'data': {'object': {'booking': {'id': 'external', 'customer_id': 'square-customer', 'start_at': '2026-12-10T18:00'}}}})
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM reservations').fetchone()[0], 0)

    def test_line_staff_booking_rejected_before_customer_or_invoice(self):
        import line_reservations
        self.block()
        data = dict(action='add', reservation_id=None, target_date='', guest_name='別名',
                    visit_at='2099-11-10T18:00', party_size=2, phone='+81 90 1234 5678',
                    seating_area='PRIVATE', note='', uncertain=[])
        with self.assertRaises(blocks.BookingBlocked):
            line_reservations.perform(run, self.c, data)
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM reservations').fetchone()[0], 0)
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM customers').fetchone()[0], 2)

    def test_concierge_linked_customer_cannot_bypass_by_changing_contacts(self):
        import concierge
        import concierge_booking
        import hashlib
        import time
        self.block()
        token = 'a' * 43
        c = concierge.connect(run.DB)
        with c:
            concierge.put(c, 'enabled', '1')
            c.execute("INSERT INTO concierge_customers(user_id,customer_id) VALUES('blocked-user',1)")
            slot = json.dumps({'course': 'matsuba-seko', 'date': '2099-11-10', 'time': '18:00', 'area': 'PRIVATE', 'party_size': 2})
            c.execute('INSERT INTO concierge_proposals(hash,user_id,slot,expires) VALUES(?,?,?,?)',
                      (hashlib.sha256(token.encode()).hexdigest(), 'blocked-user', slot, time.time() + 3600))
        c.close()
        with patch('vip_access.authorize_browser'), patch.object(run, 'public_slot_allowed', return_value=True), patch.object(run, 'availability_check', return_value=(True, 'OK')), patch.object(run, 'deliver_confirmation') as confirmation:
            with self.assertRaises(blocks.BookingBlocked):
                concierge_booking.booking(run, token, {'guest_name': '別名', 'phone': '08099999999', 'email': 'other@example.com', 'cancellation_policy_accepted': True})
            confirmation.assert_not_called()
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM reservations').fetchone()[0], 0)

    def test_http_public_staff_auth_and_no_invoice_side_effects(self):
        self.block()
        patches = [patch.object(run, 'ADMIN_TOKEN', 'test-admin'), patch.object(run, 'APP_BASE_URL', 'http://localhost'),
                   patch.object(run, 'SQUARE_TOKEN', 'test'), patch.object(run, 'SQUARE_LOCATION_ID', 'test'),
                   patch.object(run, 'expire_public_reservations'), patch.object(run, 'public_slot_allowed', return_value=True),
                   patch.object(run, 'staff_slot_allowed', return_value=True), patch.object(run, 'bookable_course', return_value=('コース', 66000)),
                   patch.object(run, 'make_invoice', side_effect=AssertionError('must not invoice blocked guest'))]
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            server = run.ThreadingHTTPServer(('127.0.0.1', 0), run.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            def request(path, body, headers=None):
                conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
                conn.request('POST', path, json.dumps(body), {'Content-Type': 'application/json', **(headers or {})})
                res = conn.getresponse(); result = (res.status, json.loads(res.read())); conn.close()
                return result
            try:
                request_body = {'guest_name': '別名', 'email': 'guest@example.com', 'phone': '09012345678', 'date': '2026-12-10', 'time': '18:00', 'seating_area': 'PRIVATE1', 'party_size': 2, 'request_id': 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'cancellation_policy_accepted': True}
                code, body = request('/api/public/reservations', request_body)
                self.assertEqual(code, 403)
                self.assertEqual(body['error'], blocks.PUBLIC_MESSAGE)
                self.assertNotIn('無断', json.dumps(body, ensure_ascii=False))
                staff = {**request_body, 'visit_at': '2026-12-10T18:00', 'customer_id': 1}
                for endpoint in ['/api/reservations/direct', '/api/reservations/phone']:
                    code, _ = request(endpoint, staff, {'x-admin-token': 'test-admin'})
                    self.assertEqual(code, 400)
                change = {'active': False, 'reason': '解除確認', 'version': 1}
                self.assertEqual(request('/api/customers/1/booking-block', change)[0], 401)
                self.assertEqual(request('/api/customers/1/booking-block', change, {'x-admin-token': 'test-admin'})[0], 403)
                self.assertTrue(blocks.state(self.c, 1)['active'])
                code, _ = request('/api/customers/1/booking-block', change, {'x-admin-token': 'test-admin', 'Origin': 'http://localhost', 'X-Tsukiya-Action': 'booking-block'})
                self.assertEqual(code, 200)
                self.assertEqual(self.c.execute('SELECT COUNT(*) FROM reservations').fetchone()[0], 0)
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
