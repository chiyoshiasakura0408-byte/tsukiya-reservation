"""Instagram Login messaging adapter; credentials and platform approval required."""
import hashlib
import hmac
import json
import os
import re
import time
import urllib.request
import urllib.error
import uuid
from datetime import date, datetime, timedelta
from urllib.parse import urlencode
import concierge as bot


def setup(c):
    c.execute('CREATE TABLE IF NOT EXISTS instagram_contacts(user_id TEXT PRIMARY KEY,last_inbound REAL NOT NULL,language TEXT NOT NULL DEFAULT "en")')


def configured():
    return all(os.getenv(k) for k in ('INSTAGRAM_APP_SECRET','INSTAGRAM_VERIFY_TOKEN','INSTAGRAM_ACCESS_TOKEN','INSTAGRAM_ACCOUNT_ID','INSTAGRAM_GRAPH_VERSION'))


def verify(query):
    expected=os.getenv('INSTAGRAM_VERIFY_TOKEN','')
    token=(query.get('hub.verify_token') or [''])[0]
    challenge=(query.get('hub.challenge') or [''])[0]
    if expected and (query.get('hub.mode') or [''])[0]=='subscribe' and hmac.compare_digest(expected,token) and re.fullmatch(r'\d{1,100}',challenge):
        return challenge
    return None


def language(text, previous='en'):
    if re.search(r'[ぁ-んァ-ヶ一-龯]',text):return 'ja'
    if re.search(r'[A-Za-z]',text):return 'en'
    return previous


def handoff(c,user,body,lang,base):
    request_id='ig-'+uuid.uuid4().hex[:12]
    c.execute('INSERT INTO concierge_requests(id,user_id,body,created) VALUES(?,?,?,?)',(request_id,user,body[:2000],time.time()))
    owner=bot.setting(c,'owner')
    if owner:
        bot.enqueue(c,owner,[bot.text_message('【Instagramからのご相談】'+request_id+'\n回答言語：'+lang+'\n'+body[:2000]+'\n'+base+'/concierge',False)],channel='owner',kind='request')
    return 'Thank you for your message. I will pass your request to our VIP concierge for personal attention. Please await our reply; this message does not confirm a reservation or a special arrangement.' if lang=='en' else 'お問い合わせを承りました。VIP担当に確認のうえ、ご案内いたします。ご予約や特別な手配は、回答をお待ちくださいますようお願い申し上げます。'


def respond(c,user,text,lang,courses,base,slots=None):
    en=lang=='en'; lower=text.lower();cat=bot.catalog(c)
    if text.startswith('お客様連携 '):
        result=bot.link_customer(c,user,text)
        return ('Your guest profile has been linked. We look forward to offering a more personal service.' if '完了' in result else 'We could not verify this link. Please contact the restaurant.') if en else result
    if any(k in lower for k in ('allerg','diet','taxi','birthday','celebrat','private room','special request','cancel','change my','アレルギー','タクシー','記念日','個室','変更','取消','キャンセル')):
        return handoff(c,user,text,lang,base)
    if slots is not None:
        if slots == 'error':return 'We are unable to check availability at present. Please try again shortly.' if en else 'ただいま空席を確認できません。恐れ入りますが、少し後にお試しください。'
        if not slots:return 'We do not have an available table for the requested date and party size, or the date is outside the booking period. May we suggest another date?' if en else 'ご希望日は満席または受付期間外でございます。別の日程をご検討いただけますでしょうか。'
        lines=['The following tables are currently available. Please follow a link to review the details and complete your reservation. Full payment is required to confirm an Instagram booking.' if en else '以下のお席をご案内できます。リンクよりお手続きください。Instagram経由のご予約は、お料理代のお支払い後に確定いたします。']
        for slot in slots[:4]:
            query=urlencode({'course':slot['course'],'date':slot['date'],'party_size':slot['party_size'],'area':slot['area'],'time':slot['time'],**({'lang':'en'} if en else {})})
            lines.append(slot['time']+' '+('Private room' if slot['area']=='PRIVATE' else 'Counter')+'\n'+base+'/book?'+query)
        return '\n\n'.join(lines)
    if any(k in lower for k in ('reserv','availab','book','空席','予約')):
        return 'May I have your preferred date and party size? Please send them as YYYY-MM-DD followed by the number of guests, for example 2026-11-10 2. Our sittings begin at 18:00 and 20:30, Japan time.' if en else 'ご希望の日程と人数を「2026-11-10 2名」の形式でお知らせください。18時と20時30分の二部制でございます。'
    if any(k in lower for k in ('price','cost','course','menu','料金','コース','お品書き')):
        prices=sorted({v[1] for v in courses.values()})
        text_en='Our seasonal crab courses are '+', '.join('JPY '+format(v,',') for v in prices)+' per guest, including tax. Beverages are charged separately. Matsuba and seko crab: November 10–December 31. Matsuba crab with our signature shark-fin sauce dish: January 1–March 20.'
        text_ja='\n'.join(n+'：お一人様 ¥'+format(p,',')+'（税込）' for n,p in courses.values())+'\nお飲み物代は別途、当日のお支払いとなります。'
        return (text_en+('\n'+cat['menu_en'] if cat.get('menu_en') else '')) if en else text_ja+('\n'+cat['menu'] if cat.get('menu') else '')
    if any(k in lower for k in ('photo','video','写真','動画')):
        links=[cat.get(k) for k in ('photo','video') if cat.get(k)]
        return ('Please enjoy a preview of our cuisine.\n' if en else 'お料理の写真・動画をご案内いたします。\n')+'\n'.join(links) if links else ('Our photographs and videos are being prepared. our VIP concierge will be pleased to assist with details.' if en else '写真・動画はただいま準備中でございます。')
    if any(k in lower for k in ('address','location','direction','where','アクセス','場所')):
        return 'Our private rooms are in the Bettei annex at 3-8-7 Nishitenma, Kita-ku, Osaka, separate from the main restaurant. Please check the venue in your confirmation email. If you are unsure, please send your reservation date and name, and we will confirm your destination.' if en else '個室は本店とは別の建物、別邸（大阪市北区西天満3-8-7）にございます。ご予約確定メールの来店先をご確認ください。ご不明でしたら、ご予約日とお名前をお知らせください。'
    if lower.strip() in ('hello','hi','good evening','menu','こんにちは','こんばんは','メニュー','english','日本語'):
        return 'Welcome to Tsukiya at Nishi-Tenma. I would be delighted to assist you with availability, seasonal courses, prices, directions or a special request. How may I assist you?' if en else '西天満つきやのコンシェルジュでございます。お席・お料理・料金・アクセスなど、ご希望をお聞かせください。'
    return handoff(c,user,text,lang,base)


def receive(db,raw,signature,lookup,courses,base):
    secret=os.getenv('INSTAGRAM_APP_SECRET','');account=os.getenv('INSTAGRAM_ACCOUNT_ID','')
    if not secret or not account:return 503,{'error':'Instagram is not configured'}
    expected='sha256='+hmac.new(secret.encode(),raw,hashlib.sha256).hexdigest()
    if len(raw)>1024*1024:return 413,{'error':'payload too large'}
    if not signature or not hmac.compare_digest(expected,signature):return 403,{'error':'invalid signature'}
    try:
        payload=json.loads(raw)
        if payload.get('object')!='instagram' or not isinstance(payload.get('entry'),list):raise ValueError()
        events=[]
        for entry in payload['entry']:
            if str(entry.get('id'))!=account:continue
            for event in entry.get('messaging',[]):
                message=event.get('message',{})
                if not message or message.get('is_echo'):continue
                if str(event.get('recipient',{}).get('id'))!=account:continue
                sender=event.get('sender',{}).get('id');mid=message.get('mid');stamp=float(event['timestamp'])/1000
                if not isinstance(sender,str) or not sender.isdigit() or not isinstance(mid,str) or not mid:raise ValueError()
                text=message.get('text','[Attachment received; please review in Instagram]')
                if not isinstance(text,str):raise ValueError()
                if time.time()-stamp>86400 or stamp>time.time()+300:continue
                events.append((sender,mid,stamp,text))
    except (ValueError,TypeError,KeyError,AttributeError):return 400,{'error':'invalid payload'}
    # Resolve live slots before any SQLite write transaction.
    resolved={}
    for sender,mid,stamp,text in events:
        m=re.fullmatch(r'(\d{4}-\d{2}-\d{2})\s+(\d+)\s*(?:名|guests?|people|persons?)?',text.strip(),re.I)
        if m:
            try:
                day=date.fromisoformat(m[1]);party=int(m[2]);today=datetime.now(bot.JST).date()
                if not today<=day<=today+timedelta(days=365) or not 2<=party<=8:raise ValueError()
                resolved[mid]=[{**s,'date':str(day),'party_size':party} for s in lookup(day,party)]
            except Exception:resolved[mid]='error'
    with bot.LOCK:
        c=bot.connect(db)
        try:
            setup(c)
            with c:
                bot.put(c,'instagram_verified',datetime.now(bot.JST).isoformat())
                enabled = bot.setting(c,'instagram_enabled')=='1'
                for sender,mid,stamp,text in events:
                    if not c.execute('INSERT OR IGNORE INTO concierge_events VALUES(?,?)',('ig:'+mid,time.time())).rowcount:continue
                    user='ig:'+sender
                    previous=c.execute('SELECT language FROM instagram_contacts WHERE user_id=?',(user,)).fetchone()
                    lang=language(text,previous[0] if previous else 'en')
                    c.execute('INSERT INTO instagram_contacts VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET last_inbound=MAX(last_inbound,excluded.last_inbound),language=excluded.language',(user,stamp,lang))
                    c.execute('INSERT INTO concierge_customers(user_id) VALUES(?) ON CONFLICT(user_id) DO UPDATE SET active=1',(user,))
                    if not enabled:continue
                    answer=respond(c,user,text,lang,courses,base,resolved.get(mid))
                    # Instagram text messages have a smaller length limit; each outbox row is one send.
                    for offset in range(0,len(answer),950):bot.enqueue(c,user,[bot.text_message(answer[offset:offset+950],False)])
        finally:c.close()
    return 200,{'ok':True}


def deliver_row(c,row):
    setup(c)
    contact=c.execute('SELECT last_inbound FROM instagram_contacts WHERE user_id=?',(row['user_id'],)).fetchone()
    state,error='failed',None
    if bot.setting(c,'instagram_enabled')!='1':error='Instagram delivery disabled'
    elif not contact or time.time()-contact[0]>=86400:error='Instagram reply window expired'
    elif not configured():error='Instagram credentials incomplete'
    else:
        account=os.environ['INSTAGRAM_ACCOUNT_ID'];version=os.environ['INSTAGRAM_GRAPH_VERSION']
        if not account.isdigit() or not re.fullmatch(r'v\d+\.\d+',version):error='invalid Instagram API configuration'
        else:
            messages=json.loads(row['payload'])
            text='\n'.join(m.get('text','') for m in messages)
            if not text or len(text)>1000:error='Instagram response must be at most 1000 characters'
            else:
                # No guaranteed idempotent resend: mark before I/O. Ambiguous sends need human review.
                with c:c.execute("UPDATE concierge_outbox SET state='unknown',error='Instagram send outcome requires verification' WHERE id=?",(row['id'],))
                body=json.dumps({'recipient':{'id':row['user_id'][3:]},'message':{'text':bot.elegant(text)}}).encode()
                req=urllib.request.Request('https://graph.instagram.com/'+version+'/'+account+'/messages',data=body,headers={'Authorization':'Bearer '+os.environ['INSTAGRAM_ACCESS_TOKEN'],'Content-Type':'application/json'})
                try:
                    with urllib.request.urlopen(req,timeout=10) as response:
                        data=json.load(response)
                        state='accepted' if data.get('message_id') else 'unknown'
                except urllib.error.HTTPError as exc:error='Instagram HTTP '+str(exc.code)
                except (OSError,ValueError,TimeoutError):state,error='unknown','Instagram send outcome requires verification'
    with c:
        c.execute('UPDATE concierge_outbox SET state=?,error=?,attempts=attempts+1 WHERE id=?',(state,error,row['id']))
        if row['kind']=='answer':c.execute('UPDATE concierge_requests SET status=? WHERE delivery_id=?',('回答Instagram受付済み' if state=='accepted' else '回答送信要確認',row['id']))
