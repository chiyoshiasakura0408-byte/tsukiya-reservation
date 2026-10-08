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
import concierge_menu
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse

LOCK = threading.RLock()
DELIVERY_LOCK = threading.Lock()
JST = timezone(timedelta(hours=9))
MENU = ['空席案内', 'ただいまのコース', 'コース内容', '年間スケジュール', 'VIP担当に相談', '配信停止', '配信再開']


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
    columns = {r['name'] for r in c.execute('PRAGMA table_info(concierge_customers)')}
    if 'customer_id' not in columns:
        c.execute('ALTER TABLE concierge_customers ADD COLUMN customer_id INTEGER')
    if 'stopped' not in columns:
        c.execute('ALTER TABLE concierge_customers ADD COLUMN stopped INTEGER NOT NULL DEFAULT 0')
        # Preserve explicit historical stop requests when upgrading the old opt-in flow.
        c.execute("UPDATE concierge_customers SET stopped=1 WHERE user_id IN (SELECT user_id FROM concierge_outbox WHERE payload LIKE '%案内の配信を停止しました%')")
    c.executescript("""
    CREATE TABLE IF NOT EXISTS concierge_links(hash TEXT PRIMARY KEY,customer_id INTEGER NOT NULL,expires REAL NOT NULL,used INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS concierge_proposals(hash TEXT PRIMARY KEY,user_id TEXT NOT NULL,slot TEXT NOT NULL,expires REAL NOT NULL,reservation_id INTEGER);
    CREATE TABLE IF NOT EXISTS concierge_arrivals(id TEXT PRIMARY KEY,body TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending');
    """)
    c.commit()
    return c


def setting(c, key, default=''):
    row = c.execute('SELECT value FROM concierge_settings WHERE key=?', (key,)).fetchone()
    return row[0] if row else default


def put(c, key, value):
    c.execute('INSERT OR REPLACE INTO concierge_settings VALUES (?,?)', (key, str(value)))


def text_message(text, menu=True):
    message = {'type': 'text', 'text': elegant(re.sub(r'。(?![\n\s]|$)', '。\n\n', text))[:4900]}
    # Keep navigation available until the fixed menu has been installed successfully.
    if menu and not os.path.isfile(os.path.join(os.environ.get('DATA_DIR', '.'), 'concierge-rich-menu-ready')):
        message['quickReply'] = {'items': [{'type': 'action', 'action': {'type': 'message', 'label': label, 'text': label}} for label in MENU]}
    return message


def enqueue(c, user, messages, channel='customer', kind='reply'):
    if user.startswith('ig:') and channel == 'customer':
        channel = 'instagram'
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
                if action == 'customer-link':
                    customer_id = data.get('customer_id')
                    if type(customer_id) is not int or not c.execute('SELECT id FROM customers WHERE id=?', (customer_id,)).fetchone():
                        raise ValueError('顧客を選択してください')
                    code = secrets.token_urlsafe(18)
                    c.execute('INSERT INTO concierge_links VALUES (?,?,?,0)', (hashlib.sha256(code.encode()).hexdigest(), customer_id, time.time()+86400))
                    return {'command': 'お客様連携 ' + code, 'expires_in': 86400}
                if action == 'instagram-enable':
                    import instagram_concierge
                    if not instagram_concierge.configured() or not setting(c, 'instagram_verified') or not setting(c,'owner') or not os.getenv('LINE_CHANNEL_ACCESS_TOKEN'):
                        raise ValueError('Instagramの資格情報・Webhook検証・VIP担当連携が必要です')
                    put(c, 'instagram_enabled', '1')
                elif action == 'instagram-disable':
                    put(c, 'instagram_enabled', '0')
                    c.execute("UPDATE concierge_outbox SET state='cancelled' WHERE channel='instagram' AND state='pending'")
                elif action == 'enable':
                    if not (os.getenv('CONCIERGE_LINE_CHANNEL_SECRET') and os.getenv('CONCIERGE_LINE_CHANNEL_ACCESS_TOKEN') and setting(c, 'verified')):
                        raise ValueError('お客様用LINEの資格情報とWebhook検証が必要です')
                    if os.getenv('CONCIERGE_LINE_CHANNEL_SECRET') == os.getenv('LINE_CHANNEL_SECRET'):
                        raise ValueError('お客様用と業務用のLINEは別チャネルにしてください')
                    if not setting(c, 'owner') or not os.getenv('LINE_CHANNEL_ACCESS_TOKEN'):
                        raise ValueError('VIP担当の個人LINE連携が必要です')
                    put(c, 'enabled', '1')
                elif action == 'disable':
                    put(c, 'enabled', '0')
                    c.execute("UPDATE concierge_outbox SET state='cancelled' WHERE channel!='instagram' AND state='pending'")
                elif action == 'catalog':
                    new = {}
                    for key, maximum in [('crab', 100), ('origin', 100), ('arrival_date', 10), ('menu', 3000), ('notes', 1000), ('sake', 150), ('menu_en', 3000)]:
                        value = data.get(key, '')
                        if not isinstance(value, str) or len(value) > maximum:
                            raise ValueError('入力文字数を確認してください')
                        new[key] = value.strip()
                    for key in ('photo', 'video', 'preview', 'menu_url'):
                        new[key] = valid_url(data.get(key, ''))
                    if bool(new['video']) != bool(new['preview']):
                        raise ValueError('動画にはサムネイル画像URLも必要です')
                    previous = catalog(c)
                    changed = bool(new['crab']) and any(new[k] != previous.get(k, '') for k in ('crab', 'origin', 'arrival_date'))
                    if new['crab']:
                        if not new['origin'] or not new['arrival_date']:
                            raise ValueError('蟹の産地と入荷日を入力してください')
                        date.fromisoformat(new['arrival_date'])
                    put(c, 'catalog', json.dumps(new, ensure_ascii=False))
                    if changed:
                        event_id = hashlib.sha256(json.dumps([new[k] for k in ('crab','origin','arrival_date')],ensure_ascii=False).encode()).hexdigest()
                        c.execute('INSERT OR IGNORE INTO concierge_arrivals(id,body) VALUES (?,?)', (event_id, json.dumps(new, ensure_ascii=False)))
                    queue_arrivals(c)
                elif action == 'answer':
                    body = data.get('body', '')
                    if not isinstance(body, str) or not 1 <= len(body.strip()) <= 2000:
                        raise ValueError('回答は1〜2000文字で入力してください')
                    row = c.execute('SELECT * FROM concierge_requests WHERE id=?', (data.get('id'),)).fetchone()
                    if not row or row['status'] != '未回答':
                        raise ValueError('未回答のご相談が見つかりません')
                    if row['user_id'].startswith('web:'):
                        import english_concierge
                        english_concierge.answer(c, row, body)
                        return {'ok': True}
                    if row['user_id'].startswith('booking:'):
                        raise ValueError('予約に登録された連絡先へ回答してください')
                    if row['user_id'].startswith('ig:') and len(body.strip())>950:
                        raise ValueError('Instagramへの回答は950文字以内で入力してください')
                    if setting(c, 'instagram_enabled' if row['user_id'].startswith('ig:') else 'enabled') != '1':
                        raise ValueError('配信が停止中です')
                    delivery_id = enqueue(c, row['user_id'], [text_message(body.strip())], kind='answer')
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
    command = {'予約': '空席案内', 'ご予約': '空席案内', '空席': '空席案内', '空席確認': '空席案内', 'コース・料金': 'ただいまのコース',
               '朝倉へ相談': 'VIP担当に相談', '写真': 'コース内容', '動画': 'コース内容', 'お品書き': 'コース内容',
               '蟹の時期': '年間スケジュール', '空席・ご予約': '空席案内', 'お料理・お品書き': 'コース内容'}.get(command, command)
    if command in MENU or command.startswith(('calendar:', 'visit:')):
        c.execute("UPDATE concierge_customers SET state='{}' WHERE user_id=?", (user,))
    if command == '空席案内' or command.startswith('calendar:'):
        return [concierge_menu.calendar_message(command[9:] if command.startswith('calendar:') else None)]
    if command.startswith('visit:'):
        try:
            day = date.fromisoformat(command[6:])
            today = datetime.now(JST).date()
            if not today <= day <= today + timedelta(days=365):
                raise ValueError()
        except ValueError:
            return [text_message('日付を確認できませんでした。\n\n「空席案内」から、もう一度ご希望日をお選びください。')]
        message = text_message(f'{day.year}年{day.month}月{day.day}日\n\nご来店人数をお選びください。\n\n1名様・9名様以上のご相談は「VIP担当に相談」へお願いいたします。', False)
        message['quickReply'] = {'items': [{'type':'action','action':{'type':'message','label':f'{n}名様','text':f'{day} {n}名'}} for n in range(2,9)] + [{'type':'action','action':{'type':'message','label':'VIP担当に相談','text':'VIP担当に相談'}}]}
        return [message]
    if command == 'ただいまのコース':
        return [text_message(concierge_menu.current_courses(courses))]
    if command == '年間スケジュール':
        return [text_message(concierge_menu.annual_courses(courses))]
    if command == 'コース内容':
        details = ['【コース内容】']
        if cat.get('crab'):
            details.append(cat['crab'])
        details.append(cat.get('menu') or '\n\n'.join(item['name'] + '\n' + item['description'] for item in concierge_menu.catalog()['courses'] if item['online_booking'] and item['description']))
        if cat.get('menu_url'):
            details.append(cat['menu_url'])
        if not cat.get('photo'):
            details.append('料理写真は準備中でございます。')
        if not (cat.get('video') and cat.get('preview')):
            details.append('料理動画は準備中でございます。')
        messages = [text_message('\n\n'.join(details))]
        if cat.get('photo'):
            messages.append({'type':'image','originalContentUrl':cat['photo'],'previewImageUrl':cat['photo']})
        if cat.get('video') and cat.get('preview'):
            messages.append({'type':'video','originalContentUrl':cat['video'],'previewImageUrl':cat['preview']})
        return messages
    if command in ('記念日・食事の相談','忘れ物'):
        c.execute("UPDATE concierge_customers SET state='request' WHERE user_id=?",(user,))
        return [text_message('ご来店日・お名前と、詳しい内容をお聞かせください。VIP担当へ確認のうえ、ご案内いたします。')]
    if command=='来店案内':
        return [text_message('18時と20時30分の二部制でございます。お時間に合わせてお越しくださいませ。個室は別邸（大阪市北区西天満3-8-7）で、本店とは別の建物です。ご予約確定メールの来店先をご確認ください。')]
    if any(k in command for k in ('道順','場所','住所','アクセス','何時','来店時間','お品書きの案内','記念日','花束','食事制限','アレルギー','タクシー','忘れ物')):
        if any(k in command for k in ('道順','場所','住所','アクセス')):
            return [text_message('個室は別邸（大阪市北区西天満3-8-7）にございます。本店とは別の建物ですので、ご予約確定メールの来店先をご確認ください。ご不明でしたら「VIP担当に相談」よりご予約日をお知らせください。')]
        if '来店時間' in command or '何時' in command:
            return [text_message('18時と20時30分の二部制で、一斉にお料理をご提供しております。ご予約のお時間に合わせてお越しくださいませ。')]
        if 'お品書き' in command:
            return respond(c, user, 'コース内容', lookup, courses, base_url)
        else:
            c.execute("UPDATE concierge_customers SET state='request' WHERE user_id=?",(user,))
    if command.startswith('お客様連携 '):
        return [text_message(link_customer(c, user, command))]
    if command == '配信停止':
        c.execute('UPDATE concierge_customers SET subscribed=0,stopped=1 WHERE user_id=?', (user,))
        c.execute("UPDATE concierge_outbox SET state='cancelled' WHERE user_id=? AND kind='announcement' AND state='pending'", (user,))
        return [text_message('蟹の変更・入荷案内の配信を停止しました。空席確認・ご予約は引き続きご利用いただけます。')]
    if command in ('配信再開', '入荷案内を受け取る'):
        c.execute('UPDATE concierge_customers SET subscribed=1,stopped=0 WHERE user_id=?', (user,))
        return [text_message('蟹の変更・入荷案内をお届けします。「配信停止」でいつでも停止できます。')]
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
            return [text_message('現在、空席を確認できません。少し後にお試しいただくか、「VIP担当に相談」からお問い合わせください。')]
        if not slots:
            return [text_message(f'{day}・{party}名様は、満席または受付期間外です。別の日付をお送りいただくか、VIP担当へご相談ください。')]
        actions = []
        for slot in slots[:4]:
            token = secrets.token_urlsafe(32)
            proposal = {**slot, 'date':str(day), 'party_size':party}
            c.execute('INSERT INTO concierge_proposals(hash,user_id,slot,expires) VALUES (?,?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), user, json.dumps(proposal), time.time()+3600))
            actions.append({'type': 'uri', 'label': slot['time'] + (' 個室' if slot['area'] == 'PRIVATE' else ' カウンター'), 'uri': base_url + '/concierge/book#' + token})
        personal = preference_message(c, user, cat)
        if personal:
            c.execute('UPDATE concierge_customers SET state=? WHERE user_id=?', ('preference:'+str(day)+' '+str(party)+'名様\n'+personal, user))
        return [text_message(f'{day}・{party}名様のお席をご案内できます。お料理代は合計 ¥{courses[slots[0]["course"]][1]*party:,}（税込）でございます。\nLINEコンシェルジュからは前受けなしでご予約いただけます。以下より内容をご確認のうえ、お申し込みください。' + ('\n\n'+personal if personal else '')),
                {'type': 'template', 'altText': 'お席をお選びください', 'template': {'type': 'buttons', 'text': 'ご希望のお席・お時間をお選びください', 'actions': actions}}]
    if command == 'VIP担当に相談':
        c.execute("UPDATE concierge_customers SET state='request' WHERE user_id=?", (user,))
        return [text_message('ご希望日時・お名前・ご要望をお送りください。VIP担当へ取り次ぎます。アレルギー等の対応可否はVIP担当からの回答をお待ちください。')]
    state = c.execute('SELECT state FROM concierge_customers WHERE user_id=?', (user,)).fetchone()[0]
    if state.startswith('preference:') and command not in ('メニュー', 'キャンセル'):
        command = 'ご案内内容：'+state.removeprefix('preference:')+'\nお客様のご返答：'+command
        state = 'request'
    if state == 'request' and command not in ('メニュー', 'キャンセル'):
        if len(command) > 2000:
            return [text_message('ご相談は2000文字以内でお送りください。')]
        request_id = uuid.uuid4().hex[:12]
        c.execute('INSERT INTO concierge_requests(id,user_id,body,created) VALUES (?,?,?,?)', (request_id, user, command, time.time()))
        c.execute("UPDATE concierge_customers SET state='{}' WHERE user_id=?", (user,))
        owner = setting(c, 'owner')
        if owner:
            enqueue(c, owner, [text_message('【VIPからのご相談】受付 ' + request_id + '\n' + (profile(c, user).get('name', '未連携のお客様') + '様\n') + command + '\n回答はこちら：' + base_url + '/concierge', False)], channel='owner', kind='request')
        return [text_message('ご相談を受け付けました（受付番号 ' + request_id + '）。VIP担当の確認・回答をお待ちください。この時点では予約・特別対応は確定していません。')]
    c.execute("UPDATE concierge_customers SET state='{}' WHERE user_id=?", (user,))
    return [text_message('西天満つきやのコンシェルジュでございます。\n\nお席のご相談や、お料理のご案内を承ります。\n\nご希望の項目を、下の「ご案内メニュー」からお選びください ↓')]


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
        command = concierge_menu.event_command(e)
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
                enabled = setting(c, 'enabled') == '1'
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
                    if e.get('type') != 'follow' and not (e.get('type') == 'message' and msg.get('type') == 'text' and isinstance(msg.get('text'), str)) and not (e.get('type') == 'postback' and concierge_menu.event_command(e)):
                        continue
                    c.execute('INSERT INTO concierge_customers(user_id) VALUES (?) ON CONFLICT(user_id) DO UPDATE SET active=1', (user,))
                    command = concierge_menu.event_command(e)
                    if enabled:
                        enqueue(c, user, respond(c, user, command, cached_lookup, courses, base_url))
                    elif command == '配信停止':
                        c.execute('UPDATE concierge_customers SET stopped=1,subscribed=0 WHERE user_id=?', (user,))
        finally:
            c.close()
    return 200, {'ok': True}


def deliver(db):
    # A separate sender lock keeps webhook ingestion responsive during network calls.
    # Stable retry keys survive restarts; recheck cancellation before each push.
    with DELIVERY_LOCK:
        c = connect(db)
        try:
            with c:
                queue_arrivals(c)
            rows = c.execute("SELECT * FROM concierge_outbox WHERE state='pending' AND next_try<=? ORDER BY created LIMIT 10", (time.time(),)).fetchall()
            for row in rows:
                current = c.execute('SELECT state FROM concierge_outbox WHERE id=?', (row['id'],)).fetchone()
                if not current or current['state'] != 'pending':
                    continue
                if row['kind'] == 'guest-service':
                    import guest_service
                    if not guest_service.ready_delivery(c,row['id']):continue
                    if not guest_service.validate_delivery(c,row['id']):
                        with c:c.execute("UPDATE concierge_outbox SET state='cancelled' WHERE id=?",(row['id'],))
                        continue
                if time.time() - row['created'] > 23*3600:
                    with c:
                        c.execute("UPDATE concierge_outbox SET state='failed',error='retry window expired' WHERE id=?", (row['id'],))
                        c.execute("UPDATE concierge_requests SET status='回答送信失敗' WHERE delivery_id=?", (row['id'],))
                    continue
                if row['channel'] == 'instagram':
                    import instagram_concierge
                    instagram_concierge.deliver_row(c, row)
                    continue
                if row['channel'] == 'customer' and setting(c, 'enabled') != '1':
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
            return {'enabled': setting(c, 'enabled') == '1', 'instagram_enabled': setting(c, 'instagram_enabled')=='1', 'instagram_verified':setting(c,'instagram_verified'), 'instagram_configured': all(os.getenv(k) for k in ('INSTAGRAM_APP_SECRET','INSTAGRAM_VERIFY_TOKEN','INSTAGRAM_ACCESS_TOKEN','INSTAGRAM_ACCOUNT_ID','INSTAGRAM_GRAPH_VERSION')), 'verified_at': setting(c, 'verified'), 'owner_linked': bool(setting(c, 'owner')),
                    'secret_configured': bool(os.getenv('CONCIERGE_LINE_CHANNEL_SECRET')), 'token_configured': bool(os.getenv('CONCIERGE_LINE_CHANNEL_ACCESS_TOKEN')),
                    'owner_token_configured': bool(os.getenv('LINE_CHANNEL_ACCESS_TOKEN')), 'catalog': catalog(c),
                    'subscribers': c.execute("SELECT count(*) FROM concierge_customers WHERE active=1 AND stopped=0 AND user_id NOT LIKE 'ig:%'").fetchone()[0], 'arrivals_pending': c.execute("SELECT count(*) FROM concierge_arrivals WHERE state='pending'").fetchone()[0],
                    'requests': [{**dict(r), 'customer_name': profile(c, r['user_id']).get('name', '')} for r in c.execute('SELECT id,user_id,body,created,status FROM concierge_requests ORDER BY created DESC LIMIT 100')],
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


def elegant(text):
    return re.sub(r'[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]', '', text)


def link_customer(c, user, command):
    code = command.removeprefix('お客様連携 ').strip()
    hashed = hashlib.sha256(code.encode()).hexdigest()
    row = c.execute('SELECT * FROM concierge_links WHERE hash=? AND used=0 AND expires>?', (hashed, time.time())).fetchone()
    if not row:
        return '連携情報を確認できませんでした。恐れ入りますが、店舗へお問い合わせください。'
    c.execute('UPDATE concierge_links SET used=1 WHERE hash=?', (hashed,))
    c.execute('UPDATE concierge_customers SET customer_id=? WHERE user_id=?', (row['customer_id'], user))
    return 'お客様情報との連携が完了いたしました。今後は、お好みに合わせてご案内いたします。'


def profile(c, user):
    linked = c.execute('SELECT customer_id FROM concierge_customers WHERE user_id=?', (user,)).fetchone()
    if not linked or not linked[0]:
        return {}
    row = c.execute('SELECT * FROM customers WHERE id=?', (linked[0],)).fetchone()
    return dict(row) if row else {}


def preference_message(c, user, cat):
    p = profile(c, user)
    parts = []
    if p.get('preferred_seat') and '個室' in p['preferred_seat']:
        parts.append('このたびも、いつもの個室をご希望でしょうか。空き状況を確認のうえ、ご用意いたします。')
    elif p.get('id'):
        visits = c.execute("SELECT seating_area FROM reservations WHERE customer_id=? AND status='CONFIRMED' AND visit_at<? ORDER BY visit_at DESC LIMIT 3", (p['id'], datetime.now(JST).strftime('%Y-%m-%dT%H:%M'))).fetchall()
        if len(visits)>=2 and all((r[0] or '').startswith('PRIVATE') for r in visits):
            parts.append('お席は、いつもの個室でよろしいでしょうか。空き状況を確認いたします。')
    if 'タクシー' in p.get('return_transport', ''):
        parts.append('お帰りのタクシーをお手配いたしましょうか。ご希望のお時間がございましたらお申し付けください。')
    if '日本酒' in p.get('preferred_drinks', '') and cat.get('sake'):
        parts.append('日本酒がお好きなお客様に、今回新たにご用意した「'+cat['sake']+'」もご紹介できればと存じます。')
    return '\n'.join(parts)


def announcement(c, user, cat):
    p = profile(c, user)
    greeting = p.get('name', '') + '様\n\n' if p.get('name') else ''
    d = date.fromisoformat(cat['arrival_date'])
    arrived = d <= datetime.now(JST).date()
    favorite = p.get('preferred_crab', '')
    matched = favorite and (favorite in cat['crab'] or ('ずわい' in favorite and any(k in cat['crab'] for k in ('ずわい', 'ズワイ', '松葉'))))
    lead = 'お待たせいたしました。' if matched else 'いつも西天満つきやをご愛顧いただき、誠にありがとうございます。\n'
    origin = cat['origin'].removesuffix('産')
    arrival = f"{origin}産{cat['crab']}が{d.month}月{d.day}日より" + ('入荷しております。' if arrived else '入荷いたします。')
    return greeting + lead + arrival + '\nVIPのお客様に先行して、ご予約の受付を開始いたします。ご希望の日程・人数をお知らせいただけましたら、お席をご案内いたします。' + ('\n\n'+preference_message(c,user,cat) if p else '')


def queue_arrivals(c):
    if setting(c, 'enabled') != '1':
        return
    for event in c.execute("SELECT * FROM concierge_arrivals WHERE state='pending'").fetchall():
        cat = json.loads(event['body'])
        for row in c.execute("SELECT user_id FROM concierge_customers WHERE active=1 AND stopped=0 AND user_id NOT LIKE 'ig:%'").fetchall():
            enqueue(c, row[0], [text_message(announcement(c,row[0],cat))], kind='announcement')
        c.execute("UPDATE concierge_arrivals SET state='queued' WHERE id=?", (event['id'],))


