"""Check blank Safe Browser sessions with real Chromium and public-URL fixtures."""
from pathlib import Path
from contextlib import closing
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import patch
import logging
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from playwright.sync_api import CDPSession, Error as PlaywrightError, Page, sync_playwright, expect
from werkzeug.serving import make_server
from rss_site_bridge.app import _safe_browser_sessions
from rss_site_bridge import downloaders as d
from rss_site_bridge.downloader_adapters import WebAPIv2Downloader
from ui_auth_support import create_ui_app, authenticate_page

logging.disable(logging.CRITICAL)
original_route = Page.route
MAGNET = 'magnet:?xt=urn:btih:' + 'a' * 40 + '&dn=Fixture%20magnet'
BUTTON_MAGNET = 'magnet:?xt=urn:btih:' + 'b' * 40 + '&dn=Button%20magnet'
LOCATION_MAGNET = 'magnet:?xt=urn:btih:' + 'c' * 40 + '&dn=Location%20magnet'


class FixtureDownloader:
    extensions = ('.torrent',)
    supports_magnets = True
    identify = staticmethod(WebAPIv2Downloader.identify)
    added = []

    def __init__(self, profile, key):
        pass

    def categories(self):
        return [{'name': 'Movies', 'save_path': '/downloads/movies'}]

    def contains(self, hashes):
        return bool(self.added)

    def add(self, data, category, start):
        self.added.append((data, category, start))


def fixture_route(page, pattern, handler, **kwargs):
    def handle(route):
        if route.request.url == 'https://safe-browser.example.test/':
            route.fulfill(content_type='text/html', body=f'''<title>Browser fixture</title><h1>Website loaded</h1>
                <a target="_blank" href="{MAGNET}" style="position:absolute;left:10px;top:100px;width:280px;height:40px"><span>Send this magnet</span></a>
                <a href="/fixture.torrent" style="position:absolute;left:10px;top:160px;width:280px;height:40px">Download torrent file</a>
                <form onsubmit="event.preventDefault();document.title='Submitted:'+document.querySelector('#entry').value">
                  <input id="entry" aria-label="Remote text" oninput="document.title='Entry:'+this.value" style="position:absolute;left:10px;top:220px;width:250px;height:35px">
                  <input type="email" oninput="document.title='Email:'+this.value" style="position:absolute;left:10px;top:500px;width:250px;height:35px">
                  <button type="submit" hidden>Submit</button>
                </form>
                <button onclick="window.open('{BUTTON_MAGNET}')" style="position:absolute;left:10px;top:280px;height:35px">Button magnet</button>
                <button onclick="location.href='{LOCATION_MAGNET}'" style="position:absolute;left:10px;top:340px;height:35px">Location magnet</button>
                <a href="/next?x=1&amp;y=2" style="position:absolute;left:10px;top:400px;height:35px"><span>Copy this link</span></a>
                <div style="height:1800px"></div>''')
        elif route.request.url.startswith('https://safe-browser.example.test/submission?'):
            mode = route.request.url.split('mode=')[-1]
            action = {
                'navigate': "setTimeout(() => location.assign('/submitted'), 800)",
                'reload': "setTimeout(() => location.reload(), 800)",
                'blur': "setTimeout(() => document.querySelector('#entry').blur(), 800)",
                'recover': "setTimeout(() => location.assign('/redirect'), 100)",
            }.get(mode, '')
            field = ('<textarea id="entry" oninput="document.title=\'Lines:\'+this.value.split(String.fromCharCode(10)).length"'
                     if mode == 'multiline' else '<input id="entry" autofocus type="' + ('email' if mode == 'invalid' else 'text') + '" required oninvalid="document.title=\'Validation failed\'"')
            field += ' style="position:absolute;left:10px;top:20px;width:260px;height:70px">'
            if mode == 'multiline':
                field += '</textarea>'
            route.fulfill(content_type='text/html', body=f'''<title>Submission fixture</title>
                <form onsubmit="event.preventDefault();{action}">{field}<button hidden>Submit</button></form>''')
        elif route.request.url == 'https://safe-browser.example.test/redirect':
            route.fulfill(content_type='text/html', body="<script>location.replace('https://safe-browser-results.example.org/submitted')</script>")
        elif route.request.url == 'https://safe-browser-results.example.org/submitted':
            route.fulfill(content_type='text/html', body='<title>Cross-site result</title><input autofocus value="New result field">')
        elif route.request.url == 'https://safe-browser.example.test/submitted':
            route.fulfill(content_type='text/html', body='<title>Submission complete</title><input autofocus value="New page field">')
        elif route.request.url.startswith('https://safe-browser.example.test/next'):
            route.fulfill(content_type='text/html', body='<title>Next page</title><h1>Next page</h1>')
        elif route.request.url == 'https://safe-browser.example.test/fixture.torrent':
            route.fulfill(content_type='application/x-bittorrent', headers={'Content-Disposition': 'attachment; filename=fixture.torrent'},
                          body=b'd4:infod6:lengthi1e4:name7:Fixture12:piece lengthi16384e6:pieces20:xxxxxxxxxxxxxxxxxxxxee')
        else:
            handler(route)
    return original_route(page, pattern, handle, **kwargs)


with TemporaryDirectory() as temp, patch.object(Page, 'route', fixture_route), patch.dict(d.ADAPTERS, qbittorrent=FixtureDownloader):
    app = create_ui_app({'DATABASE_PATH': Path(temp) / 'browser.db', 'START_SCHEDULER': False, 'TESTING': True})
    values = dict(name='Fixture', kind='qbittorrent', enabled='1', base_url='https://downloader.example.test', auth_mode='none',
                  timeout='15', extensions='.torrent', default_category='Movies', require_category='1', start_immediately='1', enable_magnets='1', button_label='Queue at home')
    config = d.parse_profile(values, None, d.encryption_key(app.config['DATABASE_PATH']))
    with closing(d.connect(app.config['DATABASE_PATH'])) as connection:
        connection.execute('INSERT INTO downloaders (' + ','.join(config) + ') VALUES (' + ','.join('?' for _ in config) + ')', tuple(config.values()))
        connection.commit()
    server = make_server('127.0.0.1', 0, app, threaded=True)
    Thread(target=server.serve_forever, daemon=True).start()
    origin = f'http://127.0.0.1:{server.server_port}'
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            for width in (390, 1440):
                FixtureDownloader.added = []
                with closing(d.connect(app.config['DATABASE_PATH'])) as connection:
                    connection.execute('UPDATE downloaders SET enabled=1, enable_magnets=1')
                    connection.commit()
                context = browser.new_context(viewport={'width': width, 'height': 900}, has_touch=True)
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                authenticate_page(page, app, origin)
                page.goto(origin)
                expect(page.get_by_role('link', name='Safe Browser', exact=True)).to_be_visible()
                assert not page.evaluate('document.documentElement.scrollWidth > innerWidth + 1')
                page.get_by_role('link', name='Safe Browser', exact=True).click()
                address = page.get_by_role('textbox', name='Website URL')
                typing_tools = page.locator('[data-browser-typing-tools]')
                expect(typing_tools).to_be_hidden()
                expect(address).to_have_attribute('inputmode', 'url')
                expect(address).to_have_attribute('autocomplete', 'off')
                expect(address).to_have_value('')
                expect(page.locator('[data-browser-empty]')).to_be_visible()
                expect(page.locator('[data-browser-original]')).to_be_hidden()
                expect(page.get_by_role('button', name='Back', exact=True)).to_be_disabled()
                expect(page.get_by_role('button', name='Forward', exact=True)).to_be_disabled()
                address.fill('safe-browser.example.test/')
                # Polling must preserve the URL before submission.
                page.wait_for_timeout(2600)
                expect(address).to_have_value('safe-browser.example.test/')
                page.screenshot(path=str(ROOT / f'.test-preview/safe-browser-blank-{width}.png'))
                page.get_by_role('button', name='Go', exact=True).click()
                expect(page.locator('[data-browser-title]')).to_have_text('Browser fixture')
                expect(page.locator('[data-browser-empty]')).to_be_hidden()
                expect(page.locator('[data-browser-original]')).to_have_attribute('href', 'https://safe-browser.example.test/')
                assert not page.evaluate('document.documentElement.scrollWidth > innerWidth + 1')
                state_url = page.locator('[data-safe-browser]').get_attribute('data-state-url')
                state = page.request.get(origin + state_url).json()
                screen = page.locator('[data-browser-screen]')
                box = screen.bounding_box()
                def remote_position(x, y):
                    current = screen.bounding_box()
                    return {'x': x * current['width'] / state['viewport_width'], 'y': y * current['height'] / state['viewport_height']}
                def click_remote(x, y):
                    screen.click(position=remote_position(x, y))
                typing = page.locator('[data-browser-typing]')
                click_remote(30, 30)
                expect(typing_tools).to_be_hidden()
                # A delayed field-selection reply must not reopen controls over the URL field.
                page.evaluate("""() => {
                    window.typingTestFetch = window.fetch;
                    window.delayedTypingReplyDone = false;
                    window.fetch = async (...args) => {
                      const response = await window.typingTestFetch(...args);
                      if (String(args[0]).endsWith('/command') && JSON.parse(args[1].body).action === 'click') {
                        await new Promise(resolve => setTimeout(resolve, 800));
                        window.delayedTypingReplyDone = true;
                      }
                      return response;
                    };
                }""")
                screen.tap(position=remote_position(30, 235))
                address.focus()
                page.wait_for_function('() => window.delayedTypingReplyDone')
                page.wait_for_timeout(100)
                expect(typing_tools).to_be_hidden()
                page.evaluate('window.fetch = window.typingTestFetch')
                # Show controls only after confirming an editable remote field.
                pos = remote_position(30, 235)
                screen.tap(position=pos)
                expect(typing_tools).to_be_visible()
                expect(typing).to_be_focused()
                typing.press_sequentially('hello')
                typing.press('Backspace')
                typing.press_sequentially('é')
                expect(page.locator('[data-browser-title]')).to_have_text('Entry:hellé')
                expect(typing).to_have_value('hellé')
                typing.dispatch_event('compositionstart')
                typing.evaluate("el => {el.value='hellé日本';}")
                typing.dispatch_event('compositionend', {'data':'日本'})
                typing.dispatch_event('input', {'inputType':'insertText','data':'日本'})
                expect(page.locator('[data-browser-title]')).to_have_text('Entry:hellé日本')
                expect(typing).to_have_value('hellé日本')
                typing.press('Home')
                typing.press_sequentially('X')
                expect(page.locator('[data-browser-title]')).to_have_text('Entry:Xhellé日本')
                typing.press('Backspace')
                expect(page.locator('[data-browser-title]')).to_have_text('Entry:hellé日本')
                page.locator('[data-browser-enter]').click()
                expect(page.locator('[data-browser-title]')).to_have_text('Submitted:hellé日本')
                page.locator('[data-browser-tab]').click()
                expect(typing).to_have_attribute('inputmode', 'email')
                typing.press_sequentially('test@example.org')
                expect(page.locator('[data-browser-title]')).to_have_text('Email:test@example.org')
                page.locator('[data-browser-typing-done]').click()
                expect(typing_tools).to_be_hidden()
                click_remote(30, 235)
                expect(typing).to_be_focused()
                expect(typing).to_have_value('hellé日本')
                expect(typing_tools).to_be_visible()
                address.focus()
                expect(typing_tools).to_be_hidden()
                screen.tap(position=remote_position(30, 235))
                expect(typing_tools).to_be_visible()
                assert not page.evaluate('document.documentElement.scrollWidth > innerWidth + 1')
                page.screenshot(path=str(ROOT / f'.test-preview/safe-browser-input-{width}.png'))
                # Exercise long press: inspecting must preserve the current URL.
                pos = remote_position(30, 410)
                box = screen.bounding_box()
                touch_session = context.new_cdp_session(page)
                touch_session.send('Input.dispatchTouchEvent', {'type':'touchStart', 'touchPoints':[{'x':box['x']+pos['x'], 'y':box['y']+pos['y']}]})
                expect(page.locator('[data-browser-link-dialog]')).to_be_visible()
                expect(typing_tools).to_be_hidden()
                touch_session.send('Input.dispatchTouchEvent', {'type':'touchEnd', 'touchPoints':[]})
                touch_session.detach()
                expect(page.locator('[data-browser-link-url]')).to_have_value('https://safe-browser.example.test/next?x=1&y=2')
                expect(address).to_have_value('https://safe-browser.example.test/')
                page.context.grant_permissions(['clipboard-read', 'clipboard-write'])
                page.locator('[data-browser-link-copy]').click()
                expect(page.locator('[data-browser-link-dialog]')).to_be_hidden()
                assert page.evaluate('navigator.clipboard.readText()') == 'https://safe-browser.example.test/next?x=1&y=2'
                screen.click(position=remote_position(30, 410), button='right')
                expect(page.locator('[data-browser-link-dialog]')).to_be_visible()
                expect(page.locator('[data-browser-link-url]')).to_have_value('https://safe-browser.example.test/next?x=1&y=2')
                page.locator('[data-browser-link-close]').click()
                click_remote(30, 110)
                expect(page.locator('.safe-browser-download-name')).to_have_text('Fixture magnet')
                expect(page.locator('[data-browser-original]')).to_have_attribute('href', 'https://safe-browser.example.test/')
                expect(page.get_by_role('link', name='Save file', exact=True)).to_have_count(0)
                assert not FixtureDownloader.added, 'Capturing must not submit automatically'
                page.get_by_role('button', name='Queue at home', exact=True).click()
                expect(page.locator('[data-send-file]')).to_have_text('Fixture magnet')
                expect(page.locator('[data-send-category]')).to_have_value('Movies')
                expect(page.locator('[data-send-submit]')).to_have_text('Queue at home')
                page.locator('[data-send-submit]').click()
                expect(page.locator('[data-download-submission]')).to_contain_text('Added to Fixture')
                assert FixtureDownloader.added == [(MAGNET, 'Movies', True)]
                page.get_by_role('button', name='Downloads', exact=False).click()
                click_remote(30, 110)
                expect(page.locator('.safe-browser-download-name')).to_have_count(1)
                assert len(FixtureDownloader.added) == 1
                if page.locator('[data-browser-download-popover]').is_visible():
                    page.get_by_role('button', name='Downloads', exact=False).click()
                click_remote(30, 290)
                expect(page.locator('.safe-browser-download-name')).to_have_count(2)
                page.get_by_role('button', name='Downloads', exact=False).click()
                click_remote(30, 350)
                expect(page.locator('.safe-browser-download-name')).to_have_count(3)
                assert len(FixtureDownloader.added) == 1
                page.screenshot(path=str(ROOT / f'.test-preview/safe-browser-magnet-{width}.png'))
                if page.locator('[data-browser-download-popover]').is_visible():
                    page.get_by_role('button', name='Downloads', exact=False).click()
                click_remote(30, 170)
                try:
                    expect(page.get_by_role('link', name='Save file', exact=True)).to_have_count(1, timeout=15000)
                except AssertionError:
                    print('Download failure state:', page.request.get(origin + state_url).json(), 'UI errors:', errors, flush=True)
                    raise
                expect(page.locator('.safe-browser-download-name')).to_have_count(4)
                assert len(FixtureDownloader.added) == 1
                with closing(d.connect(app.config['DATABASE_PATH'])) as connection:
                    connection.execute('UPDATE downloaders SET enable_magnets=0')
                    connection.commit()
                expect(page.locator('.safe-browser-download-name')).to_have_count(1)
                expect(page.locator('.safe-browser-download-name')).to_have_text('fixture.torrent')
                expect(page.get_by_role('link', name='Configure downloader', exact=True)).to_have_count(0)
                if page.locator('[data-browser-download-popover]').is_visible():
                    page.get_by_role('button', name='Downloads', exact=False).click()
                address.fill('http://127.0.0.1/')
                page.get_by_role('button', name='Go', exact=True).click()
                expect(page.locator('[data-browser-loading]')).to_contain_text('Only public http or https')
                page.get_by_role('button', name='Back', exact=True).click()
                expect(address).to_have_value('')
                expect(page.locator('[data-browser-empty]')).to_be_visible()
                expect(page.get_by_role('button', name='Back', exact=True)).to_be_disabled()
                expect(page.get_by_role('button', name='Forward', exact=True)).to_be_enabled()
                page.get_by_role('button', name='Forward', exact=True).click()
                expect(address).to_have_value('https://safe-browser.example.test/')
                expect(page.get_by_role('button', name='Forward', exact=True)).to_be_disabled()
                click_remote(30, 410)
                expect(page.locator('[data-browser-title]')).to_have_text('Next page')
                page.get_by_role('button', name='Back', exact=True).click()
                expect(page.locator('[data-browser-title]')).to_have_text('Browser fixture')
                page.get_by_role('button', name='Forward', exact=True).click()
                expect(page.locator('[data-browser-title]')).to_have_text('Next page')
                page.get_by_role('button', name='Back', exact=True).click()
                expect(page.locator('[data-browser-title]')).to_have_text('Browser fixture')
                address.fill('safe-browser.example.test/#branch')
                page.get_by_role('button', name='Go', exact=True).click()
                expect(page.get_by_role('button', name='Forward', exact=True)).to_be_disabled()
                # Enter replies before these delayed actions: polling must retire the old field.
                for mode in ('navigate', 'reload', 'blur', 'invalid', 'multiline'):
                    address.fill('safe-browser.example.test/submission?mode=' + mode)
                    page.get_by_role('button', name='Go', exact=True).click()
                    expect(address).to_have_value('https://safe-browser.example.test/submission?mode=' + mode)
                    expect(page.locator('[data-browser-title]')).to_have_text('Submission fixture')
                    click_remote(30, 40)
                    expect(typing).to_be_focused()
                    typing.press_sequentially('line' if mode == 'multiline' else 'invalid' if mode == 'invalid' else 'search')
                    before = page.request.get(origin + state_url).json()
                    if mode == 'reload':
                        typing.press('Enter')
                    else:
                        page.locator('[data-browser-enter]').click()
                    if mode in ('navigate', 'reload', 'blur'):
                        expect(typing_tools).to_be_hidden(timeout=10000)
                        assert not typing.evaluate('el => el === document.activeElement')
                        expect(typing).to_have_value('')
                        if mode == 'navigate':
                            expect(page.locator('[data-browser-title]')).to_have_text('Submission complete')
                        elif mode == 'reload':
                            after = page.request.get(origin + state_url).json()
                            assert after['url'] == before['url']
                            assert after['document_id'] != before['document_id']
                        # A destination autofocus must not reopen the dismissed panel.
                        page.wait_for_timeout(2200)
                        expect(typing_tools).to_be_hidden()
                    elif mode == 'invalid':
                        expect(page.locator('[data-browser-title]')).to_have_text('Validation failed')
                        page.wait_for_timeout(2200)
                        expect(typing_tools).to_be_visible()
                        expect(typing).to_have_value('invalid')
                    else:
                        expect(page.locator('[data-browser-title]')).to_have_text('Lines:2')
                        page.wait_for_timeout(2200)
                        expect(typing_tools).to_be_visible()
                    page.screenshot(path=str(ROOT / f'.test-preview/safe-browser-submit-{mode}-{width}.png'))
                # Reproduce a delivered Enter whose reply and subsequent reads fail during a redirect.
                address.fill('safe-browser.example.test/submission?mode=recover')
                page.get_by_role('button', name='Go', exact=True).click()
                expect(address).to_have_value('https://safe-browser.example.test/submission?mode=recover')
                click_remote(30, 40)
                expect(typing).to_be_focused()
                typing.press_sequentially('search once')
                session_id = state_url.split('/')[-2]
                remote_session = _safe_browser_sessions[session_id]
                original_execute = remote_session.execute
                failures = {'history': 0, 'screenshot': 0}
                original_cdp_send = CDPSession.send
                def transient_history(cdp_session, method, *args, **kwargs):
                    if method == 'Page.getNavigationHistory' and failures['history']:
                        failures['history'] -= 1
                        raise PlaywrightError('CDPSession.send: Protocol error (Page.getNavigationHistory): Not attached to an active page')
                    return original_cdp_send(cdp_session, method, *args, **kwargs)
                enter_calls = []
                responses = []
                def transient_navigation(action, **payload):
                    if action == 'key' and payload.get('key') == 'Enter':
                        enter_calls.append(action)
                        original_execute(action, **payload)
                        failures.update(history=7, screenshot=2)
                        raise RuntimeError('Navigation replaced the active renderer after Enter')
                    if failures.get(action, 0):
                        failures[action] -= 1
                        raise RuntimeError('Navigation replaced the active renderer during ' + action)
                    return original_execute(action, **payload)
                def record_response(response):
                    if '/safe-browser/' + session_id + '/' in response.url:
                        responses.append((response.url.rsplit('/', 1)[-1], response.status))
                page.on('response', record_response)
                with patch.object(remote_session, 'execute', side_effect=transient_navigation), patch.object(CDPSession, 'send', transient_history):
                    page.locator('[data-browser-enter]').click()
                    expect(page.locator('[data-browser-title]')).to_have_text('Cross-site result', timeout=15000)
                    expect(page.locator('[data-browser-loading]')).to_be_hidden(timeout=15000)
                    expect(page.locator('[data-session-ended]')).to_be_hidden()
                    expect(typing_tools).to_be_hidden()
                    assert enter_calls == ['key'], 'Enter must never be replayed after an uncertain reply'
                    assert failures == {'history': 0, 'screenshot': 0}, failures
                    assert ('command', 400) in responses, responses
                    assert ('state', 503) in responses, responses
                    assert ('screenshot', 503) in responses, responses
                    assert _safe_browser_sessions[session_id] is remote_session and not remote_session.closed
                    assert page.request.get(origin + state_url).status == 200
                    screenshot_url = page.locator('[data-safe-browser]').get_attribute('data-screenshot-url')
                    assert page.request.get(origin + screenshot_url).status == 200
                page.remove_listener('response', record_response)
                page.screenshot(path=str(ROOT / f'.test-preview/safe-browser-enter-recovery-{width}.png'))
                page.once('dialog', lambda dialog: dialog.accept())
                page.get_by_role('button', name='Close session', exact=True).click()
                expect(page).to_have_url(origin + '/', timeout=15000)
                assert not errors, errors
                context.close()
            browser.close()
    finally:
        for session in list(_safe_browser_sessions.values()):
            session.stop()
        server.shutdown()
print('PASS: desktop/mobile form typing, deletion, Unicode/IME, Enter, Next field, email keyboard mode, long-press/right-click link copying, anchor/JavaScript magnets, submission, duplicate prevention, file downloads, private URL blocking, navigation, delayed submission/navigation, same-URL reload, focus loss, validation, multiline Enter, cross-site Enter recovery without replay or session expiry, and close.')
