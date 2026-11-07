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
 def pending_room(self, ident='pending-room', **kw):
  self.signed([self.event(ident)])
  with patch.object(lb,'extract',return_value={'is_reservation':True,'items':[item(seating_area='PRIVATE3',**kw)]}):lb.drain(run)
  return hashlib.sha256(ident.encode()).hexdigest()[:16]+'-1'
 def test_room_exception_owner_only_and_occupancy(self):
  ident=self.pending_room()
  with self.assertRaises(ValueError):lb.approve_room_exception(run,ident,'U'+'b'*32)
  result=lb.approve_room_exception(run,ident)
  self.assertEqual(result['state'],'done')
  self.assertEqual(lb.approve_room_exception(run,ident)['reservation_id'],result['reservation_id'])
  c=run.con();row=c.execute('SELECT * FROM reservations').fetchone()
  self.assertEqual((row['seating_area'],row['party_size'],row['source']),('PRIVATE3',2,'DIRECT'))
  self.assertFalse(run.public_party_allowed('PRIVATE3',2));self.assertFalse(run.availability_check(c,'PRIVATE3','2099-11-11T18:00',2)[0]);c.close()
  second=self.pending_room('second-room',guest_name='他の予約')
  self.assertEqual(lb.approve_room_exception(run,second)['state'],'needs_info')
  c=run.con();self.assertEqual(c.execute('SELECT COUNT(*) FROM reservations').fetchone()[0],1);c.close()
 def test_room_exception_natural_owner_command(self):
  ident=self.pending_room()
  self.signed([self.event('approve-room','例外として個室③で予約')])
  with patch.object(lb,'extract') as extract:lb.drain(run);extract.assert_not_called()
  c=run.con();self.assertEqual(c.execute('SELECT state FROM line_booking_items WHERE id=?',(ident,)).fetchone()[0],'done');c.close()
 def test_room_exception_preserves_blocks(self):
  ident=self.pending_room()
  c=run.con();c.execute("INSERT INTO seat_blocks(visit_date,time_text,seating_area,seat_number,note,created_at) VALUES('2099-11-10','18:00','PRIVATE3',0,'test','now')");c.commit();c.close()
  self.assertEqual(lb.approve_room_exception(run,ident)['state'],'needs_info')
 def test_phone_sentinels_register_direct_without_billing(self):
  for i,phone in enumerate(('', 'なし', 'vip', 'VIP', ' ＶＩＰ ')):
   with self.subTest(phone=phone):
    c=run.con()
    with c:r,_,_=lb.perform(run,c,item(phone=phone,visit_at=f'2099-11-{10+i:02}T18:00'))
    c.close()
    self.assertEqual((r['source'],r['status'],r['phone']),('DIRECT','CONFIRMED',''))
  with patch.object(run,'make_invoice') as invoice:lb.invoice_pending(run);invoice.assert_not_called()
 def test_multiline_owner_booking_exclusively_routes_and_registers_once(self):
  text='予約\n11/10\n18時\nかわぐち\n2名\n別邸\nずわい'
  e=self.event('short-dm',text);e['source']={'type':'user','userId':OWNER}
  with patch.object(line_bot.staff_faq,'respond') as faq:
   self.signed([e]);self.signed([e]);faq.assert_not_called()
  with patch.object(lb,'extract',return_value={'is_reservation':True,'items':[item(guest_name='かわぐち')]}),patch.object(run,'make_invoice') as invoice:
   lb.drain(run);lb.drain(run)
   again=self.event('short-dm-resend',text);again['source']=e['source']
   self.signed([again]);lb.drain(run);invoice.assert_not_called()
  c=concierge.connect(run.DB)
  self.assertEqual(c.execute('SELECT COUNT(*) FROM reservations').fetchone()[0],1)
  row=c.execute('SELECT source,status,phone FROM reservations').fetchone()
  self.assertEqual(tuple(row),('DIRECT','CONFIRMED',''))
  payloads=[json.loads(r[0])[0]['text'] for r in c.execute("SELECT payload FROM concierge_outbox WHERE kind='line-booking'")]
  self.assertTrue(any('予約登録成功' in p for p in payloads))
  self.assertTrue(any('重複登録なし' in p for p in payloads));c.close()
 def test_special_owner_text_preserves_seats_meals_and_duration_without_duplicates(self):
  text='予約追加、 特別枠\n2099/12/25 17:00〜19:00 別邸・個室③ 試験 太郎 大人4名・子ども3名（席のみ）料理4人前'
  payload=item(visit_at='2099-12-25T17:00',party_size=7,seating_area='PRIVATE3',
               special_end_time='19:00',special_meal_count=4)
  with patch.object(lb,'extract',side_effect=lambda *args:{'is_reservation':True,'items':[dict(payload)]}),patch.object(run,'make_invoice') as invoice:
   for ident in ('special','special-resend'):
    self.signed([self.event(ident,text)])
    lb.drain(run)
   invoice.assert_not_called()
  c=run.con()
  try:
   rows=c.execute('SELECT * FROM reservations').fetchall()
   self.assertEqual(len(rows),1)
   row=dict(rows[0])
   self.assertEqual((row['party_size'],row['special_meal_count'],row['duration_minutes'],row['amount'],row['status']),
                    (7,4,120,264000,'CONFIRMED'))
   self.assertEqual(calendar_sync.event_body(row,'test')['end']['dateTime'],'2099-12-25T19:00:00+09:00')
   self.assertFalse(run.availability_check(c,'PRIVATE3','2099-12-25T18:00',7)[0])
   self.assertEqual([r[0] for r in c.execute('SELECT action FROM line_booking_audit ORDER BY id')],['add','duplicate'])
   with self.assertRaisesRegex(ValueError,'特別枠の変更'):
    lb.perform(run,c,item(action='party',reservation_id=row['id'],party_size=6))
  finally:
   c.close()

 def test_owner_seat_move_preserves_paid_booking_and_obeys_blocks(self):
  c=run.con()
  with c:
   row,_,_=lb.perform(run,c,item(seating_area='PRIVATE2'))
   rid=row['id']
   c.execute("UPDATE reservations SET source='WEB_CARD',payment_confirmed_at='paid' WHERE id=?",(rid,))
   before=dict(c.execute('SELECT * FROM reservations WHERE id=?',(rid,)).fetchone())
   for seat in range(1,8):
    c.execute("INSERT INTO seat_blocks(visit_date,time_text,seating_area,seat_number,note,created_at) VALUES('2099-11-10','18:00','COUNTER',?,'test','now')",(seat,))
  command=f'予約変更 例外 #{rid} カウンター'
  with patch.object(lb,'extract') as extract:
   self.signed([self.event('unauthorized-move',command,actor='U'+'b'*32)])
   self.signed([self.event('blocked-move',command)])
   lb.drain(run)
   self.assertEqual(dict(c.execute('SELECT * FROM reservations WHERE id=?',(rid,)).fetchone()),before)
   with c:c.execute("DELETE FROM seat_blocks")
   self.signed([self.event('authorized-move',command)])
   lb.drain(run)
   extract.assert_not_called()
  after=dict(c.execute('SELECT * FROM reservations WHERE id=?',(rid,)).fetchone());c.close()
  self.assertEqual((after['seating_area'],after['counter_round']),('COUNTER',1))
  for key in before.keys()-{'seating_area','counter_round','updated_at'}:
   self.assertEqual(after[key],before[key],key)

 def test_explicit_owner_special_add_allows_three_in_room_three(self):
  command='予約追加 特別枠 2099-11-10 19:00-20:00 個室③ 3名 料理3人前 試験'
  with patch.object(lb,'extract') as extract,patch.object(run,'make_invoice') as invoice:
   self.signed([self.event('owner-special-1',command),self.event('owner-special-2',command)])
   lb.drain(run)
   extract.assert_not_called()
   invoice.assert_not_called()
  c=run.con()
  try:
   self.assertEqual([tuple(row) for row in c.execute(
    'SELECT seating_area,party_size,special_meal_count,duration_minutes,status FROM reservations')],
    [('PRIVATE3',3,3,60,'CONFIRMED')])
  finally:c.close()

 def test_line_phone_invoice_once_after_commit(self):
  self.signed([self.event()])
  def invoice(row):
   c=run.con();self.assertIsNotNone(c.execute('SELECT id FROM reservations WHERE id=?',(row['id'],)).fetchone());c.close()
   self.assertEqual(row['phone'],'09000000000')
   return 'c','o','i','https://example.com/invoice'
  with patch.object(lb,'extract',return_value={'is_reservation':True,'items':[item(phone='09000000000')]}),patch.object(run,'make_invoice',side_effect=invoice) as make,patch.object(run,'send_invoice_sms',side_effect=lambda r:{**r,'invoice_sms_status':'QUEUED'}) as sms:
   lb.drain(run);lb.drain(run)
   self.assertEqual(make.call_count,1);self.assertEqual(sms.call_count,1)
  c=run.con();r=dict(c.execute('SELECT * FROM reservations').fetchone());c.close()
  self.assertEqual((r['source'],r['status'],r['square_invoice_id']),('PHONE','INVOICED','i'))
 def test_line_phone_failure_preserves_booking_without_automatic_retry(self):
  self.signed([self.event()])
  with patch.object(lb,'extract',return_value={'is_reservation':True,'items':[item(phone='09000000000')]}),patch.object(run,'make_invoice',side_effect=RuntimeError('timeout')) as make,patch.object(run,'send_invoice_sms') as sms:
   lb.drain(run);lb.drain(run)
   self.assertEqual(make.call_count,1);sms.assert_not_called()
  c=run.con();rows=c.execute('SELECT source,status FROM reservations').fetchall();c.close()
  self.assertEqual([tuple(r) for r in rows],[('PHONE','ERROR')])
 def test_phone_invalid_and_supplement_template(self):
  c=run.con()
  with self.assertRaises(ValueError):lb.perform(run,c,item(phone='不明'))
  c.close();self.signed([self.event()])
  with patch.object(lb,'extract',return_value={'is_reservation':True,'items':[item(seating_area='')]}),patch.object(lb,'notify') as notice:
   lb.drain(run)
   self.assertIn('電話番号',notice.call_args.kwargs['command'])
   self.assertIn('なし・VIP・無記載は直接予約',notice.call_args.kwargs['command'])
 def test_ingress_owner_group_signature_and_retry(self):
  self.signed([self.event(actor='U'+'b'*32)]);self.signed([self.event('other',image=True,actor='U'+'b'*32)])
  c=sqlite3.connect(run.DB);self.assertEqual(c.execute('SELECT COUNT(*) FROM line_booking_jobs').fetchone()[0],0);c.close()
  self.signed([self.event('owner',image=True)]);self.signed([self.event('owner',image=True)])
  c=sqlite3.connect(run.DB);self.assertEqual(c.execute('SELECT COUNT(*) FROM line_booking_jobs').fetchone()[0],1)
  self.assertEqual(c.execute('SELECT COUNT(*) FROM receipt_images').fetchone()[0],1);c.close()
  with patch.dict(os.environ,{'LINE_CHANNEL_SECRET':'test'}):self.assertEqual(line_bot.receive(run.DB,b'{"events":[]}','bad')[0],403)
 def test_owner_dm_and_reply_destination(self):
  e=self.event('dm','予約追加');e['source']={'type':'user','userId':OWNER}
  other=self.event('not-owner','予約追加');other['source']={'type':'user','userId':'U'+'b'*32}
  self.signed([e,other]);lb.drain(run)
  c=concierge.connect(run.DB)
  self.assertEqual(c.execute('SELECT COUNT(*) FROM line_booking_jobs').fetchone()[0],1)
  self.assertEqual(c.execute('SELECT group_id FROM line_booking_jobs').fetchone()[0],OWNER)
  c.close()
  image=self.event('dm-image',image=True);image['source']=e['source'];self.signed([image])
  with patch.object(lb,'extract',return_value={'is_reservation':True,'items':[item()]}),patch.object(lb,'image_bytes',return_value=(b'image','image/png')),patch.object(lb,'notify') as notify:
   lb.drain(run);self.assertEqual(notify.call_args.args[2],OWNER)
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
 def test_group_registration_and_calendar_notices_go_to_owner_once(self):
  self.signed([self.event()])
  with patch.object(lb,'extract',return_value={'is_reservation':True,'items':[item()]}),patch.object(lb,'sync_state',return_value=('同期待ち',False)):
   lb.drain(run);lb.drain(run)
  with patch.object(lb,'sync_state',return_value=('同期済み',True)):
   lb.drain(run);lb.drain(run)
  c=concierge.connect(run.DB)
  rows=c.execute("SELECT user_id,payload FROM concierge_outbox WHERE kind='line-booking' ORDER BY created").fetchall()
  self.assertEqual(len(rows),2)
  self.assertEqual([r['user_id'] for r in rows],[OWNER,OWNER])
  self.assertTrue(json.loads(rows[0]['payload'])[0]['text'].startswith('【予約登録成功】'))
  self.assertTrue(json.loads(rows[1]['payload'])[0]['text'].startswith('【Googleカレンダー同期成功】'))
  c.close()
 def test_completion_notices_require_owner_other_replies_keep_destination(self):
  c=concierge.connect(run.DB)
  with c:
   concierge.put(c,'owner','')
   for title in ('予約登録成功','Googleカレンダー同期成功'):
    with self.assertRaises(RuntimeError):lb.notify(c,'【'+title+'】\n試験',lb.GROUP)
   self.assertEqual(c.execute('SELECT COUNT(*) FROM concierge_outbox').fetchone()[0],0)
   lb.notify(c,'【予約未処理】\n試験',lb.GROUP,command='予約補足')
  row=c.execute('SELECT user_id,payload FROM concierge_outbox').fetchone()
  self.assertEqual(row['user_id'],lb.GROUP)
  self.assertEqual(json.loads(row['payload'])[1]['text'],'予約補足')
  c.close()
 def test_change_cancel_and_paid_vip_protection(self):
  c=run.con()
  with c:
   r,_,_=lb.perform(run,c,item())
   blank=item(action='party',reservation_id=r['id'],visit_at='',guest_name='',seating_area='',party_size=3)
   changed,before,action=lb.perform(run,c,blank);self.assertEqual(changed['party_size'],3);self.assertEqual(changed['amount'],r['amount']//r['party_size']*3)
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
 def test_quote_notes_target_auth_and_deduplication(self):
  c=concierge.connect(run.DB)
  with c:
   r,_,_=lb.perform(run,c,item())
   other,_,_=lb.perform(run,c,item(guest_name='別のお客様',visit_at='2099-11-11T18:00'))
   out=concierge.enqueue(c,OWNER,[{'type':'text','text':'予約通知'}],channel='owner',reservation_id=r['id'])
   row=c.execute('SELECT * FROM concierge_outbox WHERE id=?',(out,)).fetchone()
   concierge.remember_reservation_messages(c,row,json.dumps({'sentMessages':[{'id':'777'}]}))
  c.close()
  for i,body in enumerate(('花束用意','ケーキ用意','甲殻類アレルギー。ケーキは不要','プレートは Happy Birthday')):
   e=self.event('note'+str(i),body);e['source']={'type':'user','userId':OWNER};e['message']['quotedMessageId']='777'
   self.signed([e]);self.signed([e]);lb.drain(run)
  bad=self.event('bad-note','ケーキ追加',actor='U'+'b'*32);bad['message']['quotedMessageId']='777';self.signed([bad]);lb.drain(run)
  c=concierge.connect(run.DB)
  after=dict(c.execute('SELECT * FROM reservations WHERE id=?',(r['id'],)).fetchone())
  self.assertEqual(after['guest_note'].count('【朝倉追記】'),4)
  self.assertIn('ケーキは不要',after['guest_note'])
  for key in ('amount','status','party_size','celebration_items','seating_area','visit_at'):
   self.assertEqual(after[key],r[key])
  self.assertFalse(c.execute('SELECT guest_note FROM reservations WHERE id=?',(other['id'],)).fetchone()[0])
  self.assertEqual(c.execute("SELECT COUNT(*) FROM line_booking_audit WHERE action='note'").fetchone()[0],4)
  self.assertNotEqual(calendar_sync.event_body(after,'test'),calendar_sync.event_body(r,'test'))
  c.close()
 def test_consultation_reply_group_binding_status_and_no_repeat(self):
  import guest_service
  group='C'+'e'*32
  c=concierge.connect(run.DB)
  with c:
   r,_,_=lb.perform(run,c,item())
   c.execute("UPDATE reservations SET celebration_items='ケーキ',guest_note='卵不可' WHERE id=?",(r['id'],))
  c.close();guest_service.sync_requests(run)
  c=concierge.connect(run.DB)
  with c:
   delivery=c.execute('SELECT delivery_id FROM booking_consultations').fetchone()[0]
   c.execute('UPDATE concierge_outbox SET user_id=? WHERE id=?',(group,delivery))
   row=c.execute('SELECT * FROM concierge_outbox WHERE id=?',(delivery,)).fetchone()
   self.assertIn('リプライ',row['payload'])
   concierge.remember_reservation_messages(c,row,json.dumps({'sentMessages':[{'id':'888'}]}))
  c.close()
  e=self.event('group-reply','ケーキ用意可能');e['source']['groupId']=group;e['message']['quotedMessageId']='888'
  self.signed([e]);self.signed([e])
  with patch.object(lb,'extract') as extract:lb.drain(run);extract.assert_not_called()
  guest_service.sync_requests(run)
  c=concierge.connect(run.DB)
  self.assertEqual(c.execute('SELECT count(*) FROM booking_consultations').fetchone()[0],1)
  self.assertEqual(c.execute('SELECT status FROM concierge_requests').fetchone()[0],'担当回答受付済み（予約備考へ記録）')
  self.assertEqual(c.execute('SELECT guest_note FROM reservations').fetchone()[0],'卵不可\n【朝倉追記】ケーキ用意可能')
  self.assertEqual(c.execute("SELECT count(*) FROM line_booking_audit WHERE action='note'").fetchone()[0],1)
  confirmation=c.execute("SELECT user_id,payload FROM concierge_outbox WHERE kind='line-booking'").fetchone()
  self.assertEqual(confirmation['user_id'],group);self.assertIn('お客様への送信',confirmation['payload'])
  c.close()
  # Another member, or copying the quote ID into another group, cannot write.
  for i,(actor,dest) in enumerate((('U'+'b'*32,group),(OWNER,'C'+'f'*32))):
   bad=self.event('unauthorized-reply'+str(i),'変更しない',actor=actor)
   bad['source']['groupId']=dest;bad['message']['quotedMessageId']='888';self.signed([bad]);lb.drain(run)
  c=concierge.connect(run.DB)
  self.assertNotIn('変更しない',c.execute('SELECT guest_note FROM reservations').fetchone()[0]);c.close()
 def test_unknown_quote_and_wrong_destination_do_not_mutate(self):
  c=concierge.connect(run.DB)
  with c:
   r,_,_=lb.perform(run,c,item())
   c.execute('INSERT INTO reservation_message_links VALUES(?,?,?,?)',('777','out',r['id'],OWNER))
  c.close()
  for i,mid in enumerate(('777','999')):
   e=self.event('unknown'+str(i),'花束用意');e['message']['quotedMessageId']=mid
   self.signed([e]);lb.drain(run)
  c=concierge.connect(run.DB)
  self.assertFalse(c.execute('SELECT guest_note FROM reservations').fetchone()[0])
  self.assertEqual(c.execute("SELECT count(*) FROM line_booking_jobs WHERE state='needs_info'").fetchone()[0],2);c.close()
 def test_explicit_note_and_admin_retry(self):
  c=concierge.connect(run.DB)
  with c:r,_,_=lb.perform(run,c,item())
  c.close()
  self.signed([self.event('explicit','予約追記 '+str(r['id'])+' ケーキ用意')]);lb.drain(run)
  data={'reservation_id':r['id'],'note':'花束用意','request_id':'12345678-1234-1234-1234-123456789abc'}
  lb.admin_note(run,data);lb.admin_note(run,data)
  c=concierge.connect(run.DB)
  self.assertEqual(c.execute('SELECT guest_note FROM reservations').fetchone()[0].count('花束用意'),1)
  self.assertEqual(c.execute('SELECT COUNT(*) FROM line_booking_audit').fetchone()[0],2);c.close()
 def test_note_dialog_lookup_details_append_and_retry(self):
  c=concierge.connect(run.DB)
  with c:r,_,_=lb.perform(run,c,item())
  c.close()
  for i,text in enumerate(('追加','2099/11/10','試験 太郎','ケーキ用意。プレートは祝還暦')):
   event=self.event('dialog'+str(i),text)
   self.signed([event]);self.signed([event]);lb.drain(run)
  c=concierge.connect(run.DB)
  self.assertEqual(c.execute('SELECT guest_note FROM reservations').fetchone()[0],'【朝倉追記】ケーキ用意。プレートは祝還暦')
  self.assertEqual(c.execute('SELECT COUNT(*) FROM line_note_sessions').fetchone()[0],0)
  payloads=[json.loads(r[0]) for r in c.execute('SELECT payload FROM concierge_outbox ORDER BY created')]
  self.assertEqual(payloads[0][0]['text'],'日付\n名前\n\nを教えて下さい')
  self.assertTrue(any('【予約詳細】' in msgs[0]['text'] and '人数：2名' in msgs[0]['text'] for msgs in payloads))
  self.assertEqual(c.execute("SELECT COUNT(*) FROM line_booking_audit WHERE action='note'").fetchone()[0],1);c.close()
 def test_dialog_multiple_candidates_scope_and_expiry(self):
  c=concierge.connect(run.DB)
  with c:
   a,_,_=lb.perform(run,c,item(guest_name='山田 一郎'))
   b,_,_=lb.perform(run,c,item(guest_name='山田 二郎',visit_at='2099-11-10T20:30'))
  c.close()
  for i,text in enumerate(('予約追記','2099年11月10日 山田','2')):
   self.signed([self.event('choose'+str(i),text)]);lb.drain(run)
  e=self.event('wrong-room','花束用意');e['source']={'type':'user','userId':OWNER};self.signed([e]);lb.drain(run)
  self.signed([self.event('chosen-note','花束用意')]);lb.drain(run)
  c=concierge.connect(run.DB)
  self.assertFalse(c.execute('SELECT guest_note FROM reservations WHERE id=?',(a['id'],)).fetchone()[0])
  self.assertIn('花束用意',c.execute('SELECT guest_note FROM reservations WHERE id=?',(b['id'],)).fetchone()[0]);c.close()
  self.signed([self.event('expire-start','追加')]);lb.drain(run)
  c=concierge.connect(run.DB)
  with c:c.execute('UPDATE line_note_sessions SET expires=0')
  c.close();self.signed([self.event('expired','ケーキ用意')]);lb.drain(run)
  c=concierge.connect(run.DB);self.assertEqual(c.execute('SELECT COUNT(*) FROM line_note_sessions').fetchone()[0],0);c.close()
 def test_dialog_rapid_messages_and_changed_booking(self):
  c=concierge.connect(run.DB)
  with c:r,_,_=lb.perform(run,c,item())
  c.close()
  self.signed([self.event('rapid1','追加'),self.event('rapid2','2099/11/10 試験 太郎')]);lb.drain(run);lb.drain(run)
  c=concierge.connect(run.DB)
  with c:c.execute('UPDATE reservations SET party_size=3 WHERE id=?',(r['id'],))
  c.close();self.signed([self.event('changed','ケーキ用意')]);lb.drain(run)
  c=concierge.connect(run.DB);self.assertFalse(c.execute('SELECT guest_note FROM reservations').fetchone()[0]);c.close()
  self.signed([self.event('stop','中止')]);lb.drain(run)
  c=concierge.connect(run.DB);self.assertEqual(c.execute('SELECT COUNT(*) FROM line_note_sessions').fetchone()[0],0);c.close()
if __name__=='__main__':unittest.main()

