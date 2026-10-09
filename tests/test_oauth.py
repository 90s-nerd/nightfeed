"""OAuth boundary and lifecycle tests; no live accounts or external services."""
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import time
import unittest
from urllib.parse import urlencode, parse_qs, urlsplit
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

from bs4 import BeautifulSoup
from werkzeug.datastructures import MultiDict

from auth_support import authenticated_client
from oauth_support import ISSUER, CALLBACK, VERIFIER, register_client, authorize, exchange
from rss_site_bridge import app as core
from rss_site_bridge.auth import fingerprint
from rss_site_bridge.oauth import validate_issuer


class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'db'
        self.app = core.create_app(dict(TESTING=True, DATABASE_PATH=self.db, START_SCHEDULER=False))
        self.browser = authenticated_client(self.app)
        self.client = self.app.test_client()
        self.sql('UPDATE assistant_config SET mcp_enabled=1,mcp_public_url=?', (ISSUER,))

    def sql(self, query, args=()):
        with closing(core.connect_db(self.db)) as conn:
            result = conn.execute(query, args).fetchall()
            conn.commit()
            return result

    def connection(self):
        client = register_client(self.app)
        code = authorize(self.app, self.browser, client)
        response = exchange(self.app, client, code)
        self.assertEqual(response.status_code, 200, response.data)
        return client, response.json

    def mcp(self, raw='', method='ping', params=None, **kwargs):
        headers = {'Accept': 'application/json, text/event-stream'}
        if raw: headers['Authorization'] = 'Bearer ' + raw
        headers.update(kwargs.pop('headers', {}))
        return self.client.post('/mcp', base_url=ISSUER, headers=headers,
            json=dict(jsonrpc='2.0', id=1, method=method, params=params or {}), **kwargs)

    def refresh(self, client, token, **changes):
        values = dict(client_id=client['client_id'], grant_type='refresh_token', refresh_token=token['refresh_token'], resource=ISSUER + '/mcp')
        values.update(changes)
        return self.client.post('/oauth/token', base_url=ISSUER, data=values)

    def test_discovery_challenge_and_fixed_origin(self):
        response = self.mcp()
        self.assertEqual(response.status_code, 401)
        self.assertIn('resource_metadata="' + ISSUER, response.headers['WWW-Authenticate'])
        resource = self.client.get('/.well-known/oauth-protected-resource/mcp', base_url=ISSUER)
        self.assertEqual(resource.json['resource'], ISSUER + '/mcp')
        metadata = self.client.get('/.well-known/oauth-authorization-server', base_url=ISSUER).json
        self.assertEqual(metadata['code_challenge_methods_supported'], ['S256'])
        self.assertTrue(metadata['authorization_response_iss_parameter_supported'])
        self.assertEqual(self.client.get('/.well-known/oauth-authorization-server', base_url='https://evil.example').status_code, 400)
        for uri in ('http://localhost', 'https://x/path', 'https://x/?a=b', 'https://user@x', 'https://x/#f', 'https://x:bad', 'https://x\\evil'):
            with self.assertRaises(ValueError): validate_issuer(uri)

    def test_real_flow_inherits_user_and_rejects_other_credentials(self):
        client, token = self.connection()
        self.assertEqual(token['scope'], 'nightfeed:access')
        self.assertEqual(token['expires_in'], 900)
        self.assertEqual(self.mcp(token['access_token']).status_code, 200)
        tools = self.mcp(token['access_token'], 'tools/list').json['result']['tools']
        names = {tool['name'] for tool in tools}
        self.assertTrue({'search_topics', 'propose_feed_change', 'get_settings', 'refresh_feed'} <= names)
        self.assertNotIn('open_safe_browser', names)
        self.assertTrue(all(tool['securitySchemes'] == [dict(type='oauth2', scopes=['nightfeed:access'])] for tool in tools))
        self.assertEqual(self.mcp(token['refresh_token']).status_code, 401)
        self.assertEqual(self.browser.post('/mcp', base_url=ISSUER, json={}).status_code, 401)
        raw = 'nf_old_mcp_key'
        self.sql('INSERT INTO auth_api_keys(user_id,name,token_hash,prefix,scopes,feed_ids,created) VALUES(?,?,?,?,?,?,?)',
                 (1, 'Legacy', fingerprint(raw), raw[:11], json.dumps(['mcp:read','mcp:write']), '[]', time.time()))
        self.assertEqual(self.mcp(raw).status_code, 401)
        for path in ('/', '/settings/security', '/settings/connected-applications', '/api/v1/feeds'):
            self.assertIn(self.client.get(path, base_url=ISSUER, headers={'Authorization':'Bearer '+token['access_token']}).status_code, (401,403))
        self.assertEqual(self.mcp(token['access_token'], headers={'Origin':'https://evil.example'}).status_code, 403)
        stored = self.sql('SELECT * FROM auth_oauth_tokens')[0]
        self.assertNotIn(token['access_token'], str(dict(stored)))
        self.assertNotIn(token['refresh_token'], str(dict(stored)))
        from rss_site_bridge.assistant_audit import redact
        self.assertNotIn(token['access_token'],redact('Pasted credential: '+token['access_token']))
        self.assertNotIn(token['refresh_token'],redact('Pasted credential: '+token['refresh_token']))

    def test_code_pkce_client_resource_and_single_use(self):
        client = register_client(self.app)
        code = authorize(self.app, self.browser, client)
        other = register_client(self.app)
        for changes in (dict(code_verifier='b'*64), dict(code_verifier=''), dict(redirect_uri='https://evil.example'), dict(resource='https://evil.example/mcp'), dict(client_id=other['client_id'])):
            self.assertEqual(exchange(self.app, client, code, **changes).status_code, 400)
        response = exchange(self.app, client, code)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(exchange(self.app, client, code).status_code, 400)
        self.assertEqual(self.mcp(response.json['access_token']).status_code, 401)

    def test_authorization_rejects_bad_parameters_without_redirect(self):
        from authlib.oauth2.rfc7636 import create_s256_code_challenge
        client = register_client(self.app)
        values = dict(client_id=client['client_id'], response_type='code', redirect_uri=CALLBACK, resource=ISSUER+'/mcp',
                      code_challenge=create_s256_code_challenge(VERIFIER), code_challenge_method='S256', scope='nightfeed:access')
        for changes in (dict(redirect_uri='https://evil.example'), dict(code_challenge_method='plain'), dict(code_challenge=''), dict(scope='mcp:write'), dict(resource='https://evil.example/mcp'), dict(response_type='token')):
            response = self.browser.get('/oauth/authorize?' + urlencode(values | changes), base_url=ISSUER)
            self.assertEqual(response.status_code, 400, response.data)
            self.assertNotIn('Location', response.headers)
        duplicate = self.browser.get('/oauth/authorize?' + urlencode(values) + '&resource='+ISSUER+'/mcp', base_url=ISSUER)
        self.assertEqual(duplicate.status_code, 400)

    def test_consent_requires_login_csrf_and_bound_ticket(self):
        from authlib.oauth2.rfc7636 import create_s256_code_challenge
        client = register_client(self.app)
        values = dict(client_id=client['client_id'], redirect_uri=CALLBACK, response_type='code', resource=ISSUER+'/mcp', state='one',
                      code_challenge=create_s256_code_challenge(VERIFIER), code_challenge_method='S256')
        path = '/oauth/authorize?' + urlencode(values)
        response = self.client.get(path, base_url=ISSUER)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/auth/login', response.location)
        page = self.browser.get(path, base_url=ISSUER)
        self.assertIn("form-action 'self' https://client.example",page.headers['Content-Security-Policy'])
        ticket = BeautifulSoup(page.data,'html.parser').select_one('[name=consent_ticket]')['value']
        # Plain client with the browser cookies, but no CSRF helper.
        for cookie in self.browser._cookies.values():
            self.client.set_cookie(cookie.key,cookie.value,domain=cookie.domain)
        response = self.client.post(path, base_url=ISSUER, data=dict(consent_ticket=ticket,decision='allow'))
        self.assertEqual(response.status_code, 403)
        response = self.browser.post(path.replace('state=one','state=two'), base_url=ISSUER, data=dict(consent_ticket=ticket,decision='allow'))
        self.assertEqual(response.status_code, 400)
        response = self.browser.post(path, base_url=ISSUER, data=dict(consent_ticket=ticket,decision='deny'))
        self.assertEqual(parse_qs(urlsplit(response.location).query)['error'], ['access_denied'])
        self.assertEqual(self.sql('SELECT * FROM auth_oauth_grants'), [])

    def test_refresh_rotation_and_family_replay_revocation(self):
        client, token = self.connection()
        wrong = register_client(self.app)
        self.assertEqual(self.refresh(wrong, token).status_code, 400)
        self.assertEqual(self.refresh(client, token, resource='https://evil.example/mcp').status_code, 400)
        response = self.refresh(client, token)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotEqual(response.json['refresh_token'], token['refresh_token'])
        self.assertEqual(self.mcp(token['access_token']).status_code, 401)
        self.assertEqual(self.mcp(response.json['access_token']).status_code, 200)
        self.assertEqual(self.refresh(client, token).status_code, 400)
        self.assertEqual(self.mcp(response.json['access_token']).status_code, 401)
        self.assertEqual(self.refresh(client, response.json).status_code, 400)

    def test_expiry_role_and_security_changes(self):
        client, token = self.connection()
        self.sql('UPDATE auth_oauth_tokens SET expires=0')
        self.assertEqual(self.mcp(token['access_token']).status_code, 401)
        refreshed = self.refresh(client, token)
        self.assertEqual(refreshed.status_code, 200)
        self.sql("UPDATE auth_users SET role='other'")
        self.assertEqual(self.mcp(refreshed.json['access_token']).status_code, 401)
        self.sql("UPDATE auth_users SET role='owner',password_hash='changed'")
        self.assertEqual(self.refresh(client, refreshed.json).status_code, 400)

    def test_connection_revocation_and_token_revocation(self):
        client, token = self.connection()
        listing = self.browser.get('/settings/connected-applications', base_url=ISSUER)
        self.assertEqual(listing.status_code, 200, listing.data)
        self.assertIn(b'Fixture application', listing.data)
        grant = self.sql('SELECT id FROM auth_oauth_grants')[0]['id']
        self.assertEqual(self.browser.post('/settings/connected-applications/'+grant+'/revoke',base_url=ISSUER).status_code,302)
        self.assertEqual(self.mcp(token['access_token']).status_code,401)
        self.assertEqual(self.refresh(client,token).status_code,400)
        client, token = self.connection()
        response = self.client.post('/oauth/revoke',base_url=ISSUER,data=dict(client_id=client['client_id'],token=token['refresh_token']))
        self.assertEqual(response.status_code,200,response.data)
        self.assertEqual(self.mcp(token['access_token']).status_code,401)
        self.assertEqual(self.client.post('/oauth/revoke',base_url=ISSUER,data=dict(client_id=client['client_id'],token='unknown')).status_code,200)

    def test_disable_and_origin_changes_revoke_connections(self):
        client, token = self.connection()
        self.assertEqual(self.browser.post('/settings/ai',data=dict(action='mcp',mcp_public_url=ISSUER)).status_code,200)
        self.assertEqual(self.mcp(token['access_token']).status_code,404)
        self.browser.post('/settings/ai',data=dict(action='mcp',mcp_enabled='1',mcp_public_url=ISSUER))
        self.assertEqual(self.mcp(token['access_token']).status_code,401)
        client, token = self.connection()
        self.browser.post('/settings/ai',data=dict(action='mcp',mcp_enabled='1',mcp_public_url='https://new.example'))
        self.assertEqual(self.mcp(token['access_token']).status_code,400)

    def test_cross_connection_drafts_and_user_task_ownership(self):
        _, one = self.connection()
        _, two = self.connection()
        params = dict(name='propose_timezone',arguments=dict(timezone_name='Europe/London'))
        draft = self.mcp(one['access_token'],'tools/call',params).json['result']['structuredContent']
        params = dict(name='apply_draft',arguments=dict(draft_id=draft['draft_id']))
        self.assertTrue(self.mcp(two['access_token'],'tools/call',params).json['result']['isError'])
        self.assertFalse(self.mcp(one['access_token'],'tools/call',params).json['result']['isError'])
        self.assertEqual(core.get_app_settings(self.db).timezone_name,'Europe/London')

    def test_registration_restrictions_limits_and_confidential_client(self):
        for uri in ('http://client.example/cb','https://client.example/cb#frag','https://user@client.example/cb','https://client.example/*','https://client.example:bad/cb'):
            response = self.client.post('/oauth/register',base_url=ISSUER,json=dict(redirect_uris=[uri]))
            self.assertEqual(response.status_code,400)
        response = self.client.post('/oauth/register',base_url=ISSUER,json=dict(redirect_uris=[CALLBACK],token_endpoint_auth_method='client_secret_post'))
        client = response.json
        code = authorize(self.app,self.browser,client)
        self.assertIn(exchange(self.app,client,code).status_code,(400,401))
        token = exchange(self.app,client,code,client_secret=client['client_secret'])
        self.assertEqual(token.status_code,200,token.data)
        self.assertEqual(self.refresh(client,token.json,client_secret=client['client_secret']).status_code,200)
        for _ in range(19): register_client(self.app)
        self.assertEqual(self.client.post('/oauth/register',base_url=ISSUER,json=dict(redirect_uris=[CALLBACK])).status_code,429)

    def test_token_body_limits_duplicate_and_unsupported_grant(self):
        self.assertEqual(self.client.post('/oauth/token',base_url=ISSUER,json={}).status_code,400)
        self.assertEqual(self.client.post('/oauth/token',base_url=ISSUER,data='x'*16385,content_type='application/x-www-form-urlencoded').status_code,413)
        response = self.client.post('/oauth/token',base_url=ISSUER,data=MultiDict([('resource',ISSUER+'/mcp'),('resource',ISSUER+'/mcp')]))
        self.assertEqual(response.status_code,400)
        response = self.client.post('/oauth/token',base_url=ISSUER,data=dict(resource=ISSUER+'/mcp',grant_type='client_credentials'))
        self.assertEqual(response.json['error'],'unsupported_grant_type')

    def test_concurrent_code_and_refresh_redemption_are_serialized(self):
        client = register_client(self.app)
        code = authorize(self.app, self.browser, client)
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: exchange(self.app,client,code), range(2)))
        self.assertEqual(sorted(response.status_code for response in responses), [200,400])
        issued = next(response.json for response in responses if response.status_code == 200)
        self.assertEqual(self.mcp(issued['access_token']).status_code,401)
        client, token = self.connection()
        def rotate(_):
            return self.app.test_client().post('/oauth/token',base_url=ISSUER,data=dict(
                client_id=client['client_id'],grant_type='refresh_token',refresh_token=token['refresh_token'],resource=ISSUER+'/mcp'))
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(rotate, range(2)))
        self.assertEqual(sorted(response.status_code for response in responses), [200,400])
        issued = next(response.json for response in responses if response.status_code == 200)
        self.assertEqual(self.mcp(issued['access_token']).status_code,401)

    def test_legacy_scope_migration_preserves_rss_credentials(self):
        from rss_site_bridge.auth import initialize
        for name, scopes in (('mixed',['rss:read','mcp:read']),('mcp-only',['mcp:read','mcp:write'])):
            self.sql('INSERT INTO auth_api_keys(user_id,name,token_hash,prefix,scopes,feed_ids,created) VALUES(?,?,?,?,?,?,?)',
                     (1,name,fingerprint(name),name,json.dumps(scopes),'[]',time.time()))
        initialize(self.db)
        keys = {row['name']:dict(row) for row in self.sql('SELECT name,scopes,revoked FROM auth_api_keys')}
        self.assertEqual(json.loads(keys['mixed']['scopes']),['rss:read'])
        self.assertEqual(keys['mixed']['revoked'],0)
        self.assertEqual(keys['mcp-only']['revoked'],1)
        self.assertEqual(json.loads(keys['mcp-only']['scopes']),[])

    def test_access_uses_https_and_connections_have_no_scheduled_expiry(self):
        client, token = self.connection()
        self.assertIsNone(self.sql('SELECT expires FROM auth_oauth_grants')[0]['expires'])
        with patch('rss_site_bridge.oauth.time.time', return_value=time.time() + 31 * 86400):
            renewed = self.refresh(client, token)
        self.assertEqual(renewed.status_code, 200)
        token = renewed.json
        response = self.client.post('/mcp',headers={'Authorization':'Bearer '+token['access_token']},json={})
        self.assertEqual(response.status_code,400)
        self.sql('UPDATE auth_oauth_grants SET expires=0')
        self.assertEqual(self.mcp(token['access_token']).status_code,401)
        self.assertEqual(self.refresh(client,token).status_code,400)

    def test_environment_origin_overrides_ui_and_bad_origin_fails_startup(self):
        with patch.dict('os.environ',{'NIGHTFEED_OAUTH_ISSUER':'https://env.example'}):
            response = self.client.get('/.well-known/oauth-authorization-server',base_url='https://env.example')
            self.assertEqual(response.json['issuer'],'https://env.example')
            self.assertIn(b'https://env.example/mcp',self.browser.get('/settings/ai').data)
        with patch.dict('os.environ',{'NIGHTFEED_OAUTH_ISSUER':'http://bad.example'}):
            with self.assertRaises(ValueError):
                core.create_app(dict(TESTING=True,DATABASE_PATH=self.db,START_SCHEDULER=False))

    def test_password_change_persistently_revokes_grants(self):
        from auth_support import PASSWORD
        client, token = self.connection()
        original = self.sql('SELECT password_hash FROM auth_users')[0]['password_hash']
        response = self.browser.post('/auth/password',data=dict(current_password=PASSWORD,
            new_password='new isolated owner passphrase',confirm_password='new isolated owner passphrase'))
        self.assertEqual(response.status_code,302,response.data)
        self.assertEqual(self.sql('SELECT revoked FROM auth_oauth_grants')[0]['revoked'],1)
        self.sql('UPDATE auth_users SET password_hash=?',(original,))
        self.assertEqual(self.mcp(token['access_token']).status_code,401)
        self.assertEqual(self.refresh(client,token).status_code,400)

    def test_changed_environment_origin_revokes_grants_on_restart(self):
        client, token = self.connection()
        with patch.dict('os.environ',{'NIGHTFEED_OAUTH_ISSUER':'https://new.example'}):
            core.create_app(dict(TESTING=True,DATABASE_PATH=self.db,START_SCHEDULER=False))
        self.assertEqual(self.sql('SELECT revoked FROM auth_oauth_grants')[0]['revoked'],1)
        self.assertEqual(self.mcp(token['access_token']).status_code,401)

    def test_native_loopback_callback_and_code_only_client(self):
        callback='http://127.0.0.1:49152/callback'
        response=self.client.post('/oauth/register',base_url=ISSUER,json=dict(
            redirect_uris=[callback],token_endpoint_auth_method='none',grant_types=['authorization_code']))
        self.assertEqual(response.status_code,201,response.data)
        client=response.json
        code=authorize(self.app,self.browser,client,redirect_uri=callback)
        response=exchange(self.app,client,code,redirect_uri=callback)
        self.assertEqual(response.status_code,200,response.data)
        self.assertNotIn('refresh_token',response.json)
        self.assertEqual(self.mcp(response.json['access_token']).status_code,200)
        response=self.client.post('/oauth/register',base_url=ISSUER,json=dict(
            redirect_uris=[CALLBACK],grant_types=['refresh_token','authorization_code']))
        self.assertEqual(response.status_code,201,response.data)

    def test_profile_name_preferences_preserve_connections(self):
        client,token=self.connection()
        cfg=json.loads(self.sql('SELECT value FROM auth_config')[0]['value'])
        cfg['oidc_use_name']=not cfg['oidc_use_name']
        self.sql('UPDATE auth_config SET value=?',(json.dumps(cfg,sort_keys=True),))
        self.sql("UPDATE auth_users SET name='Updated display name'")
        self.assertEqual(self.mcp(token['access_token']).status_code,200)
        self.assertEqual(self.refresh(client,token).status_code,200)


if __name__ == '__main__': unittest.main()
