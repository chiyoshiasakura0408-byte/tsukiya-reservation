"""Fake Square, temporary DB, no external notification or real payment."""
import concurrent.futures
import json
import os
from pathlib import Path
import secrets
import tempfile
import unittest
import uuid
from unittest.mock import patch
from cryptography.fernet import Fernet
os.environ.setdefault('DATA_DIR',tempfile.mkdtemp(prefix='checkout-import-'))
import run
import web_checkout as wc

class CheckoutTest(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  for key,value in {'DB':Path(self.temp.name)/'test.sqlite','SQUARE_ENV':'sandbox','SQUARE_TOKEN':'test','SQUARE_LOCATION_ID':'JP-TEST','SQUARE_EN_LOCATION_ID':'EN-TEST','SQUARE_WEBHOOK_SIGNATURE_KEY':'test','APP_BASE_URL':'https://checkout.test'}.items():
   p=patch.object(run,key,value);p.start();self.addCleanup(p.stop)
  p=patch.dict(os.environ,{'WEB_CARD_CHECKOUT_ENABLED':'1','SQUARE_APPLICATION_ID':'sandbox-app','CHECKOUT_TOKEN_ENCRYPTION_KEY':Fernet.generate_key().decode()});p.start();self.addCleanup(p.stop)
  self.payments={};self.bodies=[];self.mode='success'
  p=patch.object(run,'square',side_effect=self.gateway);self.square=p.start();self.addCleanup(p.stop)
  p=patch.object(run,'deliver_confirmation',side_effect=lambda r:r);self.notice=p.start();self.addCleanup(p.stop)
  p=patch.object(run.guest_service,'payment_event');p.start();self.addCleanup(p.stop)
  c=run.con();c.close()
 def gateway(self,path,body=None,method=None):
  if path=='/v2/orders':return {'order':{'id':'order-'+body['idempotency_key'],'location_id':body['order']['location_id']}}
  if path=='/v2/payments':
   self.bodies.append(body.copy())
   if self.mode=='decline':raise RuntimeError(json.dumps({'errors':[{'code':'CARD_DECLINED'}]}))
   if self.mode=='before-timeout':raise TimeoutError()
   key=body['idempotency_key']
   if key not in self.payments:self.payments[key]={'id':'payment-'+key,'status':'COMPLETED','source_type':'CARD','reference_id':body['reference_id'],'order_id':body['order_id'],'location_id':body['location_id'],'amount_money':body['amount_money'],'total_money':body['amount_money']}
   if self.mode=='after-timeout':raise TimeoutError()
   return {'payment':self.payments[key]}
  if path.startswith('/v2/payments/'):return {'payment':next(p for p in self.payments.values() if p['id']==path.rsplit('/',1)[-1])}
  raise AssertionError(path)
 def data(self,**extra):
  x={'request_id':str(uuid.uuid4()),'checkout_secret':secrets.token_hex(32),'date':'2027-01-15','time':'18:00','seating_area':'PRIVATE1','party_size':2,'guest_name':'Sandbox Guest','email':uuid.uuid4().hex+'@example.invalid','phone':'09012345678','cancellation_policy_accepted':True,'course':'matsuba-fukahire'};x.update(extra);return x
 def book(self,**extra):
  x=self.data(**extra);return x,wc.create(run,x)
 def row(self,rid):
  c=run.con()
  try:return dict(c.execute('SELECT * FROM reservations WHERE id=?',(rid,)).fetchone())
  finally:c.close()
 def lease(self):
  c=run.con();c.execute('UPDATE web_checkouts SET lease_until=0');c.commit();c.close()
 def test_server_price_full_confirmation(self):
  x,s=self.book(amount=1,location_id='attacker');self.assertEqual(s['amount'],132000)
  self.assertEqual(wc.pay(run,s['id'],x['checkout_secret'],'cnon:test-token')['state'],'PAID')
  r=self.row(s['reservation_id']);self.assertEqual((r['status'],r['payment_source']),('CONFIRMED','SQUARE'))
  c=run.con();self.assertEqual(c.execute('SELECT count(*) FROM payment_audit').fetchone()[0],1);self.assertIsNone(c.execute('SELECT encrypted_request FROM web_checkouts').fetchone()[0]);c.close();self.notice.assert_called_once()
  self.assertFalse(any('/invoices' in v.args[0] for v in self.square.call_args_list))
 def test_duplicate_request_payload_and_secret(self):
  x,s=self.book();self.assertEqual(wc.create(run,x)['id'],s['id'])
  for change in ({'guest_name':'changed'},{'checkout_secret':secrets.token_hex(32)}):
   with self.assertRaises(wc.CheckoutError):wc.create(run,{**x,**change})
 def test_duplicate_payment_and_webhooks(self):
  x,s=self.book();wc.pay(run,s['id'],x['checkout_secret'],'cnon:test-token');wc.pay(run,s['id'],x['checkout_secret'],'cnon:new-test-token')
  p=next(iter(self.payments.values()));wc.finalize(run,p);wc.finalize(run,p)
  self.assertEqual(len(self.bodies),1);self.notice.assert_called_once()
 def test_timeout_after_payment_replays_exact_request(self):
  x,s=self.book();self.mode='after-timeout';self.assertEqual(wc.pay(run,s['id'],x['checkout_secret'],'cnon:test-token')['state'],'UNKNOWN')
  self.assertEqual(self.row(s['reservation_id'])['status'],'PENDING')
  with self.assertRaises(wc.CheckoutError):wc.abandon(run,s['id'],x['checkout_secret'])
  self.mode='success';self.lease();wc.reconcile(run)
  self.assertEqual(wc.status(run,s['id'],x['checkout_secret'])['state'],'PAID');self.assertEqual(self.bodies[0],self.bodies[1]);self.assertEqual(len(self.payments),1)
 def test_timeout_before_payment_keeps_seat_after_expiry(self):
  x,s=self.book();self.mode='before-timeout';wc.pay(run,s['id'],x['checkout_secret'],'cnon:test-token')
  c=run.con();c.execute('UPDATE web_checkouts SET expires=0,lease_until=0');c.commit();c.close();self.assertEqual(wc.expire(run),0)
  self.mode='success';wc.reconcile(run);self.assertEqual(self.row(s['reservation_id'])['status'],'CONFIRMED')
 def test_decline_releases_seat_erases_token(self):
  x,s=self.book();self.mode='decline';self.assertEqual(wc.pay(run,s['id'],x['checkout_secret'],'cnon:test-token')['state'],'FAILED');self.assertEqual(self.row(s['reservation_id'])['status'],'CANCELLED')
  c=run.con();self.assertIsNone(c.execute('SELECT encrypted_request FROM web_checkouts').fetchone()[0]);c.close();self.book()
 def test_hold_and_legacy_expiry(self):
  x,s=self.book();run.expire_public_reservations();self.assertEqual(self.row(s['reservation_id'])['status'],'PENDING')
  c=run.con();c.execute('UPDATE web_checkouts SET expires=0');c.commit();c.close();wc.expire(run)
  self.assertEqual(wc.pay(run,s['id'],x['checkout_secret'],'cnon:test-token')['state'],'EXPIRED');self.assertEqual(self.square.call_count,0)
 def test_concurrent_last_room_one_winner(self):
  def create(_):
   try:return wc.create(run,self.data())['id']
   except wc.CheckoutError:return None
  with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:result=list(pool.map(create,range(4)))
  self.assertEqual(sum(x is not None for x in result),1)
 def test_rules_and_english_card_only(self):
  for change in ({'vip_through':True},{'payment_method':'BANK'},{'party_size':True},{'cancellation_policy_accepted':False},{'date':'2026-11-15','seating_area':'COUNTER'}):
   with self.assertRaises(wc.CheckoutError):wc.create(run,self.data(**change))
 def test_english_server_location(self):
  x,s=self.book(booking_language='en');wc.pay(run,s['id'],x['checkout_secret'],'cnon:test-token');self.assertEqual(self.bodies[0]['location_id'],'EN-TEST')
 def test_wrong_payment_amount_cannot_confirm(self):
  x,s=self.book();self.mode='after-timeout';wc.pay(run,s['id'],x['checkout_secret'],'cnon:test-token');p=next(iter(self.payments.values()));p['amount_money']={'amount':1,'currency':'JPY'}
  with self.assertRaises(ValueError):wc.finalize(run,p)
  self.assertEqual(self.row(s['reservation_id'])['status'],'PENDING')
 def test_feature_off_existing_can_finish(self):
  x,s=self.book()
  with patch.dict(os.environ,{'WEB_CARD_CHECKOUT_ENABLED':'0'}):
   with self.assertRaises(wc.CheckoutError):wc.create(run,self.data())
   self.assertEqual(wc.pay(run,s['id'],x['checkout_secret'],'cnon:test-token')['state'],'PAID')
 def test_resume_abandon(self):
  x,s=self.book();self.assertEqual(wc.lookup(run,x['request_id'],x['checkout_secret'])['id'],s['id']);wc.abandon(run,s['id'],x['checkout_secret']);self.assertEqual(self.row(s['reservation_id'])['status'],'CANCELLED');self.assertEqual(self.square.call_count,0)
 def test_no_invoice_or_confirmation_for_hold(self):
  x,s=self.book()
  with self.assertRaises(ValueError):run.make_invoice(self.row(s['reservation_id']))
  self.notice.assert_not_called()
 def test_no_secret_in_response(self):
  x,s=self.book();self.assertNotIn(x['checkout_secret'],json.dumps(s))
  with self.assertRaises(wc.CheckoutError):wc.status(run,s['id'],secrets.token_hex(32))
 def test_production_gate(self):
  with patch.object(run,'SQUARE_ENV','production'),patch.dict(os.environ,{'WEB_CARD_PRODUCTION_APPROVED':'0'}):self.assertFalse(wc.config(run)['enabled'])

 def test_cash_external_and_saved_card_sources_are_rejected(self):
  x,s=self.book()
  for source in ('CASH','EXTERNAL','ccof:saved-card'):
   with self.assertRaises(wc.CheckoutError):wc.pay(run,s['id'],x['checkout_secret'],source)
  self.assertEqual(self.square.call_count,0)

 def test_noncard_completed_payment_cannot_confirm(self):
  x,s=self.book();self.mode='after-timeout';wc.pay(run,s['id'],x['checkout_secret'],'cnon:test-token')
  p=next(iter(self.payments.values()));p['source_type']='CASH'
  with self.assertRaises(ValueError):wc.finalize(run,p)
  self.assertEqual(self.row(s['reservation_id'])['status'],'PENDING')

 def http(self,path,data=None,origin='https://checkout.test'):
  import http.client
  import threading
  from http.server import ThreadingHTTPServer
  server=ThreadingHTTPServer(('127.0.0.1',0),run.Handler)
  threading.Thread(target=server.serve_forever,daemon=True).start()
  try:
   conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
   body=json.dumps(data) if data is not None else None
   conn.request('POST' if data is not None else 'GET',path,body,{'Origin':origin,'Content-Type':'application/json','x-admin-token':'checkout-test-admin'})
   result=conn.getresponse();raw=result.read();headers=dict(result.getheaders());status=result.status;conn.close()
   return status,headers,raw
  finally:server.shutdown();server.server_close()

 def test_http_checkout_and_cross_origin_rejection(self):
  x=self.data();code,headers,raw=self.http('/api/public/checkouts',x,origin='https://untrusted.invalid');self.assertEqual(code,403)
  code,headers,raw=self.http('/api/public/checkouts',x);self.assertEqual(code,200,raw);s=json.loads(raw)
  code,_,raw=self.http('/api/public/checkouts/'+s['id']+'/pay',{'checkout_secret':x['checkout_secret'],'source_id':'cnon:test-token'});self.assertEqual(code,200,raw);self.assertEqual(json.loads(raw)['state'],'PAID')

 def test_public_page_csp_and_sdk_file(self):
  for path in ('/book','/book?lang=en'):
   code,headers,raw=self.http(path);self.assertEqual(code,200);csp=headers['Content-Security-Policy'];self.assertIn('sandbox.web.squarecdn.com',csp);self.assertIn("'nonce-",csp);self.assertIn(b'/web-checkout.js',raw)
  self.assertEqual(self.http('/web-checkout.js')[0],200)

 def test_owner_hold_notices_wait_for_confirmation(self):
  x,s=self.book();c=run.concierge.connect(run.DB)
  with c:
   run.concierge.put(c,'owner','U'+'a'*32)
   run.concierge.queue_new_web_reservations(c)
  self.assertEqual(c.execute("SELECT count(*) FROM concierge_outbox WHERE kind='owner-new-booking'").fetchone()[0],0);c.close()
  wc.pay(run,s['id'],x['checkout_secret'],'cnon:test-token')
  c=run.concierge.connect(run.DB)
  with c:run.concierge.queue_new_web_reservations(c)
  self.assertEqual(c.execute("SELECT count(*) FROM concierge_outbox WHERE kind='owner-new-booking'").fetchone()[0],1);c.close()

 def test_refund_uses_direct_payment_and_english_location(self):
  import refunds
  x,s=self.book(booking_language='en');wc.pay(run,s['id'],x['checkout_secret'],'cnon:test-token');r=self.row(s['reservation_id'])
  c=run.con();c.execute("UPDATE reservations SET status='CANCELLED' WHERE id=?",(r['id'],));refunds.enqueue(c,r,0,run.now_iso());c.commit();c.close()
  original=self.gateway;refund_calls=[]
  def gateway(path,body=None,method=None):
   if path=='/v2/refunds':refund_calls.append(body);return {'refund':{'id':'test-refund','status':'COMPLETED'}}
   return original(path,body,method)
  with patch.object(run,'REFUND_APPROVAL_SECRET','owner-only-checkout-test-secret'),patch.object(refunds,'notify'),patch.object(run,'square',side_effect=gateway):
   self.assertEqual(refunds.approve(run,r['id'],run.REFUND_APPROVAL_SECRET,132000)[0],200)
   refunds.process(run);refunds.process(run)
  self.assertEqual(len(refund_calls),1);self.assertEqual(refund_calls[0]['payment_id'],next(iter(self.payments.values()))['id'])

 def test_admin_cannot_cancel_or_invoice_unknown_payment(self):
  x,s=self.book();self.mode='after-timeout';wc.pay(run,s['id'],x['checkout_secret'],'cnon:test-token')
  with patch.object(run,'ADMIN_TOKEN','checkout-test-admin'):
   self.assertEqual(self.http('/api/reservations/'+str(s['reservation_id'])+'/cancel',{})[0],409)
   self.assertEqual(self.http('/api/reservations/'+str(s['reservation_id'])+'/send-invoice',{})[0],409)
  self.assertEqual(self.row(s['reservation_id'])['status'],'PENDING')

 def test_csp_allows_3ds_form_and_preserves_script_nonce(self):
  sandbox=wc.csp('testnonce',True);production=wc.csp('testnonce',False)
  self.assertIn("form-action 'self' https://api.squareupsandbox.com",sandbox)
  self.assertIn("frame-src 'self' https://sandbox.web.squarecdn.com https://api.squareupsandbox.com",sandbox)
  self.assertIn("frame-src 'self' https:",production)
  scripts=production.split('script-src ')[1].split(';')[0]
  self.assertIn("'nonce-testnonce'",scripts)
  self.assertNotIn('unsafe-inline',scripts);self.assertNotIn('unsafe-eval',scripts)
