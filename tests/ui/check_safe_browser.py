"""Check blank Safe Browser sessions with real Chromium and public-URL fixtures."""
from pathlib import Path
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
from ui_auth_support import create_ui_app, authenticate_page

logging.disable(logging.CRITICAL)
original_route = Page.route
original_launch = BrowserType.launch


def launch_chrome(browser_type, **kwargs):
    return original_launch(browser_type, channel='chrome', **kwargs)


def fixture_route(page, pattern, handler, **kwargs):
    def handle(route):
        if route.request.url == 'https://safe-browser.example.test/':
            route.fulfill(content_type='text/html', body='<title>Browser fixture</title><h1>Website loaded</h1>')
        else:
            handler(route)
    return original_route(page, pattern, handle, **kwargs)


with TemporaryDirectory() as temp, patch.object(Page, 'route', fixture_route), patch.object(BrowserType, 'launch', launch_chrome):
    app = create_ui_app({'DATABASE_PATH': Path(temp) / 'browser.db', 'START_SCHEDULER': False, 'TESTING': True})
    server = make_server('127.0.0.1', 0, app, threaded=True)
    Thread(target=server.serve_forever, daemon=True).start()
    origin = f'http://127.0.0.1:{server.server_port}'
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            for width in (390, 1440):
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
                address.fill('http://127.0.0.1/')
                page.get_by_role('button', name='Go', exact=True).click()
                expect(page.locator('[data-browser-loading]')).to_contain_text('Only public http or https')
                page.get_by_role('button', name='Back', exact=True).click()
                expect(address).to_have_value('')
                expect(page.locator('[data-browser-empty]')).to_be_visible()
                page.get_by_role('button', name='Close session', exact=True).click()
                expect(page).to_have_url(origin + '/')
                assert not errors, errors
                context.close()
            browser.close()
    finally:
        for session in list(_safe_browser_sessions.values()):
            session.stop()
        server.shutdown()
print('PASS: desktop/mobile blank startup, URL entry, polling, navigation, private URL blocking, back, and close.')
