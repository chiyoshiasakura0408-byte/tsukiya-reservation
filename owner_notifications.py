"""Durable owner booking notices, independent of reservation/payment delivery.

Only the existing owner-new-booking kind is handled here. Customer messages and
other staff workflows retain their existing transport. No reservation is updated.
"""
import hashlib
import json
import logging
import os
import re
import smtplib
import time
import urllib.error
import urllib.request
from email.message import EmailMessage
from email.utils import parseaddr

LOG = logging.getLogger(__name__)
if not LOG.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    LOG.addHandler(handler)
LOG.setLevel(logging.INFO)
LOG.propagate = False
MAX_ATTEMPTS = 8
RETRY_WINDOW = 23 * 3600  # Conservative margin before LINE's 24-hour key expiry.
LEASE_SECONDS = 90
OWNER_ID = re.compile(r'U[0-9a-fA-F]{32}')


def capture_schema(c):
    """Capture a tiny event in the booking transaction; never call a network API."""
    import accounting_booking_notices
    accounting_booking_notices.schema(c)
    c.execute('''CREATE TABLE IF NOT EXISTS owner_booking_events(
        reservation_id INTEGER PRIMARY KEY, registered REAL NOT NULL)''')
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reservations'").fetchone():
        return
    c.execute('''CREATE TRIGGER IF NOT EXISTS owner_booking_registered
        AFTER INSERT ON reservations
        WHEN COALESCE(NEW.course_name,'') NOT LIKE '決済テスト%'
        BEGIN INSERT OR IGNORE INTO owner_booking_events VALUES(NEW.id,strftime('%s','now')); END''')


def schema(c):
    if not c.in_transaction:
        c.execute('BEGIN IMMEDIATE')
    capture_schema(c)
    cols = {r['name'] for r in c.execute('PRAGMA table_info(concierge_outbox)')}
    for name, typ in {
        'first_try': 'REAL', 'last_try': 'REAL', 'accepted_at': 'REAL',
        'request_id': 'TEXT', 'accepted_request_id': 'TEXT',
        'error_code': 'TEXT', 'lease_until': 'REAL',
        'uncertain': 'INTEGER NOT NULL DEFAULT 0',
        'retry_limit': 'INTEGER NOT NULL DEFAULT 8',
        'channel_fingerprint': 'TEXT',
    }.items():
        if name not in cols:
            c.execute(f'ALTER TABLE concierge_outbox ADD COLUMN {name} {typ}')
    c.execute('''CREATE TABLE IF NOT EXISTS owner_notice_attempts(
        outbox_id TEXT NOT NULL, attempt INTEGER NOT NULL, started REAL NOT NULL,
        finished REAL, http_status INTEGER, outcome TEXT NOT NULL,
        message TEXT, request_id TEXT, accepted_request_id TEXT,
        PRIMARY KEY(outbox_id,attempt))''')
    c.execute('''CREATE TABLE IF NOT EXISTS owner_notice_channel(
        id INTEGER PRIMARY KEY CHECK(id=1), blocked_until REAL NOT NULL,
        reason TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS owner_notice_fallback(
        outbox_id TEXT PRIMARY KEY, state TEXT NOT NULL, updated REAL NOT NULL,
        error TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS owner_notice_recovery_audit(
        id INTEGER PRIMARY KEY, outbox_id TEXT NOT NULL, created REAL NOT NULL,
        evidence TEXT NOT NULL, previous_state TEXT NOT NULL)''')
    # Pre-upgrade attempts have no request IDs/response bodies. Freeze them for
    # reconciliation instead of silently sending #29 with unverifiable history.
    c.execute("""UPDATE concierge_outbox SET state='held',error_code='legacy_history',
        error='旧送信履歴との照合が必要です',first_try=created,uncertain=1
        WHERE kind='owner-new-booking' AND state IN ('pending','failed')
        AND attempts>0 AND first_try IS NULL""")
    c.execute("CREATE INDEX IF NOT EXISTS owner_notice_due ON concierge_outbox(kind,state,next_try)")


def queue(c):
    import concierge
    import accounting_booking_notices
    accounting_booking_notices.queue(c)
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reservations'").fetchone():
        return
    if concierge.setting(c, 'owner_booking_notifications_enabled', '1') != '1':
        return
    # Preserve the already-authorized October 20 WEB backfill; future inserts
    # from PHONE, DIRECT, LINE, English, VIP, Square and imports use the trigger.
    c.execute("""INSERT OR IGNORE INTO owner_booking_events
        SELECT id,strftime('%s','now') FROM reservations WHERE source='WEB'
        AND julianday(created_at)>=julianday('2026-10-20T00:00:00+09:00')
        AND status IN ('PENDING','INVOICED','CONFIRMED','ERROR')
        AND amount>1 AND COALESCE(course_name,'') NOT LIKE '%テスト%'""")
    owner = concierge.setting(c, 'owner')
    rows = c.execute('''SELECT r.* FROM owner_booking_events e
        JOIN reservations r ON r.id=e.reservation_id
        LEFT JOIN owner_booking_notices n ON n.reservation_id=r.id
        WHERE n.reservation_id IS NULL AND (r.source!='WEB_CARD' OR r.status='CONFIRMED') ORDER BY e.registered,r.id LIMIT 50''').fetchall()
    for record in rows:
        r = dict(record)
        # A bank request or an owner-DM registration receipt already tells the
        # owner about this booking. Adopt its exact UUID/payload, never send a
        # second new-booking notice. Group acknowledgements are separate.
        existing = None
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='bank_notices'").fetchone():
            existing = c.execute('''SELECT o.* FROM bank_notices b JOIN concierge_outbox o ON o.id=b.outbox_id
                WHERE b.reservation_id=? AND b.stage='requested' AND o.channel='owner'
                AND o.user_id=? AND o.state!='cancelled' LIMIT 1''', (r['id'], owner)).fetchone()
        if not existing:
            existing = c.execute('''SELECT o.* FROM reservation_notice_links l JOIN concierge_outbox o ON o.id=l.outbox_id
                WHERE l.reservation_id=? AND o.channel='owner' AND o.user_id=? AND o.kind='line-booking'
                AND o.payload LIKE '%【予約登録成功】%' AND o.state!='cancelled' ORDER BY o.created LIMIT 1''',
                (r['id'], owner)).fetchone()
        if existing:
            c.execute("UPDATE concierge_outbox SET kind='owner-new-booking' WHERE id=?", (existing['id'],))
            c.execute('INSERT INTO owner_booking_notices VALUES (?,?)', (r['id'], existing['id']))
            if existing['attempts'] and existing['state'] != 'accepted' and existing['first_try'] is None:
                c.execute("""UPDATE concierge_outbox SET first_try=created,uncertain=1 WHERE id=?""", (existing['id'],))
                _hold(c, existing['id'], 'legacy_history', '旧送信履歴との照合が必要です')
            continue
        seat = {'COUNTER': 'カウンター', 'PRIVATE1': '個室1', 'PRIVATE2': '個室2', 'PRIVATE3': '個室3'}.get(r.get('seating_area'), '未設定')
        state = {'PENDING': '受付・未確定', 'INVOICED': '請求済み・未決済（未確定）',
                 'CONFIRMED': '予約確定', 'ERROR': '受付済み・決済処理要確認',
                 'CANCELLED': '受付後にキャンセル'}.get(r['status'], r['status'])
        body = (f"【新規予約受付】\n予約番号 #{r['id']}\n{r['guest_name']}様\n"
                f"来店：{r['visit_at'].replace('T', ' ')}\n人数：{r['party_size']}名 ／ {seat}\n"
                f"コース：{r.get('course_name') or '未設定'}\n金額：¥{r.get('amount', 0):,}\n"
                f"状態：{state}\n経路：{r['source']}\n受付日時：{r['created_at']}\n予約管理："
                + os.getenv('APP_BASE_URL', 'https://tsukiya-reservation.onrender.com').rstrip('/') + '/reservations')
        key = concierge.enqueue(c, owner, [concierge.text_message(body, False)],
                                channel='owner', kind='owner-new-booking', reservation_id=r['id'])
        c.execute('INSERT INTO owner_booking_notices VALUES (?,?)', (r['id'], key))


def _header(headers, key):
    value = str((headers or {}).get(key, ''))
    return value[:200] if re.fullmatch(r'[A-Za-z0-9_.:-]*', value) else ''


def _error_message(raw):
    # Keep LINE's diagnostic message, not entire payloads/credentials/guest data.
    try:
        value = json.loads(raw).get('message', '')
    except (ValueError, AttributeError, TypeError):
        return 'Unparseable LINE error response'
    known = ['You have reached your monthly limit.',
             'The API rate limit has been exceeded. Try again later.',
             'Too many requests', 'Failed to send messages',
             'Authentication failed', 'Invalid reply token',
             'Access to this API is not available for your account']
    for message in known:
        if str(value).startswith(message):
            return message
    return 'Unrecognized LINE error message (response sha256=' + hashlib.sha256(raw).hexdigest()[:16] + ')'


def _hold(c, key, code, message):
    c.execute("UPDATE concierge_outbox SET state='held',error_code=?,error=?,lease_until=NULL WHERE id=?",
              (code, message, key))


def deliver(c, record):
    """Claim across processes; persist intent before sending; persist API receipt.

    The immutable outbox UUID is the LINE retry key. An expired/ambiguous notice
    can only be released using the explicit, audited recovery operation below.
    """
    import concierge
    now = time.time()
    c.execute('BEGIN IMMEDIATE')
    try:
        row = dict(c.execute('SELECT * FROM concierge_outbox WHERE id=?', (record['id'],)).fetchone())
        if row['state'] != 'pending' or row['next_try'] > now:
            c.commit()
            return
        if row['attempts'] >= row['retry_limit']:
            _hold(c, row['id'], 'retry_limit', '再送上限に到達しました。履歴の確認が必要です')
            c.commit()
            return
        if row['first_try'] is not None and now - row['first_try'] >= RETRY_WINDOW:
            _hold(c, row['id'], 'retry_expired', '重複防止の再送期限を超えました。履歴の確認が必要です')
            c.commit()
            return
        if not OWNER_ID.fullmatch(row['user_id']):
            _hold(c, row['id'], 'recipient_missing', '朝倉個人LINEの連携が必要です')
            c.commit()
            return
        if row['user_id'] != concierge.setting(c, 'owner'):
            _hold(c, row['id'], 'recipient_changed', '登録時と現在のLINE送信先が異なります')
            c.commit()
            return
        token = os.getenv('LINE_CHANNEL_ACCESS_TOKEN', '').strip()
        if not token:
            _hold(c, row['id'], 'token_missing', '業務LINEの接続設定が必要です')
            c.commit()
            return
        fingerprint = hashlib.sha256(token.encode()).hexdigest()
        if row['channel_fingerprint'] and row['channel_fingerprint'] != fingerprint:
            _hold(c, row['id'], 'channel_changed', '送信開始後にLINE接続情報が変わりました。チャネルの照合が必要です')
            c.commit()
            return
        block = c.execute('SELECT * FROM owner_notice_channel WHERE id=1').fetchone()
        if block and block['blocked_until'] > now:
            if block['reason'] == 'monthly_quota':
                _hold(c, row['id'], 'monthly_quota', 'LINE月間上限。配信枠確認後の再開が必要です')
            else:
                c.execute('UPDATE concierge_outbox SET next_try=? WHERE id=?', (block['blocked_until'], row['id']))
            c.commit()
            return
        attempt = row['attempts'] + 1
        c.execute("""UPDATE concierge_outbox SET state='sending',attempts=?,
            first_try=COALESCE(first_try,?),last_try=?,lease_until=?,
            channel_fingerprint=COALESCE(channel_fingerprint,?) WHERE id=?""",
                  (attempt, now, now, now + LEASE_SECONDS, fingerprint, row['id']))
        c.execute("INSERT INTO owner_notice_attempts(outbox_id,attempt,started,outcome) VALUES(?,?,?,'unknown')",
                  (row['id'], attempt, now))
        c.commit()
    except Exception:
        c.rollback()
        raise
    status, request_id, accepted_id, message = None, '', '', ''
    response_body = b'{}'
    outcome, state, uncertain = 'connection', 'pending', True
    delay = min(3600, 30 * 2 ** (attempt - 1))
    try:
        body = json.dumps({'to': row['user_id'], 'messages': json.loads(row['payload']),
                           'notificationDisabled': False}).encode()
        req = urllib.request.Request('https://api.line.me/v2/bot/message/push', data=body,
              headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
                       'X-Line-Retry-Key': row['id']})
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                status = response.status
                request_id = _header(response.headers, 'x-line-request-id')
                response_body = response.read(65536)
            if status == 200:
                outcome, state, uncertain = 'accepted', 'accepted', False
            else:
                outcome, state = 'unexpected_response', 'held'
        except urllib.error.HTTPError as exc:
            status = exc.code
            request_id = _header(exc.headers, 'x-line-request-id')
            accepted_id = _header(exc.headers, 'x-line-accepted-request-id')
            response_body = exc.read(65536) or b'{}'
            message = _error_message(response_body)
            uncertain = status >= 500
            if status == 409 and accepted_id:
                outcome, state, uncertain = 'accepted', 'accepted', False
            elif status == 429:
                if message == 'You have reached your monthly limit.':
                    outcome, state = 'monthly_quota', 'held'
                elif message == 'The API rate limit has been exceeded. Try again later.':
                    outcome = 'rate_limit'
                    retry_after = (exc.headers or {}).get('Retry-After', '')
                    if str(retry_after).isdigit():
                        delay = max(delay, min(int(retry_after), RETRY_WINDOW))
                else:
                    outcome, state = 'unknown_429', 'held'
            elif status >= 500:
                outcome = 'server_error'
            else:
                outcome, state = 'http_' + str(status), 'failed'
    except (OSError, TimeoutError):
        message = 'LINE connection failed; acceptance unknown'
    except (ValueError, TypeError):
        outcome, state, uncertain, message = 'invalid_payload', 'failed', False, 'Invalid stored notification payload'
    if state == 'pending' and attempt >= row['retry_limit']:
        state = 'held'
    finished = time.time()
    with c:
        c.execute('''UPDATE owner_notice_attempts SET finished=?,http_status=?,outcome=?,
            message=?,request_id=?,accepted_request_id=? WHERE outbox_id=? AND attempt=?''',
            (finished, status, outcome, message, request_id, accepted_id, row['id'], attempt))
        c.execute('''UPDATE concierge_outbox SET state=?,error=?,error_code=?,next_try=?,
            lease_until=NULL,request_id=?,accepted_request_id=?,accepted_at=?,
            uncertain=CASE WHEN ?='accepted' THEN 0 ELSE MAX(uncertain,?) END WHERE id=?''',
            (state, None if state == 'accepted' else message or outcome,
             None if state == 'accepted' else outcome, finished + delay,
             request_id, accepted_id, finished if state == 'accepted' else None,
             state, int(uncertain), row['id']))
        if outcome in ('monthly_quota', 'rate_limit'):
            c.execute('INSERT OR REPLACE INTO owner_notice_channel VALUES(1,?,?)',
                      (1e20 if outcome == 'monthly_quota' else finished + delay, outcome))
    # Do not roll back acceptance or re-send if optional message-link bookkeeping fails.
    if state == 'accepted':
        with c:
            concierge.remember_reservation_messages(c, row, response_body)
    LOG.info('owner_booking_notice outbox=%s attempt=%s http=%s outcome=%s request_id=%s',
             row['id'], attempt, status, outcome, request_id or accepted_id)


def recover_leases(c):
    now = time.time()
    c.execute("""UPDATE concierge_outbox SET state='pending',uncertain=1,lease_until=NULL,
        error_code='interrupted',error='送信処理中断・API受付結果要確認',next_try=?
        WHERE kind='owner-new-booking' AND state='sending' AND lease_until<=?""", (now, now))
    # Never-attempted notices may pick up newly configured credentials safely.
    import concierge
    owner = concierge.setting(c, 'owner')
    if OWNER_ID.fullmatch(owner) and os.getenv('LINE_CHANNEL_ACCESS_TOKEN', '').strip():
        c.execute("""UPDATE concierge_outbox SET state='pending',user_id=?,next_try=?,error=NULL,error_code=NULL
            WHERE kind='owner-new-booking' AND state='held' AND attempts=0 AND first_try IS NULL
            AND error_code IN ('recipient_missing','token_missing')""", (owner, now))


def fallback(c):
    """One email alert per failed notice; ambiguous SMTP outcomes are not retried."""
    rows = c.execute("""SELECT o.id,n.reservation_id FROM concierge_outbox o
        JOIN owner_booking_notices n ON n.outbox_id=o.id
        LEFT JOIN owner_notice_fallback f ON f.outbox_id=o.id
        WHERE o.kind='owner-new-booking' AND o.state IN ('pending','failed','held')
        AND o.error_code IS NOT NULL AND (f.outbox_id IS NULL OR f.state='blocked')
        ORDER BY o.created LIMIT 10""").fetchall()
    recipient = os.getenv('OWNER_NOTIFICATION_EMAIL', '').strip()
    sender = parseaddr(os.getenv('MAIL_FROM', os.getenv('SMTP_USER', '')))[1]
    configured = (re.fullmatch(r'[^\s<>@]+@[^\s<>@]+', recipient) and sender and
                  all(os.getenv(k) for k in ('SMTP_HOST', 'SMTP_USER', 'SMTP_PASS')))
    for row in rows:
        c.execute('BEGIN IMMEDIATE')
        prior = c.execute('SELECT state FROM owner_notice_fallback WHERE outbox_id=?', (row['id'],)).fetchone()
        if prior and prior['state'] != 'blocked':
            c.commit()
            continue
        c.execute('INSERT OR REPLACE INTO owner_notice_fallback VALUES(?,?,?,?)',
                  (row['id'], 'sending' if configured else 'blocked', time.time(),
                   None if configured else '代替通知用メールアドレスまたはSMTP未設定'))
        c.commit()
        if not configured:
            continue
        state, error = 'unknown', 'SMTP受付結果要確認・自動再送停止'
        try:
            msg = EmailMessage()
            msg['From'], msg['To'] = sender, recipient
            msg['Subject'] = f"【つきや・要確認】予約 #{row['reservation_id']} のLINE通知"
            msg['Message-ID'] = f"<owner-notice-{row['id']}@tsukiya.local>"
            msg.set_content(f"予約 #{row['reservation_id']} のLINE通知はAPI受付を確認できていません。\n"
                            '予約情報は予約管理画面で確認してください。\n'
                            + os.getenv('APP_BASE_URL', 'https://tsukiya-reservation.onrender.com').rstrip('/')
                            + '/reservations\nこのメールはLINE通知の成功を意味しません。')
            port = int(os.getenv('SMTP_PORT', '587'))
            cls = smtplib.SMTP_SSL if port == 465 else smtplib.SMTP
            with cls(os.environ['SMTP_HOST'], port, timeout=10) as smtp:
                if port != 465:
                    smtp.starttls()
                smtp.login(os.environ['SMTP_USER'], os.environ['SMTP_PASS'])
                refused = smtp.send_message(msg)
            state, error = ('failed', 'SMTP宛先拒否') if refused else ('accepted', None)
        except (OSError, smtplib.SMTPException, ValueError):
            pass
        with c:
            c.execute('UPDATE owner_notice_fallback SET state=?,updated=?,error=? WHERE outbox_id=?',
                      (state, time.time(), error, row['id']))


def status(c, reservation_id=None):
    result = {'notices': [], 'max_attempts': MAX_ATTEMPTS, 'summary': {},
              'fallback_configured': bool(os.getenv('OWNER_NOTIFICATION_EMAIL') and all(os.getenv(k) for k in ('SMTP_HOST','SMTP_USER','SMTP_PASS')))}
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reservations'").fetchone():
        return result
    where, args = ('WHERE e.reservation_id=?', [reservation_id]) if reservation_id is not None else ('', [])
    rows = c.execute('''SELECT e.reservation_id,r.guest_name,r.created_at,r.source,
        o.id AS outbox_id,COALESCE(o.state,'queued') AS state,o.attempts,o.first_try,o.last_try,
        o.next_try,o.accepted_at,o.error,o.error_code,o.request_id,o.accepted_request_id,
        o.uncertain,f.state AS fallback_state,f.error AS fallback_error
        FROM owner_booking_events e JOIN reservations r ON r.id=e.reservation_id
        LEFT JOIN owner_booking_notices n ON n.reservation_id=e.reservation_id
        LEFT JOIN concierge_outbox o ON o.id=n.outbox_id
        LEFT JOIN owner_notice_fallback f ON f.outbox_id=o.id ''' + where +
        " ORDER BY CASE WHEN o.state IN ('held','failed') THEN 0 ELSE 1 END,e.reservation_id DESC LIMIT 100", args).fetchall()
    result['notices'] = [dict(r) for r in rows]
    result['summary'] = {r['state']: r['n'] for r in c.execute("""SELECT state,count(*) AS n
        FROM concierge_outbox WHERE kind='owner-new-booking' GROUP BY state""")}
    if reservation_id is not None:
        result['history'] = [dict(r) for r in c.execute('''SELECT a.* FROM owner_notice_attempts a
            JOIN owner_booking_notices n ON n.outbox_id=a.outbox_id
            WHERE n.reservation_id=? ORDER BY a.attempt''', (reservation_id,))]
    return result


def resume(c, reservation_id, evidence, verified_not_accepted=False):
    """Operator-only reconciliation. Caller must hold a transaction/DB write lock.

    No new retry key, payload, destination or reservation mutation is permitted.
    An expired/legacy key requires documented nonacceptance, never guesswork.
    """
    if not isinstance(evidence, str) or not 10 <= len(evidence.strip()) <= 1000:
        raise ValueError('送信履歴を照合した根拠を10〜1000文字で記録してください')
    row = c.execute('''SELECT o.* FROM concierge_outbox o JOIN owner_booking_notices n
        ON n.outbox_id=o.id WHERE n.reservation_id=?''', (reservation_id,)).fetchone()
    if not row or row['state'] not in ('held', 'failed'):
        raise ValueError('再開できる保留・失敗通知ではありません。受付済み通知は再送できません')
    if c.execute("SELECT 1 FROM owner_notice_attempts WHERE outbox_id=? AND outcome='accepted'", (row['id'],)).fetchone():
        raise ValueError('過去にAPI受付済みです。再送できません')
    if c.execute('SELECT 1 FROM reservation_message_links WHERE outbox_id=?', (row['id'],)).fetchone():
        raise ValueError('LINEメッセージIDが存在します。再送できません')
    if row['first_try'] is not None and (row['error_code'] == 'legacy_history' or time.time() - row['first_try'] >= RETRY_WINDOW):
        if not verified_not_accepted:
            raise ValueError('受付結果不明です。未受付を確認するまで再送できません')
    # Quota blocks are cleared separately only after a live read-only quota check.
    c.execute('INSERT INTO owner_notice_recovery_audit(outbox_id,created,evidence,previous_state) VALUES(?,?,?,?)',
              (row['id'], time.time(), evidence.strip(), row['state']))
    # Keep cumulative history. Explicit recovery allows at most one extra try.
    reset_window = verified_not_accepted and row['first_try'] is not None and time.time() - row['first_try'] >= RETRY_WINDOW
    c.execute("""UPDATE concierge_outbox SET state='pending',next_try=?,error=NULL,error_code=NULL,
        first_try=CASE WHEN ? THEN ? ELSE first_try END,
        retry_limit=MAX(retry_limit,attempts+1),
        uncertain=CASE WHEN ? THEN 0 ELSE uncertain END WHERE id=?""",
              (time.time(), int(reset_window), time.time(), int(verified_not_accepted), row['id']))


def quota():
    """Read-only diagnostics on exactly the channel used for owner notifications."""
    token = os.getenv('LINE_CHANNEL_ACCESS_TOKEN', '').strip()
    if not token:
        return {'configured': False, 'available': False}
    result = {'configured': True, 'available': False}
    try:
        for key, path in [('limit', 'message/quota'), ('consumption', 'message/quota/consumption')]:
            req = urllib.request.Request('https://api.line.me/v2/bot/' + path,
                                         headers={'Authorization': 'Bearer ' + token})
            with urllib.request.urlopen(req, timeout=8) as response:
                result[key] = json.loads(response.read(65536))
        limit, usage = result['limit'], result['consumption'].get('totalUsage')
        result['available'] = (limit.get('type') == 'none' or
            (limit.get('type') == 'limited' and isinstance(limit.get('value'), int)
             and isinstance(usage, int) and usage < limit['value']))
    except urllib.error.HTTPError as exc:
        result['http_status'] = exc.code
    except (OSError, ValueError):
        result['error'] = 'LINE quota check failed'
    return result


