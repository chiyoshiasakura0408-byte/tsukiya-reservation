import os, json, sqlite3, urllib.request, urllib.error, hashlib, hmac, base64, smtplib
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from pathlib import Path
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

BASE = Path(__file__).parent
DATA_DIR = Path(os.getenv('DATA_DIR', str(BASE)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB = DATA_DIR / 'tsukiya.sqlite'

PORT = int(os.getenv('PORT', '10000'))
ADMIN_TOKEN = os.getenv('ADMIN_TOKEN', '')
SQUARE_TOKEN = os.getenv('SQUARE_ACCESS_TOKEN', '')
SQUARE_LOCATION_ID = os.getenv('SQUARE_LOCATION_ID', '')
SQUARE_ENV = os.getenv('SQUARE_ENV', 'production')
SQUARE_API_VERSION = os.getenv('SQUARE_API_VERSION', '2026-08-19')
APP_BASE_URL = os.getenv('APP_BASE_URL', '')
SQUARE_WEBHOOK_SIGNATURE_KEY = os.getenv('SQUARE_WEBHOOK_SIGNATURE_KEY', '')
COUNTER_CAPACITY = int(os.getenv('COUNTER_CAPACITY', '8'))

SMTP_HOST = os.getenv('SMTP_HOST', '')
SMTP_PORT = int(os.getenv('SMTP_PORT', '587'))
SMTP_USER = os.getenv('SMTP_USER', '')
SMTP_PASS = os.getenv('SMTP_PASS', '')
MAIL_FROM = os.getenv('MAIL_FROM', SMTP_USER)

ACTIVE_STATUSES = ('PENDING','INVOICED','CONFIRMED')
ROOMS = ('PRIVATE1','PRIVATE2','PRIVATE3')


def con():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute('''CREATE TABLE IF NOT EXISTS reservations(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT NOT NULL,
        guest_name TEXT NOT NULL,
        phone TEXT, email TEXT,
        visit_at TEXT NOT NULL,
        party_size INTEGER NOT NULL,
        course_name TEXT,
        amount INTEGER NOT NULL,
        seating_area TEXT NOT NULL DEFAULT 'COUNTER',
        counter_round INTEGER,
        duration_minutes INTEGER NOT NULL DEFAULT 150,
        status TEXT NOT NULL DEFAULT 'PENDING',
        square_customer_id TEXT,
        square_order_id TEXT,
        square_invoice_id TEXT,
        square_invoice_url TEXT,
        square_booking_id TEXT,
        confirmation_sent_at TEXT,
        last_error TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS webhook_events(
        event_id TEXT PRIMARY KEY,
        event_type TEXT,
        received_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    # Migrate older DBs safely.
    cols = {r['name'] for r in c.execute('PRAGMA table_info(reservations)').fetchall()}
    for name, ddl in [
        ('seating_area', "TEXT NOT NULL DEFAULT 'COUNTER'"),
        ('counter_round', 'INTEGER'),
        ('duration_minutes', 'INTEGER NOT NULL DEFAULT 150'),
        ('square_booking_id', 'TEXT'),
        ('confirmation_sent_at', 'TEXT')
    ]:
        if name not in cols:
            c.execute(f'ALTER TABLE reservations ADD COLUMN {name} {ddl}')
    c.commit()
    return c


def square(path, method='POST', body=None):
    if not SQUARE_TOKEN or not SQUARE_LOCATION_ID:
        raise RuntimeError('Square認証情報が未設定です')
    base = 'https://connect.squareupsandbox.com' if SQUARE_ENV == 'sandbox' else 'https://connect.squareup.com'
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method, headers={
        'Authorization': f'Bearer {SQUARE_TOKEN}',
        'Square-Version': SQUARE_API_VERSION,
        'Content-Type': 'application/json'
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError(e.read().decode())


def make_invoice(r):
    if not r['email']:
        raise RuntimeError('Square請求書メール送信にはメールアドレスが必要です')
    rid = str(r['id'])
    customer = square('/v2/customers', body={
        'idempotency_key': f'tsukiya-customer-{rid}',
        'given_name': r['guest_name'],
        'email_address': r['email'],
        'phone_number': r['phone'] or None,
        'reference_id': f'tsukiya-reservation-{rid}'
    })['customer']
    order = square('/v2/orders', body={
        'idempotency_key': f'tsukiya-order-{rid}',
        'order': {
            'location_id': SQUARE_LOCATION_ID,
            'reference_id': f'tsukiya-reservation-{rid}',
            'customer_id': customer['id'],
            'line_items': [{
                'name': r['course_name'] or 'ご予約代金',
                'quantity': '1',
                'base_price_money': {'amount': int(r['amount']), 'currency': 'JPY'}
            }]
        }
    })['order']
    due = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
    inv = square('/v2/invoices', body={
        'idempotency_key': f'tsukiya-invoice-{rid}',
        'invoice': {
            'location_id': SQUARE_LOCATION_ID,
            'order_id': order['id'],
            'primary_recipient': {'customer_id': customer['id']},
            'delivery_method': 'EMAIL',
            'title': '西天満 つきや ご予約代金',
            'payment_requests': [{'request_type': 'BALANCE', 'due_date': due}],
            'accepted_payment_methods': {'card': True}
        }
    })['invoice']
    pub = square(f'/v2/invoices/{inv["id"]}/publish', body={
        'version': inv['version'],
        'idempotency_key': f'tsukiya-publish-{rid}'
    })['invoice']
    return customer['id'], order['id'], pub['id'], pub.get('public_url')


def verify_square(raw, signature):
    if not SQUARE_WEBHOOK_SIGNATURE_KEY or not APP_BASE_URL or not signature:
        return False
    msg = (APP_BASE_URL.rstrip('/') + '/webhooks/square').encode() + raw
    digest = base64.b64encode(hmac.new(SQUARE_WEBHOOK_SIGNATURE_KEY.encode(), msg, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(digest, signature)


def parse_dt(s):
    return datetime.fromisoformat(s.replace('Z', '+00:00'))


def availability_check(c, seating_area, visit_at, party_size, counter_round=None, duration_minutes=150, exclude_id=None):
    if seating_area == 'COUNTER':
        if counter_round not in (1, 2):
            return False, 'カウンターは1部または2部を指定してください'
        day = visit_at[:10]
        sql = '''SELECT COALESCE(SUM(party_size),0) AS used FROM reservations
                 WHERE seating_area='COUNTER' AND counter_round=? AND substr(visit_at,1,10)=?
                   AND status IN ('PENDING','INVOICED','CONFIRMED')'''
        params = [counter_round, day]
        if exclude_id:
            sql += ' AND id<>?'; params.append(exclude_id)
        used = int(c.execute(sql, params).fetchone()['used'])
        remaining = COUNTER_CAPACITY - used
        if party_size > remaining:
            return False, f'カウンター{counter_round}部は残り{max(0,remaining)}席です'
        return True, f'残席 {remaining - party_size}席'

    if seating_area in ROOMS:
        start = parse_dt(visit_at)
        end = start + timedelta(minutes=duration_minutes)
        sql = '''SELECT id,visit_at,duration_minutes FROM reservations
                 WHERE seating_area=? AND status IN ('PENDING','INVOICED','CONFIRMED')'''
        params = [seating_area]
        if exclude_id:
            sql += ' AND id<>?'; params.append(exclude_id)
        for row in c.execute(sql, params).fetchall():
            other_start = parse_dt(row['visit_at'])
            other_end = other_start + timedelta(minutes=int(row['duration_minutes'] or 150))
            if start < other_end and other_start < end:
                return False, 'この個室は指定時間帯に別の予約があります'
        return True, '予約可能'

    return False, '席種が不正です'


def send_confirmation_email(r):
    if not r['email']:
        return False, 'メールアドレス未登録'
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS and MAIL_FROM):
        return False, 'SMTP未設定'
    area = {'COUNTER':'カウンター', 'PRIVATE1':'個室1', 'PRIVATE2':'個室2', 'PRIVATE3':'個室3'}.get(r['seating_area'], r['seating_area'])
    if r['seating_area'] == 'COUNTER' and r['counter_round']:
        area += f'（{r["counter_round"]}部）'
    body = f'''{r['guest_name']} 様\n\nこの度は、西天満 つきやへご予約を賜り、誠にありがとうございます。\nご予約代金のお支払いを確認し、下記の通りご予約を確定いたしました。\n\nご来店日時：{r['visit_at'].replace('T',' ')}\nお席：{area}\n人数：{r['party_size']}名様\nコース：{r['course_name'] or 'ご予約コース'}\n\n当日は、選び抜いた活蟹とともに、心を尽くしてお迎えいたします。\n皆様のご来店を心よりお待ち申し上げております。\n\n西天満 つきや\n'''
    msg = EmailMessage()
    msg['Subject'] = '【西天満 つきや】ご予約確定のご案内'
    msg['From'] = MAIL_FROM
    msg['To'] = r['email']
    msg.set_content(body)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
        s.starttls(); s.login(SMTP_USER, SMTP_PASS); s.send_message(msg)
    return True, None


def sync_square_booking(c, booking):
    bid = booking.get('id')
    if not bid:
        return
    existing = c.execute('SELECT id FROM reservations WHERE square_booking_id=?', (bid,)).fetchone()
    status = booking.get('status', 'ACCEPTED')
    local_status = 'CANCELLED' if status in ('CANCELLED_BY_CUSTOMER','CANCELLED_BY_SELLER','NO_SHOW') else 'CONFIRMED'
    if existing:
        c.execute('UPDATE reservations SET visit_at=?,status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                  (booking.get('start_at') or '', local_status, existing['id']))
        return
    # External Square booking: import basic data. Seating stays UNKNOWN until staff assigns it.
    customer_id = booking.get('customer_id')
    name, email, phone = 'Square予約', None, None
    if customer_id:
        try:
            cust = square(f'/v2/customers/{customer_id}', method='GET').get('customer', {})
            name = (cust.get('given_name','') + ' ' + cust.get('family_name','')).strip() or 'Square予約'
            email = cust.get('email_address'); phone = cust.get('phone_number')
        except Exception:
            pass
    duration = 150
    segs = booking.get('appointment_segments') or []
    if segs:
        duration = sum(int(x.get('duration_minutes') or 0) + int(x.get('intermission_minutes') or 0) for x in segs) or 150
    c.execute('''INSERT INTO reservations(source,guest_name,phone,email,visit_at,party_size,course_name,amount,seating_area,duration_minutes,status,square_customer_id,square_booking_id)
                 VALUES('SQUARE',?,?,?,?,1,'Square予約',0,'UNASSIGNED',?,?,?,?)''',
              (name, phone, email, booking.get('start_at') or '', duration, local_status, customer_id, bid))


class H(BaseHTTPRequestHandler):
    def send_json(self, obj, status=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers(); self.wfile.write(b)

    def auth(self):
        return bool(ADMIN_TOKEN) and self.headers.get('x-admin-token') == ADMIN_TOKEN

    def read_json(self):
        n = int(self.headers.get('Content-Length', '0'))
        return json.loads(self.rfile.read(n) or b'{}')

    def do_GET(self):
        p = urlparse(self.path).path
        if p == '/':
            b = (BASE / 'public/index.html').read_bytes()
            self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8')
            self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b); return
        if p == '/health':
            return self.send_json({'ok':True,'square_configured':bool(SQUARE_TOKEN and SQUARE_LOCATION_ID),'counter_capacity':COUNTER_CAPACITY})
        if p == '/api/reservations':
            if not self.auth(): return self.send_json({'error':'unauthorized'},401)
            c=con(); rows=[dict(x) for x in c.execute('SELECT * FROM reservations ORDER BY visit_at,id').fetchall()]; c.close()
            return self.send_json(rows)
        if p == '/api/availability':
            if not self.auth(): return self.send_json({'error':'unauthorized'},401)
            q = __import__('urllib.parse').parse.parse_qs(urlparse(self.path).query)
            area=(q.get('seating_area') or [''])[0]; visit=(q.get('visit_at') or [''])[0]
            party=int((q.get('party_size') or ['1'])[0]); rnd=int((q.get('counter_round') or ['0'])[0]); dur=int((q.get('duration_minutes') or ['150'])[0])
            c=con(); ok,msg=availability_check(c,area,visit,party,rnd,dur); c.close()
            return self.send_json({'available':ok,'message':msg})
        self.send_error(404)

    def do_POST(self):
        p = urlparse(self.path).path
        if p == '/api/reservations/phone':
            if not self.auth(): return self.send_json({'error':'unauthorized'},401)
            x=self.read_json()
            required=('guest_name','visit_at','party_size','amount','seating_area')
            if any(x.get(k) in (None,'') for k in required): return self.send_json({'error':'必須項目が不足しています'},400)
            area=x['seating_area']; rnd=int(x.get('counter_round') or 0) or None; dur=int(x.get('duration_minutes') or 150)
            c=con(); ok,msg=availability_check(c,area,x['visit_at'],int(x['party_size']),rnd,dur)
            if not ok: c.close(); return self.send_json({'error':msg},409)
            cur=c.execute('''INSERT INTO reservations(source,guest_name,phone,email,visit_at,party_size,course_name,amount,seating_area,counter_round,duration_minutes,status)
              VALUES('PHONE',?,?,?,?,?,?,?,?,?,?,'PENDING')''',
              (x['guest_name'],x.get('phone'),x.get('email'),x['visit_at'],int(x['party_size']),x.get('course_name'),int(x['amount']),area,rnd,dur))
            c.commit(); row=dict(c.execute('SELECT * FROM reservations WHERE id=?',(cur.lastrowid,)).fetchone()); c.close()
            return self.send_json(row)

        if p.startswith('/api/reservations/') and p.endswith('/send-invoice'):
            if not self.auth(): return self.send_json({'error':'unauthorized'},401)
            try: rid=int(p.split('/')[3])
            except: return self.send_json({'error':'bad id'},400)
            c=con(); r=c.execute('SELECT * FROM reservations WHERE id=?',(rid,)).fetchone()
            if not r: c.close(); return self.send_json({'error':'not found'},404)
            if r['square_invoice_id']:
                out=dict(r); c.close(); return self.send_json(out)
            try:
                cid,oid,iid,url=make_invoice(r)
                c.execute('''UPDATE reservations SET square_customer_id=?,square_order_id=?,square_invoice_id=?,square_invoice_url=?,status='INVOICED',last_error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?''',(cid,oid,iid,url,rid)); c.commit()
            except Exception as e:
                c.execute("UPDATE reservations SET status='ERROR',last_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(str(e),rid)); c.commit(); c.close()
                return self.send_json({'error':str(e)},500)
            out=dict(c.execute('SELECT * FROM reservations WHERE id=?',(rid,)).fetchone()); c.close(); return self.send_json(out)

        if p == '/webhooks/square':
            n=int(self.headers.get('Content-Length','0')); raw=self.rfile.read(n)
            if not verify_square(raw,self.headers.get('x-square-hmacsha256-signature','')):
                return self.send_json({'error':'invalid signature'},403)
            ev=json.loads(raw); eid=ev.get('event_id'); et=ev.get('type')
            c=con()
            if eid and c.execute('SELECT 1 FROM webhook_events WHERE event_id=?',(eid,)).fetchone():
                c.close(); return self.send_json({'ok':True})
            if eid: c.execute('INSERT INTO webhook_events(event_id,event_type) VALUES(?,?)',(eid,et))
            if et == 'invoice.payment_made':
                iid=((ev.get('data') or {}).get('object') or {}).get('invoice',{}).get('id')
                if iid:
                    row=c.execute('SELECT * FROM reservations WHERE square_invoice_id=?',(iid,)).fetchone()
                    if row:
                        c.execute("UPDATE reservations SET status='CONFIRMED',updated_at=CURRENT_TIMESTAMP WHERE id=?",(row['id'],)); c.commit()
                        row=c.execute('SELECT * FROM reservations WHERE id=?',(row['id'],)).fetchone()
                        if not row['confirmation_sent_at']:
                            try:
                                sent,err=send_confirmation_email(row)
                                if sent: c.execute('UPDATE reservations SET confirmation_sent_at=CURRENT_TIMESTAMP WHERE id=?',(row['id'],))
                                elif err: c.execute('UPDATE reservations SET last_error=? WHERE id=?',(err,row['id']))
                            except Exception as e:
                                c.execute('UPDATE reservations SET last_error=? WHERE id=?',(f'確認メール送信エラー: {e}',row['id']))
            elif et in ('booking.created','booking.updated'):
                booking=((ev.get('data') or {}).get('object') or {}).get('booking') or {}
                sync_square_booking(c,booking)
            c.commit(); c.close(); return self.send_json({'ok':True})

        self.send_error(404)

if __name__ == '__main__':
    con().close()
    ThreadingHTTPServer(('0.0.0.0', PORT), H).serve_forever()
