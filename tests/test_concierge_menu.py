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
from vip_fixtures import approve
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
        approve(self.db,'guest')
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

    def test_course_tap_uses_only_its_registered_photo(self):
        bot.configure(self.db, {'action':'catalog','course_id':'tarabagani','photo':'https://example.com/king.jpg','menu':'前菜。焼き蟹。'})
        messages=self.send(postback='course:tarabagani')
        self.assertEqual([m['type'] for m in messages],['text','image'])
        self.assertEqual(messages[1]['originalContentUrl'],'https://example.com/king.jpg')
        self.assertEqual(len(self.send(postback='course:hairy')),1)
        self.assertEqual(self.send('コース内容')[0]['type'],'flex')
        self.assertEqual(self.send('ただいまのコース')[0]['type'],'flex')
        with patch('concierge.os.path.isfile', return_value=True):
            self.assertNotIn('quickReply', bot.text_message('ご案内'))

    def test_years_and_day_course(self):
        self.assertIn('2026年〜2027年',json.dumps(menu.annual_message(),ensure_ascii=False))
        self.assertEqual(menu.course_name_for_day(__import__('datetime').date(2026,10,29)), '活たらば蟹おまかせコース')
        self.assertIn('お問い合わせ',menu.course_name_for_day(__import__('datetime').date(2027,10,29)))
        for item in menu.catalog()['courses']:
            if not item['id'].startswith('matsuba-'):self.assertNotIn('日',menu.display_period(item))

    def test_unopened_day_request_reaches_owner_without_booking(self):
        day=(datetime.now(bot.JST).date()+timedelta(days=180)).isoformat()
        result=self.send(day+' 2名',lookup=lambda d,p:[])
        command=result[0]['quickReply']['items'][0]['action']['text']
        self.send(command)
        self.send('山田太郎 17:00希望')
        c=bot.connect(self.db)
        try:
            row=c.execute("SELECT body FROM concierge_requests").fetchone()
            self.assertIn(day,row[0]);self.assertIn('2名',row[0]);self.assertIn('山田太郎',row[0])
            self.assertEqual(c.execute('SELECT count(*) FROM concierge_proposals').fetchone()[0],0)
            self.assertEqual(c.execute("SELECT count(*) FROM concierge_outbox WHERE channel='owner' AND kind='request'").fetchone()[0],1)
        finally:c.close()

    def test_vip_time_boundaries(self):
        import vip_slots
        from types import SimpleNamespace
        app=SimpleNamespace(bookable_course_key=lambda d:'test',concierge=bot)
        day=datetime.now(bot.JST).date()+timedelta(days=1)
        for clock in ('17:00','17:59','20:59','21:00'):
            self.assertTrue(vip_slots.valid_start(app,day,clock,'test'))
        for clock in ('16:59','21:01','22:00','17:00:00'):
            self.assertFalse(vip_slots.valid_start(app,day,clock,'test'))

    def test_course_seasons_and_no_invented_summer_menu(self):
        text=menu.current_courses(self.courses)
        self.assertIn('66,000',text); self.assertIn('11月10日',text); self.assertIn('03月20日',text)
        annual=menu.annual_courses(self.courses)
        self.assertIn('10月〜11月',annual)
        self.assertIn('活たらば蟹おまかせコース',annual)
        self.assertIn('45,000',annual)
        self.assertIn('7月〜9月',annual)
        self.assertNotIn('3月21日〜11月9日',annual)
