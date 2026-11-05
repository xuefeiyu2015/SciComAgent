/* Access gate for the hosted app (backend/webui/access.py decides; this only asks).
 *
 * The backend answers 401 {"error": "locked"} to every /api call until this
 * browser holds its access cookie. On load, and whenever a call comes back 401,
 * this shows one small dialog for the access key; the right key sets the cookie
 * (HttpOnly, on the backend's address) and the page reloads. A local run has no
 * gate, so the dialog never appears there. */
(function () {
  const API = globalThis.SCICOM_API_BASE || '';
  const zh = (navigator.language || '').toLowerCase().startsWith('zh');
  const text = zh
    ? { title: '需要访问密钥', lead: '这个应用会调用付费的模型接口，请输入管理员给你的访问密钥。', ph: '访问密钥', go: '解锁', wrong: '密钥不对，请再试一次。', down: '连不上服务器，请稍后再试。' }
    : { title: 'Access key required', lead: 'This app calls paid model APIs. Enter the access key an admin gave you.', ph: 'Access key', go: 'Unlock', wrong: 'That key isn’t right. Try again.', down: 'Can’t reach the server. Try again shortly.' };
  let shown = false;

  function show() {
    if (shown) return;
    shown = true;
    const veil = document.createElement('div');
    veil.setAttribute('role', 'dialog');
    veil.setAttribute('aria-modal', 'true');
    veil.style.cssText = 'position:fixed;inset:0;z-index:9999;background:rgba(15,18,25,.55);display:grid;place-items:center;padding:16px;';
    veil.innerHTML =
      '<form style="background:#fff;color:#1b1f27;max-width:360px;width:100%;border-radius:12px;padding:22px 22px 18px;box-shadow:0 20px 50px rgba(0,0,0,.25);font:15px/1.5 system-ui,sans-serif">' +
      '<h2 style="margin:0 0 6px;font-size:18px"></h2><p style="margin:0 0 14px;color:#555;font-size:13.5px"></p>' +
      '<input type="password" autocomplete="off" style="width:100%;box-sizing:border-box;padding:9px 11px;border:1px solid #ccd;border-radius:8px;font:inherit">' +
      '<p data-err style="min-height:20px;margin:6px 0 8px;color:#b42318;font-size:13px"></p>' +
      '<button type="submit" style="width:100%;padding:9px;border:0;border-radius:8px;background:#1b1f27;color:#fff;font:inherit;font-weight:600;cursor:pointer"></button></form>';
    const form = veil.querySelector('form');
    form.querySelector('h2').textContent = text.title;
    form.querySelector('p').textContent = text.lead;
    const input = form.querySelector('input');
    input.placeholder = text.ph;
    const err = form.querySelector('[data-err]');
    const btn = form.querySelector('button');
    btn.textContent = text.go;
    form.onsubmit = async (e) => {
      e.preventDefault();
      if (!input.value.trim()) return;
      btn.disabled = true;
      try {
        const r = await fetch(API + '/api/unlock', {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ key: input.value }),
        });
        if (r.ok) return location.reload();
        err.textContent = text.wrong;
        input.value = '';
      } catch {
        err.textContent = text.down;
      }
      btn.disabled = false;
      input.focus();
    };
    document.body.append(veil);
    input.focus();
  }

  globalThis.SciComGate = { show };
  addEventListener('DOMContentLoaded', () => {
    fetch(API + '/api/session', { credentials: 'include' })
      .then((r) => r.json())
      .then((s) => { if (s.gated && !s.unlocked) show(); })
      .catch(() => {});
  });
})();
