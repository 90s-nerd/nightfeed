// Success confirmations expire; progress and errors remain until replaced.
(() => {
  const timers = new WeakMap();
  window.expireNightfeedNotice = element => {
    clearTimeout(timers.get(element));
    timers.set(element, setTimeout(() => {
      element.hidden = true;
      // Dynamic status nodes should not leave an empty paragraph behind.
      if (!element.hasAttribute('data-transient-notice')) element.textContent = '';
      timers.delete(element);
    }, 6000));
  };
  window.nightfeedStatus = (element, message, kind = 'success') => {
    clearTimeout(timers.get(element)); timers.delete(element);
    element.textContent = message; element.hidden = !message;
    if (message && kind === 'success') window.expireNightfeedNotice(element);
  };
  window.clearNightfeedNotices = () => document.querySelectorAll('[data-transient-notice]').forEach(element => {
    clearTimeout(timers.get(element)); timers.delete(element); element.hidden = true;
  });
  document.addEventListener('DOMContentLoaded', () => {
    const url = new URL(location.href);
    let cleaned = false;
    document.querySelectorAll('[data-transient-notice]').forEach(element => {
      window.expireNightfeedNotice(element);
      const parameter = element.dataset.noticeQuery;
      if (parameter && url.searchParams.has(parameter)) { url.searchParams.delete(parameter); cleaned = true; }
    });
    // Refresh and history navigation should not replay an old action confirmation.
    if (cleaned) history.replaceState(history.state, '', url);
  });
})();

// Apply appearance before styles load; storage can be unavailable in private contexts.
(() => {
  const key = 'nightfeed.appearance.v1';
  const choices = ['system', 'light', 'dark'];
  let appearance = 'system';
  try {
    const saved = localStorage.getItem(key);
    if (choices.includes(saved)) appearance = saved;
  } catch (_) { /* Keep the in-memory choice. */ }
  const apply = value => {
    appearance = value;
    document.documentElement.dataset.theme = value;
    document.querySelectorAll('[data-appearance]').forEach(select => { select.value = value; });
  };
  apply(appearance);
  document.addEventListener('DOMContentLoaded', () => {
    apply(appearance);
    document.querySelectorAll('[data-appearance]').forEach(select => {
      select.addEventListener('change', () => {
        if (!choices.includes(select.value)) return;
        apply(select.value);
        try { localStorage.setItem(key, appearance); } catch (_) { /* Remains usable in memory. */ }
      });
    });
  });
  window.addEventListener('storage', event => {
    if (event.key === key || event.key === null) apply(choices.includes(event.newValue) ? event.newValue : 'system');
  });
})();

// Display dates in the device's current timezone, including newly refreshed content.
document.addEventListener('DOMContentLoaded', () => {
  const menus = [...document.querySelectorAll('[data-notification-menu]')];
  const close = (menu, returnFocus = false) => {
    menu.open = false;
    if (returnFocus) menu.querySelector('summary').focus();
  };
  menus.forEach(menu => {
    menu.addEventListener('toggle', () => {
      if (menu.open) {
        menus.forEach(other => { if (other !== menu) close(other); });
        const content = menu.querySelector('.row-menu-content');
        content.style.top = '100%'; content.style.bottom = 'auto';
        if (content.getBoundingClientRect().bottom > innerHeight && menu.getBoundingClientRect().top > content.offsetHeight) {
          content.style.top = 'auto'; content.style.bottom = '100%';
        }
        menu.querySelector('button')?.focus();
      }
    });
    menu.addEventListener('keydown', event => {
      if (!menu.open) return;
      if (event.key === 'Escape') { close(menu, true); event.preventDefault(); }
      if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
        const buttons = [...menu.querySelectorAll('button')];
        const index = buttons.indexOf(document.activeElement);
        const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 : (index + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length;
        buttons[next]?.focus(); event.preventDefault();
      }
    });
    menu.addEventListener('focusout', () => setTimeout(() => {
      if (!menu.contains(document.activeElement)) close(menu);
    }, 0));
  });
  document.addEventListener('click', event => menus.forEach(menu => {
    if (!menu.contains(event.target)) close(menu);
  }));
});

document.addEventListener('DOMContentLoaded', () => {
  const hints = [...document.querySelectorAll('.ui-hint')];
  hints.forEach(hint => {
    const summary = hint.querySelector('summary');
    const field = hint.closest('.field');
    const label = field?.querySelector('label') || hint.closest('.notification-choice')?.querySelector('strong');
    if (label) summary.setAttribute('aria-label', `Help with ${label.textContent.trim()}`);
    hint.addEventListener('toggle', () => {
      if (hint.open) {
        hints.forEach(other => { if (other !== hint) other.open = false; });
        const content = hint.querySelector('.ui-hint-content');
        content.style.transform = '';
        const bounds = content.getBoundingClientRect();
        const shift = bounds.right > innerWidth - 16 ? innerWidth - 16 - bounds.right : bounds.left < 16 ? 16 - bounds.left : 0;
        content.style.transform = `translateX(${shift}px)`;
      }
    });
  });
  document.addEventListener('click', event => {
    hints.forEach(hint => { if (!hint.contains(event.target)) hint.open = false; });
  });
  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape') return;
    hints.filter(hint => hint.open).forEach(hint => {
      hint.open = false;
      hint.querySelector('summary').focus();
      event.preventDefault();
    });
  });
});

(() => {
  const icon = (kind) => {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('aria-hidden', 'true');
    svg.classList.add(kind === 'external' ? 'external-icon' : 'privacy-icon');
    const path = document.createElementNS(svg.namespaceURI, 'path');
    path.setAttribute('d', kind === 'external' ? 'M14 3h7v7M21 3 10 14M10 5H4v15h15v-6' : 'M7 10V7a5 5 0 0 1 10 0v3M5 10h14v11H5zM12 14v3');
    svg.append(path); return svg;
  };
  const format = el => {
    const date = new Date(el.dateTime);
    if (!Number.isNaN(date.valueOf())) {
      const now = new Date();
      const dayKey = value => `${value.getFullYear()}-${value.getMonth()}-${value.getDate()}`;
      const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
      const tomorrow = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1);
      const day = dayKey(date) === dayKey(now) ? 'Today' : dayKey(date) === dayKey(yesterday) ? 'Yesterday' : dayKey(date) === dayKey(tomorrow) ? 'Tomorrow' : new Intl.DateTimeFormat(undefined, {month:'short', day:'numeric', ...(date.getFullYear() !== now.getFullYear() ? {year:'numeric'} : {})}).format(date);
      const clock = new Intl.DateTimeFormat(undefined, {hour:'2-digit', minute:'2-digit'}).format(date);
      const value = `${day} ${clock}`;
      el.title = new Intl.DateTimeFormat(undefined, {dateStyle:'long', timeStyle:'short'}).format(date);
      if (el.textContent !== value) el.textContent = value;
    }
  };
  window.nightfeedTime = (container, iso, fallback) => {
    if (!iso) {container.textContent = fallback; return;}
    const time = document.createElement('time'); time.dateTime = iso; time.dataset.localTime = '';
    format(time); container.replaceChildren(time);
  };
  window.nightfeedUnread = count => {
    if (!Number.isInteger(count) || count < 0) return;
    document.querySelectorAll('[data-unread-notifications]').forEach(badge => {
      badge.textContent = count;
      badge.hidden = !count;
      badge.setAttribute('aria-label', `${count} unread notifications`);
    });
  };
  const enhance = () => {
    document.querySelectorAll('time[data-local-time]').forEach(format);
    document.querySelectorAll('a[href]:not([data-link-enhanced])').forEach(link => {
      link.dataset.linkEnhanced = '';
      try {
        const url = new URL(link.href);
        if (['http:', 'https:'].includes(url.protocol) && url.origin !== location.origin) {
          link.append(icon('external'));
          link.title = link.title || 'Opens an external site';
        }
      } catch (_) { /* Relative or unsupported links need no external marker. */ }
      if (link.classList.contains('safe-link')) link.prepend(icon('privacy'));
    });
  };
  document.addEventListener('DOMContentLoaded', () => {
    enhance();
    new MutationObserver(enhance).observe(document.body, {childList:true, subtree:true});
    document.querySelectorAll('[data-auto-search]').forEach(form => {
      let timer, controller, generation = 0;
      const results = document.querySelector('[data-timeline-results]');
      const status = document.querySelector('[data-search-status]');
      const input = form.querySelector('[name=q]');
      const clear = form.querySelector('.search-clear');
      const settings = form.querySelector('.search-settings');
      const trigger = settings.querySelector('summary');
      const dialog = document.createElement('dialog');
      dialog.className = 'search-settings-dialog'; dialog.id = 'search-settings-dialog';
      dialog.setAttribute('aria-labelledby', 'search-settings-title');
      dialog.append(settings.querySelector('.search-settings-panel')); form.append(dialog);
      trigger.setAttribute('aria-haspopup', 'dialog'); trigger.setAttribute('aria-controls', dialog.id);
      const mobileSettings = matchMedia('(max-width: 767px)');
      let restoreSettingsFocus = true;
      const closeSearchSettings = (restore = true) => {
        restoreSettingsFocus = restore;
        dialog.close();
      };
      trigger.setAttribute('aria-expanded', 'false');
      trigger.addEventListener('click', event => {
        event.preventDefault();
        if (dialog.open) { closeSearchSettings(); return; }
        restoreSettingsFocus = true;
        if (mobileSettings.matches) dialog.showModal(); else dialog.show();
        trigger.setAttribute('aria-expanded', 'true');
      });
      const closeSettings = dialog.querySelector('.search-settings-close');
      closeSettings.hidden = false;
      closeSettings.addEventListener('click', () => closeSearchSettings());
      dialog.addEventListener('close', () => {
        trigger.setAttribute('aria-expanded', 'false');
        if (restoreSettingsFocus) trigger.focus();
      });
      dialog.addEventListener('keydown', event => {
        if (event.key === 'Escape') { event.preventDefault(); closeSearchSettings(); }
      });
      document.addEventListener('pointerdown', event => {
        if (dialog.open && !mobileSettings.matches && !dialog.contains(event.target) && !trigger.contains(event.target)) closeSearchSettings(false);
      });
      dialog.addEventListener('focusout', () => setTimeout(() => {
        if (dialog.open && !mobileSettings.matches && !dialog.contains(document.activeElement) && document.activeElement !== trigger) closeSearchSettings(false);
      }, 0));
      mobileSettings.addEventListener('change', () => { if (dialog.open) closeSearchSettings(); });
      dialog.addEventListener('click', event => {
        const bounds = dialog.getBoundingClientRect();
        if (event.target === dialog && (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom)) closeSearchSettings();
      });
      const cancel = () => {
        clearTimeout(timer);
        controller?.abort();
        generation++;
        results.removeAttribute('aria-busy');
        status.hidden = true;
      };
      const updateFeedLabel = () => {
        const count = form.querySelectorAll('[name=feed]:checked').length;
        const active = count > 0 || form.querySelector('[name=sort]').value !== 'new' || !!form.querySelector('[name=new_only]:checked, [name=saved_only]:checked');
        form.querySelector('.search-filter-dot').hidden = !active;
        trigger.setAttribute('aria-label', `Search settings${active ? ', filters active' : ''}`);
        clear.hidden = !input.value && !active;
      };
      updateFeedLabel();
      clear.addEventListener('click', () => {
        input.value = '';
        form.querySelector('[name=sort]').value = 'new';
        form.querySelectorAll('[name=feed]').forEach(input => { input.checked = false; });
        form.querySelectorAll('[name=new_only], [name=saved_only]').forEach(input => { input.checked = false; });
        input.focus(); form.requestSubmit();
      });
      const load = async (url, historyMode = 'push') => {
        cancel();
        const current = generation;
        controller = new AbortController();
        results.setAttribute('aria-busy', 'true');
        status.textContent = 'Updating timeline…'; status.hidden = false;
        try {
          const response = await fetch(url, {signal: controller.signal});
          if (!response.ok) throw new Error('Timeline request failed');
          const document = new DOMParser().parseFromString(await response.text(), 'text/html');
          const next = document.querySelector('[data-timeline-results]');
          if (!next) throw new Error('Timeline results missing');
          if (current !== generation) return;
          const browse = document.querySelector('[name=browse]')?.value;
          if (browse) {
            form.querySelector('[name=browse]').value = browse;
            url.searchParams.set('browse', browse);
          }
          // Keep the form and its focused input mounted, including on mobile.
          results.replaceChildren(...next.childNodes);
          if (historyMode === 'push' && url.href !== location.href) history.pushState(null, '', url);
          enhance();
          status.textContent = results.querySelector('p').textContent;
          status.hidden = true;
        } catch (error) {
          if (current !== generation || error.name === 'AbortError') return;
          status.textContent = 'Could not update the timeline. ';
          const retry = document.createElement('button');
          retry.type = 'button'; retry.className = 'link-text'; retry.textContent = 'Retry';
          retry.addEventListener('click', () => load(url, historyMode));
          status.append(retry); status.hidden = false;
        } finally {
          if (current === generation) results.removeAttribute('aria-busy');
        }
      };
      const apply = () => { clearTimeout(timer); timer = setTimeout(() => form.requestSubmit(), 450); };
      const isTextInput = target => target.matches('input[type=search], input[type=text], textarea');
      form.addEventListener('input', event => {
        if (!isTextInput(event.target)) return;
        updateFeedLabel();
        cancel();
        if (!event.isComposing) apply();
      });
      form.addEventListener('compositionend', event => { if (isTextInput(event.target)) apply(); });
      form.addEventListener('change', event => {
        if (isTextInput(event.target)) return;
        clearTimeout(timer);
        form.requestSubmit();
      });
      form.addEventListener('submit', event => {
        if (event.defaultPrevented) return;
        event.preventDefault();
        clearTimeout(timer);
        updateFeedLabel();
        const url = new URL(form.action);
        url.search = new URLSearchParams(new FormData(form)).toString();
        load(url);
      });
      const plainClick = event => !event.defaultPrevented && event.button === 0 && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey;
      results.addEventListener('click', event => {
        const link = event.target.closest('.pagination-actions a');
        if (!link || !plainClick(event)) return;
        event.preventDefault(); load(new URL(link.href));
      });
      window.addEventListener('popstate', () => {
        const url = new URL(location.href);
        form.querySelector('[name=q]').value = url.searchParams.get('q') || '';
        form.querySelector('[name=browse]').value = url.searchParams.get('browse') || form.querySelector('[name=browse]').value;
        const sort = url.searchParams.get('sort') || 'new';
        form.querySelector('[name=sort]').value = ['new', 'recent', 'oldest', 'priority', 'title'].includes(sort) ? sort : 'new';
        form.querySelectorAll('[name=feed]').forEach(input => { input.checked = url.searchParams.getAll('feed').includes(input.value); });
        form.querySelectorAll('[name=new_only], [name=saved_only]').forEach(input => { input.checked = url.searchParams.get(input.name) === '1'; });
        updateFeedLabel(); load(url, 'none');
      });
    });
  });
  window.addEventListener('focus', enhance);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) enhance(); });
  setInterval(enhance, 60000);
})();

// Settings drafts remain in memory; credentials never enter browser storage.
document.addEventListener('DOMContentLoaded', () => {
  const form = document.querySelector('[data-dirty-form]');
  if (!form) return;
  const dialog = document.querySelector('[data-management-discard]');
  const initial = JSON.stringify([...new FormData(form)]);
  let submitting = false, pending = null, origin = null;
  const dirty = () => !submitting && (form.dataset.returnedDraft === 'true' || JSON.stringify([...new FormData(form)]) !== initial);
  const guard = (event, action) => {
    if (!dirty()) return;
    event.preventDefault(); event.stopImmediatePropagation(); pending = action; origin = event.target.closest('a, button') || event.target;
    dialog.showModal(); dialog.querySelector('[data-management-stay]').focus();
  };
  document.addEventListener('click', event => {
    const link = event.target.closest('a[href]');
    if (!link || event.defaultPrevented || link.target === '_blank' || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return;
    guard(event, () => location.assign(link.href));
  });
  window.addEventListener('submit', event => {
    if (event.target === form || dialog.contains(event.target)) return;
    if (submitting) { queueMicrotask(() => { if (event.defaultPrevented) submitting = false; }); return; }
    guard(event, () => event.target.requestSubmit(event.submitter));
  }, true);
  form.addEventListener('submit', event => { queueMicrotask(() => {submitting = !event.defaultPrevented;}); });
  const stay = () => {dialog.close(); pending = null; origin?.focus();};
  dialog.querySelector('[data-management-stay]').addEventListener('click', stay);
  dialog.addEventListener('cancel', () => {pending = null; origin?.focus();});
  dialog.querySelector('[data-management-leave]').addEventListener('click', () => {
    const action = pending; dialog.close(); submitting = true; pending = null; action?.();
    // A cancelled management form resets protection in its submit handler.
  });
  window.addEventListener('beforeunload', event => {if (dirty()) {event.preventDefault(); event.returnValue = '';}});
  document.querySelector('[data-form-error]')?.focus();
});
