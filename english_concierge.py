"""Public English enquiries with private, token-protected replies in the browser."""
import hashlib
import re
import time
import unicodedata
import concierge

LIFETIME = 30 * 86400
DUPLICATE_WINDOW = 600


def normalized(value):
    return " ".join(unicodedata.normalize("NFKC", value).replace("\u200b", "").split())


def related(c, row):
    setup(c)
    group = c.execute('SELECT canonical_id FROM english_enquiry_groups WHERE request_id=?', (row['id'],)).fetchone()
    if not group:
        return [row['id']]
    return [r[0] for r in c.execute('SELECT request_id FROM english_enquiry_groups WHERE canonical_id=?', (group[0],))]


def merge(c, ids):
    setup(c)
    if not isinstance(ids,list) or not 2<=len(ids)<=10 or any(not isinstance(x,str) for x in ids) or len(set(ids))!=len(ids):
        raise ValueError('まとめる受付を2〜10件選択してください')
    rows = [c.execute('SELECT r.*,e.peer_hash,e.expires FROM concierge_requests r JOIN english_enquiries e ON e.request_id=r.id WHERE r.id=?', (key,)).fetchone() for key in ids]
    if any(r is None or r['status']!='未回答' or r['expires']<=time.time() for r in rows):
        raise ValueError('未回答の有効な英語受付を選択してください')
    first = min(rows,key=lambda r:r['created'])
    if any(normalized(r['body'])!=normalized(first['body']) or abs(r['created']-first['created'])>DUPLICATE_WINDOW for r in rows):
        raise ValueError('同一内容・短時間の受付のみまとめられます')
    if any(c.execute('SELECT 1 FROM english_enquiry_groups WHERE request_id=?',(r['id'],)).fetchone() for r in rows):
        raise ValueError('既にまとめられた受付があります。再読み込みしてください')
    c.executemany('INSERT INTO english_enquiry_groups(request_id,canonical_id) VALUES(?,?)', [(r['id'],first['id']) for r in rows])
    return {'ok': True}


def grouped_requests(c, requests):
    # Display every original reference, with one staff response per duplicate group.
    seen = set()
    result = []
    for row in requests:
        if row["id"] in seen:
            continue
        if row["id"].startswith("web-") and row["status"] == "未回答":
            ids = related(c, row)
            seen.update(ids)
            row["related_ids"] = ids
            if len(ids)==1:
                candidates = c.execute("SELECT r.id,r.body FROM concierge_requests r JOIN english_enquiries e ON e.request_id=r.id WHERE r.status='未回答' AND abs(r.created-?)<=?", (row['created'],DUPLICATE_WINDOW)).fetchall()
                row['duplicate_candidates'] = [r['id'] for r in candidates if normalized(r['body'])==normalized(row['body'])]
        result.append(row)
    return result


def setup(c):
    c.execute('CREATE TABLE IF NOT EXISTS english_enquiry_groups(request_id TEXT PRIMARY KEY,canonical_id TEXT NOT NULL)')
    c.execute('''CREATE TABLE IF NOT EXISTS english_enquiries (
        token_hash TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
        peer_hash TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
        answer TEXT NOT NULL DEFAULT '')''')


def handle(db, data, peer, base):
    if not isinstance(data, dict):
        raise ValueError('Please check your request.')
    if data.get('action') in ('chat', 'chat-status'):
        import english_chat
        return english_chat.handle(db, data, peer, base)
    token = data.get('token', '')
    if not isinstance(token, str) or not re.fullmatch(r'[0-9a-f]{64}', token):
        raise ValueError('Your private enquiry key is invalid. Please start a new enquiry.')
    key = hashlib.sha256(token.encode()).hexdigest()
    action = data.get('action')
    if action not in ('submit', 'status'):
        raise ValueError('Please check your request.')
    with concierge.LOCK:
        c = concierge.connect(db)
        try:
            with c:
                setup(c)
                c.execute("BEGIN IMMEDIATE")
                row = c.execute('SELECT * FROM english_enquiries WHERE token_hash=?', (key,)).fetchone()
                if row:
                    if row['expires'] <= time.time():
                        return 410, {'error': 'This enquiry link has expired. Please submit a new enquiry.'}
                    return 200, {'id': row['request_id'], 'answer': row['answer'], 'expires': row['expires']}
                if action == 'status':
                    return 404, {'error': 'No enquiry was found for this private link.'}
                name, body = data.get('name', ''), data.get('body', '')
                if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
                    raise ValueError('Please enter your name (up to 100 characters).')
                if not isinstance(body, str) or not 1 <= len(body.strip()) <= 1500:
                    raise ValueError('Please enter your enquiry (up to 1,500 characters).')
                if data.get('consent') is not True:
                    raise ValueError('Please confirm that we may share this enquiry with the restaurant owner.')
                if data.get('website'):
                    raise ValueError('Please check your request.')
                owner = concierge.setting(c, 'owner')
                if not owner:
                    return 503, {'error': 'Personal enquiries are temporarily unavailable. Please try again later.'}
                now = time.time()
                # Use the connection peer, never an untrusted forwarded header. A shared
                # reverse proxy intentionally shares the conservative hourly limit.
                peer_hash = hashlib.sha256(peer.encode()).hexdigest()
                recent = c.execute('SELECT count(*) FROM english_enquiries WHERE peer_hash=? AND created>?', (peer_hash, now-3600)).fetchone()[0]
                total = c.execute('SELECT count(*) FROM english_enquiries WHERE created>?', (now-3600,)).fetchone()[0]
                if recent >= 20 or total >= 100:
                    return 429, {'error': 'We have received many enquiries. Please try again later.'}
                message = '【English Concierge】\n回答言語：英語 / 回答先：英語窓口\nName: ' + name.strip() + '\n' + body.strip()
                candidates = c.execute('SELECT r.body FROM concierge_requests r JOIN english_enquiries e ON e.request_id=r.id WHERE e.created>?', (now-DUPLICATE_WINDOW,))
                if any(normalized(r['body']) == normalized(message) for r in candidates):
                    # Never reveal another private token, reference or answer by content matching.
                    return 409, {'error': 'This enquiry was recently received. Please use your original private reply link to check for our response. No additional enquiry has been created.'}
                request_id = 'web-' + key[:24]
                c.execute('INSERT INTO concierge_requests(id,user_id,body,created) VALUES(?,?,?,?)', (request_id, 'web:'+key, message, now))
                c.execute('INSERT INTO english_enquiries(token_hash,request_id,peer_hash,created,expires) VALUES(?,?,?,?,?)', (key, request_id, peer_hash, now, now+LIFETIME))
                concierge.enqueue(c, owner, [concierge.text_message(message + '\n受付：' + request_id + '\n英語で回答：' + base + '/concierge', False)], channel='owner', kind='request')
                return 200, {'id': request_id, 'answer': '', 'expires': now+LIFETIME}
        finally:
            c.close()


def answer(c, row, body):
    setup(c)
    enquiry = c.execute('SELECT expires FROM english_enquiries WHERE request_id=?', (row['id'],)).fetchone()
    if not enquiry or enquiry['expires'] <= time.time():
        raise ValueError('英語窓口の回答期限（30日）が過ぎています')
    for request_id in related(c, row):
        c.execute('UPDATE english_enquiries SET answer=? WHERE request_id=?', (body.strip(), request_id))
        c.execute("UPDATE concierge_requests SET status='英語窓口に回答掲載済み' WHERE id=?", (request_id,))
