import base64
import hashlib
import hmac
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
import concierge as bot


class ConciergeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = self.temp.name + '/test.sqlite'
        env = patch.dict(os.environ, {'CONCIERGE_LINE_CHANNEL_SECRET':'customer-secret', 'CONCIERGE_LINE_CHANNEL_ACCESS_TOKEN':'customer-token', 'LINE_CHANNEL_ACCESS_TOKEN':'work-token'})
        env.start(); self.addCleanup(env.stop)
        c = bot.connect(self.db)
        with c:
            bot.put(c, 'enabled', '1'); bot.put(c, 'owner', 'owner-id')
        c.close()
        self.number = 0

    def send(self, text, event_id=None, kind='message', source='user', lookup=None):
        self.number += 1
        events = [{'webhookEventId':event_id or str(self.number),'type':kind,'source':{'type':source,'userId':'customer-id'},'message':{'type':'text','text':text}}]
        raw = json.dumps({'events':events}).encode()
        sig = base64.b64encode(hmac.new(b'customer-secret',raw,hashlib.sha256).digest()).decode()
        return bot.receive(self.db,raw,sig,lookup or (lambda day,party:[]),{'matsuba-seko':('松葉蟹、せこ蟹',60000)},'https://example.com')

    def rows(self, table):
        c = bot.connect(self.db)
        try: return [dict(row) for row in c.execute('SELECT * FROM '+table)]
        finally: c.close()

    def test_catalog_notes_persist(self):
        bot.configure(self.db,{'action':'catalog','notes':'コースに関する補足事項'})
        c=bot.connect(self.db)
        try: self.assertEqual(bot.catalog(c)['notes'],'コースに関する補足事項')
        finally: c.close()

    def test_signature_and_dedup(self):
        self.assertEqual(bot.receive(self.db,b'{}','invalid',None,{},'' )[0],403)
        self.send('メニュー','a'); self.send('メニュー','a')
        self.assertEqual(len(self.rows('concierge_outbox')),1)

    def test_automatic_change_and_stop(self):
        self.send('メニュー')
        bot.configure(self.db,{'action':'catalog','crab':'蟹A','origin':'北海道','arrival_date':'2026-11-10'})
        self.assertTrue(any(x['kind']=='announcement' for x in self.rows('concierge_outbox')))
        self.send('入荷案内を受け取る')
        for _ in range(2): bot.configure(self.db,{'action':'catalog','crab':'蟹B','origin':'北海道','arrival_date':'2026-11-10'})
        self.assertEqual(sum(x['kind']=='announcement' for x in self.rows('concierge_outbox')),2)
        self.send('配信停止')
        self.assertEqual([x['state'] for x in self.rows('concierge_outbox') if x['kind']=='announcement'],['cancelled','cancelled'])

    def test_private_owner_pairing_only(self):
        result=bot.configure(self.db,{'action':'pair'})
        e={'source':{'type':'group','userId':'stranger'},'message':{'text':result['pair_command']}}
        bot.pair_owner(self.db,[e])
        c=bot.connect(self.db); self.assertEqual(bot.setting(c,'owner'),'owner-id'); c.close()
        e['source']={'type':'user','userId':'verified-owner'}; bot.pair_owner(self.db,[e])
        c=bot.connect(self.db); self.assertEqual(bot.setting(c,'owner'),'verified-owner'); c.close()
        e['source']['userId']='replay';bot.pair_owner(self.db,[e])
        c=bot.connect(self.db);self.assertEqual(bot.setting(c,'owner'),'verified-owner');c.close()

    def test_request_routes_only_owner_and_answer_once(self):
        self.send('朝倉へ相談'); self.send('アレルギーの相談')
        request=self.rows('concierge_requests')[0]
        notices=[x for x in self.rows('concierge_outbox') if x['kind']=='request']
        self.assertEqual(notices[0]['user_id'],'owner-id')
        bot.configure(self.db,{'action':'answer','id':request['id'],'body':'確認しました'})
        with self.assertRaises(ValueError): bot.configure(self.db,{'action':'answer','id':request['id'],'body':'重複'})
        answer=[x for x in self.rows('concierge_outbox') if x['kind']=='answer'][0]
        self.assertEqual(answer['user_id'],'customer-id')

    def test_live_availability_and_failure(self):
        day=(datetime.now(bot.JST).date()+timedelta(days=1)).isoformat()
        def lookup(d,p):
            # Real lookup needs a separate writable connection; no concierge write lock held.
            c=bot.connect(self.db)
            with c: bot.put(c,'lookup','called')
            c.close()
            return [{'course':'matsuba-seko','area':'COUNTER','time':'18:00'}]
        self.send(day+' 2名',lookup=lookup)
        payload=json.loads(self.rows('concierge_outbox')[-1]['payload'])
        self.assertIn('¥120,000',payload[0]['text'])
        self.assertIn('/concierge/book#',payload[1]['template']['actions'][0]['uri'])
        self.assertNotIn('事前決済',payload[0]['text'])
        def unavailable(d,p): raise RuntimeError()
        self.send(day+' 2名',lookup=unavailable)
        self.assertIn('確認できません',json.loads(self.rows('concierge_outbox')[-1]['payload'])[0]['text'])

    def test_group_ignored_and_unfollow_cancels(self):
        self.send('朝倉へ相談',source='group')
        self.assertFalse(self.rows('concierge_outbox'))
        self.send('メニュー');self.send('',kind='unfollow')
        self.assertEqual(self.rows('concierge_outbox')[0]['state'],'cancelled')

    def test_failed_delivery_not_success(self):
        self.send('メニュー')
        with patch.dict(os.environ,{'CONCIERGE_LINE_CHANNEL_ACCESS_TOKEN':''}): bot.deliver(self.db)
        self.assertEqual(self.rows('concierge_outbox')[0]['state'],'failed')

    def test_actual_reservation_lookup_has_no_nested_database_lock(self):
        import run
        with patch.object(run, 'DB', self.db):
            c=run.con();c.close()
            today=datetime.now(bot.JST).date()
            day=today.replace(month=11,day=10)
            if day<today: day=day.replace(year=day.year+1)
            self.send(str(day)+' 2名',lookup=run.concierge_availability)
            payload=json.loads(self.rows('concierge_outbox')[-1]['payload'])
            self.assertIn('お席をご案内できます',payload[0]['text'])

    def test_retry_uses_same_key_and_409_is_accepted(self):
        import urllib.error
        self.send('メニュー')
        first=self.rows('concierge_outbox')[0]
        with patch('urllib.request.urlopen',side_effect=OSError()) as call:
            bot.deliver(self.db)
            self.assertEqual(call.call_args.args[0].get_header('X-line-retry-key'),first['id'])
        self.assertEqual(self.rows('concierge_outbox')[0]['state'],'pending')
        c=bot.connect(self.db)
        with c:c.execute('UPDATE concierge_outbox SET next_try=0')
        c.close()
        exc=urllib.error.HTTPError('https://api.line.me',409,'conflict',{'x-line-accepted-request-id':'accepted-id'},None)
        with patch('urllib.request.urlopen',side_effect=exc) as call:
            bot.deliver(self.db)
            self.assertEqual(call.call_args.args[0].get_header('X-line-retry-key'),first['id'])
        self.assertEqual(self.rows('concierge_outbox')[0]['state'],'accepted')

    def test_arrival_waits_until_enabled(self):
        bot.configure(self.db,{'action':'disable'})
        bot.configure(self.db,{'action':'catalog','crab':'新しい蟹','origin':'北海道','arrival_date':'2026-11-10'})
        self.assertEqual(bot.status(self.db)['arrivals_pending'],1)


if __name__=='__main__': unittest.main()

