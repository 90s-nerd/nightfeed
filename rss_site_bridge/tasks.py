"""Durable topic watches. Matching never calls an AI provider."""
from contextlib import closing
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo
import hashlib
import json
import re
import secrets
import time
import unicodedata

from flask import Blueprint, g, jsonify, render_template, request


class DeliveryUnavailable(ValueError):
    """Safe, local delivery diagnostics; provider exceptions stay redacted."""


def connect(db):
    from .app import connect_db
    return connect_db(db)


def initialize(db):
    with closing(connect(db)) as conn:
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS topic_tasks (
          id TEXT PRIMARY KEY, principal TEXT NOT NULL, name TEXT NOT NULL, config TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'active', reason TEXT NOT NULL DEFAULT '',
          created REAL NOT NULL, updated REAL NOT NULL, expires REAL, last_checked REAL,
          revision INTEGER NOT NULL DEFAULT 1, setup_id TEXT);
        CREATE INDEX IF NOT EXISTS topic_tasks_owner ON topic_tasks(principal,state);
        CREATE TABLE IF NOT EXISTS task_matches (
          id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
          feed_id INTEGER NOT NULL, item_id INTEGER NOT NULL, title TEXT NOT NULL, link TEXT NOT NULL,
          created REAL NOT NULL, notification_id INTEGER, UNIQUE(task_id,fingerprint));
        CREATE TABLE IF NOT EXISTS task_events (
          id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, kind TEXT NOT NULL, created REAL NOT NULL, details TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS task_deliveries (
          id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, notification_id INTEGER NOT NULL,
          channel TEXT NOT NULL, destination TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
          attempts INTEGER NOT NULL DEFAULT 0, due REAL NOT NULL, lease REAL NOT NULL DEFAULT 0,
          last_error TEXT NOT NULL DEFAULT '', sent REAL,
          UNIQUE(notification_id,channel,destination));
        CREATE INDEX IF NOT EXISTS task_deliveries_due ON task_deliveries(state,due);
        ''')
        from .app import ensure_column
        ensure_column(conn,'topic_tasks','setup_id','TEXT')
        conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS topic_tasks_setup ON topic_tasks(setup_id)');conn.commit()


def owner(db, access):
    if access.principal.startswith('key:'):
        with closing(connect(db)) as conn:
            row = conn.execute('SELECT user_id FROM auth_api_keys WHERE id=?', (access.principal.split(':')[1],)).fetchone()
        if not row: raise ValueError('Credential not found.')
        return 'user:' + str(row['user_id'])
    return access.principal


def event(conn, task, kind, **details):
    now = time.time()
    conn.execute('INSERT INTO task_events(task_id,kind,created,details) VALUES(?,?,?,?)', (task['id'], kind, now, json.dumps(details)))
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='assistant_audit'").fetchone():
        conn.execute('INSERT INTO assistant_audit(principal,kind,status,created,details) VALUES(?,?,?,?,?)',
                     (task['principal'], 'task_' + kind, 'error' if kind == 'delivery_failed' else 'ok', now, json.dumps(dict(task_id=task['id'], **details))))


def options(db, device_id=None, principal=None):
    from . import app as core
    settings = core.get_app_settings(db)
    with closing(connect(db)) as conn:
        user_id = principal.split(':',1)[1] if principal and principal.startswith('user:') else None
        devices = conn.execute('SELECT id FROM push_devices WHERE user_id=? AND enabled=1', (user_id,)).fetchall()
    return dict(timezone=settings.timezone_name, current_time=datetime.now(ZoneInfo(settings.timezone_name)).isoformat(), email_available=core.smtp_configured(settings),
                email_recipient=settings.smtp_to_email if core.smtp_configured(settings) else '',
                push_available=bool(devices), push_device_count=len(devices),
                push_label=f'All devices ({len(devices)})',
                feeds=[dict(id=p.id, title=p.feed_title, active=p.active, next_refresh=core.get_next_refresh_at(p).isoformat() if core.get_next_refresh_at(p) else None) for p in core.list_profiles(db)])


def words(value):
    return unicodedata.normalize('NFKC', value).casefold()


def matches(config, title, summary=''):
    text = words(title + (' ' + summary if config['fields'] == 'title_summary' else ''))
    def found(term):
        term = words(term)
        if config['match_mode'] == 'flexible':
            pattern = r'[\W_]*'.join(re.escape(word) for word in re.findall(r'[^\W_]+', term))
        else:
            pattern = re.escape(term)
        return bool(pattern and re.search(r'(?<!\w)' + pattern + r'(?!\w)', text))
    return any(found(t) for t in config['terms']) and all(found(t) for t in config.get('required_terms',[])) and not any(found(t) for t in config['exclude_terms'])


def normalize(db, value, access, *, existing=None):
    if not isinstance(value, dict): raise ValueError('Choose task settings.')
    if set(value) - {'name','terms','required_terms','exclude_terms','feed_ids','mode','match_mode','fields','channels','expires_at'}:
        raise ValueError('Unknown task setting.')
    result = dict(name=value.get('name',''), terms=value.get('terms',[]), required_terms=value.get('required_terms',[]), exclude_terms=value.get('exclude_terms',[]),
                  feed_ids=value.get('feed_ids',[]), mode=value.get('mode',''), match_mode=value.get('match_mode','flexible'),
                  fields=value.get('fields','title'), channels=value.get('channels',['nightfeed']), expires_at=value.get('expires_at'))
    if not isinstance(result['name'], str) or not 1 <= len(result['name'].strip()) <= 120: raise ValueError('Give the task a name of up to 120 characters.')
    result['name'] = result['name'].strip()
    for key in ('terms','required_terms','exclude_terms'):
        if not isinstance(result[key], list) or len(result[key]) > 12 or any(not isinstance(t,str) or not t.strip() or len(t)>160 or not re.search(r'\w',t) for t in result[key]):
            raise ValueError('Use up to 12 words or phrases, each up to 160 characters.')
        result[key] = list(dict.fromkeys(t.strip() for t in result[key]))
    if not result['terms']: raise ValueError('What topic should this task watch for?')
    for key, allowed in [('mode',('every','once')),('match_mode',('flexible','exact')),('fields',('title','title_summary'))]:
        if result[key] not in allowed: raise ValueError('Choose a valid ' + key.replace('_',' ') + '.')
    ids = result['feed_ids']
    if not isinstance(ids,list) or len(ids)>100 or any(type(i) is not int for i in ids): raise ValueError('Choose valid feeds.')
    result['feed_ids'] = sorted(set(ids))
    if access.feed_ids and (not ids or any(i not in access.feed_ids for i in ids)): raise ValueError('Choose only feeds permitted by this credential.')
    from . import app as core
    if any(core.get_profile_by_id(db,i) is None for i in ids): raise ValueError('A selected feed no longer exists.')
    if not isinstance(result['channels'],list) or any(c not in ('nightfeed','push','email') for c in result['channels']): raise ValueError('Choose valid delivery channels.')
    result['channels'] = list(dict.fromkeys(['nightfeed', *result['channels']]))
    caps = options(db, access.device_id, owner(db,access))
    if 'email' in result['channels'] and not caps['email_available']: raise ValueError('Configure SMTP before choosing email.')
    if 'push' in result['channels']:
        if not caps['push_available']: raise ValueError('Register at least one device for push notifications first.')
    result['push_all_devices'] = 'push' in result['channels']
    result['email_recipient'] = caps['email_recipient'] if 'email' in result['channels'] else ''
    expires = result['expires_at']
    if expires not in (None,''):
        try:
            parsed = datetime.fromisoformat(expires.replace('Z','+00:00')) if isinstance(expires,str) else datetime.fromtimestamp(float(expires),timezone.utc)
            if parsed.tzinfo is None:
                wall=parsed;parsed=parsed.replace(tzinfo=ZoneInfo(caps['timezone']))
                if parsed.astimezone(timezone.utc).astimezone(parsed.tzinfo).replace(tzinfo=None)!=wall:
                    raise ValueError('That time does not exist because of daylight saving time.')
            expires = parsed.timestamp()
        except (ValueError,TypeError,OverflowError): raise ValueError('Choose a valid expiry date and time.')
        if expires <= time.time(): raise ValueError('Expiry must be in the future.')
    else: expires = None
    result['expires_at'] = expires
    result['timezone'] = caps['timezone']
    return result


def permitted(task, access):
    config = json.loads(task['config'])
    return not access.feed_ids or (config['feed_ids'] and set(config['feed_ids']).issubset(access.feed_ids))


def expire(conn, now):
    for task in conn.execute("SELECT * FROM topic_tasks WHERE state IN ('active','paused') AND expires IS NOT NULL AND expires<=?", (now,)).fetchall():
        conn.execute("UPDATE topic_tasks SET state='archived',reason='expired',updated=?,revision=revision+1 WHERE id=?", (now,task['id']))
        event(conn,task,'expired')


def serialize(conn, task):
    result = dict(task);result['config'] = json.loads(task['config'])
    result['match_count'] = conn.execute('SELECT COUNT(*) FROM task_matches WHERE task_id=?',(task['id'],)).fetchone()[0]
    result['delivery_issues'] = conn.execute("SELECT COUNT(*) FROM task_deliveries WHERE task_id=? AND state IN ('retry','failed')",(task['id'],)).fetchone()[0]
    return result


def list_tasks(db, access, state='all'):
    with closing(connect(db)) as conn:
        conn.execute('BEGIN IMMEDIATE');expire(conn,time.time());conn.commit()
        rows = conn.execute('SELECT * FROM topic_tasks WHERE principal=? ORDER BY updated DESC', (owner(db,access),)).fetchall()
        return [serialize(conn,r) for r in rows if permitted(r,access) and (state=='all' or r['state']==state)]


def get_task(db, access, identity):
    tasks = list_tasks(db,access)
    task = next((t for t in tasks if t['id']==identity),None)
    if not task: raise ValueError('Task not found.')
    with closing(connect(db)) as conn:
        task['matches'] = [dict(r) for r in conn.execute('SELECT * FROM task_matches WHERE task_id=? ORDER BY id DESC LIMIT 100',(identity,))]
        task['deliveries'] = [dict(r) for r in conn.execute("SELECT id,notification_id,channel,state,attempts,last_error,sent,CASE WHEN channel='push' THEN destination ELSE '' END AS device_id FROM task_deliveries WHERE task_id=? ORDER BY id DESC LIMIT 100",(identity,))]
        task['history'] = [dict(r,details=json.loads(r['details'])) for r in conn.execute('SELECT * FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 100',(identity,))]
    return task


def save(db, access, value, identity=None, revision=None, *, conn=None, setup_id=None):
    access.permit('mcp:write')
    if identity and conn is not None:
        row=conn.execute('SELECT * FROM topic_tasks WHERE id=? AND principal=?',(identity,owner(db,access))).fetchone()
        if not row or not permitted(row,access): raise ValueError('Task not found.')
        old=dict(row);old['config']=json.loads(row['config'])
    else: old = get_task(db,access,identity) if identity else None
    config = normalize(db,value,access,existing=old['config'] if old else None)
    own_conn = conn is None
    conn = connect(db) if own_conn else conn
    try:
        if own_conn: conn.execute('BEGIN IMMEDIATE')
        now = time.time(); identity = identity or secrets.token_urlsafe(18)
        if setup_id:
            duplicate=conn.execute('SELECT id FROM topic_tasks WHERE setup_id=? AND principal=?',(setup_id,owner(db,access))).fetchone()
            if duplicate: return dict(message='Task already created.',url='/tasks?task='+duplicate['id'],task_id=duplicate['id'],setup_id=setup_id)
        if old:
            if type(revision) is not int: raise ValueError('Reload this task before editing it.')
            if not conn.execute('UPDATE topic_tasks SET name=?,config=?,expires=?,updated=?,revision=revision+1 WHERE id=? AND revision=?',
                                (config['name'],json.dumps(config),config['expires_at'],now,identity,revision)).rowcount:
                raise ValueError('This task changed. Reload it before saving.')
        else:
            conn.execute('INSERT INTO topic_tasks(id,principal,name,config,created,updated,expires,setup_id) VALUES(?,?,?,?,?,?,?,?)',
                         (identity,owner(db,access),config['name'],json.dumps(config),now,now,config['expires_at'],setup_id))
        task = conn.execute('SELECT * FROM topic_tasks WHERE id=?',(identity,)).fetchone()
        event(conn,task,'updated' if old else 'created',config=config,actor=access.principal)
        if own_conn: conn.commit()
        return dict(message='Task updated.' if old else 'Task created. Watching from now on.',url='/tasks?task='+identity,task_id=identity,setup_id=setup_id)
    finally:
        if own_conn: conn.close()


def change_state(db,access,identity,action):
    access.permit('mcp:write');get_task(db,access,identity)
    if action not in ('pause','resume','archive'): raise ValueError('Choose pause, resume, or archive.')
    with closing(connect(db)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        task=conn.execute('SELECT * FROM topic_tasks WHERE id=?',(identity,)).fetchone()
        if action=='resume' and task['expires'] and task['expires']<=time.time(): raise ValueError('Set a future expiry or remove expiry before reactivating this task.')
        state={'pause':'paused','resume':'active','archive':'archived'}[action]
        if action=='pause' and task['state']!='active': raise ValueError('Only active tasks can be paused.')
        conn.execute('UPDATE topic_tasks SET state=?,reason=?,updated=?,revision=revision+1 WHERE id=?',(state,'manual' if state=='archived' else '',time.time(),identity))
        event(conn,task,action,actor=access.principal);conn.commit()
    return dict(message={'pause':'Task paused.','resume':'Task reactivated.','archive':'Task archived.'}[action],url='/tasks?task='+identity,task_id=identity)


def preview(db,access,value,identity=None):
    existing=get_task(db,access,identity)['config'] if identity else None
    config=normalize(db,value,access,existing=existing)
    with closing(connect(db)) as conn:
        sql='SELECT i.*,p.feed_title FROM feed_items i JOIN profiles p ON p.id=i.profile_id'
        args=[]
        if config['feed_ids']:
            sql+=' WHERE i.profile_id IN ('+','.join('?' for _ in config['feed_ids'])+')';args=config['feed_ids']
        rows=conn.execute(sql+' ORDER BY i.id DESC',args)
        total=0;items=[]
        for row in rows:
            if matches(config,row['title'],row['summary']):
                total+=1
                if len(items)<3: items.append(dict(title=row['title'],link=row['link'],feed_title=row['feed_title']))
    return dict(total_count=total,items=items,note='Preview only. Existing items will not trigger this new watch.')


def process_refresh(conn, profile_id, new_items, now):
    """Called inside the item-save transaction: no gap between items and alerts."""
    expire(conn,now)
    for task in conn.execute("SELECT * FROM topic_tasks WHERE state='active' AND created<=?",(now,)).fetchall():
        config=json.loads(task['config'])
        if config['feed_ids'] and profile_id not in config['feed_ids']: continue
        conn.execute('UPDATE topic_tasks SET last_checked=? WHERE id=?',(now,task['id']))
        found=[]
        for item in new_items:
            if not matches(config,item['title'],item['summary']): continue
            fingerprint=hashlib.sha256(item['link'].encode()).hexdigest()
            cursor=conn.execute('INSERT OR IGNORE INTO task_matches(task_id,fingerprint,feed_id,item_id,title,link,created) VALUES(?,?,?,?,?,?,?)',
                                (task['id'],fingerprint,profile_id,item['id'],item['title'],item['link'],now))
            if cursor.rowcount: found.append(dict(item,item_id=item['id'],match_id=cursor.lastrowid,kind='new',previous_title='',previous_summary=''))
        if not found: continue
        title=f"{task['name']} · {len(found)} new {'match' if len(found)==1 else 'matches'}"
        metadata=dict(task_id=task['id'],changes=found,new_items=len(found),updated_items=0)
        cursor=conn.execute('INSERT INTO notifications(profile_id,event_type,severity,category,title,message,source_url,created_at,metadata_json) VALUES(?,?,?,?,?,?,?,?,?)',
                            (profile_id,'topic_match','info','topic_match',title,'\n'.join(item['title'] for item in found),found[0]['link'],datetime.fromtimestamp(now,timezone.utc).isoformat(),json.dumps(metadata)))
        notice=cursor.lastrowid
        conn.executemany('UPDATE task_matches SET notification_id=? WHERE id=?',[(notice,item['match_id']) for item in found])
        for channel in config['channels']:
            destinations = [r['id'] for r in conn.execute('SELECT id FROM push_devices WHERE user_id=? AND enabled=1', (task['principal'].split(':',1)[1],))] if channel=='push' else [config['email_recipient'] if channel=='email' else '']
            for destination in destinations:
                conn.execute('INSERT INTO task_deliveries(task_id,notification_id,channel,destination,state,due,sent) VALUES(?,?,?,?,?,?,?)',
                             (task['id'],notice,channel,destination,'sent' if channel=='nightfeed' else 'pending',now,now if channel=='nightfeed' else None))
        event(conn,task,'matched',count=len(found),notification_id=notice)
        if config['mode']=='once':
            conn.execute("UPDATE topic_tasks SET state='completed',reason='matched',updated=?,revision=revision+1 WHERE id=?",(now,task['id']))
            event(conn,task,'completed')


def dispatch(db,now=None):
    from . import app as core
    from . import push_notifications as push
    now=time.time() if now is None else now
    with closing(connect(db)) as conn:
        conn.execute('BEGIN IMMEDIATE');expire(conn,now);conn.commit()
        rows=conn.execute("SELECT * FROM task_deliveries WHERE state IN ('pending','retry') AND due<=? AND lease<=? ORDER BY due,id LIMIT 50",(now,now)).fetchall()
    for row in rows:
        with closing(connect(db)) as conn:
            conn.execute('BEGIN IMMEDIATE')
            device=conn.execute("SELECT d.* FROM push_devices d JOIN topic_tasks t ON t.id=? AND t.principal='user:' || d.user_id WHERE d.id=?",(row['task_id'],row['destination'])).fetchone() if row['channel']=='push' else None
            if device and device['enabled']:
                prefs=json.loads(device['preferences']);local=datetime.fromtimestamp(now,ZoneInfo(prefs['timezone']));hour=local.strftime('%H:%M');start,end=prefs['quiet_start'],prefs['quiet_end']
                quiet=prefs['quiet'] and (start<=hour<end if start<end else hour>=start or hour<end)
                limited=device['day']==local.date().isoformat() and device['sent']>=prefs['daily_limit']
                if quiet or limited or device['lease_until']>now:
                    # Deferred pushes must not starve due email or other devices.
                    conn.execute('UPDATE task_deliveries SET due=? WHERE id=? AND lease<=?',(now+60,row['id'],now));conn.commit();continue
            if not conn.execute("UPDATE task_deliveries SET lease=?,attempts=attempts+1 WHERE id=? AND state IN ('pending','retry') AND lease<=?",(now+120,row['id'],now)).rowcount: continue
            if device: conn.execute('UPDATE push_devices SET lease_until=? WHERE id=?',(now+120,device['id']))
            conn.commit()
        notice=core.get_notification(db,row['notification_id'])
        try:
            if not notice: raise DeliveryUnavailable('Notification was removed before delivery.')
            if row['channel']=='push':
                if not device or not device['enabled']: raise DeliveryUnavailable('Push subscription is unavailable. Enable notifications again.')
                push.deliver(db,device,dict(title=notice.title,body=notice.message[:240],url=f'/notifications/{notice.id}',tag=f'nightfeed-task-{notice.id}'))
            else:
                settings=core.get_app_settings(db)
                if not core.smtp_configured(settings): raise DeliveryUnavailable('Email configuration is unavailable.')
                message=EmailMessage();message['Subject']='Nightfeed · '+notice.title;message['From']=settings.smtp_from_email;message['To']=row['destination']
                message['Message-ID']=f'<task-{row["id"]}-{hashlib.sha256(str(db).encode()).hexdigest()[:12]}@nightfeed.local>'
                message.set_content(notice.title+'\n\n'+'\n\n'.join(item['title']+'\n'+item['link'] for item in json.loads(notice.metadata_json).get('changes',[])))
                core.send_smtp_message(settings,message)
        except Exception as exc:
            code=getattr(getattr(exc,'response',None),'status_code',None)
            permanent=isinstance(exc,ValueError) or code in (404,410) or row['attempts']>=7
            with closing(connect(db)) as conn:
                diagnostic=str(exc) if isinstance(exc,DeliveryUnavailable) else 'Delivery failed.' if permanent else 'Delivery failed. Nightfeed will retry.'
                conn.execute('UPDATE task_deliveries SET state=?,lease=0,due=?,last_error=? WHERE id=?',('failed' if permanent else 'retry',now+min(3600,60*2**min(row['attempts'],6)),diagnostic,row['id']))
                if device: conn.execute('UPDATE push_devices SET lease_until=0 WHERE id=?',(device['id'],))
                if code in (404,410) and device: conn.execute('UPDATE push_devices SET enabled=0 WHERE id=?',(device['id'],))
                task=conn.execute('SELECT * FROM topic_tasks WHERE id=?',(row['task_id'],)).fetchone();event(conn,task,'delivery_failed',channel=row['channel'],retry=not permanent);conn.commit()
        else:
            with closing(connect(db)) as conn:
                conn.execute("UPDATE task_deliveries SET state='sent',sent=?,lease=0,last_error='' WHERE id=?",(now,row['id']))
                if row['channel']=='email': conn.execute('UPDATE notifications SET emailed_at=? WHERE id=?',(core.utcnow_text(),notice.id))
                if device:
                    day=datetime.fromtimestamp(now,ZoneInfo(json.loads(device['preferences'])['timezone'])).date().isoformat()
                    conn.execute('UPDATE push_devices SET sent=CASE WHEN day=? THEN sent+1 ELSE 1 END,day=?,lease_until=0 WHERE id=?',(day,day,device['id']))
                task=conn.execute('SELECT * FROM topic_tasks WHERE id=?',(row['task_id'],)).fetchone();event(conn,task,'delivered',channel=row['channel']);conn.commit()


def register(app):
    from .assistant import access_from_request
    db=Path(app.config['DATABASE_PATH']);bp=Blueprint('tasks',__name__)
    @bp.get('/tasks')
    def page(): return render_template('tasks.html')
    @bp.get('/api/tasks')
    def index(): return jsonify(tasks=list_tasks(db,access_from_request(),request.args.get('state','all')))
    @bp.post('/api/tasks/options')
    def capabilities():
        access=access_from_request(chat=True);return jsonify(options(db,access.device_id,owner(db,access)))
    @bp.get('/api/tasks/<identity>')
    def detail(identity):
        try: return jsonify(get_task(db,access_from_request(),identity))
        except ValueError as exc: return jsonify(error=str(exc)),404
    @bp.post('/api/tasks/preview')
    def sample():
        try:
            payload=request.get_json()
            if not isinstance(payload,dict): raise ValueError('Choose task settings.')
            identity=payload.get('task_id')
            if identity is not None and not isinstance(identity,str): raise ValueError('Invalid task.')
            return jsonify(preview(db,access_from_request(chat=True),payload['config'],identity))
        except (ValueError,KeyError,TypeError) as exc: return jsonify(error=str(exc)),400
    @bp.post('/api/tasks')
    def create():
        try:
            access=access_from_request(chat=True);payload=request.get_json() or {}
            if not isinstance(payload,dict): raise ValueError('Choose task settings.')
            setup_id=payload.get('setup_id')
            if setup_id is not None and (not isinstance(setup_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{10,80}',setup_id)): raise ValueError('Invalid task setup.')
            conversation=payload.get('conversation')
            if isinstance(conversation,str):
                with closing(connect(db)) as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    row=conn.execute('SELECT history,busy_until FROM assistant_conversations WHERE id=? AND principal=?',(conversation,access.principal)).fetchone()
                    if not row: raise ValueError('Conversation not found.')
                    if row['busy_until']>time.time(): raise ValueError('Wait for the reply to finish before creating this task.')
                    history=json.loads(row['history'])
                    if not setup_id or not any(c['kind']=='task_setup' and c['data'].get('setup_id')==setup_id for m in history for c in m.get('_cards',[])):
                        raise ValueError('Task setup not found.')
                    result=save(db,access,payload.get('config'),setup_id=setup_id,conn=conn)
                    if not any(c['kind']=='result' and c['data'].get('task_id')==result['task_id'] for m in history for c in m.get('_cards',[])):
                        history.append(dict(role='assistant',content='',_cards=[dict(kind='result',data=result)]))
                        conn.execute('UPDATE assistant_conversations SET history=?,updated=? WHERE id=?',(json.dumps(history),time.time(),conversation))
                    conn.commit()
            else: result=save(db,access,payload.get('config'),setup_id=setup_id)
            return jsonify(result)
        except ValueError as exc: return jsonify(error=str(exc)),400
    @bp.post('/api/tasks/<identity>')
    def update(identity):
        try:
            access=access_from_request(chat=True);payload=request.get_json() or {}
            if not isinstance(payload,dict): raise ValueError('Choose task settings.')
            result=save(db,access,payload.get('config'),identity,payload.get('revision')) if 'config' in payload else change_state(db,access,identity,payload.get('action'))
            return jsonify(result)
        except ValueError as exc: return jsonify(error=str(exc)),409
    app.register_blueprint(bp)
