"""Private UI fixtures use actual onboarding and authenticated browser cookies."""
from pathlib import Path
from unittest.mock import patch
import os
from rss_site_bridge.app import create_app
from rss_site_bridge.auth import COOKIE, setup_token


def create_ui_app(config):
    assert config.get('TESTING'), 'UI fixtures must use a temporary testing app.'
    with patch.dict(os.environ, {'NIGHTFEED_SECURE_COOKIES': '0', 'NIGHTFEED_FORCE_LOGIN_FORM': '0'}):
        app = create_app(config)
    client = app.test_client()
    client.get('/auth/setup')
    with client.session_transaction() as state:
        csrf = state['csrf']
    password = 'isolated browser fixture passphrase'
    response = client.post('/auth/setup', data=dict(auth_csrf=csrf,
                           setup_token=setup_token(Path(config['DATABASE_PATH'])),
                           username='fixture-owner', name='Fixture owner', password=password, confirm_password=password))
    assert response.status_code == 302, response.data
    app.extensions['ui_auth_cookie'] = client.get_cookie(COOKIE).value
    return app


def authenticate_page(page, app, origin):
    page.context.add_cookies([dict(name=COOKIE, value=app.extensions['ui_auth_cookie'],
                                  url=origin, httpOnly=True, secure=False, sameSite='Lax')])
