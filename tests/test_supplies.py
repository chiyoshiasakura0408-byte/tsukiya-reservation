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
import line_bot
import line_delivery
import run
import supplies


class SuppliesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = self.temp.name + '/inventory.sqlite'
        self.env = patch.dict(os.environ, {'LINE_CHANNEL_ACCESS_TOKEN': 'test-token'})
        self.env.start()
        self.addCleanup(self.env.stop)
        c = line_bot.connect(self.db)
        c.execute('INSERT INTO line_sources VALUES(?,?,?,1)', (line_delivery.TARGET, 'group', '2026-10-21'))
        c.commit()
        c.close()
        c = supplies.connect(self.db)
        concierge.put(c, 'owner', 'U' + '1' * 32)
        c.commit()
        c.close()

    def stock(self, item=1, branch='main'):
        return next(r for r in supplies.view(self.db)['items'] if r['item_id'] == item and r['branch'] == branch)

    def save(self, item=1, **values):
        return supplies.save(self.db, {'branch': 'main', 'actor': '確認担当', 'items': [dict(item_id=item, version=self.stock(item)['version'], **values)]})

    def test_blank_zero_pending_and_manual(self):
        self.assertEqual(self.stock()['status'], '未確認')
        self.save(qty=0, reorder=2, target=5, pending=0, confirm=True)
        self.assertEqual((self.stock()['status'], self.stock()['recommend']), ('要発注', 5))
        self.assertTrue(self.stock()['current'])
        self.save(pending=5)
        self.assertEqual(self.stock()['status'], '入荷待ち')
        self.save(reorder=None, target=None, manual_need=True, requested=None)
        self.assertEqual(self.stock()['status'], '要発注')
        self.assertIsNone(self.stock()['recommend'])

    def test_atomic_conflict_and_invalid_inputs(self):
        self.save(qty=1)
        with self.assertRaises(supplies.Conflict):
            supplies.save(self.db, {'branch':'main','actor':'A','items':[{'item_id':2,'version':0,'qty':10},{'item_id':1,'version':0,'qty':99}]})
        self.assertIsNone(self.stock(2)['qty'])
        self.assertEqual(self.stock()['qty'], 1)
        for values in ({'qty':-1},{'qty':True},{'qty':float('nan')},{'qty':float('inf')},{'qty':'2'},{'reorder':5,'target':2},{'confirm':True,'qty':None}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.save(**values)

    def test_check_history_and_week_rollover(self):
        monday = supplies.now().replace(hour=12, minute=0) - timedelta(days=supplies.now().weekday())
        with patch.object(supplies, 'now', return_value=monday):
            self.save(qty=3, confirm=True)
            self.assertTrue(self.stock()['current'])
        with patch.object(supplies, 'now', return_value=monday+timedelta(days=7)):
            self.assertFalse(self.stock()['current'])
        c = supplies.connect(self.db)
        self.assertEqual(c.execute('SELECT COUNT(*) FROM supply_audit').fetchone()[0], 1)
        c.close()

    def test_scoped_expiring_link(self):
        c = supplies.connect(self.db)
        url = supplies.new_link(c, 'https://example.invalid')
        c.commit()
        c.close()
        token = url.split('#access=')[1]
        self.assertTrue(supplies.authorized(self.db, token))
        self.assertFalse(supplies.authorized(self.db, token+'x'))
        with patch.object(supplies.time, 'time', return_value=supplies.time.time()+9*86400):
            self.assertFalse(supplies.authorized(self.db, token))

    def test_report_no_false_all_clear_and_dedup(self):
        self.save(qty=1, manual_need=True, requested=4, confirm=True)
        first=supplies.submit(self.db, {'branch':'main','actor':'担当'}, 'https://example.invalid')
        second=supplies.submit(self.db, {'branch':'main','actor':'担当'}, 'https://example.invalid')
        self.assertEqual(first['state'], 'pending')
        self.assertTrue(second['already_queued'])
        c=supplies.connect(self.db)
        record=c.execute('SELECT * FROM concierge_outbox').fetchone()
        text=json.loads(record['payload'])[0]['text']
        self.assertEqual(record['user_id'],'U'+'1'*32)
        self.assertIn('未確認 33品目',text)
        self.assertIn('推奨発注 4',text)
        self.assertNotIn('要発注の備品はありません',text)
        self.assertEqual(c.execute('SELECT COUNT(*) FROM supply_reports').fetchone()[0],1)
        c.close()

    def test_weekly_schedule_and_restart_idempotency(self):
        mon=datetime(2026,10,26,12,0,tzinfo=supplies.JST)
        for _ in range(2):supplies.tick(self.db,'https://example.invalid',mon)
        for _ in range(2):supplies.tick(self.db,'https://example.invalid',mon.replace(hour=17))
        c=supplies.connect(self.db)
        self.assertEqual(c.execute('SELECT COUNT(*) FROM supply_notices').fetchone()[0],4)
        recipients=[r[0] for r in c.execute('SELECT user_id FROM concierge_outbox')]
        self.assertEqual(recipients.count(line_delivery.TARGET),2)
        self.assertEqual(recipients.count('U'+'1'*32),2)
        c.close()

    def test_command_restricts_destinations_and_deduplicates(self):
        c=supplies.connect(self.db)
        def event(id, group):
            return {'webhookEventId':id,'source':{'type':'group','groupId':group},'message':{'type':'text','text':'備品'}}
        with c:
            self.assertEqual(supplies.ingest(c,[event('other','C'+'2'*32)],'https://example.invalid'),set())
            for _ in range(2):
                self.assertEqual(supplies.ingest(c,[event('allowed',line_delivery.TARGET)],'https://example.invalid'),{'allowed'})
        self.assertEqual(c.execute('SELECT COUNT(*) FROM concierge_outbox').fetchone()[0],1)
        c.close()

    def test_inactive_group_no_onboarding(self):
        c=supplies.connect(self.db)
        with c:c.execute('UPDATE line_sources SET active=0')
        c.close()
        supplies.tick(self.db,'https://example.invalid')
        c=supplies.connect(self.db)
        self.assertEqual(c.execute('SELECT COUNT(*) FROM concierge_outbox').fetchone()[0],0)
        c.close()


    def test_http_scope_origin_save_and_report(self):
        c=supplies.connect(self.db)
        token=supplies.new_link(c,'https://example.invalid').split('#access=')[1]
        c.commit();c.close()
        with patch.object(run,'DB',self.db), patch.object(run,'ADMIN_TOKEN','test-admin'), patch.object(run,'APP_BASE_URL','https://example.invalid'):
            server=ThreadingHTTPServer(('127.0.0.1',0),run.Handler)
            threading.Thread(target=server.serve_forever,daemon=True).start()
            def request(path,body=None,auth=True,origin='https://example.invalid'):
                conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=4)
                headers={'Origin':origin,'X-Tsukiya-Action':'supplies','Content-Type':'application/json'}
                if auth:headers['X-Supplies-Token']=token
                conn.request('POST' if body else 'GET',path,json.dumps(body) if body else None,headers)
                r=conn.getresponse();status=r.status;raw=r.read();conn.close()
                return status,raw
            try:
                self.assertEqual(request('/api/supplies',auth=False)[0],401)
                self.assertEqual(request('/api/reservations')[0],401)
                self.assertEqual(request('/api/supplies')[0],200)
                body={'branch':'main','actor':'担当','items':[{'item_id':1,'version':0,'qty':1,'manual_need':True,'confirm':True}]}
                self.assertEqual(request('/api/supplies/save',body,origin='https://attacker.invalid')[0],403)
                self.assertEqual(request('/api/supplies/save',body)[0],200)
                self.assertEqual(request('/api/supplies/report',{'branch':'main','actor':'担当'})[0],200)
                self.assertEqual(self.stock()['qty'],1)
            finally:
                server.shutdown();server.server_close()


if __name__ == '__main__':
    unittest.main()
