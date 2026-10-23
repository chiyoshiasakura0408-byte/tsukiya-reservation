"""Manual customer booking restrictions, checked before any reservation side effect."""
import json
import re
import unicodedata
from datetime import datetime, timezone

PUBLIC_MESSAGE = '現在、このご予約を承ることができません。店舗へお問い合わせください。'


class BookingBlocked(ValueError):
    pass


def schema(c):
    # Do not use executescript: callers may already hold a booking transaction.
    c.execute('''CREATE TABLE IF NOT EXISTS customer_booking_blocks(
        customer_id INTEGER PRIMARY KEY, active INTEGER NOT NULL DEFAULT 0,
        reason TEXT NOT NULL DEFAULT '', identifiers TEXT NOT NULL DEFAULT '[]',
        version INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS customer_booking_block_audit(
        id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER NOT NULL,
        active INTEGER NOT NULL, reason TEXT NOT NULL, actor TEXT NOT NULL,
        created_at TEXT NOT NULL)''')


def phone_key(value):
    digits = re.sub(r'[^0-9]', '', unicodedata.normalize('NFKC', str(value or '')))
    if digits.startswith('0081'):
        digits = '0' + digits[4:]
    elif digits.startswith('81') and len(digits) in (11, 12):
        digits = '0' + digits[2:]
    return digits if 9 <= len(digits) <= 15 else ''


def keys(phone='', email=''):
    result = set()
    number = phone_key(phone)
    address = unicodedata.normalize('NFKC', str(email or '')).strip().casefold()
    if number:
        result.add('phone:' + number)
    if re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', address):
        result.add('email:' + address)
    return result


def state(c, customer_id):
    row = c.execute('SELECT * FROM customer_booking_blocks WHERE customer_id=?', (customer_id,)).fetchone()
    result = {'active': False, 'reason': '', 'version': 0, 'updated_at': '', 'identifier_count': 0}
    if row:
        result.update(active=bool(row['active']), reason=row['reason'], version=row['version'],
                      updated_at=row['updated_at'], identifier_count=len(json.loads(row['identifiers'])))
    result['history'] = [dict(r) for r in c.execute('''SELECT active,reason,actor,created_at
        FROM customer_booking_block_audit WHERE customer_id=? ORDER BY id DESC LIMIT 30''', (customer_id,))]
    return result


def annotate(c, rows):
    flags = {r['customer_id']: bool(r['active']) for r in c.execute('SELECT customer_id,active FROM customer_booking_blocks')}
    for row in rows:
        row['booking_blocked'] = flags.get(row['id'], False)
    return rows


def retain_contacts(c, customer):
    """Keep restricted contacts even when staff subsequently edits the profile."""
    block = c.execute('SELECT * FROM customer_booking_blocks WHERE customer_id=?', (customer['id'],)).fetchone()
    if block and block['active']:
        identifiers = set(json.loads(block['identifiers'])) | keys(customer['phone'], customer['email'])
        c.execute('UPDATE customer_booking_blocks SET identifiers=? WHERE customer_id=?',
                  (json.dumps(sorted(identifiers)), customer['id']))


def set_state(c, customer_id, data):
    if not isinstance(data, dict) or set(data) != {'active', 'reason', 'version'}:
        raise ValueError('入力内容を確認してください')
    if type(data['active']) is not bool or type(data['version']) is not int or data['version'] < 0:
        raise ValueError('予約拒否の設定を確認してください')
    reason = data['reason']
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 1000:
        raise ValueError('登録・解除の理由を1〜1,000文字で入力してください')
    customer = c.execute('SELECT * FROM customers WHERE id=?', (customer_id,)).fetchone()
    if not customer:
        raise ValueError('顧客が見つかりません')
    before = c.execute('SELECT * FROM customer_booking_blocks WHERE customer_id=?', (customer_id,)).fetchone()
    version = before['version'] if before else 0
    if version != data['version']:
        raise ValueError('別の操作で更新されています。顧客情報を開き直してください')
    identifiers = set(json.loads(before['identifiers'])) if before else set()
    identifiers.update(keys(customer['phone'], customer['email']))
    for row in c.execute('SELECT phone,email FROM reservations WHERE customer_id=?', (customer_id,)):
        identifiers.update(keys(row['phone'], row['email']))
    now = datetime.now(timezone.utc).isoformat()
    c.execute('''INSERT INTO customer_booking_blocks(customer_id,active,reason,identifiers,version,updated_at)
        VALUES(?,?,?,?,?,?) ON CONFLICT(customer_id) DO UPDATE SET active=excluded.active,
        reason=excluded.reason,identifiers=excluded.identifiers,version=excluded.version,updated_at=excluded.updated_at''',
        (customer_id, int(data['active']), reason.strip(), json.dumps(sorted(identifiers)), version + 1, now))
    c.execute('''INSERT INTO customer_booking_block_audit(customer_id,active,reason,actor,created_at)
        VALUES(?,?,?,?,?)''', (customer_id, int(data['active']), reason.strip(), '管理画面', now))
    return state(c, customer_id)


def ensure_allowed(c, phone='', email='', customer_id=None, staff=False):
    incoming = keys(phone, email)
    for block in c.execute('''SELECT b.*,c.phone,c.email FROM customer_booking_blocks b
            JOIN customers c ON c.id=b.customer_id WHERE b.active=1'''):
        identifiers = set(json.loads(block['identifiers'])) | keys(block['phone'], block['email'])
        if (customer_id is not None and block['customer_id'] == customer_id) or incoming & identifiers:
            message = ('予約拒否対象のお客様です。顧客管理で理由を確認してください。'
                       if staff else PUBLIC_MESSAGE)
            raise BookingBlocked(message)
