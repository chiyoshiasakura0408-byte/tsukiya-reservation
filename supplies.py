"""Staff-only supplies counts, weekly checks and durable LINE notifications."""
import hashlib
import json
import math
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

import concierge
import line_delivery

JST = timezone(timedelta(hours=9))
LOCK = threading.RLock()
BRANCHES = {'main': '本店', 'annex': '別邸'}
ITEMS = [
    'ハイターE（青）', 'ワイドハイター（緑）', 'キュキュット', 'アタック（洗濯）',
    'タオルペーパー', '大ラップ（黄）', 'ミニラップ（赤）', 'ミニミニラップ（緑）',
    'リード', 'スポンジ（黄）', 'スポンジ（赤）', '柔軟剤（ハミング）', '柔軟剤（ファーファ）',
    'トイレクイックル', 'トイレマジックリン', 'トイレ消臭力', 'ガスボンベ', 'お香（白川）',
    'ゴミ袋90L', 'トイレットペーパー', 'ティッシュペーパー', 'ボンスター', '黒文字楊枝',
    'コピー用紙', '箸', 'マウスウォッシュ', '歯間ブラシ', '除菌シート', '竹皮', '紙袋',
    '会計票', 'ストロー', 'コロコロのロール', 'ニトリル黒手袋', 'ハンドソープ（ミューズ）',
]
SOURCE = 'https://www.icloud.com/numbers/037u3gBe1Z3_WctReue8iLmcw'
COMMANDS = {'備品', '備品管理', '備品在庫', '備品チェック'}


class Conflict(ValueError):
    pass


def now():
    return datetime.now(JST)


def week_start(at=None):
    day = (at or now()).date()
    return (day - timedelta(days=day.weekday())).isoformat()


def schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS supply_stock (
        branch TEXT NOT NULL, item_id INTEGER NOT NULL, name TEXT NOT NULL,
        unit TEXT NOT NULL DEFAULT '', qty REAL, reorder REAL, target REAL,
        pending REAL NOT NULL DEFAULT 0, manual_need INTEGER NOT NULL DEFAULT 0,
        requested REAL, applicable INTEGER NOT NULL DEFAULT 1, note TEXT NOT NULL DEFAULT '',
        supplier TEXT NOT NULL DEFAULT '', order_state TEXT NOT NULL DEFAULT '未発注',
        checked_at TEXT, checked_by TEXT NOT NULL DEFAULT '', version INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(branch,item_id))''')
    c.execute('CREATE TABLE IF NOT EXISTS supply_access(hash TEXT PRIMARY KEY, expires REAL NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS supply_notices(key TEXT PRIMARY KEY, outbox_id TEXT NOT NULL, created REAL NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS supply_audit(id INTEGER PRIMARY KEY AUTOINCREMENT, branch TEXT NOT NULL, item_id INTEGER NOT NULL, at TEXT NOT NULL, actor TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS supply_reports(id TEXT PRIMARY KEY, week TEXT NOT NULL, branch TEXT NOT NULL, at TEXT NOT NULL, actor TEXT NOT NULL, snapshot TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS supply_commands(event_id TEXT PRIMARY KEY, at REAL NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS supply_state(key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    for branch in BRANCHES:
        for i, name in enumerate(ITEMS, 1):
            note = '' if i in (4, 27, 31, 32) else '未開封分を数えてください'
            if i == 28:
                note = '元表では別邸のみ。未開封分を数えてください'
            c.execute('INSERT OR IGNORE INTO supply_stock(branch,item_id,name,note,applicable) VALUES(?,?,?,?,?)',
                      (branch, i, name, note, int(not (branch == 'main' and i == 28))))


def connect(db):
    c = concierge.connect(db)
    schema(c)
    c.commit()
    return c


def enabled(c):
    row = c.execute("SELECT value FROM supply_state WHERE key='enabled'").fetchone()
    return row is None or row[0] == '1'


def recipient_status(c):
    owner = concierge.setting(c, 'owner')
    table = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='line_sources'").fetchone()
    group = bool(table and c.execute("SELECT 1 FROM line_sources WHERE source_id=? AND source_type='group' AND active=1", (line_delivery.TARGET,)).fetchone())
    return owner, bool(re.fullmatch(r'U[0-9a-fA-F]{32}', owner)), group


def new_link(c, base_url):
    if not base_url.startswith('https://'):
        raise ValueError('公開URLを確認してください')
    token = secrets.token_urlsafe(32)
    c.execute('DELETE FROM supply_access WHERE expires<?', (time.time(),))
    c.execute('INSERT INTO supply_access VALUES(?,?)', (hashlib.sha256(token.encode()).hexdigest(), time.time() + 8 * 86400))
    # Fragment is not sent to the web server or included in referrer URLs.
    return base_url.rstrip('/') + '/supplies#access=' + token


def authorized(db, token):
    if not isinstance(token, str) or not 40 <= len(token) <= 80:
        return False
    c = connect(db)
    try:
        return bool(c.execute('SELECT 1 FROM supply_access WHERE hash=? AND expires>?', (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone())
    finally:
        c.close()


def classify(row, week):
    r = dict(row)
    current = bool(r['checked_at'] and r['checked_at'][:10] >= week and r['checked_at'] <= now().isoformat())
    recommend = None
    if not r['applicable']:
        status = '対象外'
    elif r['qty'] is None:
        status = '未確認'
    elif r['manual_need']:
        status, recommend = '要発注', r['requested']
    elif r['reorder'] is None or r['target'] is None:
        status = '基準未設定'
    elif r['qty'] <= r['reorder']:
        recommend = max(0, round(r['target'] - r['qty'] - r['pending'], 3))
        status = '要発注' if recommend > 0 else '入荷待ち'
    else:
        status, recommend = '在庫あり', 0
    return {**r, 'current': current, 'status': status, 'recommend': recommend}


def rows_for(c, branch=None, at=None):
    rows = c.execute('SELECT * FROM supply_stock ORDER BY branch DESC,item_id')
    return [classify(r, week_start(at)) for r in rows if branch is None or r['branch'] == branch]


def counts(rows):
    active = [r for r in rows if r['applicable']]
    return {'total': len(active), 'checked': sum(r['current'] for r in active),
            'missing': sum(not r['current'] for r in active),
            'needs': sum(r['status'] == '要発注' for r in active),
            'unconfigured': sum(r['status'] == '基準未設定' for r in active)}


def view(db):
    c = connect(db)
    try:
        rows = rows_for(c)
        _, owner, group = recipient_status(c)
        notices = [dict(r) for r in c.execute('''SELECT n.key,o.state,o.error,n.created
            FROM supply_notices n JOIN concierge_outbox o ON n.outbox_id=o.id ORDER BY n.created DESC LIMIT 8''')]
        return {'items': rows, 'week': week_start(), 'summary': {b: counts([r for r in rows if r['branch'] == b]) for b in BRANCHES},
                'enabled': enabled(c), 'owner_linked': owner, 'group_linked': group,
                'schedule': '毎週土曜 朝の経理LINE（9:00）で確認依頼／日曜23:00 朝倉へ集計（日本時間）', 'notices': notices,
                'source': SOURCE, 'version': 1}
    finally:
        c.close()


def number(value, nullable=True):
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not 0 <= value <= 1000000:
        raise ValueError('数量は0以上100万以下の数値で入力してください')
    return round(value, 3)


def short_text(value, limit):
    if not isinstance(value, str) or len(value.strip()) > limit or any(ord(ch) < 32 for ch in value):
        raise ValueError('入力文字をご確認ください')
    return value.strip()


def save(db, data):
    if not isinstance(data, dict) or data.get('branch') not in BRANCHES:
        raise ValueError('店舗を選択してください')
    actor = short_text(data.get('actor'), 40)
    if not actor:
        raise ValueError('確認者のお名前を入力してください')
    items = data.get('items')
    if not isinstance(items, list) or not 1 <= len(items) <= 35:
        raise ValueError('更新する備品を選択してください')
    c = connect(db)
    try:
        with c:
            c.execute('BEGIN IMMEDIATE')
            seen = set()
            for patch in items:
                if not isinstance(patch, dict) or type(patch.get('item_id')) is not int or patch['item_id'] in seen:
                    raise ValueError('品目の指定が不正です')
                seen.add(patch['item_id'])
                row = c.execute('SELECT * FROM supply_stock WHERE branch=? AND item_id=?', (data['branch'], patch['item_id'])).fetchone()
                if not row or type(patch.get('version')) is not int or patch['version'] != row['version']:
                    raise Conflict('他の担当者が更新しました。再読み込みして確認してください')
                result = dict(row)
                for key in ('qty', 'reorder', 'target', 'pending', 'requested'):
                    if key in patch:
                        result[key] = number(patch[key], key != 'pending')
                for key, limit in [('unit', 12), ('note', 200), ('supplier', 80), ('order_state', 10)]:
                    if key in patch:
                        result[key] = short_text(patch[key], limit)
                for key in ('manual_need', 'applicable'):
                    if key in patch:
                        if type(patch[key]) is not bool:
                            raise ValueError('選択内容が不正です')
                        result[key] = int(patch[key])
                if result['order_state'] not in ('未発注', '承認待ち', '発注済', '入荷済'):
                    raise ValueError('発注状況を選択してください')
                if result['target'] is not None and result['reorder'] is not None and result['target'] < result['reorder']:
                    raise ValueError('目標在庫は発注点以上にしてください')
                if patch.get('confirm') is True:
                    if result['applicable'] and result['qty'] is None:
                        raise ValueError('在庫数を入力してから確認済みにしてください')
                    result['checked_at'], result['checked_by'] = now().isoformat(), actor
                result['version'] += 1
                keys = [k for k in result if k not in ('branch', 'item_id', 'name')]
                c.execute('UPDATE supply_stock SET ' + ','.join(k + '=?' for k in keys) + ' WHERE branch=? AND item_id=?',
                          [result[k] for k in keys] + [data['branch'], patch['item_id']])
                c.execute('INSERT INTO supply_audit(branch,item_id,at,actor,before_json,after_json) VALUES(?,?,?,?,?,?)',
                          (data['branch'], patch['item_id'], now().isoformat(), actor, json.dumps(dict(row), ensure_ascii=False), json.dumps(result, ensure_ascii=False)))
        return {'ok': True}
    finally:
        c.close()


def queue(c, key, recipient, messages):
    old = c.execute('SELECT outbox_id FROM supply_notices WHERE key=?', (key,)).fetchone()
    if old:
        return old[0]
    oid = concierge.enqueue(c, recipient, messages, channel='owner', kind='supplies')
    c.execute('INSERT INTO supply_notices VALUES(?,?,?)', (key, oid, time.time()))
    return oid


def split_messages(text):
    parts, part = [], ''
    for line in text.split('\n'):
        if len((part + line).encode('utf-16-le')) // 2 > 4200:
            parts.append({'type': 'text', 'text': part.rstrip()})
            part = ''
        part += line + '\n'
    if part:
        parts.append({'type': 'text', 'text': part.rstrip()})
    if len(parts) > 5:
        raise ValueError('報告が長すぎます')
    return parts


def report_text(rows, actor='', week=None):
    summary = counts(rows)
    lines = ['【つきや・備品確認報告】', f'{week or week_start()}の週',
             f'確認：{summary["checked"]}/{summary["total"]}品目／未確認 {summary["missing"]}品目',
             f'要発注 {summary["needs"]}品目／基準未設定 {summary["unconfigured"]}品目']
    if actor:
        lines.append('確認者：' + actor)
    for r in rows:
        if r['status'] == '要発注':
            quantity = f'{r["recommend"]:g}{r["unit"]}' if r['recommend'] is not None else '数量未定'
            current = '' if r['current'] else '【今週未確認】'
            lines.extend(['', f'・{BRANCHES[r["branch"]]}／{r["name"]}{current}',
                          f'在庫 {r["qty"]:g}{r["unit"]}／推奨発注 {quantity}／{r["order_state"]}'])
    if summary['needs'] == 0:
        lines.append('要発注の備品はありません。' if not summary['missing'] and not summary['unconfigured'] else '現時点で要発注の登録はありません。未確認・基準未設定分の確認が必要です。')
    if summary['missing']:
        lines.extend(['', '未確認：' + '、'.join(BRANCHES[r['branch']] + '/' + r['name'] for r in rows if r['applicable'] and not r['current'])])
    return '\n'.join(lines)


def submit(db, data, base_url):
    if not isinstance(data, dict) or data.get('branch') not in BRANCHES:
        raise ValueError('店舗を選択してください')
    actor = short_text(data.get('actor'), 40)
    if not actor:
        raise ValueError('確認者のお名前を入力してください')
    c = connect(db)
    try:
        with c:
            c.execute('BEGIN IMMEDIATE')
            owner, ready, _ = recipient_status(c)
            if not ready or not os.getenv('LINE_CHANNEL_ACCESS_TOKEN'):
                raise ValueError('朝倉LINEの連携を確認してください。在庫は保存されています')
            rows = rows_for(c, data['branch'])
            # Ignore version-only changes: identical reports within a week are idempotent.
            snapshot = json.dumps([{k: v for k, v in r.items() if k != 'version'} for r in rows], sort_keys=True, ensure_ascii=False)
            key = 'report:' + week_start() + ':' + data['branch'] + ':' + hashlib.sha256(snapshot.encode()).hexdigest()
            old = c.execute('SELECT outbox_id FROM supply_notices WHERE key=?', (key,)).fetchone()
            if old:
                state = c.execute('SELECT state FROM concierge_outbox WHERE id=?', (old[0],)).fetchone()[0]
                return {'ok': True, 'already_queued': True, 'state': state}
            link = new_link(c, base_url)
            text = report_text(rows, actor) + '\n\n備品管理：\n' + link
            queue(c, key, owner, split_messages(text))
            c.execute('INSERT INTO supply_reports VALUES(?,?,?,?,?,?)', (key, week_start(), data['branch'], now().isoformat(), actor, snapshot))
        return {'ok': True, 'state': 'pending'}
    finally:
        c.close()


def ingest(c, events, base_url):
    """Called inside the verified work-channel webhook transaction."""
    claimed = set()
    owner, owner_ready, group_ready = recipient_status(c)
    for event in events:
        src, msg = event.get('source', {}), event.get('message', {})
        body = msg.get('text', '')
        destination = src.get('groupId') if src.get('type') == 'group' else src.get('userId')
        allowed = (group_ready and src.get('type') == 'group' and destination == line_delivery.TARGET) or (owner_ready and src.get('type') == 'user' and destination == owner)
        if not allowed or msg.get('type') != 'text' or not isinstance(body, str) or body.strip() not in COMMANDS:
            continue
        claimed.add(event['webhookEventId'])
        if not c.execute('INSERT OR IGNORE INTO supply_commands VALUES(?,?)', (event['webhookEventId'], time.time())).rowcount:
            continue
        link = new_link(c, base_url)
        text = '【つきや・備品管理】\n店舗を選び、在庫数を入力して「確認」を押してください。\n最後に「朝倉へ報告」を押すと要発注一覧を送信します。\n\n' + link + '\n\nリンクは8日間有効です。経理LINE内でご利用ください。'
        queue(c, 'command:' + event['webhookEventId'], destination, split_messages(text))
    return claimed


def briefing_request(db, day, base_url):
    """Append Saturday's request to the existing daily text; do not send here."""
    if datetime.strptime(day, '%Y-%m-%d').weekday() != 5:
        return ''
    c = connect(db)
    try:
        with c:
            _, owner_ready, group_ready = recipient_status(c)
            if not enabled(c) or not owner_ready or not group_ready:
                return ''
            link = new_link(c, base_url)
            return ('【今週の備品チェック】\n経理LINEの皆様、明日（日曜）23時までに本店・別邸の在庫をご確認ください。\n'
                    '在庫数・確認者を入力し、要発注を選んで朝倉へ報告してください。\n\n' + link)
    finally:
        c.close()


def tick(db, base_url, at=None):
    at = at or now()
    c = connect(db)
    try:
        with c:
            c.execute('BEGIN IMMEDIATE')
            owner, owner_ready, group_ready = recipient_status(c)
            if not enabled(c) or not owner_ready or not group_ready or not os.getenv('LINE_CHANNEL_ACCESS_TOKEN') or not base_url.startswith('https://'):
                return
            # One initial link lets staff start immediately after deployment.
            if not c.execute("SELECT 1 FROM supply_notices WHERE key='intro:group'").fetchone():
                link = new_link(c, base_url)
                text = ('【つきや・備品管理を開始します】\n経理LINEの皆様で、毎週日曜23時までに本店・別邸の備品をご確認ください。\n'
                        '土曜の朝の連絡で確認依頼、日曜23時に朝倉へ集計します。確認完了時は画面からすぐに報告できます。\n'
                        '初回は在庫数と発注基準を設定してください。基準が未設定でも「要発注」を選んで報告できます。\n\n' + link +
                        '\n\nLINEで「備品」と送ると新しいリンクが届きます。リンクは8日間有効・経理LINE内でご利用ください。\n元のNumbers表とは自動同期しません。今後の入力はこちらに統一してください。')
                queue(c, 'intro:group', line_delivery.TARGET, split_messages(text))
                queue(c, 'intro:owner', owner, split_messages('【つきや・備品管理】\niPhone用の管理画面を経理LINEへご案内しました。\n毎週土曜の朝の連絡で確認依頼、日曜23時に未確認分を含めて集計します。\n' + link))
            week = week_start(at)
            # The group request is part of Saturday's durable daily briefing.
            if at.weekday() == 6 and at.hour >= 23:
                key = 'weekly:report:' + week
                if not c.execute('SELECT 1 FROM supply_notices WHERE key=?', (key,)).fetchone():
                    link = new_link(c, base_url)
                    queue(c, key, owner, split_messages(report_text(rows_for(c, at=at), week=week) + '\n\n' + link))
    finally:
        c.close()


def loop(db, base_url):
    while True:
        try:
            with LOCK:
                tick(db, base_url)
        except Exception as exc:
            print('Supplies scheduler:', type(exc).__name__)
        time.sleep(60)
