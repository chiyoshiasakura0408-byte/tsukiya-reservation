const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const script=fs.readFileSync('public/attribution.js','utf8');
function run(url,referrer='',storage={},dnt='0'){
 const location=new URL(url),requests=[],a={href:'https://tsukiya-reservation.onrender.com/book?course=matsuba-seko'},listeners={};
 const ctx={URL,URLSearchParams,Date,Set,JSON,Number,String,crypto:require('node:crypto').webcrypto,location,navigator:{doNotTrack:dnt},document:{referrer,body:{},querySelectorAll:()=>[a],addEventListener:(n,f)=>listeners[n]=f},sessionStorage:{getItem:k=>storage[k],setItem:(k,v)=>storage[k]=v},MutationObserver:class{observe(){}},fetch:(url,opt)=>{requests.push(JSON.parse(opt.body));return Promise.resolve()},window:{}};
 vm.runInNewContext(script,ctx);return {ctx,a,requests,storage};
}
const h=run('https://nishitenma-tsukiya-home.chiyoshi-a-0408.chatgpt.site/?utm_source=google_maps');
assert.equal(h.requests[0].source,'google_maps');
const b=run(h.a.href,'https://nishitenma-tsukiya-home.chiyoshi-a-0408.chatgpt.site/');
assert.equal(b.requests[0].session_id,h.requests[0].session_id);assert.equal(b.requests[0].source,'google_maps');
b.ctx.window.TsukiyaAttribution.event('booking_start');b.ctx.window.TsukiyaAttribution.event('booking_start');assert.equal(b.requests.length,2);
assert.equal(run('https://tsukiya-reservation.onrender.com/book','https://www.google.com/search?q=private').requests[0].source,'google_search');
assert.equal(run('https://tsukiya-reservation.onrender.com/book-test').requests.length,0);
assert.equal(run('https://tsukiya-reservation.onrender.com/book','',{},'1').requests.length,0);
assert.ok(!JSON.stringify(b.requests).includes('course='));console.log('Attribution: transfer, source, deduplication, test and privacy checks passed');

// AI sources must survive the homepage-to-booking handoff without storing raw URLs.
for(const [source,host] of [['chatgpt','chatgpt.com'],['gemini','gemini.google.com'],['perplexity','www.perplexity.ai'],['claude','claude.ai'],['copilot','copilot.microsoft.com']]){
 const home=run('https://nishitenma-tsukiya-home.chiyoshi-a-0408.chatgpt.site/', 'https://'+host+'/private-query');
 assert.equal(home.requests[0].source,source);
 const book=run(home.a.href,'https://nishitenma-tsukiya-home.chiyoshi-a-0408.chatgpt.site/');
 assert.equal(book.requests[0].source,source);
 assert.equal(book.requests[0].session_id,home.requests[0].session_id);
 assert.equal(run('https://tsukiya-reservation.onrender.com/book?utm_source='+source).requests[0].source,source);
 assert.ok(!JSON.stringify(book.requests).includes('private-query'));
}
assert.equal(run('https://tsukiya-reservation.onrender.com/book?utm_source=chatgpt.com').requests[0].source,'chatgpt');
assert.equal(run('https://tsukiya-reservation.onrender.com/book','https://chat.openai.com/').requests[0].source,'chatgpt');
for(const host of ['chatgpt.com.evil.example','fakechatgpt.com','perplexity.ai.evil.example'])assert.equal(run('https://tsukiya-reservation.onrender.com/book','https://'+host+'/').requests[0].source,'other');
assert.equal(run('https://tsukiya-reservation.onrender.com/book').requests[0].source,'unknown');
assert.equal(run('https://tsukiya-reservation.onrender.com/book?utm_source=__proto__').requests[0].source,'unknown');
console.log('AI attribution: provider hosts, UTM, handoff, unknown and spoof-host checks passed');
