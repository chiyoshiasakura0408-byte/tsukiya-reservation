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

    def test_confirmed_shift_bypasses_faq_and_is_relayed_once(self):
        for i, text in enumerate(('確定シフト', '確定シフト11月分', '確定シフト １１月分',
                                  '確定シフトカレンダー', 'シフト確認', '確定カレンダー')):
            event = {'webhookEventId': 'confirmed-'+str(i), 'type': 'message',
                     'source': {'type': 'user', 'userId': 'U'+'1'*32},
                     'message': {'type': 'text', 'text': text}, 'replyToken': 'test-only'}
            with self.subTest(text=text), patch.object(line_bot.staff_faq, 'question_events', wraps=line_bot.staff_faq.question_events) as faq:
                self.assertEqual(self.request([event])[0], 200)
                self.assertFalse(any(event in call.args[0] for call in faq.call_args_list))
                self.assertEqual(self.request([event])[1]['received'], 0)
            with line_bot.connect(self.db) as c:
                self.assertEqual(c.execute('SELECT COUNT(*) FROM receipt_relay').fetchone()[0], i+1)
                body = c.execute('SELECT body FROM receipt_relay ORDER BY created DESC LIMIT 1').fetchone()[0]
                self.assertEqual(json.loads(body)['events'][0]['message']['text'], text)

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
