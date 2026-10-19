"""Expired app sessions resume through SSO; deliberate logout remains signed out.

The provider is a UI fixture. Signed-token/state validation is covered separately
by the OIDC protocol tests in test_auth.py.
"""
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import Mock, patch
import json
import sys
import time
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from flask import Flask, redirect
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server
from rss_site_bridge.auth import connect, owner, settings
from ui_auth_support import create_ui_app, authenticate_page

with TemporaryDirectory() as temp:
    db = Path(temp) / 'resume.db'
    app = create_ui_app({'DATABASE_PATH': db, 'START_SCHEDULER': False, 'TESTING': True})
    issuer = 'https://provider.test'
    cfg = settings(db) | dict(oidc_enabled=True, auto_login=True, disable_form=True,
        subject='fixture-owner', issuer=issuer, client_id='fixture',
        client_secret='', redirect_uri='https://localhost/auth/oidc/callback', discovery=True)
    # Supply an encrypted dummy client secret for constructing the client.
    from cryptography.fernet import Fernet
    from rss_site_bridge.auth import encryption_key
    cfg['client_secret'] = Fernet(encryption_key(db)).encrypt(b'fixture-secret').decode()
    with closing(connect(db)) as conn:
        conn.execute('UPDATE auth_config SET value=? WHERE id=1', (json.dumps(cfg),))
        conn.execute('INSERT INTO auth_identities VALUES(?,?,?)', (issuer, cfg['subject'], owner(db)['id']))
        conn.commit()
    provider = Mock()
    provider.load_server_metadata.return_value = dict(issuer=issuer,
        authorization_endpoint=issuer+'/authorize', token_endpoint=issuer+'/token',
        jwks_uri=issuer+'/jwks', id_token_signing_alg_values_supported=['RS256'])
    provider.authorize_access_token.return_value = {'userinfo': {'iss': issuer, 'sub': cfg['subject']}}
    server = make_server('127.0.0.1', 0, app, threaded=True)
    Thread(target=server.serve_forever, daemon=True).start()
    origin = f'http://127.0.0.1:{server.server_port}'
    provider_app = Flask('fixture-provider')
    visits = []
    @provider_app.get('/authorize')
    def authorize():
        visits.append('authorize')
        return redirect(origin+'/auth/oidc/callback?code=fixture')
    provider_server = make_server('127.0.0.1', 0, provider_app, threaded=True)
    Thread(target=provider_server.serve_forever, daemon=True).start()
    provider.authorize_redirect.side_effect = lambda *args, **kwargs: redirect(f'http://127.0.0.1:{provider_server.server_port}/authorize')
    try:
        with patch('authlib.integrations.flask_client.OAuth.register', return_value=provider), sync_playwright() as pw:
            browser = pw.chromium.launch(channel='chrome', headless=True)
            for width in (390, 1440):
                page = browser.new_page(viewport={'width': width, 'height': 850})
                authenticate_page(page, app, origin)
                with page.expect_response(lambda response: '/api/auth/session' in response.url and response.status == 200):
                    page.goto(origin+'/feeds')
                page.wait_for_timeout(100)
                visits.clear()
                # Resuming an expired Home Screen page should go through SSO once,
                # automatically return to /feeds, and establish a fresh app session.
                with closing(connect(db)) as conn:
                    conn.execute('UPDATE auth_sessions SET expires=?', (time.time()-1,))
                    conn.commit()
                with page.expect_navigation(url=origin+'/feeds'):
                    with page.expect_response(lambda response: '/api/auth/session' in response.url and response.status == 401):
                        page.evaluate("window.dispatchEvent(new Event('focus'))")
                expect(page.get_by_role('heading', name='Feeds', exact=True)).to_be_visible(timeout=15000)
                expect(page.locator('meta[name=auth-session-status]')).to_have_count(1)
                assert len(visits) == 1, visits
                assert page.url == origin+'/feeds', page.url
                # Local logout has no provider logout URL: do not silently sign back in.
                page.locator('.account-menu > summary').click()
                page.get_by_role('button', name='Log out', exact=True).click()
                expect(page.get_by_role('status')).to_contain_text('You’re signed out of Nightfeed.')
                page.goto(origin+'/feeds')
                expect(page.get_by_role('heading', name='Sign in to Nightfeed')).to_be_visible()
                expect(page.get_by_label('Password', exact=True)).to_have_count(0)
                assert len(visits) == 1
                page.get_by_role('link', name=cfg['button_text'], exact=True).click()
                page.wait_for_url(origin+'/feeds')
                assert len(visits) == 2
                assert page.evaluate("async () => (await fetch('/api/auth/session')).status") == 200
                # When auto redirect is disabled, expiration shows the SSO button.
                with closing(connect(db)) as conn:
                    conn.execute('UPDATE auth_config SET value=? WHERE id=1', (json.dumps(cfg | {'auto_login': False}),))
                    conn.execute('UPDATE auth_sessions SET expires=?', (time.time()-1,))
                    conn.commit()
                page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted:true}))")
                expect(page.get_by_role('heading', name='Sign in to Nightfeed')).to_be_visible()
                assert len(visits) == 2
                with closing(connect(db)) as conn:
                    conn.execute('UPDATE auth_config SET value=? WHERE id=1', (json.dumps(cfg),))
                    conn.commit()
                page.close()
            browser.close()
    finally:
        server.shutdown()
        provider_server.shutdown()
print('PASS: mobile/desktop automatic SSO resumption, destination, explicit logout, manual return, and disabled auto redirect.')
