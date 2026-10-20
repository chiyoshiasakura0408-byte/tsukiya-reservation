import ast
import json
import os
import re
import sqlite3
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

# Execute the real small functions without unrelated external integrations.
source = ast.parse(Path(__file__).with_name('concierge.py').read_text())
names = {'queue_new_web_reservations', 'setting', 'put', 'enqueue', 'text_message', 'elegant'}
scope = dict(globals(), JST=timezone(timedelta(hours=9)))
exec(compile(ast.Module(body=[n for n in source.body if isinstance(n, ast.FunctionDef) and n.name in names], type_ignores=[]), 'concierge.py', 'exec'), scope)

class Notices(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(':memory:')
        self.c.row_factory = sqlite3.Row
        self.c.executescript('''CREATE TABLE concierge_settings(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE reservations(id INTEGER PRIMARY KEY,source TEXT,guest_name TEXT,created_at TEXT,status TEXT,amount INTEGER,course_name TEXT,visit_at TEXT,party_size INTEGER,seating_area TEXT,payment_method TEXT);
        CREATE TABLE owner_booking_notices(reservation_id INTEGER PRIMARY KEY,outbox_id TEXT UNIQUE);
        CREATE TABLE concierge_outbox(id TEXT PRIMARY KEY,user_id TEXT,channel TEXT,kind TEXT,payload TEXT,created REAL,next_try REAL);
        CREATE TABLE reservation_notice_links(outbox_id TEXT PRIMARY KEY,reservation_id INTEGER);''')
        scope['put'](self.c, 'owner', 'U'+'a'*32)
        self.env = patch.dict(os.environ, {'LINE_CHANNEL_ACCESS_TOKEN': 'test-token-never-sent'})
        self.env.start()
    def tearDown(self):
        self.env.stop()
        self.c.close()
    def add(self, id=1, **kw):
        row = dict(id=id,source='WEB',guest_name='架空予約',created_at='2026-10-20T01:00:00+09:00',status='INVOICED',amount=120000,course_name='蟹',visit_at='2026-12-03T18:00',party_size=2,seating_area='COUNTER',payment_method='CARD')
        row.update(kw)
        self.c.execute('INSERT INTO reservations VALUES ('+','.join('?' for _ in row)+')',tuple(row.values()))
    def queue(self):
        with self.c: scope['queue_new_web_reservations'](self.c)
        return self.c.execute('SELECT * FROM concierge_outbox').fetchall()
    def test_once_and_owner_only(self):
        self.add()
        rows=self.queue(); self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['channel'],'owner')
        self.assertEqual(rows[0]['user_id'],'U'+'a'*32)
        self.assertIn('未決済',rows[0]['payload'])
        self.assertIn('初回テスト通知',rows[0]['payload'])
        self.assertEqual(len(self.queue()),1)
        self.assertEqual(self.c.execute('SELECT status FROM reservations').fetchone()[0],'INVOICED')
    def test_jst_boundary(self):
        self.add(1,created_at='2026-10-19T14:59:59+00:00')
        self.add(2,created_at='2026-10-19T15:00:00+00:00')
        self.assertEqual(len(self.queue()),1)
        self.assertEqual(self.c.execute('SELECT reservation_id FROM owner_booking_notices').fetchone()[0],2)
    def test_excludes_legacy_tablecheck_tests_and_cancelled(self):
        self.add(1,source='TABLECHECK');self.add(2,source='DIRECT')
        self.add(3,amount=1);self.add(4,course_name='決済テスト')
        self.add(5,status='CANCELLED');self.add(6,status='ERROR')
        self.assertEqual(len(self.queue()),0)
    def test_pending_card_waits_for_invoice(self):
        self.add(status='PENDING'); self.assertEqual(len(self.queue()),0)
        self.c.execute("UPDATE reservations SET status='INVOICED'")
        self.assertEqual(len(self.queue()),1)
    def test_pending_bank(self):
        self.add(status='PENDING',payment_method='BANK')
        self.assertEqual(len(self.queue()),1)
    def test_group_recipient_rejected(self):
        scope['put'](self.c,'owner','C'+'a'*32);self.add()
        self.assertEqual(len(self.queue()),0)
    def test_missing_token_and_disabled(self):
        self.add()
        with patch.dict(os.environ, {'LINE_CHANNEL_ACCESS_TOKEN':''}):
            self.assertEqual(len(self.queue()),0)
        scope['put'](self.c,'owner_booking_notifications_enabled','0')
        self.assertEqual(len(self.queue()),0)
    def test_future_booking_is_not_test(self):
        self.add(created_at='2026-10-21T12:00:00+09:00')
        scope['put'](self.c,'owner_booking_notifications_started','2026-10-20T19:00:00+09:00')
        self.assertNotIn('初回テスト通知',self.queue()[0]['payload'])

if __name__=='__main__': unittest.main()
