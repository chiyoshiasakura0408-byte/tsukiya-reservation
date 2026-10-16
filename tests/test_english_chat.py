import hashlib
import json
import os
import secrets
import tempfile
import unittest
import uuid
from unittest.mock import patch
import concierge
import concierge_ai
import english_chat
import english_concierge
import booking_policy


class EnglishChatTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=self.tmp.name+'/chat.sqlite'
        c=concierge.connect(self.db)
        with c:concierge.put(c,'owner','owner')
        c.close()
        self.data={'action':'chat','token':secrets.token_hex(32),'message_id':str(uuid.uuid4()),'name':'Guest','body':'Tell me about your crab.','consent':True}

    def call(self, **changes):
        return english_concierge.handle(self.db,{**self.data,**changes},'proxy','https://example.invalid')

    def test_free_conversation_is_private_and_idempotent(self):
        result={'answer':'We prepare live crab after welcoming our guests.','needs_owner':False,'summary_ja':'活蟹について','reason_ja':''}
        with patch.object(concierge_ai,'reply',return_value=result) as reply:
            self.assertEqual(self.call()[0],200)
            self.assertEqual(self.call()[0],200)
            self.assertEqual(reply.call_count,1)
        self.assertEqual(self.call(action='chat-status')[1]['messages'][0]['answer'],result['answer'])
        self.assertEqual(self.call(action='chat-status',token=secrets.token_hex(32))[1]['messages'],[])
        c=concierge.connect(self.db)
        self.assertEqual(c.execute('SELECT count(*) FROM concierge_outbox').fetchone()[0],0)
        self.assertNotIn(self.data['token'],str([tuple(r) for r in c.execute('SELECT * FROM english_chat_messages')]))
        c.close()

    def test_ai_failure_escalates_and_staff_reply_returns_to_chat(self):
        with patch.object(concierge_ai,'reply',side_effect=TimeoutError):
            code,data=self.call()
        message=data['messages'][0]
        self.assertEqual(code,200);self.assertTrue(message['request_id'].startswith('web-'))
        concierge.configure(self.db,{'action':'answer','id':message['request_id'],'body':'Our team will assist you.'})
        self.assertEqual(self.call(action='chat-status')[1]['messages'][0]['owner_answer'],'Our team will assist you.')
        c=concierge.connect(self.db)
        self.assertEqual(c.execute('SELECT count(*) FROM concierge_outbox').fetchone()[0],1)
        c.close()

    def test_uncertain_delivery_does_not_claim_owner_notified(self):
        c=concierge.connect(self.db)
        with c:concierge.put(c,'owner','')
        c.close()
        with patch.object(concierge_ai,'reply',side_effect=TimeoutError):
            _,data=self.call()
        self.assertIsNone(data['messages'][0]['request_id'])
        self.assertIn('temporarily unavailable',data['messages'][0]['answer'])

    def test_english_cannot_offer_extended_booking(self):
        with patch.object(concierge_ai,'request') as api:
            result=concierge_ai.reply('Could we book 18:30 and stay all evening?',[])
        api.assert_not_called()
        self.assertTrue(result['needs_owner']);self.assertIn('cannot be arranged',result['answer'])

    def test_mutation_claim_is_replaced_with_owner_handoff(self):
        bad={'answer':'Your reservation is confirmed','needs_owner':False,'summary_ja':'','reason_ja':''}
        with patch.object(concierge_ai,'request',return_value=bad):
            result=concierge_ai.reply('Tell me about dinner.',[])
        self.assertTrue(result['needs_owner']);self.assertEqual(result['answer'],concierge_ai.HANDOFF)

    def test_owner_notification_has_japanese_and_full_original(self):
        translated={'summary_ja':'12月15日2名の予約希望','translation_ja':'18時30分または19時を希望します。','reason_ja':'二部制以外の時間のため'}
        original='【English Concierge】\nName: Guest\nMay I reserve at 6:30 PM?\n受付：web-test'
        with patch.object(concierge_ai,'translate',return_value=translated):
            payload=concierge_ai.owner_messages([{'type':'text','text':original}])
        self.assertIn('日本語要約',payload[0]['text']);self.assertIn('日本語訳',payload[0]['text'])
        self.assertIn(original,payload[1]['text'])

    def test_beverage_categories_and_forged_public_fields(self):
        for source,lang,through,label,included in [('WEB','ja',False,'通常予約',True),('WEB','en',False,'Englishコンシェルジュ',True),('CONCIERGE_LINE','ja',False,'VIP通常予約',True),('CONCIERGE_LINE','ja',True,'VIP通し予約',False)]:
            policy=booking_policy.details({'source':source,'booking_language':lang,'vip_through':through,'visit_at':'2026-12-15T18:00'})
            self.assertEqual(policy['reservation_category'],label);self.assertEqual(policy['beverages_included'],included)
        for data in ({'through':True},{'vip_through':1},{'time_specified':True},{'duration_minutes':300}):
            with self.assertRaises(ValueError):booking_policy.require_public(data)
