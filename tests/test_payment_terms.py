import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import test_workflow as fixtures
import test_bank_transfer as bank_fixtures

run = fixtures.run


class PaymentTermsTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.WorkflowTest()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def test_phone_is_exactly_24_hours_and_day_before_is_capped(self):
        now = datetime(2026, 12, 1, 9, 30, tzinfo=timezone.utc)
        self.assertEqual(run.new_phone_payment_deadline({'visit_at':'2026-12-05T18:00'}, now), now + timedelta(hours=24))
        self.assertEqual(run.new_phone_payment_deadline({'visit_at':'2026-12-02T18:00'}, now), datetime(2026,12,1,23,tzinfo=timezone.utc))

    def test_invoice_retry_preserves_exact_term_and_matches_square_description(self):
        rid = self.fixture.reservation(None)
        c = run.con()
        c.execute("UPDATE reservations SET visit_at='2026-12-05T18:00',status='PENDING' WHERE id=?", (rid,))
        c.commit(); c.close()
        requests = []
        def square(path, body=None):
            if path == '/v2/customers': return {'customer':{'id':'customer'}}
            if path == '/v2/orders': return {'order':{'id':'order'}}
            if path == '/v2/invoices':
                requests.append(body['invoice'])
                return {'invoice':{'id':'invoice','version':1}}
            return {'invoice':{'id':'invoice','public_url':'https://example.com/test'}}
        before = datetime.now(timezone.utc)
        with patch.object(run,'square',side_effect=square):
            run.make_invoice(self.fixture.get(rid))
            saved = self.fixture.get(rid)
            with patch.object(run,'new_phone_payment_deadline',side_effect=AssertionError('recomputed')):
                run.make_invoice(saved)
        due = datetime.fromisoformat(saved['invoice_payment_due_at'])
        self.assertAlmostEqual((due-before).total_seconds(), 86400, delta=2)
        self.assertEqual(requests[0], requests[1])
        invoice = dict(requests[0], timezone='Asia/Tokyo')
        self.assertEqual(run.square_payment_deadline(invoice,saved),due)
        self.assertIsNone(run.square_payment_deadline(dict(invoice,description='changed'),saved))

    def test_exact_phone_expiry_without_reminder_and_paid_is_not_cancelled(self):
        rid = self.fixture.reservation()
        future = datetime.now(timezone.utc)+timedelta(minutes=1)
        c = run.con()
        c.execute("UPDATE reservations SET visit_at='2026-12-05T18:00',invoice_payment_due_at=? WHERE id=?",(future.isoformat(),rid))
        c.commit();c.close()
        with patch.object(run,'square') as gateway:
            self.assertEqual(run.expire_public_reservations(),0)
            gateway.assert_not_called()
        due = datetime.now(timezone.utc)-timedelta(seconds=1)
        c = run.con()
        c.execute("UPDATE reservations SET visit_at='2026-12-05T18:00',invoice_payment_due_at=? WHERE id=?",(due.isoformat(),rid))
        c.commit();c.close()
        jp = due.astimezone(run.ZoneInfo('Asia/Tokyo'))
        invoice={'id':'inv-test','version':1,'status':'PAID','timezone':'Asia/Tokyo',
                 'payment_requests':[{'request_type':'BALANCE','due_date':jp.date().isoformat()}],
                 'description':'お支払期限 / Payment deadline: '+jp.isoformat(timespec='seconds')+' (JST)'}
        with patch.object(run,'square',return_value={'invoice':invoice}) as gateway:
            self.assertEqual(run.expire_public_reservations(),0)
            self.assertEqual(gateway.call_count,1)
        with patch.object(run,'square',return_value={'invoice':dict(invoice,status='UNPAID')}) as gateway:
            self.assertEqual(run.expire_public_reservations(),1)
            self.assertEqual(gateway.call_count,2)
            self.assertTrue(gateway.call_args.args[0].endswith('/cancel'))
        self.assertEqual(self.fixture.get(rid)['status'],'CANCELLED')

    def test_legacy_invoice_has_no_retroactive_24_hour_deadline(self):
        rid=self.fixture.reservation()
        c=run.con()
        issued=(datetime.now(timezone.utc)-timedelta(hours=25)).isoformat()
        c.execute("UPDATE reservations SET visit_at='2026-12-05T18:00',created_at=?,invoice_issued_at=?,reminder_status='SENT' WHERE id=?",(issued,issued,rid))
        c.commit();c.close()
        with patch.object(run,'square') as gateway:
            self.assertEqual(run.expire_public_reservations(),0)
            gateway.assert_not_called()

    def test_bank_registration_sets_24_hours_and_keeps_existing_due_at(self):
        bank=bank_fixtures.BankTransferTest();bank.setUp()
        try:
            bank.body['seating_area']='PRIVATE1'
            too_late=(datetime.now(bank_fixtures.bank_transfer.JST).date()+timedelta(days=2)).isoformat()
            self.assertEqual(bank.call('/api/public/reservations',{**bank.body,'bank_planned_date':too_late})[0],400)
            rid=bank.book()
            c=run.con();r=dict(c.execute('SELECT * FROM reservations WHERE id=?',(rid,)).fetchone());c.close()
            self.assertEqual(datetime.fromisoformat(r['bank_due_at'])-datetime.fromisoformat(r['created_at']),timedelta(hours=24))
            c=run.con();old_due=(datetime.now(timezone.utc)+timedelta(hours=40)).isoformat()
            c.execute('UPDATE reservations SET bank_due_at=? WHERE id=?',(old_due,rid));c.commit();c.close()
            self.assertEqual(bank.book(),rid)
            c=run.con();self.assertEqual(c.execute('SELECT bank_due_at FROM reservations WHERE id=?',(rid,)).fetchone()[0],old_due);c.close()
        finally:
            bank.tearDown()
