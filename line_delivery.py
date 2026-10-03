"""Daily LINE briefing: explicit destination, durable retry key, expiring images."""
import hashlib
import hmac
import json
import os
import threading
import time
import uuid
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path
import line_bot

JST = timezone(timedelta(hours=9))
LOCK = threading.Lock()
# Group observed in the owner's authenticated connection-status screenshot.
TARGET = 'Cc95ce7808ee54a2a3d19a4a7b7d78818'

def connect(db):
    c = line_bot.connect(db)
    c.execute('CREATE TABLE IF NOT EXISTS line_deliveries(day TEXT PRIMARY KEY, retry_key TEXT NOT NULL, payload TEXT, state TEXT NOT NULL, error TEXT, updated REAL NOT NULL)')
    c.commit()
    return c

def api(path, body=None, retry_key=None):
    token = os.getenv('LINE_CHANNEL_ACCESS_TOKEN', '').strip()
    if not token:
        raise RuntimeError('LINE_CHANNEL_ACCESS_TOKEN が未設定です')
    headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
    if retry_key:
        headers['X-Line-Retry-Key'] = retry_key
    req = urllib.request.Request('https://api.line.me/v2/bot/' + path,
        data=json.dumps(body).encode() if body is not None else None, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b'{}')
    except urllib.error.HTTPError as e:
        if e.code == 409 and e.headers.get('x-line-accepted-request-id'):
            return {'accepted': True}
        raise RuntimeError('LINE API HTTP ' + str(e.code)) from None

def image_signature(name, expiry):
    key = os.getenv('LINE_CHANNEL_SECRET', '').strip()
    if not key:
        raise RuntimeError('LINE_CHANNEL_SECRET が未設定です')
    return hmac.new(key.encode(), (name + ':' + str(expiry)).encode(), hashlib.sha256).hexdigest()

def image_path(db, name, expiry, signature):
    if name not in ('original.jpg', 'preview.jpg') and not __import__('re').fullmatch(r'[0-9a-f]{32}-(original|preview)\.jpg', name):
        return None
    try:
        expiry = int(expiry)
        if expiry < time.time() or expiry > time.time() + 49 * 3600:
            return None
        if not hmac.compare_digest(image_signature(name, expiry), signature):
            return None
    except (ValueError, RuntimeError):
        return None
    path = Path(db).parent / 'line-images' / name
    return path if path.is_file() else None

def capture(db, day, port, admin, base_url):
    os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', '0')
    from playwright.sync_api import sync_playwright
    from PIL import Image
    folder = Path(db).parent / 'line-images'
    folder.mkdir(exist_ok=True)
    for old in folder.glob('*.jpg'):
        if old.stat().st_mtime < time.time() - 49 * 3600:
            old.unlink(missing_ok=True)
    stem = uuid.uuid4().hex
    original = folder / (stem + '-original.jpg')
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--disable-dev-shm-usage'])
        try:
            context = browser.new_context(viewport={'width':1440, 'height':1000})
            expiry = str(int(time.time()) + 120)
            sig = hmac.new(admin.encode(), expiry.encode(), hashlib.sha256).hexdigest()
            context.add_cookies([{'name':'tsukiya_session','value':expiry+'.'+sig,'url':f'http://127.0.0.1:{port}/','httpOnly':True,'sameSite':'Strict'}])
            page = context.new_page()
            page.goto(f'http://127.0.0.1:{port}/reservations?snapshot=1&date={day}', wait_until='networkidle', timeout=60000)
            page.wait_for_selector('body[data-snapshot-ready="true"]', timeout=60000)
            page.screenshot(path=str(original), full_page=True, type='jpeg', quality=85)
        finally:
            browser.close()
    preview = folder / (stem + '-preview.jpg')
    with Image.open(original) as img:
        img.thumbnail((1000,1000))
        img.convert('RGB').save(preview, quality=75)
    if original.stat().st_size > 10_000_000 or preview.stat().st_size > 1_000_000:
        raise RuntimeError('予約表の画像サイズが上限を超えています')
    expiry = int(time.time()) + 48 * 3600
    def url(path):
        return f'{base_url}/line-image/{path.name}?expires={expiry}&sig={image_signature(path.name, expiry)}'
    return {'type':'image','originalContentUrl':url(original),'previewImageUrl':url(preview)}

def message(db, day):
    c = connect(db)
    try:
        rows = c.execute("SELECT guest_name,celebration_items,visit_at,status FROM reservations WHERE substr(visit_at,1,10)=? AND status!='CANCELLED' AND COALESCE(celebration_items,'')!='' ORDER BY visit_at,id", (day,)).fetchall()
        text = 'おはようございます、本日のご予約状況をお伝え致します。\n' + day
        for r in rows:
            text += '\n・' + r['guest_name'] + '様：' + r['celebration_items'] + '（' + r['status'] + '）'
        if len(text.encode('utf-16-le')) // 2 > 4900:
            raise RuntimeError('リクエストが多いため予約表を確認してください')
        return {'type':'text','text':text}
    finally:
        c.close()

def deliver(db, port, admin, base_url, day=None):
    day = day or datetime.now(JST).date().isoformat()
    if not base_url.startswith('https://') or not admin:
        raise RuntimeError('公開URLまたは管理認証が未設定です')
    with LOCK:
        c = connect(db)
        try:
            target = c.execute("SELECT 1 FROM line_sources WHERE source_id=? AND source_type='group' AND active=1", (TARGET,)).fetchone()
            if not target:
                raise RuntimeError('通知先グループの接続が確認できません')
            c.execute('INSERT OR IGNORE INTO line_deliveries VALUES(?,?,NULL,\'NEW\',NULL,?)', (day,str(uuid.uuid4()),time.time()))
            c.commit()
            row = c.execute('SELECT * FROM line_deliveries WHERE day=?', (day,)).fetchone()
            if row['state'] == 'SENT':
                return {'ok':True,'already_sent':True}
            if row['payload'] and time.time() - row['updated'] > 23 * 3600:
                raise RuntimeError('再送期限を超えています。送信履歴の確認が必要です')
            try:
                if row['payload']:
                    payload = json.loads(row['payload'])
                else:
                    api('info')  # Validate token before generating the screenshot.
                    payload = {'to':TARGET,'messages':[message(db,day),capture(db,day,port,admin,base_url)]}
                    c.execute("UPDATE line_deliveries SET payload=?,state='READY',updated=? WHERE day=?", (json.dumps(payload),time.time(),day))
                    c.commit()
                api('message/push',payload,row['retry_key'])
                c.execute("UPDATE line_deliveries SET state='SENT',error=NULL WHERE day=?", (day,))
                c.commit()
                return {'ok':True}
            except Exception as e:
                # Never include API response bodies, tokens, URLs or customer data in errors.
                error = str(e) if isinstance(e, RuntimeError) else type(e).__name__
                c.execute("UPDATE line_deliveries SET state='ERROR',error=? WHERE day=?", (error[:200],day))
                c.commit()
                raise RuntimeError(error) from None
        finally:
            c.close()

def status(db):
    c = connect(db)
    try:
        rows = [dict(r) for r in c.execute('SELECT day,state,error FROM line_deliveries ORDER BY day DESC LIMIT 7')]
        enabled = c.execute("SELECT value FROM line_connection_state WHERE key='daily_enabled'").fetchone()
        return {'delivery_enabled':bool(enabled and enabled['value']=='1'),'delivery_time':'09:00 Asia/Tokyo','deliveries':rows}
    finally:
        c.close()

def enable(db, enabled):
    c = connect(db)
    with c:
        c.execute("INSERT OR REPLACE INTO line_connection_state VALUES('daily_enabled',?)", ('1' if enabled else '0',))
    c.close()

def loop(db, port, admin, base_url):
    while True:
        try:
            now = datetime.now(JST)
            if now.hour == 9 and status(db)['delivery_enabled']:
                deliver(db,port,admin,base_url)
        except Exception:
            pass  # Recorded in authenticated delivery status; retry with the same key.
        time.sleep(60)
