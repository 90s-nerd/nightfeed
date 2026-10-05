"""Existing feature tests use real owner sessions and global CSRF protection."""
from flask.testing import FlaskClient
from rss_site_bridge.auth import setup_token, owner

PASSWORD = 'test-owner-passphrase-only'


class AuthenticatedClient(FlaskClient):
    def open(self, *args, **kwargs):
        method = kwargs.get('method', 'GET').upper()
        if method not in ('GET', 'HEAD', 'OPTIONS'):
            with self.session_transaction() as state:
                token = state.get('csrf')
            headers = dict(kwargs.pop('headers', {}) or {})
            if token:
                headers.setdefault('X-Nightfeed-CSRF', token)
            kwargs['headers'] = headers
        return super().open(*args, **kwargs)


def authenticated_client(app):
    previous = app.test_client_class
    app.test_client_class = AuthenticatedClient
    client = app.test_client()
    app.test_client_class = previous
    client.get('/auth/setup')
    db = app.config['DATABASE_PATH']
    if not owner(db):
        result = client.post('/auth/setup', data=dict(setup_token=setup_token(db), username='owner',
                             name='Test owner', password=PASSWORD, confirm_password=PASSWORD))
    else:
        client.get('/auth/login')
        result = client.post('/auth/login', data=dict(username='owner', password=PASSWORD))
    assert result.status_code == 302, result.data
    return client
