"""Staff-entered bottle and serving volumes; no costing or automatic inference."""
import math
import supplies
import drinks


def connect(db):
    c = supplies.connect(db)
    c.execute('''CREATE TABLE IF NOT EXISTS drink_capacity (
        branch TEXT NOT NULL, item_id INTEGER NOT NULL, bottle_ml REAL,
        serving_ml REAL, whole INTEGER NOT NULL DEFAULT 0,
        actor TEXT NOT NULL, updated TEXT NOT NULL, version INTEGER NOT NULL,
        PRIMARY KEY(branch,item_id))''')
    c.commit()
    return c


def view(db):
    c = connect(db)
    try:
        saved = {(r['branch'], r['item_id']): dict(r) for r in c.execute('SELECT * FROM drink_capacity')}
        return {'items': [{**{k:r[k] for k in ('branch','item_id','name')},
                           'bottle_ml': None, 'serving_ml': None, 'whole': 0,
                           'version': 0, 'actor': '', 'updated': '',
                           **saved.get((r['branch'],r['item_id']), {})}
                          for r in drinks.rows_for(c)]}
    finally:
        c.close()


def volume(x):
    if x is None or x == '':
        return None
    if type(x) not in (int, float) or not math.isfinite(x) or not 0 < x <= 100000:
        raise ValueError('容量・提供量は0より大きい数値をmlで入力してください')
    return float(x)


def save(db, data):
    if not isinstance(data, dict) or data.get('branch') not in supplies.BRANCHES:
        raise ValueError('店舗を選んでください')
    actor = supplies.short_text(data.get('actor'),40)
    if not actor:
        raise ValueError('入力者のお名前を入力してください')
    items = data.get('items')
    if not isinstance(items,list) or not 1 <= len(items) <= len(drinks.ITEMS):
        raise ValueError('入力する商品を選んでください')
    c = connect(db)
    try:
        with c:
            c.execute('BEGIN IMMEDIATE')
            seen = set()
            for p in items:
                if not isinstance(p,dict) or set(p) != {'item_id','version','bottle_ml','serving_ml','whole'}:
                    raise ValueError('入力内容を確認してください')
                item = p['item_id']
                if type(item) is not int or item not in range(1,len(drinks.ITEMS)+1) or item in seen:
                    raise ValueError('商品を確認してください')
                seen.add(item)
                row = c.execute('SELECT version FROM drink_capacity WHERE branch=? AND item_id=?',(data['branch'],item)).fetchone()
                if type(p['version']) is not int or p['version'] != (row['version'] if row else 0):
                    raise supplies.Conflict('他の担当者が更新しました。再読み込みして確認してください')
                if type(p['whole']) is not bool:
                    raise ValueError('提供方法を選んでください')
                bottle = volume(p['bottle_ml'])
                serving = bottle if p['whole'] else volume(p['serving_ml'])
                if bottle is not None and serving is not None and serving > bottle:
                    raise ValueError('1杯の提供量が容器容量を超えています')
                c.execute('''INSERT INTO drink_capacity VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(branch,item_id) DO UPDATE SET bottle_ml=excluded.bottle_ml,
                    serving_ml=excluded.serving_ml,whole=excluded.whole,actor=excluded.actor,
                    updated=excluded.updated,version=excluded.version''',
                    (data['branch'],item,bottle,serving,int(p['whole']),actor,supplies.now().isoformat(),p['version']+1))
        return {'ok':True}
    finally:
        c.close()
