"""Temporary databases and mocked gateways only; never contact live services."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import test_workflow as fixtures
import test_bank_transfer as bank_fixtures
import test_web_checkout as card_fixtures
import payment_terms

run = fixtures.run


class UnifiedTermsTest(unittest.TestCase):
    def test_new_bank_cancels_at_deadline_once_and_legacy_is_unchanged(self):
        f = bank_fixtures.BankTransferTest(); f.setUp()
        try:
            rid = f.book()
            c = run.con()
            c.execute('UPDATE reservations SET payment_term_due_at=? WHERE id=?',
                      ((datetime.now(timezone.utc)+timedelta(seconds=5)).isoformat(), rid))
            c.commit(); c.close()
            self.assertEqual(payment_terms.expire(run), 0)
            c = run.con()
            c.execute('UPDATE reservations SET payment_term_due_at=? WHERE id=?',
                      ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(), rid))
            c.commit(); c.close()
            with patch.object(run, 'square') as square:
                self.assertEqual(payment_terms.expire(run), 1)
                self.assertEqual(payment_terms.expire(run), 0)
                square.assert_not_called()
            c = run.con()
            self.assertEqual(c.execute('SELECT status FROM reservations WHERE id=?',(rid,)).fetchone()[0], 'CANCELLED')
            self.assertEqual(c.execute("SELECT count(*) FROM bank_notices WHERE stage='cancelled'").fetchone()[0], 1)
            self.assertEqual(c.execute('SELECT count(*) FROM cancellation_emails').fetchone()[0], 1)
            c.close()
        finally:
            f.tearDown()

    def test_card_hold_lasts_24_hours_but_unknown_payment_is_not_released(self):
        f = card_fixtures.CheckoutTest(); f.setUp()
        try:
            x, session = f.book()
            c = run.con(); saved = c.execute('SELECT * FROM web_checkouts').fetchone()
            self.assertEqual(saved['expires']-saved['created'], 86400)
            c.execute("UPDATE web_checkouts SET state='UNKNOWN',expires=0")
            c.commit(); c.close()
            self.assertEqual(run.web_checkout.expire(run), 0)
            self.assertEqual(f.row(session['reservation_id'])['status'], 'PENDING')
        finally:
            f.tearDown()

    def test_new_phone_has_24_hours_even_when_booked_day_before(self):
        now = datetime(2026,12,1,9,30,tzinfo=timezone.utc)
        due = run.new_phone_payment_deadline({'visit_at':'2026-12-02T18:00',
                 'payment_term_policy':payment_terms.POLICY},now)
        self.assertEqual(due, now+timedelta(hours=24))

    def test_unpaid_invoice_cancels_only_after_provider_verification(self):
        f = fixtures.WorkflowTest(); f.setUp()
        try:
            rid = f.reservation()
            due = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
            c = run.con()
            c.execute('UPDATE reservations SET source=\'WEB\',payment_term_policy=?,payment_term_due_at=?,invoice_payment_due_at=? WHERE id=?',
                      (payment_terms.POLICY,due,due,rid))
            c.commit(); c.close()
            jp = datetime.fromisoformat(due).astimezone(run.ZoneInfo('Asia/Tokyo'))
            invoice = {'id':'inv-test','version':1,'status':'UNPAID','timezone':'Asia/Tokyo',
                'payment_requests':[{'request_type':'BALANCE','due_date':jp.date().isoformat()}],
                'description':'お支払期限 / Payment deadline: '+jp.isoformat(timespec='seconds')+' (JST)'}
            with patch.object(run,'square',return_value={'invoice':invoice}) as square:
                self.assertEqual(payment_terms.expire(run),1)
                self.assertEqual(square.call_count,2)
                self.assertTrue(square.call_args.args[0].endswith('/cancel'))
            self.assertEqual(f.get(rid)['status'],'CANCELLED')
        finally:
            f.tearDown()

    def test_paid_and_uncertain_invoice_are_never_cancelled(self):
        f = fixtures.WorkflowTest(); f.setUp()
        try:
            rid = f.reservation()
            c = run.con()
            due = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
            c.execute('UPDATE reservations SET payment_term_policy=?,payment_term_due_at=?,invoice_payment_due_at=? WHERE id=?',
                      (payment_terms.POLICY,due,due,rid))
            c.commit(); c.close()
            for status in ('PAID', 'PAYMENT_PENDING', 'PARTIALLY_PAID'):
                with patch.object(run,'square',return_value={'invoice':{'id':'inv-test','status':status}}) as square:
                    self.assertEqual(payment_terms.expire(run),0)
                    self.assertEqual(square.call_count,1)
            with patch.object(run,'square',side_effect=TimeoutError):
                self.assertEqual(payment_terms.expire(run),0)
            self.assertNotEqual(f.get(rid)['status'],'CANCELLED')
        finally:
            f.tearDown()

