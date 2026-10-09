(() => {
  const form = document.querySelector('[data-feed-editor]');
  if (form) {
    const guided = form.dataset.feedEditor === 'create';
    const sections = [...form.querySelectorAll('[data-editor-step]')];
    const errors = form.querySelector('[data-editor-errors]');
    const saveButton = form.querySelector('[data-editor-save]');
    const previewButton = form.querySelector('[data-editor-preview]');
    const status = form.querySelector('[data-editor-preview-status]');
    const saveDialog = document.querySelector('[data-editor-save-dialog]');
    const discardDialog = document.querySelector('[data-editor-discard-dialog]');
    const mode = form.querySelector('[data-schedule-mode]');
    const interval = form.elements.refresh_interval_minutes;
    const cron = form.elements.cron_expression;
    let step = 0, submitting = false, previewing = false, leaveAction = null;
    let previewState = form.dataset.initialPreview === 'ready' ? 'ready' : 'none';
    const extractionNames = ['source_url', 'item_selector', 'title_selector', 'link_selector', 'summary_selector', 'filter_rules', 'exclude_filter_rules', 'fetch_mode', 'max_items'];
    const fingerprint = () => JSON.stringify(extractionNames.map(name => [name, form.elements[name].value]));
    let previewFingerprint = previewState === 'ready' ? fingerprint() : null;
    const draft = () => JSON.stringify([...new FormData(form)]);
    let initialDraft;
    const dirty = () => !submitting && (form.dataset.returnedDraft === 'true' || draft() !== initialDraft);
    const showStep = (index, focus = true) => {
      step = index;
      if (!guided) return;
      sections.forEach((section, i) => { section.hidden = i !== index; });
      form.querySelectorAll('[data-editor-go]').forEach(button => {
        if (Number(button.dataset.editorGo) === index) button.setAttribute('aria-current', 'step');
        else button.removeAttribute('aria-current');
      });
      form.querySelector('[data-editor-back]').hidden = index === 0;
      form.querySelector('[data-editor-next]').hidden = index === 2;
      saveButton.hidden = index !== 2;
      if (focus) sections[index].querySelector('h2').focus();
    };
    const reveal = input => {
      const section = input.closest('[data-editor-step]');
      if (section) showStep(Number(section.dataset.editorStep), false);
      for (let parent = input.parentElement; parent && parent !== form; parent = parent.parentElement) {
        if (parent.tagName === 'DETAILS') parent.open = true;
      }
    };
    const validate = (scope = form) => {
      const invalid = [...scope.querySelectorAll('input, textarea, select')].filter(input => !input.checkValidity());
      form.querySelectorAll('[aria-invalid]').forEach(input => {
        input.removeAttribute('aria-invalid');
        input.removeAttribute('aria-errormessage');
        const descriptions = (input.getAttribute('aria-describedby') || '').split(' ').filter(id => id && id !== errors.id);
        if (descriptions.length) input.setAttribute('aria-describedby', descriptions.join(' '));
        else input.removeAttribute('aria-describedby');
      });
      if (!invalid.length) { errors.hidden = true; return true; }
      errors.replaceChildren();
      const heading = document.createElement('strong');
      heading.textContent = '⚠ Check your feed settings';
      const list = document.createElement('ul');
      invalid.forEach(input => {
        const item = document.createElement('li'), link = document.createElement('a');
        link.href = `#${input.id}`;
        link.textContent = `${input.labels?.[0]?.textContent || input.name}: ${input.validationMessage}`;
        link.addEventListener('click', event => { event.preventDefault(); reveal(input); input.focus(); });
        input.setAttribute('aria-invalid', 'true');
        input.setAttribute('aria-errormessage', errors.id);
        input.setAttribute('aria-describedby', `${input.getAttribute('aria-describedby') || ''} ${errors.id}`.trim());
        item.append(link); list.append(item);
      });
      errors.append(heading, list); errors.hidden = false;
      reveal(invalid[0]); errors.focus();
      return false;
    };
    errors.id = `${guided ? 'create' : 'edit'}-errors`;
    form.noValidate = true;
    let intervalDraft = interval.value === '0' ? '60' : interval.value;
    let cronDraft = cron.value;
    mode.value = cron.value.trim() ? 'calendar' : Number(interval.value) === 0 ? 'manual' : 'interval';
    const syncSchedule = (changed = false) => {
      if (changed) {
        if (mode.value === 'calendar') { cron.value = cronDraft; interval.value = intervalDraft; }
        else { cron.value = ''; interval.value = mode.value === 'manual' ? '0' : intervalDraft; }
      }
      form.querySelector('[data-schedule-interval]').hidden = mode.value !== 'interval';
      form.querySelector('[data-schedule-calendar]').hidden = mode.value !== 'calendar';
      cron.required = mode.value === 'calendar';
      interval.min = mode.value === 'interval' ? '1' : '0';
      form.querySelector('[data-schedule-summary]').textContent = mode.value === 'manual'
        ? 'Manual only. Automatic refresh is off.'
        : mode.value === 'calendar' ? 'The calendar expression will be validated on save and uses the timezone in Settings. Manual refresh does not shift calendar times.'
        : 'Automatic refresh runs while the feed is enabled. Manual refresh restarts the interval.';
    };
    form.querySelector('[data-schedule-choice]').hidden = false;
    syncSchedule();
    mode.addEventListener('change', () => syncSchedule(true));
    interval.addEventListener('input', () => { if (mode.value === 'interval') intervalDraft = interval.value; });
    cron.addEventListener('input', () => { if (mode.value === 'calendar') cronDraft = cron.value; });
    if (guided) {
      form.querySelector('.editor-steps').hidden = false;
      form.querySelector('[data-editor-next]').addEventListener('click', () => {
        if (validate(sections[step])) showStep(Math.min(2, step + 1));
      });
      form.querySelector('[data-editor-back]').addEventListener('click', () => showStep(Math.max(0, step - 1)));
      form.querySelectorAll('[data-editor-go]').forEach(button => button.addEventListener('click', () => {
        const target = Number(button.dataset.editorGo);
        for (let i = 0; i < target; i++) if (!validate(sections[i])) return;
        showStep(target);
      }));
      showStep(previewState === 'ready' || location.search.includes('preview=1') ? 1 : 0, false);
    }
    initialDraft = draft();
    const markStale = () => {
      if (previewFingerprint && fingerprint() !== previewFingerprint) {
        previewState = 'stale';
        status.textContent = 'Settings changed. Preview again before saving.';
      }
    };
    form.addEventListener('input', markStale);
    form.addEventListener('change', markStale);
    const renderPreview = (payload, requestedFingerprint) => {
      const error = form.querySelector('[data-editor-preview-error]');
      const empty = form.querySelector('[data-editor-preview-empty]');
      const list = form.querySelector('[data-editor-preview-list]');
      list.replaceChildren(); error.hidden = !payload.error; error.textContent = payload.error || '';
      const items = payload.items || [];
      empty.hidden = Boolean(payload.error || items.length);
      for (const item of items) {
        const row = document.createElement('article'), title = document.createElement('h3'), link = document.createElement('p');
        row.className = 'preview-card'; title.textContent = item.title; link.className = 'meta'; link.textContent = item.link;
        row.append(title, link); list.append(row);
      }
      previewFingerprint = requestedFingerprint;
      previewState = payload.error ? 'error' : items.length ? 'ready' : 'zero';
      status.textContent = payload.error ? 'Preview failed. Your settings are retained.' : items.length ? `${items.length} matching items. Preview is current.` : 'No matching items. Check your selectors and filters.';
      markStale();
    };
    const runPreview = async () => {
      if (previewing || !validate()) return;
      previewing = true; previewButton.disabled = true; saveButton.disabled = true;
      const requestedFingerprint = fingerprint();
      const loading = form.querySelector('[data-editor-loading]');
      loading.hidden = false; loading.classList.add('active');
      status.textContent = 'Running preview…';
      const values = new FormData(form);
      try {
        if (typeof EventSource === 'undefined') {
          const response = await fetch(previewButton.dataset.previewUrl, {method: 'POST', body: values});
          renderPreview(await response.json(), requestedFingerprint);
        } else {
          await new Promise(resolve => {
            const source = new EventSource(`${previewButton.dataset.previewStreamUrl}?${new URLSearchParams(values)}`);
            let resultReceived = false;
            const finish = () => { clearTimeout(timeout); source.close(); resolve(); };
            const fail = message => { renderPreview({error: message}, requestedFingerprint); finish(); };
            const timeout = setTimeout(() => fail('Preview timed out. Try again or save without preview.'), 90000);
            source.addEventListener('stage', event => {
              try {
                const stage = JSON.parse(event.data);
                form.querySelector('[data-editor-stage]').textContent = stage.title;
                form.querySelector('[data-editor-stage-detail]').textContent = stage.detail;
              } catch (_) { fail('Could not read preview progress. Try again.'); }
            });
            source.addEventListener('result', event => {
              try { renderPreview(JSON.parse(event.data), requestedFingerprint); resultReceived = true; }
              catch (_) { fail('Could not read the preview result. Try again.'); }
            });
            source.addEventListener('error', event => {
              if (event.data) {
                try { renderPreview(JSON.parse(event.data), requestedFingerprint); resultReceived = true; }
                catch (_) { fail('Could not read the preview error. Try again.'); }
              } else fail('Preview disconnected. Try again or save without preview.');
            });
            source.addEventListener('done', () => {
              if (!resultReceived) renderPreview({error: 'Preview ended without a result. Try again.'}, requestedFingerprint);
              finish();
            });
          });
        }
      } catch (_) { renderPreview({error: 'Preview failed. Try again or save without preview.'}, requestedFingerprint); }
      finally { previewing = false; previewButton.disabled = false; saveButton.disabled = false; loading.hidden = true; loading.classList.remove('active'); }
    };
    const submit = () => {
      // Revalidate because controls can change while the confirmation is open.
      if (!validate() || previewing) return;
      submitting = true;
      // Browsers suppress requestSubmit inside the original submit dispatch.
      // Start a new task so the cancelled dispatch has finished first.
      setTimeout(() => {
        if (!validate() || previewing) { submitting = false; return; }
        form.requestSubmit(saveButton);
      }, 0);
    };
    form.addEventListener('submit', event => {
      if (submitting) return;
      event.preventDefault();
      if (guided && step === 0) { if (validate(sections[0])) showStep(1); return; }
      if (event.submitter === previewButton) { runPreview(); return; }
      if (guided && step < 2) { if (validate(sections[step])) showStep(step + 1); return; }
      if (previewing || !validate()) return;
      if (previewState !== 'ready' || previewFingerprint !== fingerprint()) saveDialog.showModal();
      else submit();
    });
    [saveDialog, discardDialog].forEach(dialog => dialog.querySelector('[data-dialog-stay]').addEventListener('click', () => dialog.close()));
    saveDialog.querySelector('[data-dialog-save]').addEventListener('click', () => { saveDialog.close(); submit(); });
    discardDialog.querySelector('[data-dialog-discard]').addEventListener('click', () => {
      discardDialog.close(); submitting = true; leaveAction?.();
    });
    document.addEventListener('click', event => {
      const link = event.target.closest('a[href]');
      if (!link || link.target === '_blank' || link.getAttribute('href').startsWith('#') || event.ctrlKey || event.metaKey || !dirty()) return;
      event.preventDefault(); leaveAction = () => { location.href = link.href; }; discardDialog.showModal();
    });
    // Window capture runs before shared destructive confirmations. If a resumed
    // confirmation is cancelled, keep protecting the unchanged draft.
    window.addEventListener('submit', event => {
      if (event.target === form || event.target.matches('[data-async-refresh]')) return;
      if (submitting) {
        queueMicrotask(() => { if (event.defaultPrevented) submitting = false; });
        return;
      }
      if (!dirty()) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      const otherForm = event.target, submitter = event.submitter;
      leaveAction = () => otherForm.requestSubmit(submitter);
      discardDialog.showModal();
    }, true);
    window.addEventListener('beforeunload', event => { if (dirty()) { event.preventDefault(); event.returnValue = ''; } });
    window.addEventListener('pageshow', () => { submitting = false; });
    if (!errors.hidden) {
      const text = errors.textContent.toLowerCase();
      const index = /source|feed title/.test(text) ? 0 : /selector|filter|browser/.test(text) ? 1 : 2;
      showStep(index, false);
      sections[index].querySelectorAll('details').forEach(detail => { detail.open = true; });
      const field = [...sections[index].querySelectorAll('input, select, textarea')].find(input => {
        const label = input.labels?.[0]?.textContent.toLowerCase().replace(' (optional)', '');
        return label && text.includes(label);
      });
      if (field) {
        field.setAttribute('aria-invalid', 'true');
        field.setAttribute('aria-describedby', `${field.getAttribute('aria-describedby') || ''} ${errors.id}`.trim());
      }
      errors.focus();
    }
  }

  const focusLinkedItem = () => {
    const item = document.querySelector('[data-selected-item]');
    if (!item) return;
    item.focus({preventScroll: true});
    item.scrollIntoView({block: 'center', behavior: 'instant'});
  };
  // Run after browser scroll restoration, including reloads and history returns.
  window.addEventListener('pageshow', () => requestAnimationFrame(focusLinkedItem));

  // Delegation survives in-place refreshes of the RSS view.
  document.addEventListener('click', async event => {
    const button = event.target.closest('[data-copy-feed-url]');
    if (!button) return;
    const input = document.getElementById(button.dataset.copyFeedUrl);
    const status = document.querySelector('[data-copy-status]');
    window.nightfeedStatus(status, '', 'progress');
    try { await navigator.clipboard.writeText(input.value); window.nightfeedStatus(status, 'Feed URL copied.'); }
    catch (_) { input.focus(); input.select(); window.nightfeedStatus(status, 'Copy unavailable. The feed URL is selected; copy it with your device’s copy command.', 'error'); }
  });
  document.querySelector('[data-async-refresh]')?.addEventListener('submit', async event => {
    if (event.defaultPrevented) return;
    event.preventDefault();
    const refreshForm = event.currentTarget, button = refreshForm.querySelector('button');
    const status = document.querySelector('[data-refresh-result]');
    if (button.disabled) return;
    button.disabled = true; button.setAttribute('aria-busy', 'true');
    window.clearNightfeedNotices();
    window.nightfeedStatus(status, 'Fetching feed…', 'progress'); status.classList.remove('editor-notice-error');
    try {
      const response = await fetch(refreshForm.action, {method: 'POST', headers: {'X-Requested-With': 'XMLHttpRequest'}});
      const payload = await response.json();
      if (!response.ok || payload.status === 'error') throw new Error(payload.error || 'Refresh failed. Stored items remain available.');
      const updated = await fetch(location.href);
      if (!updated.ok) throw new Error('Feed refreshed, but the display could not be updated. Reload this page.');
      const doc = new DOMParser().parseFromString(await updated.text(), 'text/html');
      document.querySelector('[data-feed-status]').replaceChildren(...doc.querySelector('[data-feed-status]').childNodes);
      if (!document.querySelector('[data-feed-editor]')) document.querySelector('[data-feed-content]').replaceChildren(...doc.querySelector('[data-feed-content]').childNodes);
      focusLinkedItem();
      window.nightfeedUnread(payload.unread_notifications);
      window.nightfeedStatus(status, payload.message);
    } catch (error) { status.classList.add('editor-notice-error'); window.nightfeedStatus(status, error.message, 'error'); }
    finally { button.disabled = false; button.removeAttribute('aria-busy'); }
  });
})();
