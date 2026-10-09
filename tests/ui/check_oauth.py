"""OAuth consent and revocation in Chrome against an isolated HTTPS fixture."""
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.parse import urlencode, parse_qs, urlsplit
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server
from rss_site_bridge.app import connect_db, create_profile, FeedRequest
from ui_auth_support import create_ui_app, authenticate_page


with TemporaryDirectory(dir=ROOT / '.test-preview') as temp:
    db = Path(temp) / 'oauth-ui.db'
    app = create_ui_app(dict(TESTING=True, DATABASE_PATH=db, START_SCHEDULER=False))
    profile = create_profile(db, FeedRequest('Header fixture', 'https://example.com', 'article', 'a', 'a', '', 10, 60, 'http'))
    server = make_server('127.0.0.1', 0, app, threaded=True, ssl_context='adhoc')
    address = f'https://127.0.0.1:{server.server_port}'
    with closing(connect_db(db)) as conn:
        conn.execute('UPDATE assistant_config SET mcp_enabled=1,mcp_public_url=?', (address,))
        conn.commit()
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        callback = 'https://client.example/oauth/callback'
        client = app.test_client().post('/oauth/register', base_url=address,
            json=dict(client_name='ChatGPT fixture', redirect_uris=[callback], token_endpoint_auth_method='none')).json
        verifier = 'nightfeed-browser-code-verifier-' + 'a' * 32
        params = dict(client_id=client['client_id'], redirect_uri=callback, response_type='code',
                      resource=address + '/mcp', scope='nightfeed:access', state='browser-fixture',
                      code_challenge=create_s256_code_challenge(verifier), code_challenge_method='S256')
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='chrome')
            context = browser.new_context(ignore_https_errors=True, viewport=dict(width=1280, height=900), reduced_motion='reduce')
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            authenticate_page(page, app, address)
            page.goto(address + '/settings/ai')
            button_style = '(el) => { const s=getComputedStyle(el); return [s.minHeight,s.borderRadius,s.padding,s.fontSize,s.backgroundColor,s.color,s.borderColor]; }'
            primary_style = page.get_by_role('button', name='Save MCP settings').evaluate(button_style)
            secondary_style = page.get_by_role('link', name='Back to settings').evaluate(button_style)
            page.screenshot(path=str(ROOT / '.test-preview/oauth-settings-desktop.png'), full_page=True)
            page.route(callback + '**', lambda route: route.fulfill(status=200, content_type='text/html', body='<h1>Client callback fixture</h1>'))
            page.goto(address + '/oauth/authorize?' + urlencode(params))
            expect(page.get_by_role('heading', name='Connect ChatGPT fixture to Nightfeed?')).to_be_visible()
            assert page.get_by_role('button', name='Allow connection').evaluate(button_style) == primary_style
            assert page.get_by_role('button', name='Cancel').evaluate(button_style) == secondary_style
            page.screenshot(path=str(ROOT / '.test-preview/oauth-consent-desktop.png'), full_page=True)
            page.set_viewport_size(dict(width=390, height=844))
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'Consent overflows on mobile'
            page.screenshot(path=str(ROOT / '.test-preview/oauth-consent-mobile.png'), full_page=True)
            page.emulate_media(color_scheme='dark')
            page.screenshot(path=str(ROOT / '.test-preview/oauth-consent-mobile-dark.png'), full_page=True)
            page.emulate_media(color_scheme='light')
            page.get_by_role('button', name='Allow connection').click()
            page.wait_for_url(callback + '**')
            returned = parse_qs(urlsplit(page.url).query)
            assert returned['state'] == ['browser-fixture'] and returned['iss'] == [address]
            response = context.request.post(address + '/oauth/token', form=dict(
                client_id=client['client_id'], grant_type='authorization_code', code=returned['code'][0],
                redirect_uri=callback, code_verifier=verifier, resource=address + '/mcp'))
            assert response.status == 200, response.text()
            token = response.json()['access_token']
            page.goto(address + '/settings/connected-applications')
            expect(page.get_by_role('heading', name='ChatGPT fixture')).to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'Connections overflow on mobile'
            page.screenshot(path=str(ROOT / '.test-preview/oauth-connections-mobile.png'), full_page=True)
            page.set_viewport_size(dict(width=1280, height=900))
            back = page.get_by_role('link', name='Back to security')
            heading = page.get_by_role('heading', name='Connected applications', exact=True)
            assert back.bounding_box()['x'] > heading.bounding_box()['x'] + heading.bounding_box()['width'], 'Desktop heading action must sit beside the title'
            assert back.evaluate(button_style) == secondary_style
            page.screenshot(path=str(ROOT / '.test-preview/oauth-connections-desktop.png'), full_page=True)
            page.emulate_media(color_scheme='dark')
            page.screenshot(path=str(ROOT / '.test-preview/oauth-connections-desktop-dark.png'), full_page=True)
            page.emulate_media(color_scheme='light')
            page.get_by_role('button', name='Revoke connection').click()
            expect(page.get_by_text('Inactive · Connected:', exact=False)).to_be_visible()
            response = context.request.post(address + '/mcp', headers={
                'Authorization': 'Bearer ' + token, 'Accept': 'application/json, text/event-stream'},
                data=dict(jsonrpc='2.0', id=1, method='ping'))
            assert response.status == 401
            page.goto(address + '/settings/ai')
            page.set_viewport_size(dict(width=390, height=844))
            expect(page.get_by_label('Public Nightfeed URL')).to_have_value(address)
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'MCP settings overflow on mobile'
            page.screenshot(path=str(ROOT / '.test-preview/oauth-settings-mobile.png'), full_page=True)
            page.emulate_media(color_scheme='dark')
            page.screenshot(path=str(ROOT / '.test-preview/oauth-settings-mobile-dark.png'), full_page=True)
            page.emulate_media(color_scheme='light')
            for width in (1280, 390, 320):
                page.set_viewport_size(dict(width=width, height=900))
                for path, label, screenshot in (
                    ('/settings/api-keys', 'Back to security', 'keys'),
                    ('/settings/downloaders', 'Back to settings', 'downloaders'),
                    ('/compose', 'Cancel', 'compose'),
                    (f'/profiles/{profile.id}', 'Back to feeds', 'feed'),
                    (f'/feeds/{profile.feed_token}/view', 'Back to feed', 'xml'),
                ):
                    page.goto(address + path)
                    back = page.get_by_role('link', name=label, exact=True)
                    expect(back).to_be_visible()
                    assert back.bounding_box()['width'] < width * .65, f'{path} back button stretches at {width}px'
                    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), f'{path} overflows at {width}px'
                    page.screenshot(path=str(ROOT / f'.test-preview/header-{screenshot}-{width}.png'), full_page=True)
            assert not errors, errors
            browser.close()
        print('OAuth UI: HTTPS consent, PKCE exchange, connection revocation, desktop/mobile layout and JavaScript errors checked.')
    finally:
        server.shutdown()
        server.server_close()
