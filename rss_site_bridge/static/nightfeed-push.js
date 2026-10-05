(() => {
  const section = document.querySelector('[data-push-settings]');
  if (!section) return;
  const csrf = document.querySelector('meta[name=push-csrf]').content;
  const form = section.querySelector('[data-push-preferences]');
  const state = section.querySelector('[data-push-state]'), error = section.querySelector('[data-push-error]');
  const enable = section.querySelector('[data-push-enable]'), test = section.querySelector('[data-push-test]'), disable = section.querySelector('[data-push-disable]');
  const feedback = section.querySelector('[data-push-feedback]');
  const guide = document.querySelector('[data-push-guide-dialog]');
  const ios = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const installed = () => navigator.standalone || matchMedia('(display-mode: standalone)').matches;
  let token = '', registration, config, installPrompt, step = 0, storage = true, renewSubscription = false;
  try { token = localStorage.getItem('nightfeed.push.device') || ''; localStorage.setItem('nightfeed.push.check', '1'); localStorage.removeItem('nightfeed.push.check'); }
  catch (_) { storage = false; }
  const api = async (path, data) => {
    const response = await fetch(`/api/push/${path}`, data === undefined ? {} : {method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf}, body: JSON.stringify(data)});
    const result = await response.json();
    if (!response.ok) {
      const failure = new Error(result.error || 'Notification settings are unavailable.');
      failure.status = response.status;
      throw failure;
    }
    return result;
  };
  const showError = message => { error.textContent = message; error.hidden = !message; };
  const syncChoices = () => {
    form.querySelector('[data-push-feeds]').hidden = form.querySelector('[data-push-all-feeds]').checked;
    form.querySelector('[data-push-quiet]').hidden = !form.elements.quiet.checked;
  };
  form.addEventListener('change', syncChoices);
  const fill = prefs => {
    ['new', 'updated', 'failures', 'quiet'].forEach(key => { form.elements[key].checked = prefs[key]; });
    ['interval', 'daily_limit', 'quiet_start', 'quiet_end'].forEach(key => { form.elements[key].value = prefs[key]; });
    form.querySelector('[data-push-all-feeds]').checked = !prefs.feeds.length;
    form.querySelectorAll('[name=feed]').forEach(input => { input.checked = prefs.feeds.includes(Number(input.value)); });
    syncChoices();
  };
  const read = () => ({new: form.elements.new.checked, updated: form.elements.updated.checked, failures: form.elements.failures.checked,
    interval: Number(form.elements.interval.value), daily_limit: Number(form.elements.daily_limit.value), quiet: form.elements.quiet.checked,
    quiet_start: form.elements.quiet_start.value, quiet_end: form.elements.quiet_end.value,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC',
    feeds: form.querySelector('[data-push-all-feeds]').checked ? [] : [...form.querySelectorAll('[name=feed]:checked')].map(input => Number(input.value))});
  const subscribed = enabled => {
    state.textContent = '';
    form.hidden = !enabled; test.hidden = !enabled; disable.hidden = !token; enable.hidden = enabled;
  };
  const renderGuide = () => {
    const steps = ios ? [
      ['Add to Home Screen', 'In Safari, tap Share, choose Add to Home Screen (it may be under More), then tap Add.'],
      ['Choose your notifications', 'Open the Nightfeed icon on your Home Screen. In Settings, tap Enable on this device, allow notifications, then choose your feeds and preferences.']
    ] : [
      ['Install Nightfeed', 'Use Install Nightfeed below if available, or the browser menu’s Install app / Add to Home screen option.'],
      ['Choose your notifications', 'Open Nightfeed from its new icon. In Settings, enable notifications and select your feeds, summary frequency, and daily limit.']
    ];
    guide.querySelector('[data-push-guide-counter]').textContent = `Step ${step + 1} of ${steps.length}`;
    guide.querySelector('[data-push-guide-step-title]').textContent = steps[step][0];
    guide.querySelector('[data-push-guide-step-text]').textContent = steps[step][1];
    guide.querySelector('[data-push-guide-back]').hidden = step === 0;
    guide.querySelector('[data-push-guide-next]').textContent = step === steps.length - 1 ? 'Done' : 'Next';
    guide.querySelector('[data-push-install]').hidden = ios || step !== 0 || !installPrompt;
  };
  const openGuide = () => { step = 0; renderGuide(); guide.showModal(); };
  section.querySelector('[data-push-guide]').addEventListener('click', openGuide);
  guide.querySelector('[data-push-guide-close]').addEventListener('click', () => guide.close());
  guide.querySelector('[data-push-guide-back]').addEventListener('click', () => { step--; renderGuide(); });
  guide.querySelector('[data-push-guide-next]').addEventListener('click', () => { if (step === 1) guide.close(); else { step++; renderGuide(); } });
  window.addEventListener('beforeinstallprompt', event => { event.preventDefault(); installPrompt = event; renderGuide(); });
  guide.querySelector('[data-push-install]').addEventListener('click', async () => {
    if (!installPrompt) return;
    await installPrompt.prompt(); await installPrompt.userChoice; installPrompt = null; renderGuide();
  });
  enable.addEventListener('click', async () => {
    if (ios && !installed()) { openGuide(); return; }
    enable.disabled = true; showError('');
    try {
      // Invoke permission directly from this click, particularly for Safari.
      const permission = await Notification.requestPermission();
      if (permission !== 'granted') throw new Error('Notifications were not allowed. Change this site’s notification permission in your device settings, then try again.');
      registration ||= await navigator.serviceWorker.register('/service-worker.js', {scope: '/'});
      await navigator.serviceWorker.ready;
      let sub = await registration.pushManager.getSubscription();
      if (sub && renewSubscription) { await sub.unsubscribe(); sub = null; }
      if (!sub) {
        const key = config.public_key.replace(/-/g, '+').replace(/_/g, '/');
        const bytes = Uint8Array.from(atob(key + '='.repeat((4 - key.length % 4) % 4)), character => character.charCodeAt(0));
        sub = await registration.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: bytes});
      }
      const result = await api('subscribe', {device_token: token, subscription: sub.toJSON(), preferences: read()});
      token = result.device_token; localStorage.setItem('nightfeed.push.device', token);
      renewSubscription = false; fill(result.preferences); subscribed(true);
    } catch (failure) { showError(failure.message); }
    finally { enable.disabled = false; }
  });
  form.addEventListener('submit', async event => {
    event.preventDefault(); showError('');
    const button = form.querySelector('[type=submit]'); button.disabled = true;
    try {
      const prefs = read();
      if (!form.querySelector('[data-push-all-feeds]').checked && !prefs.feeds.length) throw new Error('Select at least one feed or choose All feeds.');
      await api('preferences', {device_token: token, preferences: prefs});
      window.nightfeedStatus(feedback, 'Notification preferences saved.');
    } catch (failure) { showError(failure.message); }
    finally { button.disabled = false; }
  });
  test.addEventListener('click', async () => {
    test.disabled = true; showError('');
    try { const result = await api('test', {device_token: token}); window.nightfeedStatus(feedback, result.message); }
    catch (failure) { showError(failure.message); }
    finally { test.disabled = false; }
  });
  disable.addEventListener('click', async () => {
    disable.disabled = true; showError('');
    try {
      await api('disable', {device_token: token});
      subscribed(false); disable.hidden = true;
      const sub = await registration?.pushManager.getSubscription(); if (sub) await sub.unsubscribe();
    } catch (failure) { showError(failure.message); }
    finally { disable.disabled = false; }
  });
  (async () => {
    if (!window.isSecureContext) { state.textContent = 'Use Nightfeed’s HTTPS address to enable notifications.'; return; }
    if (!storage) { state.textContent = 'Allow browser storage on this device to manage notifications.'; return; }
    if (ios && !installed()) { state.textContent = 'Add Nightfeed to your Home Screen to enable notifications.'; enable.disabled = false; return; }
    if (!('serviceWorker' in navigator) || !('PushManager' in window) || !('Notification' in window)) {
      state.textContent = 'This browser does not support mobile push. Try Safari on iPhone or Chrome on Android.'; return;
    }
    try {
      config = await api('config');
      const feeds = form.querySelector('[data-push-feeds]');
      config.feeds.forEach(feed => {
        const label = document.createElement('label'); label.className = 'check-option';
        const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.name = 'feed'; checkbox.value = feed.id;
        const name = document.createElement('span'); name.textContent = feed.feed_title; label.append(checkbox, name); feeds.append(label);
      });
      fill(config.defaults);
      registration = await navigator.serviceWorker.register('/service-worker.js', {scope: '/'});
      if (token) {
        try {
          const result = await api('device', {device_token: token});
          await navigator.serviceWorker.ready;
          const subscription = await registration.pushManager.getSubscription();
          fill(result.preferences); subscribed(result.enabled && Notification.permission === 'granted' && !!subscription);
          renewSubscription = !result.enabled && !!result.error;
          disable.hidden = !result.enabled;
          if (result.error) showError(result.error);
          else if (result.enabled && !subscription) showError('This browser’s notification subscription is missing. Enable on this device to restore notifications.');
        } catch (failure) {
          // A temporary network, server, or login failure must retain device identity.
          if (failure.status === 400) { token = ''; localStorage.removeItem('nightfeed.push.device'); }
          subscribed(false); showError(failure.message);
        }
      } else subscribed(false);
      enable.disabled = false;
    } catch (failure) { state.textContent = 'Notification settings could not be loaded.'; showError(failure.message); }
  })();
})();
