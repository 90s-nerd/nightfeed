"""Persistent, credential-redacted assistant audit records."""
from contextlib import closing
import json
import re
import time
from . import app as core


def redact(value):
    if isinstance(value, dict):
        return {k: ('[redacted]' if re.search(r'password|secret|api_key|authorization|encrypted_content|signature|untrusted_html|(?:^|_)(?:token|key)$', k, re.I) else redact(v)) for k, v in value.items() if not k.startswith('_')}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value[:100]]
    if isinstance(value, str):
        value = re.sub(r'data:image/[^;,\s]+;base64,[A-Za-z0-9+/=]+', '[image data omitted]', value, flags=re.I)
        value = re.sub(r'\b(?:sk-|nf_)[A-Za-z0-9_-]{12,}', '[redacted]', value)
        value = re.sub(r'(?i)(bearer\s+)[^\s"<>]+', r'\1[redacted]', value)
        value = re.sub(r'(?i)([?&](?:key|token|access_token|api_key|password|secret|auth)=)[^&#\s]+', r'\1[redacted]', value)
        value = re.sub(r'(?i)(https?://)[^/@\s]+:[^/@\s]+@', r'\1[redacted]@', value)
        value = re.sub(r'(?i)((?:api[_ -]?key|password|secret|access[_ -]?token)\s*[:=]\s*)[^\s,;"<>]+', r'\1[redacted]', value)
        return value[:12000]
    return value


def record(db, principal, kind, *, conversation=None, status='ok', **details):
    with closing(core.connect_db(db)) as conn:
        conn.execute('INSERT INTO assistant_audit(principal,conversation,kind,status,created,details) VALUES(?,?,?,?,?,?)',
                     (principal, conversation, kind, status, time.time(), json.dumps(redact(details))))
        conn.commit()


def grouped_page(db, page, kind, conversation=None):
    """Page conversations independently; a selected conversation pages events."""
    with closing(core.connect_db(db)) as conn:
        if conversation is None:
            heads=conn.execute('''SELECT COALESCE(conversation,'') AS conversation, COUNT(*) AS event_count,
                MAX(created) AS latest, SUM(status='error') AS errors FROM assistant_audit
                WHERE (?='' OR kind=?) GROUP BY COALESCE(conversation,'') ORDER BY MAX(id) DESC LIMIT 21 OFFSET ?''',
                (kind,kind,(page-1)*20)).fetchall()
            more=len(heads)>20;heads=heads[:20]
        else:
            heads=conn.execute('''SELECT COALESCE(conversation,'') AS conversation,COUNT(*) AS event_count,
                MAX(created) AS latest,SUM(status='error') AS errors FROM assistant_audit
                WHERE COALESCE(conversation,'')=? AND (?='' OR kind=?) GROUP BY COALESCE(conversation,'')''',(conversation,kind,kind)).fetchall()
            more=False
        groups=[];events=[]
        for head in heads:
            key=head['conversation']
            rows=conn.execute("SELECT * FROM assistant_audit WHERE COALESCE(conversation,'')=? AND (?='' OR kind=?) ORDER BY id DESC LIMIT ? OFFSET ?",
                              (key,kind,kind,101 if conversation is not None else 5,(page-1)*100 if conversation is not None else 0)).fetchall()
            if conversation is not None: more=len(rows)>100;rows=rows[:100]
            entries=[dict(row,details=json.loads(row['details'])) for row in reversed(rows)]
            saved=conn.execute('SELECT title FROM assistant_conversations WHERE id=?',(key,)).fetchone() if key else None
            first=conn.execute("SELECT details FROM assistant_audit WHERE conversation=? AND kind='user_message' ORDER BY id LIMIT 1",(key,)).fetchone() if key else None
            title=saved['title'] if saved else (json.loads(first['details']).get('message') or 'Deleted conversation')[:100] if first else 'Deleted conversation' if key else 'Outside conversations'
            title=redact(title)
            totals=dict(conn.execute('''SELECT COUNT(*) AS requests,
                COALESCE(SUM(json_extract(details,'$.usage.available')=1),0) AS measured,
                COALESCE(SUM(CASE WHEN json_extract(details,'$.usage.available')=1 THEN json_extract(details,'$.usage.input_tokens') ELSE 0 END),0) AS input_tokens,
                COALESCE(SUM(CASE WHEN json_extract(details,'$.usage.available')=1 THEN json_extract(details,'$.usage.output_tokens') ELSE 0 END),0) AS output_tokens,
                COALESCE(SUM(json_extract(details,'$.usage.available')=1 AND json_extract(details,'$.usage.estimated_usd') IS NOT NULL),0) AS priced,
                COALESCE(SUM(CASE WHEN json_extract(details,'$.usage.available')=1 THEN json_extract(details,'$.usage.estimated_usd') ELSE 0 END),0) AS estimated_usd
                FROM assistant_audit WHERE COALESCE(conversation,'')=? AND (?='' OR kind=?) AND kind IN ('provider_request','connection_test')''',(key,kind,kind)).fetchone())
            groups.append(dict(head,title=title,deleted=bool(key and not saved),events=entries,usage=totals,partial=len(entries)<head['event_count']))
            events.extend(entries)
    return groups,events,more


def usage(config, raw):
    """Normalize billable categories without inventing unavailable token counts."""
    if not raw:
        return dict(available=False, context_window=config.get('context_window') or None, estimated_usd=None)
    family = config['api_type']
    cached = written = reasoning = None
    if family == 'anthropic':
        cached, written = raw.get('cache_read_input_tokens'), raw.get('cache_creation_input_tokens')
        incoming = raw.get('input_tokens', 0) + (cached or 0) + (written or 0)
        outgoing = raw.get('output_tokens', 0)
    elif family == 'gemini':
        incoming, reasoning = raw.get('promptTokenCount', 0), raw.get('thoughtsTokenCount')
        outgoing = raw.get('candidatesTokenCount', 0) + (reasoning or 0)
        cached = raw.get('cachedContentTokenCount')
    else:
        incoming = raw.get('input_tokens', raw.get('prompt_tokens'))
        outgoing = raw.get('output_tokens', raw.get('completion_tokens'))
        cached = (raw.get('input_tokens_details') or raw.get('prompt_tokens_details') or {}).get('cached_tokens')
        reasoning = (raw.get('output_tokens_details') or raw.get('completion_tokens_details') or {}).get('reasoning_tokens')
    available = isinstance(incoming, int) and isinstance(outgoing, int)
    estimate = None
    if available:
        categories = [(max(0, incoming - (cached or 0) - (written or 0)), 'input_price'), (outgoing, 'output_price'), (cached or 0, 'cache_price'), (written or 0, 'cache_write_price')]
        if all(not count or config.get(rate) is not None for count, rate in categories):
            estimate = sum(count * float(config.get(rate) or 0) for count, rate in categories) / 1_000_000
    return dict(available=available, input_tokens=incoming, output_tokens=outgoing, cached_tokens=cached,
                cache_write_tokens=written, reasoning_tokens=reasoning, context_window=config.get('context_window') or None,
                estimated_usd=estimate, pricing='Configured USD per million tokens; input without cache breakdown uses standard input price. Estimate excludes non-token fees.')
