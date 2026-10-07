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

def briefing_html(db, day, font_data, session=None):
    """Portrait report; keep personal contact details out of the group image."""
    from html import escape
    c = connect(db)
    try:
        rows = [dict(r) for r in c.execute("SELECT guest_name,party_size,visit_at,seating_area,status,celebration_items,plate_message,guest_note FROM reservations WHERE substr(visit_at,1,10)=? AND status!='CANCELLED' ORDER BY visit_at,seating_area,id", (day,))]
    finally:
        c.close()
    if session is not None:
        rows = [r for r in rows if ('18:00' if r['visit_at'][11:16] < '20:30' else '20:30') == session]
    labels = {'COUNTER':'本店・カウンター','PRIVATE1':'別邸・個室①','PRIVATE2':'別邸・個室②','PRIVATE3':'別邸・個室③'}
    statuses = {'CONFIRMED':'予約確定','INVOICED':'請求済み','PENDING':'未決済','ERROR':'要確認'}
    cards = []
    for row in rows:
        time_label = escape(row['visit_at'][11:16])
        seat = escape(labels.get(row['seating_area'], row['seating_area'] or '席未設定'))
        status = escape(statuses.get(row['status'],row['status']))
        request = escape(row['celebration_items'] or '')
        plate = escape(row['plate_message'] or '')
        note = escape(row['guest_note'] or '')
        details = []
        if request:
            details.append('<div>'+request+'</div>')
        if plate:
            details.append('<div>'+plate+'</div>')
        if note:
            details.append('<div>'+note+'</div>')
        extra = '<aside>'+''.join(details)+'</aside>' if details else ''
        cards.append(f'<article><section><div class="meta">{time_label}　{seat}</div><div class="guest">{escape(row["guest_name"])} 様 <b>{row["party_size"]}名</b></div><div class="status">{status}</div></section>{extra}</article>')
    content = ''.join(cards) or '<article class="empty">本日の登録予約はありません</article>'
    total = sum(r['party_size'] for r in rows)
    return f'''<!doctype html><html lang="ja"><meta charset="utf-8"><style>
    @font-face{{font-family:TsukiyaJP;src:url(data:font/otf;base64,{font_data}) format("opentype");font-display:block}}
    *{{box-sizing:border-box}}body{{margin:0;background:#f5f2ec;color:#102b45;font-family:TsukiyaJP,sans-serif}}
    main{{width:720px;padding:32px}}h1{{font-size:34px;margin:0 0 12px}}header{{border-bottom:3px solid #b69b65;padding-bottom:22px;margin-bottom:24px}}
    .date{{font-size:30px}}.total{{font-size:25px;margin-top:12px}}article{{display:flex;gap:20px;align-items:flex-start;background:white;border:1px solid #d9d4c9;border-radius:16px;padding:24px;margin:16px 0;break-inside:avoid}}
    .meta{{font-size:23px}}section{{flex:1;min-width:0}}.guest{{font-size:29px;margin:12px 0;overflow-wrap:anywhere}}b{{white-space:nowrap}}.status{{font-size:23px;color:#496451}}aside{{width:42%;flex-shrink:0;font-size:22px;background:#fff3cd;padding:14px;border-radius:10px;overflow-wrap:anywhere;white-space:pre-wrap}}aside div+div{{margin-top:12px}}.empty{{font-size:28px}}footer{{font-size:20px;color:#647080;margin-top:24px}}
    </style><main><header><h1>西天満 つきや｜本日のご予約</h1><div class="date">{escape(day)}　{escape(session or '')}</div><div class="total">{len(rows)}組・{total}名（未決済を含む）</div></header>{content}<footer>送信時点の予約情報です。変更は予約管理画面をご確認ください。</footer></main></html>'''


def capture(db, day, port, admin, base_url, session=None):
    # Render directly with Pillow: no Chromium process or base64 font copies.
    from PIL import Image, ImageDraw, ImageFont
    folder = Path(db).parent / 'line-images'
    folder.mkdir(exist_ok=True)
    for old in folder.glob('*.jpg'):
        if old.stat().st_mtime < time.time() - 49 * 3600:
            old.unlink(missing_ok=True)
    stem = uuid.uuid4().hex
    original = folder / (stem + '-original.jpg')
    font = folder / 'NotoSerifCJKjp-Regular.otf'
    if not font.exists():
        req = urllib.request.Request('https://raw.githubusercontent.com/notofonts/noto-cjk/main/Serif/OTF/Japanese/NotoSerifCJKjp-Regular.otf')
        temp = font.with_suffix('.tmp')
        try:
            with urllib.request.urlopen(req, timeout=60) as response, temp.open('wb') as out:
                size = 0
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > 35_000_000:
                        raise RuntimeError('日本語フォントのサイズが上限を超えています')
                    out.write(chunk)
            with temp.open('rb') as saved:
                valid = saved.read(4) == b'OTTO'
            if not valid or size < 1_000_000:
                raise RuntimeError('日本語フォントを取得できませんでした')
            temp.replace(font)
        finally:
            temp.unlink(missing_ok=True)
    c = connect(db)
    try:
        rows = [dict(r) for r in c.execute("SELECT guest_name,party_size,visit_at,seating_area,status,celebration_items,plate_message,guest_note FROM reservations WHERE substr(visit_at,1,10)=? AND status!='CANCELLED' ORDER BY visit_at,seating_area,id", (day,))]
    finally:
        c.close()
    if session is not None:
        rows = [r for r in rows if ('18:00' if r['visit_at'][11:16] < '20:30' else '20:30') == session]
    labels = {'COUNTER':'本店・カウンター','PRIVATE1':'別邸・個室①','PRIVATE2':'別邸・個室②','PRIVATE3':'別邸・個室③'}
    statuses = {'CONFIRMED':'予約確定','INVOICED':'請求済み','PENDING':'未決済','ERROR':'要確認'}
    fonts = {size: ImageFont.truetype(str(font), size) for size in (22,23,25,29,30,34)}
    def wrap(value, size, width):
        lines = []
        for paragraph in str(value or '').split('\n'):
            line = ''
            for ch in paragraph:
                if line and fonts[size].getlength(line + ch) > width:
                    lines.append(line)
                    line = ''
                line += ch
            lines.append(line)
        return lines
    # Layout before allocating the image; cap pixels even for oversized notes.
    cards = []
    y = 205
    for row in rows:
        details = '\n'.join(str(row[k]).strip() for k in ('celebration_items','plate_message','guest_note') if row[k] and str(row[k]).strip())
        left_width = 330 if details else 600
        left = [(wrap(row['visit_at'][11:16] + '　' + labels.get(row['seating_area'], row['seating_area'] or '席未設定'),23,left_width),23),
                (wrap(str(row['guest_name']) + ' 様　' + str(row['party_size']) + '名',29,left_width),29),
                (wrap(statuses.get(row['status'],row['status']),23,left_width),23)]
        right = wrap(details,22,210) if details else []
        height = max(sum(len(lines)*(size+12)+10 for lines,size in left),len(right)*34+28) + 40
        if y + height > 9500:
            raise RuntimeError('予約表が長すぎます。備考の長さを確認してください')
        cards.append((y,height,left,right))
        y += height + 16
    if not cards:
        y += 100
    with Image.new('RGB',(720,y+110),'#f5f2ec') as canvas:
        draw = ImageDraw.Draw(canvas)
        ink = '#102b45'
        draw.text((32,25),'西天満 つきや｜本日のご予約',font=fonts[34],fill=ink)
        draw.text((32,85),day + '　' + (session or ''),font=fonts[30],fill=ink)
        draw.text((32,137),str(len(rows))+'組・'+str(sum(r['party_size'] for r in rows))+'名（未決済を含む）',font=fonts[25],fill=ink)
        draw.line((32,190,688,190),fill='#b69b65',width=3)
        for top,height,left,right in cards:
            draw.rounded_rectangle((32,top,688,top+height),radius=16,fill='white',outline='#d9d4c9')
            ty = top+20
            for lines,size in left:
                for line in lines:
                    draw.text((54,ty),line,font=fonts[size],fill=ink)
                    ty += size+12
                ty += 10
            if right:
                draw.rounded_rectangle((432,top+20,668,top+height-20),radius=10,fill='#fff3cd')
                for i,line in enumerate(right):
                    draw.text((446,top+28+i*34),line,font=fonts[22],fill=ink)
        if not cards:
            draw.text((45,225),'本日の登録予約はありません',font=fonts[29],fill=ink)
        draw.text((32,y+15),'送信時点の予約情報です。',font=fonts[22],fill='#647080')
        draw.text((32,y+49),'変更は予約管理画面をご確認ください。',font=fonts[22],fill='#647080')
        canvas.save(original,quality=95)
    preview = folder / (stem + '-preview.jpg')
    with Image.open(original) as img:
        img.thumbnail((1440,2400))
        img.convert('RGB').save(preview, quality=85)
    if original.stat().st_size > 10_000_000 or preview.stat().st_size > 1_000_000:
        raise RuntimeError('予約表の画像サイズが上限を超えています')
    expiry = int(time.time()) + 48 * 3600
    def url(path):
        return f'{base_url}/line-image/{path.name}?expires={expiry}&sig={image_signature(path.name, expiry)}'
    return {'type':'image','originalContentUrl':url(original),'previewImageUrl':url(preview)}

def message(db, day):
    c = connect(db)
    try:
        rows = c.execute("SELECT guest_name,celebration_items,plate_message,guest_note,visit_at FROM reservations WHERE substr(visit_at,1,10)=? AND status!='CANCELLED' ORDER BY visit_at,id", (day,)).fetchall()
        date = datetime.strptime(day, '%Y-%m-%d')
        date_label = f'{date.year}年{date.month}月{date.day}日（{"月火水木金土日"[date.weekday()]}）'
        lines = ['おはようございます。', date_label, '本日のご予約状況をお伝え致します。']
        requests = []
        for r in rows:
            details = [str(r[key] or '').strip() for key in ('celebration_items', 'plate_message', 'guest_note')]
            details = [value for value in details if value]
            if details:
                requests.append('・' + r['visit_at'][11:16] + ' ' + r['guest_name'] + '様：' + '／'.join(details))
        lines.extend(requests or ['本日特別なリクエストはありません。'])
        text = '\n'.join(lines)
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
                    payload = {'to':TARGET,'messages':[message(db,day)] + [capture(db,day,port,admin,base_url,session) for session in ('18:00','20:30')]}
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
