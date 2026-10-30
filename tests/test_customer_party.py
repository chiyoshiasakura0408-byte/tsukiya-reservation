import json
import os
import tempfile
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

os.environ.setdefault('DATA_DIR', tempfile.mkdtemp())
import run
import customer_party


class CustomerPartyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [patch.object(run,'DB',self.tmp.name+'/db'),
                        patch.object(run,'ADMIN_TOKEN','test-only-token')]
        for p in self.patches: p.start()
        self.visit = '2099-11-14T18:00'
        c = run.con()
        c.execute('''INSERT INTO reservations(id,source,guest_name,email,visit_at,
            party_size,seating_area,amount,status,payment_method,bank_payer,bank_due_at,
            bank_email_status,created_at,updated_at,duration_minutes,course_name)
            VALUES(67,'WEB','guest','guest@example.com',?,4,'PRIVATE1',264000,
                'PENDING','BANK','payer','2099-10-31T21:02:14+09:00','SENT',
                'created','updated',150,'松葉蟹')''', (self.visit,))
        c.commit(); c.close()
        self.token = run.cancellation_token(self.row())

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.tmp.cleanup()

    def row(self):
        c=run.con()
        try: return dict(c.execute('SELECT * FROM reservations WHERE id=67').fetchone())
        finally: c.close()

    def update(self, sql, args=()):
        c=run.con(); c.execute(sql,args); c.commit(); c.close()

    def preview(self, party=2):
        return customer_party.handle(run, {'token':self.token,'party_size':party})

    def confirm_data(self, party=2):
        status, data=self.preview(party); self.assertEqual(status,200,data)
        return dict(token=self.token,party_size=party,accepted=True,
                    quote=data['quote'],issued=data['issued'],request_id=str(uuid.uuid4()))

    def change(self, party=2):
        return customer_party.handle(run,self.confirm_data(party),True)

    def paid(self, source='SQUARE'):
        self.update("UPDATE reservations SET status='CONFIRMED',payment_source=?,payment_confirmed_at='paid'",(source,))

    def test_minimum_two_server_validation(self):
        for party in [0,1,-1,9,2.0,'2',True,None]:
            if party is None: continue  # no party is a read-only lookup
            with self.subTest(party=party):
                self.assertEqual(self.preview(party)[0],400)
        self.assertEqual(self.row()['party_size'],4)

    def test_bank_update_preserves_identity_terms_and_never_sends_money(self):
        before=self.row()
        with patch.object(run,'square') as square,patch.object(run.smtplib,'SMTP') as smtp:
            status,data=self.change(2)
            self.assertEqual(status,200,data)
            square.assert_not_called();smtp.assert_not_called()
        after=self.row()
        self.assertEqual((after['party_size'],after['amount']),(2,132000))
        for key in before.keys()-{'party_size','amount','staff_seen_at','updated_at'}:
            self.assertEqual(before[key],after[key],key)
        self.assertEqual(data['balance'],0)

    def test_paid_reduction_refund_and_increase_due_are_immediate(self):
        for source in ['SQUARE','BANK']:
            with self.subTest(source=source):
                self.paid(source)
                status,data=self.change(2)
                self.assertEqual((status,data['party_size'],data['balance']),(200,2,-132000))
                # A second change uses actual paid balance, not the previous proposed amount.
                status,data=self.change(3)
                self.assertEqual((status,data['amount'],data['balance']),(200,198000,-66000))
                status,data=self.change(4)
                self.assertEqual((status,data['amount'],data['balance']),(200,264000,0))
        self.change(2)
        status,data=self.change(4)
        self.assertEqual(data['balance'],0)

    def test_paid_increase_tracks_signed_balance_and_settlement(self):
        self.update("UPDATE reservations SET party_size=2,amount=132000")
        self.paid()
        self.assertEqual(self.change(4)[1]['balance'],132000)
        c=run.con()
        try:
            row=customer_party.latest(c,67)
            with self.assertRaises(ValueError):
                customer_party.settle(run,c,67,{'change_id':row['id'],'expected_balance':1,'owner_confirmed':True})
            self.assertEqual(customer_party.settle(run,c,67,{'change_id':row['id'],'expected_balance':132000,'owner_confirmed':True})['ok'],True)
            c.commit()
            self.assertFalse(customer_party.open_balance(c,67))
        finally:c.close()
        self.assertEqual(self.change(3)[1]['balance'],-66000)

    def test_fee_boundary_and_fee_retention(self):
        self.paid()
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):
                return datetime.fromisoformat('2099-11-11T00:00:00+09:00').astimezone(tz)
        with patch.object(run,'datetime',Clock):
            status,data=self.preview(2)
            self.assertEqual(data['proposal']['fee'],132000)
            status,data=self.change(2)
            self.assertEqual((data['amount'],data['fee_total'],data['balance']),(264000,132000,0))
            self.assertEqual(self.change(4)[1]['amount'],396000)
        self.assertEqual(self.row()['party_size'],4)

    def test_invalid_token_quote_consent_stale_and_retry(self):
        self.assertEqual(customer_party.handle(run,{'token':self.token+'x'})[0],403)
        body=self.confirm_data(2)
        for extra in [{'accepted':False},{'quote':'fake'},{'party_size':1},{'issued':0}]:
            self.assertGreaterEqual(customer_party.handle(run,{**body,**extra},True)[0],400)
        self.update("UPDATE reservations SET updated_at='another update'")
        self.assertEqual(customer_party.handle(run,body,True)[0],409)
        body=self.confirm_data(2)
        self.assertEqual(customer_party.handle(run,body,True)[0],200)
        self.assertTrue(customer_party.handle(run,body,True)[1]['duplicate'])
        c=run.con();self.assertEqual(c.execute('SELECT count(*) FROM customer_party_changes').fetchone()[0],1);c.close()

    def test_capacity_and_blocks_rechecked_after_preview(self):
        self.assertEqual(self.preview(5)[0],409)
        self.update("UPDATE reservations SET party_size=2,amount=132000,seating_area='COUNTER',counter_round=1")
        body=self.confirm_data(4)
        c=run.con();c.execute("INSERT INTO reservations(source,guest_name,visit_at,party_size,seating_area,counter_round,amount,status,created_at,updated_at) VALUES('DIRECT','other',?,6,'COUNTER',1,396000,'CONFIRMED','c','u')",(self.visit,));c.commit();c.close()
        self.assertEqual(customer_party.handle(run,body,True)[0],409)
        self.assertEqual(self.row()['party_size'],2)
        self.update("UPDATE reservations SET seating_area='PRIVATE3',party_size=4,amount=264000 WHERE id=67")
        self.assertEqual(self.preview(2)[0],409)

    def test_cancelled_expired_and_checkout_pending_rejected(self):
        self.update("UPDATE reservations SET status='CANCELLED'")
        self.assertEqual(self.preview(2)[0],409)
        self.update("UPDATE reservations SET status='PENDING',payment_method='CARD',source='WEB_CARD'")
        self.assertEqual(self.preview(2)[0],409)
        self.update("UPDATE reservations SET status='CONFIRMED',visit_at='2000-01-01T18:00'")
        self.token=run.cancellation_token(self.row())
        self.assertEqual(self.preview(2)[0],410)

    def test_cancellation_waits_for_balance_and_adjusted_refunds_are_manual(self):
        self.paid()
        self.change(2)
        self.assertEqual(run.customer_cancellation(self.token)[0],409)
        c=run.con();row=customer_party.latest(c,67)
        customer_party.settle(run,c,67,{'change_id':row['id'],'expected_balance':-132000,'owner_confirmed':True});c.commit();c.close()
        self.assertEqual(run.customer_cancellation(self.token,True,True,0)[0],200)
        c=run.con();row=c.execute('SELECT * FROM cancellation_refunds').fetchone()
        self.assertEqual((row['amount'],row['status']),(132000,'MANUAL'));c.close()

    def test_http_authorization_and_cache_headers(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),run.Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        def request(path,body=None,headers=None):
            req=Request(f'http://127.0.0.1:{server.server_port}'+path,
                data=json.dumps(body).encode() if body is not None else None,
                headers={'Content-Type':'application/json',**(headers or {})})
            try:
                with urlopen(req) as response:return response.status,json.load(response),response.headers
            except HTTPError as error:return error.code,json.load(error),error.headers
        try:
            path='/api/public/party-change/'
            status,data,headers=request(path+'preview',{'token':self.token,'party_size':2})
            self.assertEqual(status,200);self.assertEqual(headers['Cache-Control'],'no-store')
            body=dict(token=self.token,party_size=2,accepted=True,request_id=str(uuid.uuid4()),quote=data['quote'],issued=data['issued'])
            self.assertEqual(request(path+'confirm',body)[0],200)
            self.assertEqual(request('/api/customer-party-changes')[0],401)
            self.assertEqual(request('/api/reservations/67/party-settlement',{})[0],401)
            self.assertEqual(request('/api/reservations/67/party-settlement',{}, {'x-admin-token':'test-only-token'})[0],403)
        finally:server.shutdown();server.server_close()
