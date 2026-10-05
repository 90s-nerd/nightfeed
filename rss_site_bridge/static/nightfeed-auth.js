// Preserve feature-specific security tokens; add the session-bound global token.
(() => {
  const token = document.querySelector('meta[name="auth-csrf"]')?.content;
  if (!token) return;
  const original = window.fetch.bind(window);
  window.fetch = (input, options = {}) => {
    const target = new URL(input instanceof Request ? input.url : input, location.href);
    const method = (options.method || (input instanceof Request ? input.method : 'GET')).toUpperCase();
    if (target.origin === location.origin && !['GET', 'HEAD', 'OPTIONS'].includes(method)) {
      const headers = new Headers(options.headers || (input instanceof Request ? input.headers : undefined));
      headers.set('X-Nightfeed-CSRF', token);
      options = {...options, headers};
    }
    return original(input, options);
  };
})();

document.addEventListener('DOMContentLoaded', () => {
  // Clear private download submission state without removing the service worker,
  // the push-device token, or the user's appearance preference.
  document.querySelector('[data-auth-logout]')?.addEventListener('submit', () => {
    try {
      for (const key of Object.keys(sessionStorage)) {
        if (key.startsWith('nightfeed-submissions:')) sessionStorage.removeItem(key);
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
