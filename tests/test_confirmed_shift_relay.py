import json
import sqlite3
import unittest
import line_receipts

class ConfirmedShiftRelayTests(unittest.TestCase):
    def test_confirmed_commands_are_enqueued_for_employee(self):
        for text in ('確定シフト', '確定シフト11月分', '確定シフト １１月分', '確定シフトカレンダー', 'シフト確認', '確定カレンダー', 'シフト', '希望シフト'):
            with self.subTest(text=text), sqlite3.connect(':memory:') as c:
                event={'webhookEventId':'test-event','type':'message','source':{'type':'user','userId':'U'+'1'*32},'message':{'type':'text','text':text}}
                payload={'events':[event]};raw=json.dumps(payload).encode()
                line_receipts.enqueue(c,raw,'test-signature',payload)
                self.assertEqual(c.execute('SELECT body FROM receipt_relay').fetchone()[0],raw)
    def test_unrelated_text_and_group_commands_are_not_enqueued(self):
        for source,text in (({'type':'user','userId':'U'+'1'*32},'こんにちは'),({'type':'group','groupId':'C'+'1'*32},'確定シフト')):
            with sqlite3.connect(':memory:') as c:
                payload={'events':[{'webhookEventId':'test-event','type':'message','source':source,'message':{'type':'text','text':text}}]}
                line_receipts.enqueue(c,json.dumps(payload).encode(),'test',payload)
                self.assertEqual(c.execute('SELECT count(*) FROM receipt_relay').fetchone()[0],0)

if __name__=='__main__': unittest.main()
