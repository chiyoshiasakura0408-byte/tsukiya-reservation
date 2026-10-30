(function () {
  let loading = false;
  const money = n => '¥' + Number(n).toLocaleString('ja-JP');
  async function refresh() {
    if (loading || document.hidden) return;
    loading = true;
    try {
      const response = await fetch('/api/customer-party-changes', {cache: 'no-store', credentials: 'same-origin'});
      if (!response.ok) return;
      const {changes} = await response.json();
      let panel = document.getElementById('customerPartyAdjustments');
      if (!changes.length && !panel) return;
      if (!panel) {
        panel = document.createElement('section');
        panel.id = 'customerPartyAdjustments'; panel.className = 'card';
        panel.style.margin = '16px';
        document.querySelector('main').append(panel);
      }
      panel.replaceChildren();
      const title = document.createElement('h2');
      const outstanding = changes.filter(r => r.state === 'OPEN').length;
      title.textContent = 'お客様の人数変更・差額精算' + (outstanding ? `（要対応 ${outstanding}件）` : '');
      panel.append(title);
      const note = document.createElement('p');
      note.textContent = '人数は反映済みです。追加請求・返金は自動実行していません。決済元の取引と変更履歴を確認し、実際の入金・返金完了後に精算済みとして記録してください。';
      panel.append(note);
      for (const r of changes) {
        const box = document.createElement('article');
        box.style.cssText = 'border-top:1px solid #ddd;padding:14px 0';
        const text = document.createElement('p');
        text.textContent = `予約 #${r.reservation_id} ${r.guest_name}様｜${r.visit_at.replace('T', ' ')}｜${r.old_party}名 → ${r.new_party}名｜総額 ${money(r.new_amount)}（うち減員キャンセル料 ${money(r.fee_total)}）｜基準支払済額 ${money(r.paid_amount)}｜` +
          (r.state !== 'OPEN' ? '精算待ちなし' : r.balance > 0 ? `追加請求・入金待ち ${money(r.balance)}` : `返金対応待ち ${money(-r.balance)}`);
        box.append(text);
        if (r.state === 'OPEN') {
          const button = document.createElement('button');
          button.type = 'button'; button.textContent = '朝倉確認済み・差額精算を記録';
          const status = document.createElement('p'); status.setAttribute('role', 'status');
          button.onclick = async () => {
            const action = r.balance > 0 ? `${money(r.balance)}の追加入金` : `${money(-r.balance)}の返金`;
            if (!confirm(`${r.guest_name}様の${action}が完了していることを朝倉が確認済みとして記録します。実際の送金・返金・請求送信はこのボタンでは行いません。`)) return;
            button.disabled = true;
            try {
              const response = await fetch(`/api/reservations/${r.reservation_id}/party-settlement`, {
                method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json', 'X-Tsukiya-Action': 'party-settlement'},
                body: JSON.stringify({change_id: r.id, expected_balance: r.balance, owner_confirmed: true})
              });
              const result = await response.json();
              if (!response.ok) throw Error(result.error || '保存できませんでした');
              await refresh();
            } catch (error) { status.textContent = error.message; button.disabled = false; }
          };
          box.append(button, status);
        }
        panel.append(box);
      }
      let badge = document.getElementById('customerPartyBadge');
      if (!badge) {
        const link = document.querySelector('nav[aria-label="管理画面"] a[href="/"]');
        if (link) { badge = document.createElement('span'); badge.id = 'customerPartyBadge'; link.append(badge); }
      }
      if (badge) badge.textContent = outstanding ? ` 差額${outstanding}件` : '';
    } catch (error) { console.error('人数変更の精算状況を取得できませんでした'); }
    finally { loading = false; }
  }
  refresh(); setInterval(refresh, 60000);
})();
