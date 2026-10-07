import secrets
import tempfile
import unittest
import http.client
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import concierge
import english_concierge as en
import run


class EnglishConciergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'test.sqlite'
        c = concierge.connect(self.db)
        with c:
            concierge.put(c, 'owner', 'test-owner')
        c.close()
        self.data = dict(action='submit', token=secrets.token_hex(32), name='Test Guest', body='May we arrange flowers?', consent=True)

    def call(self, data=None):
        return en.handle(self.db, data or self.data, 'test-peer', 'https://example.invalid')

    def test_private_round_trip_and_idempotency(self):
        code, result = self.call()
        self.assertEqual(code, 200)
        self.assertEqual(result['answer'], '')
        self.assertEqual(self.call()[1]['id'], result['id'])
        c = concierge.connect(self.db)
        self.assertEqual(c.execute('SELECT count(*) FROM concierge_outbox').fetchone()[0], 1)
        self.assertEqual(c.execute('SELECT channel FROM concierge_outbox').fetchone()[0], 'owner')
        self.assertNotIn(self.data['token'], str([tuple(r) for r in c.execute('SELECT * FROM english_enquiries')]))
        c.close()
        concierge.configure(self.db, dict(action='answer', id=result['id'], body='We can prepare flowers. Please confirm your preferred budget.'))
        code, result = self.call(dict(action='status', token=self.data['token']))
        self.assertEqual(code, 200)
        self.assertIn('preferred budget', result['answer'])
        c = concierge.connect(self.db)
        self.assertEqual(c.execute('SELECT count(*) FROM concierge_outbox').fetchone()[0], 1)
        self.assertEqual(c.execute('SELECT status FROM concierge_requests').fetchone()[0], '英語窓口に回答掲載済み')
        c.close()
        self.assertEqual(self.call(dict(action='status', token=secrets.token_hex(32)))[0], 404)

    def test_validation_does_not_queue(self):
        for change in ({'consent':False}, {'body':'x'*1501}, {'name':''}, {'token':'bad'}, {'website':'spam'}, {'action':'bad'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.call({**self.data, **change})
        c = concierge.connect(self.db)
        self.assertEqual(c.execute('SELECT count(*) FROM concierge_requests').fetchone()[0], 0)
        c.close()

    def test_expiry_and_rate_limit(self):
        _, result = self.call()
        with patch('english_concierge.time.time', return_value=result['expires']+1):
            self.assertEqual(self.call(dict(action='status',token=self.data['token']))[0], 410)
            with self.assertRaises(ValueError):
                concierge.configure(self.db, dict(action='answer',id=result['id'],body='Too late'))
        for _ in range(19):
            self.assertEqual(self.call({**self.data,'token':secrets.token_hex(32)})[0],200)
        self.assertEqual(self.call({**self.data,'token':secrets.token_hex(32)})[0],429)
        self.assertEqual(self.call()[0],200)

    def test_missing_owner_returns_unavailable(self):
        c = concierge.connect(self.db)
        with c:
            concierge.put(c,'owner','')
        c.close()
        self.assertEqual(self.call()[0],503)

    def test_http_public_submit_and_admin_reply(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), run.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        def request(method, path, data=None, header=None):
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            headers = {'Content-Type':'application/json'}
            if header:
                headers['X-Tsukiya-Action'] = header
            conn.request(method, path, json.dumps(data) if data is not None else None, headers)
            response = conn.getresponse()
            status, payload, cache = response.status, response.read(), response.getheader('Cache-Control')
            conn.close()
            return status, payload, cache
        try:
            with patch.object(run, 'DB', self.db):
                status, page, _ = request('GET','/concierge/en')
                self.assertEqual(status,200)
                self.assertIn(b'English Concierge',page)
                self.assertEqual(request('POST','/api/public/english-concierge',self.data)[0],403)
                status, payload, cache = request('POST','/api/public/english-concierge',self.data,'english-concierge')
                self.assertEqual(status,200)
                self.assertEqual(cache,'no-store')
                result = json.loads(payload)
                answer = dict(action='answer',id=result['id'],body='Thank you. We will arrange flowers for your visit.')
                self.assertEqual(request('POST','/api/concierge/configure',answer,'concierge')[0],401)
                with patch.object(run.Handler,'auth',return_value=True):
                    self.assertEqual(request('POST','/api/concierge/configure',answer,'concierge')[0],200)
                status, payload, _ = request('POST','/api/public/english-concierge',dict(action='status',token=self.data['token']),'english-concierge')
                self.assertEqual(status,200)
                self.assertIn('arrange flowers',json.loads(payload)['answer'])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
