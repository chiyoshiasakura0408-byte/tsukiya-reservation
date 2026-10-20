import json
from unittest.mock import patch
from test_staff_chat import StaffChatTests
import concierge, concierge_ai, english_chat, english_concierge, line_reservations

class QuoteReplyTests(StaffChatTests):
 def prepare(self):
  _,j=english_chat.handle(self.db,self.data,'peer','https://example.invalid')
  rid=j['messages'][0]['request_id']
  c=concierge.connect(self.db)
  with c:
   row=c.execute('SELECT o.* FROM concierge_outbox o JOIN english_notice_links l ON l.outbox_id=o.id WHERE l.request_id=?',(rid,)).fetchone()
   english_concierge.remember_messages(c,row,json.dumps({'sentMessages':[{'id':'111'},{'id':'222'}]}))
  c.close()
  return rid
 def event(self,mid='111',actor='owner',event='quoted-test',text='本日から受付け開始しました。'):
  return {'type':'message','webhookEventId':event,'source':{'type':'user','userId':actor},'message':{'type':'text','id':'999','quotedMessageId':mid,'text':text}}
 def test_quote_publishes_once_and_both_notice_parts_link(self):
  rid=self.prepare()
  for mid in ['111','222']:
   e=self.event(mid,event='reply-'+mid)
   with patch.object(concierge_ai,'request',return_value={'translation_en':'Reservations are now open.'}) as ai:
    english_concierge.owner_line_replies(self.db,[e,e]);self.assertEqual(ai.call_count,1)
  _,j=english_chat.handle(self.db,{**self.data,'action':'chat-status'},'peer','https://example.invalid')
  self.assertEqual(j['messages'][0]['owner_answer'],'Reservations are now open.\n\nReservations are now open.')
 def test_wrong_owner_unknown_quote_conflicting_reference_and_group_rejected(self):
  self.prepare()
  group=self.event();group['source']['type']='group'
  events=[self.event(actor='other'),self.event(mid='333'),self.event(text='英語返信 web-'+'a'*24+' 間違った宛先'),group]
  with patch.object(concierge_ai,'request') as ai:
   english_concierge.owner_line_replies(self.db,events);ai.assert_not_called()
 def test_translation_failure_does_not_publish(self):
  self.prepare()
  with patch.object(concierge_ai,'request',side_effect=ValueError('failed')):
   english_concierge.owner_line_replies(self.db,[self.event()])
  _,j=english_chat.handle(self.db,{**self.data,'action':'chat-status'},'peer','https://example.invalid')
  self.assertFalse(j['messages'][0]['owner_answer'])
 def test_quote_not_processed_as_reservation_note(self):
  self.prepare();c=concierge.connect(self.db)
  with c:
   actor='U'+'1'*32
   concierge.put(c,'owner',actor)
   c.execute('UPDATE english_message_links SET destination=?',(actor,))
   e=self.event(actor=actor)
   claimed=line_reservations.ingest(c,b'', '', {'events':[e]})
   self.assertIn(e['webhookEventId'],claimed)
   self.assertEqual(c.execute('SELECT count(*) FROM line_booking_jobs').fetchone()[0],0)
  c.close()
 def test_resend_notice_keeps_same_request(self):
  rid=self.prepare();c=concierge.connect(self.db)
  with c:
   a=english_concierge.resend_notice(c,rid);b=english_concierge.resend_notice(c,rid)
   self.assertEqual(a['outbox_id'],b['outbox_id'])
   self.assertEqual(c.execute('SELECT request_id FROM english_notice_links WHERE outbox_id=?',(a['outbox_id'],)).fetchone()[0],rid)
  c.close()
