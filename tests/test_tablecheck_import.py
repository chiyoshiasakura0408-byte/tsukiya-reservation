import base64
import csv
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='tc-module-'))
import run
import tablecheck_import as tc

HEADERS=['顧客ID','氏 (漢字)','名 (漢字)','氏 (あ·ア·A)','名 (あ·ア·A)','電話 1','Eメール 1','顧客メモ','食事制限・アレルギー','予約履歴','タグ','苦手なもの','会社名']
def payload(rows):
    f=io.StringIO();w=csv.writer(f);w.writerow(['顧客']);w.writerow(HEADERS)
    for r in rows:w.writerow([r.get(k,'') for k in HEADERS])
    return {'file':base64.b64encode(f.getvalue().encode('utf-8-sig')).decode()}
def row(sid='tc1',name='山田',phone='09012345678',**fields):
    return {'顧客ID':sid,'氏 (漢字)':name,'名 (漢字)':'太郎','電話 1':phone,**fields}

class ImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.dbpatch=patch.object(run,'DB',Path(self.tmp.name)/'test.sqlite3');self.dbpatch.start()
        self.tokenpatch=patch.object(run,'ADMIN_TOKEN','test-import-only');self.tokenpatch.start()
    def tearDown(self):
        self.tokenpatch.stop();self.dbpatch.stop();self.tmp.cleanup()
    def existing(self,name='山田 太郎',phone='09012345678',email='',note='既存メモ'):
        c=run.con();key=run.customer_match_key({'guest_name':name,'phone':phone,'email':email,'id':99})
        cid=c.execute('INSERT INTO customers(match_key,name,phone,email,note,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',(key,name,phone,email,note,'now','now')).lastrowid;c.commit();c.close();return cid
    def apply(self,p):
        pre=tc.handle(run,p);return tc.handle(run,dict(p,preview_token=pre['preview_token']),True)
    def test_preview_is_read_only_then_import_is_idempotent_and_originals_preserved(self):
        long='大切な接客メモ'*500
        p=payload([row(**{'顧客メモ':long,'予約履歴':'2025年の参考履歴','食事制限・アレルギー':'えび','タグ':'VIP'})])
        pre=tc.handle(run,p);self.assertEqual(pre['new'],1)
        c=run.con();self.assertEqual(c.execute('SELECT COUNT(*) FROM customers').fetchone()[0],0);c.close()
        result=self.apply(p);self.assertEqual(result['new'],1)
        c=run.con();customer=c.execute('SELECT * FROM customers').fetchone();archive=tc.detail(c,customer['id'])[0]
        self.assertEqual(archive['fields']['顧客メモ'],long);self.assertEqual(customer['allergies'],'えび')
        self.assertEqual(c.execute('SELECT COUNT(*) FROM reservations').fetchone()[0],0);c.close()
        result=self.apply(p);self.assertEqual(result['skip'],1);self.assertEqual(result['new'],0)
    def test_matching_keeps_existing_notes_and_requires_name_and_contact(self):
        cid=self.existing();p=payload([row(**{'顧客メモ':'輸入メモ'}),row('tc2','鈴木')])
        result=self.apply(p);self.assertEqual((result['link'],result['new']),(1,1))
        c=run.con();self.assertEqual(c.execute('SELECT note FROM customers WHERE id=?',(cid,)).fetchone()[0],'既存メモ')
        self.assertEqual(tc.detail(c,cid)[0]['fields']['顧客メモ'],'輸入メモ');c.close()
    def test_same_phone_different_name_not_merged(self):
        self.existing(name='鈴木 太郎');self.assertEqual(self.apply(payload([row()]))['new'],1)
    def test_ambiguous_or_shared_target_blocks_entire_import(self):
        self.existing(phone='09012345678',email='a@example.com')
        self.existing(phone='08012345678',email='b@example.com')
        p=payload([row(**{'Eメール 1':'b@example.com'})]);pre=tc.handle(run,p);self.assertEqual(pre['review'],1)
        with self.assertRaises(ValueError):tc.handle(run,dict(p,preview_token=pre['preview_token']),True)
        c=run.con();self.assertEqual(c.execute('SELECT COUNT(*) FROM tablecheck_customer_imports').fetchone()[0],0);c.close()
    def test_duplicate_ids_bad_csv_and_unpreviewed_commit_rejected(self):
        for p in [payload([row(),row()]),{'file':'not base64'},payload([row(name='',**{'名 (漢字)':''})])]:
            with self.assertRaises(ValueError):tc.handle(run,p)
        with self.assertRaises(ValueError):tc.handle(run,payload([row()]),True)
    def test_stale_preview_rejected(self):
        p=payload([row()]);pre=tc.handle(run,p);self.existing()
        with self.assertRaises(ValueError):tc.handle(run,dict(p,preview_token=pre['preview_token']),True)
    def test_batch_failure_rolls_back(self):
        p=payload([row(),row('tc2','鈴木')]);pre=tc.handle(run,p)
        c=run.con();c.execute("CREATE TRIGGER reject_tc2 BEFORE INSERT ON tablecheck_customer_imports WHEN NEW.source_id='tc2' BEGIN SELECT RAISE(ABORT, 'test'); END;");c.commit();c.close()
        with self.assertRaises(Exception):tc.handle(run,dict(p,preview_token=pre['preview_token']),True)
        c=run.con();self.assertEqual(c.execute('SELECT COUNT(*) FROM customers').fetchone()[0],0);self.assertEqual(c.execute('SELECT COUNT(*) FROM tablecheck_customer_imports').fetchone()[0],0);c.close()
    def test_visit_counts_exclude_cancellations_future_and_overlapping_period(self):
        cid=self.existing()
        fields=row(**{'過去の予約':'4','直近の来店':'2026-10-10','キャンセル':'3','無断キャンセル':'1','予約履歴':'西天満つきや: 5'})
        c=run.con()
        c.execute('INSERT INTO tablecheck_customer_imports VALUES(?,?,?,?)',('tc1',cid,json.dumps(fields),'now'))
        for day,status in [('2026-10-10T18:00','CONFIRMED'),('2026-10-11T18:00','CONFIRMED'),('2026-10-12T18:00','CANCELLED'),('2026-11-01T18:00','CONFIRMED')]:
            c.execute("INSERT INTO reservations(source,guest_name,visit_at,party_size,amount,status,customer_id,created_at,updated_at) VALUES('DIRECT','山田 太郎',?,2,0,?,?,'now','now')",(day,status,cid))
        c.commit()
        summary=tc.visit_summaries(c,[{'id':cid}],'2026-10-17T20:00')[0]
        self.assertEqual((summary['tablecheck_visit_count'],summary['local_visit_count'],summary['overlap_visit_count'],summary['visit_count']),(4,2,1,5))
        self.assertEqual(summary['last_visit'],'2026-10-11T18:00')
        self.assertEqual(tc.visit_summaries(c,[{'id':cid}],'2026-10-17T20:00')[0],summary)
        c.close()
    def test_visit_counts_keep_local_only_and_count_overlapping_period_once(self):
        cid=self.existing();c=run.con()
        for day in ['2026-01-01T18:00','2026-01-02T18:00']:
            c.execute("INSERT INTO reservations(source,guest_name,visit_at,party_size,amount,status,customer_id,created_at,updated_at) VALUES('DIRECT','山田 太郎',?,2,0,'CONFIRMED',?,'now','now')",(day,cid))
        c.commit()
        self.assertEqual(tc.visit_summaries(c,[{'id':cid}],'2026-10-17T20:00')[0]['visit_count'],2)
        fields={'過去の予約':'1','直近の来店':'2026-01-03'}
        c.execute('INSERT INTO tablecheck_customer_imports VALUES(?,?,?,?)',('tc1',cid,json.dumps(fields),'now'));c.commit()
        self.assertEqual(tc.visit_summaries(c,[{'id':cid}],'2026-10-17T20:00')[0]['visit_count'],2)
        c.close()
    def test_http_auth_action_origin_and_full_flow(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),run.Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base=f'http://127.0.0.1:{server.server_port}'
        def request(path,data=None,headers=None):
            req=urllib.request.Request(base+path,data=json.dumps(data).encode() if data is not None else None,headers={'Content-Type':'application/json',**(headers or {})})
            return json.load(urllib.request.urlopen(req))
        auth={'x-admin-token':'test-import-only','X-Tsukiya-Action':'tablecheck-import'}
        try:
            for headers,code in [({},401),({'x-admin-token':'test-import-only'},403),({**auth,'Origin':'https://evil.example'},403)]:
                with self.assertRaises(urllib.error.HTTPError) as e:request('/api/customers/tablecheck/preview',payload([row()]),headers)
                self.assertEqual(e.exception.code,code)
            p=payload([row()]);pre=request('/api/customers/tablecheck/preview',p,auth)
            result=request('/api/customers/tablecheck/import',dict(p,preview_token=pre['preview_token']),auth)
            self.assertTrue(result['ok'])
            listed=request('/api/customers',headers=auth);self.assertEqual(len(listed),1)
            detail=request('/api/customers/'+str(listed[0]['id']),headers=auth)
            self.assertEqual(detail['tablecheck_imports'][0]['source_id'],'tc1')
        finally:server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main()
