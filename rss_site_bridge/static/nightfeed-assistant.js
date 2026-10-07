(() => {
  const panel = document.querySelector('[data-assistant-panel]');
  if (!panel) return;
  const select = panel.querySelector('[data-assistant-conversations]');
  const messages = panel.querySelector('[data-assistant-messages]');
  const status = panel.querySelector('[data-assistant-status]');
  const form = panel.querySelector('[data-assistant-form]');
  const input = form.elements.message;
  const mic = panel.querySelector('[data-assistant-mic]');
  const speak = panel.querySelector('[data-assistant-speak]');
  const send = panel.querySelector('[data-assistant-send]');
  const stop = panel.querySelector('[data-assistant-stop]');
  let turnController, stopRequested = false;
  const upload = panel.querySelector('[data-assistant-upload]');
  const attachments = panel.querySelector('[data-assistant-attachments]');
  let images = [], readingImages = false;
  const launcher = document.querySelector('[data-assistant-toggle]');
  let conversation = '', busy = false, recorder, media, recordTimer, streamingMessage, pendingNavigation = '', turnError = false, activeStream = false, reloadTimer;
  let recordingCancelled = false, startingRecording = false;
  const stored = key => { try { return sessionStorage.getItem('nightfeed-assistant:' + key); } catch (_) { return null; } };
  const remember = (key, value) => { try { sessionStorage.setItem('nightfeed-assistant:' + key, value); } catch (_) {} };
  const node = (tag, text, cls) => { const el = document.createElement(tag); if (text) el.textContent = text; if (cls) el.className = cls; return el; };
  const bottom = () => { messages.scrollTop = messages.scrollHeight; };
  const deviceContext = () => { let token=''; try { token=localStorage.getItem('nightfeed.push.device') || ''; } catch (_) {} return {device_token:token, appearance:document.documentElement.dataset.theme || 'system'}; };
  const setBusy = value => {
    busy = value; panel.dataset.busy = String(value); input.readOnly = value;
    stop.hidden = !value || !conversation;
    send.hidden = !stop.hidden;
    send.disabled = value || readingImages || (!input.value.trim() && !images.length);
    upload.disabled = value || readingImages;
    panel.querySelectorAll('.assistant-remove-image').forEach(button => { button.disabled = value || readingImages; });
    mic.disabled = value || !navigator.mediaDevices?.getUserMedia || !window.MediaRecorder;
    panel.querySelector('[data-assistant-new]').disabled = value;
    panel.querySelector('[data-assistant-delete]').disabled = value || !conversation;
    select.disabled = value;
    panel.querySelectorAll('[data-apply-draft]').forEach(button => { if (!button.dataset.applied) button.disabled = value; });
    messages.querySelectorAll('.task-setup button').forEach(button => { button.disabled=value; });
  };
  const api = async (path, options = {}) => {
    const response = await fetch(path, options);
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || 'Request failed.');
    return value;
  };
  const link = (label, href) => {
    const a = node('a', label);
    let url;
    try { url = new URL(href, location.origin); } catch (_) { return node('span', label); }
    if (!['http:', 'https:'].includes(url.protocol)) return node('span', label);
    a.href = url.href;
    if (url.origin !== location.origin) { a.target = '_blank'; a.rel = 'noreferrer noopener'; }
    return a;
  };
  const imagePreview = (target, items) => {
    for (const item of items) {
      const img = document.createElement('img'); img.src = item.data_url; img.alt = item.name; img.className = 'assistant-image'; target.append(img);
    }
  };
  const renderAttachments = () => {
    attachments.replaceChildren(); attachments.hidden = !images.length;
    images.forEach((item, index) => {
      const tile = node('div', '', 'assistant-attachment'); imagePreview(tile, [item]);
      const remove = node('button', '×', 'assistant-remove-image'); remove.type = 'button'; remove.setAttribute('aria-label', 'Remove ' + item.name);
      remove.disabled = busy; remove.addEventListener('click', () => { images.splice(index, 1); renderAttachments(); resize(); });
      tile.append(remove); attachments.append(tile);
    });
  };
  const addImages = async files => {
    if (busy || readingImages) return;
    readingImages = true; setBusy(busy);
    try {
      if (images.length + files.length > 4) throw new Error('Attach up to four images.');
      const added = [];
      for (const file of files) {
        if (!['image/png','image/jpeg','image/webp'].includes(file.type) || file.size > 2 * 1024 * 1024) throw new Error('Use PNG, JPEG or WebP images up to 2 MB each.');
        const data_url = await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = () => reject(new Error('Could not read this image.')); reader.readAsDataURL(file); });
        await new Promise((resolve, reject) => { const img = new Image(); img.onload = resolve; img.onerror = () => reject(new Error('Could not open this image.')); img.src = data_url; });
        added.push({name:file.name || 'Pasted image', data_url});
      }
      images.push(...added); renderAttachments(); status.textContent = '';
    } catch (error) { status.textContent = error.message; }
    finally { readingImages = false; upload.value = ''; setBusy(busy); resize(); input.focus(); }
  };
  panel.querySelector('[data-assistant-add-image]').addEventListener('click', () => { panel.querySelector('.assistant-shortcuts').open = false; upload.click(); });
  upload.addEventListener('change', () => addImages(Array.from(upload.files)));
  input.addEventListener('paste', event => { const files = Array.from(event.clipboardData?.files || []); if (files.length) { event.preventDefault(); addImages(files); } });
  form.addEventListener('dragover', event => { if (Array.from(event.dataTransfer.types).includes('Files')) event.preventDefault(); });
  form.addEventListener('drop', event => { if (event.dataTransfer.files.length) { event.preventDefault(); addImages(Array.from(event.dataTransfer.files)); } });
  // Build DOM nodes rather than accepting HTML from model responses.
  const inlineMarkdown = (target, text, depth = 0) => {
    if (depth > 5) { target.append(document.createTextNode(text)); return; }
    const tokens = /(\\[\\`*_{}\[\]()#+.!>\-]|`[^`\n]+`|\*\*[^*\n]+\*\*|__[^_\n]+__|\*[^*\n]+\*|\[(?:\\.|[^\]\\\n])+\]\((?:\\.|[^\s()\\]|\([^\s()]*\))+\))/g;
    const unescape = value => value.replace(/\\([\\`*_{}\[\]()#+.!>\-])/g, '$1');
    let cursor = 0;
    for (const match of text.matchAll(tokens)) {
      target.append(document.createTextNode(text.slice(cursor, match.index)));
      const token = match[0];
      if (token[0] === '\\') {
        target.append(document.createTextNode(unescape(token)));
      } else if (token[0] === '[') {
        const parts = token.match(/^\[((?:\\.|[^\]\\\n])+)\]\(((?:\\.|[^\s()\\]|\([^\s()]*\))+)\)$/);
        target.append(link(unescape(parts[1]), unescape(parts[2])));
      } else {
        const code = token[0] === '`', bold = token.startsWith('**') || token.startsWith('__');
        const element = document.createElement(code ? 'code' : bold ? 'strong' : 'em');
        const value = token.slice(bold ? 2 : 1, bold ? -2 : -1);
        if (code) element.textContent = value;
        else inlineMarkdown(element, value, depth + 1);
        target.append(element);
      }
      cursor = match.index + token.length;
    }
    target.append(document.createTextNode(text.slice(cursor)));
  };
  const renderReply = (target, text) => {
    target.replaceChildren(); target.classList.add('assistant-markdown');
    const lines = text.replace(/\r\n?/g, '\n').split('\n');
    let paragraph, list, quote, code;
    for (const line of lines) {
      if (code) {
        if (/^\s*```/.test(line)) code = null;
        else code.textContent += (code.textContent ? '\n' : '') + line;
        continue;
      }
      if (/^\s*```/.test(line)) {
        const pre = node('pre'); code = node('code'); pre.append(code); target.append(pre);
        paragraph = list = quote = null; continue;
      }
      if (!line.trim()) { paragraph = list = quote = null; continue; }
      const heading = line.match(/^#{1,6}\s+(.+)$/);
      const item = line.match(/^\s*(?:([-*+])|\d+[.)])\s+(.+)$/);
      if (heading) {
        const h = node('h4'); inlineMarkdown(h, heading[1]); target.append(h); paragraph = list = quote = null;
      } else if (item) {
        const tag = item[1] ? 'ul' : 'ol';
        if (!list || list.localName !== tag) { list = node(tag); target.append(list); }
        const li = node('li'); inlineMarkdown(li, item[2]); list.append(li); paragraph = quote = null;
      } else if (/^>\s?/.test(line)) {
        if (!quote) { quote = node('blockquote'); target.append(quote); }
        else quote.append(node('br'));
        inlineMarkdown(quote, line.replace(/^>\s?/, '')); paragraph = list = null;
      } else {
        if (!paragraph) { paragraph = node('p'); target.append(paragraph); }
        else paragraph.append(node('br'));
        inlineMarkdown(paragraph, line); list = quote = null;
      }
    }
  };
  const message = (role, text, items = []) => {
    const article = node('article', '', 'assistant-message'); article.dataset.role = role;
    article.append(node('strong', role === 'user' ? 'You' : 'Nightfeed'));
    const content = node('div');
    if (role === 'assistant') renderReply(content, text); else content.textContent = text;
    imagePreview(article, items); article.append(content); messages.append(article); bottom();
    return content;
  };
  const choices = (content, values, mode='single') => {
    if (!values?.length) return;
    const row=node('div','','assistant-choices');
    const selected=new Set(['Nightfeed']);
    for (const value of values) {
      const button=node('button',value,'btn btn-secondary');button.type='button';
      if (mode==='multi') {
        button.setAttribute('aria-pressed',String(selected.has(value)));button.disabled=value==='Nightfeed';
        button.addEventListener('click',()=>{ if (busy) return; if (selected.has(value)) selected.delete(value);else selected.add(value);button.setAttribute('aria-pressed',String(selected.has(value))); });
      } else button.addEventListener('click',()=>{ if (busy) return;input.value=value;form.requestSubmit(); });
      row.append(button);
    }
    if (mode==='multi') {
      const next=node('button','Continue','btn btn-primary');next.type='button';
      next.addEventListener('click',()=>{ if (busy) return;input.value=selected.has('Push')?(selected.has('Email')?'Push and email':'Push'):selected.has('Email')?'Email':'Nightfeed only';form.requestSubmit(); });row.append(next);
    }
    content.closest('.assistant-message').append(row);
  };
  const previews = (target, entries) => {
    for (const item of entries || []) {
      const row = node('div', '', 'assistant-preview');
      row.append(node('p', item.title));
      if (item.link) row.append(link(item.link, item.link));
      if (item.summary) row.append(node('p', item.summary));
      target.append(row);
    }
  };
  const card = ({kind, data}, replay = false) => {
    if (kind === 'task_setup') {
      const content=message('assistant',`Would you like to continue setting up your watch for “${data.topic}”?`);
      content.closest('.assistant-message').dataset.setupId=data.setup_id;
      if (!replay) choices(content,['Set up this watch']);
      bottom();return;
    }
    if (kind === 'tasks') {
      const box=node('section','','assistant-card');box.append(node('h3','Tasks'));const list=node('div');box.append(list);
      const openTask=task=>location.assign('/tasks?task='+task.id),refreshTasks=async()=>{const result=await api('/api/tasks');window.nightfeedTasks.renderList(list,result.tasks,openTask,refreshTasks);};
      window.nightfeedTasks.renderList(list,data.tasks,openTask,refreshTasks);
      if (data.has_more) box.append(node('small',`Showing ${data.returned_count} of ${data.total_count} tasks. Ask “show more results” for the next page.`));
      messages.append(box);bottom();return;
    }
    if (kind === 'navigation') {
      if (!replay) pendingNavigation = data.navigate;
      const box = node('section', '', 'assistant-card'); box.append(link('Open saved topic safely', data.navigate)); messages.append(box); return;
    }
    const existing = kind === 'result' ? Array.from(messages.querySelectorAll('[data-draft-id],[data-setup-id]')).find(el => data.draft_id && el.dataset.draftId === data.draft_id || data.setup_id && el.dataset.setupId === data.setup_id) : null;
    const box = existing || node('section', '', 'assistant-card');
    if (existing) { box.replaceChildren(); box.classList.remove('assistant-proposal'); }
    if (kind === 'preview') {
      box.append(node('h3', `${data.matched_count} source matches`)); previews(box, data.items);
    } else if (kind === 'search') {
      box.append(node('h3', data.total_count === undefined ? 'Stored content' : `${data.total_count} matching items`));
      if (data.added_on) box.append(node('small', `Added ${data.added_on} · ${data.timezone}`));
      else if (data.added_from || data.added_until) box.append(node('small', `Added ${data.added_from || 'earlier'} to ${data.added_until || 'now'} · ${data.timezone}`));
      if (data.truncated) box.append(node('p', `Showing ${data.returned_count} of ${data.total_count} matching items.`));
      if (!data.items.length) box.append(node('p', 'No matching items.'));
      for (const item of data.items) {
        const row = node('p'); row.append(link(item.title, item.url), node('small', ` · ${item.feed_title}`)); box.append(row);
      }
      if (data.has_more) box.append(node('small', 'Ask “show more items” for the next page.'));
    } else if (kind === 'feeds') {
      box.append(node('h3', `${data.total_count ?? data.feeds.length} feeds`));
      for (const feed of data.feeds) {
        const row=node('p');row.append(link(feed.feed_title || feed.config.feed_title,feed.url),node('small',` · ${feed.active ? feed.last_status : 'paused'} · ${feed.stored_item_count} items`));box.append(row);
      }
      if (data.has_more) box.append(node('small','Ask “show more results” for the next page.'));
    } else if (kind === 'notifications') {
      box.append(node('h3', `${data.unread_count} unread notifications`));
      if (data.truncated) box.append(node('p', `Showing ${data.returned_count} of ${data.total_count} ${data.status} notifications.`));
      for (const item of data.items) { const row=node('p'); row.append(link(item.title, item.url), node('small', item.read ? ' · Read' : ' · Unread')); box.append(row); }
      if (data.has_more) box.append(node('small','Ask “show more results” for the next page.'));
    } else if (kind === 'result') {
      if (data.draft_id) panel.querySelectorAll('[data-apply-draft]').forEach(button => { if (button.dataset.applyDraft === data.draft_id) { button.dataset.applied='1'; button.disabled=true; button.textContent='Applied'; } });
      if (!replay && ['system','light','dark'].includes(data.browser_action?.appearance)) document.dispatchEvent(new CustomEvent('nightfeed:appearance',{detail:data.browser_action.appearance}));
      if (!replay && data.browser_action?.unread_notifications !== undefined) {
        const action=data.browser_action; window.nightfeedUnread?.(action.unread_notifications);
        document.querySelectorAll('[data-notification-count]').forEach(el => { el.textContent = `${action.unread_notifications} unread notifications.`; });
        for (const id of action.notification_ids || []) {
          const row=document.querySelector(`[data-notification-id="${id}"]`); if (!row) continue;
          if (action.notification_action?.startsWith('delete') || new URL(location.href).searchParams.get('status') === 'unread') row.remove();
          else { row.classList.remove('notification-unread'); row.querySelector('.notification-dot')?.remove(); row.querySelector(`form[action="/notifications/${id}/read"]`)?.remove(); }
        }
      }
      if (!replay && data.browser_action?.topic_action) {
        const action=data.browser_action;
        for (const id of action.topic_ids || []) document.querySelectorAll(`[data-topic-id="${id}"]`).forEach(row => {
          if (['save','unsave'].includes(action.topic_action)) row.querySelectorAll('[data-topic-save]').forEach(button => {
            const saved=action.topic_action==='save'; button.setAttribute('aria-pressed', String(saved)); button.setAttribute('aria-label', saved ? 'Remove from saved' : 'Save for later');
            const label=button.querySelector('span'); if (label) label.textContent=saved ? 'Saved' : 'Save for later';
          });
          else { row.querySelector('.topic-new')?.remove(); row.querySelector('.topic-updated')?.remove(); }
        });
      }
      box.append(node('h3', 'Nightfeed'), node('p', data.message));
      if (data.url) {
        const action = link('Open in Nightfeed ↗', data.url); action.className = 'assistant-action-link';
        box.append(action);
      }
    } else if (kind === 'help') {
      box.append(node('h3', 'Nightfeed help'));
      for (const article of data.articles) { box.append(link(article.title, article.url), node('p', article.text)); }
    } else if (kind === 'draft') {
      box.classList.add('assistant-proposal'); box.dataset.draftId = data.draft_id;
      box.append(node('h3', 'Nightfeed'));
      const payload = data.payload;
      if (data.kind === 'task') {
        const cfg=payload.config;box.append(node('p',cfg.name),node('p',`Match: ${cfg.terms.join(', ')} · ${cfg.feed_ids?.length?'Selected feeds':'All feeds'} · ${cfg.mode==='once'?'Once':'Every new match'}`),node('p',`Delivery: ${cfg.channels.join(' + ')} · ${cfg.expires_at?'Expires '+window.nightfeedTasks.date(Number(cfg.expires_at)): 'No expiry'}`));
        if (cfg.required_terms?.length) box.append(node('p','Include: '+cfg.required_terms.join(', ')));
        if (cfg.exclude_terms?.length) box.append(node('p','Exclude: '+cfg.exclude_terms.join(', ')));
        if (payload.preview) {
          const preview=node('details');preview.append(node('summary',`${payload.preview.total_count} existing matches · Preview only`));
          for (const item of payload.preview.items) preview.append(node('p',item.title));
          preview.append(node('small','I’ll notify you only about future arrivals.'));box.append(preview);
        }
      } else if (data.kind === 'task_state') {
        box.append(node('p',`${payload.action[0].toUpperCase()+payload.action.slice(1)} “${payload.name}”?`));
      } else if (data.kind === 'feed_maintenance') {
        box.append(node('p',`${payload.action[0].toUpperCase()+payload.action.slice(1)} “${payload.feed_title}”?`),node('p',payload.impact));
      } else if (data.kind === 'feed') {
        const config = payload.config;
        box.append(node('p', config.feed_title));
        box.append(node('p', config.cron_expression ? `Schedule: ${config.cron_expression} (${config.schedule_timezone})` : config.refresh_interval_minutes ? `Every ${config.refresh_interval_minutes} minutes` : 'Manual refresh only'));
        box.append(node('p', `Include: ${config.filter_rules || 'all titles'} · Exclude: ${config.exclude_filter_rules || 'none'}`));
        box.append(node('p', payload.email_available ? `Email: successful refresh ${config.notify_on_success ? 'on' : 'off'}; failures ${config.notify_on_failure ? config.notify_failure_categories.join(', ') : 'off'}` : 'Email unavailable until SMTP is configured. Failure preferences are saved for later.'));
        const preview = node('details'); preview.append(node('summary', 'Preview items')); previews(preview, payload.preview.items); box.append(preview);
        const detail = node('details'); detail.append(node('summary', 'Configuration changes'));
        const dl = node('dl');
        for (const [key, value] of Object.entries(config)) {
          if (payload.before && JSON.stringify(payload.before[key]) === JSON.stringify(value)) continue;
          dl.append(node('dt', key.replaceAll('_', ' ')), node('dd', payload.before ? `${JSON.stringify(payload.before[key])} → ${JSON.stringify(value)}` : String(value)));
        }
        detail.append(dl); box.append(detail);
      } else if (data.kind === 'notifications' || data.kind === 'topics') {
        box.append(node('p', payload.impact));
        if ((payload.items || []).length && payload.count <= 3) for (const item of payload.items) box.append(node('p', item.title));
      } else if (data.kind === 'timezone') {
        box.append(node('p', `${payload.before} → ${payload.timezone_name}`), node('p', payload.impact));
      } else if (data.kind === 'settings') {
        box.append(node('p', payload.impact));
        for (const [key, value] of Object.entries(payload.settings)) {
          if (payload.before[key] !== value) box.append(node('p', `${key.replaceAll('_', ' ')}: ${String(payload.before[key])} → ${String(value)}`));
        }
      } else if (data.kind === 'appearance') {
        box.append(node('p', `${payload.before} → ${payload.appearance}`), node('p',payload.impact));
      } else if (data.kind === 'push') {
        box.append(node('p', payload.impact));
        for (const [key,value] of Object.entries(payload.preferences)) if (JSON.stringify(payload.before[key])!==JSON.stringify(value)) box.append(node('p',`${key.replaceAll('_',' ')}: ${JSON.stringify(payload.before[key])} → ${JSON.stringify(value)}`));
      } else if (data.kind === 'active') {
        box.append(node('p', `${payload.active ? 'Resume' : 'Pause'} feed #${payload.feed_id}`));
      } else box.append(node('p', `Refresh feed #${payload.feed_id} now`));
      const button = node('button', '✓ Approve', 'assistant-decision assistant-approve'); button.type = 'button'; button.dataset.applyDraft = data.draft_id;
      const deny = node('button', '✕ Deny', 'assistant-decision assistant-deny'); deny.type = 'button'; deny.dataset.applyDraft = data.draft_id;
      button.disabled = busy;
      button.addEventListener('click', async () => {
        stopRequested = false; setBusy(true); status.textContent = 'Applying…';
        try {
          const result = await api(`/api/assistant/conversations/${conversation}/apply/${data.draft_id}`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(deviceContext())});
          button.dataset.applied = '1'; button.disabled = true; button.textContent = 'Applied';
          if (stopRequested) return;
          card({kind:'result', data:result}); status.textContent = '';
        } catch (error) { status.textContent = error.message; }
        finally { setBusy(false); }
      });
      deny.addEventListener('click', async () => {
        setBusy(true); status.textContent = 'Declining…';
        try {
          const result = await api(`/api/assistant/conversations/${conversation}/deny/${data.draft_id}`, {method:'POST'});
          card({kind:'result', data:result}); status.textContent = '';
        } catch (error) { status.textContent = error.message; }
        finally { setBusy(false); }
      });
      const actions = node('div', '', 'assistant-decisions'); actions.append(button, deny); box.append(actions);
    }
    if (!existing) messages.append(box); bottom();
  };
  const refreshList = async () => {
    const result = await api('/api/assistant/conversations');
    select.replaceChildren(new Option('New chat', ''));
    for (const item of result.conversations) select.add(new Option(item.title, item.id));
    select.value = conversation;
  };
  const load = async id => {
    if (id !== conversation) stopVoice();
    clearTimeout(reloadTimer);
    conversation = id; select.value = id; remember('conversation', id); messages.replaceChildren();
    showUsage(null); status.textContent = '';
    if (!id) { setBusy(false); message('assistant', 'I help with Nightfeed feeds, saved content, notifications, tasks, and settings. Paste a listing URL or tell me what you want to do.'); return; }
    const result = await api(`/api/assistant/conversations/${id}`);
    if (conversation !== id) return;
    showUsage(result.usage && Object.keys(result.usage).length ? result.usage : null);
    for (const item of result.messages) { if (item.content || item.images?.length) choices(message(item.role, item.content, item.images || []),item.choices,item.choice_mode); (item.cards || []).forEach(value => card(value, true)); }
    panel.querySelectorAll('[data-apply-draft]').forEach(button => { if ((result.applied_drafts || []).includes(button.dataset.applyDraft)) { button.dataset.applied='1'; button.disabled=true; button.textContent='Applied'; } });
    setBusy(result.busy);
    for (const box of messages.querySelectorAll('[data-draft-id]')) {
      if ((result.inactive_drafts || []).includes(box.dataset.draftId) && box.classList.contains('assistant-proposal')) {
        box.querySelector('.assistant-decisions')?.replaceChildren(node('small', 'This proposal is no longer active.'));
      }
    }
    status.textContent = result.busy ? 'Working… You can stop this response.' : '';
    if (result.busy) reloadTimer = setTimeout(() => { if (!panel.hidden && conversation === id && !activeStream) load(id).catch(error => { status.textContent = error.message; }); }, 3000);
    bottom();
  };
  const open = async value => {
    panel.hidden = !value; launcher.setAttribute('aria-expanded', String(value)); document.body.classList.toggle('assistant-open', value); remember('open', value ? '1' : '0');
    if (!value) { stopVoice(); recordingCancelled = true; if (recorder?.state === 'recording') recorder.stop(); window.speechSynthesis?.cancel(); launcher.focus(); return; }
    resize();
    try {
      await refreshList();
      if (!activeStream) {
        const saved = conversation || stored('conversation') || '';
        await load(Array.from(select.options).some(option => option.value === saved) ? saved : '');
      }
      input.focus();
    }
    catch (error) { await load(''); status.textContent = error.message; }
  };
  document.addEventListener('click', event => { panel.querySelectorAll('details[open]').forEach(menu => { if (!menu.contains(event.target)) menu.open = false; }); });
  launcher.addEventListener('click', () => open(panel.hidden));
  const switchView = value => {
    panel.querySelector('#assistant-chat-view').hidden=value!=='chat';
    panel.querySelector('#assistant-tasks-view').hidden=value!=='tasks';
    panel.querySelectorAll('[data-assistant-view]').forEach(button=>{const selected=button.dataset.assistantView===value;button.setAttribute('aria-selected',String(selected));button.tabIndex=selected?0:-1;});
    if(value==='tasks')window.nightfeedTasks.mount(panel.querySelector('#assistant-tasks-view'));
  };
  panel.querySelectorAll('[data-assistant-view]').forEach(button=>{button.addEventListener('click',()=>switchView(button.dataset.assistantView));button.addEventListener('keydown',event=>{if(['ArrowLeft','ArrowRight'].includes(event.key)){event.preventDefault();const value=button.dataset.assistantView==='chat'?'tasks':'chat';switchView(value);panel.querySelector(`[data-assistant-view=${value}]`).focus();}});});
  panel.querySelector('[data-assistant-close]').addEventListener('click', () => open(false));
  panel.addEventListener('keydown', event => { if (event.key === 'Escape') open(false); });
  select.addEventListener('change', () => load(select.value).catch(error => { status.textContent = error.message; }));
  panel.querySelector('[data-assistant-new]').addEventListener('click', () => { load(''); select.value = ''; input.focus(); });
  panel.querySelector('[data-assistant-delete]').addEventListener('click', async () => {
    panel.querySelector('.assistant-chat-menu').open = false;
    if (!conversation || !window.confirm('Delete this conversation and its pending proposals?')) return;
    try { await api(`/api/assistant/conversations/${conversation}`, {method:'DELETE'}); await load(''); await refreshList(); }
    catch (error) { status.textContent = error.message; }
  });
  stop.addEventListener('click', async () => {
    stop.disabled = true; stopRequested = true; stopVoice(); clearTimeout(reloadTimer);
    status.textContent = 'Stopping…';
    try {
      await api(`/api/assistant/conversations/${conversation}/stop`, {method:'POST'});
      turnController?.abort(); activeStream = false;
      await load(conversation); status.textContent = '';
    } catch (error) { stopRequested = false; status.textContent = error.message; }
    finally { stop.disabled = false; }
  });
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (busy || readingImages || (!input.value.trim() && !images.length)) return;
    const draftText = input.value, text = draftText.trim(), sentImages = images.slice();
    const controller = new AbortController(); turnController = controller; stopRequested = false;
    let accepted = false, rejected = false, completed = false;
    setBusy(true); activeStream = true; status.textContent = 'Thinking…';
    input.value = ''; images = []; renderAttachments(); resize();
    try {
      if (!conversation) { const result = await api('/api/assistant/conversations', {method:'POST'}); conversation = result.id; remember('conversation', conversation); setBusy(true); }
      const response = await fetch(`/api/assistant/conversations/${conversation}/messages`, {method:'POST', signal:controller.signal, headers:{'Content-Type':'application/json'}, body:JSON.stringify({message:text, images:sentImages, path:location.pathname, ...deviceContext()})});
      if (!response.ok) {
        rejected = true;
        const error = await response.json().catch(() => ({}));
        throw new Error(error.error || (response.status === 413 ? 'Request is too large.' : `Request rejected (HTTP ${response.status}).`));
      }
      accepted = true;
      messages.querySelectorAll('.assistant-choices').forEach(row=>row.remove());
      message('user', text, sentImages); streamingMessage = null; pendingNavigation = ''; turnError = false;
      status.textContent = 'Thinking…';
      const reader = response.body.getReader(), decoder = new TextDecoder(); let buffer = '';
      const processEvent = block => {
        const type = block.split('\n').find(line => line.startsWith('event: '))?.slice(7);
        const raw = block.split('\n').find(line => line.startsWith('data: '))?.slice(6);
        if (!raw) return; const value = JSON.parse(raw);
        if (type === 'progress') status.textContent = value.title + (value.detail ? ': ' + value.detail : '');
        if (type === 'card') card(value);
        if (type === 'usage') showUsage(value);
        if (type === 'reply_start') streamingMessage = null;
        if (type === 'delta') { streamingMessage ||= message('assistant', ''); streamingMessage.textContent += value.text; bottom(); }
        if (type === 'message') {
          if (value.card_only) { streamingMessage?.closest('.assistant-message')?.remove(); streamingMessage = null; }
          else { streamingMessage ||= message('assistant', ''); renderReply(streamingMessage, value.content); choices(streamingMessage,value.choices,value.choice_mode); }
          bottom();
          if (speak.checked && window.speechSynthesis) {
            speechSynthesis.cancel(); const utterance = new SpeechSynthesisUtterance(value.content);
            speechSynthesis.speak(utterance);
          }
        }
        if (type === 'error') { turnError = true; status.textContent = value.error; message('assistant', value.error); }
        if (type === 'done') { completed = true; setBusy(false); if (pendingNavigation) { remember('open', '1'); location.assign(pendingNavigation); } }
      };
      while (true) {
        const {value, done} = await reader.read(); buffer += decoder.decode(value || new Uint8Array(), {stream:!done});
        let end; while ((end = buffer.indexOf('\n\n')) !== -1) { processEvent(buffer.slice(0, end)); buffer = buffer.slice(end + 2); }
        if (done) break;
      }
      if (!completed) throw new Error('The reply stream ended before completion.');
      if (!turnError) status.textContent = '';
      await refreshList();
    } catch (error) {
      if (stopRequested || controller !== turnController) return;
      if (!accepted) { input.value = draftText; images = sentImages; renderAttachments(); resize(); }
      status.textContent = accepted
        ? 'Your message was received, but the reply could not finish loading. Reopen this conversation to check its status.'
        : rejected ? `Not sent. ${error.message}`
        : `Could not confirm delivery. Your draft is preserved. Reopen this conversation before retrying. ${error.message}`;
    }
    finally { if (controller === turnController) { activeStream = false; setBusy(false); resize(); input.focus(); } }
  });
  input.addEventListener('keydown', event => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); form.requestSubmit(); } });
  const resize = () => { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 180) + 'px'; send.disabled = busy || readingImages || (!input.value.trim() && !images.length); };
  input.addEventListener('input', resize);
  window.addEventListener('resize', () => { if (!panel.hidden) resize(); });
  panel.querySelectorAll('[data-prompt]').forEach(button => button.addEventListener('click', () => { input.value = button.dataset.prompt; button.closest('details').open = false; resize(); input.focus(); }));
  const showUsage = value => {
    const output = panel.querySelector('[data-context-usage]');
    if (!value) {
      output.textContent = 'Usage appears after this conversation\'s next reply.';
      panel.querySelector('[data-context-ring]').setAttribute('stroke-dasharray', '0 63');
      return;
    }
    if (!value.available) { output.textContent = 'This provider did not report token usage. See audit history for request sizes and timing.'; panel.querySelector('[data-context-ring]').setAttribute('stroke-dasharray','0 63'); return; }
    let text = `${value.input_tokens.toLocaleString()} input · ${value.output_tokens.toLocaleString()} output tokens`;
    if (value.context_window) text += ` · ${Math.round(value.input_tokens / value.context_window * 100)}% of ${value.context_window.toLocaleString()} context window`;
    if (value.cached_tokens) text += ` · ${value.cached_tokens.toLocaleString()} cached`;
    text += value.estimated_usd === null ? ' · Cost unavailable' : ` · Estimated $${value.estimated_usd.toFixed(6)}`;
    output.textContent = text + ' (latest provider request)';
    panel.querySelector('[data-context-ring]').setAttribute('stroke-dasharray', `${value.context_window ? Math.min(63,63*value.input_tokens/value.context_window) : 0} 63`);
  };
  const stopVoice = () => {
    recordingCancelled = true; clearTimeout(recordTimer);
    if (recorder?.state === 'recording') recorder.stop();
    media?.getTracks().forEach(track => track.stop());
    window.speechSynthesis?.cancel();
  };
  const startRecording = async () => {
    if (busy || startingRecording || recorder?.state === 'recording') return;
    stopRequested = false;
    if (panel.dataset.voiceConfigured !== 'true') { status.textContent = 'Configure a transcription endpoint in AI settings to use voice input.'; stopVoice(); return; }
    try {
      startingRecording = true;
      recordingCancelled = false; window.speechSynthesis?.cancel(); media = await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true, noiseSuppression:true, autoGainControl:true}});
      if (panel.hidden || recordingCancelled) { media.getTracks().forEach(track => track.stop()); return; }
      const recording = new MediaRecorder(media), tracks = media.getTracks(), chunks = []; recorder = recording;
      recording.addEventListener('dataavailable', event => { if (event.data.size) chunks.push(event.data); });
      recording.addEventListener('stop', async () => {
        clearTimeout(recordTimer); tracks.forEach(track => track.stop());
        mic.setAttribute('aria-pressed', 'false'); mic.setAttribute('aria-label','Dictate message');
        if (panel.hidden || recordingCancelled) return;
        setBusy(true); status.textContent = 'Transcribing…';
        const mime = recording.mimeType, data = new FormData(); data.append('audio', new Blob(chunks, {type:mime}), mime.includes('mp4') ? 'recording.m4a' : 'recording.webm');
        try {
          if (!conversation) { const result=await api('/api/assistant/conversations',{method:'POST'});conversation=result.id;remember('conversation',conversation); }
          data.append('conversation',conversation);
          const result = await api('/api/assistant/transcribe', {method:'POST', body:data}); if (stopRequested) return; input.value = result.text; resize(); setBusy(false); form.requestSubmit();
        }
        catch (error) { stopVoice(); status.textContent = error.message; setBusy(false); }
      });
      recording.start(); mic.setAttribute('aria-pressed', 'true'); mic.setAttribute('aria-label','Finish dictation'); status.textContent = 'Dictating… tap the microphone when finished.';
      recordTimer = setTimeout(() => { if (recording.state === 'recording') recording.stop(); }, 60000);
    } catch (_) { stopVoice(); status.textContent = 'Microphone unavailable. Check browser permission and use HTTPS.'; }
    finally { startingRecording = false; }
  };
  mic.addEventListener('click', async () => {
    if (recorder?.state === 'recording') { recorder.stop(); return; }
    startRecording();
  });
  window.addEventListener('pagehide', () => { stopVoice(); clearTimeout(recordTimer); });
  // Quick, irregular blinks keep the companion alive without constant motion.
  const companions = Array.from(document.querySelectorAll('[data-companion]'));
  const motion = window.matchMedia('(prefers-reduced-motion: reduce)');
  let blinkTimer, blinkReady = false, firstBlink = true;
  const eyesOpen = () => companions.forEach(companion => { companion.dataset.blink = 'false'; });
  const scheduleBlink = () => {
    clearTimeout(blinkTimer); eyesOpen();
    if (!blinkReady || document.hidden) return;
    blinkTimer = setTimeout(() => blink(!motion.matches && Math.random() < .16), firstBlink ? 1500 + Math.random() * 1000 : 3500 + Math.random() * 4000);
  };
  const blink = double => {
    if (document.hidden) { scheduleBlink(); return; }
    firstBlink = false;
    companions.forEach(companion => { companion.dataset.blink = 'true'; });
    blinkTimer = setTimeout(() => {
      eyesOpen();
      if (double) blinkTimer = setTimeout(() => blink(false), 160 + Math.random() * 100);
      else scheduleBlink();
    }, 180 + Math.random() * 50);
  };
  Promise.all(companions.flatMap(companion => Array.from(companion.querySelectorAll('img'), image => image.decode())))
    .then(() => { blinkReady = true; scheduleBlink(); })
    .catch(() => { eyesOpen(); });
  document.addEventListener('visibilitychange', scheduleBlink);
  motion.addEventListener('change', scheduleBlink);
  window.addEventListener('pagehide', () => { clearTimeout(blinkTimer); eyesOpen(); });
  window.addEventListener('pageshow', scheduleBlink);
  setBusy(false);
  if (stored('open') === '1') open(true);
})();
