"""LINE-only approval and browser proof, bound to signed webhook identities."""
import hashlib
import json
import re
import secrets
import time
from datetime import datetime, timezone


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
