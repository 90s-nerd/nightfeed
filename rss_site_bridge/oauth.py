"""User-delegated OAuth for MCP. Browser login remains owned by auth.py.

Authlib validates OAuth clients, authorization codes and PKCE. Opaque credentials
are hashed at rest; SQLite transactions serialize redemption and rotation.
"""
from __future__ import annotations

from contextlib import closing
import json
import ipaddress
import logging
import os
from pathlib import Path
import re
import secrets
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

from authlib.integrations.flask_oauth2 import AuthorizationServer
from authlib.oauth2.rfc6749 import InvalidGrantError, InvalidRequestError, OAuth2Error
from authlib.oauth2.rfc6749.grants import AuthorizationCodeGrant, RefreshTokenGrant
from authlib.oauth2.rfc7636 import CodeChallenge
from authlib.oauth2.rfc7009 import RevocationEndpoint
from flask import Blueprint, abort, g, jsonify, redirect, render_template, request, url_for
from itsdangerous import BadSignature, URLSafeTimedSerializer

from .auth import audit, equal, fingerprint
from .downloaders import connect

SCOPE = 'nightfeed:access'
ACCESS_SECONDS = 900
PUBLIC = {'oauth.resource_metadata', 'oauth.server_metadata', 'oauth.register_client', 'oauth.token', 'oauth.revoke'}


def initialize(db):
    with closing(connect(db)) as conn:
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS auth_oauth_clients (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, redirect_uris TEXT NOT NULL,
          auth_method TEXT NOT NULL, secret_hash TEXT NOT NULL, created REAL NOT NULL,
          grant_types TEXT NOT NULL DEFAULT '["authorization_code","refresh_token"]'
        );
        CREATE TABLE IF NOT EXISTS auth_oauth_grants (
          id TEXT PRIMARY KEY, client_id TEXT NOT NULL, user_id INTEGER NOT NULL,
          user_version TEXT NOT NULL, issuer TEXT NOT NULL, resource TEXT NOT NULL,
          created REAL NOT NULL, expires REAL, last_used REAL,
          revoked INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS auth_oauth_codes (
          hash TEXT PRIMARY KEY, grant_id TEXT NOT NULL, redirect_uri TEXT NOT NULL,
          code_challenge TEXT NOT NULL, expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS auth_oauth_tokens (
          access_hash TEXT PRIMARY KEY, refresh_hash TEXT UNIQUE NOT NULL,
          grant_id TEXT NOT NULL, expires REAL NOT NULL, rotated INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS auth_oauth_registration_limits (
          bucket TEXT PRIMARY KEY, started REAL NOT NULL, count INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS oauth_tokens_grant ON auth_oauth_tokens(grant_id);
        ''')
        if 'grant_types' not in {row['name'] for row in conn.execute('PRAGMA table_info(auth_oauth_clients)')}:
            conn.execute('ALTER TABLE auth_oauth_clients ADD COLUMN grant_types TEXT NOT NULL DEFAULT \'["authorization_code","refresh_token"]\'')
        conn.commit()


def validate_issuer(value):
    """A fixed administrator-supplied origin prevents Host-header discovery poisoning."""
    if not isinstance(value, str) or len(value) > 512:
        raise ValueError('Enter the public HTTPS Nightfeed URL.')
    value = value.strip().rstrip('/')
    parts = urlsplit(value)
    if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password
            or parts.path or parts.query or parts.fragment or re.search(r'[\s\\\x00-\x1f]', value)):
        raise ValueError('Enter the public HTTPS Nightfeed URL without a path, query or fragment.')
    try:
        parts.port
    except ValueError:
        raise ValueError('Invalid public Nightfeed URL port.') from None
    return 'https://' + parts.netloc.lower()


def configuration(conn):
    row = conn.execute('SELECT mcp_enabled,mcp_public_url FROM assistant_config WHERE id=1').fetchone()
    value = os.environ.get('NIGHTFEED_OAUTH_ISSUER', '') or row['mcp_public_url']
    return bool(row['mcp_enabled']), validate_issuer(value) if value else ''


def user_version(conn, user):
    cfg = json.loads(conn.execute('SELECT value FROM auth_config WHERE id=1').fetchone()['value'])
    # Display-name preferences do not change the user's authentication or access.
    cfg.pop('oidc_use_name', None)
    return fingerprint(user['password_hash'] + '\n' + json.dumps(cfg, sort_keys=True))


def grant_user(conn, grant):
    enabled, issuer = configuration(conn)
    user = conn.execute('SELECT * FROM auth_users WHERE id=?', (grant['user_id'],)).fetchone()
    first = conn.execute('SELECT id FROM auth_users ORDER BY id LIMIT 1').fetchone()
    if (not enabled or not issuer or grant['revoked']
            or (grant['expires'] is not None and grant['expires'] <= time.time())
            or grant['issuer'] != issuer or grant['resource'] != issuer + '/mcp'
            or not user or user['role'] != 'owner' or user['id'] != first['id']
            or not equal(grant['user_version'], user_version(conn, user))):
        return None
    return dict(user)


def challenge(db, invalid=False):
    with closing(connect(db)) as conn:
        enabled, issuer = configuration(conn)
    if not enabled:
        return jsonify(error='MCP is disabled in Settings.'), 404
    if not issuer:
        return jsonify(error='Configure the public HTTPS Nightfeed URL in MCP settings.'), 503
    response = jsonify(error='A Nightfeed OAuth access token is required.')
    response.status_code = 401
    response.headers['WWW-Authenticate'] = ('Bearer resource_metadata="' + issuer
        + '/.well-known/oauth-protected-resource/mcp", scope="' + SCOPE + '"'
        + (', error="invalid_token"' if invalid else ''))
    return response


def authenticate_mcp(db):
    """Only an access token is accepted here, never a cookie or an API key."""
    authorization = request.authorization
    raw = authorization.token if authorization and authorization.type.lower() == 'bearer' else ''
    if not raw or len(raw) > 128 or request.headers.get('X-API-Key'):
        return challenge(db, bool(authorization))
    with closing(connect(db)) as conn:
        _, issuer = configuration(conn)
        if issuer and request.host_url.rstrip('/') != issuer:
            return jsonify(error='Use the configured public HTTPS Nightfeed URL.'), 400
        token = conn.execute('SELECT * FROM auth_oauth_tokens WHERE access_hash=? AND expires>?',
                             (fingerprint(raw), time.time())).fetchone()
        grant = conn.execute('SELECT * FROM auth_oauth_grants WHERE id=?', (token['grant_id'],)).fetchone() if token else None
        user = grant_user(conn, grant) if grant else None
        if not user:
            return challenge(db, True)
        conn.execute('UPDATE auth_oauth_grants SET last_used=? WHERE id=?', (time.time(), grant['id']))
        conn.commit()
    g.auth_user, g.oauth_grant = user, dict(grant)
    # Rechecked inside service writes, after they acquire their SQLite write lock.
    def check_active():
        with closing(connect(db)) as conn:
            fresh = conn.execute('SELECT * FROM auth_oauth_grants WHERE id=?', (grant['id'],)).fetchone()
            active = conn.execute('SELECT expires FROM auth_oauth_tokens WHERE access_hash=?', (fingerprint(raw),)).fetchone()
            if not fresh or not active or active['expires'] <= time.time() or not grant_user(conn, fresh):
                raise ValueError('The connected application is no longer authorized. Reconnect in your client.')
    g.oauth_check_active = check_active
    return None


class Client:
    def __init__(self, row):
        self.client_id = row['id']
        self.name = row['name']
        self.redirect_uris = json.loads(row['redirect_uris'])
        self.auth_method, self.secret_hash = row['auth_method'], row['secret_hash']
        self.grant_types = json.loads(row['grant_types'])

    def get_client_id(self): return self.client_id
    def get_default_redirect_uri(self): return self.redirect_uris[0]
    def check_redirect_uri(self, uri): return uri in self.redirect_uris
    def get_allowed_scope(self, scope): return SCOPE if not scope or scope == SCOPE else ''
    def check_client_secret(self, secret): return bool(self.secret_hash) and equal(self.secret_hash, fingerprint(secret))
    def check_endpoint_auth_method(self, method, endpoint): return method == self.auth_method
    def check_response_type(self, response_type): return response_type == 'code'
    def check_grant_type(self, grant_type): return grant_type in self.grant_types


class Credential(SimpleNamespace):
    def get_redirect_uri(self): return self.redirect_uri
    def get_scope(self): return SCOPE
    def check_client(self, client): return self.client_id == client.client_id


def register(app):
    db = Path(app.config['DATABASE_PATH'])
    if os.environ.get('NIGHTFEED_OAUTH_ISSUER'):
        validate_issuer(os.environ['NIGHTFEED_OAUTH_ISSUER'])
    initialize(db)
    with closing(connect(db)) as conn:
        enabled, issuer = configuration(conn)
        if enabled and issuer:
            conn.execute('UPDATE auth_oauth_grants SET revoked=1 WHERE issuer<>? OR resource<>?', (issuer, issuer + '/mcp'))
        else:
            conn.execute('UPDATE auth_oauth_grants SET revoked=1')
        conn.commit()
    bp = Blueprint('oauth', __name__)
    # The nf_ prefix also lets the existing audit redactor recognize a token
    # accidentally pasted into a chat message; only fingerprints enter auth tables.
    app.config.update(OAUTH2_SCOPES_SUPPORTED=[SCOPE],
                      OAUTH2_ACCESS_TOKEN_GENERATOR=lambda *args, **kwargs: 'nf_oauth_' + secrets.token_urlsafe(32),
                      OAUTH2_REFRESH_TOKEN_GENERATOR=lambda *args, **kwargs: 'nf_refresh_' + secrets.token_urlsafe(32),
                      OAUTH2_TOKEN_EXPIRES_IN={'authorization_code': ACCESS_SECONDS, 'refresh_token': ACCESS_SECONDS})
    signer = URLSafeTimedSerializer(app.secret_key, salt='nightfeed-oauth-consent')
    # Authlib's debug grant logs include raw issued tokens. Never enable those logs.
    for logger in ('authlib.oauth2.rfc6749.grants.authorization_code', 'authlib.oauth2.rfc6749.grants.refresh_token'):
        logging.getLogger(logger).setLevel(logging.WARNING)

    def query_client(client_id):
        row = g.oauth_conn.execute('SELECT * FROM auth_oauth_clients WHERE id=?', (client_id,)).fetchone()
        return Client(row) if row else None

    def save_token(token, oauth_request):
        credential = getattr(oauth_request, 'authorization_code', None) or oauth_request.refresh_token
        g.oauth_conn.execute('INSERT INTO auth_oauth_tokens VALUES(?,?,?,?,0)',
            (fingerprint(token['access_token']), fingerprint(token.get('refresh_token') or secrets.token_urlsafe(32)), credential.grant_id,
             time.time() + token['expires_in']))

    server = AuthorizationServer(app, query_client=query_client, save_token=save_token)

    class CodeGrant(AuthorizationCodeGrant):
        TOKEN_ENDPOINT_AUTH_METHODS = ['none', 'client_secret_basic', 'client_secret_post']

        def save_authorization_code(self, code, oauth_request):
            conn = g.oauth_conn
            identifier, now = secrets.token_urlsafe(24), time.time()
            user = conn.execute('SELECT * FROM auth_users WHERE id=?', (oauth_request.user['id'],)).fetchone()
            conn.execute('INSERT INTO auth_oauth_grants(id,client_id,user_id,user_version,issuer,resource,created,expires) VALUES(?,?,?,?,?,?,?,?)',
                         (identifier, oauth_request.client.client_id, user['id'], user_version(conn, user), g.oauth_issuer,
                          g.oauth_issuer + '/mcp', now, None))
            conn.execute('INSERT INTO auth_oauth_codes VALUES(?,?,?,?,?,0)',
                         (fingerprint(code), identifier, oauth_request.payload.redirect_uri,
                          oauth_request.payload.data['code_challenge'], now + 300))
            audit(conn, 'oauth_connected', user['id'])

        def query_authorization_code(self, code, client):
            conn = g.oauth_conn
            row = conn.execute('SELECT c.*,g.client_id FROM auth_oauth_codes c JOIN auth_oauth_grants g ON g.id=c.grant_id WHERE c.hash=? AND g.client_id=?',
                               (fingerprint(code), client.client_id)).fetchone()
            if not row:
                return None
            if row['used']:
                conn.execute('UPDATE auth_oauth_grants SET revoked=1 WHERE id=?', (row['grant_id'],))
                return None
            if row['expires'] <= time.time(): return None
            return Credential(**dict(row), code_challenge_method='S256')

        def delete_authorization_code(self, code):
            g.oauth_conn.execute('UPDATE auth_oauth_codes SET used=1 WHERE hash=?', (code.hash,))

        def authenticate_user(self, credential):
            grant = g.oauth_conn.execute('SELECT * FROM auth_oauth_grants WHERE id=?', (credential.grant_id,)).fetchone()
            return grant_user(g.oauth_conn, grant) if grant else None

    class RotationGrant(RefreshTokenGrant):
        TOKEN_ENDPOINT_AUTH_METHODS = CodeGrant.TOKEN_ENDPOINT_AUTH_METHODS
        INCLUDE_NEW_REFRESH_TOKEN = True

        def authenticate_refresh_token(self, raw):
            # Verify the client before replay revocation, to prevent other clients causing a denial of service.
            row = g.oauth_conn.execute('SELECT t.*,g.client_id FROM auth_oauth_tokens t JOIN auth_oauth_grants g ON g.id=t.grant_id WHERE t.refresh_hash=? AND g.client_id=?',
                (fingerprint(raw), self.request.client.client_id)).fetchone()
            if not row: return None
            if row['rotated']:
                g.oauth_conn.execute('UPDATE auth_oauth_grants SET revoked=1 WHERE id=?', (row['grant_id'],))
                return None
            return Credential(**dict(row))

        def authenticate_user(self, credential):
            user = CodeGrant.authenticate_user(self, credential)
            if not user: raise InvalidGrantError()
            return user

        def revoke_old_credential(self, credential):
            g.oauth_conn.execute('UPDATE auth_oauth_tokens SET rotated=1,expires=0 WHERE access_hash=?', (credential.access_hash,))

    class RevokeEndpoint(RevocationEndpoint):
        CLIENT_AUTH_METHODS = CodeGrant.TOKEN_ENDPOINT_AUTH_METHODS

        def query_token(self, raw, hint):
            row = g.oauth_conn.execute('SELECT t.*,g.client_id FROM auth_oauth_tokens t JOIN auth_oauth_grants g ON g.id=t.grant_id WHERE t.access_hash=? OR t.refresh_hash=?',
                                       (fingerprint(raw), fingerprint(raw))).fetchone()
            return Credential(**dict(row)) if row else None

        def revoke_token(self, token, oauth_request):
            g.oauth_conn.execute('UPDATE auth_oauth_grants SET revoked=1 WHERE id=?', (token.grant_id,))

    server.register_grant(CodeGrant, [CodeChallenge(required=True)])
    server.register_grant(RotationGrant)
    server.register_endpoint(RevokeEndpoint)

    @bp.before_request
    def require_configured_origin():
        request.max_content_length = 16384
        with closing(connect(db)) as conn:
            enabled, issuer = configuration(conn)
        if request.endpoint in ('oauth.connections', 'oauth.disconnect'): return None
        if not enabled: return jsonify(error='MCP is disabled in Settings.'), 404
        if not issuer: return jsonify(error='Configure the public HTTPS Nightfeed URL in MCP settings.'), 503
        if request.host_url.rstrip('/') != issuer:
            return jsonify(error='Use the configured public HTTPS Nightfeed URL.'), 400
        g.oauth_issuer = issuer

    @bp.after_request
    def consent_headers(response):
        # Browsers apply form-action to redirects after the consent POST too.
        # Permit only the callback whose exact URI Authlib has validated.
        callback_origin = getattr(g, 'oauth_callback_origin', None)
        if callback_origin:
            response.headers['Content-Security-Policy'] = ("object-src 'none'; base-uri 'self'; frame-ancestors 'self'; form-action 'self' " + callback_origin)
        return response

    @bp.get('/.well-known/oauth-protected-resource')
    @bp.get('/.well-known/oauth-protected-resource/mcp')
    def resource_metadata():
        return jsonify(resource=g.oauth_issuer + '/mcp', authorization_servers=[g.oauth_issuer],
                       scopes_supported=[SCOPE], bearer_methods_supported=['header'])

    @bp.get('/.well-known/oauth-authorization-server')
    def server_metadata():
        issuer = g.oauth_issuer
        return jsonify(issuer=issuer, authorization_endpoint=issuer + '/oauth/authorize', token_endpoint=issuer + '/oauth/token',
                       registration_endpoint=issuer + '/oauth/register', revocation_endpoint=issuer + '/oauth/revoke',
                       scopes_supported=[SCOPE], response_types_supported=['code'], grant_types_supported=['authorization_code', 'refresh_token'],
                       token_endpoint_auth_methods_supported=CodeGrant.TOKEN_ENDPOINT_AUTH_METHODS,
                       revocation_endpoint_auth_methods_supported=CodeGrant.TOKEN_ENDPOINT_AUTH_METHODS,
                       code_challenge_methods_supported=['S256'], authorization_response_iss_parameter_supported=True)

    @bp.post('/oauth/register')
    def register_client():
        data = request.get_json(silent=True)
        if not isinstance(data, dict): return jsonify(error='invalid_client_metadata'), 400
        uris, name = data.get('redirect_uris'), data.get('client_name', 'External application')
        method = data.get('token_endpoint_auth_method', 'none')
        grants = data.get('grant_types', ['authorization_code', 'refresh_token'])
        if (not isinstance(name, str) or not name.strip() or len(name) > 100 or re.search(r'[\x00-\x1f]', name)
                or not isinstance(uris, list) or not 1 <= len(uris) <= 10 or method not in CodeGrant.TOKEN_ENDPOINT_AUTH_METHODS
                or data.get('scope', SCOPE) != SCOPE or data.get('response_types', ['code']) != ['code']
                or not isinstance(grants, list) or not 1 <= len(grants) <= 2 or 'authorization_code' not in grants
                or any(grant not in ('authorization_code', 'refresh_token') for grant in grants) or len(set(grants)) != len(grants)):
            return jsonify(error='invalid_client_metadata'), 400
        for uri in uris:
            if not isinstance(uri, str) or len(uri) > 2048 or re.search(r'[\s\\\x00-\x1f*]', uri):
                return jsonify(error='invalid_redirect_uri'), 400
            try:
                parts = urlsplit(uri)
                parts.port
            except ValueError:
                return jsonify(error='invalid_redirect_uri'), 400
            try:
                loopback = ipaddress.ip_address(parts.hostname).is_loopback
            except ValueError:
                loopback = False
            if (not (parts.scheme == 'https' or (parts.scheme == 'http' and loopback))
                    or not parts.hostname or parts.username or parts.password or parts.fragment):
                return jsonify(error='invalid_redirect_uri'), 400
        identifier, secret, now = secrets.token_urlsafe(24), secrets.token_urlsafe(32) if method != 'none' else '', time.time()
        with closing(connect(db)) as conn:
            conn.execute('BEGIN IMMEDIATE')
            # DCR does not grant access. Bound unauthenticated registration storage and request rate.
            conn.execute('DELETE FROM auth_oauth_clients WHERE created<? AND id NOT IN (SELECT client_id FROM auth_oauth_grants)', (now - 7 * 86400,))
            conn.execute('DELETE FROM auth_oauth_registration_limits WHERE started<?', (now - 3600,))
            for bucket, maximum in (('global', 100), (fingerprint(request.remote_addr or 'unknown'), 20)):
                row = conn.execute('SELECT count FROM auth_oauth_registration_limits WHERE bucket=?', (bucket,)).fetchone()
                if row and row['count'] >= maximum:
                    return jsonify(error='temporarily_unavailable'), 429
            if conn.execute('SELECT count(*) FROM auth_oauth_clients').fetchone()[0] >= 1000:
                return jsonify(error='temporarily_unavailable'), 429
            for bucket in ('global', fingerprint(request.remote_addr or 'unknown')):
                conn.execute('INSERT INTO auth_oauth_registration_limits VALUES(?,?,1) ON CONFLICT(bucket) DO UPDATE SET count=count+1', (bucket, now))
            conn.execute('INSERT INTO auth_oauth_clients(id,name,redirect_uris,auth_method,secret_hash,created,grant_types) VALUES(?,?,?,?,?,?,?)',
                         (identifier, name.strip(), json.dumps(uris), method, fingerprint(secret) if secret else '', now, json.dumps(grants)))
            conn.commit()
        result = dict(client_id=identifier, client_id_issued_at=int(now), client_name=name.strip(), redirect_uris=uris,
                      token_endpoint_auth_method=method, grant_types=grants, response_types=['code'], scope=SCOPE)
        if secret: result.update(client_secret=secret, client_secret_expires_at=0)
        return jsonify(result), 201

    def validate_parameters(authorization=False):
        data = request.args if authorization else request.form
        if any(len(request.values.getlist(k)) != 1 for k in request.values):
            raise InvalidRequestError('Duplicate parameters are not accepted.')
        if data.get('resource') != g.oauth_issuer + '/mcp':
            raise InvalidRequestError('The resource must be the configured MCP URL.')
        if data.get('scope', SCOPE) != SCOPE:
            raise InvalidRequestError('Unsupported scope.')
        if authorization and (not data.get('redirect_uri') or data.get('code_challenge_method') != 'S256'
                              or not re.fullmatch(r'[A-Za-z0-9_-]{43}', data.get('code_challenge', ''))):
            raise InvalidRequestError('An exact callback and PKCE S256 challenge are required.')

    @bp.route('/oauth/authorize', methods=['GET', 'POST'])
    def authorize():
        with closing(connect(db)) as conn:
            g.oauth_conn = conn
            conn.execute('BEGIN IMMEDIATE')
            try:
                validate_parameters(authorization=True)
                grant = server.get_consent_grant(end_user=g.auth_user)
                callback = urlsplit(request.args['redirect_uri'])
                g.oauth_callback_origin = callback.scheme + '://' + callback.netloc
                fresh = conn.execute('SELECT * FROM auth_users WHERE id=?', (g.auth_user['id'],)).fetchone()
                from .auth import COOKIE
                active = conn.execute('SELECT 1 FROM auth_sessions WHERE token_hash=? AND user_id=? AND expires>?',
                    (fingerprint(request.cookies.get(COOKIE, '')), g.auth_user['id'], time.time())).fetchone()
                if not active or not fresh or fresh['role'] != 'owner': abort(401)
                proof = dict(query=fingerprint(request.query_string.hex()), user=g.auth_user['id'], version=user_version(conn, fresh))
                if request.method == 'GET':
                    return render_template('oauth_consent.html', client=grant.request.client,
                        callback=request.args['redirect_uri'], consent_ticket=signer.dumps(proof))
                try:
                    submitted = signer.loads(request.form.get('consent_ticket', ''), max_age=600)
                except BadSignature:
                    abort(400, 'The connection request expired. Start again in your client.')
                if submitted != proof: abort(400, 'The connection request changed. Start again in your client.')
                response = server.create_authorization_response(grant_user=g.auth_user if request.form.get('decision') == 'allow' else None, grant=grant)
                if response.status_code == 302:
                    from authlib.common.urls import add_params_to_uri
                    response.headers['Location'] = add_params_to_uri(response.headers['Location'], [('iss', g.oauth_issuer)])
                conn.commit()
                return response
            except OAuth2Error as exc:
                return jsonify(error=exc.error, error_description='Invalid connection request. Start again in your client.'), 400

    def credential_response(revocation=False):
        if request.mimetype != 'application/x-www-form-urlencoded' or request.args:
            return jsonify(error='invalid_request'), 400
        with closing(connect(db)) as conn:
            g.oauth_conn = conn
            conn.execute('BEGIN IMMEDIATE')
            try:
                if revocation:
                    if any(len(request.form.getlist(k)) != 1 for k in request.form): raise InvalidRequestError()
                    response = server.create_endpoint_response('revocation')
                else:
                    validate_parameters()
                    response = server.create_token_response()
                # Commit even OAuth errors: replay detection must persist family revocation.
                conn.commit()
                return response
            except OAuth2Error as exc:
                conn.commit()
                return jsonify(error=exc.error), 400

    @bp.post('/oauth/token')
    def token(): return credential_response()

    @bp.post('/oauth/revoke')
    def revoke(): return credential_response(revocation=True)

    @bp.get('/settings/connected-applications')
    def connections():
        with closing(connect(db)) as conn:
            grants = [dict(row) for row in conn.execute('SELECT g.*,c.name,c.redirect_uris FROM auth_oauth_grants g JOIN auth_oauth_clients c ON c.id=g.client_id WHERE g.user_id=? ORDER BY g.created DESC', (g.auth_user['id'],))]
            for grant in grants:
                grant['active'] = bool(grant_user(conn, grant))
                grant['callback'] = json.loads(grant['redirect_uris'])[0]
        return render_template('oauth_connections.html', grants=grants)

    @bp.post('/settings/connected-applications/<grant_id>/revoke')
    def disconnect(grant_id):
        with closing(connect(db)) as conn:
            conn.execute('UPDATE auth_oauth_grants SET revoked=1 WHERE id=? AND user_id=?', (grant_id, g.auth_user['id']))
            audit(conn, 'oauth_disconnected', g.auth_user['id'])
            conn.commit()
        return redirect(url_for('oauth.connections'))

    app.register_blueprint(bp)
