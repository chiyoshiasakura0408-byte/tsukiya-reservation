import base64
import hashlib
import hmac
import json
import os
import tempfile
import unittest
from unittest.mock import patch
import line_bot


class LineWebhookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = self.temp.name + '/test.sqlite'
        self.env = patch.dict(os.environ, {'LINE_CHANNEL_SECRET': 'test-secret'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def request(self, events):
        raw = json.dumps({'events': events}, ensure_ascii=False).encode()
        signature = base64.b64encode(hmac.new(b'test-secret', raw, hashlib.sha256).digest()).decode()
        return line_bot.receive(self.db, raw, signature)

    def test_verify_empty_events(self):
        self.assertEqual(self.request([])[0], 200)
        self.assertIsNotNone(line_bot.status(self.db)['last_verified_at'])

    def test_fail_closed(self):
        self.assertEqual(line_bot.receive(self.db, b'{}', '')[0], 403)
        with patch.dict(os.environ, {'LINE_CHANNEL_SECRET': ''}):
            self.assertEqual(line_bot.receive(self.db, b'{}', '')[0], 503)
        self.assertFalse(os.path.exists(self.db))

    def test_dedup_and_no_chat_storage(self):
        event = {'webhookEventId': 'event1', 'type': 'message',
                 'source': {'type': 'group', 'groupId': 'test-group'},
                 'message': {'type': 'text', 'text': '秘密の会話'}, 'replyToken': 'secret-reply'}
        self.assertEqual(self.request([event])[1]['received'], 1)
        self.assertEqual(self.request([event])[1]['received'], 0)
        c = line_bot.connect(self.db)
        dump = '\n'.join(c.iterdump())
        c.close()
        self.assertNotIn('秘密の会話', dump)
        self.assertNotIn('secret-reply', dump)
        self.assertEqual(len(line_bot.status(self.db)['sources']), 1)

    def test_malformed_batch_not_partially_saved(self):
        self.assertEqual(self.request([{'webhookEventId': 'e', 'type': 'join'}, None])[0], 400)
        self.assertFalse(os.path.exists(self.db))

    def test_leave_marks_inactive(self):
        self.request([{'webhookEventId': 'a', 'type': 'join', 'source': {'type': 'group', 'groupId': 'g'}}])
        self.request([{'webhookEventId': 'b', 'type': 'leave', 'source': {'type': 'group', 'groupId': 'g'}}])
        self.assertEqual(line_bot.status(self.db)['sources'][0]['active'], 0)

    def test_tampered_body_rejected(self):
        raw = b'{"events":[]}'
        sig = base64.b64encode(hmac.new(b'test-secret', raw, hashlib.sha256).digest()).decode()
        self.assertEqual(line_bot.receive(self.db, raw + b' ', sig)[0], 403)


if __name__ == '__main__':
    unittest.main()
