import base64
import hashlib
import hmac
import json
import os
import time
import http.client
import threading
from http.server import ThreadingHTTPServer
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
        c=run.con();c.execute("UPDATE reservations SET staff_seen_at='seen'");c.commit();c.close()
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()
    def job(self):
        c=run.con();r=c.execute('SELECT * FROM cancellation_refunds').fetchone();c.close();return dict(r) if r else None
    def approve(self):
        self.assertEqual(self.approve_amount(120000), 200)

    def approve_amount(self, amount):
        c = run.con()
        try:
            with c:
                return refunds.approve(c, self.r["id"], amount, run.now_iso())[0]
        finally:
            c.close()

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
        c=run.con();self.assertIsNone(c.execute('SELECT staff_seen_at FROM reservations').fetchone()[0]);c.close()
        self.approve()
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
        self.approve()
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
    def test_policy_switches_at_japan_midnight_three_days_before(self):
        c=run.con();c.execute("UPDATE reservations SET visit_at='2026-11-11T18:00:00+09:00'");c.commit();r=dict(c.execute('SELECT * FROM reservations').fetchone());c.close()
        token=run.cancellation_token(r)
        for instant, expected in [('2026-11-07T23:59:59+09:00',0),('2026-11-08T00:00:00+09:00',120000),('2026-11-11T17:59:59+09:00',120000)]:
            class Clock(datetime):
                @classmethod
                def now(cls,tz=None):
                    return datetime.fromisoformat(instant).astimezone(tz)
            with self.subTest(instant=instant), patch.object(run,'datetime',Clock):
                self.assertEqual(run.customer_cancellation(token)[1]['fee'],expected)

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
        self.approve()
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

    def test_no_approval_means_no_square_calls(self):
        run.customer_cancellation(self.token,True,True,0)
        with patch.object(run,'square') as sq:
            refunds.process(run)
            sq.assert_not_called()
        self.assertEqual(self.job()['status'],'AWAITING_APPROVAL')

    def test_wrong_amount_and_duplicate_approval(self):
        run.customer_cancellation(self.token,True,True,0)
        self.assertEqual(self.approve_amount(1),409)
        self.approve()
        self.assertEqual(self.approve_amount(120000),409)
        self.assertEqual(self.job()['approved_by'],'朝倉')

    def test_legacy_queue_waits_and_ambiguous_submission_not_resent(self):
        run.customer_cancellation(self.token,True,True,0)
        refunds.update(run,self.r['id'],status='QUEUED')
        with patch.object(run,'square') as sq:
            refunds.process(run)
            sq.assert_not_called()
        self.assertEqual(self.job()['status'],'AWAITING_APPROVAL')
        refunds.update(run,self.r['id'],status='SUBMITTING',payload='{}')
        with patch.object(run,'square') as sq:
            refunds.process(run)
            sq.assert_not_called()
        self.assertEqual(self.job()['status'],'MANUAL')

    @patch.dict(os.environ, {'LINE_CHANNEL_SECRET':'test-line-secret','LINE_CHANNEL_ACCESS_TOKEN':'test-token'})
    def test_http_request_only_queues_line_and_signed_owner_postback_approves(self):
        run.customer_cancellation(self.token,True,True,0)
        owner = 'U' + 'a'*32
        c = run.concierge.connect(run.DB)
        with c:
            run.concierge.put(c, 'owner', owner)
        c.close()
        server=ThreadingHTTPServer(('127.0.0.1',0),run.Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()

        def request(auth, action):
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
            headers={'Content-Type':'application/json'}
            if auth: headers['x-admin-token']=run.ADMIN_TOKEN
            if action: headers['X-Tsukiya-Action']='request-refund-approval'
            conn.request('POST',f'/api/refunds/{self.r["id"]}/approval-request',json.dumps({'amount':120000}),headers)
            response=conn.getresponse();status=response.status;response.read();conn.close();return status

        def postback(event_id, user, data, source_type='user', valid_signature=True):
            raw=json.dumps({'events':[{'type':'postback','webhookEventId':event_id,
                 'source':{'type':source_type,'userId':user},'postback':{'data':data}}]}).encode()
            signature=base64.b64encode(hmac.new(b'test-line-secret',raw,hashlib.sha256).digest()).decode()
            return run.line_bot.receive(run.DB,raw,signature if valid_signature else 'invalid')[0]

        try:
            self.assertEqual(request(False,True),401)
            self.assertEqual(request(True,False),403)
            self.assertEqual(request(True,True),202)
            self.assertEqual(request(True,True),429)
            c = run.concierge.connect(run.DB)
            row=c.execute("SELECT user_id,payload FROM concierge_outbox WHERE kind='refund-approval'").fetchone()
            c.close()
            self.assertEqual(row['user_id'],owner)
            data=json.loads(row['payload'])[0]['template']['actions'][0]['data']
            self.assertEqual(postback('invalid',owner,data,valid_signature=False),403)
            self.assertEqual(postback('staff','U'+'b'*32,data),200)
            self.assertEqual(postback('group',owner,data,'group'),200)
            self.assertEqual(postback('expired',owner,data.rsplit(':',1)[0]+':'+str(int(time.time())-901)),200)
            self.assertEqual(self.job()['status'],'AWAITING_APPROVAL')
            with patch.object(run,'square') as sq:
                self.assertEqual(postback('approve',owner,data),200)
                self.assertEqual(postback('approve',owner,data),200)
                sq.assert_not_called()
            self.assertEqual(self.job()['status'],'QUEUED')
            with patch.object(run,'square',side_effect=self.fake):
                refunds.process(run)
            self.assertEqual(self.job()['status'],'PENDING')
        finally:
            server.shutdown();server.server_close()

if __name__=='__main__':unittest.main()

