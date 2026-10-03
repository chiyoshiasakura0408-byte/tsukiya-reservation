"""Customer LINE concierge. Separate channel, signed ingress and durable push outbox."""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.request
import urllib.error
import uuid
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse

LOCK = threading.RLock()
DELIVERY_LOCK = threading.Lock()
JST = timezone(timedelta(hours=9))
MENU = ['空席確認', 'コース・料金', '写真', '動画', 'お品書き', '朝倉へ相談', '入荷案内を受け取る', '配信停止']


def connect(db):
    c = sqlite3.connect(db, timeout=30)
    c.row_factory = sqlite3.Row
    c.executescript('''
    CREATE TABLE IF NOT EXISTS concierge_settings (key TEXT PRIMARY KEY,value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS concierge_customers (user_id TEXT PRIMARY KEY,active INTEGER DEFAULT 1,subscribed INTEGER DEFAULT 0,state TEXT DEFAULT '{}');
    CREATE TABLE IF NOT EXISTS concierge_events (id TEXT PRIMARY KEY,received REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS concierge_requests (id TEXT PRIMARY KEY,user_id TEXT NOT NULL,body TEXT NOT NULL,created REAL NOT NULL,status TEXT NOT NULL DEFAULT '未回答',delivery_id TEXT);
    CREATE TABLE IF NOT EXISTS concierge_outbox (id TEXT PRIMARY KEY,user_id TEXT NOT NULL,channel TEXT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',created REAL NOT NULL,next_try REAL NOT NULL,attempts INTEGER DEFAULT 0,error TEXT);
    ''')
    return c


def setting(c, key, default=''):
    row = c.execute('SELECT value FROM concierge_settings WHERE key=?', (key,)).fetchone()
    return row[0] if row else default


def put(c, key, value):
    c.execute('INSERT OR REPLACE INTO concierge_settings VALUES (?,?)', (key, str(value)))


def text_message(text, menu=True):
    message = {'type': 'text', 'text': text[:4900]}
    if menu:
        message['quickReply'] = {'items': [{'type': 'action', 'action': {'type': 'message', 'label': label, 'text': label}} for label in MENU]}
    return message


def enqueue(c, user, messages, channel='customer', kind='reply'):
    key = str(uuid.uuid4())
    c.execute('INSERT INTO concierge_outbox(id,user_id,channel,kind,payload,created,next_try) VALUES (?,?,?,?,?,?,?)',
              (key, user, channel, kind, json.dumps(messages, ensure_ascii=False), time.time(), time.time()))
    return key


def catalog(c):
    return json.loads(setting(c, 'catalog', '{}'))


def valid_url(value):
    if not value:
        return ''
    if not isinstance(value, str) or len(value) > 2000:
        raise ValueError('URLが不正です')
    u = urlparse(value)
    if u.scheme != 'https' or not u.hostname or u.username or u.password or u.hostname in ('localhost', '127.0.0.1', '::1'):
        raise ValueError('画像・動画は公開HTTPS URLを指定してください')
    return value


def configure(db, data):
    with LOCK:
        c = connect(db)
        try:
            with c:
                action = data.get('action')
                if action == 'pair':
                    code = secrets.token_urlsafe(12)
                    put(c, 'pair_hash', hashlib.sha256(code.encode()).hexdigest())
                    put(c, 'pair_expires', time.time() + 600)
                    return {'pair_command': 'つきや朝倉連携 ' + code, 'expires_in': 600}
                if action == 'enable':
                    if not (os.getenv('CONCIERGE_LINE_CHANNEL_SECRET') and os.getenv('CONCIERGE_LINE_CHANNEL_ACCESS_TOKEN') and setting(c, 'verified')):
                        raise ValueError('お客様用LINEの資格情報とWebhook検証が必要です')
                    if os.getenv('CONCIERGE_LINE_CHANNEL_SECRET') == os.getenv('LINE_CHANNEL_SECRET'):
                        raise ValueError('お客様用と業務用のLINEは別チャネルにしてください')
                    if not setting(c, 'owner') or not os.getenv('LINE_CHANNEL_ACCESS_TOKEN'):
                        raise ValueError('朝倉さんの個人LINE連携が必要です')
                    put(c, 'enabled', '1')
                elif action == 'disable':
                    put(c, 'enabled', '0')
                    c.execute("UPDATE concierge_outbox SET state='cancelled' WHERE state='pending'")
                elif action == 'catalog':
                    new = {}
                    for key, maximum in [('crab', 100), ('menu', 3000)]:
                        value = data.get(key, '')
                        if not isinstance(value, str) or len(value) > maximum:
                            raise ValueError('入力文字数を確認してください')
                        new[key] = value.strip()
                    for key in ('photo', 'video', 'preview', 'menu_url'):
                        new[key] = valid_url(data.get(key, ''))
                    if bool(new['video']) != bool(new['preview']):
                        raise ValueError('動画にはサムネイル画像URLも必要です')
                    previous = catalog(c)
                    changed = bool(new['crab']) and new['crab'] != previous.get('crab')
                    put(c, 'catalog', json.dumps(new, ensure_ascii=False))
                    if changed and data.get('announce') is True:
                        if setting(c, 'enabled') != '1':
                            raise ValueError('配信を有効にしてからお知らせしてください')
                        for row in c.execute('SELECT user_id FROM concierge_customers WHERE active=1 AND subscribed=1'):
                            enqueue(c, row[0], [text_message('西天満つきやより、蟹のお知らせです。\n今回の蟹：' + new['crab'] + '\nコース詳細や空席は下のメニューからご確認ください。')], kind='announcement')
                elif action == 'answer':
                    body = data.get('body', '')
                    if not isinstance(body, str) or not 1 <= len(body.strip()) <= 2000:
                        raise ValueError('回答は1〜2000文字で入力してください')
                    row = c.execute('SELECT * FROM concierge_requests WHERE id=?', (data.get('id'),)).fetchone()
                    if not row or row['status'] != '未回答':
                        raise ValueError('未回答のご相談が見つかりません')
                    if setting(c, 'enabled') != '1':
                        raise ValueError('配信が停止中です')
                    delivery_id = enqueue(c, row['user_id'], [text_message('朝倉からの回答です。\n' + body.strip())], kind='answer')
                    c.execute("UPDATE concierge_requests SET status='回答送信待ち',delivery_id=? WHERE id=?", (delivery_id, row['id']))
                else:
                    raise ValueError('操作が不正です')
            return {'ok': True}
        finally:
            c.close()


def pair_owner(db, events):
    """Called only AFTER signature verification on the existing work channel."""
    events = [event for event in events if isinstance(event.get('message', {}).get('text'), str) and event['message']['text'].startswith('つきや朝倉連携 ')]
    if not events:
        return
    with LOCK:
        c = connect(db)
        try:
            with c:
                for event in events:
                    src = event.get('source', {})
                    msg = event.get('message', {})
                    body = msg.get('text', '')
                    if src.get('type') != 'user' or not src.get('userId') or not isinstance(body, str) or not body.startswith('つきや朝倉連携 '):
                        continue
                    code = body.removeprefix('つきや朝倉連携 ').strip()
                    if time.time() <= float(setting(c, 'pair_expires', '0')) and hmac.compare_digest(hashlib.sha256(code.encode()).hexdigest(), setting(c, 'pair_hash')):
                        put(c, 'owner', src['userId'])
                        put(c, 'pair_expires', '0')
                        put(c, 'pair_hash', '')
        finally:
            c.close()


def respond(c, user, command, lookup, courses, base_url):
    cat = catalog(c)
    if command == '配信停止':
        c.execute('UPDATE concierge_customers SET subscribed=0 WHERE user_id=?', (user,))
        c.execute("UPDATE concierge_outbox SET state='cancelled' WHERE user_id=? AND kind='announcement' AND state='pending'", (user,))
        return [text_message('蟹の変更・入荷案内の配信を停止しました。空席確認・ご予約は引き続きご利用いただけます。')]
    if command == '入荷案内を受け取る':
        c.execute('UPDATE concierge_customers SET subscribed=1 WHERE user_id=?', (user,))
        return [text_message('蟹の変更・入荷案内をお届けします。「配信停止」でいつでも停止できます。')]
    if command == 'コース・料金':
        description = '\n\n'.join(f'{name}\nお一人様 ¥{price:,}（税込）' for name, price in courses.values())
        return [text_message(description + '\n\nお料理代は事前決済、お飲み物代は当日のお支払いです。提供期間と空席は「空席確認」でご確認ください。')]
    if command in ('写真', '動画', 'お品書き'):
        if command == '写真' and cat.get('photo'):
            return [{'type': 'image', 'originalContentUrl': cat['photo'], 'previewImageUrl': cat['photo']}]
        if command == '動画' and cat.get('video') and cat.get('preview'):
            return [{'type': 'video', 'originalContentUrl': cat['video'], 'previewImageUrl': cat['preview']}]
        if command == 'お品書き' and (cat.get('menu') or cat.get('menu_url')):
            return [text_message('\n'.join(filter(None, [cat.get('crab'), cat.get('menu'), cat.get('menu_url')])))]
        return [text_message('こちらのご案内は準備中です。詳しい内容は「朝倉へ相談」からお問い合わせください。')]
    if command == '空席確認':
        return [text_message('ご希望日と人数を「2026-11-10 2名」の形式で送ってください。予約台帳の空席を確認します。2〜8名様以外のご相談は「朝倉へ相談」へ。')]
    match = re.fullmatch(r'(\d{4}-\d{2}-\d{2})\s+(\d+)\s*名?', command)
    if match:
        try:
            day = date.fromisoformat(match[1])
            party = int(match[2])
            today = datetime.now(JST).date()
            if not today <= day <= today + timedelta(days=365) or not 2 <= party <= 8:
                raise ValueError()
        except ValueError:
            return [text_message('本日から1年以内の日付と、2〜8名様でご指定ください。')]
        try:
            slots = lookup(day, party)
        except Exception:
            return [text_message('現在、空席を確認できません。少し後にお試しいただくか、「朝倉へ相談」からお問い合わせください。')]
        if not slots:
            return [text_message(f'{day}・{party}名様は、満席または受付期間外です。別の日付をお送りいただくか、朝倉へご相談ください。')]
        actions = []
        for slot in slots[:4]:
            query = urlencode({'course': slot['course'], 'date': str(day), 'party_size': party, 'area': slot['area'], 'time': slot['time']})
            actions.append({'type': 'uri', 'label': slot['time'] + (' 個室' if slot['area'] == 'PRIVATE' else ' カウンター'), 'uri': base_url + '/book?' + query})
        return [text_message(f'{day}・{party}名様の空席が見つかりました。お料理代合計 ¥{courses[slots[0]["course"]][1]*party:,}（税込）。\n空席は変動します。以下からお客様情報の入力・事前決済へお進みください。決済完了後にご予約確定となります。'),
                {'type': 'template', 'altText': '空席を選んで予約へ進む', 'template': {'type': 'buttons', 'text': 'ご希望のお席・時間をお選びください', 'actions': actions}}]
    if command == '朝倉へ相談':
        c.execute("UPDATE concierge_customers SET state='request' WHERE user_id=?", (user,))
        return [text_message('ご希望日時・お名前・ご要望をお送りください。内容を店舗管理画面に保存し、朝倉へ取り次ぎます。アレルギー等の対応可否は朝倉からの回答をお待ちください。')]
    state = c.execute('SELECT state FROM concierge_customers WHERE user_id=?', (user,)).fetchone()[0]
    if state == 'request' and command not in ('メニュー', 'キャンセル'):
        if len(command) > 2000:
            return [text_message('ご相談は2000文字以内でお送りください。')]
        request_id = uuid.uuid4().hex[:12]
        c.execute('INSERT INTO concierge_requests(id,user_id,body,created) VALUES (?,?,?,?)', (request_id, user, command, time.time()))
        c.execute("UPDATE concierge_customers SET state='{}' WHERE user_id=?", (user,))
        owner = setting(c, 'owner')
        if owner:
            enqueue(c, owner, [text_message('【常連様からのご相談】受付 ' + request_id + '\n' + command + '\n回答はこちら：' + base_url + '/concierge', False)], channel='owner', kind='request')
        return [text_message('ご相談を受け付けました（受付番号 ' + request_id + '）。朝倉の確認・回答をお待ちください。この時点では予約・特別対応は確定していません。')]
    c.execute("UPDATE concierge_customers SET state='{}' WHERE user_id=?", (user,))
    return [text_message('西天満つきやの予約コンシェルジュです。下のメニューから空席確認・コース案内・ご相談をお選びください。')]


def receive(db, raw, signature, lookup, courses, base_url):
    secret = os.getenv('CONCIERGE_LINE_CHANNEL_SECRET', '')
    if not secret:
        return 503, {'error': 'customer channel is not configured'}
    if len(raw) > 1024*1024:
        return 413, {'error': 'payload too large'}
    expected = base64.b64encode(hmac.new(secret.encode(), raw, hashlib.sha256).digest()).decode()
    if not isinstance(signature, str) or not hmac.compare_digest(expected, signature):
        return 403, {'error': 'invalid signature'}
    try:
        events = json.loads(raw)['events']
        if not isinstance(events, list):
            raise ValueError()
        for e in events:
            if not isinstance(e, dict) or not isinstance(e.get('webhookEventId'), str) or not e['webhookEventId'] or not isinstance(e.get('source', {}), dict) or not isinstance(e.get('message', {}), dict):
                raise ValueError()
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        return 400, {'error': 'invalid payload'}
    resolved = {}
    for e in events:
        command = e.get('message', {}).get('text', '')
        match = re.fullmatch(r'(\d{4}-\d{2}-\d{2})\s+(\d+)\s*名?', command.strip()) if isinstance(command, str) else None
        if match:
            try:
                day, party = date.fromisoformat(match[1]), int(match[2])
                today = datetime.now(JST).date()
                if today <= day <= today + timedelta(days=365) and 2 <= party <= 8:
                    resolved[(day, party)] = lookup(day, party)
            except Exception:
                pass
    def cached_lookup(day, party):
        if (day, party) not in resolved:
            raise RuntimeError('availability unavailable')
        return resolved[(day, party)]
    with LOCK:
        c = connect(db)
        try:
            with c:
                put(c, 'verified', datetime.now(JST).isoformat())
                if setting(c, 'enabled') != '1':
                    return 200, {'ok': True, 'enabled': False}
                for e in events:
                    if not c.execute('INSERT OR IGNORE INTO concierge_events VALUES (?,?)', (e['webhookEventId'], time.time())).rowcount:
                        continue
                    src = e.get('source', {})
                    user = src.get('userId')
                    if src.get('type') != 'user' or not isinstance(user, str) or not user:
                        continue
                    if e.get('type') == 'unfollow':
                        c.execute('UPDATE concierge_customers SET active=0,subscribed=0 WHERE user_id=?', (user,))
                        c.execute("UPDATE concierge_outbox SET state='cancelled' WHERE user_id=? AND channel='customer' AND state='pending'", (user,))
                        continue
                    msg = e.get('message', {})
                    if e.get('type') != 'follow' and not (e.get('type') == 'message' and msg.get('type') == 'text' and isinstance(msg.get('text'), str)):
                        continue
                    c.execute('INSERT INTO concierge_customers(user_id) VALUES (?) ON CONFLICT(user_id) DO UPDATE SET active=1', (user,))
                    command = msg.get('text', 'メニュー').strip()
                    enqueue(c, user, respond(c, user, command, cached_lookup, courses, base_url))
        finally:
            c.close()
    return 200, {'ok': True}


def deliver(db):
    # A separate sender lock keeps webhook ingestion responsive during network calls.
    # Stable retry keys survive restarts; recheck cancellation before each push.
    with DELIVERY_LOCK:
        c = connect(db)
        try:
            if setting(c, 'enabled') != '1':
                return
            rows = c.execute("SELECT * FROM concierge_outbox WHERE state='pending' AND next_try<=? ORDER BY created LIMIT 10", (time.time(),)).fetchall()
            for row in rows:
                current = c.execute('SELECT state FROM concierge_outbox WHERE id=?', (row['id'],)).fetchone()
                if setting(c, 'enabled') != '1' or not current or current['state'] != 'pending':
                    continue
                if time.time() - row['created'] > 23*3600:
                    with c:
                        c.execute("UPDATE concierge_outbox SET state='failed',error='retry window expired' WHERE id=?", (row['id'],))
                        c.execute("UPDATE concierge_requests SET status='回答送信失敗' WHERE delivery_id=?", (row['id'],))
                    continue
                name = 'LINE_CHANNEL_ACCESS_TOKEN' if row['channel'] == 'owner' else 'CONCIERGE_LINE_CHANNEL_ACCESS_TOKEN'
                token = os.getenv(name, '')
                error, accepted, retryable = 'channel token missing', False, False
                if token:
                    body = json.dumps({'to': row['user_id'], 'messages': json.loads(row['payload'])}).encode()
                    req = urllib.request.Request('https://api.line.me/v2/bot/message/push', data=body, headers={'Authorization': 'Bearer '+token, 'Content-Type': 'application/json', 'X-Line-Retry-Key': row['id']})
                    try:
                        with urllib.request.urlopen(req, timeout=10) as response:
                            accepted = response.status == 200
                    except urllib.error.HTTPError as exc:
                        accepted = exc.code == 409 and bool(exc.headers.get('x-line-accepted-request-id'))
                        error = 'LINE HTTP ' + str(exc.code)
                        retryable = exc.code == 429 or exc.code >= 500
                    except (OSError, TimeoutError):
                        error, retryable = 'LINE connection failed', True
                with c:
                    if row['kind'] == 'answer' and (accepted or not retryable):
                        c.execute('UPDATE concierge_requests SET status=? WHERE delivery_id=?', ('回答LINE受付済み' if accepted else '回答送信失敗', row['id']))
                    c.execute('UPDATE concierge_outbox SET state=?,error=?,attempts=attempts+1,next_try=? WHERE id=?',
                              ('accepted' if accepted else 'pending' if retryable else 'failed', None if accepted else error, time.time()+min(3600, 30*2**min(row['attempts'], 7)), row['id']))
        finally:
            c.close()


def status(db):
    with LOCK:
        c = connect(db)
        try:
            return {'enabled': setting(c, 'enabled') == '1', 'verified_at': setting(c, 'verified'), 'owner_linked': bool(setting(c, 'owner')),
                    'secret_configured': bool(os.getenv('CONCIERGE_LINE_CHANNEL_SECRET')), 'token_configured': bool(os.getenv('CONCIERGE_LINE_CHANNEL_ACCESS_TOKEN')),
                    'owner_token_configured': bool(os.getenv('LINE_CHANNEL_ACCESS_TOKEN')), 'catalog': catalog(c),
                    'subscribers': c.execute('SELECT count(*) FROM concierge_customers WHERE active=1 AND subscribed=1').fetchone()[0],
                    'requests': [dict(r) for r in c.execute('SELECT id,body,created,status FROM concierge_requests ORDER BY created DESC LIMIT 100')],
                    'delivery': [dict(r) for r in c.execute('SELECT kind,state,count(*) AS count FROM concierge_outbox GROUP BY kind,state')]}
        finally:
            c.close()


def loop(db):
    while True:
        try:
            deliver(db)
        except Exception:
            pass  # Never log tokens, guest texts or request bodies.
        time.sleep(2)
