(() => {
  const links = [...document.querySelectorAll('nav[aria-label="管理画面"] a')]
    .filter(link => link.getAttribute('href') === '/');
  if (!links.length) return;
  const badges = links.map(link => {
    const badge = document.createElement('span');
    badge.className = 'refund-action-badge';
    badge.hidden = true;
    badge.setAttribute('role', 'status');
    badge.style.cssText = 'background:#c62828;color:white;border-radius:999px;min-width:20px;padding:2px 6px;margin-left:6px;font-size:12px;text-align:center;';
    link.append(badge);
    return badge;
  });
  let busy = false;
  async function refresh() {
    if (busy) return;
    busy = true;
    try {
      const response = await fetch('/api/refunds', {cache: 'no-store'});
      if (!response.ok) throw new Error('unavailable');
      const data = await response.json();
      const count = data.action_required_count;
      if (!Number.isInteger(count) || count < 0) throw new Error('invalid count');
      for (const badge of badges) {
        badge.textContent = String(count);
        badge.hidden = count === 0;
        badge.setAttribute('aria-label', `返金対応 ${count}件`);
        badge.title = `返金対応 ${count}件`;
      }
    } catch (_) {
      for (const badge of badges) {
        badge.hidden = false;
        badge.textContent = '!';
        badge.setAttribute('aria-label', '返金状況を確認できません');
        badge.title = '返金状況を確認できません';
      }
    } finally { busy = false; }
  }
  document.addEventListener('refund-status-updated', refresh);
  window.addEventListener('focus', refresh);
  refresh();
  setInterval(refresh, 30000);
})();
