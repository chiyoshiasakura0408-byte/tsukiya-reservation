"""Authenticated, read-only connection checks. Never returns credentials or guest records."""
import json
import os
import re
import urllib.error
import urllib.request
from urllib.parse import urlparse
import concierge_ai
import owner_notifications


def line_get(path, token):
    req=urllib.request.Request('https://api.line.me/v2/bot/'+path,headers={'Authorization':'Bearer '+token})
    with urllib.request.urlopen(req,timeout=8) as response:
        return json.loads(response.read(100000))


def line_check(variable, menu=False):
    token=os.getenv(variable,'')
    if not token:
        return {'configured':False,'connected':False}
    try:
        info=line_get('info',token)
        result={'configured':True,'connected':True,'display_name':info.get('displayName','')}
        if menu:
            try:
                default=line_get('user/all/richmenu',token).get('richMenuId','')
                if not re.fullmatch(r'richmenu-[A-Za-z0-9_-]+',default):
                    return {**result,'default_menu':False}
                spec=line_get('richmenu/'+default,token)
                actions=[a.get('action',{}) for a in spec.get('areas',[])]
                result.update(default_menu=True,buttons=len(actions),official_website_buttons=sum(a.get('type')=='uri' and urlparse(a.get('uri','')).hostname=='nishitenma-tsukiya-home.chiyoshi-a-0408.chatgpt.site' for a in actions))
            except urllib.error.HTTPError as exc:
                result['menu_http_status']=exc.code
        return result
    except urllib.error.HTTPError as exc:
        return {'configured':True,'connected':False,'http_status':exc.code}
    except Exception:
        return {'configured':True,'connected':False,'error':'connection check failed'}


def check(db=None):
    ai={'configured':concierge_ai.configured(),'connected':False}
    if ai['configured']:
        try:
            answer=concierge_ai.request('Return ready=true. This is a connection check with no guest data.','Connection check',{'ready':{'type':'boolean'}})
            ai['connected']=answer['ready'] is True
        except Exception:
            ai['error']='AI connection check failed'
    notice = {}
    if db is not None:
        import concierge
        c=concierge.connect(db)
        try:
            rows=c.execute("SELECT state,payload FROM concierge_outbox WHERE channel='owner' AND kind='request' ORDER BY created DESC LIMIT 10").fetchall()
            for row in rows:
                text='\n'.join(m.get('text','') for m in json.loads(row['payload']))
                if 'English Concierge' in text or '英語問い合わせ' in text:
                    notice={'state':row['state'],'japanese_summary':('日本語要約：' in text or '要約：' in text),'japanese_translation':'日本語訳：' in text and '現在自動翻訳を利用できません' not in text}
                    break
        finally:c.close()
    return {'latest_english_notice':notice,'booking_paused':False,'openai':ai,'customer_line':line_check('CONCIERGE_LINE_CHANNEL_ACCESS_TOKEN',True),'owner_line':{**line_check('LINE_CHANNEL_ACCESS_TOKEN'), 'quota':owner_notifications.quota()}}
