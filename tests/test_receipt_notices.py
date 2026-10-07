import hashlib,hmac,json,os,sqlite3,tempfile,time,unittest
from unittest.mock import patch
import line_receipts as relay
import concierge
class ReceiptNotices(unittest.TestCase):
 def test_signed_reason_survives_queue_and_retry(self):
  with tempfile.TemporaryDirectory() as tmp:
   db=tmp+'/test.db';secret='1'*64
   relay.configure(db,secret,'test-receiver-token')
   c=concierge.connect(db)
   with c:concierge.put(c,'owner','U'+'a'*32)
   c.close()
   with sqlite3.connect(db) as c:c.execute('INSERT INTO receipt_images VALUES(?,?,?,0)',('123',relay.GROUP,time.time()))
   text='【つきや経理・予約の確認依頼】\n伝票は売上に反映済みです。'
   body=json.dumps(dict(message_id='123',group_id=relay.GROUP,timestamp=int(time.time()),action='review_notice',notice_text=text)).encode()
   signature=hmac.new(secret.encode(),b'tsukiya-receipt-content-v1\n'+body,hashlib.sha256).hexdigest()
   self.assertEqual(relay.content(db,body,'bad')[0],403)
   self.assertEqual(relay.content(db,body,signature)[0],200)
   self.assertEqual(relay.content(db,body,signature)[0],200)
   sent=[]
   class Response:
    status=200
    def __enter__(self):return self
    def __exit__(self,*args):pass
   def deliver(req,**kwargs):sent.append(json.loads(req.data));return Response()
   with patch.dict(os.environ,{'LINE_CHANNEL_ACCESS_TOKEN':'test-only'}),patch('urllib.request.urlopen',side_effect=deliver):
    relay.drain_reviews(db);relay.drain_reviews(db)
   self.assertEqual(len(sent),1)
   self.assertEqual(sent[0]['messages'][0]['text'],text)
   self.assertEqual(sent[0]['to'],'U'+'a'*32)
   self.assertNotEqual(sent[0]['to'],relay.GROUP)
 def test_existing_schema_upgrade_preserves_notices(self):
  with sqlite3.connect(':memory:') as c:
   c.execute("CREATE TABLE receipt_review_notices(message_id TEXT PRIMARY KEY,retry_key TEXT NOT NULL,created REAL NOT NULL,next_try REAL NOT NULL,state TEXT NOT NULL DEFAULT 'pending')")
   c.execute("INSERT INTO receipt_review_notices VALUES('123','retry',1,1,'sent')")
   relay.schema(c);relay.schema(c)
   self.assertEqual(c.execute('SELECT state,notice_text FROM receipt_review_notices').fetchone(),('sent',''))

 def test_no_owner_never_falls_back_to_group(self):
  with tempfile.TemporaryDirectory() as tmp:
   db=tmp+'/test.db'
   self.assertEqual(relay.review_recipient(db),'')
   with patch.dict(os.environ,{'LINE_CHANNEL_ACCESS_TOKEN':'test-only'}),patch('urllib.request.urlopen',side_effect=AssertionError('must not send to group')):
    relay.drain_reviews(db)
