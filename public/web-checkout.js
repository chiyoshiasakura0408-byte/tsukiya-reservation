/* Square hosts all card fields. No PAN/CVV enters our form, storage, or API. */
(async () => {
  const form = document.getElementById('booking');
  if (!form || location.pathname === '/book-test') return;
  const english = new URLSearchParams(location.search).get('lang') === 'en';
  const t = (ja,en) => english ? en : ja;
  const key = 'tsukiya-web-card-session';
  const legacy = form.onsubmit;
  let configuration, session = null, card = null, busy = false, polling = null;
  let pending = null, completed = false, checking = false;
  let cardAccount = null, saveIntent = null, paidCard = null;
  const bankChoice = english ? null : document.getElementById('bankChoice');
  const panel = document.createElement('section');
  panel.className = 'card'; panel.hidden = true;
  panel.setAttribute('aria-label', t('カードのお支払い','Card payment'));
  panel.innerHTML = '<h2></h2><div data-summary class="summary"></div><p data-timer class="sub"></p><div id="tsukiya-square-card" data-card></div><p data-message class="message" role="status"></p><button data-pay type="button" class="primary"></button> <button data-retry type="button" class="primary" hidden></button> <button data-bank type="button" hidden></button> <button data-back type="button"></button>';
  const cardOptions = document.createElement('div');
  cardOptions.innerHTML = `<label data-use-saved hidden><input type="checkbox"> <span></span></label>
    <label data-save-card hidden><input type="checkbox"> <span></span><small class="sub" style="display:block"></small></label>
    <p data-card-save-result class="sub" role="status" hidden></p>`;
  panel.querySelector('[data-card]').after(cardOptions);
  const enrollment = document.createElement('div');
  enrollment.className = 'guest-passkeys';
  const enrollButton = document.createElement('button');
  enrollButton.type = 'button';
  enrollButton.hidden = true;
  enrollButton.textContent = t('次回のためにパスキーを登録する','Set up a passkey for your next booking');
  enrollment.append(enrollButton);
  panel.append(enrollment);
  document.getElementById('details').after(panel);
  const q = selector => panel.querySelector(selector);
  q('h2').textContent = t('カードのお支払い','Card payment');
  q('[data-retry]').textContent = t('別のカードで支払う','Try another card');
  q('[data-bank]').textContent = '銀行振込に切り替える';
  q('[data-back]').textContent = t('入力に戻る','Back to your details');
  const savedChoice = q('[data-use-saved] input');
  const saveChoice = q('[data-save-card] input');
  q('[data-save-card] span').textContent = t('次回からカード情報入力を省略する','Skip entering card details next time');
  q('[data-save-card] small').textContent = t(
    '＊カード情報は決済サービス「Square」に安全に保存され、パスキーによる本人確認でカード情報を再入力せずに保存済みのカードでお支払いいただけます。',
    'Your card details are securely stored with Square. After verifying with your passkey, you can pay with the saved card without re-entering its details.');
  savedChoice.addEventListener('change', () => controls(session?.state));

  function message(text,error=false) { q('[data-message]').textContent=text; q('[data-message]').className='message'+(error?' error':''); }
  function save() { if (session) sessionStorage.setItem(key,JSON.stringify(session)); }
  function clear() { sessionStorage.removeItem(key); session=null; }
  async function api(path,data) {
    const response = await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
    const json = await response.json();
    if (!response.ok) { const e = new Error(json.error || t('処理結果をご確認ください。','Please check the payment status.')); e.status=response.status; throw e; }
    return json;
  }
  function stopPolling() { clearInterval(polling); polling=null; }
  function controls(state) {
    const locked=['PAYING','UNKNOWN','REVIEW'].includes(state);
    q('[data-pay]').disabled=busy || state!=='HOLD';
    const canChoose = state === 'HOLD' && !!cardAccount;
    q('[data-use-saved]').hidden = !canChoose || !cardAccount?.card;
    const canSave = state === 'HOLD' && !!configuration?.saved_cards && !cardAccount?.card;
    q('[data-save-card]').hidden = !canSave;
    savedChoice.disabled = busy || !canChoose;
    saveChoice.disabled = busy || !canSave;
    q('[data-card]').hidden = state !== 'HOLD' || (canChoose && !!cardAccount.card && savedChoice.checked);

    const canRecover=state==='FAILED' && !!pending && !!configuration?.enabled;
    q('[data-retry]').hidden=!canRecover;
    q('[data-bank]').hidden=!canRecover || !bankChoice || bankChoice.disabled;
    q('[data-retry]').disabled=q('[data-bank]').disabled=busy;
    q('[data-back]').hidden=locked || state==='PAID' || canRecover;
    form.querySelector('button[type="submit"]').disabled=!!session;
  }
  function finishCardSave(event) {
    const status = q('[data-card-save-result]');
    const intent = paidCard;
    if (!intent) return;
    const grant = intent.grant || event?.detail?.grant || window.TsukiyaGuestCards?.grant;
    status.hidden = false;
    if (!grant) {
      status.textContent = t('カードを保存するには、パスキーを登録してください。',
        'Set up a passkey to save your card.');
      return;
    }
    paidCard = null;
    status.textContent = t('カードを保存しています…','Saving your card…');
    api('/api/public/checkouts/'+intent.id+'/save-card', {checkout_secret:intent.secret, card_grant:grant})
      .then(() => { status.textContent = t('次回のお支払い用にカードを保存しました。','Your card was saved for your next booking.'); })
      .catch(() => { status.textContent = t('カードを保存できませんでした。今回のお支払いは完了しています。','The card could not be saved. This payment is complete.'); });
  }
  window.addEventListener('tsukiya-passkey-card-verified', finishCardSave);

  async function show(result) {
    session.id=result.id; session.state=result.state; session.expires=result.expires; save();
    panel.hidden=false; form.hidden=true;
    q('[data-summary]').textContent=t('合計（税込）：','Total (tax included): ')+'¥'+Number(result.amount).toLocaleString();
    q('[data-pay]').textContent=t('¥'+Number(result.amount).toLocaleString()+'を支払う','Pay JPY '+Number(result.amount).toLocaleString());
    controls(result.state);
    if(result.state==='PAID') {
      stopPolling(); q('[data-timer]').textContent='';
      q('[data-card]').hidden=true; q('[data-pay]').hidden=true;
      q('h2').textContent=t('ご予約が確定しました','Your reservation is confirmed');
      message(t('決済が完了しました。予約番号：','Payment completed. Reservation reference: ')+result.reservation_id+t('。確認メールをお送りします。','. We will send a confirmation email.'));
      if (pending && window.TsukiyaPasskeySetup && !cardAccount) {
        const {email, phone} = pending;
        enrollButton.hidden = false;
        enrollButton.onclick = () => {
          enrollButton.hidden = true;
          window.TsukiyaPasskeySetup(enrollment, {reference:String(result.reservation_id), email, phone});
        };
      }
      if (saveIntent?.id === session.id) {
        if (document.body.hasAttribute('data-passkey-email-only') && !saveIntent.grant) {
          const status = q('[data-card-save-result]');
          status.hidden = false;
          status.textContent = t('別メールのリンクからパスキーを登録すると、今回のカードを保存できます。',
            'Set up your passkey using the link in the separate email to save this card.');
        } else {
          paidCard = {...saveIntent, secret:session.secret};
          finishCardSave();
        }
      }
      saveIntent = null;
      completed=true;clear(); return;
    }
    if(['FAILED','EXPIRED','CANCELLED'].includes(result.state)) {
      stopPolling(); q('[data-timer]').textContent='';
      q('[data-card]').hidden=true; q('[data-pay]').hidden=true;
      if(result.state==='FAILED') {
        const decline=result.decline_reason==='CVV_FAILURE'
          ? t('セキュリティコードをご確認ください。','Please check your card security code.')
          : t('カードの承認が得られませんでした。別のカードをお試しいただくか、カード発行会社にご確認ください。','Your card was declined. Try another card or contact your card issuer.');
        const next=pending
          ? t('ご予約はまだ確定していません。お支払い方法を選ぶと、空席を確認します。','Your reservation is not confirmed. Choose another card to check availability.')
          : t('ご予約はまだ確定していません。入力に戻って空席を選び直してください。','Your reservation is not confirmed. Return to your details and select an available table.');
        message(decline+' '+next,true);
      } else {
        message(t('決済は完了しておらず、予約は確定していません。入力に戻り、空席を選び直してください。','Payment has not completed and your reservation is not confirmed. Return and select an available table.'),true);
      }
      return;
    }
    if(result.state==='REVIEW') message(t('お支払いと予約の照合が必要です。追加のお支払いは行わず、店舗へお問い合わせください。','Your payment and reservation need to be reviewed. Please contact us without making another payment.'),true);
    else if(['PAYING','UNKNOWN'].includes(result.state)) message(t('決済結果を確認中です。重ねて支払わず、この画面でお待ちください。','We are checking the payment result. Please wait here and do not pay again.'));
    else message(t('お料理代の全額決済後に予約確定となります。カード会社の本人認証が表示される場合があります。','Your reservation is confirmed after full payment. Your card issuer may ask you to verify your identity.'));
    if(!polling) polling=setInterval(check,5000);
  }
  async function check() {
    if(!session || busy || checking)return;
    const active=session, state=active.state;
    const current=()=>session===active && active.state===state && !busy;
    checking=true;
    try {
      const result = active.id ? await api('/api/public/checkouts/'+active.id+'/status',{checkout_secret:active.secret}) : await api('/api/public/checkouts/lookup',{request_id:active.request_id,checkout_secret:active.secret});
      if(!current())return;
      active.amount=result.amount;await show(result);if(result.state==='HOLD'&&!card)await mount();
    }
    catch(e){if(current())message(t('通信状態を確認しています。再度支払わず、この画面を保持してください。','Checking the connection. Keep this page open and do not pay again.'),true);}
    finally{checking=false;}
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
      const savedCard = savedChoice.checked ? cardAccount?.card : null;
      const wantsSave = saveChoice.checked && configuration.saved_cards && !cardAccount?.card;
      const details={amount:String(session.amount),currencyCode:'JPY',intent:wantsSave?'CHARGE_AND_STORE':'CHARGE',customerInitiated:true,sellerKeyedIn:false,
        billingContact:pending?{email:pending.email,phone:pending.phone}:{}};
      message(t('カード会社の本人認証を確認しています…','Checking your card issuer verification…'));
      const result=await Promise.race([savedCard?card.tokenize(details,savedCard.id):card.tokenize(details),new Promise((_,reject)=>setTimeout(()=>reject(new Error(t('本人認証が完了しませんでした。入力に戻り、もう一度お試しください。決済は送信していません。','Verification did not finish. Return to your details and try again. No payment was submitted.'))),120000))]);
      if(result.status!=='OK')throw new Error(t('本人認証またはカード情報をご確認ください。決済は送信していません。','Please check your card details or verification. Payment has not been submitted.'));
      // Persist the fact that a payment is about to be sent before dispatch.
      saveIntent = wantsSave ? {id:session.id,grant:cardAccount?.grant} : null;
      session.state='PAYING';save();controls('PAYING');
      const paid=await api('/api/public/checkouts/'+session.id+'/pay',{checkout_secret:session.secret,source_id:result.token,store_card:wantsSave,
        ...(savedCard ? {saved_card_id:savedCard.id,card_grant:cardAccount.grant} : {})});
      await show(paid);
    } catch(e) {
      if(session?.state==='PAYING') message(t('決済結果を確認中です。重ねて支払わず、お待ちください。','Checking the payment result. Please do not pay again.'),true);
      else message(e.message,true);
    } finally { busy=false;if(session)controls(session.state); }
  });
  async function resetCheckout() {
    if(card){await card.destroy();card=null;}
    stopPolling();clear();pending=null;cardAccount=null;saveIntent=null;panel.hidden=true;form.hidden=false;
    savedChoice.checked=saveChoice.checked=false;
    q('[data-card-save-result]').hidden=true;
    q('[data-card]').hidden=false;q('[data-pay]').hidden=false;
    document.getElementById('submit').disabled=false;
    selected=null;requestId=null;document.getElementById('details').hidden=true;
  }
  q('[data-back]').addEventListener('click',async()=>{
    if(busy || !session || !['HOLD','FAILED','EXPIRED','CANCELLED'].includes(session.state))return;
    busy=true;
    try {
      if(session.state==='HOLD') {
        const result=await api('/api/public/checkouts/'+session.id+'/cancel',{checkout_secret:session.secret});
        if(result.state!=='CANCELLED' && result.state!=='EXPIRED'){await show(result);return;}
      }
      await resetCheckout();await load();
    }catch(e){message(e.message,true);}
    finally{busy=false;if(session)controls(session.state);}
  });
  async function recover(useBank) {
    if(busy || session?.state!=='FAILED' || !pending || !configuration?.enabled ||
        (useBank && (!bankChoice || bankChoice.disabled)))return;
    const {date,seating_area,time,party_size}=pending;
    const choice=useBank ? bankChoice : document.getElementById('cardChoice');
    busy=true;controls('FAILED');
    try {
      await resetCheckout();
      if(choice){choice.checked=true;choice.dispatchEvent(new Event('change',{bubbles:true}));}
      document.getElementById('start').value=date;
      document.getElementById('party').value=party_size;
      const days=await load();
      const day=days?.find(item=>item.date===date);
      const available=days===availableDays && day?.slots?.[seating_area]?.[time]
        && document.getElementById('start').value===date
        && Number(document.getElementById('party').value)===party_size;
      busy=false;
      if(available) {
        choose(date,seating_area,time);
        const result=document.getElementById('result');
        result.className='message';
        result.textContent=useBank
          ? '銀行振込に切り替えました。内容と振込名義をご確認のうえ、振込のお申し込みへお進みください。'
          : t('内容をご確認のうえ、別のカードでお支払いにお進みください。','Review your reservation, then continue with another card.');
      } else if(document.getElementById('start').value===date &&
                Number(document.getElementById('party').value)===party_size && days) {
        document.getElementById('calendarStatus').textContent=t('先ほどのお席はご利用いただけません。別の空席をお選びください。','Your previous table is no longer available. Please select another table.');
      }
    }catch(e){
      if(session)message(e.message,true);
      else document.getElementById('calendarStatus').textContent=e.message;
    }finally{busy=false;if(session)controls(session.state);}
  }
  q('[data-retry]').addEventListener('click',()=>recover(false));
  q('[data-bank]').addEventListener('click',()=>recover(true));
  window.TsukiyaCheckout={canSelect:()=>{if(session||completed){panel.hidden=false;panel.scrollIntoView({behavior:'smooth'});return false;}return !busy;}};
  try {
    configuration=await (await fetch('/api/public/checkout-config?language='+(english?'en':'ja'))).json();
    if(configuration.environment==='sandbox'){const banner=document.createElement('p');banner.className='summary';banner.setAttribute('role','note');banner.textContent=t('Sandboxテスト環境です。実際の予約・課金は行われません。架空の情報とSquareテストカードのみでお試しください。','Sandbox test environment. No real booking or charge. Use fictitious details and Square test cards only.');document.querySelector('main').prepend(banner);}
    const saved=sessionStorage.getItem(key);
    if(saved){try{session=JSON.parse(saved);}catch(e){clear();}}
    if(session){
      let result;
      try {result=await api('/api/public/checkouts/lookup',{request_id:session.request_id,checkout_secret:session.secret});}
      catch(e){if(e.status===404)clear();else throw e;}
      if(result){session.amount=result.amount;await show(result);if(result.state==='HOLD')await mount();}
    }
    if(configuration.enabled){
      if (english) {
        const guide = document.createElement('p');
        guide.className = 'sub';
        guide.textContent = 'Pay by credit card within this reservation page. Your reservation is confirmed after full payment. Card payment is immediate. Your table is held for 10 minutes while you enter your card details. After the hold expires, please select an available table again.';
        form.prepend(guide);
      }
      document.getElementById('submit').textContent=t('内容を確認してお支払いへ','Review and continue to payment');
    }
  }catch(e){configuration=null;message(e.message,true);if(session){panel.hidden=false;form.hidden=true;}}
  form.onsubmit=async event=>{
    const bank=new FormData(form).get('bank_choice')==='on';
    if(bank || !configuration?.enabled){if(busy||session||completed){event.preventDefault();return;}return legacy.call(form,event);}
    event.preventDefault();if(busy||session||!selected)return;
    busy=true;document.getElementById('submit').disabled=true;
    const data=new FormData(form);
    if(!session){const bytes=crypto.getRandomValues(new Uint8Array(32));session={request_id:requestId,secret:Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join(''),state:'CREATING'};save();}
    pending={...selected,course,request_id:session.request_id,checkout_secret:session.secret,booking_language:english?'en':'ja',payment_method:'CARD',
      attribution:window.TsukiyaAttribution?.data(),guest_name:data.get('guest_name'),email:data.get('email'),phone:data.get('phone'),receipt_name:data.get('receipt_name')||'',
      guest_note:data.get('guest_note'),celebration_items:data.getAll('celebration_items'),plate_message:data.get('plate_message'),cancellation_policy_accepted:data.get('cancellation_policy_accepted')==='on'};
    const authenticated = window.TsukiyaGuestCards;
    cardAccount = configuration.saved_cards && authenticated
      && ['guest_name','email','phone'].every(field => pending[field].trim() === authenticated[field].trim())
      ? authenticated : null;
    savedChoice.checked = saveChoice.checked = false;
    if (cardAccount?.card) {
      const {brand,last4}=cardAccount.card;
      q('[data-use-saved] span').textContent=t('保存済みカードを使う','Use your saved card') + ' (' + brand + ' •••• ' + last4 + ')';
    }
    try {const result=await api('/api/public/checkouts',pending);session.amount=result.amount;await show(result);if(result.state==='HOLD')await mount();panel.scrollIntoView({behavior:'smooth'});}
    catch(e){
      if(!session?.id && e.status && e.status<500){clear();document.getElementById('submit').disabled=false;}
      panel.hidden=false;message(e.message,true);if(session&&!polling)polling=setInterval(check,5000);
    }finally{busy=false;if(session?.id)controls(session.state);}
  };
  setInterval(()=>{if(session?.expires && session.state==='HOLD'){const seconds=Math.max(0,Math.ceil(session.expires-Date.now()/1000));q('[data-timer]').textContent=t('お席の一時確保 残り ','Table hold remaining: ')+Math.floor(seconds/60)+':'+String(seconds%60).padStart(2,'0');}},1000);
})();
