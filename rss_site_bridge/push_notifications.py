"""Device-scoped Web Push preferences and a durable, rate-limited digest queue."""
from contextlib import closing
from datetime import datetime
from pathlib import Path
from threading import Event, Thread
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import base64
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import time

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from flask import Blueprint, g, jsonify, request, url_for
from itsdangerous import URLSafeTimedSerializer, BadSignature
from pywebpush import webpush, WebPushException
import requests

from .downloaders import encryption_key

DEFAULTS = dict(new=True, updated=False, failures=False, feeds=[], interval=15,
                daily_limit=12, quiet=False, quiet_start='22:00', quiet_end='08:00', timezone='UTC')
log = logging.getLogger('nightfeed.push')


def connect(db):
    conn = sqlite3.connect(db, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def initialize(db):
    with closing(connect(db)) as conn:
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS push_config(id INTEGER PRIMARY KEY, private_key TEXT NOT NULL, public_key TEXT NOT NULL, contact TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS push_devices(
          id TEXT PRIMARY KEY, secret_hash TEXT NOT NULL, endpoint_hash TEXT UNIQUE NOT NULL,
          subscription TEXT NOT NULL, preferences TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
          due_at REAL, lease_until REAL NOT NULL DEFAULT 0, day TEXT NOT NULL DEFAULT '', sent INTEGER NOT NULL DEFAULT 0,
          retries INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '', test_at REAL NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS push_events(
          id INTEGER PRIMARY KEY, device_id TEXT NOT NULL, notification_id INTEGER NOT NULL,
          feed_id INTEGER NOT NULL, feed_title TEXT NOT NULL, new_count INTEGER NOT NULL, updated_count INTEGER NOT NULL,
          failed INTEGER NOT NULL, created_at REAL NOT NULL, UNIQUE(device_id,notification_id));
        CREATE INDEX IF NOT EXISTS push_events_device ON push_events(device_id,created_at);
        CREATE TABLE IF NOT EXISTS push_failures(device_id TEXT NOT NULL, feed_id INTEGER NOT NULL, PRIMARY KEY(device_id,feed_id));
        ''')
        from .app import ensure_column
        ensure_column(conn, 'push_devices', 'user_id', 'INTEGER')
        claim_legacy_devices(conn)
        conn.commit()
        conn.execute('BEGIN IMMEDIATE')
        if not conn.execute('SELECT id FROM push_config WHERE id=1').fetchone():
            key = ec.generate_private_key(ec.SECP256R1())
            private = key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
            public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
            conn.execute('INSERT INTO push_config VALUES(1,?,?,?)',
                         (Fernet(encryption_key(db)).encrypt(private).decode(), base64.urlsafe_b64encode(public).decode().rstrip('='), ''))
        conn.commit()


def claim_legacy_devices(conn):
    """Pre-auth subscriptions belong to the sole Nightfeed owner on upgrade."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='auth_users'").fetchone():
        users = conn.execute('SELECT id FROM auth_users LIMIT 2').fetchall()
        if len(users) == 1:
            conn.execute('UPDATE push_devices SET user_id=? WHERE user_id IS NULL', (users[0]['id'],))


def preferences(value):
    if not isinstance(value, dict):
        raise ValueError('Invalid notification preferences.')
    result = {**DEFAULTS, **{key: value[key] for key in DEFAULTS if key in value}}
    if any(type(result[key]) is not bool for key in ('new', 'updated', 'failures', 'quiet')):
        raise ValueError('Choose valid notification types.')
    if type(result['interval']) is not int or result['interval'] not in (5, 15, 60):
        raise ValueError('Choose a 5, 15, or 60 minute summary interval.')
    if type(result['daily_limit']) is not int or result['daily_limit'] not in (1, 3, 6, 12, 24):
        raise ValueError('Choose a valid daily notification limit.')
    feeds = result['feeds']
    if not isinstance(feeds, list) or len(feeds) > 1000 or any(type(item) is not int or not 0 < item < 2**63 for item in feeds):
        raise ValueError('Invalid feed selection.')
    result['feeds'] = list(dict.fromkeys(feeds))
    try:
        if not isinstance(result['timezone'], str) or len(result['timezone']) > 100:
            raise ValueError()
        ZoneInfo(result['timezone'])
        for key in ('quiet_start', 'quiet_end'):
            datetime.strptime(result[key], '%H:%M')
            if len(result[key]) != 5:
                raise ValueError()
    except (ValueError, TypeError, OSError, ZoneInfoNotFoundError):
        raise ValueError('Choose valid quiet hours and a device timezone.')
    if result['quiet'] and result['quiet_start'] == result['quiet_end']:
        raise ValueError('Quiet hours must have different start and end times.')
    return result


def subscription(value):
    if not isinstance(value, dict):
        raise ValueError('Invalid push subscription.')
    endpoint = value.get('endpoint', '')
    if not isinstance(endpoint, str) or len(endpoint) > 4096:
        raise ValueError('Invalid push endpoint.')
    parsed = urlsplit(endpoint)
    host = parsed.hostname or ''
    # Never turn a submitted subscription into a request to an arbitrary server.
    allowed = host in ('fcm.googleapis.com', 'updates.push.services.mozilla.com') or host.endswith('.push.apple.com')
    if not allowed or parsed.scheme != 'https' or parsed.port not in (None, 443) or parsed.username or parsed.password or parsed.fragment:
        raise ValueError('Unsupported push service. Use Safari, Chrome, or Firefox.')
    keys = value.get('keys')
    if not isinstance(keys, dict):
        raise ValueError('Invalid subscription keys.')
    try:
        if any(not isinstance(keys.get(key), str) or len(keys[key]) > 100 for key in ('p256dh', 'auth')):
            raise ValueError()
        decoded = {key: base64.b64decode(keys[key] + '=' * (-len(keys[key]) % 4), altchars=b'-_', validate=True) for key in ('p256dh', 'auth')}
        if len(decoded['auth']) != 16 or len(decoded['p256dh']) != 65:
            raise ValueError()
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), decoded['p256dh'])
    except (ValueError, KeyError, TypeError):
        raise ValueError('Invalid subscription keys.')
    return {'endpoint': endpoint, 'keys': {key: keys[key] for key in ('p256dh', 'auth')}}


class PushSession(requests.Session):
    def request(self, *args, **kwargs):
        kwargs['allow_redirects'] = False
        return super().request(*args, **kwargs)


def deliver(db, device, payload):
    with closing(connect(db)) as conn:
        config = conn.execute('SELECT * FROM push_config WHERE id=1').fetchone()
    private = Fernet(encryption_key(db)).decrypt(config['private_key'].encode())
    with PushSession() as session:
        response = webpush(subscription_info=json.loads(device['subscription']), data=json.dumps(payload),
                           vapid_private_key=base64.urlsafe_b64encode(private).decode().rstrip('='),
                           vapid_claims={'sub': config['contact']}, ttl=86400, timeout=10, requests_session=session)
        if not 200 <= response.status_code < 300:
            raise WebPushException('Push service rejected the request.', response=response)


def enqueue(db, profile, notification, status, new_items, updated_items, now=None):
    now = time.time() if now is None else now
    with closing(connect(db)) as conn:
        if status == 'ok':
            conn.execute('DELETE FROM push_failures WHERE feed_id=?', (profile.id,))
            conn.execute('DELETE FROM push_events WHERE feed_id=? AND failed=1', (profile.id,))
        for device in conn.execute('SELECT * FROM push_devices WHERE enabled=1').fetchall():
            prefs = json.loads(device['preferences'])
            if prefs['feeds'] and profile.id not in prefs['feeds']:
                continue
            new = new_items if prefs['new'] else 0
            updated = updated_items if prefs['updated'] else 0
            failed = status != 'ok' and prefs['failures']
            if failed:
                if conn.execute('SELECT 1 FROM push_failures WHERE device_id=? AND feed_id=?', (device['id'], profile.id)).fetchone():
                    continue
                conn.execute('INSERT INTO push_failures VALUES(?,?)', (device['id'], profile.id))
            if not failed and not new and not updated:
                continue
            conn.execute('INSERT OR IGNORE INTO push_events(device_id,notification_id,feed_id,feed_title,new_count,updated_count,failed,created_at) VALUES(?,?,?,?,?,?,?,?)',
                         (device['id'], notification.id, profile.id, profile.feed_title, new, updated, int(failed), now))
            conn.execute('UPDATE push_devices SET due_at=COALESCE(due_at, ?) WHERE id=?', (now + prefs['interval'] * 60, device['id']))
        conn.commit()


def dispatch(db, now=None):
    now = time.time() if now is None else now
    with closing(connect(db)) as conn:
        conn.execute('DELETE FROM push_events WHERE created_at < ?', (now - 86400,))
        conn.execute('UPDATE push_devices SET due_at=NULL WHERE NOT EXISTS(SELECT 1 FROM push_events WHERE device_id=push_devices.id)')
        devices = conn.execute('SELECT * FROM push_devices WHERE enabled=1 AND due_at<=? AND lease_until<=? LIMIT 100', (now, now)).fetchall()
        conn.commit()
    for device in devices:
        prefs = json.loads(device['preferences'])
        local = datetime.fromtimestamp(now, ZoneInfo(prefs['timezone']))
        hour = local.strftime('%H:%M')
        start, end = prefs['quiet_start'], prefs['quiet_end']
        quiet = prefs['quiet'] and (start <= hour < end if start < end else hour >= start or hour < end)
        day = local.date().isoformat()
        if quiet or (device['day'] == day and device['sent'] >= prefs['daily_limit']):
            continue
        with closing(connect(db)) as conn:
            conn.execute('BEGIN IMMEDIATE')
            # Task alerts share the device's delivery lease and daily allowance.
            device = conn.execute('SELECT * FROM push_devices WHERE id=?', (device['id'],)).fetchone()
            if not device or (device['day'] == day and device['sent'] >= prefs['daily_limit']):
                continue
            if not conn.execute('UPDATE push_devices SET lease_until=? WHERE id=? AND enabled=1 AND lease_until<=? AND due_at<=?', (now + 120, device['id'], now, now)).rowcount:
                conn.rollback(); continue
            events = conn.execute('SELECT * FROM push_events WHERE device_id=? ORDER BY id', (device['id'],)).fetchall()
            conn.commit()
        if not events:
            with closing(connect(db)) as conn:
                conn.execute('UPDATE push_devices SET lease_until=0 WHERE id=?', (device['id'],)); conn.commit()
            continue
        # Device opt-in permits these summaries while signed out. Full reports
        # remain behind the application authentication gate.
        feeds = list(dict.fromkeys(event['feed_title'] for event in events))
        new = sum(event['new_count'] for event in events)
        updated = sum(event['updated_count'] for event in events)
        failures = len({event['feed_id'] for event in events if event['failed']})
        parts = ([f'{new} new topics'] if new else []) + ([f'{updated} updated topics'] if updated else []) + ([f'{failures} feed failures'] if failures else [])
        payload = {'title': 'Nightfeed · ' + (feeds[0][:160] if len(feeds) == 1 else f'{len(feeds)} feeds'),
                   'body': ', '.join(parts), 'url': f'/notifications/{events[-1]["notification_id"]}' if len(events) == 1 else '/notifications',
                   # Distinct batches alert again; retrying a batch replaces only itself.
                   'tag': 'nightfeed-summary-' + hashlib.sha256(json.dumps([
                       device['id'], events[-1]['notification_id'], events[-1]['created_at'], new, updated, failures
                   ]).encode()).hexdigest()[:24]}
        try:
            deliver(db, device, payload)
        except Exception as exc:
            code = getattr(getattr(exc, 'response', None), 'status_code', None)
            retries = device['retries'] + 1
            with closing(connect(db)) as conn:
                # Only a provider-confirmed expired subscription needs re-registration.
                # Network outages and provider errors must not revoke device opt-in.
                disabled = code in (404, 410)
                conn.execute('UPDATE push_devices SET enabled=?, retries=?, last_error=?, due_at=?, lease_until=0 WHERE id=?',
                             (int(not disabled), retries, 'Subscription expired. Enable notifications again.' if disabled else 'Push delivery temporarily failed. Nightfeed will retry automatically.', now + min(3600, 300 * 2**min(retries, 4)), device['id']))
                if disabled:
                    conn.execute('DELETE FROM push_events WHERE device_id=?', (device['id'],))
                conn.commit()
            log.warning('Push delivery failed (status=%s); subscription payload omitted', code)
        else:
            with closing(connect(db)) as conn:
                conn.execute('DELETE FROM push_events WHERE device_id=? AND id<=?', (device['id'], events[-1]['id']))
                conn.execute('UPDATE push_devices SET sent=?, day=?, retries=0, last_error=\'\', lease_until=0, due_at=? WHERE id=?',
                             ((device['sent'] if device['day'] == day else 0) + 1, day, now + prefs['interval'] * 60, device['id']))
                conn.commit()
            log.info('Push summary accepted by provider (new=%s, updated=%s, failures=%s); subscription payload omitted', new, updated, failures)


def register(app):
    db = Path(app.config['DATABASE_PATH'])
    signer = URLSafeTimedSerializer(encryption_key(db), salt='nightfeed-push')
    bp = Blueprint('push', __name__)

    @app.context_processor
    def context():
        return {'push_csrf': signer.dumps('push')}

    @bp.before_request
    def protect():
        from .auth import same_origin
        if request.method == 'POST':
            if request.content_length is None or request.content_length > 16384:
                return jsonify(error='Invalid notification request size.'), 413
            try:
                if signer.loads(request.headers.get('X-CSRF-Token', ''), max_age=86400) != 'push':
                    raise BadSignature('Invalid token')
            except BadSignature:
                return jsonify(error='Reload Settings and try again.'), 403
            if not same_origin():
                return jsonify(error='Cross-origin requests are not allowed.'), 403

    @bp.after_request
    def no_store(response):
        if request.path.startswith('/api/push/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @bp.errorhandler(ValueError)
    def invalid(exc):
        return jsonify(error=str(exc)), 400

    def device(payload):
        if not isinstance(payload, dict):
            raise ValueError('Invalid device request.')
        token = payload.get('device_token', '')
        if not isinstance(token, str) or len(token) > 200:
            raise ValueError('Invalid device token.')
        with closing(connect(db)) as conn:
            row = conn.execute('SELECT * FROM push_devices WHERE secret_hash=?', (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if not row or row['user_id'] != g.auth_user['id']:
            raise ValueError('This device is not registered. Enable notifications again.')
        return row

    @bp.get('/api/push/config')
    def config():
        with closing(connect(db)) as conn:
            key = conn.execute('SELECT public_key FROM push_config WHERE id=1').fetchone()[0]
            feeds = [dict(row) for row in conn.execute('SELECT id,feed_title FROM profiles ORDER BY feed_title COLLATE NOCASE')]
        return jsonify(public_key=key, defaults=DEFAULTS, feeds=feeds)

    @bp.post('/api/push/subscribe')
    def subscribe():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise ValueError('Invalid subscription request.')
        sub, prefs = subscription(payload.get('subscription')), preferences(payload.get('preferences', {}))
        endpoint_hash = hashlib.sha256(sub['endpoint'].encode()).hexdigest()
        token = secrets.token_urlsafe(32)
        with closing(connect(db)) as conn:
            conn.execute('BEGIN IMMEDIATE')
            existing = conn.execute('SELECT * FROM push_devices WHERE endpoint_hash=?', (endpoint_hash,)).fetchone()
            if existing and json.loads(existing['subscription'])['keys'] != sub['keys']:
                raise ValueError('Subscription keys do not match this device.')
            identity = existing['id'] if existing else secrets.token_urlsafe(24)
            old_token = payload.get('device_token')
            if not existing and isinstance(old_token, str) and len(old_token) <= 200:
                existing = conn.execute('SELECT * FROM push_devices WHERE secret_hash=?', (hashlib.sha256(old_token.encode()).hexdigest(),)).fetchone()
                if existing:
                    identity = existing['id']
            if existing:
                if existing['user_id'] != g.auth_user['id']:
                    raise ValueError('This subscription belongs to another account.')
                conn.execute('DELETE FROM push_events WHERE device_id=?', (identity,))
                conn.execute('DELETE FROM push_failures WHERE device_id=?', (identity,))
                conn.execute('DELETE FROM push_devices WHERE id=?', (identity,))
            conn.execute('INSERT INTO push_devices(id,secret_hash,endpoint_hash,subscription,preferences,day,sent,test_at,user_id) VALUES(?,?,?,?,?,?,?,?,?)',
                         (identity, hashlib.sha256(token.encode()).hexdigest(), endpoint_hash, json.dumps(sub), json.dumps(prefs),
                          existing['day'] if existing else '', existing['sent'] if existing else 0, existing['test_at'] if existing else 0, g.auth_user['id']))
            contact = os.environ.get('NIGHTFEED_PUSH_CONTACT') or ('https://' + urlsplit(request.host_url).hostname)
            conn.execute('UPDATE push_config SET contact=? WHERE id=1', (contact,))
            conn.commit()
        return jsonify(device_token=token, preferences=prefs, enabled=True)

    @bp.post('/api/push/device')
    def device_status():
        row = device(request.get_json(silent=True))
        return jsonify(preferences=json.loads(row['preferences']), enabled=bool(row['enabled']), error=row['last_error'])

    @bp.post('/api/push/preferences')
    def save():
        payload = request.get_json(silent=True)
        row = device(payload)
        prefs = preferences(payload.get('preferences'))
        with closing(connect(db)) as conn:
            conn.execute('UPDATE push_devices SET preferences=? WHERE id=?', (json.dumps(prefs), row['id']))
            conn.execute('DELETE FROM push_events WHERE device_id=?', (row['id'],))
            conn.commit()
        return jsonify(preferences=prefs)

    @bp.post('/api/push/disable')
    def disable():
        row = device(request.get_json(silent=True))
        with closing(connect(db)) as conn:
            conn.execute('UPDATE push_devices SET enabled=0,due_at=NULL WHERE id=?', (row['id'],))
            for table in ('push_events', 'push_failures'):
                conn.execute(f'DELETE FROM {table} WHERE device_id=?', (row['id'],))
            conn.commit()
        return jsonify(disabled=True)

    @bp.post('/api/push/test')
    def test():
        row = device(request.get_json(silent=True))
        now = time.time()
        with closing(connect(db)) as conn:
            if not conn.execute('UPDATE push_devices SET test_at=? WHERE id=? AND test_at<=?', (now, row['id'], now - 60)).rowcount:
                return jsonify(error='Wait one minute before sending another test.'), 429
            conn.commit()
        try:
            deliver(db, row, dict(title='Nightfeed', body='Notifications are ready on this device.', url='/settings', tag=f'nightfeed-test-{int(now)}'))
        except Exception:
            return jsonify(error='The test could not be delivered. Check server connectivity and device permissions.'), 502
        return jsonify(message='Test sent. Check your device notifications.')

    @bp.get('/manifest.webmanifest')
    def manifest():
        return jsonify(id='/', name='Nightfeed', short_name='Nightfeed', start_url='/', scope='/', display='standalone',
                       background_color='#1B2739', theme_color='#1B2739', icons=[{'src': url_for('static', filename=f'nightfeed-{size}.png'), 'sizes': f'{size}x{size}', 'type': 'image/png'} for size in (192,512)])

    @bp.get('/service-worker.js')
    def worker():
        response = app.send_static_file('nightfeed-worker.js')
        response.headers['Cache-Control'] = 'no-cache'
        response.headers['Service-Worker-Allowed'] = '/'
        return response

    app.register_blueprint(bp)
    if not app.config.get('TESTING') and app.config.get('START_PUSH_WORKER', True):
        stop = Event()
        def loop():
            while not stop.is_set():
                try:
                    dispatch(db)
                    from .tasks import dispatch as dispatch_tasks
                    dispatch_tasks(db)
                except Exception:
                    log.exception('Push queue sweep failed')
                stop.wait(20)
        thread = Thread(target=loop, daemon=True, name='nightfeed-push')
        app.config.update(PUSH_STOP_EVENT=stop, PUSH_THREAD=thread)
        thread.start()
