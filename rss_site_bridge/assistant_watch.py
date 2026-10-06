"""Short, durable chat questions for watch setup; creation still needs approval."""
from datetime import datetime, timedelta
import re
from zoneinfo import ZoneInfo
from .assistant_audit import record


def reply(service, history, emit, text, state=None, choices=None, mode='single'):
    values=choices or []
    message=dict(role='assistant',content=text)
    if state: message.update(_watch=state,_choices=values,_choice_mode=mode)
    history.append(message)
    record(service.db,service.access.principal,'local_reply',conversation=service.access.conversation,response=text,choices=values,purpose='watch_setup')
    emit('message',dict(content=text,choices=values,choice_mode=mode))


def previous(history):
    for message in reversed(history[:-1]):
        if message['role']=='assistant': return message.get('_watch')
    return None


def question(state, options):
    config=state['config'];topic=state['topic']
    if not config.get('terms'):
        if state.get('specific_title'):
            return 'Which exact title should I watch for?',[], 'title'
        return f'Should I watch for any title matching “{topic}”, or a specific title?', ['Any matching title','A specific title'], 'terms'
    if 'feed_ids' not in config:
        return 'Should I check all your feeds, or just particular ones?', ['All feeds','Choose feeds'], 'feeds'
    if 'mode' not in config:
        return 'Would you like a notification for every new match, or just the first one?', ['Every new match','Once, then complete'], 'mode'
    if 'channels' not in config:
        choices=['Nightfeed']
        if options['push_available']: choices.append('Push')
        if options['email_available']: choices.append('Email')
        return 'How should I notify you? Nightfeed notifications are always included.'+ (' Push reaches all your registered devices.' if options['push_available'] else ''),choices,'channels'
    if 'expires_at' not in config:
        return f'How long should I keep watching? Dates use {options["timezone"]}.', ['7 days','30 days','No expiry','Choose a date'], 'expiry'
    if not state.get('filters_chosen') and not any(key in config for key in ('required_terms','exclude_terms')):
        return 'Would you like to narrow the match by language, quality, or words to exclude?', ['No extra filters','Add filters'], 'filters'
    return None


def emit_setup(service, history, emit, topic, preferences=None, state=None):
    from .assistant_services import TASK_FIELDS
    options=service.call('prepare_topic_watch',dict(topic=topic))['options']
    state=dict(state or {},topic=topic,config=dict((state or {}).get('config',{}),**(preferences or {})))
    state['config']={k:v for k,v in state['config'].items() if k in TASK_FIELDS}
    next_question=question(state,options)
    if next_question:
        text,choices,stage=next_question;state['stage']=stage
        mode='multi' if stage=='channels' else 'single'
        reply(service,history,emit,text,state,choices,mode)
    else:
        config=dict(name='Watch for '+topic,match_mode='flexible',fields='title',**state['config'])
        preview=service.call('preview_task',dict(config=config))
        draft=service.call('propose_task',dict(config=config))
        draft['payload']['preview']=preview
        card=dict(kind='draft',data=draft)
        history.append(dict(role='assistant',content='Ready to create this watch. Please approve or deny it.',_card_only=True,_cards=[card]))
        emit('card',card);emit('message',dict(content='',card_only=True))


def handle(service, history, emit):
    state=previous(history)
    if not state or history[-1].get('_images'): return False
    state=dict(state,config=dict(state['config']));text=history[-1]['content'].strip();key=text.casefold().rstrip('.!')
    stage=state['stage'];config=state['config']
    if key in ('cancel','cancel this watch','never mind'):
        reply(service,history,emit,'Okay, I haven’t created a watch.');return True
    if stage=='terms' and key=='any matching title': config['terms']=[state['topic']]
    elif stage=='terms' and key=='a specific title': state['specific_title']=True
    elif stage=='title' and len(text)<=160 and not re.search(r'[?!;\n]|\b(ignore|instructions|president|who|what|delete|refresh)\b',text,re.I): config['terms']=[text]
    elif stage=='feeds' and key in ('all feeds','across all feeds'): config['feed_ids']=list(service.access.feed_ids)
    elif stage=='feeds' and key=='choose feeds':
        options=service.call('prepare_topic_watch',dict(topic=state['topic']))['options']
        titles=[feed['title'] for feed in options['feeds']]
        choices=['Only '+feed['title']+(f" (feed #{feed['id']})" if titles.count(feed['title'])>1 else '') for feed in options['feeds']]
        state['stage']='feed_selection';state['feed_choices']={label:[feed['id']] for label,feed in zip(choices,options['feeds'])}
        reply(service,history,emit,'Which feeds? Pick one below, or name several in your reply.',state,choices);return True
    elif stage=='feed_selection' and text in state.get('feed_choices',{}): config['feed_ids']=state['feed_choices'][text]
    elif stage=='mode' and key in ('every new match','every time','every match'): config['mode']='every'
    elif stage=='mode' and key in ('once','once, then complete','just once'): config['mode']='once'
    elif stage=='channels' and key in ('nightfeed only','push','email','push and email'):
        options=service.call('prepare_topic_watch',dict(topic=state['topic']))['options']
        if ('push' in key and not options['push_available']) or ('email' in key and not options['email_available']): return False
        config['channels']=['nightfeed']+(['push'] if 'push' in key else [])+(['email'] if 'email' in key else [])
    elif stage=='expiry' and key in ('7 days','30 days','no expiry'):
        options=service.call('prepare_topic_watch',dict(topic=state['topic']))['options']
        config['expires_at']='' if key=='no expiry' else (datetime.now(ZoneInfo(options['timezone']))+timedelta(days=int(key.split()[0]))).isoformat()
    elif stage=='expiry' and key=='choose a date':
        state['stage']='date';reply(service,history,emit,'What date should I stop watching? Enter YYYY-MM-DD, optionally with a time.',state);return True
    elif stage=='date' and re.fullmatch(r'\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2})?',text):
        options=service.call('prepare_topic_watch',dict(topic=state['topic']))['options']
        try:
            expiry=datetime.fromisoformat(text).replace(tzinfo=ZoneInfo(options['timezone']))
            if expiry<=datetime.now(expiry.tzinfo): raise ValueError('Past expiry')
        except ValueError:
            reply(service,history,emit,'Please choose a valid future date, using YYYY-MM-DD.',state);return True
        config['expires_at']=expiry.isoformat()
    elif stage=='filters' and key=='no extra filters': state['filters_chosen']=True
    elif stage=='filters' and key=='add filters':
        state['stage']='filter_details';reply(service,history,emit,'What should I include or exclude? For example, “Tamil, 1080p, exclude trailers”.',state);return True
    else: return False
    history[-1]['_scope_allowed']=True
    emit_setup(service,history,emit,state['topic'],state=state)
    return True
