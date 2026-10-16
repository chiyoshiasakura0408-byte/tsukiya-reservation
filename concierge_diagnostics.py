"""Authenticated, read-only connection checks. Never returns credentials or guest records."""
import json
import os
import re
import urllib.error
import urllib.request
from urllib.parse import urlparse
import concierge_ai


def line_get(path, token):
    req=urllib.request.Request('https://api.line.me/v2/bot/'+path,headers={'Authorization':'Bearer '+token})
    with urllib.request.urlopen(req,timeout=8) as response:
        return json.loads(response.read(100000))


def line_check(variable, menu=False):
    token=os.getenv(variable,'')
    if not token:
        return {'configured':False,'connected':False}
    try:
        line_get('info',token)
        result={'configured':True,'connected':True}
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


def check():
    ai={'configured':concierge_ai.configured(),'connected':False}
    if ai['configured']:
        try:
            answer=concierge_ai.request('Return ready=true. This is a connection check with no guest data.','Connection check',{'ready':{'type':'boolean'}})
            ai['connected']=answer['ready'] is True
        except Exception:
            ai['error']='AI connection check failed'
    return {'booking_paused':True,'openai':ai,'customer_line':line_check('CONCIERGE_LINE_CHANNEL_ACCESS_TOKEN',True),'owner_line':line_check('LINE_CHANNEL_ACCESS_TOKEN')}
