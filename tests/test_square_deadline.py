import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from test_workflow import run

class SquareDeadlineTest(unittest.TestCase):
    def invoice(self, day='2026-10-24', tz='Asia/Tokyo'):
        return {'id':'test', 'status':'UNPAID', 'timezone':tz,
                'payment_requests':[{'request_type':'BALANCE','due_date':day}],
                'primary_recipient':{'email_address':'test@example.com'}}
    def test_due_day_end_in_invoice_timezone(self):
        self.assertEqual(run.square_payment_deadline(self.invoice()).isoformat(),'2026-10-25T00:00:00+09:00')
    def test_square_documented_example(self):
        self.assertEqual(run.square_payment_deadline(self.invoice('2021-03-09','America/Los_Angeles')).astimezone(timezone.utc).isoformat(),'2021-03-10T08:00:00+00:00')
    def test_unknown_schedule_fails_closed(self):
        for invoice in ({},dict(self.invoice(),timezone='Unknown/Zone'),dict(self.invoice(),payment_requests=[]),dict(self.invoice(),payment_requests=[{'request_type':'BALANCE','due_date':'bad'}]),dict(self.invoice(),payment_requests=self.invoice()['payment_requests']*2)):
            self.assertIsNone(run.square_payment_deadline(invoice))
    def test_phone_explicit_exception(self):
        r={'source':'PHONE','visit_at':'2026-10-24T18:00','created_at':'2026-10-23T10:00:00+09:00'}
        invoice=dict(self.invoice(),description='前日電話予約のお支払期限：2026年10月24日 08:00（日本時間）。')
        self.assertEqual(run.square_payment_deadline(invoice,r).isoformat(),'2026-10-24T08:00:00+09:00')
        self.assertEqual(run.square_payment_deadline(self.invoice(),r).hour,0)
    def test_window_boundary_and_wrong_recipient(self):
        r={'id':1,'status':'INVOICED','square_invoice_id':'test','email':'test@example.com'}
        class Clock(datetime):
            value=datetime(2026,10,24,2,59,59,tzinfo=timezone.utc)
            @classmethod
            def now(cls,tz=None): return cls.value.astimezone(tz)
        with patch.object(run,'datetime',Clock),patch.multiple(run,SMTP_HOST='test',SMTP_USER='user',SMTP_PASS='pw',MAIL_FROM='shop@example.com'),patch.object(run,'con') as con:
            self.assertFalse(run.send_payment_reminder(r,self.invoice()))
            Clock.value=datetime(2026,10,24,15,0,tzinfo=timezone.utc)
            self.assertFalse(run.send_payment_reminder(r,self.invoice()))
            Clock.value=datetime(2026,10,24,3,0,tzinfo=timezone.utc)
            self.assertFalse(run.send_payment_reminder(r,dict(self.invoice(),primary_recipient={'email_address':'other@example.com'})))
            self.assertFalse(run.send_payment_reminder(r,dict(self.invoice(),id='other')))
            con.assert_not_called()
    def test_paid_after_claim_does_not_send(self):
        from test_workflow import WorkflowTest
        fixture=WorkflowTest();fixture.setUp()
        try:
            rid=fixture.reservation()
            invoice=dict(self.invoice(),id='inv-test',primary_recipient={'email_address':'guest@example.com'})
            class Clock(datetime):
                @classmethod
                def now(cls,tz=None):return datetime(2026,10,24,4,tzinfo=timezone.utc).astimezone(tz)
            with patch.object(run,'datetime',Clock),patch.multiple(run,SMTP_HOST='test',SMTP_USER='user',SMTP_PASS='pw',MAIL_FROM='shop@example.com'),patch.object(run,'square',return_value={'invoice':dict(invoice,status='PAID')}),patch.object(run.smtplib,'SMTP') as smtp:
                self.assertFalse(run.send_payment_reminder(fixture.get(rid),invoice))
                smtp.assert_not_called()
                self.assertEqual(fixture.get(rid)['reminder_status'],'ERROR')
        finally:fixture.tearDown()
    def test_migration_preserves_manual_receipts_and_skips_existing(self):
        from test_workflow import WorkflowTest
        fixture=WorkflowTest();fixture.setUp()
        try:
            rid=fixture.reservation()
            c=run.con();c.execute('ALTER TABLE reservations DROP COLUMN square_reminder_policy');c.commit();c.close()
            c=run.con();c.close()
            self.assertEqual(fixture.get(rid)['reminder_status'],'SKIPPED_LEGACY')
            next_id=fixture.reservation('next-invoice')
            self.assertIsNone(fixture.get(next_id)['reminder_status'])
        finally:fixture.tearDown()
    def test_does_not_cancel_before_square_deadline(self):
        from test_workflow import WorkflowTest
        fixture=WorkflowTest();fixture.setUp()
        try:
            rid=fixture.reservation()
            old='2026-01-01T00:00:00+00:00'
            c=run.con();c.execute("UPDATE reservations SET source='WEB',created_at=?,invoice_issued_at=?,reminder_status='SENT' WHERE id=?",(old,old,rid));c.commit();c.close()
            with patch.object(run,'square',return_value={'invoice':self.invoice('2099-12-31')}) as square:
                self.assertEqual(run.expire_public_reservations(),0)
                self.assertEqual(square.call_count,1)
                self.assertEqual(fixture.get(rid)['status'],'INVOICED')
        finally:fixture.tearDown()
