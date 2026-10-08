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
