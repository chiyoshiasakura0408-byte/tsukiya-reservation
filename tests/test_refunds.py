import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
import run
import refunds

class RefundTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.patches=[patch.object(run,'DB',Path(self.tmp.name)/'db'),patch.object(run,'ADMIN_TOKEN','secret'),patch.object(run,'SQUARE_LOCATION_ID','loc'),patch.object(run,'SMTP_HOST','')]
        for p in self.patches:p.start()
        self.day=(datetime.now(timezone(timedelta(hours=9)))+timedelta(days=10)).isoformat()
        c=run.con();c.execute("INSERT INTO reservations(source,guest_name,email,visit_at,party_size,amount,status,payment_source,square_invoice_id,created_at,updated_at) VALUES('WEB','Test','a@example.com',?,2,120000,'CONFIRMED','SQUARE','inv','created','updated')",(self.day,));c.commit();self.r=dict(c.execute('SELECT * FROM reservations').fetchone());c.close()
        self.token=run.cancellation_token(self.r)
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()
    def job(self):
        c=run.con();r=c.execute('SELECT * FROM cancellation_refunds').fetchone();c.close();return dict(r) if r else None
    def fake(self,path,body=None):
        if path=='/v2/invoices/inv':return {'invoice':{'status':'PAID','order_id':'ord'}}
        if path=='/v2/orders/ord':return {'order':{'tenders':[{'id':'pay','type':'CARD'}]}}
        if path=='/v2/payments/pay':return {'payment':{'id':'pay','order_id':'ord','location_id':'loc','status':'COMPLETED','total_money':{'amount':120000,'currency':'JPY'}}}
        if path=='/v2/refunds':return {'refund':{'id':'ref','status':'PENDING'}}
        if path=='/v2/refunds/ref':return {'refund':{'id':'ref','status':'COMPLETED'}}
        raise AssertionError(path)
    def test_secure_preview_consent_and_idempotent_refund(self):
        self.assertEqual(run.customer_cancellation(self.token+'a')[0],403)
        self.assertEqual(run.customer_cancellation(self.token)[1]['fee'],0)
        self.assertIsNone(self.job())
        self.assertEqual(run.customer_cancellation(self.token,True,False,0)[0],400)
        self.assertEqual(run.customer_cancellation(self.token,True,True,120000)[0],400)
        self.assertEqual(run.customer_cancellation(self.token,True,True,0)[0],200)
        run.customer_cancellation(self.token,True,True,0)
        with patch.object(run,'square',side_effect=self.fake) as sq:
            refunds.process(run);self.assertEqual(self.job()['status'],'PENDING')
            refunds.process(run);self.assertEqual(self.job()['status'],'COMPLETED')
            refunds.process(run)
            self.assertEqual(sum(c.args[0]=='/v2/refunds' for c in sq.call_args_list),1)
    def test_insufficient_and_uncertain_result_requires_manual(self):
        run.customer_cancellation(self.token,True,True,0)
        def fail(path,body=None):
            if path=='/v2/refunds':raise RuntimeError('INSUFFICIENT_FUNDS')
            return self.fake(path,body)
        with patch.object(run,'square',side_effect=fail) as sq:
            refunds.process(run);refunds.process(run)
            self.assertEqual(self.job()['status'],'MANUAL')
            self.assertTrue(self.job()['payload'])
            self.assertEqual(sum(c.args[0]=='/v2/refunds' for c in sq.call_args_list),1)
    def test_policy_and_unpaid_direct_no_refund(self):
        c=run.con();near=(datetime.now(timezone(timedelta(hours=9)))+timedelta(days=2)).isoformat();c.execute('UPDATE reservations SET visit_at=?',(near,));c.commit();r=dict(c.execute('SELECT * FROM reservations').fetchone());c.close()
        token=run.cancellation_token(r)
        self.assertEqual(run.customer_cancellation(token)[1]['fee'],120000)
        run.customer_cancellation(token,True,True,120000)
        with patch.object(run,'square') as sq:refunds.process(run);sq.assert_not_called()
        self.assertEqual(self.job()['amount'],0)
    def test_bank_manual(self):
        c=run.con();c.execute("UPDATE reservations SET payment_source='BANK'");c.commit();c.close()
        run.customer_cancellation(self.token,True,True,0)
        with patch.object(run,'square') as sq:refunds.process(run);sq.assert_not_called()
        self.assertEqual(self.job()['status'],'MANUAL')
    def test_existing_refund_stops_new_refund(self):
        run.customer_cancellation(self.token,True,True,0)
        def existing(path,body=None):
            d=self.fake(path,body)
            if path=='/v2/payments/pay':d['payment']['refund_ids']=['old']
            return d
        with patch.object(run,'square',side_effect=existing) as sq:
            refunds.process(run)
            self.assertFalse(any(c.args[0]=='/v2/refunds' for c in sq.call_args_list))
        self.assertEqual(self.job()['status'],'MANUAL')

    def test_notice_contains_support_steps_only_for_store(self):
        run.customer_cancellation(self.token,True,True,0)
        job=self.job();job['status']='MANUAL'
        with patch.object(run,'SMTP_HOST','smtp'),patch.object(run,'SMTP_USER','u'),patch.object(run,'SMTP_PASS','p'),patch.object(run,'MAIL_FROM','store@example.com'),patch.object(run.smtplib,'SMTP') as smtp:
            refunds.notify(run,job,self.r,'MANUAL')
            messages=[c.args[0] for c in smtp.return_value.__enter__.return_value.send_message.call_args_list]
            self.assertEqual(len(messages),2)
            self.assertIn('square-jp@help-messaging.squareup.com',messages[0].get_content())
            self.assertNotIn('square-jp@help-messaging.squareup.com',messages[1].get_content())
            self.assertIn('2〜7営業日',messages[1].get_content())
            refunds.notify(run,job,self.r,'MANUAL')
            self.assertEqual(smtp.return_value.__enter__.return_value.send_message.call_count,2)

    def test_direct_has_no_paid_balance(self):
        c=run.con();c.execute("UPDATE reservations SET source='DIRECT',payment_source=NULL");c.commit();c.close()
        self.assertEqual(run.customer_cancellation(self.token)[1]['paid'],0)
        run.customer_cancellation(self.token,True,True,0)
        self.assertEqual(self.job()['amount'],0)

if __name__=='__main__':unittest.main()
