"""Booking-note editing must not mutate the booking or send guest messages."""
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch
import test_workflow as workflow

run = workflow.run


class ReservationNotesTest(unittest.TestCase):
    def setUp(self):
        self.fixture = workflow.WorkflowTest()
        self.fixture.setUp()
        self.origin = patch.object(run, 'APP_BASE_URL', 'https://tsukiya.test')
        self.origin.start()
        self.addCleanup(self.origin.stop)
        self.rid = self.fixture.reservation()
        self.other = self.fixture.reservation('other-invoice')
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), run.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.headers = {'x-admin-token': 'test-secret', 'Origin': run.APP_BASE_URL.rstrip('/'),
                        'Content-Type': 'application/json', 'X-Tsukiya-Action': 'edit-reservation-note'}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.fixture.tearDown()

    def post(self, body, headers=None):
        req = urllib.request.Request(f'{self.base}/api/reservations/{self.rid}/note',
            data=json.dumps(body).encode(), headers=self.headers if headers is None else headers)
        try:
            with urllib.request.urlopen(req) as res:
                return res.status, json.load(res)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def test_edit_preserves_booking_and_other_guest_without_notifications(self):
        before = self.fixture.get(self.rid)
        other = self.fixture.get(self.other)
        with patch.object(run, 'deliver_confirmation') as delivery, \
             patch.object(run, 'send_confirmation') as email, \
             patch.object(run, 'send_confirmation_sms') as sms, \
             patch.object(run, 'make_invoice') as invoice:
            code, result = self.post({'guest_note': '17時45分来店。\n別邸・2名。', 'expected_guest_note': ''})
            self.assertEqual(code, 200)
            for sender in (delivery, email, sms, invoice):
                sender.assert_not_called()
        after = self.fixture.get(self.rid)
        self.assertEqual(after['guest_note'], result['guest_note'])
        for key in before:
            if key not in ('guest_note', 'updated_at'):
                self.assertEqual(after[key], before[key], key)
        self.assertEqual(self.fixture.get(self.other), other)
        c = run.con()
        try:
            audit = dict(c.execute('SELECT * FROM reservation_note_history').fetchone())
            self.assertEqual((audit['reservation_id'], audit['old_note'], audit['new_note']),
                             (self.rid, '', result['guest_note']))
        finally:
            c.close()

    def test_stale_note_rejected_and_retry_does_not_duplicate_history(self):
        self.assertEqual(self.post({'guest_note': 'original', 'expected_guest_note': ''})[0], 200)
        self.assertEqual(self.post({'guest_note': 'wrong', 'expected_guest_note': ''})[0], 409)
        self.assertEqual(self.fixture.get(self.rid)['guest_note'], 'original')
        self.assertEqual(self.post({'guest_note': 'original', 'expected_guest_note': 'original'})[0], 200)
        c = run.con()
        try:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM reservation_note_history').fetchone()[0], 1)
        finally:
            c.close()

    def test_can_clear_note(self):
        self.post({'guest_note': 'wrong guest', 'expected_guest_note': ''})
        self.assertEqual(self.post({'guest_note': '', 'expected_guest_note': 'wrong guest'})[0], 200)
        self.assertEqual(self.fixture.get(self.rid)['guest_note'], '')

    def test_invalid_input_has_no_effect(self):
        for body in ({'guest_note': 'x'}, {'guest_note': None, 'expected_guest_note': ''},
                     {'guest_note': 'x'*5001, 'expected_guest_note': ''},
                     {'guest_note': '\x00', 'expected_guest_note': ''}, [], 'invalid'):
            self.assertEqual(self.post(body)[0], 400)
        self.assertIsNone(self.fixture.get(self.rid)['guest_note'])

    def test_requires_auth_and_same_origin_explicit_action(self):
        body = {'guest_note': 'unauthorized', 'expected_guest_note': ''}
        for key, value, status in [('x-admin-token', '', 401), ('Origin', 'https://other.example', 403),
                                   ('Origin', '', 403), ('X-Tsukiya-Action', '', 403)]:
            headers = {**self.headers, key: value}
            self.assertEqual(self.post(body, headers)[0], status)
        self.assertIsNone(self.fixture.get(self.rid)['guest_note'])
