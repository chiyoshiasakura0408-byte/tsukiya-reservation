import os,tempfile,unittest,threading,json,uuid,urllib.request,urllib.error
from pathlib import Path
from datetime import datetime,timedelta,timezone
from http.server import ThreadingHTTPServer
from unittest.mock import patch
os.environ.setdefault('DATA_DIR',tempfile.mkdtemp())
import run,concierge,bank_transfer,line_receipts

class BankTransferTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.old=run.DB;run.DB=Path(self.tmp.name)/'test.sqlite';run.con().close()
  c=concierge.connect(run.DB);concierge.put(c,'owner','U'+'1'*32);c.commit();c.close()
  self.env=patch.dict(os.environ,{'LINE_CHANNEL_ACCESS_TOKEN':'test-only'});self.env.start()
  self.token=patch.object(run,'ADMIN_TOKEN','test-secret');self.token.start()
  self.receipt=patch.object(bank_transfer,'receipt');self.receipt.start()
  self.invoice=patch.object(run,'make_invoice',return_value=('c','o','i','https://square.example/pay'));self.mock_invoice=self.invoice.start()
  self.server=ThreadingHTTPServer(('127.0.0.1',0),run.Handler);threading.Thread(target=self.server.serve_forever,daemon=True).start()
  today=datetime.now(timezone(timedelta(hours=9))).date();self.day=next(today+timedelta(days=i) for i in range(1,365) if run.public_slot_allowed(today+timedelta(days=i),'18:00'))
  self.body={'date':str(self.day),'time':'18:00','seating_area':'COUNTER','party_size':2,'guest_name':'テスト','email':'test@example.com','phone':'09012345678','request_id':str(uuid.uuid4()),'cancellation_policy_accepted':True,'payment_method':'BANK','bank_payer':'テスト','bank_planned_date':''}
 def tearDown(self):
  self.server.shutdown();self.server.server_close();self.invoice.stop();self.receipt.stop();self.env.stop();self.token.stop();run.DB=self.old;self.tmp.cleanup()
 def call(self,path,body=None,auth=False):
  headers={'Content-Type':'application/json'}
  if auth:headers['x-admin-token']='test-secret'
  req=urllib.request.Request(f'http://127.0.0.1:{self.server.server_port}'+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
  try:
   with urllib.request.urlopen(req) as r:return r.status,json.load(r)
  except urllib.error.HTTPError as e:return e.code,json.load(e)
 def book(self):
  code,res=self.call('/api/public/reservations',self.body);self.assertEqual(code,200,res);return res['reservation_id']
 def test_bank_booking_atomic_idempotent_targets_and_hold(self):
  rid=self.book();self.assertEqual(self.book(),rid);self.mock_invoice.assert_not_called()
  c=run.con();r=dict(c.execute('SELECT * FROM reservations').fetchone());self.assertEqual((r['status'],r['payment_method'],r['payment_source'],r['square_invoice_id']),('PENDING','BANK',None,None));self.assertEqual(r['amount'],120000)
  targets={x[0] for x in c.execute("SELECT user_id FROM concierge_outbox WHERE kind='bank-transfer'")};self.assertEqual(targets,{'U'+'1'*32});self.assertEqual(c.execute('SELECT count(*) FROM concierge_outbox').fetchone()[0],1)
  self.assertFalse(run.availability_check(c,'COUNTER',str(self.day)+'T18:00',8,1)[0]);c.close()
  code,unpaid=self.call('/api/unpaid-invoices',auth=True);self.assertEqual(unpaid[0]['square_status'],'BANK_PENDING')
 def test_confirm_requires_auth_amount_audit_and_no_square(self):
  rid=self.book();url=f'/api/reservations/{rid}/confirm-bank-payment';body={'amount':120000,'reference':'銀行明細照合','confirmed_by':'担当者'}
  self.assertEqual(self.call(url,body)[0],401);self.assertEqual(self.call(url,{**body,'amount':1},True)[0],409)
  with patch.object(run,'square') as square,patch.object(run,'deliver_confirmation',side_effect=lambda r:r):
   self.assertEqual(self.call(url,body,True)[0],200);self.assertEqual(self.call(url,body,True)[0],409);square.assert_not_called()
  c=run.con();r=c.execute('SELECT * FROM reservations').fetchone();self.assertEqual((r['status'],r['payment_source']),('CONFIRMED','BANK'));self.assertEqual(c.execute('SELECT count(*) FROM payment_audit').fetchone()[0],1);self.assertEqual(c.execute("SELECT count(*) FROM bank_notices WHERE stage='confirmed'").fetchone()[0],1);c.close()
 def test_overdue_requests_review_not_false_cancellation(self):
  rid=self.book();c=run.con();old=(datetime.now(timezone.utc)-timedelta(hours=49)).isoformat();c.execute('UPDATE reservations SET created_at=?,bank_due_at=?',(old,old));c.commit();c.close();run.expire_public_reservations();bank_transfer.overdue(run);bank_transfer.overdue(run)
  c=run.con();self.assertEqual(c.execute('SELECT status FROM reservations').fetchone()[0],'PENDING');self.assertEqual(c.execute("SELECT count(*) FROM bank_notices WHERE stage='overdue'").fetchone()[0],1);c.close()
 def test_invalid_and_unlinked_rejected_without_booking(self):
  for changes in [{'payment_method':'OTHER'},{'bank_payer':''},{'bank_planned_date':'2028-01-01'}]:self.assertEqual(self.call('/api/public/reservations',{**self.body,**changes})[0],400)
  c=concierge.connect(run.DB);concierge.put(c,'owner','');c.commit();c.close();self.assertEqual(self.call('/api/public/reservations',self.body)[0],503)
  c=run.con();self.assertEqual(c.execute('SELECT count(*) FROM reservations').fetchone()[0],0);c.close()
 def test_cancel_and_invoice_guard(self):
  rid=self.book();self.assertEqual(self.call(f'/api/reservations/{rid}/cancel',{},True)[0],200)
  c=run.con();self.assertEqual(c.execute('SELECT status FROM reservations').fetchone()[0],'CANCELLED');self.assertEqual(c.execute("SELECT count(*) FROM bank_notices WHERE stage='cancelled'").fetchone()[0],1);c.close()
  self.invoice.stop()
  with self.assertRaises(ValueError):run.make_invoice({'payment_method':'BANK'})
  self.invoice.start()
 def test_card_path_retained(self):
  with patch.object(run,'SQUARE_TOKEN','test'),patch.object(run,'SQUARE_LOCATION_ID','location'):
   code,r=self.call('/api/public/reservations',{**self.body,'payment_method':'CARD'});self.assertEqual(code,200,r);self.assertIn('payment_url',r);self.mock_invoice.assert_called_once()
  c=run.con();self.assertEqual(c.execute('SELECT count(*) FROM bank_notices').fetchone()[0],0);c.close()
 def test_notification_delivery_uses_both_destinations_once(self):
  from unittest.mock import MagicMock
  self.book();response=MagicMock();response.__enter__.return_value=response;response.status=200;response.read.return_value=b'{}'
  with patch('urllib.request.urlopen',return_value=response) as send:
   concierge.deliver(run.DB);concierge.deliver(run.DB)
   self.assertEqual(send.call_count,1)
   self.assertEqual({json.loads(call.args[0].data)['to'] for call in send.call_args_list},{'U'+'1'*32})
 def test_admin_cannot_invoice_bank_booking(self):
  rid=self.book();self.assertEqual(self.call(f'/api/reservations/{rid}/send-invoice',{},True)[0],409);self.mock_invoice.assert_not_called()
  c=run.con();self.assertEqual(c.execute('SELECT status FROM reservations').fetchone()[0],'PENDING');c.close()
 def test_receipt_sends_bank_instructions_once(self):
  rid=self.book();self.receipt.stop();c=run.con();r=dict(c.execute('SELECT * FROM reservations').fetchone());c.close()
  with patch.object(run,'SMTP_HOST','smtp.test'),patch.object(run,'SMTP_USER','test'),patch.object(run,'SMTP_PASS','test'),patch.object(run,'MAIL_FROM','test@example.com'),patch.object(run.smtplib,'SMTP') as smtp:
   bank_transfer.receipt(run,r);bank_transfer.receipt(run,r)
   send=smtp.return_value.__enter__.return_value.send_message;self.assertEqual(send.call_count,1);self.assertIn('入金確認待ち',send.call_args.args[0].get_content());self.assertIn('0655295',send.call_args.args[0].get_content())
  self.receipt.start()

 def test_accounting_digest_pending_only_and_morning_once(self):
  import line_delivery as d
  rid=self.book()
  digest=bank_transfer.accounting_digest(run.DB,'https://example.com')
  self.assertEqual(len(digest),1);self.assertIn('未確認 1件',digest[0]['text']);self.assertIn('120,000円',digest[0]['text']);self.assertIn('テスト',digest[0]['text'])
  c=d.connect(run.DB);c.execute('INSERT INTO line_sources VALUES(?,?,?,1)',(d.TARGET,'group','now'));c.commit();c.close()
  with patch.object(d,'api',return_value={}) as send,patch.object(d,'message',return_value={'type':'text','text':'morning'}),patch.object(d,'capture',return_value={'type':'image'}),patch.object(d,'printable_message',return_value={'type':'text','text':'pdf'}):
   d.deliver(run.DB,1,'admin','https://example.com');d.deliver(run.DB,1,'admin','https://example.com')
   self.assertEqual(send.call_count,2);payload=send.call_args.args[1];self.assertEqual(payload['to'],line_receipts.GROUP);self.assertEqual(len(payload['messages']),5);self.assertIn('朝の入金確認依頼',payload['messages'][-1]['text'])
  self.call(f'/api/reservations/{rid}/cancel',{},True)
  self.assertEqual(bank_transfer.accounting_digest(run.DB,'https://example.com'),[])
 def test_legacy_group_queue_is_not_sent_immediately(self):
  self.book();c=concierge.connect(run.DB)
  concierge.enqueue(c,line_receipts.GROUP,[{'type':'text','text':'legacy bank notice'}],channel='owner',kind='bank-transfer');c.commit();c.close()
  from unittest.mock import MagicMock
  response=MagicMock();response.__enter__.return_value=response;response.status=200;response.read.return_value=b'{}'
  with patch('urllib.request.urlopen',return_value=response) as send:
   concierge.deliver(run.DB);self.assertEqual(send.call_count,1);self.assertEqual(json.loads(send.call_args.args[0].data)['to'],'U'+'1'*32)
