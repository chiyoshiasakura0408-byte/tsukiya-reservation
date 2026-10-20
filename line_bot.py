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
                if source_id and kind in ('group', 'room', 'user'):
                    active = int(event['type'] not in ('leave', 'unfollow'))
                    c.execute('INSERT INTO line_sources VALUES (?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET last_seen=excluded.last_seen, active=excluded.active',
                              (source_id, kind, now, active))
            claimed = register_inventory_mention(c, events)
            claimed |= line_reservations.ingest(c, raw, signature, payload)
            claimed |= supplies.ingest(c, events, os.getenv('APP_BASE_URL', '').strip().rstrip('/'))
            finance_payload = {**payload, "events": [e for e in events if e["webhookEventId"] not in claimed]}
            finance_raw = json.dumps(finance_payload, ensure_ascii=False).encode() if claimed else raw
            line_receipts.enqueue(c, finance_raw, signature, finance_payload)
            c.execute('INSERT OR REPLACE INTO line_connection_state VALUES (?,?)', ('last_verified_at', now))
    finally:
        c.close()
    import guest_service
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

def register_inventory_mention(c, events):
    """Bind an explicitly mentioned member only on the owner's signed command."""
    import re
    claimed = set()
    owner = concierge.setting(c, 'owner')
    for event in events:
        src, msg = event.get('source', {}), event.get('message', {})
        text = msg.get('text', '').strip()
        if not (src.get('type') == 'group' and src.get('groupId') == __import__('line_delivery').TARGET
                and src.get('userId') == owner and owner
                and msg.get('type') == 'text'
                and text.startswith('メンション登録 やまもとさん')):
            continue
        eid = event['webhookEventId']
        claimed.add(eid)
        if not c.execute('INSERT OR IGNORE INTO supply_commands VALUES(?,?)',
                         (eid, __import__('time').time())).rowcount:
            continue
        people = [m.get('userId') for m in msg.get('mention', {}).get('mentionees', [])
                  if m.get('type') == 'user' and not m.get('isSelf')]
        valid = len(people) == 1 and re.fullmatch(r'U[0-9a-fA-F]{32}', people[0] or '')
        if valid:
            try:
                __import__('line_delivery').api('group/' + src['groupId'] + '/member/' + people[0])
            except RuntimeError:
                valid = False
        if valid:
            concierge.put(c, 'inventory_mention_yamamoto', people[0])
            concierge.put(c, 'inventory_mention_group', src['groupId'])
            answer = 'やまもとさんのメンションを登録しました。今後の担当依頼に実際の@メンションを付けます。'
        else:
            answer = '登録できませんでした。「メンション登録 やまもとさん」に続けて、真瑚さん1名をLINEの候補から@メンションしてください。@Allや名前の文字入力だけでは登録できません。'
        supplies.queue(c, 'mention-bind:' + eid, src['groupId'], [{'type': 'text', 'text': answer}])
    return claimed


def inventory_mention(c, text):
    """Use only an owner-verified group-bound identity; otherwise retain wording."""
    import re
    uid = concierge.setting(c, 'inventory_mention_yamamoto')
    group = concierge.setting(c, 'inventory_mention_group')
    if ('やまもとさん、備品在庫表' not in text or
            group != __import__('line_delivery').TARGET or
            not re.fullmatch(r'U[0-9a-fA-F]{32}', uid)):
        return {'type': 'text', 'text': text}
    escaped = text.replace('{', '{{').replace('}', '}}')
    return {'type': 'textV2', 'text': escaped.replace('やまもとさん、備品在庫表', '{yamamoto}、備品在庫表', 1),
            'substitution': {'yamamoto': {'type': 'mention', 'mentionee': {'type': 'user', 'userId': uid}}}}
