// Preserve feature-specific security tokens; add the session-bound global token.
(() => {
  const token = document.querySelector('meta[name="auth-csrf"]')?.content;
  if (!token) return;
  const original = window.fetch.bind(window);
  const statusUrl = document.querySelector('meta[name="auth-session-status"]')?.content;
  let redirecting = false, checking = false, deadline;
  const mask = () => {
    document.documentElement.classList.add('auth-checking');
    if (!document.body || document.getElementById('auth-session-check')) return;
    const notice = document.createElement('div');notice.id='auth-session-check';notice.setAttribute('role','status');
    notice.textContent='Checking your session…';document.body.append(notice);
  };
  const expired = () => {
    if (redirecting) return;
    redirecting=true;mask();
    try { for (const key of Object.keys(sessionStorage)) if (key.startsWith('nightfeed-submissions:') || key.startsWith('nightfeed-assistant:')) sessionStorage.removeItem(key); } catch (_) {}
    location.replace('/auth/login?sso=off&next='+encodeURIComponent(location.pathname+location.search));
  };
  window.fetch = async (input, options = {}) => {
    const target = new URL(input instanceof Request ? input.url : input, location.href);
    const method = (options.method || (input instanceof Request ? input.method : 'GET')).toUpperCase();
    if (target.origin === location.origin && !['GET', 'HEAD', 'OPTIONS'].includes(method)) {
      const headers = new Headers(options.headers || (input instanceof Request ? input.headers : undefined));
      headers.set('X-Nightfeed-CSRF', token);
      options = {...options, headers};
    }
    const response = await original(input, options);
    if (statusUrl && target.origin === location.origin && response.status === 401) expired();
    return response;
  };
  if (statusUrl) {
    const check = async (hide = false) => {
      if (hide) mask();
      if (checking || redirecting || document.hidden) return;
      checking=true;
      const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),10000);
      try {
        const response=await original(statusUrl,{credentials:'same-origin',cache:'no-store',signal:controller.signal});
        if (response.status===401) { expired();return; }
        if (!response.ok) throw new Error('Session check unavailable');
        const state=await response.json();
        if (!state.authenticated || !Number.isFinite(state.expires_in)) throw new Error('Invalid session check');
        if (!redirecting && !document.hidden) { document.documentElement.classList.remove('auth-checking');document.getElementById('auth-session-check')?.remove(); }
        clearTimeout(deadline);
        deadline=setTimeout(()=>check(),Math.min(2147483647,Math.max(250,state.expires_in*1000+100)));
      } catch (_) {
        const notice=document.getElementById('auth-session-check');
        if (notice) notice.textContent='Reconnect to check your session.';
      } finally { clearTimeout(timeout);checking=false; }
    };
    document.addEventListener('DOMContentLoaded',()=>{ if (document.documentElement.classList.contains('auth-checking')) mask();check(); });
    document.addEventListener('visibilitychange',()=>{ if (document.hidden) mask();else check(true); });
    window.addEventListener('pageshow',event=>check(event.persisted));
    window.addEventListener('focus',()=>check(true));
    window.addEventListener('online',()=>check(true));
    setInterval(()=>check(),30000);
  }
})();

document.addEventListener('DOMContentLoaded', () => {
  // Clear private download submission state without removing the service worker,
  // the push-device token, or the user's appearance preference.
  document.querySelector('[data-auth-logout]')?.addEventListener('submit', () => {
    try {
      for (const key of Object.keys(sessionStorage)) {
        if (key.startsWith('nightfeed-submissions:') || key.startsWith('nightfeed-assistant:')) sessionStorage.removeItem(key);
      }
    } catch (_) { /* Server-side session revocation does not depend on storage. */ }
  });
  const form = document.querySelector('[data-profile-form]');
  if (form) {
    const choice = form.querySelector('[data-provider-name]');
    const name = form.querySelector('[data-display-name]');
    const help = form.querySelector('[data-name-help]');
    let localName = name.dataset.localName;
    choice.addEventListener('change', () => {
      if (choice.checked) localName = name.value;
      name.readOnly = choice.checked;
      name.value = choice.checked ? name.dataset.ssoName : localName;
      help.hidden = !choice.checked;
    });
  }
  const menu = document.querySelector('.account-menu');
  if (menu) {
    document.addEventListener('click', event => { if (!menu.contains(event.target)) menu.open = false; });
    menu.addEventListener('keydown', event => {
      if (event.key === 'Escape') { menu.open = false; menu.querySelector('summary').focus(); }
    });
  }
});
