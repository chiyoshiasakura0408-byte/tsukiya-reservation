import json
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta
from unittest.mock import patch,MagicMock
import test_vip_access as fixtures
import concierge as bot
import vip_liff as app

class LiffTests(unittest.TestCase):
    send=fixtures.VipTests.send
    dbquery=fixtures.VipTests.dbquery
    apply=fixtures.VipTests.apply
    decision=fixtures.VipTests.decision
    approve=fixtures.VipTests.approve
    def setUp(self):
        fixtures.VipTests.setUp(self)
        self.user='U'+'a'*32
        self.approve(self.user)
        c=bot.connect(self.db)
        with c:
            app.configure(c,{'liff_id':'1234567890-abcdef','channel_id':'1234567890'})
            c.execute('DELETE FROM concierge_outbox')
        c.close()
        self.identity={'sub':self.user,'aud':'1234567890','iss':'https://access.line.me','exp':time.time()+600}
        self.reply=MagicMock();self.reply.__enter__.return_value.read.side_effect=lambda *a:json.dumps(self.identity).encode()
        p=patch.object(app.urllib.request,'urlopen',return_value=self.reply);p.start();self.addCleanup(p.stop)
        self.data={'id_token':'x'*30,'date':str(datetime.now(bot.JST).date()+timedelta(days=800)), 'time':'17:00','party':1,'name':'VIP顧客','note':'個室希望','acknowledged':True,'nonce':'a'*32}

    def call(self,**kwargs):return app.handle(self.db,'request',{**self.data,**kwargs},'https://example.com')

    def test_liff_request_integrates_without_reservation_or_payment(self):
        result=self.call()
        self.assertFalse(result['confirmed'])
        self.assertEqual(len(self.dbquery('SELECT * FROM concierge_requests')),1)
        self.assertEqual(self.dbquery('SELECT * FROM reservations'),[])
        messages=self.dbquery('SELECT * FROM concierge_outbox')
        self.assertEqual([(r['channel'],r['kind']) for r in messages],[('owner','request')])
        self.assertIn('事前決済なし',messages[0]['payload'])
        self.assertEqual(app.handle(self.db,'profile',{'id_token':'x'*30},'')['times'],app.TIMES)

    def test_liff_boundary_times_and_party(self):
        for i,(clock,party) in enumerate([('17:00',1),('17:30',8),('21:00',8)]):
            self.call(time=clock,party=party,nonce=str(i)*32)
        for changes in [{'time':'16:30'},{'time':'21:30'},{'time':'19:17'},{'party':0},{'party':9},{'party':True},{'party':'2'},{'date':'2020-01-01'},{'acknowledged':False}]:
            with self.subTest(changes=changes),self.assertRaises(ValueError):self.call(**changes)

    def test_liff_idempotency_concurrent_and_new_nonce(self):
        with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(lambda _:self.call(),range(4)))
        self.assertEqual(len({r['request_id'] for r in results}),1)
        self.assertEqual(self.call(nonce='b'*32)['request_id'],results[0]['request_id'])
        self.assertEqual(len(self.dbquery('SELECT * FROM concierge_outbox')),1)
        with self.assertRaises(ValueError):self.call(party=3)

    def test_liff_invalid_identity_and_revoked_vip(self):
        for changes in [{'aud':'other'},{'iss':'https://evil.invalid'},{'exp':0},{'sub':None},{'sub':'U'+'b'*32}]:
            original=self.identity.copy();self.identity.update(changes)
            with self.subTest(changes=changes),self.assertRaises(PermissionError):self.call()
            self.identity=original
        c=bot.connect(self.db)
        with c:c.execute("UPDATE vip_access SET status='withdrawn' WHERE user_id=?",(self.user,))
        c.close()
        with self.assertRaises(PermissionError):self.call()
        self.assertFalse(self.dbquery('SELECT * FROM concierge_requests'))

    def test_liff_atomic_rollback_if_notification_fails(self):
        with patch.object(bot,'enqueue',side_effect=RuntimeError('failed')):
            with self.assertRaises(RuntimeError):self.call()
        self.assertEqual(self.dbquery('SELECT * FROM concierge_requests'),[])

    def test_liff_configured_entry_opens_screen(self):
        c=bot.connect(self.db)
        result=bot.respond(c,self.user,'空席案内',None,{},'https://example.com')
        c.close()
        self.assertEqual(result[0]['template']['actions'][0]['uri'],'https://liff.line.me/1234567890-abcdef')

    def test_liff_http_requires_proof_and_same_origin_header(self):
        import http.client
        import threading
        from http.server import ThreadingHTTPServer
        import run
        server=ThreadingHTTPServer(('127.0.0.1',0),run.Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        def request(path,data=None,headers=None):
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port)
            conn.request('POST' if data is not None else 'GET',path,body=json.dumps(data) if data is not None else None,headers=headers or {})
            r=conn.getresponse();body=r.read();status=r.status;conn.close();return status,body
        self.assertEqual(request('/concierge/vip')[0],200)
        self.assertEqual(request('/api/vip-liff/request',self.data)[0],403)
        headers={'Content-Type':'application/json','X-Tsukiya-Action':'vip-liff'}
        self.assertEqual(request('/api/vip-liff/request',{**self.data,'id_token':''},headers)[0],403)
        self.assertEqual(request('/api/vip-liff/request',self.data,headers)[0],200)
        self.assertEqual(request('/api/concierge/configure',{'action':'liff','liff_id':'1234567890-hijxyz','channel_id':'1234567890'},{'X-Tsukiya-Action':'concierge'})[0],401)
        self.assertFalse(self.dbquery('SELECT * FROM reservations'))
