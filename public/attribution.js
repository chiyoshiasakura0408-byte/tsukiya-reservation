/* Anonymous first-party, 30-minute session attribution. No URL/referrer is transmitted. */
(()=>{'use strict';
const endpoint='https://tsukiya-reservation.onrender.com/api/public/marketing-event';
const home='nishitenma-tsukiya-home.chiyoshi-a-0408.chatgpt.site';
const booking='tsukiya-reservation.onrender.com';
const sources=new Set(['google_maps','google_search','google_ads','instagram','line','other','unknown']);
const uuid=/^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/;
if(location.pathname==='/book-test'||navigator.webdriver||navigator.doNotTrack==='1')return;
let ref;try{ref=new URL(document.referrer)}catch{}
const q=new URLSearchParams(location.search),utm=(q.get('utm_source')||'').toLowerCase(),medium=(q.get('utm_medium')||'').toLowerCase();
let source='unknown';
if(['google_maps','google_business','gbp'].includes(utm)||utm==='google'&&['maps','gbp','local'].includes(medium))source='google_maps';
else if(utm==='google'&&['cpc','ppc','paid'].includes(medium)||q.has('gclid'))source='google_ads';
else if(utm==='google')source='google_search';
else if(utm==='instagram')source='instagram';
else if(utm==='line')source='line';
else if(ref && ![home,booking,location.hostname].includes(ref.hostname)){
 if(/(^|\.)google\.(com|co\.jp)$/.test(ref.hostname))source='google_search';
 else if(/(^|\.)instagram\.com$/.test(ref.hostname))source='instagram';
 else if(/(^|\.)line\.me$/.test(ref.hostname))source='line';
 else source='other';
}
let stored;try{stored=JSON.parse(sessionStorage.getItem('tsukiya-attribution'))}catch{}
const now=Date.now();
let state;
// Only the official homepage may pass an anonymous session to the booking site.
if(location.hostname===booking&&uuid.test(q.get('ta_id')||'')&&sources.has(q.get('ta_source'))&&Number(q.get('ta_time'))>now-1800000&&Number(q.get('ta_time'))<=now+60000){
 state={session_id:q.get('ta_id'),source:q.get('ta_source'),at:now};
}else if(stored&&uuid.test(stored.session_id||'')&&sources.has(stored.source)&&stored.at>now-1800000&&(!utm||source===stored.source))state={...stored,at:now};
else state={session_id:crypto.randomUUID(),source,at:now};
try{sessionStorage.setItem('tsukiya-attribution',JSON.stringify(state))}catch{}
const sent=new Set();
function event(name){if(sent.has(name))return;sent.add(name);const body=JSON.stringify({...state,event:name});
 try{fetch(location.hostname===booking?'/api/public/marketing-event':endpoint,{method:'POST',mode:'no-cors',credentials:'omit',keepalive:true,headers:{'Content-Type':'text/plain'},body}).catch(()=>{})}catch{}
}
window.TsukiyaAttribution={data:()=>({session_id:state.session_id,source:state.source}),event};
event('visit');
function decorate(a){try{const u=new URL(a.href,location.href);if(u.hostname===booking&&u.pathname==='/book'){
 u.searchParams.set('ta_id',state.session_id);u.searchParams.set('ta_source',state.source);u.searchParams.set('ta_time',String(Date.now()));a.href=u.href;return true;
}}catch{}return false;}
function scan(){document.querySelectorAll('a[href]').forEach(decorate)}
scan();new MutationObserver(scan).observe(document.body,{childList:true,subtree:true});
document.addEventListener('click',e=>{const a=e.target.closest('a[href]');if(a&&decorate(a))event('booking_click')},true);
})();
