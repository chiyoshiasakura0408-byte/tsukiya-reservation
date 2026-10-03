"""Install the approved customer menu. Run explicitly; never during app startup."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
IMAGE = ROOT / 'public/images/concierge-rich-menu.jpg'


def definition():
    items = [
        (0, 124, 768, 306, '空席・ご予約', '空席案内'),
        (768, 124, 768, 306, 'ただいまのコース', 'ただいまのコース'),
        (0, 430, 768, 303, 'コース内容', 'コース内容'),
        (768, 430, 768, 303, '年間スケジュール', '年間スケジュール'),
        (0, 733, 1536, 291, 'VIP担当に相談', 'VIP担当に相談'),
    ]
    return {
        'size': {'width': 1536, 'height': 1024},
        'selected': True,
        'name': 'tsukiya-concierge-' + hashlib.sha256(IMAGE.read_bytes()).hexdigest()[:12],
        'chatBarText': 'ご案内メニュー',
        'areas': [{'bounds': dict(zip(('x', 'y', 'width', 'height'), (x, y, w, h))),
                   'action': {'type': 'message', 'label': label, 'text': command}}
                  for x, y, w, h, label, command in items],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    token = os.environ['CONCIERGE_LINE_CHANNEL_ACCESS_TOKEN']

    def api(path, method='GET', data=None, image=False):
        host = 'https://api-data.line.me' if image else 'https://api.line.me'
        headers = {'Authorization': 'Bearer ' + token}
        if image:
            body = data
            headers['Content-Type'] = 'image/jpeg'
        elif data is not None:
            body = json.dumps(data, ensure_ascii=False).encode()
            headers['Content-Type'] = 'application/json'
        else:
            body = None
        req = urllib.request.Request(host + '/v2/bot/' + path, data=body, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read()
            if image and method == 'GET':
                return raw
            return json.loads(raw) if raw else {}

    bot = api('info')
    if bot.get('basicId') != '@387reour':
        raise SystemExit('Wrong LINE account; no changes made.')
    spec = definition()
    api('richmenu/validate', 'POST', spec)
    try:
        previous = api('user/all/richmenu').get('richMenuId')
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        previous = None
    print('VALIDATED', bot['displayName'], len(spec['areas']), 'buttons', flush=True)
    if not args.apply:
        print('DRY_RUN: no changes made')
        return
    backup = Path(os.environ.get('DATA_DIR', '.')) / 'concierge-rich-menu-backup.json'
    if not backup.exists():
        backup.write_text(json.dumps({'previousDefault': previous}), encoding='utf-8')
    menus = api('richmenu/list')['richmenus']
    match = next((m for m in menus if m['name'] == spec['name'] and m['areas'] == spec['areas']), None)
    rich_id = match['richMenuId'] if match else api('richmenu', 'POST', spec)['richMenuId']
    try:
        installed_image = api('richmenu/' + rich_id + '/content', image=True)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        installed_image = None
    if installed_image is None:
        api('richmenu/' + rich_id + '/content', 'POST', IMAGE.read_bytes(), image=True)
    api('user/all/richmenu/' + rich_id, 'POST')
    assert api('user/all/richmenu')['richMenuId'] == rich_id
    current = api('richmenu/' + rich_id)
    assert current['areas'] == spec['areas'] and current['selected'] is True
    downloaded = api('richmenu/' + rich_id + '/content', image=True)
    assert hashlib.sha256(downloaded).digest() == hashlib.sha256(IMAGE.read_bytes()).digest()
    (backup.parent / 'concierge-rich-menu-ready').write_text(rich_id, encoding='utf-8')
    print('INSTALLED_AND_VERIFIED', rich_id, '5 buttons; default open', flush=True)


if __name__ == '__main__':
    main()
