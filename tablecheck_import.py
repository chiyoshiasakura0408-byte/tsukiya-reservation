"""Authenticated, previewed TableCheck customer migration. No booking/payment actions."""
import base64
import csv
import hashlib
import hmac
import io
import json
import re
import unicodedata
import zipfile

MAX_FILE = 8 * 1024 * 1024
MAX_CSV = 24 * 1024 * 1024
MAX_ROWS = 20000


def schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS tablecheck_customer_imports(
        source_id TEXT PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id),
        payload_json TEXT NOT NULL, imported_at TEXT NOT NULL)''')
    c.execute('CREATE INDEX IF NOT EXISTS tablecheck_import_customer_idx ON tablecheck_customer_imports(customer_id)')


def norm_name(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value or '')).casefold()


def norm_phone(value):
    digits = re.sub(r'\D', '', unicodedata.normalize('NFKC', value or ''))
    if digits.startswith('81') and len(digits) in (12, 13):
        digits = '0' + digits[2:]
    return digits


def parse(data):
    if not isinstance(data, dict) or not isinstance(data.get('file'), str):
        raise ValueError('CSVまたはZIPファイルを選択してください')
    encoded = data['file']
    if len(encoded) > (MAX_FILE * 4 // 3 + 4):
        raise ValueError('ファイルは8MB以下にしてください')
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        raise ValueError('ファイルを読み取れませんでした') from None
    if not raw or len(raw) > MAX_FILE:
        raise ValueError('ファイルは8MB以下にしてください')
    if raw.startswith(b'PK'):
        password = data.get('password', '')
        if not isinstance(password, str) or len(password) > 256:
            raise ValueError('ZIP解凍パスワードを確認してください')
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                files = [f for f in z.infolist() if not f.is_dir()]
                if len(files) != 1 or not files[0].filename.lower().endswith('.csv'):
                    raise ValueError('顧客CSVが1件入ったZIPを選択してください')
                if files[0].file_size > MAX_CSV:
                    raise ValueError('展開後のCSVが大きすぎます')
                with z.open(files[0], pwd=password.encode('utf-8')) as f:
                    raw = f.read(MAX_CSV + 1)
        except (RuntimeError, zipfile.BadZipFile, NotImplementedError):
            raise ValueError('ZIPを開けません。解凍パスワードとファイルを確認してください') from None
    if len(raw) > MAX_CSV:
        raise ValueError('CSVが大きすぎます')
    for encoding in ('utf-8-sig', 'cp932'):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError('CSVの文字コードを確認してください')
    try:
        reader = csv.reader(io.StringIO(text), strict=True)
        headers = next(reader, [])
        if headers == ['顧客']:
            headers = next(reader, [])
        required = {'顧客ID', '氏 (漢字)', '名 (漢字)', '氏 (あ·ア·A)', '名 (あ·ア·A)', '電話 1', 'Eメール 1'}
        if not required.issubset(headers) or len(headers) != len(set(headers)):
            raise ValueError('TableCheckの顧客CSVを選択してください（予約CSVは対象外です）')
        rows, seen = [], set()
        for line, values in enumerate(reader, 3):
            if not values or not any(values):
                continue
            if len(values) != len(headers):
                raise ValueError(f'{line}行目の列数が一致しません')
            row = dict(zip(headers, values))
            sid = row['顧客ID'].strip()
            if not sid or len(sid) > 200 or sid in seen:
                raise ValueError(f'{line}行目の顧客IDが空欄または重複しています')
            if not names(row):
                raise ValueError(f'{line}行目のお名前が空欄です')
            seen.add(sid)
            rows.append(row)
            if len(rows) > MAX_ROWS:
                raise ValueError('1回の取込は20,000件までです')
    except csv.Error:
        raise ValueError('CSVの形式を確認してください') from None
    if not rows:
        raise ValueError('顧客データがありません')
    return rows


def names(row):
    return [name for name in (
        ' '.join(row.get(k, '').strip() for k in ('氏 (漢字)', '名 (漢字)')).strip(),
        ' '.join(row.get(k, '').strip() for k in ('氏 (あ·ア·A)', '名 (あ·ア·A)')).strip()) if name]


def contacts(row):
    phones = {norm_phone(row.get(k, '')) for k in ('電話 1', '電話 2', '電話 (E.164) 1', '電話 (E.164) 2')}
    emails = {row.get(k, '').strip().casefold() for k in ('Eメール 1', 'Eメール 2')}
    return phones - {''}, emails - {''}


def plan(c, rows):
    existing = [dict(x) for x in c.execute('SELECT id,name,phone,email,updated_at FROM customers ORDER BY id')]
    sources = {x['source_id']: dict(x) for x in c.execute('SELECT source_id,customer_id FROM tablecheck_customer_imports ORDER BY source_id')}
    linked = {x['customer_id'] for x in sources.values()}
    index = {}
    for customer in existing:
        name = norm_name(customer['name'])
        keys = [('p', norm_phone(customer['phone'])), ('e', customer['email'].strip().casefold())]
        for kind, value in keys:
            if value:
                index.setdefault((name, kind, value), set()).add(customer['id'])
    actions, reserved = [], set()
    for row in rows:
        sid = row['顧客ID'].strip()
        if sid in sources:
            actions.append(('skip', sources[sid]['customer_id']))
            continue
        phones, emails = contacts(row)
        candidates = set()
        for name in names(row):
            for kind, values in (('p', phones), ('e', emails)):
                for value in values:
                    candidates.update(index.get((norm_name(name), kind, value), set()))
        # Never consolidate several TableCheck profiles into one existing person.
        if len(candidates) > 1 or candidates & reserved:
            actions.append(('review', None))
        elif len(candidates) == 1 and not candidates & linked:
            cid = next(iter(candidates))
            reserved.add(cid)
            actions.append(('link', cid))
        else:
            actions.append(('new', None))
    state = json.dumps([rows, existing, sources, actions], ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    digest = hashlib.sha256(state.encode()).hexdigest()
    report = {'total': len(rows), 'new': 0, 'link': 0, 'skip': 0, 'review': 0}
    for action, _ in actions:
        report[action] += 1
    report['review_rows'] = [i + 1 for i, a in enumerate(actions) if a[0] == 'review'][:100]
    return actions, report, digest


def detail(c, customer_id):
    return [dict(source_id=r['source_id'], imported_at=r['imported_at'], fields=json.loads(r['payload_json']))
            for r in c.execute('SELECT * FROM tablecheck_customer_imports WHERE customer_id=? ORDER BY source_id', (customer_id,))]


def handle(app, data, commit=False):
    rows = parse(data)
    c = app.con()
    try:
        app.sync_customers(c)
        c.execute('BEGIN IMMEDIATE')
        actions, report, digest = plan(c, rows)
        token = hmac.new(app.ADMIN_TOKEN.encode(), digest.encode(), hashlib.sha256).hexdigest()
        if not commit:
            c.rollback()
            return dict(report, preview_token=token)
        supplied = data.get('preview_token')
        if not isinstance(supplied, str) or not hmac.compare_digest(token, supplied):
            raise ValueError('データが変更されました。「取込内容を確認」からやり直してください')
        if report['review']:
            raise ValueError('同一人物か確認が必要な行があります。取込を中止しました')
        for row, (action, cid) in zip(rows, actions):
            if action == 'skip':
                continue
            now = app.now_iso()
            sid = row['顧客ID'].strip()
            if action == 'new':
                name = names(row)[0]
                phone = row.get('電話 1', '').strip() or row.get('電話 2', '').strip()
                email = row.get('Eメール 1', '').strip() or row.get('Eメール 2', '').strip()
                key = app.customer_match_key({'guest_name': name, 'phone': phone, 'email': email, 'id': 'tablecheck:' + sid})
                if c.execute('SELECT 1 FROM customers WHERE match_key=?', (key,)).fetchone():
                    key = 'tablecheck:' + sid
                # Full originals, including long notes and all 75 columns, remain in the archive.
                cid = c.execute('''INSERT INTO customers(match_key,name,company_name,phone,email,note,allergies,disliked_foods,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)''', (key, name, row.get('会社名', '')[:150], phone, email,
                    row.get('顧客メモ', '')[:2000], row.get('食事制限・アレルギー', '')[:1000],
                    row.get('苦手なもの', '')[:1000], now, now)).lastrowid
            c.execute('INSERT INTO tablecheck_customer_imports(source_id,customer_id,payload_json,imported_at) VALUES(?,?,?,?)',
                      (sid, cid, json.dumps(row, ensure_ascii=False), now))
        c.commit()
        return dict(report, ok=True)
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()
