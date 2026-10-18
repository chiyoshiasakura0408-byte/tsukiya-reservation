import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import calendar_sync as sync


class Fake:
    calendar_id = 'test@group.calendar.google.com'
    def __init__(self):
        self.events = {}
        self.calls = []
        self.fail = False
    def sync(self, ident, body, namespace, rid):
        self.calls.append((ident, body))
        if self.fail:
            raise sync.SyncError('calendar_http_503')
        if body is None:
            self.events.pop(ident, None)
        else:
            self.events[ident] = copy.deepcopy(body)


class CalendarSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'test.sqlite'
        self.row = dict(id=1, guest_name='テスト', visit_at='2026-10-19T19:00:00', party_size=2,
            course_name='活たらば蟹', duration_minutes=150, vip_through=0, seating_area='PRIVATE1',
            source='DIRECT', status='CONFIRMED', payment_confirmed_at=None,
            guest_note='記念日 電話090-1234-5678 test@example.com', celebration_items='花束（小）', plate_message='Happy Birthday')
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE reservations (' + ','.join(k + (' INTEGER' if isinstance(v, int) else ' TEXT') for k,v in self.row.items()) + ')')
            db.execute('INSERT INTO reservations VALUES (' + ','.join('?' for _ in self.row) + ')', list(self.row.values()))
        self.client = Fake()
    def tearDown(self):
        self.tmp.cleanup()
    def tick(self, now=1000):
        return sync.reconcile(self.path, self.client, 'test', now)
    def update(self, **values):
        with sqlite3.connect(self.path) as db:
            db.execute('UPDATE reservations SET ' + ','.join(k+'=?' for k in values), list(values.values()))
    def test_initial_backfill_and_no_duplicates(self):
        self.assertEqual(self.tick()['synced'], 1)
        self.assertEqual(self.tick(1030)['unchanged'], 1)
        self.assertEqual(len(self.client.events), 1)
        self.assertEqual(len(self.client.calls), 1)
    def test_all_detail_changes_update_same_event(self):
        self.tick()
        self.update(visit_at='2026-10-20T20:30:00+09:00', party_size=4, seating_area='PRIVATE3', course_name='松葉蟹', payment_confirmed_at='2026-10-18')
        self.tick(1030)
        self.assertEqual(len(self.client.events), 1)
        body = next(iter(self.client.events.values()))
        self.assertIn('4名', body['summary'])
        self.assertIn('個室③', body['summary'])
        self.assertIn('決済済み', body['description'])
        self.assertIn('松葉蟹', body['description'])
    def test_cancel_deletes_only_projected_event(self):
        self.tick()
        self.update(status='CANCELLED')
        self.tick(1030)
        self.assertEqual(self.client.events, {})
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT status FROM reservations').fetchone()[0], 'CANCELLED')
    def test_direct_private_and_no_attendees(self):
        body = sync.event_body(self.row, 'test')
        self.assertEqual(body['attendees'], [])
        self.assertEqual(body['visibility'], 'private')
        self.assertIn('当日支払い', body['description'])
        self.assertNotIn('090-1234-5678', json.dumps(body))
        self.assertNotIn('test@example.com', json.dumps(body))
        self.assertIn('花束', body['description'])
    def test_vip_duration_and_october_arbitrary_start(self):
        row = dict(self.row, vip_through=1)
        body = sync.event_body(row, 'test')
        self.assertEqual(body['start']['dateTime'], '2026-10-19T19:00:00+09:00')
        self.assertEqual(body['end']['dateTime'], '2026-10-19T23:00:00+09:00')
    def test_failure_retries_and_persists(self):
        self.client.fail = True
        self.assertEqual(self.tick()['failed'], 1)
        self.tick(1010)
        self.assertEqual(len(self.client.calls), 1)
        self.client.fail = False
        self.assertEqual(self.tick(1031)['synced'], 1)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT attempts,last_error FROM calendar_sync_state').fetchone(), (0, None))
    def test_source_rows_untouched(self):
        with sqlite3.connect(self.path) as db:
            before = db.execute('SELECT * FROM reservations').fetchall()
        self.tick()
        with sqlite3.connect(self.path) as db:
            self.assertEqual(before, db.execute('SELECT * FROM reservations').fetchall())
    def test_primary_calendar_rejected(self):
        for ident in ('primary', 'chiyoshiasakura0408@gmail.com'):
            with self.assertRaises(sync.SyncError):
                sync.Google(ident)
    def test_google_notifications_and_ownership(self):
        client = sync.Google(self.client.calendar_id)
        calls = []
        def call(method, path='', body=None, missing_ok=False):
            calls.append((method, path, body))
            return None
        client.call = call
        body = sync.event_body(self.row, 'test')
        client.sync(sync.event_id(1,'test'), body, 'test', 1)
        self.assertEqual(calls[-1][0], 'POST')
        client.call = lambda *args, **kwargs: {'extendedProperties': {'private': {'tsukiya_namespace': 'other'}}}
        with self.assertRaises(sync.SyncError):
            client.sync(sync.event_id(1,'test'), body, 'test', 1)
    def test_invalid_record_does_not_block_others(self):
        self.update(visit_at='invalid')
        self.assertEqual(self.tick()['failed'], 1)
        self.update(visit_at=self.row['visit_at'])
        self.assertEqual(self.tick(1001)['synced'], 1)

if __name__ == '__main__':
    unittest.main()
