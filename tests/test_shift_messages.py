import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
import shift_messages as s

class TransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = self.tmp.name+'/test.db'
        self.user = 'U'+'1'*32
        with sqlite3.connect(self.db) as c:
            c.executescript('CREATE TABLE line_sources(source_id TEXT,source_type TEXT,active INTEGER); CREATE TABLE line_received_events(event_id TEXT,source_id TEXT,event_type TEXT,message_type TEXT);')
            c.execute('INSERT INTO line_sources VALUES(?,?,1)', (self.user,'user'))
            c.execute('INSERT INTO line_received_events VALUES(?,?,?,?)', ('event1',self.user,'message','text'))
        self.secret = patch.object(s.line_receipts, 'bridge_secret', return_value='test-secret').start()
        self.env = patch.dict(os.environ, {'LINE_CHANNEL_ACCESS_TOKEN':'test-token'}).start()
        self.calls = []
        self.api = patch.object(s, 'line_request', side_effect=self.line).start()
        s._verified = None
    def tearDown(self):
        patch.stopall()
        self.tmp.cleanup()
    def line(self,path,token,body=None,retry_key=None):
        self.calls.append((path,body,retry_key))
        return (200, {'basicId':s.BOT_ID} if path=='info' else {}, False)
    def request(self,action='push',**kw):
        data={'action':action,'timestamp':int(time.time()),'to':self.user,'text':'シフト通知','retryKey':'12345678-1234-1234-1234-123456789012',**kw}
        raw=json.dumps(data).encode()
        sig=hmac.new(b'test-secret',s.DOMAIN+raw,hashlib.sha256).hexdigest()
        return s.handle(self.db,raw,sig)
    def test_authentication_and_status_never_send(self):
        self.assertEqual(s.handle(self.db,b'{}','invalid')[0],403)
        self.assertEqual(self.request('status',timestamp=0)[0],403)
        self.assertEqual(self.request('status')[1]['basicId'],s.BOT_ID)
        self.assertEqual([c[0] for c in self.calls],['info'])
    def test_push_dedup_and_changed_payload(self):
        self.assertTrue(self.request()[1]['accepted'])
        self.assertTrue(self.request()[1]['duplicate'])
        self.assertEqual(self.request(text='different')[0],409)
        self.assertEqual([c[0] for c in self.calls],['info','message/push'])
        self.assertEqual(self.calls[-1][2],'12345678-1234-1234-1234-123456789012')
    def test_reject_unknown_blocked_group_and_bad_keys(self):
        self.assertEqual(self.request(to='U'+'2'*32)[0],403)
        self.assertEqual(self.request(to='C'+'1'*32)[0],400)
        self.assertEqual(self.request(retryKey='bad')[0],400)
        with sqlite3.connect(self.db) as c:c.execute('UPDATE line_sources SET active=0')
        self.assertEqual(self.request()[0],403)
        self.assertEqual(self.calls,[])
    def test_reply_must_match_received_user_and_calendar_origin(self):
        self.assertEqual(self.request('reply',eventId='unknown',replyToken='fake')[0],403)
        self.assertEqual(self.request('reply',eventId='event1',replyToken='fake',url='https://evil.test')[0],400)
        self.assertEqual(self.request('reply',eventId='event1',replyToken='fake')[0],200)
        self.assertTrue(self.request('reply',eventId='event1',replyToken='fake')[1]['duplicate'])
        self.assertEqual(len([c for c in self.calls if c[0]=='message/reply']),1)
    def test_failures_not_success_and_retry_keeps_key(self):
        self.api.side_effect=lambda path,*a,**kw:(503,{},False) if path!='info' else (200,{'basicId':s.BOT_ID},False)
        self.assertEqual(self.request()[0],502)
        self.api.side_effect=lambda path,*a,**kw:(409,{},True)
        self.assertTrue(self.request()[1]['accepted'])
    def test_expired_retry_and_wrong_account(self):
        self.api.side_effect=lambda *a,**kw:(200,{'basicId':'@wrong'},False)
        self.assertEqual(self.request()[0],502)
        with sqlite3.connect(self.db) as c:c.execute('UPDATE shift_delivery_keys SET created=?',(time.time()-24*3600,))
        self.assertEqual(self.request()[0],409)

if __name__ == '__main__':unittest.main()
