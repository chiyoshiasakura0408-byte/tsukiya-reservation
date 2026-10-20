import unittest
from unittest.mock import patch
import concierge_ai

class DrinkKnowledgeTests(unittest.TestCase):
    def test_approved_catalog_and_limits(self):
        data=concierge_ai.knowledge()['drinks']
        self.assertTrue(data['verified'])
        self.assertEqual(len(data['items']),26)
        self.assertEqual([r['name_ja'] for r in data['items'] if not r['included']],['山崎','白州','クエルボ1800','ヘネシー'])
        special=next(r for r in data['items'] if r['id']==17)
        self.assertTrue(special['included'])
        self.assertIn('2名で一合',special['limit_ja'])
        self.assertIn('提供できない',special['availability_ja'])

    def test_both_concierges_receive_knowledge_for_questions(self):
        with patch.object(concierge_ai,'request',return_value={'answer':'Up to one go shared by two guests; subject to availability.','needs_owner':False,'reason_ja':'','summary_ja':''}) as request:
            result=concierge_ai.reply('Is Juyondai sake included?',[])
            self.assertFalse(result['needs_owner'])
            instructions=request.call_args.args[0]
            self.assertIn('Juyondai Honmaru',instructions)
            self.assertIn('one go shared by two guests',instructions)
            self.assertIn('without volunteering',instructions)
        with patch.object(concierge_ai,'request',return_value={'answer':'十四代本丸は2名で一合まで無料です。入荷状況によりご用意できない場合がございます。','needs_owner':False}) as request:
            self.assertFalse(concierge_ai.vip_reply('十四代は無料ですか')['needs_owner'])
            self.assertIn('クエルボ1800',request.call_args.args[0])
            self.assertIn('質問に応じて',request.call_args.args[0])
