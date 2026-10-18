"""Owner-only Google consent flow; credentials never enter HTML or reservation rows."""
import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from http.cookies import SimpleCookie

EXPECTED_EMAIL = 'chiyoshiasakura0408@gmail.com'
CALLBACK = '/calendar-sync/callback'
SCOPES = ('openid', 'https://www.googleapis.com/auth/userinfo.email', 'https://www.googleapis.com/auth/calendar.events',
          'https://www.googleapis.com/auth/calendar.calendars.readonly',
          'https://www.googleapis.com/auth/calendar.acls.readonly')
PENDING = {}
LOCK = threading.Lock()


def token_path(db_path):
    return str(db_path) + '.google-calendar-token.json'


def refresh_token(db_path):
    if os.getenv('GOOGLE_CALENDAR_REFRESH_TOKEN'):
        return os.environ['GOOGLE_CALENDAR_REFRESH_TOKEN']
    try:
        with open(token_path(db_path)) as f:
            value = json.load(f)
        if value.get('email') == EXPECTED_EMAIL:
            return value.get('refresh_token', '')
    except (OSError, ValueError, TypeError):
        pass
    return ''


def save_token(db_path, token):
    path = token_path(db_path)
    fd, temporary = tempfile.mkstemp(prefix='.calendar-token-', dir=os.path.dirname(os.path.abspath(path)))
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump({'email': EXPECTED_EMAIL, 'refresh_token': token}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def configured():
    return all(os.getenv(k) for k in ('GOOGLE_CALENDAR_CLIENT_ID', 'GOOGLE_CALENDAR_CLIENT_SECRET', 'GOOGLE_CALENDAR_ID'))


def page(message, connected=False):
    return '''<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>予約カレンダー連携</title></head><body style="font-family:sans-serif;max-width:650px;margin:50px auto;padding:24px"><h1>予約カレンダー連携</h1><p>''' + html.escape(message) + '''</p><p>接続先：西天満つきや｜予約管理<br>Googleアカウント：''' + EXPECTED_EMAIL + '''</p><p>予約情報をGoogleへ一方向に反映します。電話番号・メールアドレスは予定に表示しません。</p><button id="connect">''' + ('Google接続を更新' if connected else 'Googleに接続') + '''</button><p id="result"></p><p><a href="/reservations">予約管理へ戻る</a></p><script>document.getElementById('connect').onclick=async()=>{const b=document.getElementById('connect');b.disabled=true;try{const r=await fetch('/api/calendar-sync/connect',{method:'POST',headers:{'X-Tsukiya-Action':'calendar-connect'}});const d=await r.json();if(!r.ok)throw new Error(d.error);location.assign(d.url)}catch(e){document.getElementById('result').textContent=e.message;b.disabled=false}};</script></body></html>'''


def handle_get(handler, path, query, db_path, base_url):
    if path not in ('/calendar-sync', CALLBACK):
        return False
    if path == '/calendar-sync':
        if not handler.auth():
            handler.redirect('/login')
        else:
            connected = bool(refresh_token(db_path))
            enabled = os.getenv('GOOGLE_CALENDAR_SYNC_ENABLED') == '1'
            message = 'Google接続済み。' if connected else 'Googleの接続許可が必要です。'
            message += '自動同期は有効です。' if enabled else '自動同期は現在停止中です。'
            if not configured():
                message += 'RenderのGoogle認証設定が不足しています。'
            handler.send_html(page(message, connected), loader=False)
        return True
    cookies = SimpleCookie()
    try:
        cookies.load(handler.headers.get('Cookie', ''))
        browser = cookies.get('tsukiya_calendar_oauth')
        browser = browser.value if browser else ''
        state = query.get('state', [''])[0]
        with LOCK:
            pending = PENDING.get(state)
            if not pending or pending[0] < time.time() or not hmac.compare_digest(browser, pending[1]):
                raise ValueError('invalid_state')
            del PENDING[state]
        if query.get('error') or not query.get('code'):
            raise ValueError('consent_denied')
        data = urllib.parse.urlencode({'client_id': os.environ['GOOGLE_CALENDAR_CLIENT_ID'],
            'client_secret': os.environ['GOOGLE_CALENDAR_CLIENT_SECRET'],
            'code': query['code'][0], 'redirect_uri': base_url.rstrip('/') + CALLBACK,
            'grant_type': 'authorization_code', 'code_verifier': pending[2]}).encode()
        with urllib.request.urlopen(urllib.request.Request('https://oauth2.googleapis.com/token', data=data), timeout=20) as response:
            tokens = json.load(response)
        request = urllib.request.Request('https://openidconnect.googleapis.com/v1/userinfo',
            headers={'Authorization': 'Bearer ' + tokens['access_token']})
        with urllib.request.urlopen(request, timeout=20) as response:
            owner = json.load(response)
        if owner.get('email') != EXPECTED_EMAIL or owner.get('email_verified') is not True:
            raise ValueError('wrong_account')
        if not set(SCOPES).issubset(set(tokens.get('scope', '').split())):
            raise ValueError('missing_scope')
        if not tokens.get('refresh_token'):
            raise ValueError('missing_refresh_token')
        # Verify destination and privacy before persisting any access.
        import calendar_sync
        client = calendar_sync.Google(os.environ['GOOGLE_CALENDAR_ID'])
        client.token, client.expires = tokens['access_token'], time.time() + 300
        client.check_calendar()
        save_token(db_path, tokens['refresh_token'])
        message = 'Google接続が完了しました。予約カレンダーへの同期を準備できました。'
        code = 200
    except Exception:
        # Never echo Google responses, codes, tokens, or request URLs.
        message = 'Google接続を完了できませんでした。対象アカウント、権限、リダイレクトURIを確認して、接続をやり直してください。'
        code = 400
    handler.send_html(page(message), status=code, loader=False)
    return True


def handle_post(handler, path, base_url):
    if path != '/api/calendar-sync/connect':
        return False
    if not handler.auth():
        handler.send_json({'error': '管理画面へログインしてください'}, 401)
        return True
    if (handler.headers.get('X-Tsukiya-Action') != 'calendar-connect'
            or handler.headers.get('Origin') != base_url.rstrip('/')):
        handler.send_json({'error': '接続操作を確認できませんでした'}, 403)
        return True
    if not configured() or not base_url.startswith('https://'):
        handler.send_json({'error': 'Google接続設定が不足しています'}, 503)
        return True
    state, browser, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    with LOCK:
        for key in list(PENDING):
            if PENDING[key][0] < time.time():
                del PENDING[key]
        if len(PENDING) >= 50:
            handler.send_json({'error': 'しばらくしてから接続してください'}, 429)
            return True
        PENDING[state] = (time.time() + 600, browser, verifier)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    url = 'https://accounts.google.com/o/oauth2/v2/auth?' + urllib.parse.urlencode({
        'client_id': os.environ['GOOGLE_CALENDAR_CLIENT_ID'], 'redirect_uri': base_url.rstrip('/') + CALLBACK,
        'response_type': 'code', 'scope': ' '.join(SCOPES), 'access_type': 'offline',
        'prompt': 'consent', 'login_hint': EXPECTED_EMAIL, 'state': state,
        'code_challenge': challenge, 'code_challenge_method': 'S256'})
    handler.send_json({'url': url}, headers={'Set-Cookie': 'tsukiya_calendar_oauth=' + browser +
        '; Path=/calendar-sync; Max-Age=600; HttpOnly; Secure; SameSite=Lax'})
    return True
