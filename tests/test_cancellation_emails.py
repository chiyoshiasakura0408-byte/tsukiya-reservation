import smtplib
import unittest
from unittest.mock import patch
import test_workflow as workflow
run = workflow.run
import cancellation_emails as emails


class CancellationEmailsTest(unittest.TestCase):
    def setUp(self):
        self.fixture = workflow.WorkflowTest()
        self.fixture.setUp()
        self.config = patch.multiple(run, SMTP_HOST='smtp.test', SMTP_USER='user', SMTP_PASS='secret', MAIL_FROM='shop@example.com')
        self.config.start()
        self.smtp = patch.object(emails.smtplib, 'SMTP')
        self.server = self.smtp.start().return_value
        self.server.send_message.return_value = {}

    def tearDown(self):
        self.smtp.stop()
        self.config.stop()
        self.fixture.tearDown()

    def cancel(self, reason='PAYMENT_EXPIRED'):
        rid = self.fixture.reservation()
        c = run.con()
        c.execute("UPDATE reservations SET status='CANCELLED',cancellation_reason=? WHERE id=?", (reason, rid))
        c.commit(); c.close()
        return rid

    def notice(self, rid):
        c = run.con()
        try:
            row = c.execute('SELECT * FROM cancellation_emails WHERE reservation_id=?', (rid,)).fetchone()
            return dict(row) if row else None
        finally:
            c.close()

    def test_atomic_capture_and_one_send(self):
        rid = self.cancel()
        emails.process(run); emails.process(run)
        self.assertEqual(self.notice(rid)['state'], 'SENT')
        self.assertTrue(self.notice(rid)['accepted_at'])
        self.server.send_message.assert_called_once()
        mail = self.server.send_message.call_args.args[0]
        self.assertEqual(mail['To'], 'guest@example.com')
        self.assertIn('ご予約をお取り消しいたしました', mail.get_content())
        self.assertEqual(mail['Message-ID'], self.notice(rid)['message_id'])

    def test_manual_cancel_not_queued_and_transaction_rollback(self):
        self.assertIsNone(self.notice(self.cancel('CUSTOMER')))
        rid = self.fixture.reservation('second')
        c = run.con()
        c.execute("UPDATE reservations SET status='CANCELLED',cancellation_reason='PAYMENT_EXPIRED' WHERE id=?", (rid,))
        c.rollback(); c.close()
        self.assertIsNone(self.notice(rid))

    def test_ambiguous_smtp_failure_is_held(self):
        rid = self.cancel()
        self.server.send_message.side_effect = TimeoutError()
        emails.process(run); emails.process(run)
        self.assertEqual(self.notice(rid)['state'], 'REVIEW')
        self.server.send_message.assert_called_once()

    def test_setup_failure_retries_and_limit(self):
        rid = self.cancel()
        self.server.starttls.side_effect = ConnectionError()
        for _ in range(6):
            c = run.con(); c.execute('UPDATE cancellation_emails SET next_try=0 WHERE reservation_id=?', (rid,)); c.commit(); c.close()
            emails.process(run)
        self.assertEqual(self.notice(rid)['state'], 'REVIEW')
        self.assertEqual(self.notice(rid)['attempts'], 5)
        self.server.send_message.assert_not_called()

    def test_no_mail_to_missing_address_or_restored_booking(self):
        rid = self.cancel()
        c = run.con(); c.execute("UPDATE reservations SET status='CONFIRMED' WHERE id=?", (rid,)); c.commit(); c.close()
        emails.process(run)
        self.assertEqual(self.notice(rid)['state'], 'SKIPPED')
        self.server.send_message.assert_not_called()
        other = self.cancel()
        c = run.con(); c.execute("UPDATE reservations SET email='' WHERE id=?", (other,)); c.commit(); c.close()
        emails.process(run)
        self.assertEqual(self.notice(other)['state'], 'REVIEW')
        self.server.send_message.assert_not_called()

    def test_english_body_and_safe_customer_link(self):
        r = dict(self.fixture.get(self.fixture.reservation()), booking_language='en')
        mail = emails.message(run, r, '<test@example.com>')
        self.assertIn('has been cancelled', mail.get_content())
        self.assertNotIn('支払', mail.get_content())
        from pathlib import Path
        online = Path('public/reservations.html').read_text().split('function showOnlineDetail(id)',1)[1].split('\n',1)[0]
        self.assertIn('/customers?customer_id=${Number(r.customer_id)}', online)
        self.assertIn('顧客情報へ', online)

    def test_existing_cancellation_not_backfilled(self):
        rid = self.cancel()
        c = run.con(); c.execute('DELETE FROM cancellation_emails'); c.commit(); c.close()
        emails.process(run)
        self.assertIsNone(self.notice(rid))
        self.server.send_message.assert_not_called()

    def test_cancellation_before_queue_commit_never_sends(self):
        rid = self.cancel()
        c = run.con(); c.execute("UPDATE cancellation_emails SET state='SENDING',started=0 WHERE reservation_id=?", (rid,)); c.commit(); c.close()
        emails.process(run)
        self.assertEqual(self.notice(rid)['state'], 'REVIEW')
        self.server.send_message.assert_not_called()
