"""Authentication boundary, revocation, scoped credentials and OIDC protocol tests."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlsplit
import base64
import json
import os
import time
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup
from cryptography.fernet import Fernet
from joserfc import jwt
from joserfc.jwk import RSAKey
from requests import Response

from rss_site_bridge.app import create_app, create_profile, create_notification, FeedRequest
from rss_site_bridge.auth import (COOKIE, DEFAULTS, PUBLIC, SCOPES, connect, encryption_key,
                                  fingerprint, owner, password_hash, safe_next, settings, setup_token)

PASSWORD = 'a unique owner passphrase'


class AuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hashed = password_hash(PASSWORD)

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'feed.db'
        self.environment = patch.dict(os.environ, {'NIGHTFEED_FORCE_LOGIN_FORM': '0',
                                                   'NIGHTFEED_SECURE_COOKIES': '1',
                                                   'NIGHTFEED_TRUSTED_PROXY_HOPS': '0',
                                                   'NIGHTFEED_TRUSTED_PROXIES': ''})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.app = create_app(dict(TESTING=True, DATABASE_PATH=self.db, START_SCHEDULER=False))
        self.client = self.app.test_client()
        self.profile = create_profile(self.db, FeedRequest('PRIVATE_FEED_SENTINEL', 'https://example.com',
                                      'article', 'a', 'a', '', 10, 60, 'http'))
        self.other = create_profile(self.db, FeedRequest('OTHER_PRIVATE_FEED', 'https://example.com',
                                    'article', 'a', 'a', '', 10, 60, 'http'))
        with closing(connect(self.db)) as conn:
            conn.execute('INSERT INTO feed_items(profile_id,title,link,summary,discovered_at) VALUES(?,?,?,?,?)',
                         (self.profile.id, 'PRIVATE_TOPIC_SENTINEL', 'https://example.com/topic', 'PRIVATE_SUMMARY', '2026-01-01'))
            conn.execute('INSERT INTO feed_items(profile_id,title,link,summary,discovered_at) VALUES(?,?,?,?,?)',
                         (self.other.id, 'OTHER_PRIVATE_TOPIC', 'https://example.com/other', '', '2026-01-01'))
            conn.commit()
        create_notification(self.db, profile_id=self.profile.id, event_type='refresh', severity='info',
                            category='app', title='PRIVATE_NOTIFICATION_SENTINEL', message='Private details')

    def cfg(self, **values):
        cfg = settings(self.db) | values
        with closing(connect(self.db)) as conn:
            conn.execute('UPDATE auth_config SET value=? WHERE id=1', (json.dumps(cfg),))
            conn.commit()
        return cfg

    def csrf(self, client=None):
        client = client or self.client
        with client.session_transaction() as state:
            return state.get('csrf', '')

    def post(self, path, data=None, client=None, headers=None, **kwargs):
        client = client or self.client
        return client.post(path, data=data, headers={'X-Nightfeed-CSRF': self.csrf(client)} | (headers or {}), **kwargs)

    def onboard(self):
        self.client.get('/auth/setup')
        token = setup_token(self.db)
        with patch('rss_site_bridge.auth.password_hash', return_value=self.hashed):
            result = self.post('/auth/setup', dict(username='owner', name='Owner', setup_token=token,
                              password=PASSWORD, confirm_password=PASSWORD))
        self.assertEqual(result.status_code, 302)
        return result

    def login(self, client=None, password=PASSWORD, **kwargs):
        client = client or self.client
        client.get('/auth/login?sso=off')
        return self.post('/auth/login', dict(username='owner', password=password), client=client, **kwargs)

    def make_key(self, scopes=('rss:read',), feeds=None, expires=None):
        data = dict(name='Reader', scopes=list(scopes), current_password=PASSWORD,
                    feed_ids=[str(v) for v in feeds or []], expires=expires or '')
        result = self.post('/settings/api-keys', data)
        self.assertEqual(result.status_code, 200, result.data)
        secret = next(v.get_text() for v in BeautifulSoup(result.data, 'html.parser').find_all('code') if v.get_text().startswith('nf_') and '…' not in v.get_text())
        return secret

    def headers(self, token):
        return {'Authorization': 'Bearer ' + token}

    def test_profile_local_name_policy_and_validation(self):
        self.onboard()
        self.assertEqual(self.post('/settings/profile', dict(display_name='New owner')).status_code, 200)
        self.assertEqual(owner(self.db)['name'], 'New owner')
        for value in ('', 'x' * 101, 'Name\nInjected'):
            self.assertEqual(self.post('/settings/profile', dict(display_name=value)).status_code, 400)
        self.assertEqual(self.post('/settings/profile', dict(oidc_use_name='on')).status_code, 400)
        self.assertEqual(self.client.post('/settings/profile', data=dict(display_name='Forged')).status_code, 403)
        self.cfg(oidc_enabled=True, subject='owner-subject')
        with closing(connect(self.db)) as conn:
            conn.execute("UPDATE auth_users SET oidc_name='Provider owner'")
            conn.commit()
        response = self.post('/settings/profile', dict(oidc_use_name='on', display_name='Forged name'))
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Provider owner', response.data)
        self.assertEqual(owner(self.db)['name'], 'New owner')
        editor = BeautifulSoup(response.data, 'html.parser').find('input', attrs={'name': 'display_name'})
        self.assertTrue(editor.has_attr('readonly'))
        self.assertEqual(self.post('/settings/profile', dict(display_name='Restored local')).status_code, 200)
        self.assertEqual(owner(self.db)['name'], 'Restored local')
        self.assertFalse(settings(self.db)['oidc_use_name'])

    def test_profile_is_denied_to_api_key(self):
        self.onboard()
        secret = self.make_key(scopes=tuple(SCOPES))
        client = self.app.test_client()
        self.assertEqual(client.get('/settings/profile', headers=self.headers(secret)).status_code, 403)
        self.assertEqual(client.post('/settings/profile', headers=self.headers(secret), data=dict(display_name='Forged')).status_code, 403)

    def test_existing_auth_database_migrates_provider_name(self):
        from rss_site_bridge.auth import initialize
        self.onboard()
        with closing(connect(self.db)) as conn:
            conn.execute('ALTER TABLE auth_users DROP COLUMN oidc_name')
            conn.commit()
        initialize(self.db)
        self.assertEqual(owner(self.db)['oidc_name'], '')
        self.assertEqual(owner(self.db)['name'], 'Owner')

    def test_upgrade_blocks_all_routes_before_and_after_owner_setup(self):
        # Inspect every registered route, including blueprints and all HTTP methods.
        def check():
            client = self.app.test_client()
            for rule in self.app.url_map.iter_rules():
                if rule.endpoint in PUBLIC:
                    continue
                path = rule.rule
                replacements = {'profile_id': str(self.profile.id), 'feed_id': str(self.profile.id),
                                'item_id': '1', 'notification_id': '1', 'key_id': '1',
                                'token': self.profile.feed_token, 'session_id': 'private-session',
                                'download_id': 'download', 'job_id': 'job', 'identity': '1', 'downloader_id': '1'}
                import re
                path = re.sub(r'<(?:(?:int|string|path):)?(\w+)>', lambda m: replacements.get(m[1], '1'), path)
                for method in rule.methods - {'OPTIONS'}:
                    with self.subTest(endpoint=rule.endpoint, method=method):
                        response = client.open(path, method=method)
                        self.assertIn(response.status_code, (301, 302, 401))
                        self.assertNotIn(b'PRIVATE_', response.data)
                        self.assertEqual(response.headers['Cache-Control'], 'no-store, private')
        check()
        self.onboard()
        check()

    def test_setup_requires_server_token_and_cannot_replace_owner(self):
        self.client.get('/auth/setup')
        self.assertEqual(self.post('/auth/setup', dict(setup_token='bad')).status_code, 400)
        self.assertIsNone(owner(self.db))
        token = setup_token(self.db)
        self.onboard()
        self.assertFalse(self.db.with_suffix('.setup-token').exists())
        result = self.post('/auth/setup', dict(setup_token=token, username='intruder'))
        self.assertEqual(result.status_code, 302)
        self.assertEqual(owner(self.db)['username'], 'owner')

    def test_password_policy_hash_and_no_secret_echo(self):
        for value in ('short', 'x' * 257):
            with self.assertRaises(ValueError):
                password_hash(value)
        self.onboard()
        self.assertTrue(owner(self.db)['password_hash'].startswith('scrypt:131072:8:1$'))
        self.assertNotIn(PASSWORD, self.client.get('/settings/security').get_data(as_text=True))

    def test_session_cookies_and_security_headers(self):
        response = self.onboard()
        cookie = response.headers.getlist('Set-Cookie')[0]
        self.assertIn('HttpOnly', cookie)
        self.assertIn('Secure', cookie)
        self.assertIn('SameSite=Lax', cookie)
        response = self.client.get('/', base_url='https://localhost')
        self.assertEqual(response.status_code, 200)
        self.assertIn('max-age=', response.headers['Strict-Transport-Security'])
        self.assertEqual(response.headers['Referrer-Policy'], 'strict-origin')
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        with closing(connect(self.db)) as conn:
            row = conn.execute('SELECT * FROM auth_sessions').fetchone()
        self.assertEqual(row['token_hash'], fingerprint(self.client.get_cookie(COOKIE).value))
        self.assertNotEqual(row['token_hash'], self.client.get_cookie(COOKIE).value)

    def test_global_csrf_blocks_missing_foreign_cross_origin_and_unicode_tokens(self):
        self.onboard()
        other = self.app.test_client()
        self.login(other)
        for headers in ({}, {'X-Nightfeed-CSRF': self.csrf(other)}, {'X-Nightfeed-CSRF': 'é'},
                        {'X-Nightfeed-CSRF': self.csrf(), 'Origin': 'https://evil.test'},
                        {'X-Nightfeed-CSRF': self.csrf(), 'Sec-Fetch-Site': 'cross-site'}):
            result = self.client.post(f'/profiles/{self.profile.id}/delete', headers=headers)
            self.assertEqual(result.status_code, 403)
        self.assertEqual(self.post(f'/profiles/{self.profile.id}/toggle-active').status_code, 302)

    def test_all_html_post_forms_have_session_csrf(self):
        self.onboard()
        for path in ('/', '/feeds', '/compose', f'/profiles/{self.profile.id}', '/notifications', '/settings',
                     '/settings/downloaders', '/settings/security', '/settings/password', '/settings/api-keys', '/settings/profile'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            for form in BeautifulSoup(response.data, 'html.parser').find_all('form', method='post'):
                self.assertIsNotNone(form.find('input', attrs={'name': 'auth_csrf'}), (path, form))

    def test_absolute_idle_and_configured_shorter_session_expiry(self):
        self.onboard()
        token = fingerprint(self.client.get_cookie(COOKIE).value)
        for column, value in [('expires', time.time() - 1), ('last_seen', time.time() - 1900), ('created', time.time() - 44000)]:
            with closing(connect(self.db)) as conn:
                conn.execute('UPDATE auth_sessions SET expires=?,last_seen=?,created=? WHERE token_hash=?',
                             (time.time() + 1000, time.time(), time.time(), token))
                conn.execute(f'UPDATE auth_sessions SET {column}=? WHERE token_hash=?', (value, token))
                conn.commit()
            self.assertEqual(self.client.get('/').status_code, 302)

    def test_passive_session_check_does_not_extend_idle_timeout(self):
        self.onboard();token=fingerprint(self.client.get_cookie(COOKIE).value)
        before=time.time()-100
        with closing(connect(self.db)) as conn:
            conn.execute('UPDATE auth_sessions SET last_seen=? WHERE token_hash=?',(before,token));conn.commit()
        response=self.client.get('/api/auth/session')
        self.assertEqual(response.status_code,200);self.assertTrue(response.json['authenticated'])
        self.assertGreater(response.json['expires_in'],0)
        self.assertIn('no-store',response.headers['Cache-Control'])
        with closing(connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT last_seen FROM auth_sessions WHERE token_hash=?',(token,)).fetchone()[0],before)
            conn.execute('UPDATE auth_sessions SET last_seen=? WHERE token_hash=?',(time.time()-1900,token));conn.commit()
        self.assertEqual(self.client.get('/api/auth/session').status_code,401)
        self.assertEqual(self.app.test_client().get('/api/auth/session').status_code,401)

    def test_logout_revokes_replayed_cookie_and_csrf(self):
        self.onboard()
        old = self.client.get_cookie(COOKIE).value
        response = self.post('/auth/logout')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['Clear-Site-Data'], '"cache"')
        self.client.set_cookie(COOKIE, old)
        self.assertEqual(self.client.get('/').status_code, 302)

    def test_notification_click_requires_manual_login_then_opens_report(self):
        self.onboard()
        self.cfg(oidc_enabled=True, auto_login=True)
        for mode in ('logout', 'expired'):
            with self.subTest(mode=mode):
                notification = create_notification(self.db, profile_id=self.profile.id, event_type='refresh',
                    severity='info', category='app', title='Private report ' + mode, message='Private report details')
                target = f'/notifications/{notification.id}'
                if mode == 'logout':
                    self.post('/auth/logout')
                else:
                    with closing(connect(self.db)) as conn:
                        conn.execute('UPDATE auth_sessions SET expires=?', (time.time() - 1,))
                        conn.commit()
                response = self.client.get(target)
                self.assertEqual(response.status_code, 302)
                params = parse_qs(urlsplit(response.location).query)
                self.assertEqual(params['next'], [target])
                self.assertEqual(params['sso'], ['off'])
                page = self.client.get(response.location)
                self.assertEqual(page.status_code, 200)
                self.assertNotIn(b'Private report details', page.data)
                with closing(connect(self.db)) as conn:
                    self.assertIsNone(conn.execute('SELECT read_at FROM notifications WHERE id=?', (notification.id,)).fetchone()['read_at'])
                signed_in = self.post(response.location, dict(username='owner', password=PASSWORD, next=target))
                self.assertEqual(signed_in.location, target)
                report = self.client.get(target)
                self.assertEqual(report.status_code, 200)
                self.assertIn(b'Private report details', report.data)

    def test_password_change_requires_current_and_revokes_all_other_sessions(self):
        self.onboard()
        other = self.app.test_client()
        self.login(other)
        old = self.client.get_cookie(COOKIE).value
        self.assertEqual(self.post('/auth/password', dict(current_password='wrong')).status_code, 400)
        new = 'new independent passphrase'
        result = self.post('/auth/password', dict(current_password=PASSWORD, new_password=new, confirm_password=new))
        self.assertEqual(result.status_code, 302)
        self.assertNotEqual(self.client.get_cookie(COOKIE).value, old)
        self.assertEqual(other.get('/').status_code, 302)
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertEqual(self.login(other).status_code, 401)
        self.assertEqual(self.login(other, password=new).status_code, 302)

    def test_redirects_cannot_escape_origin(self):
        for value in ('//evil.test', 'https://evil.test', '/\\evil.test', '/\nLocation:x', '//[', ''):
            self.assertEqual(safe_next(value), '/')
        self.assertEqual(safe_next('/feeds?page=2'), '/feeds?page=2')
        self.onboard()
        client = self.app.test_client()
        client.get('/auth/login')
        result = self.post('/auth/login', dict(username='owner', password=PASSWORD, next='//evil.test'), client=client)
        self.assertEqual(result.location, '/')

    def test_timed_lockout_automatic_unlock_and_account_wide_limit(self):
        self.onboard()
        client = self.app.test_client()
        self.cfg(lockout_attempts=3)
        for _ in range(3):
            self.assertEqual(self.login(client, password='wrong').status_code, 401)
        self.assertEqual(self.login(client, environ_overrides={'REMOTE_ADDR': '198.51.100.7'}).status_code, 401)
        with closing(connect(self.db)) as conn:
            conn.execute('UPDATE auth_failures SET locked_until=?,started=?', (time.time() - 1, time.time() - 1000))
            conn.commit()
        self.assertEqual(self.login(client).status_code, 302)

    def test_manual_lockout_exclusion_and_authenticated_unlock(self):
        self.onboard()
        self.cfg(lockout_attempts=3, auto_unlock=False, lockout_exclusions='192.168.50.0/24')
        client = self.app.test_client()
        for _ in range(3):
            self.login(client, password='wrong')
        self.assertEqual(self.login(client).status_code, 401)
        self.assertEqual(self.login(client, environ_overrides={'REMOTE_ADDR': '192.168.50.2'}).status_code, 302)
        self.assertEqual(self.post('/auth/unlock').status_code, 302)
        fresh = self.app.test_client()
        self.assertEqual(self.login(fresh).status_code, 302)

    def test_forwarded_headers_only_from_configured_proxy(self):
        # Remove env overrides to exercise the settings menu configuration.
        os.environ.pop('NIGHTFEED_TRUSTED_PROXY_HOPS')
        os.environ.pop('NIGHTFEED_TRUSTED_PROXIES')
        self.cfg(proxy_hops=1, trusted_proxies='10.10.0.2/32')
        observed = []
        @self.app.get('/test/proxy')
        def proxy():
            from flask import request
            observed.append((request.remote_addr, request.is_secure, request.host))
            return 'ok'
        self.onboard()
        headers = {'X-Forwarded-For': '192.168.50.2', 'X-Forwarded-Proto': 'https', 'X-Forwarded-Host': 'evil.test'}
        self.client.get('/test/proxy', headers=headers, environ_overrides={'REMOTE_ADDR': '198.51.100.7'})
        self.client.get('/test/proxy', headers=headers, environ_overrides={'REMOTE_ADDR': '10.10.0.2'})
        self.assertEqual(observed, [('198.51.100.7', False, 'localhost'), ('192.168.50.2', True, 'localhost')])

    def test_invalid_unicode_login_is_generic(self):
        self.onboard()
        client = self.app.test_client()
        client.get('/auth/login')
        result = self.post('/auth/login', dict(username='☃', password='wrong'), client=client)
        self.assertEqual(result.status_code, 401)
        self.assertNotIn(b'unknown user', result.data.lower())

    def test_rss_key_least_privilege_feed_limit_transport_and_no_secret_storage(self):
        self.onboard()
        token = self.make_key(feeds=[self.profile.id])
        client = self.app.test_client()
        path = f'/feeds/{self.profile.feed_token}.xml'
        for headers in (self.headers(token), {'X-API-Key': token},
                        {'Authorization': 'Basic ' + base64.b64encode(('apikey:' + token).encode()).decode()}):
            self.assertEqual(client.get(path, headers=headers).status_code, 200)
            self.assertEqual(client.get('/settings', headers=headers).status_code, 403)
            self.assertEqual(client.get('/api/v1/topics', headers=headers).status_code, 403)
            self.assertEqual(client.get(f'/feeds/{self.other.feed_token}.xml', headers=headers).status_code, 403)
            self.assertEqual(client.post('/auth/password', headers=headers).status_code, 403)
        self.assertEqual(client.get(path + '?api_key=' + token).status_code, 401)
        with closing(connect(self.db)) as conn:
            row = conn.execute('SELECT * FROM auth_api_keys').fetchone()
        self.assertEqual(row['token_hash'], fingerprint(token))
        self.assertIsNone(row['expires'])
        self.assertNotIn(token, self.client.get('/settings/api-keys').get_data(as_text=True))

    def test_key_expiry_and_revocation_apply_immediately(self):
        self.onboard()
        future = (datetime.now(timezone.utc) + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
        token = self.make_key(expires=future)
        path = f'/feeds/{self.profile.feed_token}.xml'
        client = self.app.test_client()
        self.assertEqual(client.get(path, headers=self.headers(token)).status_code, 200)
        with closing(connect(self.db)) as conn:
            conn.execute('UPDATE auth_api_keys SET expires=?', (time.time() - 1,))
            conn.commit()
        self.assertEqual(client.get(path, headers=self.headers(token)).status_code, 401)
        token = self.make_key()
        self.assertEqual(self.post('/auth/api-keys/2/revoke').status_code, 302)
        self.assertEqual(client.get(path, headers=self.headers(token)).status_code, 401)

    def test_api_permissions_limit_records_and_writes(self):
        self.onboard()
        token = self.make_key(scopes=('feeds:read', 'topics:read', 'notifications:read'), feeds=[self.profile.id])
        client = self.app.test_client()
        for path, sentinel in [('/api/v1/feeds', 'PRIVATE_FEED'), ('/api/v1/topics', 'PRIVATE_TOPIC'), ('/api/v1/notifications', 'PRIVATE_NOTIFICATION')]:
            result = client.get(path, headers=self.headers(token))
            self.assertEqual(result.status_code, 200)
            self.assertIn(sentinel, result.get_data(as_text=True))
            self.assertNotIn('OTHER_PRIVATE', result.get_data(as_text=True))
        self.assertEqual(client.post(f'/api/v1/feeds/{self.profile.id}/refresh', headers=self.headers(token)).status_code, 403)
        refresh = self.make_key(scopes=('feeds:refresh',), feeds=[self.profile.id])
        with patch('rss_site_bridge.app.refresh_profile', return_value={'new': 1}) as run:
            self.assertEqual(client.post(f'/api/v1/feeds/{self.profile.id}/refresh', headers=self.headers(refresh)).status_code, 200)
            self.assertEqual(client.post(f'/api/v1/feeds/{self.other.id}/refresh', headers=self.headers(refresh)).status_code, 403)
            run.assert_called_once()

    def test_invalid_key_never_falls_back_to_owner_session(self):
        self.onboard()
        for header in ({'Authorization': 'Bearer bad'}, {'Authorization': 'Bearer'}, {'X-API-Key': ''}):
            self.assertEqual(self.client.get('/', headers=header).status_code, 401)

    def test_new_private_route_is_denied_to_every_api_scope(self):
        @self.app.get('/future/feature')
        def future():
            return 'private'
        self.onboard()
        token = self.make_key(scopes=tuple(SCOPES))
        self.assertEqual(self.app.test_client().get('/future/feature', headers=self.headers(token)).status_code, 403)

    def test_security_changes_require_password_and_revoke_other_sessions(self):
        self.onboard()
        other = self.app.test_client()
        self.login(other)
        data = {k: str(v) for k, v in DEFAULTS.items() if not isinstance(v, bool)}
        data.update(auto_unlock='on', discovery='on', current_password='wrong')
        self.assertEqual(self.post('/settings/security', data).status_code, 400)
        data.update(current_password=PASSWORD, trusted_proxies='10.0.0.2', proxy_hops='1')
        self.assertEqual(self.post('/settings/security', data).status_code, 200)
        self.assertEqual(other.get('/').status_code, 302)
        self.assertEqual(settings(self.db)['trusted_proxies'], '10.0.0.2/32')

    def test_oidc_ui_disable_autologin_and_server_force_override(self):
        self.onboard()
        self.cfg(oidc_enabled=True, auto_login=True, disable_form=True)
        client = self.app.test_client()
        self.assertIn('/auth/oidc', client.get('/auth/login').location)
        page = client.get('/auth/login?sso=off')
        self.assertNotIn(b'name="password"', page.data)
        self.assertEqual(self.post('/auth/login', dict(username='owner', password=PASSWORD), client=client).status_code, 403)
        with patch.dict(os.environ, {'NIGHTFEED_FORCE_LOGIN_FORM': '1'}):
            app = create_app(dict(TESTING=True, DATABASE_PATH=self.db, START_SCHEDULER=False))
        forced = app.test_client()
        self.assertIn(b'name="password"', forced.get('/auth/login').data)
        self.assertEqual(self.post('/auth/login', dict(username='owner', password=PASSWORD), client=forced).status_code, 302)


class OIDCTests(unittest.TestCase):
    setUpClass = classmethod(AuthTests.setUpClass.__func__)
    setUp = AuthTests.setUp
    cfg = AuthTests.cfg
    csrf = AuthTests.csrf
    post = AuthTests.post
    onboard = AuthTests.onboard
    # Only these protocol tests need the provider; inherited boundary tests stay in AuthTests.
    def configure(self, discovery=True):
        self.onboard()
        self.signing_key = RSAKey.generate_key(2048)
        self.signing_key.ensure_kid()
        self.claim_changes = {}
        self.provider_changes = {}
        self.requests = []
        key = encryption_key(self.db)
        self.cfg(oidc_enabled=True, discovery=discovery, issuer='https://provider.test', client_id='nightfeed',
                 client_secret=Fernet(key).encrypt(b'private-client-secret').decode(), subject='owner-subject',
                 redirect_uri='https://localhost/auth/oidc/callback', authorization_endpoint='https://provider.test/authorize',
                 token_endpoint='https://provider.test/token', jwks_uri='https://provider.test/jwks')
        with closing(connect(self.db)) as conn:
            conn.execute('INSERT INTO auth_identities VALUES(?,?,?)', ('https://provider.test', 'owner-subject', owner(self.db)['id']))
            conn.commit()
        self.provider_patch = patch('requests.sessions.Session.request', self.provider_request)
        self.provider_patch.start()
        self.addCleanup(self.provider_patch.stop)

    def provider_request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        if url.endswith('/.well-known/openid-configuration'):
            value = dict(issuer='https://provider.test', authorization_endpoint='https://provider.test/authorize',
                         token_endpoint='https://provider.test/token', jwks_uri='https://provider.test/jwks',
                         id_token_signing_alg_values_supported=['RS256']) | self.provider_changes
        elif url.endswith('/jwks'):
            value = {'keys': [self.signing_key.as_dict(private=False)]}
        elif url.endswith('/token'):
            claims = dict(iss='https://provider.test', sub='owner-subject', aud='nightfeed', exp=int(time.time()) + 300,
                          iat=int(time.time()), nonce=self.nonce) | self.claim_changes
            value = dict(access_token='private-access-token', token_type='Bearer',
                         id_token=jwt.encode({'alg': 'RS256', 'kid': self.signing_key.kid}, claims, self.signing_key))
        else:
            raise AssertionError('Unexpected provider request ' + url)
        response = Response()
        response.status_code = 200
        response._content = json.dumps(value).encode()
        response.headers['Content-Type'] = 'application/json'
        response.url = url
        return response

    def start(self):
        self.oidc_browser = self.app.test_client()
        response = self.oidc_browser.get('/auth/oidc?next=/feeds')
        self.assertEqual(response.status_code, 302, response.data)
        params = parse_qs(urlsplit(response.location).query)
        self.state, self.nonce = params['state'][0], params['nonce'][0]
        self.assertEqual(params['code_challenge_method'], ['S256'])
        self.assertTrue(params['code_challenge'][0])
        self.assertNotIn('private-client-secret', response.location)
        return params

    def callback(self, state=None):
        return self.oidc_browser.get('/auth/oidc/callback', query_string=dict(state=state or self.state, code='private-auth-code'))

    def test_protocol_valid_signed_owner_token_and_one_use_state(self):
        self.configure()
        self.start()
        response = self.callback()
        self.assertEqual(response.status_code, 302, response.data)
        self.assertEqual(response.location, '/feeds')
        self.assertEqual(self.oidc_browser.get('/').status_code, 200)
        calls = [v for v in self.requests if v[1].endswith('/token')]
        self.assertIn('code_verifier', str(calls))
        self.assertEqual(self.callback().status_code, 401)

    def test_protocol_rejects_wrong_state_before_token_exchange(self):
        self.configure()
        self.start()
        self.assertEqual(self.callback(state='forged').status_code, 401)
        self.assertFalse(any(v[1].endswith('/token') for v in self.requests))

    def test_verified_provider_name_refresh_and_missing_claim(self):
        self.configure()
        self.cfg(oidc_use_name=True)
        self.claim_changes = {'name': 'SSO Owner <script>'}
        self.start()
        self.assertEqual(self.callback().status_code, 302)
        self.assertEqual(owner(self.db)['oidc_name'], 'SSO Owner <script>')
        self.assertEqual(owner(self.db)['name'], 'Owner')
        response = self.oidc_browser.get('/settings/profile')
        self.assertIn(b'SSO Owner &lt;script&gt;', response.data)
        self.assertNotIn(b'SSO Owner <script>', response.data)
        for value in (None, 42, 'x' * 101, 'Control\nName'):
            self.claim_changes = {'name': value}
            self.start()
            self.assertEqual(self.callback().status_code, 302)
            self.assertEqual(owner(self.db)['oidc_name'], 'SSO Owner <script>')
        self.claim_changes = {'name': 'Untrusted name', 'sub': 'other-user'}
        self.start()
        self.assertEqual(self.callback().status_code, 401)
        self.assertEqual(owner(self.db)['oidc_name'], 'SSO Owner <script>')

    def test_protocol_rejects_bad_claims(self):
        self.configure()
        for changes in ({'nonce': 'wrong'}, {'iss': 'https://evil.test'}, {'aud': 'other'},
                        {'sub': 'other-user'}, {'exp': int(time.time()) - 3600}):
            with self.subTest(changes=changes):
                self.claim_changes = changes
                self.start()
                self.assertEqual(self.callback().status_code, 401)
                self.assertEqual(self.oidc_browser.get('/').status_code, 302)

    def test_protocol_rejects_tampered_signature(self):
        self.configure()
        self.start()
        bad_key = RSAKey.generate_key(2048)
        original = jwt.encode
        with patch('test_auth.jwt.encode', side_effect=lambda head, claims, key: original(head, claims, bad_key)):
            self.assertEqual(self.callback().status_code, 401)

    def test_protocol_manual_endpoints_work(self):
        self.configure(discovery=False)
        self.start()
        self.assertEqual(self.callback().status_code, 302)
        self.assertFalse(any('.well-known' in v[1] for v in self.requests))

    def test_protocol_rejects_metadata_issuer_and_insecure_endpoints(self):
        self.configure()
        for changes in ({'issuer': 'https://evil.test'}, {'token_endpoint': 'http://provider.test/token'},
                        {'id_token_signing_alg_values_supported': ['none', 'HS256']}):
            self.provider_changes = changes
            self.assertEqual(self.app.test_client().get('/auth/oidc').status_code, 502)

    def test_protocol_configuration_change_and_stale_callback(self):
        self.configure()
        self.start()
        self.cfg(subject='replacement-owner')
        self.assertEqual(self.callback().status_code, 401)
        self.cfg(subject='owner-subject')
        self.start()
        with self.oidc_browser.session_transaction() as state:
            state['oidc_started'] = time.time() - 700
        self.assertEqual(self.callback().status_code, 401)

    def test_protocol_owner_can_link_subject_after_password_confirmation(self):
        self.configure()
        self.cfg(subject='')
        with closing(connect(self.db)) as conn:
            conn.execute('DELETE FROM auth_identities')
            conn.commit()
        self.oidc_browser = self.client
        response = self.post('/auth/oidc/link', dict(current_password=PASSWORD))
        self.assertEqual(response.status_code, 302)
        params = parse_qs(urlsplit(response.location).query)
        self.state, self.nonce = params['state'][0], params['nonce'][0]
        self.assertEqual(self.callback().status_code, 302)
        self.assertEqual(settings(self.db)['subject'], 'owner-subject')
        self.start()
        self.assertEqual(self.callback().status_code, 302)

    def test_protocol_link_cannot_be_started_without_owner_session(self):
        self.configure()
        self.assertEqual(self.app.test_client().post('/auth/oidc/link').status_code, 401)
        self.assertEqual(self.post('/auth/oidc/link', dict(current_password='wrong')).status_code, 400)

    def test_protocol_link_rechecks_session_after_provider_exchange(self):
        self.configure()
        self.oidc_browser = self.client
        response = self.post('/auth/oidc/link', dict(current_password=PASSWORD))
        params = parse_qs(urlsplit(response.location).query)
        self.state, self.nonce = params['state'][0], params['nonce'][0]
        original = self.provider_request
        def revoked(method, url, **kwargs):
            if url.endswith('/token'):
                with closing(connect(self.db)) as conn:
                    conn.execute('DELETE FROM auth_sessions')
                    conn.commit()
            return original(method, url, **kwargs)
        with patch('requests.sessions.Session.request', revoked):
            self.assertEqual(self.callback().status_code, 401)
