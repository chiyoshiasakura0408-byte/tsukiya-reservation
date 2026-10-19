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
    lines = ['【Seasonal Calendar】', annual_years()]
    for item in catalog()['courses']:
        lines.append(display_period(item) + '\n' + item['name'] + f"\nお一人様 ¥{item['price']:,}（税込）")
    lines.append('季節の目安です。入荷状況により、ご提供期間・内容が変わる場合がございます。')
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
    if re.fullmatch(r'course:[a-z0-9-]{1,50}', data):
        return data
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
        {'type': 'text', 'text': 'VIPのお客様は17:00〜21:00の間で開始時刻をご指定いただけます（個室）。\n\nご希望日をお選びください。次に人数を選択すると、空席をご案内します。\n\n受付開始前の日程も予約リクエストを承ります。VIP担当の確認後にご予約を確定いたします。\n\n日付の色は空席状況を示すものではありません。', 'size': 'xs', 'wrap': True, 'color': '#64716D'},
        {'type': 'text', 'text': f'{shown.year}年{shown.month}月', 'weight': 'bold', 'align': 'center'},
        *rows]}}
    bubble['footer'] = {'type':'box','layout':'vertical','contents': ([{'type':'box','layout':'horizontal','contents':nav}] if nav else []) + [{'type':'button','action':{'type':'message','label':'予約リクエスト','text':'予約リクエスト'}}]}
    return {'type': 'flex', 'altText': f'空席案内：{shown.year}年{shown.month}月のカレンダーから日付をお選びください', 'contents': bubble}


def display_period(item):
    import re
    period = re.sub(r'\d{4}年', '', item['period'])
    if not item['id'].startswith('matsuba-'):
        period = re.sub(r'\d{1,2}日', '', period)
    return period


def annual_years():
    import re
    years = sorted({int(y) for item in catalog()['courses'] for y in re.findall(r'(\d{4})年', item['period'])})
    return f'{years[0]}年〜{years[-1]}年' if len(years) > 1 else f'{years[0] if years else datetime.now(JST).year}年'


EN_DESCRIPTIONS = {
'matsuba-fukahire': 'As winter deepens and fishing moves into deeper waters, we select the finest Matsuba crabs available. Each guest enjoys one whole live Matsuba crab weighing approximately 1–1.2 kg, plus one-quarter of a boiled Matsuba crab. Discover the fresh sweetness of live crab preparations alongside the concentrated flavour of boiled crab.\n\nOur menu changes in the new year, introducing our winter speciality: shark fin in a rich sauce. Selected beverages are included; certain drinks cost extra. Dinner is served in two sittings.',
'hairy': 'As the drift ice recedes from the Sea of Okhotsk in early spring, the awaited hairy crab season begins. We select exceptionally large crabs weighing at least 1.2 kg from the newly ice-free waters.\n\nEnjoy delicately sweet meat and rich yet refined crab tomalley. These extra-large crabs offer generous portions and deep flavour, available only in limited quantities during a short spring season.',
'zuwaigani': 'Crab is not only a winter delicacy. In summer, we select premium live snow crab from the waters off western Sakhalin, Russia. Each guest receives one whole live crab weighing at least 1 kg, complemented by boiled crab to showcase different preparations.\n\nSummer specialities include fragrant tomalley grilled in the shell and shabu-shabu with rich crab tomalley. Savour the delicate sweetness and distinctive flavours of our summer crab menu.',
 'tarabagani': 'Large king crabs are increasingly scarce as global demand grows. We select rare live king crabs weighing 3–3.9 kg, serving one crab for every two guests.\n\nEnjoy thick, springy meat and refined sweetness through a variety of preparations. We take care of the shell removal, leaving you free to savour every bite at its best.',
'matsuba-seko': 'We select the finest live Matsuba crabs according to their condition, rather than a particular port or region. Each guest receives one whole live Matsuba crab weighing approximately 1–1.2 kg, plus one rare female Seko crab.\n\nThe Seko fishing season runs from November 6 to December 31. Enjoy the rich flavour of its internal roe and the delicate texture of its external eggs, alongside the elegant sweetness of male Matsuba crab.\n\nSelected beverages are included; certain drinks cost extra. Dinner is served in two sittings.'}

def english_period(item):
    if item['id'] == 'tarabagani': return 'October – November'
    return item.get('period_en', '')

def annual_message(language="ja"):
    rows = []
    en = language == 'en'
    for item in catalog()['courses']:
        rows.append({'type':'box','layout':'vertical','spacing':'sm','paddingAll':'md',
            'action':{'type':'postback','label':item['name'][:20],'data':'course:'+item['id'],'displayText':(item['name_en'] if en else item['name'])+' ('+(english_period(item) if en else display_period(item))+')'},
            'contents':[
                {'type':'text','text':english_period(item) if en else display_period(item),'size':'sm','color':'#D8D2C6'},
                {'type':'text','text':item['name_en'] if en else item['name'],'size':'md','wrap':True,'weight':'bold','color':'#F5F0E4'},
                {'type':'text','text':f"JPY {item['price']:,} per guest (tax included)" if en else f"お一人様 ¥{item['price']:,}（税込）",'size':'sm','color':'#D8D2C6'},
                {'type':'text','text':'Explore this course ›' if en else 'コースのご紹介へ ›','size':'md','color':'#D8D2C6'}]})
    return {'type':'flex','altText':'Seasonal Calendar '+annual_years()+'：コースをタップして詳細をご覧ください',
        'contents':{'type':'bubble','size':'mega','body':{'type':'box','layout':'vertical','backgroundColor':'#0B0B09','spacing':'md','contents':[
            {'type':'text','text':'Seasonal Calendar','size':'xl','weight':'bold','color':'#F5F0E4'},
            {'type':'text','text':annual_years().replace('年','').replace('〜','–') if en else annual_years(),'size':'sm','color':'#D8D2C6'},
            *rows,{'type':'text','text':'Seasonal availability is approximate. Dates and menus may change depending on supply.' if en else '季節の目安です。入荷状況により、ご提供期間・内容が変わる場合がございます。','size':'xs','wrap':True,'color':'#D8D2C6'}]}}}


def course_messages(course_id, registered, text_message, language="ja"):
    item = next((x for x in catalog()['courses'] if x['id'] == course_id), None)
    if not item:
        return [text_message('コースを確認できませんでした。年間スケジュールからお選びください。')]
    media = registered.get('course_media', {}).get(course_id, {})
    if language == 'en':
        parts = [item['name_en'], english_period(item), f"JPY {item['price']:,} per guest (tax included)", media.get('menu_en') or EN_DESCRIPTIONS.get(course_id, item.get('description_en',''))]
        if course_id.startswith('matsuba-'):parts.append('Beverages are charged separately for extended bookings spanning both sittings.')
        if not item['online_booking']:parts.append('Standard reservations are not yet open. VIP guests may submit a reservation request through Reservations.')
        messages = [text_message('\n\n'.join(filter(None,parts)))]
        photo = media.get('photo') or item.get('photo')
        if isinstance(photo,str) and photo.startswith('https://'):messages.append({'type':'image','originalContentUrl':photo,'previewImageUrl':photo})
        return messages
    description = media.get('menu') or item.get('description', '')
    parts = [item['name'], display_period(item), f"お一人様 ¥{item['price']:,}（税込）", description]
    if item['id'].startswith('matsuba-'):
        parts.append('通し予約はお飲み物代別途でございます。')
    if not item['online_booking']:
        parts.append('ただいまご予約いただけません。')
    if media.get('menu_url'):parts.append(media['menu_url'])
    messages = [text_message('\n\n'.join(filter(None, parts)))]
    photo = media.get('photo') or item.get('photo')
    if photo and isinstance(photo, str) and photo.startswith('https://'):
        messages.append({'type':'image','originalContentUrl':photo,'previewImageUrl':photo})
    return messages


def course_name_for_day(day):
    import re
    for item in catalog()['courses']:
        bounds = PERIODS.get(item['id'])
        if not bounds:continue
        year = re.search(r'(\d{4})年', item['period'])
        if year and day.year != int(year[1]):continue
        sm, sd, em, ed = bounds
        if date(day.year, sm, sd) <= day <= date(day.year, em, ed):
            return item['name']
    return 'この日程のコースはVIP担当へお問い合わせください。'
