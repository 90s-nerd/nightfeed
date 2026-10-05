"""Exercise device opt-in, preferences, and Home Screen guide without sending push."""
import json
import sys
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server
from rss_site_bridge.app import create_app, create_profile, FeedRequest
from ui_auth_support import create_ui_app, authenticate_page
from rss_site_bridge import push_notifications as push
from test_push_notifications import sample_subscription

with TemporaryDirectory() as temp:
    db = Path(temp) / 'app.db'
    app = create_ui_app(dict(TESTING=True, START_SCHEDULER=False, DATABASE_PATH=db))
    profile = create_profile(db, FeedRequest('News', 'https://example.com', 'article', 'a', 'a', '', 100, 0, 'http'))
    server = make_server('127.0.0.1', 0, app, threaded=True)
    Thread(target=server.serve_forever, daemon=True).start()
    origin = f'http://127.0.0.1:{server.server_port}'
    output = ROOT / '.test-preview/mobile-push'
    output.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as pw, patch.object(push, 'deliver') as deliver:
            browser = pw.chromium.launch(channel='chrome', headless=True)
            for width in (390, 1440):
                for theme in ('light', 'dark'):
                    page = browser.new_page(viewport=dict(width=width, height=900)); authenticate_page(page, app, origin)
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    sub = sample_subscription(f'{width}-{theme}')
                    page.add_init_script("""
                      window.permissionCalls = 0;
                      Object.defineProperty(Notification, 'permission', {get: () => window.permissionCalls ? 'granted' : 'default'});
                      Notification.requestPermission = async () => {window.permissionCalls++; return 'granted'};
                      window.PushManager = function() {};
                      const subscription = {toJSON: () => (SUB), unsubscribe: async () => true};
                      const registration = {pushManager: {getSubscription: async () => null, subscribe: async () => subscription}};
                      navigator.serviceWorker.register = async () => registration;
                      Object.defineProperty(navigator.serviceWorker, 'ready', {get: () => Promise.resolve(registration)});
                      localStorage.setItem('nightfeed.appearance.v1', 'THEME');
                    """.replace('SUB', json.dumps(sub)).replace('THEME', theme))
                    page.goto(origin + '/settings')
                    panel = page.locator('[data-push-settings]')
                    expect(panel.locator('[data-push-enable]')).to_be_enabled()
                    expect(panel.locator('[data-push-state]')).to_be_hidden()
                    assert panel.evaluate('el => Math.round(document.querySelector(".settings-shell").getBoundingClientRect().top - el.getBoundingClientRect().bottom)') == 24
                    assert page.evaluate('window.permissionCalls') == 0
                    panel.get_by_role('button', name='Enable on this device').click()
                    expect(panel.locator('[data-push-preferences]')).to_be_visible()
                    expect(panel.locator('[data-push-state]')).to_be_hidden()
                    assert page.evaluate('window.permissionCalls') == 1
                    panel.get_by_label('Updated topics', exact=True).check()
                    panel.get_by_label('All feeds', exact=True).uncheck()
                    panel.get_by_label('News', exact=True).check()
                    panel.get_by_label('Daily limit', exact=True).select_option('3')
                    panel.get_by_label('Quiet hours', exact=True).check()
                    panel.get_by_role('button', name='Save notification preferences').click()
                    expect(panel.locator('[data-push-feedback]')).to_have_text('Notification preferences saved.')
                    with closing(push.connect(db)) as conn:
                        prefs = json.loads(conn.execute('SELECT preferences FROM push_devices ORDER BY rowid DESC').fetchone()[0])
                    assert prefs['updated'] and prefs['quiet'] and prefs['daily_limit'] == 3 and prefs['feeds'] == [profile.id]
                    page.screenshot(path=str(output / f'preferences-{width}-{theme}.png'), full_page=True)
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    panel.get_by_role('button', name='Send test', exact=True).click()
                    expect(panel.locator('[data-push-feedback]')).to_contain_text('Test sent')
                    panel.get_by_role('button', name='Add to Home Screen', exact=True).click()
                    guide = page.locator('[data-push-guide-dialog]')
                    expect(guide).to_be_visible()
                    expect(guide.locator('[data-push-guide-counter]')).to_have_text('Step 1 of 2')
                    expect(guide.locator('[data-push-guide-step-title]')).to_have_text('Install Nightfeed')
                    guide.screenshot(path=str(output / f'guide-{width}-{theme}.png'))
                    guide.get_by_role('button', name='Next', exact=True).click()
                    expect(guide.get_by_role('button', name='Done', exact=True)).to_be_visible()
                    guide.get_by_role('button', name='Back', exact=True).click()
                    page.keyboard.press('Escape')
                    expect(guide).not_to_be_visible()
                    panel.get_by_role('button', name='Turn off', exact=True).click()
                    expect(panel.locator('[data-push-preferences]')).not_to_be_visible()
                    page.reload()
                    expect(panel.locator('[data-push-enable]')).to_be_enabled()
                    expect(panel.locator('[data-push-state]')).to_be_hidden()
                    assert not errors, errors
                    page.close()
            # Safari users receive installation guidance before any permission request.
            context = browser.new_context(viewport=dict(width=390, height=844), user_agent='Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1')
            page = context.new_page(); authenticate_page(page, app, origin)
            page.add_init_script("window.permissionCalls = 0; Notification.requestPermission = async () => {window.permissionCalls++; return 'granted'}")
            page.goto(origin + '/settings')
            page.get_by_role('button', name='Enable on this device').click()
            expect(page.locator('[data-push-guide-step-title]')).to_have_text('Add to Home Screen')
            assert page.evaluate('window.permissionCalls') == 0
            expect(page.locator('[data-push-guide-step-text]')).to_contain_text('tap Share')
            page.locator('[data-push-guide-dialog]').screenshot(path=str(output / 'guide-iphone.png'))
            page.get_by_role('button', name='Next', exact=True).click()
            expect(page.locator('[data-push-guide-step-title]')).to_have_text('Choose your notifications')
            page.get_by_role('button', name='Done', exact=True).click()
            expect(page.locator('[data-push-guide-dialog]')).to_be_hidden()
            context.close()
            # Run the real worker in a controlled JS scope: displaying an alert
            # needs no private-server fetch, and clicks cannot leave this origin.
            page = browser.new_page(); authenticate_page(page, app, origin)
            page.evaluate("""source => {
              const handlers = {}; const shown = []; const opened = [];
              const scope = {location: {origin: 'https://nightfeed.example.com'},
                addEventListener: (name, fn) => {handlers[name] = fn},
                registration: {showNotification: async (title, options) => shown.push({title, options})},
                clients: {matchAll: async () => [], openWindow: async url => opened.push(url)}};
              new Function('self', 'fetch', source)(scope, () => {throw Error('Worker fetched private server')});
              window.workerCheck = async () => {
                let work; const waitUntil = promise => {work = promise};
                handlers.push({data: {json: () => ({title:'Nightfeed · News',body:'3 new topics, 2 updated topics',url:'/notifications/7'})},waitUntil});
                await work;
                if (shown[0].title !== 'Nightfeed · News' || shown[0].options.body !== '3 new topics, 2 updated topics') throw Error('Push summary was not displayed');
                handlers.notificationclick({notification:{close(){},data:{url:'/notifications/7'}},waitUntil});
                await work;
                if (opened[0] !== 'https://nightfeed.example.com/notifications/7') throw Error('Wrong report target');
                handlers.notificationclick({notification:{close(){},data:{url:'https://evil.example.com'}},waitUntil});
                if (opened.length !== 1) throw Error('External target was opened');
              };
            }""", (ROOT / 'rss_site_bridge/static/nightfeed-worker.js').read_text())
            page.evaluate('workerCheck()')
            page.close()
            browser.close()
            assert deliver.call_count == 4
        print('Push settings passed: desktop/mobile, light/dark, opt-in, preferences, tests, disable and iPhone guide.')
    finally:
        server.shutdown()
