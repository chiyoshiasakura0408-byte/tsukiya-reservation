"""Stock-only beverage inventory; purchasing decisions belong to the owner."""
import hashlib
import json
import os

import concierge
import supplies

ITEMS = [
    '水', 'ウーロン', '緑茶', '炭酸', 'エビス', 'オールフリー',
    'シーバスミズナラ', '角　瓶', '山崎', '白州', 'クエルボ1800',
    'オレンジ', 'ジンジャーエール', 'コーラ', '呉春', 'ヘネシー',
    '十四代本丸', '洗心', '山形政宗', '空', '白ワイン', '赤ワイン', 'シャンパン', '森伊蔵', '兼八', '吉四六',
]
COMMANDS = {'酒類', '酒類在庫', '酒類在庫表', '酒類チェック', '飲料在庫'}
COUNTING = '未開封は本・缶・パックの整数。開封残は本換算（半分＝0.5、複数本は残量の合計）。在庫なしは両方0。'
# User supplied selling prices in yen per serving; missing is never treated as zero.
SALE_PRICES = {2:500,3:500,4:500,5:1000,6:500,7:1000,8:1000,
               12:500,13:500,14:500,15:1000,17:5000,18:3000,19:1000,
               20:2000,21:2000,22:2000,24:1000,25:1000}



def schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS drink_stock (
        branch TEXT NOT NULL, item_id INTEGER NOT NULL, name TEXT NOT NULL,
        qty REAL, checked_at TEXT, checked_by TEXT NOT NULL DEFAULT '',
        version INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(branch,item_id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS drink_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT, branch TEXT NOT NULL, item_id INTEGER NOT NULL,
        at TEXT NOT NULL, actor TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS drink_reports (
        id TEXT PRIMARY KEY, day TEXT NOT NULL, branch TEXT NOT NULL,
        at TEXT NOT NULL, actor TEXT NOT NULL, snapshot TEXT NOT NULL)''')
    columns = {r[1] for r in c.execute('PRAGMA table_info(drink_stock)')}
    for column in ('unopened', 'opened'):
        if column not in columns:
            c.execute(f'ALTER TABLE drink_stock ADD COLUMN {column} REAL')
    for branch in supplies.BRANCHES:
        for item_id, name in enumerate(ITEMS, 1):
            c.execute('INSERT OR IGNORE INTO drink_stock(branch,item_id,name) VALUES(?,?,?)',
                      (branch, item_id, name))


def rows_for(c, branch=None, at=None):
    at = at or supplies.now()
    rows = []
    for row in c.execute('SELECT * FROM drink_stock ORDER BY branch DESC,item_id'):
        if branch is not None and row['branch'] != branch:
            continue
        r = dict(row)
        r['current'] = bool(r['qty'] is not None and r['checked_at'] and r['checked_at'] <= at.isoformat())
        r['split_complete'] = r['unopened'] is not None and r['opened'] is not None
        r['all_in'] = r['item_id'] not in (9,10)
        r['sale_price'] = SALE_PRICES.get(r['item_id'])
        r['current'] = r['current'] and r['split_complete']
        r['status'] = '入力済み' if r['current'] else ('内訳要入力' if r['qty'] is not None else '未入力')
        rows.append(r)
    return rows


def counts(rows):
    checked = sum(r['current'] for r in rows)
    return {'total': len(rows), 'checked': checked, 'missing': len(rows)-checked}


def report_text(rows, actor=''):
    s = counts(rows)
    lines = ['【つきや・酒類在庫報告】', supplies.now().strftime('%Y/%m/%d %H:%M'),
             f'入力済み：{s["checked"]}/{s["total"]}品目／未入力 {s["missing"]}品目',
             '発注は朝倉が判断します。', '数量は本・缶・パックの個数（開封済みは残量換算）。']
    if actor:
        lines.append('確認者：' + actor)
    for branch, name in supplies.BRANCHES.items():
        items = [r for r in rows if r['branch'] == branch]
        if not items:
            continue
        lines.extend(['', '■ ' + name])
        for r in items:
            quantity = '未入力' if r['qty'] is None else f'{r["qty"]:g}'
            if r['split_complete']:
                quantity += f'本換算（未開封 {r["unopened"]:g}／開封残 {r["opened"]:g}）'
            elif r['qty'] is not None:
                quantity += '本換算・旧記録／内訳要入力'
            if r['qty'] is not None and r['checked_at']:
                quantity += '（確認 ' + r['checked_at'][:16].replace('T', ' ') + '）'
            lines.append('・' + r['name'] + '：' + quantity)
    return '\n'.join(lines)


def view(db):
    c = supplies.connect(db)
    try:
        rows = rows_for(c)
        _, owner, group = supplies.recipient_status(c)
        notices = [dict(r) for r in c.execute('''SELECT n.key,o.state,o.error,n.created
            FROM supply_notices n JOIN concierge_outbox o ON n.outbox_id=o.id
            WHERE n.key LIKE 'drinks:report:%' ORDER BY n.created DESC LIMIT 8''')]
        return {'items': rows, 'counting': COUNTING,
                'summary': {b: counts([r for r in rows if r['branch'] == b]) for b in supplies.BRANCHES},
                'preview': {b: report_text([r for r in rows if r['branch'] == b]) for b in supplies.BRANCHES},
                'owner_linked': owner, 'group_linked': group, 'notices': notices}
    finally:
        c.close()


def save(db, data):
    if not isinstance(data, dict) or data.get('branch') not in supplies.BRANCHES:
        raise ValueError('店舗を選択してください')
    actor = supplies.short_text(data.get('actor'), 40)
    if not actor:
        raise ValueError('確認者のお名前を入力してください')
    items = data.get('items')
    if not isinstance(items, list) or not 1 <= len(items) <= len(ITEMS):
        raise ValueError('更新する品目を選択してください')
    c = supplies.connect(db)
    try:
        with c:
            c.execute('BEGIN IMMEDIATE')
            seen = set()
            for patch in items:
                if (not isinstance(patch, dict) or set(patch) not in ({'item_id', 'version', 'qty'}, {'item_id', 'version', 'unopened', 'opened'})
                        or type(patch['item_id']) is not int or patch['item_id'] in seen):
                    raise ValueError('酒類は在庫数のみ入力できます')
                seen.add(patch['item_id'])
                row = c.execute('SELECT * FROM drink_stock WHERE branch=? AND item_id=?',
                                (data['branch'], patch['item_id'])).fetchone()
                if not row or type(patch['version']) is not int or patch['version'] != row['version']:
                    raise supplies.Conflict('他の担当者が更新しました。再読み込みして確認してください')
                unopened = opened = None
                if 'qty' in patch:
                    quantity = supplies.number(patch['qty'])
                else:
                    unopened = supplies.number(patch['unopened'])
                    opened = supplies.number(patch['opened'])
                    if unopened is not None and unopened != int(unopened):
                        raise ValueError('未開封本数は整数で入力してください')
                    if (unopened is None) != (opened is None):
                        raise ValueError('未開封本数と開封残を両方入力してください。在庫なしは0です')
                    quantity = supplies.number(round(unopened + opened, 3)) if unopened is not None else None
                at = supplies.now().isoformat()
                result = {**dict(row), 'qty': quantity, 'unopened': unopened, 'opened': opened, 'checked_at': at if quantity is not None else None,
                          'checked_by': actor if quantity is not None else '', 'version': row['version']+1}
                c.execute('''UPDATE drink_stock SET qty=?,unopened=?,opened=?,checked_at=?,checked_by=?,version=?
                    WHERE branch=? AND item_id=?''', (result['qty'], unopened, opened, result['checked_at'], result['checked_by'],
                    result['version'], data['branch'], patch['item_id']))
                c.execute('INSERT INTO drink_audit(branch,item_id,at,actor,before_json,after_json) VALUES(?,?,?,?,?,?)',
                          (data['branch'], patch['item_id'], at, actor, json.dumps(dict(row), ensure_ascii=False),
                           json.dumps(result, ensure_ascii=False)))
        return {'ok': True}
    finally:
        c.close()


def submit(db, data, base_url):
    if not isinstance(data, dict) or data.get('branch') not in supplies.BRANCHES:
        raise ValueError('店舗を選択してください')
    actor = supplies.short_text(data.get('actor'), 40)
    if not actor:
        raise ValueError('確認者のお名前を入力してください')
    c = supplies.connect(db)
    try:
        with c:
            c.execute('BEGIN IMMEDIATE')
            owner, ready, _ = supplies.recipient_status(c)
            if not ready or not os.getenv('LINE_CHANNEL_ACCESS_TOKEN'):
                raise ValueError('朝倉LINEの連携を確認してください。在庫は保存されています')
            rows = rows_for(c, data['branch'])
            snapshot = json.dumps([{k: v for k, v in r.items() if k != 'version'} for r in rows],
                                  sort_keys=True, ensure_ascii=False)
            day = supplies.now().date().isoformat()
            key = 'drinks:report:' + day + ':' + data['branch'] + ':' + hashlib.sha256(snapshot.encode()).hexdigest()
            old = c.execute('SELECT outbox_id FROM supply_notices WHERE key=?', (key,)).fetchone()
            if old:
                state = c.execute('SELECT state FROM concierge_outbox WHERE id=?', (old[0],)).fetchone()[0]
                return {'ok': True, 'already_queued': True, 'state': state}
            link = supplies.new_link(c, base_url, page='drinks')
            text = report_text(rows, actor) + '\n\n酒類在庫表：\n' + link
            supplies.queue(c, key, owner, supplies.split_messages(text))
            c.execute('INSERT INTO drink_reports VALUES(?,?,?,?,?,?)',
                      (key, day, data['branch'], supplies.now().isoformat(), actor, snapshot))
        return {'ok': True, 'state': 'pending'}
    finally:
        c.close()
