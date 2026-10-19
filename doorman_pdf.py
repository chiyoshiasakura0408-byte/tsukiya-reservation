"""Daily printable arrival list, using current reservations and embedded Japanese type."""
from pathlib import Path
import os
import urllib.request
from datetime import datetime

def generate(db, day, output):
    from reportlab.pdfgen import canvas
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.lib.pagesizes import A4
    import line_delivery as delivery
    font = Path(db).parent / 'NotoSansJP-Regular.ttf'
    if not font.exists():
        from fontTools.ttLib import TTFont as Font
        from fontTools.varLib.instancer import instantiateVariableFont
        variable = font.with_suffix('.download')
        with urllib.request.urlopen('https://raw.githubusercontent.com/google/fonts/main/ofl/notosansjp/NotoSansJP%5Bwght%5D.ttf', timeout=60) as r:
            data = r.read(20_000_001)
        if len(data) > 20_000_000:
            raise RuntimeError('日本語フォントのサイズが上限を超えています')
        variable.write_bytes(data)
        f = Font(variable); instantiateVariableFont(f, {'wght':400}, inplace=True)
        temp = font.with_suffix('.tmp'); f.save(temp); os.replace(temp,font); variable.unlink()
    pdfmetrics.registerFont(TTFont('DoormanJP',str(font)))
    conn=delivery.connect(db)
    try:
        rows=[dict(r) for r in conn.execute("SELECT * FROM reservations WHERE substr(visit_at,1,10)=? AND status!='CANCELLED' ORDER BY visit_at,id",(day,))]
    finally: conn.close()
    c=canvas.Canvas(str(output),pagesize=A4);c.setTitle('西天満つきや｜'+day+' ドアマン用予約リスト')
    date=datetime.strptime(day,'%Y-%m-%d');label=f'{date.year}年{date.month}月{date.day}日（{"月火水木金土日"[date.weekday()]}）'
    def text(x,y,t,size=12):
        c.setFont('DoormanJP',size);c.drawString(x,y,t)
    def header():
        text(40,794,'西天満つきや',13);text(40,758,'本日のご予約リスト',23)
        text(40,729,label+'　｜　ドアマン用',12);c.line(40,712,555,712)
        text(40,686,f'合計{len(rows)}組・{sum(r["party_size"] for r in rows)}名',15)
        items=[item for item in ('花束','ケーキ') if any(item in delivery.briefing_requests(r)[0] for r in rows)]
        if items:text(40,660,'＊'+'と'.join(items)+'のリクエストあり。',11)
        for x,t in [(40,'到着'),(81,'開始'),(154,'お名前'),(397,'人数'),(455,'お席')]:text(x,624,t,10)
    def footer():text(40,35,'作成：'+datetime.now(delivery.JST).strftime('%Y/%m/%d %H:%M')+'　｜　変更は予約管理画面をご確認ください。',9)
    def wrap(t,width=390):
        lines=[];line=''
        for ch in t:
            if pdfmetrics.stringWidth(line+ch,'DoormanJP',11)>width:lines.append(line);line=''
            line+=ch
        if line:lines.append(line)
        return lines
    header();y=607
    seats={'PRIVATE1':'別邸・個室①','PRIVATE2':'別邸・個室②','PRIVATE3':'別邸・個室③','COUNTER':'本店・カウンター'}
    for r in rows:
        payment='事前決済済み' if r.get('payment_confirmed_at') else ('事前決済なし' if r['source'] in ('DIRECT','CONCIERGE_LINE') else '事前決済未完了')
        notes=[line for n in [payment]+delivery.briefing_requests(r)[1] for line in wrap('＊'+n)]
        height=max(93,74+19*(len(notes)-1))
        if y-height<110:footer();c.showPage();header();y=607
        c.setLineWidth(.5);c.line(40,y,555,y);c.rect(44,y-35,15,15)
        text(81,y-32,r['visit_at'][11:16],16)
        name=str(r['guest_name']).removesuffix('様').strip()+'様'
        size=min(16,230/max(pdfmetrics.stringWidth(name,'DoormanJP',1),1));text(154,y-32,name,size)
        text(397,y-32,str(r['party_size'])+'名',14);text(455,y-32,seats.get(r['seating_area'],'席未設定'),10)
        for i,n in enumerate(notes):
            if y-56-i*19<90:
                footer();c.showPage();header();y=607+56+i*19
            text(154,y-56-i*19,n,11)
        y-=height
    c.line(40,y,555,y)
    if y>160:
        text(40,y-27,'連絡・引継ぎメモ',11)
        for yy in (y-56,y-83):c.line(40,yy,555,yy)
    footer();c.save()
