"""Signed, staff-only LINE transport. Credentials remain on this server."""
import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
import line_receipts

BOT_ID = '@551zcgpe'
DOMAIN = b'tsukiya-shift-message-v1\n'
CALENDAR = re.compile(r'https://tsukiya-line-receiver\.chiyoshi-a-0408\.chatgpt\.site/shift-calendar#[0-9a-f-]{36}\.[0-9a-f]{64}')
_verified = None

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

def line_request(path, token, body=None, retry_key=None):
    headers = {'Authorization': 'Bearer '+token, 'Content-Type': 'application/json'}
    if retry_key:
        headers['X-Line-Retry-Key'] = retry_key
    req = urllib.request.Request('https://api.line.me/v2/bot/'+path,
        data=None if body is None else json.dumps(body, ensure_ascii=False).encode(), headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=8) as r:
            return r.status, json.loads(r.read(16384)), False
    except urllib.error.HTTPError as e:
        return e.code, {}, bool(e.headers.get('x-line-accepted-request-id'))
    except Exception:
        return 502, {}, False

def verify_bot(token, force=False):
    global _verified
    digest = hashlib.sha256(token.encode()).hexdigest()
    if not force and _verified and _verified[0] == digest and _verified[1] > time.time():
        return True
    status, info, _ = line_request('info', token)
    if status == 200 and info.get('basicId') == BOT_ID:
        _verified = (digest, time.time()+60)
        return True
    _verified = None
    return False

def handle(db, raw, signature):
    secret = line_receipts.bridge_secret(db)
    if not secret or not hmac.compare_digest(hmac.new(secret.encode(), DOMAIN+raw, hashlib.sha256).hexdigest(), signature):
        return 403, {'error': 'invalid_signature'}
    try:
        x = json.loads(raw)
        if not isinstance(x, dict) or type(x.get('timestamp')) is not int or abs(time.time()-x['timestamp']) > 300:
            return 403, {'error': 'expired_request'}
        action = x.get('action')
        if action not in ('status', 'reply', 'push'):
            return 400, {'error': 'invalid_action'}
        token = os.getenv('LINE_CHANNEL_ACCESS_TOKEN', '').strip()
        if not token:
            return 503, {'error': 'line_not_configured'}
        if action == 'status':
            if not verify_bot(token, force=True):
                return 502, {'error': 'staff_bot_unavailable'}
            return 200, {'ready': True, 'basicId': BOT_ID}
        to, text = x.get('to', ''), x.get('text', '')
        if not isinstance(to, str) or not re.fullmatch(r'U[0-9a-f]{32}', to) or not isinstance(text, str) or not 1 <= len(text) <= 4900:
            return 400, {'error': 'invalid_message'}
        c = sqlite3.connect(db, timeout=10)
        try:
            c.execute('CREATE TABLE IF NOT EXISTS shift_delivery_keys(id TEXT PRIMARY KEY, digest TEXT NOT NULL, created REAL NOT NULL, accepted INTEGER NOT NULL DEFAULT 0)')
            # A recipient must have interacted with this exact business bot.
            source = c.execute("SELECT 1 FROM line_sources WHERE source_id=? AND source_type='user' AND active=1", (to,)).fetchone()
            if not source:
                return 403, {'error': 'recipient_not_linked'}
            if action == 'push':
                retry = x.get('retryKey', '')
                if not isinstance(retry, str) or not re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', retry):
                    return 400, {'error': 'invalid_retry_key'}
                body = {'to': to, 'messages': [{'type': 'text', 'text': text}]}
                key = 'push:'+retry
            else:
                retry = None
                event_id, reply_token = x.get('eventId', ''), x.get('replyToken', '')
                if not isinstance(event_id, str) or not 1 <= len(event_id) <= 100 or not isinstance(reply_token, str) or not 1 <= len(reply_token) <= 200:
                    return 400, {'error': 'invalid_reply'}
                event = c.execute("SELECT 1 FROM line_received_events WHERE event_id=? AND source_id=? AND event_type='message' AND message_type='text'", (event_id, to)).fetchone()
                if not event:
                    return 403, {'error': 'event_not_received'}
                messages = [{'type': 'text', 'text': text}]
                if 'url' in x:
                    if not isinstance(x['url'], str) or not CALENDAR.fullmatch(x['url']) or len(text) > 160:
                        return 400, {'error': 'invalid_calendar_link'}
                    messages = [{'type': 'template', 'altText': text, 'template': {'type': 'buttons', 'text': text,
                        'actions': [{'type': 'uri', 'label': '希望シフトを開く', 'uri': x['url']}]}}]
                body = {'replyToken': reply_token, 'messages': messages}
                key = 'reply:'+hashlib.sha256(reply_token.encode()).hexdigest()
            digest = hashlib.sha256(json.dumps({'to': to, 'body': body}, sort_keys=True).encode()).hexdigest()
            with c:
                c.execute('INSERT OR IGNORE INTO shift_delivery_keys(id,digest,created) VALUES(?,?,?)', (key, digest, time.time()))
            row = c.execute('SELECT digest,created,accepted FROM shift_delivery_keys WHERE id=?', (key,)).fetchone()
            if not hmac.compare_digest(row[0], digest):
                return 409, {'error': 'retry_payload_changed'}
            if row[2]:
                return 200, {'accepted': True, 'duplicate': True}
            if time.time()-row[1] > 23*3600:
                return 409, {'error': 'retry_expired'}
            if not verify_bot(token):
                return 502, {'error': 'staff_bot_unavailable'}
            status, _, accepted_before = line_request('message/'+action, token, body, retry)
            if status == 200 or (action == 'push' and status == 409 and accepted_before):
                with c:
                    c.execute('UPDATE shift_delivery_keys SET accepted=1 WHERE id=? AND digest=?', (key, digest))
                return 200, {'accepted': True}
            return 502, {'error': 'line_send_failed', 'lineStatus': status}
        finally:
            c.close()
    except (KeyError, ValueError, TypeError):
        return 400, {'error': 'invalid_request'}
    except Exception:
        return 503, {'error': 'shift_transport_unavailable'}
