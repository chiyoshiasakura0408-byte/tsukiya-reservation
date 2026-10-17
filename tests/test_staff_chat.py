import hashlib,hmac,os,secrets,tempfile,unittest,uuid
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
import concierge,english_chat,english_concierge,concierge_ai

class StaffChatTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.db=self.tmp.name+'/db'
  self.env=patch.dict(os.environ,{'ADMIN_TOKEN':'test-only'});self.env.start();self.addCleanup(self.env.stop)
  c=concierge.connect(self.db)
  with c:
   concierge.put(c,'owner','owner')
   c.execute('CREATE TABLE reservations(id INTEGER PRIMARY KEY,created_at TEXT,email TEXT,visit_at TEXT,status TEXT)')
   self.day=datetime.now(timezone(timedelta(hours=9))).date()
   c.execute('INSERT INTO reservations VALUES(1,?,?,?,?)',('created','test@example.invalid',str(self.day)+'T18:00','CONFIRMED'))
  c.close()
  raw='guest-chat-v1|1|created|test@example.invalid|'+str(self.day)+'T18:00'
  self.proof='1.'+hmac.new(b'test-only',raw.encode(),hashlib.sha256).hexdigest()
  self.data=dict(action='chat',token=secrets.token_hex(32),message_id=str(uuid.uuid4()),name='SYSTEM TEST',body='Water please',consent=True,mode='staff',visit_token=self.proof)
 def test_today_staff_route_and_line_reply(self):
  with patch.object(concierge_ai,'reply') as ai:
   code,j=english_chat.handle(self.db,self.data,'peer','https://example.invalid');ai.assert_not_called()
  self.assertEqual(code,200);self.assertTrue(j['staff_available']);rid=j['messages'][0]['request_id']
  event={'webhookEventId':'staff-test','source':{'type':'user','userId':'owner'},'message':{'text':'英語返信 '+rid+' お水をお持ちします'}}
  with patch.object(concierge_ai,'request',return_value={'translation_en':'We will bring you water.'}) as translate:
   english_concierge.owner_line_replies(self.db,[event,event]);self.assertEqual(translate.call_count,1)
  _,j=english_chat.handle(self.db,{**self.data,'action':'chat-status'},'peer','https://example.invalid')
  self.assertEqual(j['messages'][0]['owner_answer'],'We will bring you water.')
 def test_outside_day_cancelled_and_forged_denied(self):
  for change in ['date','cancel','forged']:
   c=concierge.connect(self.db)
   with c:
    if change=='date':c.execute('UPDATE reservations SET visit_at=?',(str(self.day+timedelta(days=1))+'T18:00',))
    if change=='cancel':c.execute("UPDATE reservations SET status='CANCELLED'")
   c.close()
   data={**self.data,'visit_token':self.proof if change!='forged' else '1.'+'0'*64}
   self.assertEqual(english_chat.handle(self.db,data,'peer','https://example.invalid')[0],403)
 def test_non_owner_cannot_publish(self):
  _,j=english_chat.handle(self.db,self.data,'peer','https://example.invalid');rid=j['messages'][0]['request_id']
  e={'webhookEventId':'evil','source':{'type':'user','userId':'other'},'message':{'text':'英語返信 '+rid+' 偽返信'}}
  with patch.object(concierge_ai,'request') as ai:
   english_concierge.owner_line_replies(self.db,[e]);ai.assert_not_called()
