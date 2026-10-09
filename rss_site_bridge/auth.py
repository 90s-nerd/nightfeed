"""Default-deny authentication. Users, identities and revocable sessions are separate.

The single owner is intentional; resource authorization must be added before allowing
additional users. Flask's signed cookie contains only transient CSRF/OIDC state.
"""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit
import argparse
import getpass
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from functools import lru_cache

from cryptography.fernet import Fernet
from flask import Blueprint, abort, current_app, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

from .downloaders import connect, encryption_key

COOKIE = 'nightfeed_auth'
PASSWORD_METHOD = 'scrypt:131072:8:1'
DEFAULTS = dict(session_minutes=720, idle_minutes=30, lockout_attempts=5,
                lockout_minutes=15, auto_unlock=True, lockout_exclusions='',
                oidc_enabled=False, oidc_use_name=False, auto_login=False, disable_form=False,
                button_text='Sign in with SSO', discovery=True, issuer='',
                client_id='', client_secret='', subject='', redirect_uri='',
                authorization_endpoint='', token_endpoint='', jwks_uri='', logout_endpoint='',
                trusted_proxies='', proxy_hops=0)
SCOPES = {'rss:read': 'Read RSS XML', 'feeds:read': 'Read feed list through API',
          'topics:read': 'Read topics through API', 'notifications:read': 'Read notifications through API',
          'feeds:refresh': 'Refresh feeds through API'}
PUBLIC = {'auth.login', 'auth.setup', 'auth.oidc_start', 'auth.oidc_callback',
          'static', 'push.manifest', 'push.worker'}


def fingerprint(value):
    return hashlib.sha256(value.encode()).hexdigest()


def user_capabilities(user):
    # Nightfeed currently has one owner. Additional roles need resource policy
    # before being enabled; unknown roles receive no application capabilities.
    return ('app:read', 'app:write', 'app:settings', 'feeds:refresh') if user and user['role'] == 'owner' else ()


def revoke_application_grants(conn, user_id):
    # Offline recovery also operates on databases created before OAuth existed.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='auth_oauth_grants'").fetchone():
        conn.execute('UPDATE auth_oauth_grants SET revoked=1 WHERE user_id=?', (user_id,))


def equal(left, right):
    return hmac.compare_digest(left.encode('utf-8'), right.encode('utf-8'))


def same_origin():
    origin = request.headers.get('Origin')
    allowed = {request.host_url.rstrip('/')}
    # Secure cookies require HTTPS at the browser. This also permits initial proxy
    # configuration without trusting any forwarded client IP or host headers.
    if current_app.config.get('SESSION_COOKIE_SECURE'):
        allowed.add('https://' + request.host)
    return not origin or origin in allowed


def initialize(db):
    with closing(connect(db)) as conn:
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS auth_users (
          id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
          name TEXT NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'owner'
        );
        CREATE TABLE IF NOT EXISTS auth_identities (
          issuer TEXT NOT NULL, subject TEXT NOT NULL,
          user_id INTEGER NOT NULL REFERENCES auth_users(id) ON DELETE CASCADE,
          PRIMARY KEY(issuer,subject)
        );
        CREATE TABLE IF NOT EXISTS auth_sessions (
          token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES auth_users(id) ON DELETE CASCADE,
          created REAL NOT NULL, expires REAL NOT NULL, last_seen REAL NOT NULL, method TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS auth_config (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS auth_failures (
          key TEXT PRIMARY KEY, attempts INTEGER NOT NULL, started REAL NOT NULL, locked_until REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS auth_audit (
          id INTEGER PRIMARY KEY, at REAL NOT NULL, event TEXT NOT NULL, user_id INTEGER, ip TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS auth_api_keys (
          id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES auth_users(id) ON DELETE CASCADE,
          name TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL, prefix TEXT NOT NULL,
          scopes TEXT NOT NULL, feed_ids TEXT NOT NULL, created REAL NOT NULL,
          expires REAL, last_used REAL, revoked INTEGER NOT NULL DEFAULT 0
        );
        ''')
        if 'oidc_name' not in {row['name'] for row in conn.execute('PRAGMA table_info(auth_users)')}:
            conn.execute("ALTER TABLE auth_users ADD COLUMN oidc_name TEXT NOT NULL DEFAULT ''")
        conn.execute('INSERT OR IGNORE INTO auth_config VALUES(1,?)', (json.dumps(DEFAULTS),))
        # Remove obsolete MCP permissions without disturbing RSS/API access on
        # mixed-use credentials. MCP-only keys no longer have a usable permission.
        for row in conn.execute("SELECT id,scopes FROM auth_api_keys WHERE scopes LIKE '%mcp:%'").fetchall():
            scopes = [scope for scope in json.loads(row['scopes']) if scope not in ('mcp:read', 'mcp:write', 'mcp:settings')]
            conn.execute('UPDATE auth_api_keys SET scopes=?,revoked=CASE WHEN ? THEN revoked ELSE 1 END WHERE id=?',
                         (json.dumps(scopes), bool(scopes), row['id']))
        conn.commit()
    if not owner(db):
        setup_token(db)


def owner(db):
    with closing(connect(db)) as conn:
        row = conn.execute('SELECT * FROM auth_users ORDER BY id LIMIT 1').fetchone()
    return dict(row) if row else None


def settings(db):
    with closing(connect(db)) as conn:
        row = conn.execute('SELECT value FROM auth_config WHERE id=1').fetchone()
    return DEFAULTS | json.loads(row['value'])


def provider_name(claims):
    value = claims.get('name')
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value and len(value) <= 100 and not any(ord(c) < 32 or ord(c) == 127 for c in value) else None


def setup_token(db):
    configured = os.environ.get('NIGHTFEED_SETUP_TOKEN', '')
    if configured:
        if len(configured) < 32:
            raise ValueError('NIGHTFEED_SETUP_TOKEN must contain at least 32 characters.')
        return configured
    path = Path(db).with_suffix('.setup-token')
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return path.read_text().strip()
    value = secrets.token_urlsafe(32)
    with os.fdopen(fd, 'w') as handle:
        handle.write(value)
    logging.getLogger(__name__).warning('Nightfeed owner setup required. Read the setup token from %s on the server.', path)
    return value


def password_hash(value):
    if not isinstance(value, str) or not 15 <= len(value) <= 256:
        raise ValueError('Use a password or passphrase of 15–256 characters.')
    return generate_password_hash(value, method=PASSWORD_METHOD)


def valid_password(user, value, dummy):
    if not isinstance(value, str) or len(value) > 256:
        return False
    valid = check_password_hash(user['password_hash'] if user else dummy, value)
    return bool(user and valid)


@lru_cache(maxsize=1)
def dummy_hash():
    return generate_password_hash(secrets.token_urlsafe(32), method=PASSWORD_METHOD)


def safe_next(value):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 for c in value) or '\\' in value:
        return '/'
    try:
        parsed = urlsplit(value)
    except ValueError:
        return '/'
    return value if value.startswith('/') and not value.startswith('//') and not parsed.scheme and not parsed.netloc else '/'


def https_url(value, label, required=False):
    value = value.strip()
    if not value and not required:
        return ''
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError:
        raise ValueError(f'Invalid {label}.')
    if (len(value) > 2048 or parsed.scheme != 'https' or not parsed.hostname
            or parsed.username or parsed.password or parsed.fragment
            or any(ord(c) < 33 for c in value)):
        raise ValueError(f'{label} must be an HTTPS URL without credentials or fragments.')
    return value


def parse_settings(form, existing, key):
    result = dict(existing)
    for name, low, high in [('session_minutes', 5, 43200), ('idle_minutes', 1, 1440),
                            ('lockout_attempts', 3, 100), ('lockout_minutes', 1, 10080), ('proxy_hops', 0, 5)]:
        try:
            value = int(form.get(name, ''))
        except (ValueError, TypeError):
            raise ValueError(f'Enter a valid {name.replace("_", " ")}.')
        if not low <= value <= high:
            raise ValueError(f'{name.replace("_", " ").capitalize()} must be between {low} and {high}.')
        result[name] = value
    for name in ('auto_unlock', 'oidc_enabled', 'auto_login', 'disable_form', 'discovery'):
        result[name] = form.get(name) == 'on'
    exclusions = str(form.get('lockout_exclusions', '')).strip()
    if len(exclusions) > 2048:
        raise ValueError('Too many lockout exclusions.')
    try:
        result['lockout_exclusions'] = ', '.join(str(ipaddress.ip_network(v.strip(), strict=False)) for v in exclusions.split(',') if v.strip())
    except ValueError:
        raise ValueError('Lockout exclusions must be comma-separated IP addresses or CIDR networks.')
    proxies = str(form.get('trusted_proxies', '')).strip()
    if len(proxies) > 2048:
        raise ValueError('Too many trusted proxies.')
    try:
        result['trusted_proxies'] = ', '.join(str(ipaddress.ip_network(v.strip(), strict=False)) for v in proxies.split(',') if v.strip())
    except ValueError:
        raise ValueError('Trusted proxies must be IP addresses or CIDR networks.')
    if result['proxy_hops'] and not result['trusted_proxies']:
        raise ValueError('Configure trusted proxy addresses before enabling forwarded headers.')
    for name in ('client_id', 'subject', 'button_text'):
        value = str(form.get(name, '')).strip()
        if len(value) > 255 or any(ord(c) < 32 for c in value):
            raise ValueError(f'Invalid {name.replace("_", " ")}.')
        result[name] = value
    result['button_text'] = result['button_text'] or DEFAULTS['button_text']
    for name in ('issuer', 'authorization_endpoint', 'token_endpoint', 'jwks_uri', 'logout_endpoint', 'redirect_uri'):
        result[name] = https_url(str(form.get(name, '')), name.replace('_', ' ').capitalize())
    secret = str(form.get('client_secret', ''))
    if len(secret) > 4096:
        raise ValueError('Client secret is too long.')
    if secret:
        result['client_secret'] = Fernet(key).encrypt(secret.encode()).decode()
    if result['oidc_enabled']:
        if not all(result[name] for name in ('issuer', 'client_id', 'client_secret', 'redirect_uri')):
            raise ValueError('OIDC requires issuer, client ID, client secret and callback URL.')
        if not result['subject'] and (result['auto_login'] or result['disable_form']):
            raise ValueError('Link the owner SSO identity before enabling auto login or disabling the local form.')
        if urlsplit(result['issuer']).query:
            raise ValueError('OIDC issuer must not have a query string.')
        if not result['discovery'] and not all(result[name] for name in ('authorization_endpoint', 'token_endpoint', 'jwks_uri')):
            raise ValueError('Manual OIDC requires authorization, token and JWKS endpoints.')
    else:
        result['disable_form'] = result['auto_login'] = False
    return result


def audit(conn, event, user_id=None, ip=None):
    conn.execute('INSERT INTO auth_audit(at,event,user_id,ip) VALUES(?,?,?,?)',
                 (time.time(), event, user_id, ip or request.remote_addr or 'unknown'))
    conn.execute('DELETE FROM auth_audit WHERE id <= (SELECT COALESCE(MAX(id),0)-1000 FROM auth_audit)')


def exempt(cfg, ip):
    try:
        address = ipaddress.ip_address(ip)
        return any(address in ipaddress.ip_network(v.strip()) for v in cfg['lockout_exclusions'].split(',') if v.strip())
    except ValueError:
        return False


def failure_keys(user, ip):
    # IP bucket bounds username spraying; account bucket bounds distributed attacks.
    return ['ip:' + ip] + (['user:' + str(user['id'])] if user else [])


def locked(conn, keys, now):
    for key in keys:
        row = conn.execute('SELECT locked_until FROM auth_failures WHERE key=?', (key,)).fetchone()
        if row and (row['locked_until'] == -1 or row['locked_until'] > now):
            return True
    return False


def record_failure(conn, keys, cfg, now):
    for key in keys:
        row = conn.execute('SELECT * FROM auth_failures WHERE key=?', (key,)).fetchone()
        count = (row['attempts'] if row and row['started'] > now - cfg['lockout_minutes'] * 60 else 0) + 1
        until = (now + cfg['lockout_minutes'] * 60 if cfg['auto_unlock'] else -1) if count >= cfg['lockout_attempts'] else 0
        conn.execute('INSERT OR REPLACE INTO auth_failures VALUES(?,?,?,?)', (key, count, row['started'] if row and count > 1 else now, until))
    conn.execute('DELETE FROM auth_failures WHERE locked_until>=0 AND locked_until<? AND started<?', (now, now - 604800))


def register(app):
    db = Path(app.config['DATABASE_PATH'])
    initialize(db)
    key = encryption_key(db)
    secure = os.environ.get('NIGHTFEED_SECURE_COOKIES', '1').lower() not in ('0', 'false', 'no')
    app.config.update(SECRET_KEY=hmac.new(key, b'nightfeed-transient-session', 'sha256').hexdigest(),
                      SESSION_COOKIE_NAME='nightfeed_state', SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SECURE=secure, SESSION_COOKIE_SAMESITE='Lax',
                      MAX_CONTENT_LENGTH=4 * 1024 * 1024)
    force_form = os.environ.get('NIGHTFEED_FORCE_LOGIN_FORM', '0').lower() in ('1', 'true', 'yes')
    dummy = dummy_hash()
    bp = Blueprint('auth', __name__)
    inner_app = app.wsgi_app

    def proxy_app(environ, start_response):
        cfg = settings(db)
        networks = os.environ.get('NIGHTFEED_TRUSTED_PROXIES', cfg['trusted_proxies'])
        hops = int(os.environ.get('NIGHTFEED_TRUSTED_PROXY_HOPS', cfg['proxy_hops']))
        trusted = exempt({'lockout_exclusions': networks}, environ.get('REMOTE_ADDR', ''))
        if hops > 0 and trusted:
            return ProxyFix(inner_app, x_for=hops, x_proto=hops)(environ, start_response)
        return inner_app(environ, start_response)

    app.wsgi_app = proxy_app
    app.jinja_env.filters['auth_datetime'] = lambda value: datetime.fromtimestamp(value, timezone.utc).strftime('%Y-%m-%d %H:%M:%S') if value else 'Never'

    def csrf():
        if 'csrf' not in session:
            session['csrf'] = secrets.token_urlsafe(32)
        return session['csrf']

    def revoke(conn):
        value = request.cookies.get(COOKIE, '')
        if value:
            conn.execute('DELETE FROM auth_sessions WHERE token_hash=?', (fingerprint(value),))

    def sign_in(user, method, destination='/', expected_config=None, oidc_name=None):
        token, now = secrets.token_urlsafe(32), time.time()
        cfg = settings(db)
        with closing(connect(db)) as conn:
            conn.execute('BEGIN IMMEDIATE')
            current = conn.execute('SELECT password_hash FROM auth_users WHERE id=?', (user['id'],)).fetchone()
            if not current or not equal(current['password_hash'], user['password_hash']):
                abort(401)
            current_config = DEFAULTS | json.loads(conn.execute('SELECT value FROM auth_config WHERE id=1').fetchone()['value'])
            if expected_config and fingerprint(json.dumps(current_config, sort_keys=True)) != expected_config:
                abort(401)
            if method == 'oidc' and oidc_name is not None:
                conn.execute('UPDATE auth_users SET oidc_name=? WHERE id=?', (oidc_name, user['id']))
            revoke(conn)
            conn.execute('DELETE FROM auth_sessions WHERE expires<? OR last_seen<?', (now, now - cfg['idle_minutes'] * 60))
            conn.execute('INSERT INTO auth_sessions VALUES(?,?,?,?,?,?)',
                         (fingerprint(token), user['id'], now, now + cfg['session_minutes'] * 60, now, method))
            audit(conn, 'login_' + method, user['id'])
            conn.commit()
        session.clear()
        csrf()
        response = redirect(safe_next(destination))
        response.set_cookie(COOKIE, token, max_age=cfg['session_minutes'] * 60, secure=secure, httponly=True, samesite='Lax')
        return response

    def authenticate_password(user, password, cfg):
        ip, now = request.remote_addr or 'unknown', time.time()
        keys = failure_keys(user, ip)
        excluded = exempt(cfg, ip)
        # Serialize counters so concurrent requests cannot skip lockout.
        with closing(connect(db)) as conn:
            conn.execute('BEGIN IMMEDIATE')
            if not excluded and locked(conn, keys, now):
                conn.rollback()
                return False
            if user:
                fresh = conn.execute('SELECT * FROM auth_users WHERE id=?', (user['id'],)).fetchone()
                user = dict(fresh) if fresh else None
            valid = valid_password(user, password, dummy)
            if valid:
                for name in keys:
                    conn.execute('DELETE FROM auth_failures WHERE key=?', (name,))
            elif not excluded:
                record_failure(conn, keys, cfg, now)
            if not valid:
                audit(conn, 'login_failed', user['id'] if user else None)
            conn.commit()
            return valid

    @app.before_request
    def protect():
        if request.path.startswith(('/api/assistant/','/api/tasks')) or request.path in ('/mcp', '/settings/ai'):
            if request.path == '/api/assistant/transcribe':
                limit = 11 * 1024 * 1024
            elif request.method == 'POST' and re.fullmatch(r'/api/assistant/conversations/[A-Za-z0-9_-]+/messages', request.path):
                limit = 12 * 1024 * 1024  # Four 2 MB images plus base64/JSON overhead.
            else:
                limit = 65536
            request.max_content_length = limit
            if request.content_length and request.content_length > limit:
                return jsonify(error='Request is too large.'), 413
        g.auth_user = None
        g.api_key = None
        g.oauth_grant = None
        g.auth_settings = settings(db)
        from . import oauth
        if request.endpoint == 'assistant.mcp':
            return oauth.authenticate_mcp(db)
        # OAuth protocol endpoints authenticate clients/grants themselves; browser
        # consent and connection management still use the normal session and CSRF.
        if request.endpoint in oauth.PUBLIC:
            request.max_content_length = 16384
            return None
        now = time.time()
        supplied_key = request.headers.get('X-API-Key', '')
        authorization = request.authorization
        if authorization:
            if authorization.type.lower() == 'bearer':
                supplied_key = authorization.token or ''
            elif authorization.type.lower() == 'basic' and authorization.username == 'apikey':
                supplied_key = authorization.password or ''
        if supplied_key or authorization or 'X-API-Key' in request.headers or 'Authorization' in request.headers:
            if len(supplied_key) > 100:
                return jsonify(error='Invalid API key.'), 401
            with closing(connect(db)) as conn:
                row = conn.execute('SELECT * FROM auth_api_keys WHERE token_hash=? AND revoked=0 AND (expires IS NULL OR expires>?)',
                                   (fingerprint(supplied_key), now)).fetchone()
                if not row:
                    return jsonify(error='Invalid API key.'), 401
                api_key = dict(row)
                api_key['scopes'] = json.loads(api_key['scopes'])
                api_key['feed_ids'] = json.loads(api_key['feed_ids'])
                required = {'feed_route': 'rss:read', 'auth.api_feeds': 'feeds:read',
                            'auth.api_topics': 'topics:read', 'auth.api_notifications': 'notifications:read',
                            'auth.api_refresh': 'feeds:refresh'}.get(request.endpoint)
                if not required or required not in api_key['scopes']:
                    return jsonify(error='API key does not permit this endpoint.'), 403
                if request.endpoint == 'feed_route':
                    feed = conn.execute('SELECT id FROM profiles WHERE feed_token=?', (request.view_args['token'],)).fetchone()
                    if not feed or (api_key['feed_ids'] and feed['id'] not in api_key['feed_ids']):
                        return jsonify(error='Feed not permitted.'), 403
                if request.endpoint == 'auth.api_refresh' and api_key['feed_ids'] and request.view_args['feed_id'] not in api_key['feed_ids']:
                    return jsonify(error='Feed not permitted.'), 403
                conn.execute('UPDATE auth_api_keys SET last_used=? WHERE id=?', (now, api_key['id']))
                conn.commit()
            g.api_key = api_key
            # Header credentials cannot be automatically attached cross-site by a browser.
            # Basic authentication is sometimes cached by browsers: require CSRF for Basic writes.
            if request.method in ('GET', 'HEAD') or not authorization or authorization.type.lower() != 'basic':
                return None
        token = request.cookies.get(COOKIE, '')
        if token and len(token) <= 100:
            with closing(connect(db)) as conn:
                row = conn.execute('''SELECT u.*, s.method, s.created AS session_created, s.expires AS session_expires, s.last_seen AS session_seen FROM auth_sessions s JOIN auth_users u ON u.id=s.user_id
                    WHERE s.token_hash=? AND s.expires>? AND s.created>? AND s.last_seen>?''',
                    (fingerprint(token), now, now - g.auth_settings['session_minutes'] * 60,
                     now - g.auth_settings['idle_minutes'] * 60)).fetchone()
                if row:
                    g.auth_user = dict(row)
                    passive = request.endpoint == 'auth.session_status'
                    g.auth_expires_at = min(row['session_expires'], row['session_created'] + g.auth_settings['session_minutes']*60,
                                            (row['session_seen'] if passive else now) + g.auth_settings['idle_minutes']*60)
                    if not passive:
                        conn.execute('UPDATE auth_sessions SET last_seen=? WHERE token_hash=?', (now, fingerprint(token)))
                        conn.commit()
        if request.endpoint not in PUBLIC and not g.auth_user and not g.api_key:
            if request.path.startswith('/api/') or request.path == '/mcp' or request.method not in ('GET', 'HEAD') or request.path.endswith('.xml'):
                return jsonify(error='Authentication required.'), 401
            destination = dict(next=safe_next(request.full_path.rstrip('?')))
            if request.endpoint in ('notifications_route', 'notification_detail_route'):
                # A notification click requires an explicit sign-in action, even
                # when automatic SSO is enabled for ordinary visits.
                destination['sso'] = 'off'
            return redirect(url_for('auth.login' if owner(db) else 'auth.setup', **destination))
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            supplied = request.headers.get('X-Nightfeed-CSRF') or request.form.get('auth_csrf', '')
            expected = session.get('csrf', '')
            if not expected or not supplied or not equal(expected, supplied):
                return jsonify(error='Invalid security token. Reload the page.'), 403
            if not same_origin():
                return jsonify(error='Cross-origin requests are not allowed.'), 403
            if request.headers.get('Sec-Fetch-Site') == 'cross-site':
                return jsonify(error='Cross-origin requests are not allowed.'), 403

    @app.after_request
    def security_headers(response):
        if request.endpoint != 'static':
            response.headers['Cache-Control'] = 'no-store, private'
            response.headers['Pragma'] = 'no-cache'
            response.headers.add('Vary', 'Cookie')
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        # Strip paths/query strings, while preserving the origin of HTML form POSTs.
        # `no-referrer` causes browsers to send Origin: null for these submissions.
        response.headers['Referrer-Policy'] = 'strict-origin'
        response.headers.setdefault('Content-Security-Policy', "object-src 'none'; base-uri 'self'; frame-ancestors 'self'; form-action 'self'")
        if request.is_secure:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        return response

    @app.context_processor
    def context():
        user = getattr(g, 'auth_user', None)
        if user:
            user = dict(user)
            if g.auth_settings['oidc_enabled'] and g.auth_settings['oidc_use_name'] and user['oidc_name']:
                user['name'] = user['oidc_name']
        return dict(auth_csrf=csrf(), auth_user=user)

    @bp.get('/api/auth/session')
    def session_status():
        # This passive check must never keep an otherwise idle session alive.
        return jsonify(authenticated=True, expires_in=max(0,g.auth_expires_at-time.time()))

    @bp.route('/settings/profile', methods=['GET', 'POST'])
    def profile():
        error, saved = None, False
        if request.method == 'POST':
            try:
                with closing(connect(db)) as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    cfg = DEFAULTS | json.loads(conn.execute('SELECT value FROM auth_config WHERE id=1').fetchone()['value'])
                    managed = request.form.get('oidc_use_name') == 'on'
                    if managed and not (cfg['oidc_enabled'] and cfg['subject']):
                        raise ValueError('Enable SSO and link your owner identity before using the provider name.')
                    if not managed:
                        name = request.form.get('display_name', '').strip()
                        if not name or len(name) > 100 or any(ord(c) < 32 or ord(c) == 127 for c in name):
                            raise ValueError('Enter a name of 1–100 characters without control characters.')
                        conn.execute('UPDATE auth_users SET name=? WHERE id=?', (name, g.auth_user['id']))
                    cfg['oidc_use_name'] = managed
                    conn.execute('UPDATE auth_config SET value=? WHERE id=1', (json.dumps(cfg),))
                    audit(conn, 'profile_updated', g.auth_user['id'])
                    conn.commit()
                g.auth_settings = cfg
                g.auth_user.update(owner(db))
                saved = True
            except ValueError as exc:
                error = str(exc)
        return render_template('profile.html', config=g.auth_settings, local_name=g.auth_user['name'],
                               sso_name=g.auth_user['oidc_name'], error=error, saved=saved), 400 if error else 200

    @bp.route('/auth/setup', methods=['GET', 'POST'])
    def setup():
        if owner(db):
            return redirect(url_for('auth.login'))
        error = None
        if request.method == 'POST':
            supplied = request.form.get('setup_token', '')
            if not equal(setup_token(db), supplied):
                error = 'Invalid setup token. Read it from the server data directory.'
            else:
                try:
                    username = request.form.get('username', '').strip().casefold()
                    name = request.form.get('name', '').strip()
                    if not re.fullmatch(r'[a-z0-9_.-]{3,64}', username) or not name or len(name) > 100:
                        raise ValueError('Enter a username (3–64 letters, numbers, dots, underscores or hyphens) and name (up to 100 characters).')
                    if request.form.get('password') != request.form.get('confirm_password'):
                        raise ValueError('Passwords do not match.')
                    hashed = password_hash(request.form.get('password', ''))
                    with closing(connect(db)) as conn:
                        conn.execute('BEGIN IMMEDIATE')
                        if conn.execute('SELECT 1 FROM auth_users').fetchone():
                            abort(409)
                        identity = conn.execute('INSERT INTO auth_users(username,name,password_hash) VALUES(?,?,?)', (username, name, hashed)).lastrowid
                        from .push_notifications import claim_legacy_devices
                        claim_legacy_devices(conn)
                        audit(conn, 'owner_created', identity)
                        conn.commit()
                    Path(db).with_suffix('.setup-token').unlink(missing_ok=True)
                    return sign_in(owner(db), 'password')
                except ValueError as exc:
                    error = str(exc)
        return render_template('auth.html', mode='setup', error=error), 400 if error else 200

    @bp.route('/auth/login', methods=['GET', 'POST'])
    def login():
        if not owner(db):
            return redirect(url_for('auth.setup'))
        if g.auth_user:
            return redirect('/')
        cfg, error = g.auth_settings, None
        show_form = force_form or not cfg['disable_form'] or not cfg['oidc_enabled']
        if request.method == 'POST':
            if not show_form:
                abort(403)
            user = owner(db)
            username = request.form.get('username', '').strip().casefold()
            candidate = user if equal(username, user['username']) else None
            if authenticate_password(candidate, request.form.get('password', ''), cfg):
                return sign_in(user, 'password', request.form.get('next'))
            error = 'Sign in failed. Check your credentials or try again after the lockout period.'
        elif cfg['oidc_enabled'] and cfg['auto_login'] and not force_form and request.args.get('sso') != 'off':
            return redirect(url_for('auth.oidc_start', next=safe_next(request.args.get('next'))))
        return render_template('auth.html', mode='login', error=error, show_form=show_form, config=cfg,
                               next=safe_next(request.args.get('next'))), 401 if error else 200

    @bp.post('/auth/logout')
    def logout():
        endpoint = g.auth_settings['logout_endpoint'] if g.auth_user['method'] == 'oidc' else ''
        with closing(connect(db)) as conn:
            revoke(conn)
            audit(conn, 'logout', g.auth_user['id'])
            conn.commit()
        session.clear()
        response = redirect(endpoint or url_for('auth.login', sso='off'))
        response.delete_cookie(COOKIE, secure=secure, httponly=True, samesite='Lax')
        # Storage clearing unregisters the service worker and loses the push-device
        # management token. Push opt-in is independent of the browser login session.
        response.headers['Clear-Site-Data'] = '"cache"'
        return response

    @bp.route('/settings/security', methods=['GET', 'POST'])
    def security():
        error, saved = None, False
        if request.method == 'POST':
            if not authenticate_password(g.auth_user, request.form.get('current_password', ''), g.auth_settings):
                error = 'Current password is incorrect or login is locked.'
            else:
                try:
                    cfg = parse_settings(request.form, g.auth_settings, key)
                    with closing(connect(db)) as conn:
                        conn.execute('BEGIN IMMEDIATE')
                        if (cfg['issuer'], cfg['subject']) != (g.auth_settings['issuer'], g.auth_settings['subject']):
                            conn.execute("UPDATE auth_users SET oidc_name='' WHERE id=?", (g.auth_user['id'],))
                            g.auth_user['oidc_name'] = ''
                        conn.execute('UPDATE auth_config SET value=? WHERE id=1', (json.dumps(cfg),))
                        conn.execute('DELETE FROM auth_identities WHERE user_id=?', (g.auth_user['id'],))
                        if cfg['oidc_enabled'] and cfg['subject']:
                            conn.execute('INSERT INTO auth_identities VALUES(?,?,?)', (cfg['issuer'], cfg['subject'], g.auth_user['id']))
                        # Settings change revokes every other session, including old OIDC sessions.
                        conn.execute('DELETE FROM auth_sessions WHERE token_hash<>?', (fingerprint(request.cookies.get(COOKIE, '')),))
                        revoke_application_grants(conn, g.auth_user['id'])
                        audit(conn, 'security_updated', g.auth_user['id'])
                        conn.commit()
                    g.auth_settings, saved = cfg, True
                except ValueError as exc:
                    error = str(exc)
        cfg = dict(g.auth_settings)
        cfg['has_secret'] = bool(cfg.pop('client_secret', ''))
        with closing(connect(db)) as conn:
            events = conn.execute('SELECT at,event,ip FROM auth_audit ORDER BY id DESC LIMIT 20').fetchall()
            locks = conn.execute('SELECT key,locked_until FROM auth_failures WHERE locked_until=-1 OR locked_until>?', (time.time(),)).fetchall()
        return render_template('security.html', config=cfg, error=error, saved=saved, events=events, locks=locks, force_form=force_form), 400 if error else 200

    @bp.post('/auth/password')
    def change_password():
        if not authenticate_password(g.auth_user, request.form.get('current_password', ''), g.auth_settings):
            return render_template('password.html', error='Current password is incorrect or login is locked.'), 400
        try:
            if request.form.get('new_password') != request.form.get('confirm_password'):
                raise ValueError('Passwords do not match.')
            hashed = password_hash(request.form.get('new_password', ''))
        except ValueError as exc:
            return render_template('password.html', error=str(exc)), 400
        with closing(connect(db)) as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('UPDATE auth_users SET password_hash=? WHERE id=?', (hashed, g.auth_user['id']))
            conn.execute('DELETE FROM auth_sessions WHERE user_id=?', (g.auth_user['id'],))
            revoke_application_grants(conn, g.auth_user['id'])
            audit(conn, 'password_changed', g.auth_user['id'])
            conn.commit()
        return sign_in(owner(db), 'password', '/settings/security?password_changed=1')

    @bp.get('/settings/password')
    def password_page():
        return render_template('password.html')

    @bp.post('/auth/unlock')
    def unlock():
        # An authenticated owner can recover an IP/account without needing an excluded IP.
        with closing(connect(db)) as conn:
            conn.execute('DELETE FROM auth_failures')
            audit(conn, 'unlocked', g.auth_user['id'])
            conn.commit()
        return redirect(url_for('auth.security'))

    @bp.route('/settings/api-keys', methods=['GET', 'POST'])
    def api_keys():
        error, new_key = None, None
        if request.method == 'POST':
            if not authenticate_password(g.auth_user, request.form.get('current_password', ''), g.auth_settings):
                error = 'Current password is incorrect or login is locked.'
            else:
                try:
                    name = request.form.get('key_label', request.form.get('name', '')).strip()
                    scopes = request.form.getlist('scopes')
                    if not name or len(name) > 100 or not scopes or any(v not in SCOPES for v in scopes):
                        raise ValueError('Enter a key name and select at least one permission.')
                    feed_ids = list(set(int(v) for v in request.form.getlist('feed_ids')))
                    expiry = request.form.get('expires', '')
                    expires = datetime.fromisoformat(expiry).replace(tzinfo=timezone.utc).timestamp() if expiry else None
                    if expires is not None and expires <= time.time():
                        raise ValueError('Expiry must be in the future (UTC).')
                    new_key = 'nf_' + secrets.token_urlsafe(32)
                    with closing(connect(db)) as conn:
                        if any(not conn.execute('SELECT 1 FROM profiles WHERE id=?', (v,)).fetchone() for v in feed_ids):
                            raise ValueError('Unknown feed restriction.')
                        conn.execute('INSERT INTO auth_api_keys(user_id,name,token_hash,prefix,scopes,feed_ids,created,expires) VALUES(?,?,?,?,?,?,?,?)',
                                     (g.auth_user['id'], name, fingerprint(new_key), new_key[:11], json.dumps(scopes), json.dumps(feed_ids), time.time(), expires))
                        audit(conn, 'api_key_created', g.auth_user['id'])
                        conn.commit()
                except (ValueError, OverflowError) as exc:
                    error, new_key = str(exc), None
        with closing(connect(db)) as conn:
            keys = conn.execute('SELECT id,name,prefix,scopes,expires,last_used,revoked,feed_ids FROM auth_api_keys WHERE user_id=? ORDER BY id DESC', (g.auth_user['id'],)).fetchall()
            feeds = conn.execute('SELECT id,feed_title FROM profiles ORDER BY feed_title').fetchall()
        return render_template('api_keys.html', keys=keys, feeds=feeds, scopes=SCOPES, new_key=new_key, error=error), 400 if error else 200

    @bp.post('/auth/api-keys/<int:key_id>/revoke')
    def revoke_api_key(key_id):
        with closing(connect(db)) as conn:
            conn.execute('UPDATE auth_api_keys SET revoked=1 WHERE id=? AND user_id=?', (key_id, g.auth_user['id']))
            audit(conn, 'api_key_revoked', g.auth_user['id'])
            conn.commit()
        return redirect(url_for('auth.api_keys'))

    def permitted_feed_ids(conn):
        if g.api_key and g.api_key['feed_ids']:
            return g.api_key['feed_ids']
        return [row['id'] for row in conn.execute('SELECT id FROM profiles')]

    @bp.get('/api/v1/feeds')
    def api_feeds():
        with closing(connect(db)) as conn:
            ids = permitted_feed_ids(conn)
            rows = [dict(row) for row in conn.execute('SELECT id,feed_title,active FROM profiles ORDER BY id') if row['id'] in ids]
        return jsonify(feeds=rows)

    @bp.get('/api/v1/topics')
    def api_topics():
        with closing(connect(db)) as conn:
            ids = permitted_feed_ids(conn)
            rows = conn.execute('SELECT id,profile_id,title,link,summary,discovered_at AS published_at FROM feed_items WHERE profile_id IN (SELECT value FROM json_each(?)) ORDER BY id DESC LIMIT 100', (json.dumps(ids),)).fetchall()
        return jsonify(topics=[dict(row) for row in rows])

    @bp.get('/api/v1/notifications')
    def api_notifications():
        with closing(connect(db)) as conn:
            ids = permitted_feed_ids(conn)
            rows = conn.execute('SELECT id,profile_id,event_type AS kind,title,message,created_at FROM notifications WHERE profile_id IN (SELECT value FROM json_each(?)) ORDER BY id DESC LIMIT 100', (json.dumps(ids),)).fetchall()
        return jsonify(notifications=[dict(row) for row in rows])

    @bp.post('/api/v1/feeds/<int:feed_id>/refresh')
    def api_refresh(feed_id):
        from .app import get_profile_by_id, refresh_profile
        if not get_profile_by_id(db, feed_id):
            abort(404)
        return jsonify(result=refresh_profile(db, feed_id))

    def oidc_client(cfg):
        from authlib.integrations.flask_client import OAuth
        oauth = OAuth(app)
        kwargs = dict(client_id=cfg['client_id'], client_secret=Fernet(key).decrypt(cfg['client_secret'].encode()).decode(),
                      client_kwargs=dict(scope='openid profile', code_challenge_method='S256', timeout=15),
                      issuer=cfg['issuer'])
        if cfg['discovery']:
            kwargs['server_metadata_url'] = cfg['issuer'].rstrip('/') + '/.well-known/openid-configuration'
        else:
            kwargs.update(authorize_url=cfg['authorization_endpoint'], access_token_url=cfg['token_endpoint'],
                          authorization_endpoint=cfg['authorization_endpoint'], token_endpoint=cfg['token_endpoint'],
                          jwks_uri=cfg['jwks_uri'], id_token_signing_alg_values_supported=['RS256', 'ES256'])
        client = oauth.register('nightfeed', **kwargs)
        metadata = client.load_server_metadata()
        if metadata.get('issuer') != cfg['issuer']:
            raise ValueError('Issuer mismatch')
        for endpoint in ('authorization_endpoint', 'token_endpoint', 'jwks_uri'):
            https_url(metadata.get(endpoint, ''), endpoint, required=True)
        algorithms = metadata.get('id_token_signing_alg_values_supported', [])
        # Only asymmetric signed ID tokens. Do not trust the token's alg alone.
        metadata['id_token_signing_alg_values_supported'] = [v for v in algorithms if v in ('RS256', 'RS384', 'RS512', 'ES256', 'ES384', 'ES512', 'PS256', 'PS384', 'PS512', 'EdDSA')]
        if not metadata['id_token_signing_alg_values_supported']:
            raise ValueError('No supported signature algorithms')
        return client

    def begin_oidc(link_user=None):
        cfg = g.auth_settings
        if not owner(db) or not cfg['oidc_enabled']:
            abort(404)
        session['oidc_next'] = safe_next(request.args.get('next'))
        session['oidc_config'] = fingerprint(json.dumps(cfg, sort_keys=True))
        session['oidc_started'] = time.time()
        session.pop('oidc_link_user', None)
        if link_user:
            session['oidc_link_user'] = link_user
        try:
            return oidc_client(cfg).authorize_redirect(cfg['redirect_uri'], nonce=secrets.token_urlsafe(32))
        except Exception:
            logging.getLogger(__name__).warning('OIDC authorization failed; provider details omitted')
            return render_template('auth.html', mode='error', error='SSO is unavailable. Contact the instance owner.'), 502

    @bp.get('/auth/oidc')
    def oidc_start():
        return begin_oidc()

    @bp.post('/auth/oidc/link')
    def oidc_link():
        if not authenticate_password(g.auth_user, request.form.get('current_password', ''), g.auth_settings):
            return jsonify(error='Current password is incorrect or login is locked.'), 400
        return begin_oidc(g.auth_user['id'])

    @bp.get('/auth/oidc/callback')
    def oidc_callback():
        cfg = g.auth_settings
        try:
            expected = session.pop('oidc_config', '')
            started = session.pop('oidc_started', 0)
            link_user = session.pop('oidc_link_user', None)
            if (not cfg['oidc_enabled'] or not owner(db) or time.time() - started > 600
                    or not equal(expected, fingerprint(json.dumps(cfg, sort_keys=True)))):
                raise ValueError('Stale OIDC flow')
            # Authlib validates state, PKCE, signature, issuer, audience, expiry and nonce.
            token = oidc_client(cfg).authorize_access_token()
            claims = token.get('userinfo')
            if not claims or claims.get('iss') != cfg['issuer'] or not isinstance(claims.get('sub'), str) or not claims['sub'] or len(claims['sub']) > 255:
                raise ValueError('Invalid owner identity')
            if link_user:
                if not g.auth_user or g.auth_user['id'] != link_user:
                    raise ValueError('Owner session expired during linking')
                with closing(connect(db)) as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    active = conn.execute('SELECT 1 FROM auth_sessions WHERE token_hash=? AND user_id=? AND expires>?',
                                          (fingerprint(request.cookies.get(COOKIE, '')), link_user, time.time())).fetchone()
                    if not active:
                        raise ValueError('Owner session was revoked during linking')
                    current = DEFAULTS | json.loads(conn.execute('SELECT value FROM auth_config WHERE id=1').fetchone()['value'])
                    if fingerprint(json.dumps(current, sort_keys=True)) != expected:
                        raise ValueError('SSO configuration changed')
                    current['subject'] = claims['sub']
                    conn.execute('UPDATE auth_users SET oidc_name=? WHERE id=?', (provider_name(claims) or '', link_user))
                    conn.execute('UPDATE auth_config SET value=? WHERE id=1', (json.dumps(current),))
                    conn.execute('DELETE FROM auth_identities WHERE user_id=?', (link_user,))
                    conn.execute('INSERT INTO auth_identities VALUES(?,?,?)', (claims['iss'], claims['sub'], link_user))
                    conn.execute('DELETE FROM auth_sessions WHERE token_hash<>?', (fingerprint(request.cookies.get(COOKIE, '')),))
                    revoke_application_grants(conn, link_user)
                    audit(conn, 'oidc_identity_linked', link_user)
                    conn.commit()
                return redirect(url_for('auth.security', oidc_linked=1))
            if claims.get('sub') != cfg['subject']:
                raise ValueError('Identity does not match owner')
            with closing(connect(db)) as conn:
                identity = conn.execute('SELECT user_id FROM auth_identities WHERE issuer=? AND subject=?', (claims['iss'], claims['sub'])).fetchone()
            user = owner(db)
            if not identity or identity['user_id'] != user['id']:
                raise ValueError('Unknown identity')
            return sign_in(user, 'oidc', session.pop('oidc_next', '/'), expected_config=fingerprint(json.dumps(cfg, sort_keys=True)), oidc_name=provider_name(claims))
        except Exception:
            # Never log codes, provider responses, claims, tokens or secrets.
            session.clear()
            with closing(connect(db)) as conn:
                audit(conn, 'oidc_failed')
                conn.commit()
            return render_template('auth.html', mode='error', error='SSO sign in failed. Retry or contact the instance owner.'), 401

    app.register_blueprint(bp)


def main():
    parser = argparse.ArgumentParser(description='Nightfeed offline authentication recovery (stop Nightfeed first).')
    parser.add_argument('action', choices=['unlock', 'reset-password'])
    parser.add_argument('--database', default=os.environ.get('NIGHTFEED_DATABASE_PATH', 'data/rss_site_bridge.db'))
    args = parser.parse_args()
    db = Path(args.database)
    if not db.is_file():
        parser.error('Database does not exist.')
    user = owner(db)
    if not user:
        parser.error('Complete initial owner setup first.')
    hashed = None
    if args.action == 'reset-password':
        value = getpass.getpass('New password (15–256 characters): ')
        if value != getpass.getpass('Confirm password: '):
            parser.error('Passwords do not match.')
        hashed = password_hash(value)
    with closing(connect(db)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('DELETE FROM auth_failures')
        conn.execute('DELETE FROM auth_sessions')
        if hashed:
            conn.execute('UPDATE auth_users SET password_hash=? WHERE id=?', (hashed, user['id']))
            revoke_application_grants(conn, user['id'])
        audit(conn, 'offline_' + args.action, user['id'], ip='console')
        conn.commit()
    print('Recovery complete. All sessions were revoked.')


if __name__ == '__main__':
    main()
