"""Exercise real OAuth registration, consent and code exchange in feature fixtures."""
from urllib.parse import urlencode, urlsplit, parse_qs
from bs4 import BeautifulSoup
from authlib.oauth2.rfc7636 import create_s256_code_challenge

ISSUER = 'https://localhost'
CALLBACK = 'https://client.example/oauth/callback'
VERIFIER = 'nightfeed-test-code-verifier-' + 'a' * 32


def register_client(app, **changes):
    response = app.test_client().post('/oauth/register', base_url=ISSUER,
        json=dict(client_name='Fixture application', redirect_uris=[CALLBACK], token_endpoint_auth_method='none', **changes))
    assert response.status_code == 201, response.data
    return response.json


def authorize(app, browser, client, **changes):
    values = dict(client_id=client['client_id'], redirect_uri=CALLBACK, response_type='code',
                  scope='nightfeed:access', resource=ISSUER + '/mcp', state='fixture-state',
                  code_challenge=create_s256_code_challenge(VERIFIER), code_challenge_method='S256')
    values.update(changes)
    path = '/oauth/authorize?' + urlencode(values)
    response = browser.get(path, base_url=ISSUER)
    assert response.status_code == 200, response.data
    ticket = BeautifulSoup(response.data, 'html.parser').select_one('[name=consent_ticket]')['value']
    response = browser.post(path, base_url=ISSUER, data=dict(consent_ticket=ticket, decision='allow'))
    assert response.status_code == 302, response.data
    params = parse_qs(urlsplit(response.location).query)
    assert params['state'] == ['fixture-state'] and params['iss'] == [ISSUER]
    return params['code'][0]


def exchange(app, client, code, **changes):
    values = dict(client_id=client['client_id'], grant_type='authorization_code', code=code,
                  redirect_uri=CALLBACK, resource=ISSUER + '/mcp', code_verifier=VERIFIER)
    values.update(changes)
    return app.test_client().post('/oauth/token', base_url=ISSUER, data=values)


def connect_client(app, browser, db):
    from contextlib import closing
    from rss_site_bridge.app import connect_db
    with closing(connect_db(db)) as conn:
        conn.execute('UPDATE assistant_config SET mcp_enabled=1,mcp_public_url=?', (ISSUER,))
        conn.commit()
    client = register_client(app)
    code = authorize(app, browser, client)
    response = exchange(app, client, code)
    assert response.status_code == 200, response.data
    return response.json['access_token']
