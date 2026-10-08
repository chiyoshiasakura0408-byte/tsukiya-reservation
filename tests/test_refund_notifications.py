import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import concierge
import refunds
import run


class RefundNotifications(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = patch.object(run, 'DB', Path(self.tmp.name) / 'db')
        self.db.start()
        c = run.con()
        c.close()
        self.job = {'reservation_id': 9, 'amount': 1, 'status': 'AWAITING_APPROVAL'}
        self.reservation = {'guest_name': 'テスト', 'visit_at': '2026-11-11T18:00'}

    def tearDown(self):
        self.db.stop()
        self.tmp.cleanup()

    def owner(self, value):
        c = concierge.connect(run.DB)
        with c:
            concierge.put(c, 'owner', value)
        c.close()

    def rows(self):
        c = concierge.connect(run.DB)
        rows = [dict(r) for r in c.execute('SELECT * FROM concierge_outbox')]
        c.close()
        return rows

    def test_action_count_excludes_processing_completed_and_zero(self):
        statuses = ['AWAITING_APPROVAL', 'MANUAL', 'FAILED', 'REJECTED', 'QUEUED', 'SUBMITTING', 'PENDING', 'COMPLETED', 'NONE']
        rows = [dict(self.job, status=s) for s in statuses]
        rows.append(dict(self.job, amount=0))
        self.assertEqual(refunds.action_required_count(rows), 4)

    def test_only_verified_personal_owner_and_dedup_each_stage(self):
        self.owner('U' + 'a' * 32)
        refunds.notify_owner(run, self.job, self.reservation)
        refunds.notify_owner(run, self.job, self.reservation)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['user_id'], 'U' + 'a' * 32)
        self.assertEqual(rows[0]['channel'], 'owner')
        message = json.loads(rows[0]['payload'])[0]['text']
        self.assertIn('返金対象額：1円', message)
        self.assertIn('/#refundPanel', message)
        refunds.notify_owner(run, dict(self.job, status='MANUAL'), self.reservation)
        self.assertEqual(len(self.rows()), 2)
        refunds.notify_owner(run, dict(self.job, status='COMPLETED'), self.reservation)
        self.assertEqual(len(self.rows()), 2)

    def test_missing_owner_or_group_never_receives_notification(self):
        for owner in ['', 'C' + 'a' * 32]:
            self.owner(owner)
            refunds.notify_owner(run, self.job, self.reservation)
            self.assertEqual(self.rows(), [])
        self.owner('U' + 'b' * 32)
        refunds.notify_owner(run, self.job, self.reservation)
        self.assertEqual(len(self.rows()), 1)

    def test_failed_queue_transaction_can_retry_without_loss(self):
        self.owner('U' + 'a' * 32)
        with patch.object(concierge, 'enqueue', side_effect=RuntimeError('test failure')):
            with self.assertRaises(RuntimeError):
                refunds.notify_owner(run, self.job, self.reservation)
        refunds.notify_owner(run, self.job, self.reservation)
        self.assertEqual(len(self.rows()), 1)
