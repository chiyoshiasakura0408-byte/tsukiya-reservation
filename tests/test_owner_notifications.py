import io
import json
import os
import tempfile
import time
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import concierge
import owner_notifications as notices
import run


OWNER = 'U' + 'a' * 32


class OwnerNotifications(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = self.temp.name + '/bookings.sqlite'
        env = patch.dict(os.environ, {'LINE_CHANNEL_ACCESS_TOKEN': 'test-never-live',
                                     'OWNER_NOTIFICATION_EMAIL': ''})
        env.start(); self.addCleanup(env.stop)
        network = patch('urllib.request.urlopen', side_effect=AssertionError('Unexpected network call'))
        self.network = network.start(); self.addCleanup(network.stop)
        with patch.object(run, 'DB', self.db):
            c = run.con(); c.close()
        c = concierge.connect(self.db)
        with c:
            concierge.put(c, 'owner', OWNER)
        c.close()

    def add(self, source='WEB', course='松葉蟹', commit=True):
        c = concierge.connect(self.db)
        with c:
            cur = c.execute('''INSERT INTO reservations(source,guest_name,visit_at,party_size,
                course_name,amount,seating_area,status,created_at,updated_at)
                VALUES(?, '通知試験架空名', '2026-12-03T18:00', 2, ?, 120000,
                'COUNTER','PENDING','2026-10-20T20:00:00+09:00','2026-10-20T20:00:00+09:00')''', (source, course))
            rid = cur.lastrowid
            if not commit:
                c.rollback()
        c.close()
        return rid

    def queue(self):
        c = concierge.connect(self.db)
        with c:
            concierge.queue_new_web_reservations(c)
        c.close()

    def rows(self, table):
        c = concierge.connect(self.db)
        try:
            return [dict(r) for r in c.execute('SELECT * FROM ' + table)]
        finally:
            c.close()

    def update(self, sql, args=()):
        c = concierge.connect(self.db)
        with c:
            c.execute(sql, args)
        c.close()

    def send(self):
        c = concierge.connect(self.db)
        try:
            row = c.execute("SELECT * FROM concierge_outbox WHERE kind='owner-new-booking' ORDER BY created LIMIT 1").fetchone()
            notices.deliver(c, row)
        finally:
            c.close()

    def response(self, status=200):
        response = MagicMock()
        response.status = status
        response.headers = {'x-line-request-id': 'request-123'}
        response.read.return_value = b'{"sentMessages":[{"id":"123456"}]}'
        response.__enter__.return_value = response
        self.network.side_effect = None
        self.network.return_value = response

    def error(self, status, message='', headers=None):
        self.network.side_effect = urllib.error.HTTPError('https://api.line.me', status, 'test',
            headers or {'x-line-request-id': 'error-123'}, io.BytesIO(json.dumps({'message': message}).encode()))

    def prepare(self):
        rid = self.add(); self.queue()
        return rid

    def test_all_new_sources_and_updates_are_once(self):
        for source in ['WEB', 'PHONE', 'DIRECT', 'CONCIERGE_LINE', 'SQUARE', 'TABLECHECK']:
            self.add(source)
        self.queue(); self.queue()
        self.update("UPDATE reservations SET status='CONFIRMED'")
        self.queue()
        self.assertEqual(len(self.rows('concierge_outbox')), 6)
        self.assertEqual(len(self.rows('owner_booking_events')), 6)
        self.assertTrue(all(r['user_id'] == OWNER for r in self.rows('concierge_outbox')))

    def test_transaction_rollback_no_event_or_notice(self):
        self.add(commit=False); self.queue()
        self.assertEqual(self.rows('owner_booking_events'), [])
        self.assertEqual(self.rows('concierge_outbox'), [])

    def test_payment_test_is_not_real_notification(self):
        self.add(course='決済テスト（お料理のご予約ではありません）'); self.queue()
        self.assertEqual(self.rows('concierge_outbox'), [])

    def test_missing_token_does_not_lose_booking_or_event(self):
        before = self.add()
        with patch.dict(os.environ, {'LINE_CHANNEL_ACCESS_TOKEN': ''}):
            self.queue(); self.send()
        row = self.rows('concierge_outbox')[0]
        self.assertEqual((row['state'], row['attempts'], row['error_code']), ('held', 0, 'token_missing'))
        self.assertEqual(self.rows('reservations')[0]['id'], before)
        self.assertEqual(self.rows('reservations')[0]['status'], 'PENDING')
        self.network.assert_not_called()
        c = concierge.connect(self.db)
        with c: notices.recover_leases(c)
        c.close()
        self.assertEqual(self.rows('concierge_outbox')[0]['state'], 'pending')

    def test_200_records_receipt_and_never_sends_again(self):
        self.prepare(); self.response(); self.send(); self.send()
        row = self.rows('concierge_outbox')[0]
        self.assertEqual(row['state'], 'accepted')
        self.assertEqual(row['request_id'], 'request-123')
        self.assertTrue(row['accepted_at'])
        self.assertEqual(self.rows('reservation_message_links')[0]['message_id'], '123456')
        self.assertEqual(self.network.call_count, 1)

    def test_timeout_then_409_same_payload_and_key(self):
        self.prepare()
        self.network.side_effect = TimeoutError()
        self.send()
        first = self.network.call_args.args[0]
        self.update('UPDATE concierge_outbox SET next_try=0')
        self.error(409, headers={'x-line-accepted-request-id': 'original-123'})
        self.send()
        second = self.network.call_args.args[0]
        self.assertEqual(first.data, second.data)
        self.assertEqual(first.get_header('X-line-retry-key'), second.get_header('X-line-retry-key'))
        self.assertEqual(self.rows('concierge_outbox')[0]['state'], 'accepted')
        self.assertEqual(self.rows('concierge_outbox')[0]['accepted_request_id'], 'original-123')

    def test_monthly_quota_holds_channel_without_retries(self):
        self.prepare(); self.error(429, 'You have reached your monthly limit.'); self.send()
        self.send()
        self.assertEqual(self.network.call_count, 1)
        row = self.rows('concierge_outbox')[0]
        self.assertEqual((row['state'], row['error_code']), ('held', 'monthly_quota'))
        self.assertEqual(self.rows('owner_notice_attempts')[0]['message'], 'You have reached your monthly limit.')
        self.add('DIRECT'); self.queue()
        c = concierge.connect(self.db)
        try:
            notices.deliver(c, c.execute("SELECT * FROM concierge_outbox WHERE state='pending'").fetchone())
        finally: c.close()
        self.assertEqual(self.network.call_count, 1)
        self.assertTrue(all(r['state'] == 'held' for r in self.rows('concierge_outbox')))

    def test_rate_limit_schedules_bounded_backoff(self):
        self.prepare()
        self.error(429, 'The API rate limit has been exceeded. Try again later.', {'Retry-After':'120'})
        self.send()
        row = self.rows('concierge_outbox')[0]
        self.assertEqual((row['state'], row['error_code']), ('pending', 'rate_limit'))
        self.assertGreater(row['next_try'], time.time() + 110)

    def test_unknown_429_does_not_guess_or_retry(self):
        self.prepare(); self.error(429, 'unexpected guest content'); self.send()
        row = self.rows('concierge_outbox')[0]
        self.assertEqual((row['state'], row['error_code']), ('held', 'unknown_429'))
        self.assertNotIn('guest content', row['error'])

    def test_eight_network_attempts_then_hold(self):
        self.prepare(); self.network.side_effect = TimeoutError()
        for _ in range(10):
            self.update('UPDATE concierge_outbox SET next_try=0')
            self.send()
        row = self.rows('concierge_outbox')[0]
        self.assertEqual((row['state'], row['attempts']), ('held', 8))
        self.assertEqual(self.network.call_count, 8)

    def test_expired_ambiguous_request_is_not_resent(self):
        self.prepare()
        self.update("UPDATE concierge_outbox SET first_try=?,attempts=1,uncertain=1", (time.time()-86400,))
        self.send(); self.network.assert_not_called()
        self.assertEqual(self.rows('concierge_outbox')[0]['state'], 'held')

    def test_legacy_retry_freezes_without_losing_id(self):
        self.prepare()
        self.update("UPDATE concierge_outbox SET attempts=3,error='LINE HTTP 429'")
        row = self.rows('concierge_outbox')[0]  # schema migration
        self.assertEqual((row['state'], row['error_code']), ('held', 'legacy_history'))
        self.send(); self.network.assert_not_called()

    def test_sending_lease_recovers_with_same_key(self):
        self.prepare()
        before = self.rows('concierge_outbox')[0]['id']
        self.update("UPDATE concierge_outbox SET state='sending',lease_until=?,first_try=?,attempts=1",
                    (time.time()-1, time.time()-10))
        c = concierge.connect(self.db)
        with c: notices.recover_leases(c)
        c.close()
        self.response(); self.send()
        self.assertEqual(self.network.call_args.args[0].get_header('X-line-retry-key'), before)

    def test_two_workers_only_one_push(self):
        self.prepare(); self.response()
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: self.send(), range(2)))
        self.assertEqual(self.network.call_count, 1)

    def test_fallback_is_explicit_and_at_most_once(self):
        self.prepare(); self.error(401, 'Authentication failed'); self.send()
        c = concierge.connect(self.db)
        try:
            notices.fallback(c)
            self.assertEqual(c.execute('SELECT state FROM owner_notice_fallback').fetchone()[0], 'blocked')
            with patch.dict(os.environ, {'OWNER_NOTIFICATION_EMAIL':'owner@example.invalid',
                    'SMTP_HOST':'smtp.example.invalid','SMTP_USER':'test','SMTP_PASS':'test',
                    'MAIL_FROM':'notice@example.invalid','SMTP_PORT':'587'}), patch('smtplib.SMTP') as smtp:
                smtp.return_value.__enter__.return_value.send_message.return_value = {}
                notices.fallback(c); notices.fallback(c)
                self.assertEqual(smtp.call_count, 1)
            self.assertEqual(c.execute('SELECT state FROM owner_notice_fallback').fetchone()[0], 'accepted')
            self.assertEqual(c.execute('SELECT state FROM concierge_outbox').fetchone()[0], 'failed')
        finally: c.close()

    def test_accepted_or_unknown_cannot_blindly_resume(self):
        rid = self.prepare(); self.response(); self.send()
        c = concierge.connect(self.db)
        try:
            with self.assertRaises(ValueError):
                with c: notices.resume(c, rid, 'operator checked all history', True)
            with c:
                c.execute("UPDATE concierge_outbox SET state='held',error_code='legacy_history',first_try=?", (time.time()-86400,))
            with self.assertRaises(ValueError):
                with c: notices.resume(c, rid, 'operator checked all history', True)
        finally: c.close()

    def test_queue_is_independent_of_vip_disable(self):
        self.prepare()
        concierge.configure(self.db, {'action':'disable'})
        self.assertEqual(self.rows('concierge_outbox')[0]['state'], 'pending')

    def test_legacy_backfill_jst_boundary_and_no_manual_history(self):
        ids = [self.add(), self.add(), self.add('DIRECT')]
        self.update('DELETE FROM owner_booking_events')
        self.update('UPDATE reservations SET created_at=? WHERE id=?', ('2026-10-19T14:59:59+00:00', ids[0]))
        self.update('UPDATE reservations SET created_at=? WHERE id=?', ('2026-10-19T15:00:00+00:00', ids[1]))
        self.queue()
        self.assertEqual([r['reservation_id'] for r in self.rows('owner_booking_notices')], [ids[1]])

    def test_group_is_never_owner_destination(self):
        self.update("UPDATE concierge_settings SET value=? WHERE key='owner'", ('C'+'a'*32,))
        self.prepare(); self.send()
        self.network.assert_not_called()
        self.assertEqual(self.rows('concierge_outbox')[0]['error_code'], 'recipient_missing')

    def test_owner_registration_ack_is_adopted_not_duplicated(self):
        rid = self.add('DIRECT')
        c = concierge.connect(self.db)
        with c:
            key = concierge.enqueue(c, OWNER, [{'type':'text','text':'【予約登録成功】\n架空予約'}],
                channel='owner', kind='line-booking', reservation_id=rid)
        c.close()
        self.queue()
        self.assertEqual(len(self.rows('concierge_outbox')), 1)
        self.assertEqual(self.rows('owner_booking_notices')[0]['outbox_id'], key)

    def test_expired_unknown_resume_requires_evidence_and_preserves_history(self):
        rid = self.prepare()
        self.update("UPDATE concierge_outbox SET state='held',error_code='legacy_history',first_try=?,attempts=9,uncertain=1", (time.time()-86400,))
        c = concierge.connect(self.db)
        try:
            with self.assertRaises(ValueError):
                with c: notices.resume(c, rid, 'No evidence of nonacceptance')
            with c: notices.resume(c, rid, 'External log: all nine requests rejected; operator verified', True)
        finally: c.close()
        self.response(); self.send(); self.send()
        self.assertEqual(self.network.call_count, 1)
        self.assertEqual(self.rows('concierge_outbox')[0]['attempts'], 10)
        self.assertEqual(len(self.rows('owner_notice_recovery_audit')), 1)

    def test_smtp_ambiguous_result_is_not_automatically_retried(self):
        self.prepare(); self.error(401, 'Authentication failed'); self.send()
        c = concierge.connect(self.db)
        try:
            with patch.dict(os.environ, {'OWNER_NOTIFICATION_EMAIL':'owner@example.invalid',
                    'SMTP_HOST':'smtp.example.invalid','SMTP_USER':'test','SMTP_PASS':'test',
                    'MAIL_FROM':'notice@example.invalid'}), patch('smtplib.SMTP', side_effect=TimeoutError()) as smtp:
                notices.fallback(c); notices.fallback(c)
                self.assertEqual(smtp.call_count, 1)
            self.assertEqual(c.execute('SELECT state FROM owner_notice_fallback').fetchone()[0], 'unknown')
        finally: c.close()

    def test_live_quota_is_read_only_on_owner_channel(self):
        values = [{'type':'limited','value':200}, {'totalUsage':200}]
        def get(request, timeout):
            self.assertEqual(request.get_method(), 'GET')
            self.assertEqual(request.get_header('Authorization'), 'Bearer test-never-live')
            response = MagicMock()
            response.read.return_value = json.dumps(values.pop(0)).encode()
            response.__enter__.return_value = response
            return response
        self.network.side_effect = get
        self.assertFalse(notices.quota()['available'])

    def test_authenticated_http_status_has_no_payload_or_recipient(self):
        import http.client
        import threading
        from http.server import ThreadingHTTPServer
        rid = self.prepare()
        with patch.object(run, 'DB', self.db), patch.object(run, 'ADMIN_TOKEN', 'test-admin'):
            server = ThreadingHTTPServer(('127.0.0.1', 0), run.Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                conn = http.client.HTTPConnection('127.0.0.1', server.server_port)
                conn.request('GET', '/api/reservation-notifications')
                response = conn.getresponse(); response.read()
                self.assertEqual(response.status, 401)
                conn.request('GET', '/api/reservation-notifications?reservation_id='+str(rid), headers={'x-admin-token':'test-admin'})
                response = conn.getresponse(); body = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertNotIn(OWNER, json.dumps(body))
                self.assertNotIn('payload', json.dumps(body))
                self.assertEqual(body['notices'][0]['state'], 'pending')
                conn.close()
            finally:
                server.shutdown(); server.server_close()

    def test_channel_changes_hold_before_any_retry(self):
        self.prepare(); self.network.side_effect = TimeoutError(); self.send()
        self.update('UPDATE concierge_outbox SET next_try=0')
        with patch.dict(os.environ, {'LINE_CHANNEL_ACCESS_TOKEN':'different-channel-token'}):
            self.send()
        self.assertEqual(self.network.call_count, 1)
        self.assertEqual(self.rows('concierge_outbox')[0]['error_code'], 'channel_changed')

    def test_disabled_customer_backlog_does_not_starve_owner(self):
        c = concierge.connect(self.db)
        with c:
            for _ in range(12):
                concierge.enqueue(c, 'customer', [{'type':'text','text':'old message'}])
        c.close()
        self.prepare(); self.response()
        concierge.deliver(self.db)
        self.assertEqual(self.network.call_count, 1)
        self.assertEqual(json.loads(self.network.call_args.args[0].data)['to'], OWNER)


if __name__ == '__main__':
    unittest.main()
