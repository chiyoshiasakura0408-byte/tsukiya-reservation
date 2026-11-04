"""Real SQLite + fake Square/SMTP; no external email or money movement."""
import concurrent.futures
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import run
import refunds
import refund_support as support


class SupportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for key, value in {'DB': Path(self.tmp.name)/'test.sqlite',
                           'SQUARE_ENV': 'production', 'SQUARE_LOCATION_ID': 'loc',
                           'SMTP_HOST': 'smtp', 'SMTP_USER': 'store@example.com',
                           'SMTP_PASS': 'fake', 'MAIL_FROM': 'store@example.com'}.items():
            p = patch.object(run, key, value)
            p.start(); self.addCleanup(p.stop)
        c = run.con()
        c.execute("INSERT INTO reservations(source,guest_name,email,phone,visit_at,party_size,amount,status,payment_source,square_invoice_id,created_at,updated_at) VALUES('WEB','PRIVATE GUEST','private@example.com','09000000000','2030-11-10T18:00',2,132000,'CANCELLED','SQUARE','inv','created','updated')")
        self.r = dict(c.execute('SELECT * FROM reservations').fetchone())
        refunds.enqueue(c, self.r, 0, run.now_iso()); c.commit(); c.close()
        self.p = {'id': 'pay', 'status': 'COMPLETED', 'order_id': 'ord',
                  'location_id': 'loc', 'total_money': {'amount': 132000, 'currency': 'JPY'},
                  'created_at': '2026-10-30T06:17:41Z',
                  'card_details': {'card': {'card_brand': 'VISA', 'last_4': '1234'}}}

    def square(self, path, body=None):
        self.assertIsNone(body, 'Inquiry must never issue a financial write')
        if path == '/v2/invoices/inv': return {'invoice': {'status': 'PAID', 'order_id': 'ord'}}
        if path == '/v2/orders/ord': return {'order': {'tenders': [{'id': 'pay', 'type': 'CARD'}]}}
        if path == '/v2/payments/pay': return {'payment': self.p}
        self.fail(path)

    def row(self):
        c = run.con()
        try: return dict(c.execute('SELECT * FROM refund_support_inquiries').fetchone())
        finally: c.close()

    def update(self, **values):
        c = run.con()
        with c:
            c.execute('UPDATE refund_support_inquiries SET '+','.join(k+'=?' for k in values), tuple(values.values()))
        c.close()

    def test_cancel_enqueues_atomically_and_sends_once_before_approval(self):
        with patch.object(run, 'square', side_effect=self.square) as square, patch.object(run.smtplib, 'SMTP') as smtp:
            smtp.return_value.__enter__.return_value.send_message.return_value = {}
            support.process(run); support.process(run)
            self.assertEqual(self.row()['state'], 'SENT')
            sent = smtp.return_value.__enter__.return_value.send_message
            sent.assert_called_once()
            msg = sent.call_args.args[0]
            self.assertEqual(msg['To'], support.RECIPIENT)
            for text in ('132,000', '2026-10-30 15:17:41', 'VISA', '1234', 'pay', '残高不足は未確認'):
                self.assertIn(text, msg.get_content())
            for private in ('PRIVATE GUEST', 'private@example.com', '09000000000'):
                self.assertNotIn(private, msg.get_content())
            self.assertFalse(any('/refunds' in call.args[0] for call in square.call_args_list))
            c = run.con()
            self.assertEqual(c.execute('SELECT status FROM cancellation_refunds').fetchone()[0], 'AWAITING_APPROVAL')
            c.close()

    def test_timeout_is_visible_and_never_replayed_after_restart(self):
        with patch.object(run, 'square', side_effect=self.square), patch.object(run.smtplib, 'SMTP') as smtp:
            smtp.return_value.__enter__.return_value.send_message.side_effect = TimeoutError()
            support.process(run); support.process(run)
            self.assertEqual(self.row()['state'], 'UNKNOWN')
            self.assertEqual(smtp.return_value.__enter__.return_value.send_message.call_count, 1)
        self.update(state='SENDING', started=0)
        with patch.object(run.smtplib, 'SMTP') as smtp:
            support.process(run)
            self.assertEqual(self.row()['state'], 'UNKNOWN')
            smtp.assert_not_called()

    def test_missing_configuration_waits_without_square_access(self):
        with patch.object(run, 'SMTP_HOST', ''), patch.object(run, 'square') as sq:
            support.process(run)
            self.assertEqual(self.row()['state'], 'WAITING_CONFIG')
            sq.assert_not_called()

    def test_missing_card_details_retry_then_stop_without_email(self):
        del self.p['card_details']
        with patch.object(run, 'square', side_effect=self.square), patch.object(run.smtplib, 'SMTP') as smtp:
            for _ in range(3):
                self.update(next_attempt=0)
                support.process(run)
            self.assertEqual(self.row()['state'], 'REVIEW')
            smtp.assert_not_called()

    def test_mismatched_payment_and_existing_refund_never_sent(self):
        for field, value in [('location_id', 'other'), ('refund_ids', ['already-refunded'])]:
            original = self.p.copy()
            self.p[field] = value
            self.update(state='PENDING', attempts=0, next_attempt=0)
            with patch.object(run, 'square', side_effect=self.square), patch.object(run.smtplib, 'SMTP') as smtp:
                support.process(run); smtp.assert_not_called()
            self.p = original

    def test_sandbox_bank_zero_and_completed_do_not_send(self):
        with patch.object(run, 'SQUARE_ENV', 'sandbox'), patch.object(run, 'square') as sq:
            support.process(run); sq.assert_not_called()
            self.assertEqual(self.row()['state'], 'SKIPPED')
        c = run.con()
        with c:
            c.execute('DELETE FROM refund_support_inquiries')
            r = {**self.r, 'payment_source': 'BANK'}
            support.enqueue(c, r, 132000)
            support.enqueue(c, self.r, 0)
            self.assertEqual(c.execute('SELECT count(*) FROM refund_support_inquiries').fetchone()[0], 0)
        c.close()

    def test_concurrent_workers_send_once(self):
        barrier = threading.Barrier(2)
        def worker():
            barrier.wait(); support.dispatch(run, self.r['id'])
        with patch.object(run, 'square', side_effect=self.square), patch.object(run.smtplib, 'SMTP') as smtp:
            smtp.return_value.__enter__.return_value.send_message.return_value = {}
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda _: worker(), range(2)))
            self.assertEqual(smtp.return_value.__enter__.return_value.send_message.call_count, 1)
            self.assertEqual(self.row()['state'], 'SENT')

    def test_legacy_migration_does_not_email_historical_jobs(self):
        c = run.con()
        with c:
            c.execute('DROP TABLE refund_support_inquiries')
            support.schema(c)
        c.close()
        self.assertEqual(self.row()['state'], 'SKIPPED_LEGACY')
        with patch.object(run, 'square') as sq, patch.object(run.smtplib, 'SMTP') as smtp:
            support.process(run); sq.assert_not_called(); smtp.assert_not_called()

    def test_rollback_cancellation_does_not_leave_inquiry(self):
        c = run.con()
        with c: c.execute('DELETE FROM refund_support_inquiries')
        support.enqueue(c, self.r, 132000)
        c.rollback(); c.close()
        c = run.con()
        self.assertEqual(c.execute('SELECT count(*) FROM refund_support_inquiries').fetchone()[0], 0)
        c.close()

    def test_web_card_uses_checkout_payment_and_checks_environment(self):
        c = run.con()
        with c:
            c.execute("UPDATE reservations SET source='WEB_CARD'")
        c.execute("INSERT INTO web_checkouts(id,request_id,secret_hash,payload_hash,reservation_id,environment,location_id,amount,language,state,expires,created,updated,idempotency_key,order_id,payment_id) VALUES('checkout','request','secret','payload',?,'production','loc',132000,'ja','PAID',0,0,0,'key','ord','pay')", (self.r['id'],))
        c.commit(); c.close()
        with patch.object(run, 'square', side_effect=self.square) as sq, patch.object(run.smtplib, 'SMTP') as smtp:
            smtp.return_value.__enter__.return_value.send_message.return_value = {}
            support.process(run)
            sq.assert_called_once_with('/v2/payments/pay')
            self.assertEqual(self.row()['state'], 'SENT')
        self.update(state='PENDING')
        c = run.con()
        with c: c.execute("UPDATE web_checkouts SET environment='sandbox'")
        c.close()
        with patch.object(run, 'square') as sq, patch.object(run.smtplib, 'SMTP') as smtp:
            support.process(run); sq.assert_not_called(); smtp.assert_not_called()


if __name__ == '__main__':
    unittest.main()
