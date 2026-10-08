"""LINE calendar and course presentation, using registered course/catalog data only."""
import calendar
import json
from pathlib import Path
from datetime import date, datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
PERIODS = {'tarabagani': (10, 15, 11, 9), 'matsuba-seko': (11, 10, 12, 31), 'matsuba-fukahire': (1, 1, 3, 20)}


def course_windows(courses, today):
    end = today + timedelta(days=365)
    result = []
    for key, (name, price) in courses.items():
        if key not in PERIODS:
            continue
        sm, sd, em, ed = PERIODS[key]
        for year in (today.year, today.year + 1):
            if key == "tarabagani" and year != 2026:
                continue
            start, finish = date(year, sm, sd), date(year, em, ed)
            if finish >= today and start <= end:
                result.append((start, finish, name, price))
    return sorted(result)


def current_courses(courses):
    registered = {item['id']:(item['name'],item['price']) for item in catalog()['courses'] if item['online_booking'] and item['id'] in courses}
    windows = course_windows(registered, datetime.now(JST).date())
    lines = ['【ただいまのコース】', '現在ご予約を承っているコースと、ご来店いただける期間です。']
    lines += [f'{name}\n{start:%Y年%m月%d日}〜{end:%Y年%m月%d日}\nお一人様 ¥{price:,}（税込）' for start, end, name, price in windows]
    if not windows:
        lines.append('現在の受付コースは、VIP担当へお問い合わせください。')
    lines.append('LINEからのご予約は、お料理代の前受けなしで承ります。\nお支払いはご来店時にお願いいたします。')
    lines.append('「空席案内」からご希望日をお選びください。\n空席は日付・人数を選択した後にご案内します。')
    return '\n\n'.join(lines)


def catalog():
    return json.loads((Path(__file__).parent / 'course_catalog.json').read_text(encoding='utf-8'))


def annual_courses(courses):
    lines = ['【年間スケジュール】', '年間のコーススケジュール']
    for item in catalog()['courses']:
        lines.append(item['period'] + '\n' + item['name'] + f"\nお一人様 ¥{item['price']:,}（税込）")
    lines.append('季節の目安です。入荷状況により、ご提供期間・内容が変わる場合がございます。\n現在受付中のコースは「ただいまのコース」でご確認ください。')
    return '\n\n'.join(lines)


def event_command(event):
    if event.get('type') != 'postback':
        text = event.get('message', {}).get('text', 'メニュー')
        return text.strip() if isinstance(text, str) else ''
    pb = event.get('postback', {})
    if not isinstance(pb, dict):
        return ''
    data = pb.get('data', '')
    if not isinstance(data, str):
        return ''
    import re
    if re.fullmatch(r'calendar:\d{4}-\d{2}', data) or re.fullmatch(r'visit:\d{4}-\d{2}-\d{2}', data):
        return data
    return ''


def calendar_message(month=None):
    today = datetime.now(JST).date()
    limit = today + timedelta(days=365)
    try:
        shown = date.fromisoformat(month + '-01') if month else today.replace(day=1)
    except (ValueError, TypeError):
        shown = today.replace(day=1)
    first, last = today.replace(day=1), limit.replace(day=1)
    shown = min(max(shown, first), last)
    rows = [{'type': 'box', 'layout': 'horizontal', 'contents': [
        {'type': 'text', 'text': x, 'align': 'center', 'size': 'xs', 'color': '#64716D', 'flex': 1}
        for x in ['月', '火', '水', '木', '金', '土', '日']]}]
    for week in calendar.Calendar(firstweekday=0).monthdayscalendar(shown.year, shown.month):
        cells = []
        for number in week:
            day = shown.replace(day=number) if number else None
            selectable = day is not None and today <= day <= limit
            cell = {'type': 'box', 'layout': 'vertical', 'flex': 1, 'paddingAll': 'sm',
                    'contents': [{'type': 'text', 'text': str(number) if number else ' ', 'align': 'center', 'size': 'sm', 'color': '#153D40' if selectable else '#BBBBBB'}]}
            if selectable:
                cell['action'] = {'type': 'postback', 'label': f'{day.month}月{day.day}日', 'data': 'visit:' + str(day), 'displayText': f'{day.year}年{day.month}月{day.day}日'}
                cell['backgroundColor'] = '#EDF4F0'
                cell['cornerRadius'] = 'sm'
            cells.append(cell)
        rows.append({'type': 'box', 'layout': 'horizontal', 'spacing': 'xs', 'contents': cells})
    nav = []
    for target, label in ((shown - timedelta(days=1), '前の月'), ((shown.replace(day=28)+timedelta(days=4)).replace(day=1), '次の月')):
        target = target.replace(day=1)
        if first <= target <= last:
            nav.append({'type': 'button', 'height': 'sm', 'action': {'type': 'postback', 'label': label, 'data': f'calendar:{target:%Y-%m}'}})
    bubble = {'type': 'bubble', 'size': 'mega', 'body': {'type': 'box', 'layout': 'vertical', 'spacing': 'md', 'contents': [
        {'type': 'text', 'text': '空席案内', 'weight': 'bold', 'size': 'lg', 'color': '#153D40'},
        {'type': 'text', 'text': 'ご希望日をお選びください。\n次に人数を選択すると、空席をご案内します。\n\n日付の色は空席状況を示すものではありません。', 'size': 'xs', 'wrap': True, 'color': '#64716D'},
        {'type': 'text', 'text': f'{shown.year}年{shown.month}月', 'weight': 'bold', 'align': 'center'},
        *rows]}}
    if nav:
        bubble['footer'] = {'type': 'box', 'layout': 'horizontal', 'contents': nav}
    return {'type': 'flex', 'altText': f'空席案内：{shown.year}年{shown.month}月のカレンダーから日付をお選びください', 'contents': bubble}

