(()=>{'use strict';if(window.TsukiyaLoader)return;
const sources=[1,2,3].map(n=>`/assets/crab-${n}.mp4`);let pending=1,active=null,guard=null;
const reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;
const css=document.createElement('style');css.textContent='#tsukiya-loading{position:fixed;inset:0;z-index:99999;background:#0b1d32;display:grid;place-items:center;color:white;font:16px system-ui}#tsukiya-loading[hidden]{display:none}#tsukiya-loading video{width:min(100%,640px);height:auto}#tsukiya-loading .fallback{position:absolute;background:#0b1d32;padding:12px 24px;border-radius:12px}';document.head.append(css);
const overlay=document.createElement('div');overlay.id='tsukiya-loading';overlay.hidden=true;overlay.setAttribute('role','status');overlay.setAttribute('aria-label','読み込み中');const video=document.createElement('video');video.muted=true;video.loop=true;video.playsInline=true;video.preload='none';video.setAttribute('aria-hidden','true');video.setAttribute('playsinline','');const fallback=document.createElement('span');fallback.className='fallback';fallback.textContent='Now loading...';overlay.append(video,fallback);
function mount(){if(!overlay.isConnected&&document.body)document.body.append(overlay)}
function hide(){overlay.hidden=true;video.pause();clearTimeout(guard);active=null}
function show(){mount();if(!overlay.isConnected)return;if(active===null){active=Math.floor(Math.random()*3);video.src=sources[active];video.currentTime=0;fallback.hidden=false;if(!reduced)video.play().catch(()=>{fallback.hidden=false})}overlay.hidden=false;clearTimeout(guard);guard=setTimeout(hide,80000)}
video.addEventListener('playing',()=>{fallback.hidden=true});video.addEventListener('error',()=>{fallback.hidden=false});
const originalFetch=window.fetch;window.fetch=function(...args){pending++;show();try{return Promise.resolve(originalFetch.apply(this,args)).finally(()=>{pending=Math.max(0,pending-1);if(!pending)hide()})}catch(error){pending=Math.max(0,pending-1);if(!pending)hide();throw error}};
document.addEventListener('DOMContentLoaded',()=>{mount();if(pending)show()},{once:true});window.addEventListener('load',()=>{pending=Math.max(0,pending-1);if(!pending)hide()},{once:true});window.addEventListener('pagehide',hide);window.addEventListener('pageshow',e=>{if(e.persisted){pending=0;hide()}});
window.TsukiyaLoader={preview(index){active=null;pending++;show();if(index>=0&&index<3){active=index;video.src=sources[index];if(!reduced)video.play().catch(()=>{})}setTimeout(()=>{pending=Math.max(0,pending-1);if(!pending)hide()},3400)}};
})();
