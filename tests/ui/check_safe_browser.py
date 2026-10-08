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
from playwright.sync_api import BrowserType, Page, sync_playwright, expect
from werkzeug.serving import make_server
from rss_site_bridge.app import _safe_browser_sessions
from rss_site_bridge import downloaders as d
from rss_site_bridge.downloader_adapters import WebAPIv2Downloader
from ui_auth_support import create_ui_app, authenticate_page

logging.disable(logging.CRITICAL)
original_route = Page.route
original_launch = BrowserType.launch
MAGNET = 'magnet:?xt=urn:btih:' + 'a' * 40 + '&dn=Fixture%20magnet'


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


def launch_chrome(browser_type, **kwargs):
    return original_launch(browser_type, channel='chrome', **kwargs)


def fixture_route(page, pattern, handler, **kwargs):
    def handle(route):
        if route.request.url == 'https://safe-browser.example.test/':
            route.fulfill(content_type='text/html', body=f'''<title>Browser fixture</title><h1>Website loaded</h1>
                <a target="_blank" href="{MAGNET}" style="position:absolute;left:10px;top:100px;width:280px;height:40px"><span>Send this magnet</span></a>
                <a href="/fixture.torrent" style="position:absolute;left:10px;top:160px;width:280px;height:40px">Download torrent file</a>''')
        elif route.request.url == 'https://safe-browser.example.test/fixture.torrent':
            route.fulfill(content_type='application/x-bittorrent', headers={'Content-Disposition': 'attachment; filename=fixture.torrent'},
                          body=b'd4:infod6:lengthi1e4:name7:Fixture12:piece lengthi16384e6:pieces20:xxxxxxxxxxxxxxxxxxxxee')
        else:
            handler(route)
    return original_route(page, pattern, handle, **kwargs)


with TemporaryDirectory() as temp, patch.object(Page, 'route', fixture_route), patch.object(BrowserType, 'launch', launch_chrome), patch.dict(d.ADAPTERS, qbittorrent=FixtureDownloader):
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
                context = browser.new_context(viewport={'width': width, 'height': 900})
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                authenticate_page(page, app, origin)
                page.goto(origin)
                expect(page.get_by_role('link', name='Safe Browser', exact=True)).to_be_visible()
                assert not page.evaluate('document.documentElement.scrollWidth > innerWidth + 1')
                page.get_by_role('link', name='Safe Browser', exact=True).click()
                address = page.get_by_role('textbox', name='Browser address')
                expect(address).to_have_value('')
                expect(page.locator('[data-browser-empty]')).to_be_visible()
                expect(page.locator('[data-browser-original]')).to_be_hidden()
                address.fill('https://safe-browser.example.test/')
                # Polling must preserve the URL before submission.
                page.wait_for_timeout(2600)
                expect(address).to_have_value('https://safe-browser.example.test/')
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
                click = {'x': 30 * box['width'] / state['viewport_width'], 'y': 110 * box['height'] / state['viewport_height']}
                screen.click(position=click)
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
                screen.click(position=click)
                expect(page.locator('.safe-browser-download-name')).to_have_count(1)
                assert len(FixtureDownloader.added) == 1
                page.screenshot(path=str(ROOT / f'.test-preview/safe-browser-magnet-{width}.png'))
                if page.locator('[data-browser-download-popover]').is_visible():
                    page.get_by_role('button', name='Downloads', exact=False).click()
                screen.click(position={'x': 30 * box['width'] / state['viewport_width'], 'y': 170 * box['height'] / state['viewport_height']})
                expect(page.get_by_role('link', name='Save file', exact=True)).to_have_count(1, timeout=15000)
                expect(page.locator('.safe-browser-download-name')).to_have_count(2)
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
print('PASS: desktop/mobile blank startup, navigation, magnet capture/review/submission, duplicate clicks, private URL blocking, back, and close.')
