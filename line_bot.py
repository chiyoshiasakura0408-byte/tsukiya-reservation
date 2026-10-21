"""LINE webhook connection layer. Does not send messages or process receipts yet."""
import base64
import hashlib
import hmac
import json
import os
import sqlite3
import line_receipts
import line_reservations
import concierge
import supplies
import staff_faq
import threading
from datetime import datetime, timezone

MAX_BODY = 1024 * 1024


def connect(db):
    c = sqlite3.connect(db, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute('''CREATE TABLE IF NOT EXISTS line_sources (
        source_id TEXT PRIMARY KEY, source_type TEXT NOT NULL,
        last_seen TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1)''')
    c.execute('''CREATE TABLE IF NOT EXISTS line_received_events (
        event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL,
        source_id TEXT, message_type TEXT, received_at TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS line_connection_state (
        key TEXT PRIMARY KEY, value TEXT NOT NULL)''')
    return c


def receive(db, raw, signature):
    secret = os.getenv('LINE_CHANNEL_SECRET', '').strip()
    if not secret:
        return 503, {'error': 'LINE_CHANNEL_SECRET is not configured'}
    if len(raw) > MAX_BODY:
        return 413, {'error': 'payload too large'}
    expected = base64.b64encode(hmac.new(secret.encode(), raw, hashlib.sha256).digest())
    if not signature or not hmac.compare_digest(expected, signature.encode('utf-8')):
        return 403, {'error': 'invalid signature'}
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict) or not isinstance(payload.get('events'), list):
            raise ValueError()
        events = payload['events']
        for event in events:
            if not isinstance(event, dict) or not isinstance(event.get('webhookEventId'), str) or not event['webhookEventId']:
                raise ValueError()
            if not isinstance(event.get('type'), str):
                raise ValueError()
            if not isinstance(event.get('source', {}), dict) or not isinstance(event.get('message', {}), dict):
                raise ValueError()
    except (ValueError, TypeError, UnicodeDecodeError):
        return 400, {'error': 'invalid payload'}
    now = datetime.now(timezone.utc).isoformat()
    # Initialize the inventory/outbox tables before the webhook transaction.
    inventory = supplies.connect(db)
    inventory.close()
    c = connect(db)
    received = 0
    new_events = []
    try:
        with c:
            # Keep deduplication metadata only; never store chat text, images or reply tokens.
            c.execute("DELETE FROM line_received_events WHERE received_at < datetime('now', '-30 days')")
            for event in events:
                source = event.get('source', {})
                kind = source.get('type')
                source_id = source.get({'group': 'groupId', 'room': 'roomId', 'user': 'userId'}.get(kind, ''))
                if not isinstance(source_id, str):
                    source_id = None
                message_type = event.get('message', {}).get('type')
                if not isinstance(message_type, str):
                    message_type = None
                row = c.execute('INSERT OR IGNORE INTO line_received_events VALUES (?,?,?,?,?)',
                                (event['webhookEventId'], event['type'], source_id, message_type, now))
                if not row.rowcount:
                    continue
                received += 1
                new_events.append(event)
                if source_id and kind in ('group', 'room', 'user'):
                    active = int(event['type'] not in ('leave', 'unfollow'))
                    c.execute('INSERT INTO line_sources VALUES (?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET last_seen=excluded.last_seen, active=excluded.active',
                              (source_id, kind, now, active))
            claimed = register_inventory_mention(c, events)
            # Question requests are exclusively handled by the internal reply API,
            # never interpreted as receipts, reservations, or inventory commands.
            staff_ids = {concierge.setting(c, 'owner')}
            staff_ids.update(concierge.setting(c, 'inventory_mention_' + key)
                             for key in ('nakagawa', 'yamamoto', 'imamura', 'saya'))
            staff_ids.discard(None)
            staff_ids.discard('')
            staff_questions = staff_faq.question_events(new_events, staff_ids)
            claimed.update(e['webhookEventId'] for e in new_events
                           if staff_faq.question_events([e], staff_ids))
            claimed |= line_reservations.ingest(c, raw, signature, payload)
            claimed |= supplies.ingest(c, events, os.getenv('APP_BASE_URL', '').strip().rstrip('/'))
            finance_payload = {**payload, "events": [e for e in events if e["webhookEventId"] not in claimed]}
            finance_raw = json.dumps(finance_payload, ensure_ascii=False).encode() if claimed else raw
            line_receipts.enqueue(c, finance_raw, signature, finance_payload)
            c.execute('INSERT OR REPLACE INTO line_connection_state VALUES (?,?)', ('last_verified_at', now))
    finally:
        c.close()
    import guest_service
    if staff_questions:
        threading.Thread(target=staff_faq.respond,
                         args=(new_events, staff_ids),
                         daemon=True).start()
    guest_service.receive_stock(db, events)
    concierge.pair_owner(db, events)
    import vip_access
    vip_access.receive_owner_invites(db, events, os.getenv("APP_BASE_URL", "").strip().rstrip("/"))
    return 200, {'ok': True, 'received': received}


def status(db):
    c = connect(db)
    try:
        row = c.execute("SELECT value FROM line_connection_state WHERE key='last_verified_at'").fetchone()
        sources = [dict(r) for r in c.execute('SELECT * FROM line_sources ORDER BY last_seen DESC')]
        return {'webhook_path': '/webhooks/line',
                'secret_configured': bool(os.getenv('LINE_CHANNEL_SECRET', '').strip()),
                'access_token_configured': bool(os.getenv('LINE_CHANNEL_ACCESS_TOKEN', '').strip()),
                'last_verified_at': row['value'] if row else None,
                'sources': sources, 'delivery_enabled': False, 'receipt_import_enabled': False}
    finally:
        c.close()


MENTION_STAFF = (('なかがわさん', 'nakagawa'), ('やまもとさん', 'yamamoto'),
                 ('いまむらさん', 'imamura'), ('さやさん', 'saya'))
MENTION_DAY = '2026-10-21'


def register_inventory_mention(c, events):
    """Owner-authorized, one-day ordered registration; never infer from names."""
    import re, time
    from datetime import timedelta
    claimed = set()
    owner = concierge.setting(c, 'owner')
    group = __import__('line_delivery').TARGET
    state_key = 'inventory_mention_bulk:' + MENTION_DAY
    for event in events:
        src, msg = event.get('source', {}), event.get('message', {})
        if not (owner and src.get('type') == 'group' and src.get('groupId') == group
                and src.get('userId') == owner and msg.get('type') == 'text'):
            continue
        text = msg.get('text', '').strip()
        explicit = next(((name, key) for name, key in MENTION_STAFF
                         if text.startswith('メンション登録 ' + name)), None)
        timestamp = event.get('timestamp')
        event_day = (datetime.fromtimestamp(timestamp / 1000, timezone(timedelta(hours=9))).date().isoformat()
                     if isinstance(timestamp, (int, float)) else '')
        completed = json.loads(concierge.setting(c, state_key, '[]'))
        mentions = msg.get('mention', {}).get('mentionees', [])
        ordered = sorted(mentions, key=lambda m: m.get('index', 0))
        if not explicit and not (event_day == MENTION_DAY and len(completed) < len(MENTION_STAFF) and mentions):
            continue
        eid = event['webhookEventId']
        claimed.add(eid)
        if not c.execute('INSERT OR IGNORE INTO supply_commands VALUES(?,?)', (eid, time.time())).rowcount:
            continue
        people = [m.get('userId') for m in ordered if m.get('type') == 'user' and not m.get('isSelf')]
        count = 1 if explicit else len(MENTION_STAFF) - len(completed)
        valid = (people and len(people) <= count and len(people) == len(mentions)
                 and len(set(people)) == len(people)
                 and all(re.fullmatch(r'U[0-9a-fA-F]{32}', uid or '') for uid in people)
                 and (bool(explicit) or not set(people).intersection(completed)))
        if valid:
            try:
                for uid in people:
                    __import__('line_delivery').api('group/' + group + '/member/' + uid)
            except Exception:
                valid = False
        if valid:
            targets = [explicit] if explicit else MENTION_STAFF[len(completed):len(completed) + len(people)]
            for (name, key), uid in zip(targets, people):
                concierge.put(c, 'inventory_mention_' + key, uid)
            concierge.put(c, 'inventory_mention_group', group)
            if not explicit:
                completed.extend(people)
                concierge.put(c, state_key, json.dumps(completed))
            answer = 'メンション登録済み：' + '、'.join(name for name, _ in targets) + '。'
            if not explicit:
                answer += ('4名全員の登録が完了しました。' if len(completed) == 4
                           else '次は' + MENTION_STAFF[len(completed)][0] + 'を@メンションしてください。')
        else:
            answer = '登録できませんでした。対象者をLINEの候補から@選択してください。@All、重複、名前の文字入力では登録しません。順番は進めていません。'
        supplies.queue(c, 'mention-bind:' + eid, group, [{'type': 'text', 'text': answer}])
    return claimed


def inventory_mention(c, text):
    """Render real mentions only for identities explicitly bound by the owner."""
    import re
    if concierge.setting(c, 'inventory_mention_group') != __import__('line_delivery').TARGET:
        return {'type': 'text', 'text': text}
    escaped = text.replace('{', '{{').replace('}', '}}')
    substitutions = {}
    for name, key in MENTION_STAFF:
        uid = concierge.setting(c, 'inventory_mention_' + key)
        if name in escaped and re.fullmatch(r'U[0-9a-fA-F]{32}', uid):
            escaped = escaped.replace(name, '{' + key + '}')
            substitutions[key] = {'type': 'mention', 'mentionee': {'type': 'user', 'userId': uid}}
    return ({'type': 'textV2', 'text': escaped, 'substitution': substitutions}
            if substitutions else {'type': 'text', 'text': text})
