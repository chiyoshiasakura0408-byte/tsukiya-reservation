import json
import tempfile
import unittest
from unittest.mock import patch
import concierge
import line_delivery
import run
import accounting_booking_notices as notices


class AccountingConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = self.tmp.name + '/db'
        with patch.object(run, 'DB', self.db):
            c = run.con(); c.close()
        self.c = concierge.connect(self.db)
        self.addCleanup(self.c.close)

    def add(self, status='CONFIRMED', source='DIRECT', course='ずわい蟹'):
        return self.c.execute('''INSERT INTO reservations(source,guest_name,visit_at,
            party_size,course_name,amount,seating_area,status,created_at,updated_at)
            VALUES(?,'架空試験名',date('now','+9 hours')||'T18:00',2,?,88000,'PRIVATE1',?,'now','now')''',
            (source, course, status)).lastrowid

    def rows(self):
        return self.c.execute("SELECT * FROM concierge_outbox WHERE kind='accounting-booking-confirmed'").fetchall()

    def test_direct_confirmation_and_resend_only_one_group_notice(self):
        with self.c:
            rid = self.add()
            notices.queue(self.c); notices.queue(self.c)
            self.c.execute("UPDATE reservations SET status='CONFIRMED' WHERE id=?", (rid,))
            notices.queue(self.c)
        rows = self.rows(); self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['user_id'], line_delivery.TARGET)
        text = json.loads(rows[0]['payload'])[0]['text']
        for value in ('架空試験名', '18:00', '2名', '個室①', '88,000', '当日精算'):
            self.assertIn(value, text)

    def test_unpaid_not_sent_then_confirmation_sent_once(self):
        with self.c:
            rid = self.add(status='INVOICED', source='PHONE')
            notices.queue(self.c); self.assertEqual(len(self.rows()), 0)
            self.c.execute("UPDATE reservations SET status='CONFIRMED',payment_confirmed_at='paid' WHERE id=?", (rid,))
            notices.queue(self.c); notices.queue(self.c)
        self.assertEqual(len(self.rows()), 1)
        self.assertIn('決済済み', self.rows()[0]['payload'])

    def test_rollback_test_booking_and_cancel_before_queue(self):
        self.add(); self.c.rollback()
        with self.c:
            self.add(course='決済テスト')
            rid = self.add()
            self.c.execute("UPDATE reservations SET status='CANCELLED' WHERE id=?", (rid,))
            notices.queue(self.c)
        self.assertEqual(len(self.rows()), 0)

    def test_other_days_are_not_notified_even_when_today_arrives(self):
        with self.c:
            self.c.execute('''INSERT INTO reservations(source,guest_name,visit_at,
                party_size,course_name,amount,seating_area,status,created_at,updated_at)
                VALUES('DIRECT','明日',date('now','+9 hours','+1 day')||'T18:00',2,
                'ずわい蟹',88000,'PRIVATE1','CONFIRMED','now','now')''')
            notices.queue(self.c)
        self.assertEqual(len(self.rows()), 0)
        self.assertEqual(self.c.execute('SELECT count(*) FROM accounting_booking_events').fetchone()[0], 0)

    def test_retry_does_not_send_after_date_or_status_changes(self):
        with self.c:
            rid = self.add(); notices.queue(self.c)
            key = self.rows()[0]['id']
            self.assertTrue(notices.ready(self.c, key))
            self.c.execute("UPDATE reservations SET visit_at=date('now','+9 hours','+1 day')||'T18:00' WHERE id=?", (rid,))
            self.assertFalse(notices.ready(self.c, key))
