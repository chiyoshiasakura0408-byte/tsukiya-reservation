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
    def fit(x,y,t,width,size=11):
        text(x,y,t,min(size,width/max(pdfmetrics.stringWidth(t,'DoormanJP',1),1)))
    def notes(r):
        payment='事前決済済み' if r.get('payment_confirmed_at') else ('事前決済なし' if r['source'] in ('DIRECT','CONCIERGE_LINE') else '事前決済未完了')
        return [payment]+delivery.briefing_requests(r)[1]
    def grid(xs,top,height):
        c.setLineWidth(.5);c.rect(xs[0],top-height,xs[-1]-xs[0],height)
        for x in xs[1:-1]:c.line(x,top,x,top-height)
    for page,session in enumerate(delivery.BRIEFING_SESSIONS,1):
        selected=[r for r in rows if delivery.reservation_session(r)==session]
        text(35,800,'西天満つきや｜ドアマン用予約表',19)
        text(35,773,label+'　'+(session if session=='通し' else session+'の回'),14)
        text(35,750,'当日の追加予約は空欄にご記入ください。',10)
        text(35,717,'本店｜カウンター8席',14)
        counter=[r for r in selected if r['seating_area']=='COUNTER']
        if sum(r['party_size'] for r in counter)>8:
            raise RuntimeError('カウンター席数が8席を超えています。予約管理画面をご確認ください')
        if not counter:text(35,699,'＊9時時点でのご予約はなし',8)
        for x,t in [(43,'席番号'),(104,'お名前'),(334,'人数'),(383,'メモ・到着確認')]:text(x,678,t,10)
        y=666;seat=1
        for r in counter:
            n=r['party_size'];height=33*n
            grid([35,96,328,375,560],y,height)
            for offset in range(n):
                text(53,y-22-33*offset,str(seat+offset),11)
                if offset:c.line(35,y-33*offset,96,y-33*offset)
            fit(104,y-25,(r['visit_at'][11:16]+'　' if session=='通し' else '')+str(r['guest_name']).removesuffix('様')+'様',216)
            text(334,y-height/2-4,str(n)+'名',12)
            for j,note in enumerate(notes(r)):fit(383,y-16-12*j,note,165,8)
            c.rect(538,y-height+9,9,9)
            y-=height;seat+=n
        while seat<=8:
            grid([35,96,328,375,560],y,33);text(53,y-22,str(seat),11)
            if counter:text(104,y-26,'＊9時時点でのご予約はなし',7)
            c.rect(538,y-22,9,9);y-=33;seat+=1
        text(35,355,'別邸｜個室',14)
        for x,t in [(43,'お席'),(104,'開始'),(161,'お名前'),(334,'人数'),(383,'ご要望・到着確認')]:text(x,332,t,10)
        y=320
        for number in (1,2,3):
            room=[r for r in selected if r['seating_area']=='PRIVATE'+str(number)]
            grid([35,96,153,328,375,560],y,71);text(43,y-25,'個室'+'①②③'[number-1],11)
            if not room:text(161,y-58,'＊9時時点でのご予約はなし',8)
            for index,r in enumerate(room):
                offset=index*(60/max(len(room),1))
                text(104,y-25-offset,r['visit_at'][11:16],10)
                fit(161,y-25-offset,str(r['guest_name']).removesuffix('様')+'様',160,11)
                text(334,y-25-offset,str(r['party_size'])+'名',11)
                for j,note in enumerate(notes(r)):fit(383,y-16-offset-j*11,note,165,7 if len(room)>1 else 8)
            c.rect(383,y-62,9,9);text(398,y-61,'到着',8);y-=71
        text(35,87,'連絡・引継ぎメモ',10);c.line(35,65,560,65)
        text(35,42,'＊空欄は9時時点の情報です。追加・変更は予約管理画面をご確認ください。',8)
        text(527,42,str(page)+'/3',9);c.showPage()
    c.save()

