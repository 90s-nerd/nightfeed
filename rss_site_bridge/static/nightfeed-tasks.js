(() => {
  const el=(tag,text='',cls='')=>{const n=document.createElement(tag);n.textContent=text;if(cls)n.className=cls;return n;};
  const api=async(path,body)=>{const response=await fetch(path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const data=await response.json();if(!response.ok)throw Error(data.error||'Could not load tasks.');return data;};
  const device=()=>{try{return localStorage.getItem('nightfeed.push.device')||'';}catch(_){return '';}};
  const caps=()=>api('/api/tasks/options',{device_token:device()});
  const date=(stamp,tz)=>stamp?new Intl.DateTimeFormat(undefined,{dateStyle:'medium',timeStyle:'short',timeZone:tz}).format(new Date(stamp*1000)):'Never';
  const localDate=(stamp,tz)=>{const parts=new Intl.DateTimeFormat('en-CA',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23',timeZone:tz}).formatToParts(new Date(stamp*1000));const p=Object.fromEntries(parts.map(p=>[p.type,p.value]));return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}`;};
  const split=value=>value.split('\n').map(v=>v.trim()).filter(Boolean);
  let identity=0;
  const link=(label,url)=>{const a=el('a',label,'task-link');a.href=url;return a;};
  const editable=config=>Object.fromEntries(Object.entries(config).filter(([key])=>['name','terms','required_terms','exclude_terms','feed_ids','mode','match_mode','fields','channels','expires_at'].includes(key)));
  const setup=(target,data,onSaved,task=null)=>{
    const opt=data.options,pref=task?task.config:(data.preferences||{}),uid='task-'+(++identity);
    const form=el('form','','task-setup');target.append(form);
    const field=(label,value='',type='text')=>{const wrap=el('div','','field'),id=uid+'-'+label.replaceAll(' ','-');const caption=el('label',label);caption.htmlFor=id;const input=el(type==='textarea'?'textarea':'input');if(type!=='textarea')input.type=type;input.id=id;input.value=value;wrap.append(caption,input);form.append(wrap);return input;};
    const name=field('Task name',pref.name||`Watch for ${data.topic||''}`);name.required=true;name.maxLength=120;
    const terms=field('Match any of these phrases',(pref.terms||[data.topic||'']).join('\n'),'textarea');terms.required=true;terms.setAttribute('aria-describedby',uid+'-terms-note');
    const note=el('small','One phrase per line. Refine a broad topic with a movie name, language, or quality below.');note.id=uid+'-terms-note';terms.parentElement.append(note);
    const choice=(label,values,selected,multiple=false)=>{
      const group=el('fieldset'),legend=el('legend',label),items=el('div','','task-choices');group.append(legend,items);form.append(group);const inputs=[];
      for(const [value,title,disabled] of values){const chip=el('label','','task-chip'),input=el('input');input.type=multiple?'checkbox':'radio';input.name=uid+'-'+label;input.value=value;input.checked=multiple?(selected||[]).includes(value):selected===value;input.disabled=!!disabled;if(!multiple)input.required=true;chip.append(input,el('span',title));items.append(chip);inputs.push(input);}
      return {group,inputs,value:()=>multiple?inputs.filter(i=>i.checked).map(i=>i.value):inputs.find(i=>i.checked)?.value};
    };
    const matching=choice('Title variations',[['flexible','Include punctuation and spacing variations'],['exact','Only my wording']],pref.match_mode||'flexible');
    matching.group.append(el('small','For example: Spider Man, Spider-Man, and Spiderman.'));
    const scope=choice('Which feeds?',[['all','All feeds'],['selected','Choose feeds']],pref.feed_ids===undefined?null:pref.feed_ids.length?'selected':'all');
    const feeds=el('div','','task-feeds');feeds.hidden=scope.value()!=='selected';scope.group.append(feeds);
    for(const feed of opt.feeds){const label=el('label'),input=el('input');input.type='checkbox';input.value=feed.id;input.checked=(pref.feed_ids||[]).includes(feed.id);label.append(input,el('span',feed.title+(!feed.active?' · Paused':!feed.next_refresh?' · Manual refresh':'')));feeds.append(label);}
    scope.inputs.forEach(i=>i.addEventListener('change',()=>{feeds.hidden=scope.value()!=='selected';}));
    scope.group.append(el('small','Checks when the selected feeds refresh. All feeds includes feeds added later. Paused feeds do not check automatically.'));
    const mode=choice('How often?',[['every','Every new match'],['once','Once, then complete']],pref.mode);
    const channels=choice('Notify me through',[['nightfeed','Nightfeed',true],...(opt.push_available||(pref.channels||[]).includes('push')?[['push','Push · '+opt.push_label]]:[]),...(opt.email_available?[['email','Email']]:[])],pref.channels||['nightfeed'],true);
    if(opt.email_available)channels.group.append(el('small','Email: '+opt.email_recipient));
    if(opt.push_available)channels.group.append(el('small','Notifies all your registered devices, including devices added later. Each device’s quiet hours and daily limit apply.'));
    if(!opt.push_available||!opt.email_available)channels.group.append(link('Configure additional delivery options','/settings'));
    const expiry=choice('Watch until',[['none','No expiry'],['7','In 7 days'],['30','In 30 days'],['custom','Choose date and time']],pref.expires_at?'custom':'none');
    const expires=field('Expiry date and time',pref.expires_at?(typeof pref.expires_at==='string'&&!/[Z+]|-\d\d:\d\d$/.test(pref.expires_at.slice(10))?pref.expires_at.slice(0,16):localDate(typeof pref.expires_at==='number'?pref.expires_at:Date.parse(pref.expires_at)/1000,opt.timezone)):'','datetime-local');expires.parentElement.hidden=expiry.value()!=='custom';expiry.inputs.forEach(i=>i.addEventListener('change',()=>{expires.parentElement.hidden=expiry.value()!=='custom';expires.required=expiry.value()==='custom';}));
    expiry.group.append(el('small',`Uses ${opt.timezone}. Expired tasks are archived automatically.`));
    const refine=el('details');refine.append(el('summary','Refine matching'));form.append(refine);
    const required=field('Also require these phrases',(pref.required_terms||[]).join('\n'),'textarea');refine.append(required.parentElement);
    const excluded=field('Exclude these phrases',(pref.exclude_terms||[]).join('\n'),'textarea');refine.append(excluded.parentElement);
    const fields=choice('Search within',[['title','Title only'],['title_summary','Title and summary']],pref.fields||'title');refine.append(fields.group);
    const error=el('p','','task-error');error.setAttribute('role','status');const sample=el('div','','task-preview');form.append(error,sample);
    const config=()=>{
      const ids=scope.value()==='all'?[]:Array.from(feeds.querySelectorAll('input:checked'),i=>Number(i.value));
      if(!scope.value())throw Error('Choose all feeds or specific feeds.');if(scope.value()==='selected'&&!ids.length)throw Error('Select at least one feed.');if(!mode.value())throw Error('Choose every match or once.');
      let expiryValue=null;if(expiry.value()==='custom'){if(!expires.value)throw Error('Choose an expiry date and time.');expiryValue=expires.value;}else if(expiry.value()!=='none')expiryValue=new Date(Date.now()+Number(expiry.value())*86400000).toISOString();
      return {name:name.value,terms:split(terms.value),required_terms:split(required.value),exclude_terms:split(excluded.value),feed_ids:ids,mode:mode.value(),match_mode:matching.value(),fields:fields.value(),channels:channels.value(),expires_at:expiryValue};
    };
    const inConversation=Boolean(data.conversation);
    const actions=el('div','',inConversation?'task-actions':'editor-actions');
    const preview=el('button','Preview existing matches',inConversation?'':'btn btn-secondary');
    const save=el('button',(inConversation?'✓ ':'')+(task?'Save changes':'Create task'),inConversation?'task-confirm':'btn btn-primary');
    const cancel=el('button',inConversation?'✕ Cancel':'Cancel',inConversation?'task-cancel':'btn btn-secondary');
    preview.type=cancel.type='button';save.type='submit';
    if(inConversation){actions.append(save,cancel,preview);}else{
      const previewActions=el('div','','button-row');previewActions.append(preview);form.insertBefore(previewActions,sample);
      actions.append(cancel,save);
    }
    form.append(actions);
    const lock=value=>form.querySelectorAll('button').forEach(b=>{b.disabled=value;});
    preview.addEventListener('click',async()=>{error.textContent='';try{const value=config();lock(true);const result=await api('/api/tasks/preview',{config:value,task_id:task?.id,device_token:device()});sample.replaceChildren(el('p',`${result.total_count} existing matches · Preview only`));for(const item of result.items)sample.append(link(item.title,item.link));sample.append(el('small',result.note));}catch(e){error.textContent=e.message;}finally{lock(false);}});
    cancel.addEventListener('click',()=>{if(data.onCancel){data.onCancel();return;}target.replaceChildren(el('p','Task setup cancelled. No task created.'));const restart=el('button','Reopen setup','task-link');restart.type='button';restart.addEventListener('click',()=>{target.replaceChildren();setup(target,data,onSaved,task);});target.append(restart);});
    form.addEventListener('submit',async e=>{e.preventDefault();error.textContent='';try{const value=config();lock(true);const result=await api(task?'/api/tasks/'+task.id:'/api/tasks',{config:value,revision:task?.revision,conversation:data.conversation,setup_id:data.setup_id,device_token:device()});target.replaceChildren(el('p',result.message),link('View task ↗',result.url));onSaved?.(result);}catch(e){error.textContent=e.message;}finally{lock(false);}});
    return form;
  };
  const renderList=(target,tasks,onOpen,onChange)=>{
    target.replaceChildren();if(!tasks.length){target.append(el('p','No tasks here yet. Ask the assistant to watch for a topic, or create one.','task-note'));return;}
    for(const task of tasks){const row=el('article','','task-row'),head=el('div','','task-row-head');head.append(el('h3',task.name));const state=el('span',task.reason==='expired'?'Expired':task.state[0].toUpperCase()+task.state.slice(1),'task-state');state.dataset.state=task.state;head.append(state);row.append(head);
      const cfg=task.config;row.append(el('p',`${cfg.feed_ids.length?cfg.feed_ids.length+' selected feeds':'All feeds'} · ${cfg.mode==='once'?'Once':'Every new match'} · ${cfg.channels.map(c=>c==='nightfeed'?'Nightfeed':c==='email'?'Email':'Push').join(' + ')}`));
      row.append(el('p',`${task.match_count} ${task.match_count===1?"match":"matches"} · ${task.last_checked?'Last checked '+date(task.last_checked,cfg.timezone):'Waiting for next refresh'}`));
      if(task.expires)row.append(el('p',`${task.reason==='expired'?'Expired':'Watch until'} ${date(task.expires,cfg.timezone)} · ${cfg.timezone}`));
      if(task.reason==='expired'&&!task.match_count)row.append(el('p','No matches found before expiry.'));
      if(task.delivery_issues)row.append(el('p',`${task.delivery_issues} delivery issues · Open task for details`,'task-error'));
      const actions=el('div','','task-actions'),open=el('button','Details');open.type='button';open.addEventListener('click',()=>onOpen(task));actions.append(open);
      if(task.state==='active'||task.state==='paused'){const pause=el('button',task.state==='active'?'Pause':'Resume');pause.type='button';pause.addEventListener('click',async()=>{pause.disabled=true;try{await api('/api/tasks/'+task.id,{action:task.state==='active'?'pause':'resume'});await onChange();}catch(e){row.append(el('p',e.message,'task-error'));pause.disabled=false;}});actions.append(pause);}
      row.append(actions);target.append(row);
    }
  };
  const mount=async(target)=>{
    let selected='all';target.replaceChildren();const tools=el('div','','task-actions'),create=el('button','New task');create.type='button';tools.append(create,link('View all tasks ↗','/tasks'));const filters=el('div','','task-filters'),content=el('div'),status=el('p','','task-error');if(!target.closest('[data-tasks-page]'))target.append(tools);target.append(filters,status,content);
    const refresh=async()=>{try{const result=await api('/api/tasks?state='+selected);renderList(content,result.tasks,open,refresh);}catch(e){status.textContent=e.message;}};
    for(const state of ['all','active','paused','completed','archived']){const button=el('button',state[0].toUpperCase()+state.slice(1));button.type='button';button.setAttribute('aria-pressed',String(state===selected));button.addEventListener('click',()=>{selected=state;filters.querySelectorAll('button').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));refresh();});filters.append(button);}
    const back=()=>{content.replaceChildren();refresh();};
    const open=async task=>{try{const detail=await api('/api/tasks/'+task.id),opt=await caps();content.replaceChildren();const backButton=el('button','← Tasks','task-link task-back');backButton.type='button';backButton.addEventListener('click',back);content.append(backButton);renderDetail(content,detail,opt,back);}catch(e){status.textContent=e.message;}};
    create.addEventListener('click',async()=>{try{content.replaceChildren();setup(content,{options:await caps(),onCancel:back},back);}catch(e){status.textContent=e.message;}});
    await refresh();return {open,refresh};
  };
  const renderDetail=(target,task,opt,back)=>{
    const detail=el('div','','task-detail');target.append(detail);renderList(detail,[task],()=>{},back);const actions=el('div','','task-actions'),edit=el('button','Edit task'),archive=el('button',task.state==='archived'||task.state==='completed'?'Reactivate':'Archive');edit.type=archive.type='button';actions.append(edit,archive);detail.append(actions);
    edit.addEventListener('click',()=>{target.replaceChildren();setup(target,{options:opt,onCancel:back},back,task);});
    archive.addEventListener('click',async()=>{archive.disabled=true;try{await api('/api/tasks/'+task.id,{action:task.state==='archived'||task.state==='completed'?'resume':'archive'});back();}catch(e){detail.append(el('p',e.message,'task-error'));archive.disabled=false;}});
    const rule=el('section');rule.append(el('h3','Matching rule'),el('p','Any: '+task.config.terms.join(', ')));if(task.config.required_terms.length)rule.append(el('p','Also require: '+task.config.required_terms.join(', ')));if(task.config.exclude_terms.length)rule.append(el('p','Exclude: '+task.config.exclude_terms.join(', ')));rule.append(el('p',(task.config.fields==='title'?'Title only':'Title and summary')+' · '+(task.config.match_mode==='flexible'?'Punctuation and spacing variations':'Exact wording')));detail.append(rule);
    const matches=el('section');matches.append(el('h3','Recent matches'));const list=el('ul','','task-history');if(!task.matches.length)list.append(el('li','No matches yet.'));for(const match of task.matches){const item=el('li');item.append(link(match.title,`/notifications/${match.notification_id}`),el('small',date(match.created,task.config.timezone)));list.append(item);}matches.append(list);detail.append(matches);
    const delivery=el('section');delivery.append(el('h3','Delivery history'));const rows=el('ul','','task-history');if(!task.deliveries.length)rows.append(el('li','No deliveries yet.'));for(const d of task.deliveries)rows.append(el('li',`${d.channel==='nightfeed'?'Nightfeed':d.channel==='push'?'Push · Device '+d.device_id.slice(0,8):'Email'} · ${d.channel==='nightfeed'?'Available in app':d.state+' · '+d.attempts+' '+(d.attempts===1?'attempt':'attempts')}${d.last_error?' · '+d.last_error:''}`));delivery.append(rows);detail.append(delivery);
    const history=el('details');history.append(el('summary','Task activity'));for(const h of task.history)history.append(el('p',h.kind.replaceAll('_',' ')+' · '+date(h.created,task.config.timezone)));detail.append(history);
  };
  window.nightfeedTasks={setup,renderList,mount,options:caps,editable,date};
  const page=document.querySelector('[data-tasks-page]');if(page){const content=page.querySelector('[data-tasks-content]');mount(content).then(controller=>{document.querySelector('[data-tasks-create]')?.addEventListener('click',async()=>{try{content.replaceChildren();setup(content,{options:await caps(),onCancel:()=>mount(content)},()=>mount(content));}catch(e){page.querySelector('[data-tasks-status]').textContent=e.message;}});const id=new URL(location.href).searchParams.get('task');if(id)api('/api/tasks/'+id).then(controller.open).catch(e=>{page.querySelector('[data-tasks-status]').textContent=e.message;});});page.querySelectorAll('[data-task-filter]').forEach(b=>b.remove());}
})();
