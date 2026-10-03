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
import concierge_menu as menu


class MenuFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.db = self.tmp.name + '/test.sqlite'
        self.env = patch.dict(os.environ, {'CONCIERGE_LINE_CHANNEL_SECRET':'secret'}); self.env.start(); self.addCleanup(self.env.stop)
        c = bot.connect(self.db)
        with c:
            bot.put(c, 'enabled', '1'); bot.put(c, 'owner', 'owner')
        c.close()
        self.counter = 0
        self.courses = {'matsuba-seko': ('松葉蟹、せこ蟹',60000),'matsuba-fukahire':('松葉蟹とふかひれ',60000)}

    def send(self, text=None, postback=None, lookup=None, event_id=None):
        self.counter += 1
        event = {'webhookEventId': event_id or str(self.counter), 'source': {'type':'user','userId':'guest'}}
        if postback is not None:
            event.update(type='postback', postback={'data':postback})
        else:
            event.update(type='message', message={'type':'text','text':text})
        raw=json.dumps({'events':[event]}).encode()
        sig=base64.b64encode(hmac.new(b'secret',raw,hashlib.sha256).digest()).decode()
        result=bot.receive(self.db,raw,sig,lookup or (lambda d,p:[]),self.courses,'https://example.com')
        self.assertEqual(result[0],200)
        c=bot.connect(self.db)
        try:
            rows=c.execute("select payload from concierge_outbox where channel='customer' order by created").fetchall()
            return json.loads(rows[-1][0]) if rows else []
        finally:c.close()

    def test_calendar_date_party_and_live_seat_proposal(self):
        messages=self.send('空席')
        self.assertEqual(messages[0]['type'],'flex')
        day=(datetime.now(bot.JST).date()+timedelta(days=1)).isoformat()
        messages=self.send(postback='visit:'+day)
        actions=messages[0]['quickReply']['items']
        self.assertEqual(actions[0]['action']['text'],day+' 2名')
        calls=[]
        def live(d,p):
            calls.append((str(d),p))
            return [{'time':'18:00','area':'COUNTER','course':'matsuba-seko'}]
        result=self.send(actions[0]['action']['text'],lookup=live)
        self.assertEqual(calls,[(day,2)])
        self.assertIn('/concierge/book#',result[1]['template']['actions'][0]['uri'])

    def test_invalid_and_duplicate_postbacks_do_not_create_bookings(self):
        self.send(postback='calendar:9999-99',event_id='same')
        before=self.send(postback='calendar:9999-99',event_id='same')
        self.assertEqual(before[0]['type'],'flex')
        self.send(postback='visit:2020-01-01')
        self.send(postback='visit:2026-99-99')
        c=bot.connect(self.db)
        self.assertEqual(c.execute('select count(*) from concierge_proposals').fetchone()[0],0)
        self.assertEqual(c.execute('select count(*) from concierge_outbox').fetchone()[0],3)
        c.close()

    def test_menu_clears_consultation_and_vip_routes_owner(self):
        self.send('VIP担当に相談'); self.send('ただいまのコース')
        c=bot.connect(self.db);self.assertEqual(c.execute('select count(*) from concierge_requests').fetchone()[0],0);c.close()
        self.send('VIP担当に相談'); self.send('誕生日のお祝いを相談したい')
        c=bot.connect(self.db)
        row=c.execute("select user_id,payload from concierge_outbox where kind='request'").fetchone()
        self.assertEqual(row[0],'owner'); self.assertIn('誕生日',row[1]);c.close()

    def test_content_includes_registered_media_and_paragraphs(self):
        c=bot.connect(self.db)
        with c:bot.put(c,'catalog',json.dumps({'menu':'前菜。焼き蟹。','photo':'https://example.com/dish.jpg','video':'https://example.com/dish.mp4','preview':'https://example.com/preview.jpg'}))
        c.close()
        messages=self.send('コース内容')
        self.assertEqual([m['type'] for m in messages],['text','image','video'])
        self.assertIn('前菜。\n\n焼き蟹。',messages[0]['text'])
        self.assertNotIn('quickReply', messages[0])

    def test_course_seasons_and_no_invented_summer_menu(self):
        text=menu.current_courses(self.courses)
        self.assertIn('60,000',text); self.assertIn('11月10日',text); self.assertIn('03月20日',text)
        self.assertIn('3月21日〜11月9日',menu.annual_courses(self.courses))
        self.assertIn('お問い合わせ',menu.annual_courses(self.courses))
