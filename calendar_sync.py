"""Opt-in, one-way Calendar projection. Never writes reservation rows or notifies guests."""
import fcntl
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

LOG = logging.getLogger(__name__)
JST = ZoneInfo('Asia/Tokyo')
ACTIVE = {'PENDING', 'INVOICED', 'CONFIRMED', 'ERROR'}
TERMINAL = {'CANCELLED', 'CANCELED', 'EXPIRED'}


class SyncError(Exception):
    pass


def clean(value):
    text = str(value or '')
    text = re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[メール非表示]', text)
    return re.sub(r'(?<!\d)\+?\d[\d ()-]{8,}\d(?!\d)', '[電話番号非表示]', text)[:1500]


def event_id(reservation_id, namespace):
    return '75' + hashlib.sha256(f'{namespace}:{reservation_id}'.encode()).hexdigest()


def event_body(row, namespace):
    status = row['status']
    if status not in ACTIVE | TERMINAL:
        raise SyncError('unknown_reservation_status')
    if status in TERMINAL:
        return None
    start = datetime.fromisoformat(row['visit_at'])
    start = start.replace(tzinfo=JST) if start.tzinfo is None else start.astimezone(JST)
    duration = 300 if row.get('vip_through') else int(row.get('duration_minutes') or 150)
    if duration <= 0:
        raise SyncError('invalid_duration')
    area = {'PRIVATE1': '個室①', 'PRIVATE2': '個室②', 'PRIVATE3': '個室③', 'COUNTER': 'カウンター'}.get(row.get('seating_area'), '席未確定')
    paid = bool(row.get('payment_confirmed_at'))
    payment = '決済済み' if paid else ('事前決済なし・当日支払い' if row.get('source') in ('DIRECT', 'CONCIERGE_LINE') else '決済未確認')
    details = [f"予約番号：{row['id']}", f"人数：{row['party_size']}名", f'席：{area}', f"コース：{clean(row.get('course_name'))}", f'決済：{payment}', f'予約状態：{status}']
    for key, label in [('celebration_items', 'お祝い'), ('plate_message', 'プレート'), ('guest_note', 'ご要望')]:
        if row.get(key):
            details.append(f'{label}：{clean(row[key])}')
    return {'summary': f"{clean(row['guest_name'])}様｜{row['party_size']}名｜{area}",
            'description': '\n'.join(details), 'start': {'dateTime': start.isoformat(), 'timeZone': 'Asia/Tokyo'},
            'end': {'dateTime': (start + timedelta(minutes=duration)).isoformat(), 'timeZone': 'Asia/Tokyo'},
            'visibility': 'private', 'status': 'confirmed', 'attendees': [],
            'reminders': {'useDefault': False},
            'extendedProperties': {'private': {'tsukiya_namespace': namespace, 'reservation_id': str(row['id'])}}}


class Google:
    def __init__(self, calendar_id):
        if calendar_id == 'primary' or not calendar_id.endswith('@group.calendar.google.com'):
            raise SyncError('dedicated_secondary_calendar_required')
        self.calendar_id = calendar_id
        self.token = None
        self.expires = 0

    def access_token(self):
        if self.token and self.expires > time.time() + 60:
            return self.token
        data = urllib.parse.urlencode({'client_id': os.environ['GOOGLE_CALENDAR_CLIENT_ID'],
            'client_secret': os.environ['GOOGLE_CALENDAR_CLIENT_SECRET'],
            'refresh_token': os.environ['GOOGLE_CALENDAR_REFRESH_TOKEN'], 'grant_type': 'refresh_token'}).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request('https://oauth2.googleapis.com/token', data=data), timeout=20) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            raise SyncError(f'oauth_http_{error.code}') from None
        self.token, self.expires = result['access_token'], time.time() + int(result.get('expires_in', 3600))
        return self.token

    def call(self, method, path='', body=None, missing_ok=False):
        url = 'https://www.googleapis.com/calendar/v3/calendars/' + urllib.parse.quote(self.calendar_id, safe='') + path
        if method != 'GET':
            url += '?sendUpdates=none'
        request = urllib.request.Request(url, method=method, data=None if body is None else json.dumps(body).encode(),
            headers={'Authorization': 'Bearer ' + self.access_token(), 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read() or b'{}')
        except urllib.error.HTTPError as error:
            if missing_ok and error.code in (404, 410):
                return None
            if error.code == 401:
                self.expires = 0
            raise SyncError(f'calendar_http_{error.code}') from None

    def check_calendar(self):
        calendar = self.call('GET')
        if calendar.get('summary') != '西天満つきや｜予約管理':
            raise SyncError('dedicated_calendar_name_mismatch')
        acl = self.call('GET', '/acl')
        if acl.get('nextPageToken') or any(item.get('scope', {}).get('type') in ('default', 'domain') for item in acl.get('items', [])):
            raise SyncError('calendar_not_private')

    def sync(self, ident, body, namespace, reservation_id):
        path = '/events/' + ident
        existing = self.call('GET', path, missing_ok=True)
        if existing is not None:
            private = existing.get('extendedProperties', {}).get('private', {})
            if private.get('tsukiya_namespace') != namespace or private.get('reservation_id') != str(reservation_id):
                raise SyncError('event_ownership_mismatch')
        if body is None:
            if existing and existing.get('status') != 'cancelled':
                self.call('DELETE', path, missing_ok=True)
        elif existing is not None:
            self.call('PATCH', path, body)
        else:
            self.call('POST', '/events', dict(body, id=ident))


def schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS calendar_sync_state (
        calendar_id TEXT NOT NULL, reservation_id INTEGER NOT NULL, event_id TEXT NOT NULL,
        fingerprint TEXT, attempts INTEGER NOT NULL DEFAULT 0, next_retry REAL NOT NULL DEFAULT 0,
        last_success REAL, last_error TEXT, PRIMARY KEY(calendar_id,reservation_id))''')
    db.commit()


def reconcile(db_path, client, namespace, now=None):
    now = time.time() if now is None else now
    with sqlite3.connect(db_path, timeout=30) as db:
        db.row_factory = sqlite3.Row
        schema(db)
        rows = {r['id']: dict(r) for r in db.execute('SELECT * FROM reservations')}
        states = {r['reservation_id']: dict(r) for r in db.execute('SELECT * FROM calendar_sync_state WHERE calendar_id=?', (client.calendar_id,))}
    result = {'synced': 0, 'failed': 0, 'unchanged': 0}
    for rid in sorted(rows.keys() | states.keys()):
        previous = states.get(rid, {})
        ident = event_id(rid, namespace)
        fingerprint = None
        try:
            body = event_body(rows[rid], namespace) if rid in rows else None
            fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if previous.get('fingerprint') == fingerprint:
                if previous.get('last_error') and previous.get('next_retry', 0) > now:
                    continue
                if not previous.get('last_error') and now - (previous.get('last_success') or 0) < 21600:
                    result['unchanged'] += 1
                    continue
            # No event was created for an already cancelled booking.
            if body is not None or previous:
                client.sync(ident, body, namespace, rid)
            values = (client.calendar_id, rid, ident, fingerprint, 0, 0, now, None)
            result['synced'] += 1
        except Exception as error:
            # Only error class and allowlisted codes are stored, never API bodies or guest data.
            code = str(error) if isinstance(error, SyncError) else type(error).__name__
            attempts = previous.get('attempts', 0) + 1
            values = (client.calendar_id, rid, ident, fingerprint, attempts,
                      now + min(3600, 30 * 2 ** min(attempts - 1, 7)), previous.get('last_success'), code)
            result['failed'] += 1
            LOG.warning('calendar sync failed reservation_id=%s code=%s', rid, code)
        with sqlite3.connect(db_path, timeout=30) as db:
            db.execute('INSERT OR REPLACE INTO calendar_sync_state VALUES (?,?,?,?,?,?,?,?)', values)
    return result


def loop(db_path):
    if os.getenv('GOOGLE_CALENDAR_SYNC_ENABLED') != '1':
        return
    required = ('GOOGLE_CALENDAR_ID', 'GOOGLE_CALENDAR_CLIENT_ID', 'GOOGLE_CALENDAR_CLIENT_SECRET', 'GOOGLE_CALENDAR_REFRESH_TOKEN')
    if not all(os.getenv(key) for key in required):
        LOG.error('calendar sync disabled: missing configuration')
        return
    namespace = os.getenv('GOOGLE_CALENDAR_NAMESPACE', 'tsukiya-production')
    client = Google(os.environ['GOOGLE_CALENDAR_ID'])
    while True:
        try:
            with open(str(db_path) + '.calendar.lock', 'a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                client.check_calendar()
                reconcile(db_path, client, namespace)
        except BlockingIOError:
            pass
        except Exception as error:
            LOG.error('calendar sync cycle failed: %s', str(error) if isinstance(error, SyncError) else type(error).__name__)
        time.sleep(30)
