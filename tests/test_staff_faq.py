import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import staff_faq


class StaffFaqTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = self.tmp.name + '/faq.sqlite'
        env = patch.dict(os.environ, {'OPENAI_API_KEY': '', 'LINE_CHANNEL_ACCESS_TOKEN': ''})
        env.start()
        self.addCleanup(env.stop)

    def event(self, text, kind='user'):
        return {'type': 'message', 'source': {'type': kind, 'userId': 'staff'},
                'replyToken': 'fake-reply', 'message': {'type': 'text', 'text': text}}

    def assert_catalog(self, answer):
        included_lines = [line for line in answer.splitlines() if line.startswith('・')]
        self.assertEqual(len(included_lines), 22)
        for item in staff_faq.drink_catalog()['items']:
            present = any(line.startswith('・' + item['name_ja']) for line in included_lines)
            self.assertEqual(present, item['included'], item['name_ja'])
        for required in ('11月10日〜3月20日', '2名で一合まで', '提供できない場合',
                         '180ml', '必ず当日の在庫', 'VIP通し予約', '冬季以外', '奇数人数'):
            self.assertIn(required, answer)
        self.assertLessEqual(len(answer), staff_faq.MAX_REPLY)
        self.assertNotIn('\\n', answer)

    def test_screenshot_questions_return_complete_catalog_without_api(self):
        for text in ('無料ドリンク', '無料ドリンクは？', '質問無料ドリンクは？',
                     '質問 フリードリンクの対象は？', 'フリードリンク一覧',
                     '無料の飲み物を教えてください', '無料ドリンクは何がありますか？',
                     'フリードリンクを全部見せて'):
            with self.subTest(text=text), patch.object(staff_faq.urllib.request, 'urlopen') as api:
                question = staff_faq.question_events([self.event(text)])[0][1]
                self.assert_catalog(staff_faq.model_answer(question, self.db))
                api.assert_not_called()
        state = staff_faq.status(self.db)
        self.assertEqual(state['latest']['answer_source']['outcome'], 'approved_catalog')
        self.assertNotIn('openai', state['latest'])
        self.assertEqual(state['drink_knowledge']['included_count'], 22)

    def test_natural_question_passes_shared_approved_facts_to_gpt(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            'status': 'completed', 'output': [{'type': 'message', 'content': [
                {'type': 'output_text', 'text': '冬季通常コースでは対象です。'}]}]}).encode()
        question = 'シャンパンもコース料金に入っていますか？'
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'fake-key'}), \
                patch.object(staff_faq.urllib.request, 'urlopen', return_value=response) as api:
            self.assertEqual(staff_faq.model_answer(question, self.db), '冬季通常コースでは対象です。')
        payload = json.loads(api.call_args.args[0].data)
        instructions = payload['instructions']
        catalog = json.loads(instructions.split('朝倉承認済みドリンク一覧：\n', 1)[1])
        self.assertEqual(catalog, staff_faq.drink_catalog())
        self.assertNotIn('確定表をこのAIが持っていない', instructions)
        self.assertEqual(payload['input'][0]['content'][0]['text'], question)
        self.assertFalse(payload['store'])
        self.assertEqual(staff_faq.status(self.db)['latest']['answer_source']['outcome'], 'openai')

    def test_api_failure_gives_general_rules_without_inventing_odd_party_allowance(self):
        error = urllib.error.HTTPError('https://api.openai.com/v1/responses', 429, 'limited', {}, None)
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'fake-key'}), \
                patch.object(staff_faq.urllib.request, 'urlopen', side_effect=error):
            answer = staff_faq.model_answer('3人なら十四代は何合ですか？', self.db)
        self.assert_catalog(answer)
        self.assertIn('一般規定', answer)
        self.assertNotIn('1.5合', answer)
        state = staff_faq.status(self.db)['latest']
        self.assertEqual(state['openai']['outcome'], 'http_error')
        self.assertEqual(state['answer_source']['outcome'], 'fallback')

    def test_unverified_or_unreadable_catalog_does_not_claim_approved_brands(self):
        source = Path(self.tmp.name) / 'catalog.json'
        for content in ('{}', '[]', 'invalid', '{"verified":false,"items":[]}',
                        '{"verified":true,"items":[{"name_ja":"invented","included":"true"}]}'):
            source.write_text(content)
            with patch.object(staff_faq, 'DRINK_CATALOG', source):
                answer = staff_faq.model_answer('無料ドリンク', self.db)
                self.assertIn('読み込めません', answer)
                self.assertNotIn('invented', answer)

    def test_groups_and_unverified_people_do_not_receive_faq(self):
        with patch.object(staff_faq, 'reply') as reply:
            staff_faq.respond([self.event('無料ドリンク', 'group')], {'staff'}, '', self.db)
            staff_faq.respond([self.event('無料ドリンク')], set(), '', self.db)
            reply.assert_not_called()
            staff_faq.respond([self.event('無料ドリンク')], {'staff'}, '', self.db)
            reply.assert_called_once_with('無料ドリンク', 'fake-reply', self.db)

    def test_structured_commands_still_belong_to_existing_workflows(self):
        for text in ('予約', '予約追記 さいとう', '希望シフト', 'シフト入力',
                     '酒類在庫', '備品', 'メンション登録 なかがわさん'):
            self.assertEqual(staff_faq.question_events([self.event(text)]), [], text)

    def test_line_reply_payload_is_complete_and_diagnostics_store_no_text(self):
        response = MagicMock()
        response.__enter__.return_value.status = 200
        with patch.dict(os.environ, {'LINE_CHANNEL_ACCESS_TOKEN': 'fake-line-token'}), \
                patch.object(staff_faq.urllib.request, 'urlopen', return_value=response) as api:
            self.assertTrue(staff_faq.reply('無料ドリンク', 'fake-reply', self.db))
        payload = json.loads(api.call_args.args[0].data)
        self.assertEqual(payload['replyToken'], 'fake-reply')
        self.assert_catalog(payload['messages'][0]['text'])
        with staff_faq.sqlite3.connect(self.db) as conn:
            dump = '\n'.join(conn.iterdump())
        for sensitive in ('fake-reply', 'fake-line-token', '無料ドリンク', '十四代'):
            self.assertNotIn(sensitive, dump)


if __name__ == '__main__':
    unittest.main()
