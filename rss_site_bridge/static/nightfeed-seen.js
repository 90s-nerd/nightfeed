// Track opportunities to see a title, rather than implying the topic was read.
(() => {
  const pending = new Map(), qualified = new Set(), targets = new Map();
  let observer, batchTimer, retryTimer, sending = false;
  const csrf = document.querySelector('meta[name=topic-csrf]')?.content;
  const active = () => !document.hidden && document.hasFocus() && !document.querySelector('dialog:modal');
  const flush = async () => {
    clearTimeout(batchTimer);
    clearTimeout(retryTimer); retryTimer = null;
    if (sending || !pending.size) return;
    const batch = [...pending].slice(0, 100), ids = batch.map(([id]) => id);
    sending = true;
    try {
      const response = await fetch('/api/topics/seen', {method: 'POST', keepalive: true,
        headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf}, body: JSON.stringify({ids, revisions: Object.fromEntries(batch)})});
      if (!response.ok) throw new Error('Seen state was not saved');
      batch.forEach(([id, revision]) => { if (pending.get(id) === revision) pending.delete(id); });
    } catch (_) {
      clearTimeout(retryTimer);
      retryTimer = setTimeout(flush, 5000);
    } finally {
      sending = false;
      if (pending.size && !retryTimer) batchTimer = setTimeout(flush, 1500);
    }
  };
  const qualify = (id, revision = '') => {
    const key = `${id}:${revision}`;
    if (qualified.has(key)) return;
    qualified.add(key); pending.set(id, revision);
    clearTimeout(batchTimer); batchTimer = setTimeout(flush, 1500);
    // Leave NEW visible for this visit; the next visit reads the saved state.
  };
  const exposed = target => {
    if (!active() || !target.isConnected) return false;
    const rect = target.getBoundingClientRect();
    return [.25, .5, .75].every(fraction => {
      const element = document.elementFromPoint(rect.left + rect.width * fraction, rect.top + rect.height / 2);
      return element && target.contains(element);
    });
  };
  const evaluate = (target, state) => {
    const id = Number(target.closest('[data-topic-id]').dataset.topicId);
    const revision = target.closest('[data-topic-id]').dataset.topicRevision || '';
    if (qualified.has(`${id}:${revision}`) || state.ratio < .75 || !exposed(target)) {
      clearTimeout(state.timer); state.timer = null; return;
    }
    if (!state.timer) state.timer = setTimeout(() => {
      state.timer = null;
      if (state.ratio >= .75 && exposed(target)) qualify(id, revision);
    }, 1000);
  };
  const refreshTargets = () => {
    targets.forEach((state, target) => {
      if (!target.isConnected) { clearTimeout(state.timer); observer.unobserve(target); targets.delete(target); }
      else evaluate(target, state);
    });
    document.querySelectorAll('[data-topic-id] [data-topic-title]').forEach(target => {
      if (!target.querySelector('.topic-new, .topic-updated') || targets.has(target)) return;
      targets.set(target, {ratio: 0, timer: null}); observer.observe(target);
    });
  };
  const setupObserver = () => {
    observer?.disconnect();
    targets.forEach(state => { clearTimeout(state.timer); state.timer = null; state.ratio = 0; });
    const fixedHeight = selector => {
      const element = document.querySelector(selector);
      return element && getComputedStyle(element).position === 'fixed' && element.checkVisibility() ? element.getBoundingClientRect().height : 0;
    };
    observer = new IntersectionObserver(entries => entries.forEach(entry => {
      const state = targets.get(entry.target);
      if (!state) return;
      state.ratio = entry.intersectionRatio; evaluate(entry.target, state);
    }), {rootMargin: `-${fixedHeight('.sidebar-header')}px 0px -${fixedHeight('.nav-list')}px 0px`, threshold: [0, .75]});
    targets.forEach((_, target) => observer.observe(target));
    refreshTargets();
  };
  if ('IntersectionObserver' in window) {
    setupObserver();
    new MutationObserver(refreshTargets).observe(document.body, {childList: true, subtree: true, attributes: true, attributeFilter: ['open']});
    // Recheck overlays and focus even when intersection geometry stays unchanged.
    setInterval(() => targets.forEach((state, target) => evaluate(target, state)), 200);
    window.addEventListener('resize', setupObserver);
    window.addEventListener('focus', refreshTargets);
    window.addEventListener('blur', () => targets.forEach(state => { clearTimeout(state.timer); state.timer = null; }));
    document.addEventListener('visibilitychange', () => {
      refreshTargets(); if (document.hidden) flush();
    });
  }
  document.addEventListener('click', event => {
    const link = event.target.closest('[data-topic-id] .topic-link, [data-topic-id] .safe-link');
    if (link && !event.defaultPrevented) {
      const topic = link.closest('[data-topic-id]');
      qualify(Number(topic.dataset.topicId), topic.dataset.topicRevision || ''); flush();
    }
  });
  document.addEventListener('click', async event => {
    const all = event.target.closest('[data-topics-seen-all]');
    if (all && !all.disabled) {
      const form = all.closest('form'), status = form.querySelector('[data-topics-seen-status]');
      all.disabled = true;
      try {
        const response = await fetch('/api/topics/seen-all', {method: 'POST',
          headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf}, body: JSON.stringify({browse: form.querySelector('[name=browse]').value})});
        if (!response.ok) throw new Error('Seen update failed');
        window.nightfeedStatus(status, `${(await response.json()).seen} topics marked seen.`);
        form.querySelector('[name=browse]').value = '';
        form.requestSubmit();
      } catch (_) { window.nightfeedStatus(status, 'Could not mark topics seen. Reload and try again.', 'error'); }
      finally { all.disabled = false; }
      return;
    }
    const button = event.target.closest('[data-topic-save]');
    if (!button || button.disabled) return;
    const topic = button.closest('[data-topic-id]');
    const saved = button.getAttribute('aria-pressed') !== 'true';
    const status = topic.querySelector('.topic-action-status');
    button.disabled = true; status.textContent = '';
    try {
      const response = await fetch(`/api/topics/${topic.dataset.topicId}/save`, {method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf}, body: JSON.stringify({saved})});
      if (!response.ok) throw new Error('Save failed');
      button.setAttribute('aria-pressed', String(saved));
      button.setAttribute('aria-label', saved ? 'Remove from saved' : 'Save for later');
      button.querySelector('span').textContent = saved ? 'Saved' : 'Save for later';
      document.querySelectorAll(`[data-topic-id="${topic.dataset.topicId}"] [data-topic-save]`).forEach(other => {
        other.setAttribute('aria-pressed', String(saved));
        other.setAttribute('aria-label', saved ? 'Remove from saved' : 'Save for later');
        other.querySelector('span').textContent = saved ? 'Saved' : 'Save for later';
      });
    } catch (_) { status.textContent = 'Could not save this change. Try again.'; }
    finally { button.disabled = false; }
  });
  window.addEventListener('pagehide', flush);
  window.addEventListener('online', () => { clearTimeout(retryTimer); retryTimer = null; flush(); });
})();
