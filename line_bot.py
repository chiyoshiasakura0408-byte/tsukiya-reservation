"""LINE webhook connection layer. Does not send messages or process receipts yet."""
import base64
import hashlib
import hmac
import json
import os
import sqlite3
import line_receipts
import concierge
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
            line_receipts.enqueue(c, raw, signature, payload)
            c.execute('INSERT OR REPLACE INTO line_connection_state VALUES (?,?)', ('last_verified_at', now))
    finally:
        c.close()
    import guest_service
    guest_service.receive_stock(db, events)
    concierge.pair_owner(db, events)
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
