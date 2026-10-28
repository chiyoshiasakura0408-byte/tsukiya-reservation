"""Scoped GPT Actions API. Existing staff handlers own all booking business rules."""
import hashlib
import hmac
import json
import os
import re
import threading
import uuid
from datetime import date
from email.message import Message

PREFIX = '/api/gpt'
WRITE_LOCK = threading.Lock()
FIELDS = ('id', 'source', 'guest_name', 'phone', 'email', 'visit_at', 'party_size',
          'course_name', 'amount', 'seating_area', 'counter_round', 'duration_minutes',
          'status', 'payment_method', 'guest_note', 'celebration_items', 'plate_message',
          'customer_id', 'created_at', 'updated_at')
CREATE_FIELDS = {'mode', 'request_id', 'guest_name', 'phone', 'email', 'visit_at',
                 'party_size', 'seating_area', 'customer_id', 'guest_note',
                 'celebration_items', 'plate_message'}
CHANGE_FIELDS = {
    'note': {'guest_note', 'expected_guest_note'},
    'time': {'time', 'expected_visit_at'},
    'party': {'party_size', 'expected_party_size', 'expected_amount', 'expected_visit_at'},
    'room': {'seating_area', 'expected_seating_area', 'expected_visit_at', 'expected_party_size'},
}


def key():
    value = os.getenv('GPT_RESERVATION_API_KEY', '')
    return value if len(value) >= 40 else ''


def authorized(h):
    expected = key()
    values = h.headers.get_all('Authorization', [])
    return bool(expected and len(values) == 1 and
                hmac.compare_digest(values[0].encode(), ('Bearer ' + expected).encode()))


def clean(row):
    return {field: row.get(field) for field in FIELDS if field in row}


def schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS gpt_api_requests(
        request_id TEXT PRIMARY KEY, path TEXT NOT NULL, body_hash TEXT NOT NULL,
        status INTEGER, response TEXT, created_at TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS gpt_api_audit(
        id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL,
        path TEXT NOT NULL, status INTEGER NOT NULL, reservation_id INTEGER,
        created_at TEXT NOT NULL)''')
    c.commit()


def reject(h):
    h.send_json({'error': 'unauthorized'}, 401,
                headers={'WWW-Authenticate': 'Bearer', 'Cache-Control': 'no-store'})


def handle_get(h, app, path, query):
    if path == '/gpt-setup':
        if not h.auth():
            h.redirect('/login?next=/gpt-setup')
        else:
            h.send_html((app.BASE / 'public' / 'gpt-setup.html').read_text(), loader=False)
        return True
    if path == PREFIX + '/openapi.json':
        h.send_json(openapi(app.APP_BASE_URL))
        return True
    if not (path == PREFIX or path.startswith(PREFIX + '/')):
        return False
    if not authorized(h):
        reject(h)
        return True
    try:
        if path == PREFIX + '/status':
            h.send_json({'ok': True, 'api_version': '1', 'timezone': 'Asia/Tokyo',
                         'operations': ['list', 'get', 'availability', 'create', 'note', 'time', 'party', 'room']})
            return True
        c = app.con()
        try:
            match = re.fullmatch(PREFIX + r'/reservations/(\d+)', path)
            if match:
                row = c.execute('SELECT * FROM reservations WHERE id=?', (int(match[1]),)).fetchone()
                h.send_json(clean(dict(row)) if row else {'error': '予約が見つかりません'}, 200 if row else 404)
            elif path == PREFIX + '/reservations':
                start = date.fromisoformat(query.get('from_date', [''])[0])
                end = date.fromisoformat(query.get('to_date', [start.isoformat()])[0])
                if not 0 <= (end - start).days <= 31:
                    raise ValueError('検索期間は32日以内で指定してください')
                limit = int(query.get('limit', ['100'])[0])
                after_id = int(query.get('after_id', ['0'])[0])
                if not 1 <= limit <= 100 or after_id < 0:
                    raise ValueError('ページ指定が不正です')
                rows = c.execute('''SELECT * FROM reservations WHERE substr(visit_at,1,10) BETWEEN ? AND ?
                    AND id>? ORDER BY id LIMIT ?''', (start.isoformat(), end.isoformat(), after_id, limit + 1)).fetchall()
                h.send_json({'reservations': [clean(dict(row)) for row in rows[:limit]],
                             'next_after_id': rows[limit - 1]['id'] if len(rows) > limit else None,
                             'timezone': 'Asia/Tokyo'})
            elif path == PREFIX + '/availability':
                visit = query.get('visit_at', [''])[0]
                validate_visit(visit)
                area = query.get('seating_area', [''])[0]
                party = int(query.get('party_size', ['0'])[0])
                if area not in ('COUNTER', *app.ROOMS) or not 1 <= party <= 8:
                    raise ValueError('席・人数を確認してください')
                rnd = (1 if visit[11:16] == '18:00' else 2) if area == 'COUNTER' else None
                ok, message = app.availability_check(c, area, visit, party, rnd, 150)
                course = app.bookable_course(app.parse_dt(visit).date())
                h.send_json({'available': bool(ok and course and app.staff_slot_allowed(app.parse_dt(visit).date(), visit[11:16])),
                             'message': message, 'course_name': course[0] if course else None,
                             'amount': course[1] * party if course else None, 'timezone': 'Asia/Tokyo'})
            else:
                h.send_json({'error': 'not found'}, 404)
        finally:
            c.close()
    except (ValueError, TypeError):
        h.send_json({'error': '日付・人数・席・検索期間を確認してください'}, 400)
    return True


def validate_visit(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T(?:18:00|20:30)', value):
        raise ValueError('来店日時は日本時間のYYYY-MM-DDT18:00または20:30で指定してください')
    date.fromisoformat(value[:10])


def validate_create(data, app):
    if set(data) - CREATE_FIELDS:
        raise ValueError('未対応の予約項目です')
    mode = data.get('mode')
    if mode not in ('direct', 'phone'):
        raise ValueError('direct（当日精算）またはphone（前受け請求）を指定してください')
    validate_visit(data.get('visit_at'))
    if type(data.get('party_size')) is not int or not 1 <= data['party_size'] <= 8:
        raise ValueError('人数を確認してください')
    if data.get('seating_area') not in ('COUNTER', *app.ROOMS):
        raise ValueError('席を確認してください')
    for field, maximum in (('guest_name', 80), ('phone', 50), ('email', 254)):
        value = data.get(field, '')
        if not isinstance(value, str) or len(value) > maximum or '\x00' in value:
            raise ValueError('名前・連絡先を確認してください')
    if not data.get('guest_name', '').strip():
        raise ValueError('名前を指定してください')
    if data.get('email') and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', data['email']):
        raise ValueError('メールアドレスを確認してください')
    payload = dict(data)
    payload.pop('mode')
    payload['counter_round'] = (1 if data['visit_at'][11:16] == '18:00' else 2) if data['seating_area'] == 'COUNTER' else None
    return '/api/reservations/' + mode, payload


def handle_post(h, app, path):
    if path == PREFIX + '/setup-key':
        if not h.auth():
            reject(h)
        elif (not app.APP_BASE_URL or h.headers.get('Origin') != app.APP_BASE_URL
              or h.headers.get('X-Tsukiya-Action') != 'gpt-setup'):
            h.send_json({'error': '設定画面を開き直してください'}, 403)
        elif not key():
            h.send_json({'error': '接続用キーが未設定です'}, 503)
        else:
            h.send_json({'api_key': key()}, headers={'Cache-Control': 'no-store'})
        return True
    if not (path == PREFIX or path.startswith(PREFIX + '/')):
        return False
    if not authorized(h):
        reject(h)
        return True
    try:
        size = int(h.headers.get('Content-Length', '0'))
        if not 0 < size <= 16000:
            h.send_json({'error': 'request too large or empty'}, 413)
            return True
        if h.headers.get_content_type() != 'application/json':
            raise ValueError('Content-Typeはapplication/jsonで指定してください')
        data = h.read_json()
        if not isinstance(data, dict):
            raise ValueError('JSONオブジェクトを指定してください')
        request_id = data.get('request_id', '')
        if not isinstance(request_id, str) or str(uuid.UUID(request_id)) != request_id:
            raise ValueError('request_idには新しい小文字UUIDを指定してください')
        if path == PREFIX + '/reservations':
            target, payload = validate_create(data, app)
        else:
            match = re.fullmatch(PREFIX + r'/reservations/(\d+)/(note|time|party|room)', path)
            if not match:
                h.send_json({'error': 'not found'}, 404)
                return True
            allowed = CHANGE_FIELDS[match[2]]
            if set(data) != allowed | {'request_id'}:
                raise ValueError('変更項目と照合用の現在値を指定してください')
            payload = {k: v for k, v in data.items() if k != 'request_id'}
            target = '/api/reservations/' + match[1] + '/' + match[2]
        digest = hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    except (ValueError, TypeError, UnicodeError, AttributeError):
        h.send_json({'error': '入力項目・来店日時・照合用の現在値・request_idを確認してください'}, 400)
        return True
    # Serialize mutations; native handlers provide seat locks and business validation.
    with WRITE_LOCK:
        c = app.con()
        try:
            schema(c)
            prior = c.execute('SELECT * FROM gpt_api_requests WHERE request_id=?', (request_id,)).fetchone()
            if prior and (prior['path'] != path or prior['body_hash'] != digest):
                h.send_json({'error': '同じrequest_idを別の操作に再利用できません'}, 409)
                return True
            if prior and prior['response'] is not None:
                h.send_json(json.loads(prior['response']), prior['status'])
                return True
            if prior:
                # A process interruption leaves an unknown outcome; never replay a write blindly.
                h.send_json({'error': '処理結果が未確定です。予約一覧と履歴を確認してから対応してください'}, 409)
                return True
            c.execute('INSERT INTO gpt_api_requests(request_id,path,body_hash,created_at) VALUES(?,?,?,?)',
                      (request_id, path, digest, app.now_iso()))
            c.commit()
            result = []
            def capture(obj, status=200, headers=None):
                obj = clean(obj) if isinstance(obj, dict) and 'guest_name' in obj else obj
                result.append((obj, status))
            class ScopedHandler(type(h)):
                def auth(self):
                    return self.path == target
                def read_json(self):
                    return payload
                def send_json(self, obj, status=200, headers=None):
                    capture(obj, status, headers)
            adapter = object.__new__(ScopedHandler)
            adapter.__dict__.update(h.__dict__)
            adapter.path = target
            adapter.headers = Message()
            adapter.headers['Content-Type'] = 'application/json'
            adapter.headers['Content-Length'] = str(len(json.dumps(payload).encode()))
            adapter.headers['Origin'] = app.APP_BASE_URL
            adapter.headers['X-Tsukiya-Action'] = 'edit-reservation-note' if target.endswith('/note') else 'change-time'
            try:
                app.Handler.do_POST(adapter)
                obj, status = result[-1] if result else ({'error': '処理結果を確認してください'}, 503)
            except Exception:
                obj, status = {'error': '結果を確認できません。予約一覧を確認してから対応してください'}, 503
            c.execute('UPDATE gpt_api_requests SET status=?,response=? WHERE request_id=?',
                      (status, json.dumps(obj, ensure_ascii=False), request_id))
            rid = obj.get('id') if isinstance(obj, dict) else None
            if rid is None:
                match = re.search(r'/reservations/(\d+)/', target)
                rid = int(match[1]) if match else None
            c.execute('INSERT INTO gpt_api_audit(request_id,path,status,reservation_id,created_at) VALUES(?,?,?,?,?)',
                      (request_id, path, status, rid, app.now_iso()))
            c.commit()
            h.send_json(obj, status)
        finally:
            c.close()
    return True


def openapi(base):
    def string(description='', **extra):
        return {'type': 'string', 'description': description, **extra}
    integer = {'type': 'integer'}
    response = {'description': '操作結果。エラー時はerrorを返します。', 'content': {'application/json': {'schema': {'type': 'object', 'additionalProperties': True}}}}
    def operation(name, summary, properties=None, required=(), parameters=None):
        item = {'operationId': name, 'summary': summary, 'responses': {'200': response, '400': response, '401': response, '409': response}}
        if properties is not None:
            item['requestBody'] = {'required': True, 'content': {'application/json': {'schema': {
                'type': 'object', 'properties': {'request_id': string('新しい小文字UUID。再試行では同じ値と同じ内容を使う。'), **properties},
                'required': ['request_id', *required], 'additionalProperties': False}}}}
            item['x-openai-isConsequential'] = True
        if parameters:
            item['parameters'] = parameters
        return item
    def param(name, definition, required=True, where='query'):
        return {'name': name, 'in': where, 'required': required, 'schema': definition}
    create = {'mode': string('directは当日精算。phoneは前受け請求書・通知を発行する場合がある。利用者の指定に従う。', enum=['direct', 'phone']),
              'guest_name': string(), 'phone': string(), 'email': string(),
              'visit_at': string('日本時間YYYY-MM-DDT18:00またはYYYY-MM-DDT20:30'),
              'party_size': {'type': 'integer', 'minimum': 1, 'maximum': 8},
              'seating_area': string(enum=['COUNTER', 'PRIVATE1', 'PRIVATE2', 'PRIVATE3']),
              'customer_id': integer, 'guest_note': string(),
              'celebration_items': {'type': 'array', 'items': string()}, 'plate_message': string()}
    paths = {
        PREFIX + '/status': {'get': operation('connectionStatus', '接続状態の確認（顧客情報なし）')},
        PREFIX + '/reservations': {
            'get': operation('listReservations', '日付範囲で予約検索。32日以内、最大100件ずつ。', parameters=[
                param('from_date', string(format='date')), param('to_date', string(format='date'), False),
                param('limit', integer, False), param('after_id', integer, False)]),
            'post': operation('createReservation', '既存の席数・料金ルールで予約登録。電話予約は請求・通知を伴う場合がある。', create,
                              ['mode', 'guest_name', 'visit_at', 'party_size', 'seating_area'])},
        PREFIX + '/reservations/{id}': {'get': operation('getReservation', '変更前に予約の現在値を取得する', parameters=[param('id', integer, where='path')])},
        PREFIX + '/availability': {'get': operation('checkAvailability', '日本時間の来店日時・席・人数で空席と料理代を確認', parameters=[
            param('visit_at', create['visit_at']), param('seating_area', create['seating_area']), param('party_size', create['party_size'])])},
    }
    definitions = {
        'note': {'guest_note': string(), 'expected_guest_note': string('getReservationで取得した備考。NULLは空文字。')},
        'time': {'time': string('同じ日の18:00または20:30', enum=['18:00', '20:30']), 'expected_visit_at': string()},
        'party': {'party_size': create['party_size'], 'expected_party_size': integer, 'expected_amount': integer, 'expected_visit_at': string()},
        'room': {'seating_area': string(enum=['PRIVATE1', 'PRIVATE2', 'PRIVATE3']), 'expected_seating_area': string(), 'expected_visit_at': string(), 'expected_party_size': integer},
    }
    for kind, props in definitions.items():
        paths[PREFIX + '/reservations/{id}/' + kind] = {'post': operation('changeReservation' + kind.title(),
            '備考変更' if kind == 'note' else '直接予約の変更。請求済み・支払済みなど既存ルールで変更できない予約は拒否する。',
            props, list(props), [param('id', integer, where='path')])}
    return {'openapi': '3.1.0', 'info': {'title': 'つきや予約管理 GPT連携', 'version': '1.0.0'},
            'servers': [{'url': base or 'https://tsukiya-reservation.onrender.com'}],
            'security': [{'gptBearer': []}], 'components': {'schemas': {}, 'securitySchemes': {'gptBearer': {'type': 'http', 'scheme': 'bearer'}}}, 'paths': paths}
