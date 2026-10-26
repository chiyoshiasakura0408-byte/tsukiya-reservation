/* Square hosts all card fields. No PAN/CVV enters our form, storage, or API. */
(async () => {
  const form = document.getElementById('booking');
  if (!form || location.pathname === '/book-test') return;
  const english = new URLSearchParams(location.search).get('lang') === 'en';
  const t = (ja,en) => english ? en : ja;
  const key = 'tsukiya-web-card-session';
  const legacy = form.onsubmit;
  let configuration, session = null, card = null, busy = false, polling = null;
  let pending = null, completed = false;
  const panel = document.createElement('section');
  panel.className = 'card'; panel.hidden = true;
  panel.setAttribute('aria-label', t('カードのお支払い','Card payment'));
  panel.innerHTML = '<h2></h2><div data-summary class="summary"></div><p data-timer class="sub"></p><div id="tsukiya-square-card" data-card></div><p data-message class="message" role="status"></p><button data-pay type="button" class="primary"></button> <button data-back type="button"></button>';
  document.getElementById('details').after(panel);
  const q = selector => panel.querySelector(selector);
  q('h2').textContent = t('カードのお支払い','Card payment');
  q('[data-back]').textContent = t('入力に戻る','Back to your details');
  function message(text,error=false) { q('[data-message]').textContent=text; q('[data-message]').className='message'+(error?' error':''); }
  function save() { if (session) sessionStorage.setItem(key,JSON.stringify(session)); }
  function clear() { sessionStorage.removeItem(key); session=null; }
  async function api(path,data) {
    const response = await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
    const json = await response.json();
    if (!response.ok) { const e = new Error(json.error || t('処理結果をご確認ください。','Please check the payment status.')); e.status=response.status; throw e; }
    return json;
  }
  function controls(state) {
    const locked=['PAYING','UNKNOWN','REVIEW'].includes(state);
    q('[data-pay]').disabled=busy || state!=='HOLD';
    q('[data-back]').hidden=locked || state==='PAID';
    form.querySelector('button[type="submit"]').disabled=!!session;
  }
  async function show(result) {
    session.id=result.id; session.state=result.state; session.expires=result.expires; save();
    panel.hidden=false; form.hidden=true;
    q('[data-summary]').textContent=t('合計（税込）：','Total (tax included): ')+'¥'+Number(result.amount).toLocaleString();
    q('[data-pay]').textContent=t('¥'+Number(result.amount).toLocaleString()+'を支払う','Pay JPY '+Number(result.amount).toLocaleString());
    controls(result.state);
    if(result.state==='PAID') {
      clearInterval(polling); q('[data-timer]').textContent='';
      q('[data-card]').hidden=true; q('[data-pay]').hidden=true;
      q('h2').textContent=t('ご予約が確定しました','Your reservation is confirmed');
      message(t('決済が完了しました。予約番号：','Payment completed. Reservation reference: ')+result.reservation_id+t('。確認メールをお送りします。','. We will send a confirmation email.'));
      completed=true;clear(); return;
    }
    if(['FAILED','EXPIRED','CANCELLED'].includes(result.state)) {
      clearInterval(polling); q('[data-pay]').hidden=true;
      message(t('決済は完了しておらず、予約は確定していません。入力に戻り、空席を選び直してください。','Payment has not completed and your reservation is not confirmed. Return and select an available table.'),true); return;
    }
    if(result.state==='REVIEW') message(t('お支払いと予約の照合が必要です。追加のお支払いは行わず、店舗へお問い合わせください。','Your payment and reservation need to be reviewed. Please contact us without making another payment.'),true);
    else if(['PAYING','UNKNOWN'].includes(result.state)) message(t('決済結果を確認中です。重ねて支払わず、この画面でお待ちください。','We are checking the payment result. Please wait here and do not pay again.'));
    else message(t('お料理代の全額決済後に予約確定となります。カード会社の本人認証が表示される場合があります。','Your reservation is confirmed after full payment. Your card issuer may ask you to verify your identity.'));
    if(!polling) polling=setInterval(check,5000);
  }
  async function check() {
    if(!session || busy)return;
    try {
      const result = session.id ? await api('/api/public/checkouts/'+session.id+'/status',{checkout_secret:session.secret}) : await api('/api/public/checkouts/lookup',{request_id:session.request_id,checkout_secret:session.secret});
      session.amount=result.amount;await show(result);if(result.state==='HOLD'&&!card)await mount();
    }
    catch(e){message(t('通信状態を確認しています。再度支払わず、この画面を保持してください。','Checking the connection. Keep this page open and do not pay again.'),true);}
  }
  function loadSdk(url) {
    return new Promise((resolve,reject)=>{const s=document.createElement('script');s.src=url;s.onload=resolve;s.onerror=()=>reject(new Error(t('カード画面を読み込めませんでした。時間をおいて再読み込みしてください。','The card form could not be loaded. Please reload later.')));document.head.appendChild(s);});
  }
  async function mount() {
    if(card)return;
    if(!window.Square)await loadSdk(configuration.sdk_url);
    const payments=window.Square.payments(configuration.application_id,configuration.location_id);
    await payments.setLocale(english?'en-US':'ja-JP');
    card=await payments.card(); await card.attach('#tsukiya-square-card');
  }
  q('[data-pay]').addEventListener('click',async()=>{
    if(busy || !session || session.state!=='HOLD')return;
    busy=true;controls('HOLD');
    try {
      await mount();
      const details={amount:String(session.amount),currencyCode:'JPY',intent:'CHARGE',customerInitiated:true,sellerKeyedIn:false,
        billingContact:pending?{email:pending.email,phone:pending.phone}:{}};
      const result=await card.tokenize(details);
      if(result.status!=='OK')throw new Error(t('本人認証またはカード情報をご確認ください。決済は送信していません。','Please check your card details or verification. Payment has not been submitted.'));
      // Persist the fact that a payment is about to be sent before dispatch.
      session.state='PAYING';save();controls('PAYING');
      const paid=await api('/api/public/checkouts/'+session.id+'/pay',{checkout_secret:session.secret,source_id:result.token});
      await show(paid);
    } catch(e) {
      if(session?.state==='PAYING') message(t('決済結果を確認中です。重ねて支払わず、お待ちください。','Checking the payment result. Please do not pay again.'),true);
      else message(e.message,true);
    } finally { busy=false;if(session)controls(session.state); }
  });
  q('[data-back]').addEventListener('click',async()=>{
    if(busy || !session || ['PAYING','UNKNOWN','REVIEW'].includes(session.state))return;
    busy=true;
    try {
      if(session.state==='HOLD') await api('/api/public/checkouts/'+session.id+'/cancel',{checkout_secret:session.secret});
      clearInterval(polling);polling=null;clear();panel.hidden=true;form.hidden=false;
      if(card){await card.destroy();card=null;}q('[data-card]').hidden=false;q('[data-pay]').hidden=false;
      document.getElementById('submit').disabled=false;selected=null;requestId=crypto.randomUUID();document.getElementById('details').hidden=true;await load();
    }catch(e){message(e.message,true);}finally{busy=false;}
  });
  window.TsukiyaCheckout={canSelect:()=>{if(session||completed){panel.hidden=false;panel.scrollIntoView({behavior:'smooth'});return false;}return true;}};
  try {
    configuration=await (await fetch('/api/public/checkout-config?language='+(english?'en':'ja'))).json();
    const saved=sessionStorage.getItem(key);
    if(saved){try{session=JSON.parse(saved);}catch(e){clear();}}
    if(session){
      let result;
      try {result=await api('/api/public/checkouts/lookup',{request_id:session.request_id,checkout_secret:session.secret});}
      catch(e){if(e.status===404)clear();else throw e;}
      if(result){session.amount=result.amount;await show(result);if(result.state==='HOLD')await mount();}
    }
    if(configuration.enabled){
      const guide=document.createElement('p');guide.className='sub';guide.textContent=t('カードはこの予約画面内でお支払いいただけます。全額決済後に予約確定となります。お支払い中のお席の一時確保は15分です。銀行振込は店舗で全額の着金を確認後に確定します。','Pay by credit card within this reservation page. Your reservation is confirmed after full payment. Your table is held for 15 minutes while you pay.');
      form.prepend(guide);document.getElementById('submit').textContent=t('内容を確認してお支払いへ','Review and continue to payment');
    }
  }catch(e){configuration=null;message(e.message,true);if(session){panel.hidden=false;form.hidden=true;}}
  form.onsubmit=async event=>{
    const bank=new FormData(form).get('bank_choice')==='on';
    if(bank || !configuration?.enabled){if(session){event.preventDefault();return;}return legacy.call(form,event);}
    event.preventDefault();if(busy||session||!selected)return;
    busy=true;document.getElementById('submit').disabled=true;
    const data=new FormData(form);
    if(!session){const bytes=crypto.getRandomValues(new Uint8Array(32));session={request_id:requestId,secret:Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join(''),state:'CREATING'};save();}
    pending={...selected,course,request_id:session.request_id,checkout_secret:session.secret,booking_language:english?'en':'ja',payment_method:'CARD',
      attribution:window.TsukiyaAttribution?.data(),guest_name:data.get('guest_name'),email:data.get('email'),phone:data.get('phone'),
      guest_note:data.get('guest_note'),celebration_items:data.getAll('celebration_items'),plate_message:data.get('plate_message'),cancellation_policy_accepted:data.get('cancellation_policy_accepted')==='on'};
    try {const result=await api('/api/public/checkouts',pending);session.amount=result.amount;await show(result);await mount();panel.scrollIntoView({behavior:'smooth'});}
    catch(e){
      if(!session?.id && e.status && e.status<500){clear();document.getElementById('submit').disabled=false;}
      panel.hidden=false;message(e.message,true);if(session&&!polling)polling=setInterval(check,5000);
    }finally{busy=false;if(session?.id)controls(session.state);}
  };
  setInterval(()=>{if(session?.expires && session.state==='HOLD'){const seconds=Math.max(0,Math.ceil(session.expires-Date.now()/1000));q('[data-timer]').textContent=t('お席の一時確保 残り ','Table hold remaining: ')+Math.floor(seconds/60)+':'+String(seconds%60).padStart(2,'0');}},1000);
})();
