"""Shared, side-effect-free stored-content queries for chat and MCP."""
from contextlib import closing
import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import app as core

STATUS = dict(type='string', enum=['all', 'unread', 'read', 'saved', 'updated'])
PAGING = dict(limit=dict(type='integer', minimum=1, maximum=100),
              offset=dict(type='integer', minimum=0, maximum=1000000),
              snapshot_id=dict(type='integer', minimum=0, maximum=2**63-1))
DAY = dict(type='string', maxLength=10)
FILTERS = dict(status=STATUS, saved_only=dict(type='boolean'),
               item_ids=dict(type='array', items=dict(type='integer', minimum=1, maximum=2**63-1), maxItems=100000, description='Exact stored item IDs, such as new_item_ids returned by a refresh. Empty means no items.'),
               feed_ids=dict(type='array', items=dict(type='integer', minimum=1, maximum=2**63-1), maxItems=100),
               added_on=dict(DAY, description='Discovery day: today, yesterday or YYYY-MM-DD.'),
               added_from=dict(DAY, description='First included discovery date, YYYY-MM-DD.'),
               added_until=dict(DAY, description='Last included discovery date, YYYY-MM-DD.'),
               period=dict(type='string', enum=['this_week','last_week','last_7_days','last_30_days']),
               timezone=dict(type='string', maxLength=100, description='Optional IANA timezone for discovery dates. Defaults to Nightfeed settings.'))
SORT = dict(type='string', enum=['recent', 'oldest', 'new', 'title', 'priority'])


def dates(db, arguments):
    """Resolve calendar dates once; carry concrete dates into subsequent pages."""
    requested = {key:arguments[key] for key in ('added_on','added_from','added_until','period') if key in arguments}
    if not requested: return {}, []
    if ('added_on' in requested or 'period' in requested) and len(requested)>1:
        raise ValueError('Choose a discovery day, a date range, or a relative period, not a combination.')
    try:
        tz=ZoneInfo(arguments.get('timezone') or core.get_app_settings(db).timezone_name)
        today=datetime.now(tz).date()
        if 'added_on' in requested:
            value=requested['added_on']
            start=today-timedelta(days=int(value=='yesterday')) if value in ('today','yesterday') else date.fromisoformat(value)
            end=start
            metadata=dict(added_on=start.isoformat(),timezone=str(tz),time_field='discovered_at')
        else:
            if requested.get('period'):
                period=requested['period']; monday=today-timedelta(days=today.weekday())
                start,end=(monday,today) if period=='this_week' else (monday-timedelta(days=7),monday-timedelta(days=1)) if period=='last_week' else (today-timedelta(days=6 if period=='last_7_days' else 29),today)
            else:
                start=date.fromisoformat(requested['added_from']) if 'added_from' in requested else None
                end=date.fromisoformat(requested['added_until']) if 'added_until' in requested else None
            if start and end and start>end: raise ValueError('Start date must not be after end date.')
            metadata=dict(timezone=str(tz),time_field='discovered_at')
            if start: metadata['added_from']=start.isoformat()
            if end: metadata['added_until']=end.isoformat()
        bounds=[]
        if start: bounds.append(('>=',datetime.combine(start,datetime.min.time(),tz).astimezone(timezone.utc).isoformat()))
        if end: bounds.append(('<',datetime.combine(end+timedelta(days=1),datetime.min.time(),tz).astimezone(timezone.utc).isoformat()))
        return metadata,bounds
    except (ValueError, OverflowError, ZoneInfoNotFoundError) as exc:
        raise ValueError('Choose valid discovery dates and an IANA timezone; start must be before the end.') from exc


def topics(db, access, arguments, *, count=False):
    if arguments.get('feed_id') and arguments.get('feed_ids'):
        raise ValueError('Choose feed_id or feed_ids, not both.')
    selected=arguments.get('feed_ids') or ([arguments['feed_id']] if arguments.get('feed_id') else list(access.feed_ids))
    for identity in selected: access.permit('app:read',identity)
    query=arguments.get('query',''); status=arguments.get('status','all')
    clauses=['(instr(lower(i.title),lower(?))>0 OR instr(lower(i.summary),lower(?))>0 OR instr(lower(i.link),lower(?))>0)']
    values=[query]*3
    if 'item_ids' in arguments:
        clauses.append('i.id IN (SELECT value FROM json_each(?))'); values.append(json.dumps(arguments['item_ids']))
    if selected:
        clauses.append('i.profile_id IN ('+','.join('?' for _ in selected)+')'); values.extend(selected)
    if status=='unread': clauses.append('i.seen_at IS NULL')
    elif status=='read': clauses.append('i.seen_at IS NOT NULL')
    elif status=='saved': clauses.append('i.saved_at IS NOT NULL')
    elif status=='updated': clauses.append("i.seen_at IS NOT NULL AND i.updated_at>i.seen_at AND (i.update_seen_at IS NULL OR i.updated_at>i.update_seen_at)")
    if arguments.get('saved_only') and status!='saved': clauses.append('i.saved_at IS NOT NULL')
    period,bounds=dates(db,arguments)
    for operator,stamp in bounds:
        clauses.append('julianday(i.discovered_at)'+operator+'julianday(?)'); values.append(stamp)
    filters={key:arguments[key] for key in ('query','feed_id','feed_ids','item_ids','status','saved_only','sort') if key in arguments}
    filters.update(query=query,status=status)
    filters.update({key:value for key,value in period.items() if key!='time_field'})
    with closing(core.connect_db(db)) as conn:
        source=' FROM feed_items i JOIN profiles p ON p.id=i.profile_id WHERE '+' AND '.join(clauses)
        ceiling=arguments.get('snapshot_id')
        if ceiling is None: ceiling=conn.execute('SELECT COALESCE(MAX(i.id),0)'+source,values).fetchone()[0]
        clauses.append('i.id<=?'); values.append(ceiling)
        source=' FROM feed_items i JOIN profiles p ON p.id=i.profile_id WHERE '+' AND '.join(clauses)
        total=conn.execute('SELECT COUNT(*)'+source,values).fetchone()[0]
        result=dict(total_count=total,feed_id=arguments.get('feed_id'),query=query,status=status,
                    source='stored_items',filters=filters,**period)
        if count:
            result['snapshot_id']=ceiling
            return result
        orders=dict(recent='i.discovered_at DESC,i.id DESC',oldest='i.discovered_at,i.id',new='(i.seen_at IS NULL) DESC,i.discovered_at DESC,i.id DESC',title='i.title COLLATE NOCASE,i.id DESC',priority='p.priority DESC,i.discovered_at DESC,i.id DESC')
        limit=arguments.get('limit',25);offset=arguments.get('offset',0)
        rows=conn.execute('SELECT i.*,p.feed_title'+source+' ORDER BY '+orders[arguments.get('sort','recent')]+' LIMIT ? OFFSET ?',[*values,limit,offset]).fetchall()
    result.update(returned_count=len(rows),truncated=total>len(rows),has_more=offset+len(rows)<total,offset=offset,limit=limit,snapshot_id=ceiling,
                  items=[dict(id=r['id'],feed_id=r['profile_id'],title=r['title'],summary=r['summary'][:500],summary_truncated=len(r['summary'])>500,
                              feed_title=r['feed_title'],seen=bool(r['seen_at']),saved=bool(r['saved_at']),discovered_at=r['discovered_at'],updated_at=r['updated_at'],
                              url=f"/profiles/{r['profile_id']}?item={r['id']}") for r in rows])
    result['next_arguments']=dict(filters,limit=limit,offset=offset+len(rows),snapshot_id=ceiling) if result['has_more'] else None
    return result
