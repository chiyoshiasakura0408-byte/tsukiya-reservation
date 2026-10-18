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


def public_page(privacy=False):
    title = 'プライバシーポリシー / Privacy Policy' if privacy else 'Tsukiya Calendar Sync'
    content = """<p>運営：西天満つきや（担当：朝倉） / Operated by Nishitenma Tsukiya.</p>
<p>Tsukiya Calendar Syncは、店舗管理者が許可したGoogleアカウントの専用カレンダーへ、店舗の予約情報を一方向に同期する業務用アプリです。一般のお客様がGoogleアカウントを接続するサービスではありません。</p>
<p>This application synchronizes restaurant reservations to the owner's designated Google Calendar. Access is limited to the restaurant administrator.</p>"""
    if privacy:
        content += """<p>更新日 / Updated: 2026-10-18</p>
<h2>取得する情報と利用目的 / Access and use</h2>
<p>Googleから取得するアカウントのメールアドレスは接続先本人の確認に使用します。専用カレンダーの名前・識別子・共有設定を読み取り、同期先が適切かを確認します。専用カレンダー上の同期対象予定を読み取り、新規作成・更新・取消に伴う削除を行います。認可スコープが許可する範囲にかかわらず、この連携処理は設定済みの専用カレンダーを対象とします。</p>
<p>We use the Google account email to verify the owner, read the designated calendar's metadata and sharing settings, and read, create, update or delete reservation events in that calendar. The integration targets the configured dedicated calendar rather than personal calendars.</p>
<h2>Googleへ送信する予約情報 / Reservation data sent to Google</h2>
<p>予約者名、予約番号、来店日時、人数、席、コース、決済状態、予約状態、お祝い・プレート・ご要望を送信します。カード番号は送信しません。電話番号・メールアドレスの専用項目は送信せず、自由記述内の一般的な連絡先表記はマスクしますが、すべての表記を検出できる保証はありません。予定は非公開設定とし、招待メールは送信しません。</p>
<p>Events contain the guest name, reservation ID, date and time, party size, seating, course, payment and reservation status, and relevant celebration or guest requests. Payment card numbers and dedicated phone/email fields are not sent. Common contact formats in free text are masked, although unusual formats may remain. Events are marked private and no invitation emails are sent.</p>
<h2>保存と保護 / Storage and protection</h2>
<p>継続同期に必要な認証トークンは運用サーバーの永続領域または秘密環境変数に保存します。ファイル保存時は所有者のみ読み書きできる権限を設定します。同期先・予定識別子、同期結果・エラーコードを予約システムに保存します。Googleへの通信にはHTTPSを使用し、認証情報を公開ページに表示しません。</p>
<p>Authorization tokens are stored on the operating server's persistent storage or in secret environment variables. Token files are restricted to their owner. Calendar/event identifiers and synchronization status are stored by the reservation system. Requests to Google use HTTPS; credentials are not displayed on public pages.</p>
<h2>共有・目的外利用 / Sharing and limitations</h2>
<p>連携の提供に必要なGoogleおよびサーバー運用基盤で情報を処理します。Googleユーザーデータを販売せず、広告配信や汎用AIモデルの学習に使用しません。この連携を通じて取得したGoogleユーザーデータの利用・他アプリへの転送は、Limited Use要件を含むGoogle API Services User Data Policyに従います。</p>
<p>Data is processed by Google and the hosting infrastructure as needed to provide this integration. Google user data is not sold, used for advertising, or used to train general-purpose AI models. Use and transfer of information received from Google APIs adhere to the Google API Services User Data Policy, including its Limited Use requirements.</p>
<h2>連携解除・保存期間・削除 / Revocation, retention and deletion</h2>
<p>Googleアカウントの「サードパーティとの接続」からアプリのアクセス権を取り消せます。取り消すと以後の同期はできなくなりますが、作成済みのカレンダー予定やサーバー上の保存データは自動では削除されません。認証情報は連携運用に必要な期間、同期記録は予約管理に必要な期間保持します。連携停止・トークンや同期記録・予定の削除依頼は下記窓口へご連絡ください。バックアップ内のデータはバックアップの更新・廃棄に伴って削除されます。</p>
<p>You may revoke access in your Google Account's third-party connections settings. Revocation stops future API access but does not automatically erase existing events or server records. Credentials are retained for integration operation and synchronization records for reservation management. Contact us to request disconnection or deletion of tokens, synchronization records or events. Backup copies are removed as backups are replaced or retired.</p>"""
    else:
        content += """<h2>機能 / Features</h2><p>予約の新規登録・日時や人数等の変更・キャンセルを専用カレンダーへ反映します。Googleカレンダーの変更を予約システムへ書き戻す機能はありません。</p><p>New reservations, changes and cancellations are reflected in the dedicated calendar. Calendar edits are not written back into the reservation system.</p><p><a href="/calendar-sync/privacy">プライバシーポリシー / Privacy Policy</a></p><p><a href="/calendar-sync">管理者の接続画面 / Administrator connection</a></p>"""
    content += '<h2>お問い合わせ / Contact</h2><p>西天満つきや 朝倉：<a href="mailto:chiyoshiasakura0408@gmail.com">chiyoshiasakura0408@gmail.com</a></p>'
    if privacy:
        content += '<p><a href="/calendar-sync/about">Tsukiya Calendar Syncについて / About</a></p>'
    return '<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>' + title + '</title><style>body{font-family:system-ui,sans-serif;line-height:1.8;color:#222;background:#faf9f6;max-width:800px;margin:40px auto;padding:24px}h1{font-size:28px}h2{font-size:20px;margin-top:32px}a{color:#36577b}p{overflow-wrap:anywhere}</style></head><body><main><h1>' + title + '</h1>' + content + '</main></body></html>'


def handle_get(handler, path, query, db_path, base_url):
    if path in ('/calendar-sync/about', '/calendar-sync/privacy'):
        handler.send_html(public_page(path.endswith('/privacy')), loader=False)
        return True
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
