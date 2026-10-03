import base64, hashlib,hmac,json,os,sqlite3,tempfile,time,unittest
from unittest.mock import patch,MagicMock
import line_bot,line_receipts as r
class RelayTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.db=self.tmp.name+'/db';self.env=patch.dict(os.environ,{'LINE_CHANNEL_SECRET':'test','LINE_CHANNEL_ACCESS_TOKEN':'token'});self.env.start();r.configure(self.db,"a"*64)
 def tearDown(self):self.env.stop();self.tmp.cleanup()
 def receive(self,mid='123',group=r.GROUP,kind='message'):
  event={'webhookEventId':'e'+mid+kind,'type':kind,'timestamp':int(time.time()*1000),'source':{'type':'group','groupId':group},'message':{'id':mid,'type':'image'},'unsend':{'messageId':mid}}
  body=json.dumps({'destination':'test','events':[event]}).encode();sig=base64.b64encode(hmac.new(b'test',body,hashlib.sha256).digest()).decode()
  return line_bot.receive(self.db,body,sig)
 def request(self,mid='123',stamp=None):
  body=json.dumps({'message_id':mid,'group_id':r.GROUP,'timestamp':stamp or int(time.time())}).encode();sig=hmac.new(b'a'*64,b'tsukiya-receipt-content-v1\n'+body,hashlib.sha256).hexdigest();return body,sig
 def test_dedup_and_allowlist(self):
  self.receive();self.receive();self.receive('124','Cother');c=sqlite3.connect(self.db);self.assertEqual(c.execute('select count(*) from receipt_relay').fetchone()[0],1);c.close()
 def test_protected_content(self):
  self.receive();body,sig=self.request()
  with patch('urllib.request.urlopen') as f:
   self.assertEqual(r.content(self.db,body,'bad')[0],403)
   self.assertEqual(r.content(self.db,*self.request(stamp=int(time.time())-600))[0],403)
   self.assertEqual(r.content(self.db,*self.request('999'))[0],404);f.assert_not_called()
   response=MagicMock();response.__enter__.return_value=response;response.read.return_value=b'\xff\xd8\xffimage';f.return_value=response
   self.assertEqual(r.content(self.db,body,sig)[0],200)
  self.receive(kind='unsend')
  self.assertEqual(r.content(self.db,body,sig)[0],404)
 def test_outbox_retries_and_redacts(self):
  self.receive()
  with patch('urllib.request.urlopen',side_effect=TimeoutError):r.drain(self.db)
  c=sqlite3.connect(self.db);self.assertEqual(c.execute('select state from receipt_relay').fetchone()[0],'pending');c.execute('update receipt_relay set next_try=0');c.commit();c.close()
  with patch('urllib.request.urlopen') as f:
   response=MagicMock();response.__enter__.return_value=response;response.status=200;f.return_value=response;r.drain(self.db);r.drain(self.db);self.assertEqual(f.call_count,1)
  c=sqlite3.connect(self.db);self.assertEqual(c.execute('select state,body,signature from receipt_relay').fetchone(),('forwarded',b'',''));c.close()
if __name__=='__main__':unittest.main()
