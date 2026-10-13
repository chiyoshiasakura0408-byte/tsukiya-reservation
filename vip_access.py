"""LINE-only approval and browser proof, bound to signed webhook identities."""
import hashlib
import json
import re
import secrets
import time
from datetime import datetime, timezone, timedelta


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def schema(c):
    fresh=not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='vip_access'").fetchone()
    c.executescript('''
    CREATE TABLE IF NOT EXISTS vip_access (
      user_id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'pending',
      name TEXT NOT NULL, introducer TEXT NOT NULL, customer_id INTEGER,
      updated REAL NOT NULL, decided_by TEXT);
    CREATE TABLE IF NOT EXISTS vip_browser (
      hash TEXT PRIMARY KEY, proposal_hash TEXT NOT NULL, user_id TEXT NOT NULL,
      code_hash TEXT UNIQUE NOT NULL, expires REAL NOT NULL, verified INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS vip_decisions (
      hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, decision TEXT NOT NULL,
      customer_id INTEGER, expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS vip_invites (
      hash TEXT PRIMARY KEY, name TEXT NOT NULL, expires REAL NOT NULL,
      used_by TEXT, created REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS vip_invite_deliveries (
      invite_hash TEXT PRIMARY KEY, delivery_id TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS vip_audit (
      id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, decision TEXT NOT NULL,
      actor TEXT NOT NULL, created REAL NOT NULL);
    ''')
    if fresh:
        c.execute("UPDATE concierge_outbox SET state='cancelled' WHERE channel='customer' AND kind IN ('reply','answer','announcement') AND state='pending'")


def approved(c, user):
    row = c.execute("SELECT 1 FROM vip_access a JOIN concierge_customers u ON u.user_id=a.user_id WHERE a.user_id=? AND a.status='approved' AND u.active=1 AND a.customer_id=u.customer_id", (user,)).fetchone()
    return bool(row)


def gate(c, user, command, base_url):
    import concierge as bot
    if user.startswith(('ig:', 'web:')):
        return None
    if command in ('退会','VIP退会','退会する'):
        c.execute('UPDATE concierge_customers SET state=? WHERE user_id=?',('withdraw:'+str(time.time()+600),user))
        message=bot.text_message('VIPコンシェルジュを退会しますか？退会するとVIP利用と配信を停止します。確定済みのご予約はキャンセルされず、顧客情報・来店履歴は残ります。',False)
        message['quickReply']={'items':[{'type':'action','action':{'type':'message','label':label,'text':text}} for label,text in [('退会を確定','VIP退会を確定'),('戻る','VIP退会をやめる')]]}
        return [message]
    if command=='VIP退会をやめる':
        c.execute("UPDATE concierge_customers SET state='{}' WHERE user_id=?",(user,))
        return [bot.text_message('退会を取りやめました。',False)]
    if command=='VIP退会を確定':
        state=c.execute('SELECT state FROM concierge_customers WHERE user_id=?',(user,)).fetchone()[0]
        try: valid=state.startswith('withdraw:') and float(state[9:])>time.time()
        except ValueError: valid=False
        if not valid:return [bot.text_message('「退会」から、もう一度確認をお願いいたします。',False)]
        c.execute("UPDATE vip_access SET status='revoked',updated=?,decided_by='self-withdrawal' WHERE user_id=?",(time.time(),user))
        c.execute("UPDATE concierge_customers SET stopped=1,subscribed=0,state='{}' WHERE user_id=?",(user,))
        c.execute('DELETE FROM vip_browser WHERE user_id=?',(user,))
        c.execute('UPDATE vip_decisions SET used=1 WHERE user_id=?',(user,))
        c.execute("UPDATE concierge_outbox SET state='cancelled' WHERE user_id=? AND channel='customer' AND state='pending'",(user,))
        c.execute("INSERT INTO vip_audit(user_id,decision,actor,created) VALUES(?,'revoked','self-withdrawal',?)",(user,time.time()))
        return [bot.text_message('退会を受け付けました。VIP利用と配信を停止しました。確定済みのご予約はそのまま承っております。これまでのご利用ありがとうございました。',False)]
    if command.startswith('VIP招待 '):
        return redeem_invite(c,user,command.removeprefix('VIP招待 ').strip(),base_url)
    if command.startswith('予約本人確認 '):
        code = command.removeprefix('予約本人確認 ').strip()
        ok = approved(c, user) and c.execute('UPDATE vip_browser SET verified=1 WHERE code_hash=? AND user_id=? AND expires>? AND verified=0', (digest(code), user, time.time())).rowcount
        return [bot.text_message('本人確認が完了しました。元の予約画面で「確認して進む」を押してください。' if ok else '確認できませんでした。ご本人の予約画面に表示された有効な確認コードをお送りください。', False)]
    if approved(c, user):
        return None
    if command == '配信停止':
        c.execute('UPDATE concierge_customers SET stopped=1,subscribed=0 WHERE user_id=?', (user,))
        return [bot.text_message('配信を停止しました。', False)]
    row = c.execute('SELECT * FROM vip_access WHERE user_id=?', (user,)).fetchone()
    state = c.execute('SELECT state FROM concierge_customers WHERE user_id=?', (user,)).fetchone()[0]
    if command == '利用申請':
        if row:
            return [bot.text_message('申請は受付済みです。承認状況は店舗へお問い合わせください。', False)]
        c.execute("UPDATE concierge_customers SET state='vip-name' WHERE user_id=?", (user,))
        return [bot.text_message('ご本人のお名前（フルネーム）をお送りください。', False)]
    if state == 'vip-name' and command and len(command) <= 80:
        c.execute('UPDATE concierge_customers SET state=? WHERE user_id=?', ('vip-ref:'+json.dumps(command, ensure_ascii=False), user))
        return [bot.text_message('ご紹介者のお名前をお送りください。ご紹介者がいない場合は「なし」とお送りください。', False)]
    if state.startswith('vip-ref:') and command and len(command) <= 150 and not row:
        name = json.loads(state[len('vip-ref:'):])
        c.execute('INSERT INTO vip_access(user_id,name,introducer,updated) VALUES(?,?,?,?)', (user,name,command,time.time()))
        c.execute("UPDATE concierge_customers SET state='{}' WHERE user_id=?", (user,))
        return [bot.text_message('利用申請を受け付けました。店舗の確認・承認をお待ちください。',False)]
    status = {'pending':'確認中','rejected':'利用未許可','revoked':'利用停止'}.get(row['status'],'利用未許可') if row else ''
    return [bot.text_message('こちらは承認制のVIPコンシェルジュです。'+('現在：'+status+'。店舗へお問い合わせください。' if row else 'ご利用を希望される方は「利用申請」とお送りください。'),False)]


def begin_browser(c, proposal, browser):
    if not approved(c, proposal['user_id']):
        raise PermissionError('VIP利用の承認が必要です。LINEより店舗へお問い合わせください。')
    now=time.time()
    c.execute('DELETE FROM vip_browser WHERE expires<?',(now,))
    if browser:
        row=c.execute('SELECT * FROM vip_browser WHERE hash=? AND proposal_hash=? AND user_id=? AND expires>?',(digest(browser),proposal['hash'],proposal['user_id'],now)).fetchone()
        if row:
            return {'verified':bool(row['verified']), **({} if row['verified'] else {'command':'予約本人確認 '+digest('line-proof:'+browser+proposal['hash'])[:20]})},None
    # Limit a leaked proposal's challenge creation without invalidating an existing browser.
    if c.execute('SELECT count(*) FROM vip_browser WHERE proposal_hash=?',(proposal['hash'],)).fetchone()[0]>=10:
        raise ValueError('本人確認の試行が多いため、10分後に再度お試しください。')
    browser=secrets.token_urlsafe(32);code=digest('line-proof:'+browser+proposal['hash'])[:20]
    c.execute('INSERT INTO vip_browser VALUES(?,?,?,?,?,0)',(digest(browser),proposal['hash'],proposal['user_id'],digest(code),now+600))
    return {'verified':False,'command':'予約本人確認 '+code,'expires_in':600},browser


def authorize_browser(c, proposal, browser):
    if not approved(c,proposal['user_id']) or not isinstance(browser,str) or not c.execute('SELECT 1 FROM vip_browser WHERE hash=? AND proposal_hash=? AND user_id=? AND verified=1 AND expires>?',(digest(browser),proposal['hash'],proposal['user_id'],time.time())).fetchone():
        raise PermissionError('ご本人のLINEから本人確認を行ってください。')


def request_decision(c, data, base_url):
    import concierge as bot
    user=data.get('user_id');decision=data.get('decision');cid=data.get('customer_id')
    row=c.execute('SELECT * FROM vip_access WHERE user_id=?',(user,)).fetchone()
    if not row or decision not in ('approved','rejected','revoked'):
        raise ValueError('申請と操作を確認してください。')
    if decision=='approved':
        if type(cid) is not int or (cid != 0 and not c.execute('SELECT 1 FROM customers WHERE id=?',(cid,)).fetchone()):
            raise ValueError('ご本人と確認した顧客を選択してください。')
    else: cid=row['customer_id']
    if row['status']==decision and (decision!='approved' or cid==row['customer_id']):
        return {'ok':True,'message':'すでに同じ状態です。'}
    c.execute('UPDATE vip_decisions SET used=1 WHERE user_id=? AND used=0',(user,))
    c.execute("UPDATE concierge_outbox SET state='cancelled' WHERE channel='owner' AND kind IN ('vip-access','vip-decision') AND state='pending'")
    row={'user_id':user,'decision':decision,'customer_id':cid}
    if row['decision']=='approved' and row['customer_id']==0:
        applicant=c.execute('SELECT name FROM vip_access WHERE user_id=?',(row['user_id'],)).fetchone()
        now=datetime.now(timezone.utc).isoformat()
        c.execute('INSERT OR IGNORE INTO customers(match_key,name,created_at,updated_at) VALUES(?,?,?,?)',('vip:'+row['user_id'],applicant[0],now,now))
        row['customer_id']=c.execute('SELECT id FROM customers WHERE match_key=?',('vip:'+row['user_id'],)).fetchone()[0]
    c.execute('UPDATE vip_access SET status=?,customer_id=?,updated=?,decided_by=? WHERE user_id=?',(row['decision'],row['customer_id'],time.time(),'admin',row['user_id']))
    if row['decision']=='approved':
        c.execute('UPDATE concierge_customers SET customer_id=?,state=\'{}\' WHERE user_id=?',(row['customer_id'],row['user_id']))
    c.execute('DELETE FROM vip_browser WHERE user_id=?',(row['user_id'],))
    c.execute("UPDATE concierge_outbox SET state='cancelled' WHERE user_id=? AND channel='customer' AND state='pending'",(row['user_id'],))
    c.execute('INSERT INTO vip_audit(user_id,decision,actor,created) VALUES(?,?,?,?)',(row['user_id'],row['decision'],'admin',time.time()))
    text='VIPコンシェルジュのご利用を承認いたしました。「メニュー」とお送りください。' if row['decision']=='approved' else 'VIPコンシェルジュは現在ご利用いただけません。詳しくは店舗へお問い合わせください。'
    bot.enqueue(c,row['user_id'],[bot.text_message(text,False)],kind='vip-access')
    return {'ok':True,'message':{'approved':'VIP利用を許可しました。','rejected':'申請を却下しました。','revoked':'VIP利用許可を取り消しました。'}[decision]}


def owner_commands(c, events):
    # Retired: decisions are made only through the authenticated management route.
    return None


def create_invite(c, data):
    from urllib.parse import quote
    name=data.get('name')
    if not isinstance(name,str) or not 1<=len(name.strip())<=80:
        raise ValueError('お客様のお名前を入力してください。')
    token=secrets.token_urlsafe(32)
    now=time.time()
    c.execute('INSERT INTO vip_invites(hash,name,expires,created) VALUES(?,?,?,?)',
              (digest(token),name.strip(),now+86400,now))
    command='VIP招待 '+token
    return {'url':'https://line.me/R/oaMessage/%40387reour/?'+quote(command,safe=''),
            'command':command,'expires_in':86400,
            'message':'24時間有効・1回限り。お客様がLINEで招待文を送信すると自動承認されます。'}


def redeem_invite(c,user,token,base_url):
    import concierge as bot
    invite=c.execute('SELECT * FROM vip_invites WHERE hash=? AND expires>?',
                     (digest(token),time.time())).fetchone()
    if not invite or invite['used_by']:
        return [bot.text_message('この招待は使用済み、または有効期限が切れています。店舗へ新しい招待をご依頼ください。',False)]
    if not c.execute('UPDATE vip_invites SET used_by=? WHERE hash=? AND used_by IS NULL AND expires>?',
                     (user,digest(token),time.time())).rowcount:
        return [bot.text_message('この招待は使用済みです。',False)]
    existing=c.execute('SELECT * FROM vip_access WHERE user_id=?',(user,)).fetchone()
    if approved(c,user):
        return [bot.text_message('すでにVIPコンシェルジュをご利用いただけます。「メニュー」とお送りください。',False)]
    if not existing:
        c.execute('INSERT INTO vip_access(user_id,name,introducer,updated) VALUES(?,?,?,?)',
                  (user,invite['name'],'朝倉（専用招待）',time.time()))
    cid=existing['customer_id'] if existing and existing['customer_id'] else 0
    request_decision(c,{'user_id':user,'decision':'approved','customer_id':cid},base_url)
    c.execute("UPDATE vip_access SET decided_by='admin-invite' WHERE user_id=?",(user,))
    c.execute("UPDATE vip_audit SET actor='admin-invite' WHERE id=(SELECT max(id) FROM vip_audit WHERE user_id=?)",(user,))
    return []


def invite_details(c, token, base_url):
    from urllib.parse import quote
    if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
        raise ValueError('招待を確認できません。新しく発行してください。')
    row = c.execute('SELECT * FROM vip_invites WHERE hash=? AND expires>? AND used_by IS NULL',
                    (digest(token), time.time())).fetchone()
    if not row:
        raise ValueError('招待は使用済み、または期限切れです。新しく発行してください。')
    url = 'https://line.me/R/oaMessage/%40387reour/?' + quote('VIP招待 '+token, safe='')
    text = (row['name']+'様\n\n西天満つきやのVIPコンシェルジュへご招待いたします。\n'
            '空席のご案内やご予約、ご来店に関するご相談にご利用ください。\n\n'
            '下記リンク、またはQRコードからLINEを開き、入力済みの招待文をそのまま送信してください。'
            '自動でご利用登録が完了いたします。\n\n'+url+
            '\n\n有効期限：'+datetime.fromtimestamp(row['expires'], timezone(timedelta(hours=9))).strftime('%Y年%m月%d日 %H:%M')+
            '（日本時間・1回限り）\nご本人様専用のため、転送はお控えください。\n\n西天満つきや　朝倉')
    return {'url':url, 'invitation_text':text,
            'qr_url':base_url.rstrip('/')+'/vip-invite-qr?token='+token}


def invite_qr(c, token):
    import io
    import qrcode
    details = invite_details(c, token, '')
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=4)
    qr.add_data(details['url']); qr.make(fit=True)
    stream = io.BytesIO()
    qr.make_image(fill_color='black', back_color='white').save(stream, format='PNG')
    return stream.getvalue()


def send_invite_to_owner(c, token, base_url):
    import os
    import concierge as bot
    from urllib.parse import urlparse
    details = invite_details(c, token, base_url)
    owner = bot.setting(c, 'owner')
    if not re.fullmatch(r'U[0-9a-fA-F]{32}', owner or ''):
        raise ValueError('朝倉さんの個人LINEを本人連携してください。グループには送信しません。')
    if not os.getenv('LINE_CHANNEL_ACCESS_TOKEN'):
        raise ValueError('業務LINEの接続設定を確認してください。')
    parsed = urlparse(base_url)
    if parsed.scheme != 'https' or not parsed.netloc:
        raise ValueError('招待QRコードの公開URLを確認してください。')
    existing = c.execute('SELECT delivery_id FROM vip_invite_deliveries WHERE invite_hash=?', (digest(token),)).fetchone()
    if existing:
        delivery_id = existing['delivery_id']
    else:
        delivery_id = bot.enqueue(c, owner, [
            {'type':'text','text':details['invitation_text']},
            {'type':'image','originalContentUrl':details['qr_url'],'previewImageUrl':details['qr_url']}
        ], channel='owner', kind='vip-invite')
        c.execute('INSERT INTO vip_invite_deliveries VALUES(?,?)',(digest(token),delivery_id))
    row = c.execute('SELECT state FROM concierge_outbox WHERE id=?',(delivery_id,)).fetchone()
    return {'delivery_id':delivery_id, 'state':row['state'] if row else 'unknown'}
