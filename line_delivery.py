"""Daily LINE briefing: explicit destination, durable retry key, expiring images."""
import hashlib
import hmac
import json
import os
import re
import threading
import time
import uuid
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path
import line_bot
import bank_transfer

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
    if name not in ('original.jpg', 'preview.jpg') and not __import__('re').fullmatch(r'(?:[0-9a-f]{32}-(?:original|preview)\.jpg|[0-9a-f]{32}-doorman\.pdf)', name):
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
        rows = [dict(r) for r in c.execute("SELECT guest_name,party_size,visit_at,seating_area,status,celebration_items,plate_message,guest_note,vip_through FROM reservations WHERE substr(visit_at,1,10)=? AND status!='CANCELLED' ORDER BY seating_area,visit_at,id", (day,))]
    finally:
        c.close()
    if session is not None:
        rows = [r for r in rows if r.get('vip_through') or ('18:00' if r['visit_at'][11:16] < '20:30' else '20:30') == session]
    labels = {'COUNTER':'本店・カウンター','PRIVATE1':'別邸・個室①','PRIVATE2':'別邸・個室②','PRIVATE3':'別邸・個室③'}
    statuses = {'CONFIRMED':'予約確定','INVOICED':'請求済み','PENDING':'未決済','ERROR':'要確認'}
    cards = []
    for row in rows:
        time_label = escape(row['visit_at'][11:16]+('〜23:00 通し' if row.get('vip_through') else ''))
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
        rows = [dict(r) for r in c.execute("SELECT guest_name,party_size,visit_at,seating_area,status,celebration_items,plate_message,guest_note,vip_through FROM reservations WHERE substr(visit_at,1,10)=? AND status!='CANCELLED' ORDER BY seating_area,visit_at,id", (day,))]
    finally:
        c.close()
    if session is not None:
        rows = [r for r in rows if r.get('vip_through') or ('18:00' if r['visit_at'][11:16] < '20:30' else '20:30') == session]
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
        details = '\n'.join('＊' + note for note in briefing_requests(row)[1])
        left_width = 330 if details else 600
        left = [(wrap(row['visit_at'][11:16] + ('〜23:00 通し' if row.get('vip_through') else '') + '　' + labels.get(row['seating_area'], row['seating_area'] or '席未設定'),23,left_width),23),
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

def briefing_requests(row):
    """Keep service requests, excluding import provenance and amendment history."""
    raw = str(row['guest_note'] or '')
    extra = str(row['celebration_items'] or '')
    text = extra + '。' + raw
    requested = []
    for item in ('花束', 'ケーキ'):
        if item in extra or re.search(item + r'(?:とケーキ)?の(?:ご)?(?:要望|希望|リクエスト)(?:あり|有)', text):
            requested.append(item)
    notes = []
    if requested:
        notes.append('と'.join(requested) + 'のご要望あり。')
    for part in re.split(r'[。\n]+', raw):
        part = re.sub(r'^【朝倉追記】\s*', '', part).strip()
        if not part or re.search(r'カレンダー|画像|に基づ|本人同定|紐付|連絡先|旧予約|開始時刻変更|→|事前決済|当日精算|未指定|確認|^\d{1,2}/\d{1,2}|^\d{1,2}月\d{1,2}日', part):
            continue
        if re.search(r'(花束|ケーキ).*の(?:ご)?(?:要望|希望|リクエスト)(あり|有)', part):
            continue
        if '記念日利用' in part and not requested:
            part = '記念日利用(リクエストなし)'
        if part not in notes:
            notes.append(part)
    if extra and not requested:
        notes.insert(0, extra)
    if row['plate_message']:
        notes.append('プレート：' + str(row['plate_message']).strip())
    return requested, notes



def new_booking_summary(c, day):
    """Count registrations since the previous daily briefing snapshot (JST)."""
    end = datetime.now(JST)
    previous = c.execute(
        "SELECT updated FROM line_deliveries WHERE state='SENT' AND length(day)=10 AND day<? ORDER BY day DESC LIMIT 1",
        (day,)).fetchone()
    start = (datetime.fromtimestamp(previous['updated'], JST) if previous else
             datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=JST, hour=9) - timedelta(days=1))
    count = c.execute("""SELECT COUNT(*) FROM reservations
        WHERE julianday(created_at)>julianday(?) AND julianday(created_at)<=julianday(?)
        AND status!='CANCELLED'
        AND COALESCE(course_name,'') NOT LIKE '%テスト%'
        AND COALESCE(guest_name,'') NOT LIKE '%テスト%'""",
        (start.isoformat(), end.isoformat())).fetchone()[0]
    return (f'新規予約{count}件ございます、ご確認お願いします' if count else
            '新規ご予約はありません。')

def message(db, day, base_url=None):
    c = connect(db)
    try:
        rows = c.execute("SELECT * FROM reservations WHERE substr(visit_at,1,10)=? AND status!='CANCELLED' ORDER BY visit_at,id", (day,)).fetchall()
        date = datetime.strptime(day, '%Y-%m-%d')
        date_label = f'{date.year}年{date.month}月{date.day}日（{"月火水木金土日"[date.weekday()]}）'
        lines = ['おはようございます。', date_label, '本日のご予約状況をお伝え致します。', '',
                 f'合計{len(rows)}組・{sum(r["party_size"] for r in rows)}名']
        lines.extend(['', new_booking_summary(c, day)])
        details = [briefing_requests(r) for r in rows]
        items = [item for item in ('花束', 'ケーキ') if any(item in requested for requested, _ in details)]
        if items:
            lines.append('＊' + 'と'.join(items) + 'のリクエストあり。')
        for r, (_, notes) in zip(rows, details):
            seat = '個室' if str(r['seating_area'] or '').startswith('PRIVATE') else {'COUNTER':'カウンター'}.get(r['seating_area'], '席未設定')
            name = str(r['guest_name']).removesuffix('様').strip()
            lines.extend(['', f'・{r["visit_at"][11:16]} {name}様　{r["party_size"]}名、{seat}'])
            if r['payment_confirmed_at']:
                payment = '事前決済済み'
            elif r['source'] in ('DIRECT', 'CONCIERGE_LINE'):
                payment = '事前決済なし'
            else:
                payment = '事前決済未完了'
            lines.append('＊' + payment)
            lines.extend('＊' + note for note in notes)
        if base_url:
            import supplies
            request = supplies.briefing_request(db, day, base_url)
            if request:
                lines.extend(['', request])
        if day == '2026-10-21' and base_url:
            import supplies
            access = supplies.connect(db)
            try:
                with access:
                    supply_link = supplies.new_link(access, base_url, page='supplies')
                    drink_link = supplies.new_link(access, base_url, page='drinks')
                    # Owner-requested pinned inventory links: valid until revoked
                    # by removing their hashes; regular links still expire in 8 days.
                    for link in (supply_link, drink_link):
                        token = link.split('#access=', 1)[1]
                        access.execute('UPDATE supply_access SET expires=? WHERE hash=?',
                                       (253402300799, hashlib.sha256(token.encode()).hexdigest()))
            finally:
                access.close()
            capacity_link = drink_link.replace('/drinks#', '/drink-capacity#')
            lines.extend(['', '【やまもとさんへの依頼事項①｜シフトLINE連携】',
                'シフトLINE連携を完了させてください。前回の連携画面は有効期限が切れているため、本日出勤の際に、新しく発行したQRコードを読み取り、登録を完了してください。',
                '', '【やまもとさんへの依頼事項②｜在庫表のアナウンス固定】',
                '備品在庫表・酒類在庫表をいつでも開けるよう、このメッセージを長押しし「アナウンス」でグループ上部に固定してください。今後は固定したリンクから直接開いてご利用ください。利用のたびに「備品」「酒類」と投稿する必要はありません。',
                '', '備品在庫表：', supply_link,
                '本店・別邸を選び、在庫を入力・確認してください。',
                '', '酒類在庫表：', drink_link,
                '本店・別邸を選び、未開封本数・開封残を入力して「朝倉へ在庫送信」を押してください。',
                '上記は継続利用できる固定用リンクです。経理グループ内でご利用ください。',
                '', '【酒類・飲料の容量確認のお願い】',
                '本店・別邸の各商品の容器容量と1杯あたりの提供量を、下の入力フォームに入力して保存してください。',
                '容量・提供量入力フォーム：', capacity_link,
                '入力者名と店舗を選び、容量・提供量をmlで入力してください。瓶・缶をそのまま提供する商品は「1本・1缶をそのまま提供」を選んでください。分からない欄は空欄で保存できます。',
                'オールイン提供分の機会損失（通常売価換算の参考額）の試算に使用します。'])
        text = '\n'.join(lines)
        if len(text.encode('utf-16-le')) // 2 > 4900:
            raise RuntimeError('リクエストが多いため予約表を確認してください')
        if day == '2026-10-21':
            return {'type': 'text', 'text': text}
        return line_bot.inventory_mention(c, text)
    finally:
        c.close()

def printable_message(db, day, base_url):
    import doorman_pdf
    folder = Path(db).parent / 'line-images'
    folder.mkdir(exist_ok=True)
    path = folder / (uuid.uuid4().hex + '-doorman.pdf')
    doorman_pdf.generate(db, day, path)
    expiry = int(time.time()) + 48 * 3600
    url = f'{base_url}/line-image/{path.name}?expires={expiry}&sig={image_signature(path.name, expiry)}'
    return {'type':'template','altText':f'{day} ドアマン用予約表 PDF','template':{'type':'buttons','title':'ドアマン用予約表','text':f'{day}｜A4・2ページ\n有効期限：48時間','actions':[{'type':'uri','label':'PDFを開く','uri':url}]}}

def resend_briefing(db, day=None, charts=None, printable=None):
    """Explicit correction: one text-only resend per day and exact payload."""
    day = day or datetime.now(JST).date().isoformat()
    with LOCK:
        c = connect(db)
        try:
            if not c.execute("SELECT 1 FROM line_sources WHERE source_id=? AND source_type='group' AND active=1", (TARGET,)).fetchone():
                raise RuntimeError('通知先グループの接続が確認できません')
            payload = {'to': TARGET, 'messages': [message(db, day)]}
            encoded = json.dumps(payload, ensure_ascii=False)
            if charts or printable:
                snapshot = [dict(r) for r in c.execute("SELECT * FROM reservations WHERE substr(visit_at,1,10)=? AND status!='CANCELLED' ORDER BY visit_at,id", (day,))]
                encoded = json.dumps(snapshot, sort_keys=True, ensure_ascii=False)
            key = day + (':pdf-card:' if printable else ':charts:' if charts else ':correction:') + hashlib.sha256(encoded.encode()).hexdigest()[:24]
            existing = c.execute('SELECT * FROM line_deliveries WHERE day=?', (key,)).fetchone()
            if printable and not existing:
                encoded = json.dumps({'to':TARGET,'messages':[printable_message(db,day,printable)]},ensure_ascii=False)
            if charts and not existing:
                port, admin, base_url = charts
                payload = {'to': TARGET, 'messages': [capture(db, day, port, admin, base_url, session) for session in ('18:00','20:30')]}
                encoded = json.dumps(payload, ensure_ascii=False)
            c.execute("INSERT OR IGNORE INTO line_deliveries VALUES(?,?,?,'READY',NULL,?)", (key, str(uuid.uuid4()), encoded, time.time()))
            c.commit()
            row = c.execute('SELECT * FROM line_deliveries WHERE day=?', (key,)).fetchone()
            if row['state'] == 'SENT':
                return {'ok': True, 'already_sent': True}
            if time.time() - row['updated'] > 23 * 3600:
                raise RuntimeError('再送期限を超えています。送信履歴の確認が必要です')
            api('message/push', json.loads(row['payload']), row['retry_key'])
            c.execute("UPDATE line_deliveries SET state='SENT',error=NULL,updated=? WHERE day=?", (time.time(),key))
            c.commit()
            return {'ok': True, 'resent': True}
        finally:
            c.close()



def briefing_text_messages(db, day, base_url):
    result = message(db, day, base_url)
    marker = '\n\n【やまもとさんへの依頼事項①'
    if day == '2026-10-21' and result.get('type') == 'text' and marker in result['text']:
        first, second = result['text'].split(marker, 1)
        return [{'type': 'text', 'text': first.rstrip()},
                {'type': 'text', 'text': '【やまもとさんへの依頼事項①' + second}]
    return [result]


def push_briefing_payload(payload, retry_key):
    # LINE accepts up to five message objects per push. Stable keys prevent
    # replaying an accepted batch if a later batch fails.
    messages = payload['messages']
    for offset in range(0, len(messages), 5):
        key = retry_key if offset == 0 else str(uuid.uuid5(uuid.UUID(retry_key), 'batch:' + str(offset)))
        api('message/push', {**payload, 'messages': messages[offset:offset+5]}, key)


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
                    payload = {'to':TARGET,'messages':briefing_text_messages(db,day,base_url) + [capture(db,day,port,admin,base_url,session) for session in ('18:00','20:30')] + [printable_message(db,day,base_url)] + bank_transfer.accounting_digest(db,base_url)}
                    c.execute("UPDATE line_deliveries SET payload=?,state='READY',updated=? WHERE day=?", (json.dumps(payload),time.time(),day))
                    c.commit()
                push_briefing_payload(payload,row['retry_key'])
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
