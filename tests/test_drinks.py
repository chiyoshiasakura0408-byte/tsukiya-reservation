import http.client
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import concierge
import drinks
import line_bot
import line_delivery
import run
import supplies


class DrinksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = self.temp.name + '/drinks.sqlite'
        self.env = patch.dict(os.environ, {'LINE_CHANNEL_ACCESS_TOKEN': 'test-token'})
        self.env.start();self.addCleanup(self.env.stop)
        c = line_bot.connect(self.db)
        c.execute('INSERT INTO line_sources VALUES(?,?,?,1)', (line_delivery.TARGET, 'group', 'now'))
        c.commit();c.close()
        c = supplies.connect(self.db)
        concierge.put(c, 'owner', 'U'+'1'*32)
        c.commit();c.close()

    def stock(self, item_id=1, branch='main'):
        return next(r for r in drinks.view(self.db)['items'] if r['branch']==branch and r['item_id']==item_id)

    def save(self, qty, item_id=1, branch='main'):
        return drinks.save(self.db, {'branch':branch,'actor':'担当','items':[
            {'item_id':item_id,'version':self.stock(item_id,branch)['version'],'qty':qty}]})

    def test_23_products_both_branches_and_idempotent_seed(self):
        self.assertEqual(len(drinks.ITEMS),23)
        self.assertEqual(drinks.ITEMS[-1],'シャンパン')
        self.assertEqual(len(drinks.view(self.db)['items']),46)
        self.save(2.5,item_id=23)
        self.assertEqual(self.stock(23)['qty'],2.5)
        self.assertIsNone(self.stock(23,'annex')['qty'])
        for _ in range(2):supplies.connect(self.db).close()
        self.assertEqual(self.stock(23)['qty'],2.5)
        self.assertEqual(drinks.view(self.db)['summary']['main'],{'total':23,'checked':1,'missing':22})
        self.assertIsNone(supplies.view(self.db)['items'][0]['qty'])

    def test_zero_blank_and_no_weekly_reset(self):
        at=datetime(2026,10,24,10,0,tzinfo=supplies.JST)
        with patch.object(supplies,'now',return_value=at):self.save(0)
        with patch.object(supplies,'now',return_value=at+timedelta(days=16)):
            self.assertTrue(self.stock()['current'])
            text=drinks.view(self.db)['preview']['main']
            self.assertIn('水：0（確認 2026-10-24 10:00）',text)
        self.save(None)
        self.assertFalse(self.stock()['current'])
        self.assertIsNone(self.stock()['checked_at'])

    def test_reject_order_fields_invalid_numbers_and_atomic_conflicts(self):
        for qty in (-1,True,'3',float('nan'),float('inf')):
            with self.subTest(qty=qty),self.assertRaises(ValueError):self.save(qty)
        with self.assertRaises(ValueError):
            drinks.save(self.db,{'branch':'main','actor':'担当','items':[{'item_id':1,'version':0,'qty':1,'manual_need':True}]})
        self.save(1)
        with self.assertRaises(supplies.Conflict):
            drinks.save(self.db,{'branch':'main','actor':'担当','items':[{'item_id':2,'version':0,'qty':20},{'item_id':1,'version':0,'qty':99}]})
        self.assertIsNone(self.stock(2)['qty'])
        self.assertEqual(self.stock()['qty'],1)
        c=supplies.connect(self.db)
        self.assertEqual(c.execute('SELECT COUNT(*) FROM drink_audit').fetchone()[0],1)
        c.close()

    def test_report_all_stock_only_to_owner_and_deduplicate(self):
        self.save(0)
        self.save(1.5,item_id=23)
        data={'branch':'main','actor':'担当'}
        self.assertEqual(drinks.submit(self.db,data,'https://example.invalid')['state'],'pending')
        self.assertTrue(drinks.submit(self.db,data,'https://example.invalid')['already_queued'])
        c=supplies.connect(self.db)
        rows=c.execute('SELECT * FROM concierge_outbox').fetchall()
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['user_id'],'U'+'1'*32)
        text=json.loads(rows[0]['payload'])[0]['text']
        for name in drinks.ITEMS:self.assertIn('・'+name+'：',text)
        self.assertIn('未入力 21品目',text)
        self.assertIn('シャンパン：1.5',text)
        self.assertIn('発注は朝倉が判断します',text)
        self.assertNotIn('推奨',text)
        self.assertNotIn('要発注',text)
        self.assertNotIn('■ 別邸',text)
        self.assertIn('/drinks#access=',text)
        self.assertEqual(c.execute('SELECT COUNT(*) FROM drink_reports').fetchone()[0],1)
        c.close()

    def test_no_scheduled_drinks_or_onboarding(self):
        for at in [datetime(2026,10,21,9,tzinfo=supplies.JST),datetime(2026,10,24,9,tzinfo=supplies.JST),datetime(2026,10,25,23,tzinfo=supplies.JST)]:
            supplies.tick(self.db,'https://example.invalid',at)
        request=supplies.briefing_request(self.db,'2026-10-24','https://example.invalid')
        self.assertNotIn('酒類',request)
        self.assertNotIn('/drinks',request)
        c=supplies.connect(self.db)
        for r in c.execute('SELECT payload FROM concierge_outbox'):
            self.assertNotIn('酒類',r[0])
            self.assertNotIn('/drinks',r[0])
        self.assertEqual(c.execute('SELECT COUNT(*) FROM drink_reports').fetchone()[0],0)
        c.close()

    def test_drinks_command_uses_verified_inventory_access(self):
        c=supplies.connect(self.db)
        def event(event_id,destination):
            return {'webhookEventId':event_id,'source':{'type':'group','groupId':destination},'message':{'type':'text','text':'酒類'}}
        with c:
            self.assertEqual(supplies.ingest(c,[event('other','C'+'2'*32)],'https://example.invalid'),set())
            supplies.ingest(c,[event('allowed',line_delivery.TARGET)],'https://example.invalid')
            supplies.ingest(c,[event('allowed',line_delivery.TARGET)],'https://example.invalid')
        rows=c.execute('SELECT * FROM concierge_outbox').fetchall()
        self.assertEqual(len(rows),1)
        text=json.loads(rows[0]['payload'])[0]['text']
        self.assertIn('/drinks#access=',text)
        self.assertNotIn('要発注',text)
        c.close()
        token=text.split('#access=')[1].split('\n')[0]
        self.assertTrue(supplies.authorized(self.db,token))

    def test_http_requires_auth_origin_and_stock_only_input(self):
        c=supplies.connect(self.db)
        token=supplies.new_link(c,'https://example.invalid',page='drinks').split('#access=')[1]
        c.commit();c.close()
        with patch.object(run,'DB',self.db),patch.object(run,'ADMIN_TOKEN','test-admin'),patch.object(run,'APP_BASE_URL','https://example.invalid'):
            server=ThreadingHTTPServer(('127.0.0.1',0),run.Handler)
            threading.Thread(target=server.serve_forever,daemon=True).start()
            def request(path,body=None,auth=True,origin='https://example.invalid'):
                conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=4)
                headers={'Origin':origin,'X-Tsukiya-Action':'supplies','Content-Type':'application/json'}
                if auth:headers['X-Supplies-Token']=token
                conn.request('POST' if body is not None else 'GET',path,json.dumps(body) if body is not None else None,headers)
                r=conn.getresponse();status=r.status;payload=r.read();conn.close()
                return status,payload
            try:
                self.assertEqual(request('/api/drinks',auth=False)[0],401)
                self.assertEqual(request('/api/drinks')[0],200)
                self.assertEqual(request('/api/reservations')[0],401)
                data={'branch':'annex','actor':'担当','items':[{'item_id':23,'version':0,'qty':2}]}
                self.assertEqual(request('/api/drinks/save',data,origin='https://attacker.invalid')[0],403)
                self.assertEqual(request('/api/drinks/save',data)[0],200)
                self.assertEqual(request('/api/drinks/report',{'branch':'annex','actor':'担当'})[0],200)
                self.assertEqual(self.stock(23,'annex')['qty'],2)
                self.assertIsNone(self.stock(23)['qty'])
            finally:
                server.shutdown();server.server_close()


if __name__=='__main__':unittest.main()
