(() => {
  'use strict';
  const booking = document.getElementById('booking');
  const calendar = document.getElementById('start');
  if (!booking || !calendar) return;
  const en = document.documentElement.lang === 'en';
  const t = (ja, english) => en ? english : ja;
  const supported = !!(window.isSecureContext && window.PublicKeyCredential && navigator.credentials);
  const panel = document.createElement('section');
  panel.className = 'card guest-passkeys';
  panel.setAttribute('aria-labelledby', 'guest-passkeys-heading');
  panel.innerHTML = `
    <svg aria-hidden="true" width="42" height="42" viewBox="0 0 48 48" fill="none"
         stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
      <path d="M6 15v-5a4 4 0 0 1 4-4h5m18 0h5a4 4 0 0 1 4 4v5m0 18v5a4 4 0 0 1-4 4h-5m-18 0h-5a4 4 0 0 1-4-4v-5M17 18v2m14-2v2m-7-2v7h-2m-4 5c3 3 9 3 12 0"/>
    </svg>
    <h2 id="guest-passkeys-heading">${t('まいど おおきに', '「Maido Ookini！」')}</h2>
    ${en ? '<p class="guest-passkeys-subtitle">- Welcome Back -</p>' : ''}
    <p>${t('パスキーを登録されたお客様は登録情報を呼び出せます。', 'Returning guests can securely retrieve their saved information using their device’s authentication.')}</p>
    <button type="button" class="primary" data-retrieve>${t('登録情報を呼び出す', 'Retrieve My Information')}</button>
    <p class="sub">${t('Face ID・指紋認証・端末のPINなどに対応', 'Face ID · Fingerprint · Device PIN')}</p>
    ${en ? '<button type="button" data-first>First-Time Guests</button>' : ''}
    <p data-feedback role="status" aria-live="polite"></p>
    <details data-setup>
      <summary>${t('パスキーを登録・再登録する', 'Set up or replace a passkey')}</summary>
      <p>${t('確定済みのWeb予約に使用した連絡先を確認します。再登録すると以前のパスキーは無効になります。', 'Use the contact details from a confirmed online reservation. Replacing a passkey disables the previous one.')}</p>
      <form data-request>
        <label>${t('予約番号', 'Reservation reference')}<input name="reference" inputmode="numeric" pattern="[1-9][0-9]{0,17}" required></label>
        <label>${t('メールアドレス', 'Email address')}<input name="email" type="email" maxlength="254" required></label>
        <label>${t('電話番号', 'Phone number')}<input name="phone" type="tel" maxlength="24" required></label>
        <button type="submit">${t('確認コードを送る', 'Send verification codes')}</button>
      </form>
      <form data-codes hidden>
        <label>${t('メール確認コード', 'Email code')}<input name="email_code" inputmode="numeric" pattern="[0-9]{6}" maxlength="6" required></label>
        <label>${t('SMS確認コード', 'SMS code')}<input name="sms_code" inputmode="numeric" pattern="[0-9]{6}" maxlength="6" required></label>
        <button type="submit">${t('コードを確認し、端末に登録する', 'Verify codes and save passkey')}</button>
      </form>
      <button type="button" data-forget>${t('登録したパスキーを解除する', 'Remove my passkey')}</button>
    </details>`;
  document.querySelector('main').prepend(panel);
  const receipt = document.createElement('label');
  receipt.textContent = t('領収書宛名（任意）', 'Receipt name (optional)');
  const receiptInput = document.createElement('input');
  receiptInput.name = 'receipt_name';
  receiptInput.maxLength = 150;
  receipt.append(receiptInput);
  booking.elements.email.closest('label').after(receipt);

  const setup = panel.querySelector('[data-setup]');
  const query = selector => panel.querySelector(selector) || (setup.matches(selector) ? setup : setup.querySelector(selector));
  const feedback = query('[data-feedback]');
  const codes = query('[data-codes]');
  let verificationTicket = '';
  let busy = false;
  const names = ['guest_name', 'phone', 'email', 'receipt_name'];
  let filled = {};
  function status(message) {
    feedback.textContent = message;
  }
  function focusBooking() {
    const target = booking.closest('[hidden]') ? calendar : booking.elements.guest_name;
    target.scrollIntoView({block: 'center'});
    target.focus({preventScroll: true});
  }
  async function api(action, body = {}) {
    const response = await fetch('/api/public/guest-passkeys/' + action, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      credentials: 'same-origin', cache: 'no-store', body: JSON.stringify(body)
    });
    if (!response.ok) throw new Error(String(response.status));
    return response.json();
  }
  function fromBase64(text) {
    return Uint8Array.from(atob(text.replace(/-/g, '+').replace(/_/g, '/')), character => character.charCodeAt(0));
  }
  function toBase64(buffer) {
    return buffer === null ? null : btoa(String.fromCharCode(...new Uint8Array(buffer)))
      .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  }
  function credentialJSON(credential, registering) {
    const response = credential.response;
    const fields = registering ? ['attestationObject'] : ['authenticatorData', 'signature', 'userHandle'];
    return {
      id: credential.id, rawId: toBase64(credential.rawId), type: credential.type,
      response: Object.fromEntries(['clientDataJSON', ...fields].map(field => [field, toBase64(response[field])]))
    };
  }
  async function run(operation) {
    if (busy) return;
    busy = true;
    status('');
    const buttons = new Set([...panel.querySelectorAll('button:not([data-first])'), ...setup.querySelectorAll('button')]);
    buttons.forEach(button => { button.disabled = true; });
    try {
      await operation();
    } catch (error) {
      status(error.message === '429'
        ? t('試行回数の上限に達しました。時間をおいて再度お試しください。', 'Too many attempts. Please try again later.')
        : t('認証を完了できませんでした。再度お試しいただくか、予約情報を手入力してください。', 'Authentication could not be completed. Please try again or enter your details manually.'));
    } finally {
      busy = false;
      buttons.forEach(button => { button.disabled = false; });
    }
  }
  async function retrieve(forget) {
    if (forget && !window.confirm(t('このパスキーでの呼び出しを解除しますか？', 'Remove this passkey from your booking profile?'))) return;
    if (!forget && names.some(name => booking.elements[name].value)
        && !window.confirm(t('入力済みの連絡先を登録情報で置き換えますか？', 'Replace the details already entered with your saved information?'))) return;
    const {ticket, options} = await api('login-options');
    options.challenge = fromBase64(options.challenge);
    const credential = await navigator.credentials.get({publicKey: options});
    const result = await api('login', {ticket, credential: credentialJSON(credential, false), forget});
    if (forget) {
      window.TsukiyaGuestCards = null;
      status(t('パスキーを解除しました。', 'Your passkey was removed.'));
      return;
    }
    window.TsukiyaGuestCards = result.card_grant ? {
      grant: result.card_grant, card: result.saved_card,
      guest_name: result.guest_name, email: result.email, phone: result.phone
    } : null;
    if (window.TsukiyaGuestCards) window.dispatchEvent(new Event('tsukiya-passkey-card-verified'));
    filled = {};
    for (const name of names) {
      const field = booking.elements[name];
      if (typeof result[name] === 'string') {
        field.value = result[name];
        filled[name] = result[name];
      }
    }
    status(t('登録情報を入力しました。内容をご確認ください。', 'Your saved details have been filled in. Please review them.'));
    focusBooking();
  }
  function clearAutofill() {
    window.TsukiyaGuestCards = null;
    for (const [name, value] of Object.entries(filled)) {
      if (booking.elements[name].value === value) booking.elements[name].value = '';
    }
    filled = {};
  }
  query('[data-first]')?.addEventListener('click', () => {
    clearAutofill();
    status('');
    focusBooking();
  });
  window.addEventListener('pagehide', clearAutofill);
  if (!supported) {
    query('[data-retrieve]').disabled = true;
    query('[data-setup]').hidden = true;
    status(t('このブラウザでは端末認証を利用できません。予約情報は手入力できます。', 'Device authentication is unavailable in this browser. You can still enter your details manually.'));
    return;
  }
  if (document.body.hasAttribute('data-passkey-email-only')) {
    setup.hidden = true;
    query('[data-retrieve]').addEventListener('click', () => run(() => retrieve(false)));
    return;
  }
  window.TsukiyaPasskeySetup = (container, details) => {
    const request = query('[data-request]');
    for (const name of ['reference', 'email', 'phone']) {
      request.elements[name].value = details[name];
      request.elements[name].closest('label').hidden = true;
    }
    setup.open = true;
    setup.querySelector('summary').hidden = true;
    setup.querySelector('p').hidden = true;
    query('[data-forget]').hidden = true;
    container.append(setup, feedback);
    request.querySelector('button').textContent = t('確認コードを再送する','Resend verification codes');
    request.requestSubmit();
  };
  query('[data-retrieve]').addEventListener('click', () => run(() => retrieve(false)));
  query('[data-forget]').addEventListener('click', () => run(() => retrieve(true)));
  query('[data-request]').addEventListener('submit', event => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(event.currentTarget));
    run(async () => {
      verificationTicket = '';
      codes.reset();
      codes.hidden = true;
      const {ticket} = await api('verify', data);
      verificationTicket = ticket;
      codes.hidden = false;
      status(t('予約情報が一致する場合はメールとSMSに確認コードが届きます。', 'If your reservation details match, codes will arrive by email and SMS.'));
      codes.elements.email_code.focus();
    });
  });
  codes.addEventListener('submit', event => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(codes));
    run(async () => {
      const ticket = verificationTicket;
      verificationTicket = '';
      codes.reset();
      codes.hidden = true;
      const registration = await api('register-options', {...data, ticket});
      const options = registration.options;
      options.challenge = fromBase64(options.challenge);
      options.user.id = fromBase64(options.user.id);
      options.excludeCredentials = (options.excludeCredentials || []).map(item => ({...item, id: fromBase64(item.id)}));
      const credential = await navigator.credentials.create({publicKey: options});
      const result = await api('register', {ticket: registration.ticket, credential: credentialJSON(credential, true)});
      if (result.card_save_grant) {
        window.dispatchEvent(new CustomEvent('tsukiya-passkey-card-verified', {detail:{grant:result.card_save_grant}}));
      }
      query('[data-request]').reset();
      setup.open = false;
      if (!panel.contains(setup)) setup.hidden = true;
      status(t('パスキーを登録しました。次回から登録情報を呼び出せます。', 'Your passkey is ready. You can retrieve your saved details next time.'));
    });
  });
})();
