import base64,hashlib,hmac,json,os,sqlite3,tempfile,unittest
from unittest.mock import patch
os.environ.setdefault('DATA_DIR',tempfile.mkdtemp())
import run,concierge,line_bot,line_reservations as lb,calendar_sync
OWNER='U'+'a'*32

def item(**kw):
 d=dict(action='add',reservation_id=None,target_date='',guest_name='試験 太郎',visit_at='2099-11-10T18:00',party_size=2,phone='',seating_area='PRIVATE',note='',uncertain=[]);d.update(kw);return d
class Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.p=patch.object(run,'DB',self.tmp.name+'/db');self.p.start();self.addCleanup(self.p.stop)
  c=run.con();c.close();c=concierge.connect(run.DB)
  with c:concierge.put(c,'owner',OWNER);lb.schema(c)
  c.close()
 def signed(self,events):
  raw=json.dumps({'events':events}).encode();sig=base64.b64encode(hmac.new(b'test',raw,hashlib.sha256).digest()).decode()
  with patch.dict(os.environ,{'LINE_CHANNEL_SECRET':'test'}):return line_bot.receive(run.DB,raw,sig)
 def event(self,ident='ev',text='予約追加 テスト',actor=OWNER,image=False):
  return {'webhookEventId':ident,'type':'message','source':{'type':'group','groupId':lb.GROUP,'userId':actor},'message':{'id':'123456','type':'image' if image else 'text','text':text}}
 def test_ingress_owner_group_signature_and_retry(self):
  self.signed([self.event(actor='U'+'b'*32)]);self.signed([self.event('other',image=True,actor='U'+'b'*32)])
  c=sqlite3.connect(run.DB);self.assertEqual(c.execute('SELECT COUNT(*) FROM line_booking_jobs').fetchone()[0],0);c.close()
  self.signed([self.event('owner',image=True)]);self.signed([self.event('owner',image=True)])
  c=sqlite3.connect(run.DB);self.assertEqual(c.execute('SELECT COUNT(*) FROM line_booking_jobs').fetchone()[0],1)
  self.assertEqual(c.execute('SELECT COUNT(*) FROM receipt_images').fetchone()[0],1);c.close()
  with patch.dict(os.environ,{'LINE_CHANNEL_SECRET':'test'}):self.assertEqual(line_bot.receive(run.DB,b'{"events":[]}','bad')[0],403)
 def test_add_duplicate_capacity_and_required(self):
  c=run.con()
  with c:
   r,_,a=lb.perform(run,c,item());self.assertEqual((a,r['status'],r['source']),('add','CONFIRMED','DIRECT'))
   same,_,a=lb.perform(run,c,item());self.assertEqual((a,same['id']),('duplicate',r['id']))
   with self.assertRaises(ValueError):lb.perform(run,c,item(party_size=3))
   for name in ('二郎','三郎'):lb.perform(run,c,item(guest_name=name,party_size=4))
   with self.assertRaises(ValueError):lb.perform(run,c,item(guest_name='満席'))
   with self.assertRaises(ValueError):lb.perform(run,c,item(guest_name='不明',seating_area=''))
   with self.assertRaises(ValueError):lb.perform(run,c,item(guest_name='不明',uncertain=['年不明']))
   with self.assertRaises(ValueError):lb.perform(run,c,item(guest_name='不明',visit_at='2099-11-11T19:00',seating_area='COUNTER'))
  c.close()
 def test_change_cancel_and_paid_vip_protection(self):
  c=run.con()
  with c:
   r,_,_=lb.perform(run,c,item())
   blank=item(action='party',reservation_id=r['id'],visit_at='',guest_name='',seating_area='',party_size=3)
   changed,before,action=lb.perform(run,c,blank);self.assertEqual(changed['party_size'],3);self.assertEqual(changed['amount'],180000)
   with self.assertRaises(ValueError):lb.perform(run,c,{**blank,'guest_name':'別人'})
   c.execute('UPDATE reservations SET vip_booking=1 WHERE id=?',(r['id'],))
   with self.assertRaises(ValueError):lb.perform(run,c,blank)
   c.execute("UPDATE reservations SET vip_booking=0,payment_confirmed_at='paid' WHERE id=?",(r['id'],))
   with self.assertRaises(ValueError):lb.perform(run,c,{**blank,'action':'cancel'})
   c.execute('UPDATE reservations SET payment_confirmed_at=NULL WHERE id=?',(r['id'],))
   cancelled,_,_=lb.perform(run,c,{**blank,'action':'cancel','party_size':None});self.assertEqual(cancelled['status'],'CANCELLED')
  c.close()
 def test_atomic_processing_questions_audit_and_no_duplicate(self):
  self.signed([self.event()])
  with patch.object(lb,'extract',return_value={'is_reservation':True,'items':[item(),item(guest_name='不明',seating_area='')]}):lb.drain(run);lb.drain(run)
  c=concierge.connect(run.DB)
  self.assertEqual(c.execute('SELECT COUNT(*) FROM reservations').fetchone()[0],1)
  self.assertEqual(c.execute('SELECT COUNT(*) FROM line_booking_audit').fetchone()[0],1)
  self.assertEqual(c.execute("SELECT COUNT(*) FROM line_booking_items WHERE state='needs_info'").fetchone()[0],1)
  self.assertEqual(c.execute("SELECT COUNT(*) FROM concierge_outbox WHERE kind='line-booking'").fetchone()[0],2)
  target=c.execute("SELECT id FROM line_booking_items WHERE state='needs_info'").fetchone()[0];c.close()
  self.signed([self.event('follow','予約補足 '+target+' 別邸個室')])
  with patch.object(lb,'extract',return_value={'is_reservation':True,'items':[item(guest_name='不明')]}):lb.drain(run)
  c=sqlite3.connect(run.DB);self.assertEqual(c.execute('SELECT COUNT(*) FROM reservations').fetchone()[0],2);c.close()
 def test_receipt_fallback_and_reserved_siblings_not_relayed(self):
  events=[self.event('photo1',image=True),{**self.event('photo2',image=True),'message':{'type':'image','id':'222'}}]
  self.signed(events)
  with patch.object(lb,'image_bytes',return_value=(b'png','image/png')),patch.object(lb,'extract',return_value={'is_reservation':False,'items':[]}):lb.drain(run)
  c=sqlite3.connect(run.DB);body=c.execute('SELECT body FROM receipt_relay').fetchone()[0];self.assertEqual(len(json.loads(body)['events']),1);c.close()
 def test_calendar_success_requires_matching_fingerprint(self):
  c=run.con()
  with c:r,_,_=lb.perform(run,c,item())
  calendar_sync.schema(c)
  with patch.dict(os.environ,{'GOOGLE_CALENDAR_SYNC_ENABLED':'1','GOOGLE_CALENDAR_ID':'test','GOOGLE_CALENDAR_NAMESPACE':'test'}):
   self.assertFalse(lb.sync_state(c,r)[1]);fp=hashlib.sha256(json.dumps(calendar_sync.event_body(r,'test'),sort_keys=True,ensure_ascii=False).encode()).hexdigest()
   with c:c.execute('INSERT INTO calendar_sync_state VALUES(?,?,?,?,?,?,?,?)',('test',r['id'],'id',fp,0,0,1,None))
   self.assertTrue(lb.sync_state(c,r)[1]);self.assertFalse(lb.sync_state(c,{**r,'party_size':4})[1])
  c.close()
 def test_recover_is_not_claimed_as_live_line(self):
  c=sqlite3.connect(run.DB);import line_receipts,time
  line_receipts.schema(c)
  with c:c.execute('INSERT INTO receipt_images VALUES(?,?,?,0)',('456',lb.GROUP,time.time()))
  c.close();lb.recover(run,['456']);lb.recover(run,['456'])
  c=sqlite3.connect(run.DB);self.assertEqual(c.execute('SELECT origin FROM line_booking_jobs').fetchall(),[('admin-recovery',)]);c.close()
if __name__=='__main__':unittest.main()
