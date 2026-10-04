"""Action notices expire, preserve errors/progress, and do not replay on reload."""
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import patch
import sys
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server
from rss_site_bridge.app import create_app, create_profile, FeedRequest, FeedEntry

with TemporaryDirectory() as temp, patch('rss_site_bridge.app.extract_feed_entries', return_value=[FeedEntry('Topic', 'https://example.com/topic', '', datetime.now(timezone.utc))]):
    db = Path(temp) / 'notices.db'
    app = create_app({'DATABASE_PATH': db, 'START_SCHEDULER': False, 'TESTING': True})
    profile = create_profile(db, FeedRequest('News', 'https://example.com', 'article', 'a', 'a', '', 100, 0, 'http'))
    server = make_server('127.0.0.1', 0, app, threaded=True)
    Thread(target=server.serve_forever, daemon=True).start()
    origin = f'http://127.0.0.1:{server.server_port}'
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='chrome', headless=True)
            page = browser.new_page()
            page.clock.install()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            for parameter in ('created', 'saved', 'purged'):
                page.goto(origin + f'/profiles/{profile.id}?view=rss&{parameter}=1')
                notice = page.locator('[data-transient-notice]')
                expect(notice).to_be_visible()
                assert parameter + '=' not in page.url and 'view=rss' in page.url
                page.clock.fast_forward(6100)
                expect(notice).to_be_hidden()
                page.reload()
                expect(page.locator('[data-transient-notice]')).to_have_count(0)
            page.goto(origin + f'/profiles/{profile.id}?purged=1')
            page.get_by_role('button', name='Refresh now').click()
            status = page.locator('[data-refresh-result]')
            expect(status).to_contain_text('Refresh complete:')
            expect(page.locator('[data-transient-notice]')).to_be_hidden()
            # Starting another refresh must cancel the previous success timer.
            pending = []
            pattern = f'**/profiles/{profile.id}/refresh'
            page.route(pattern, lambda route: pending.append(route))
            page.get_by_role('button', name='Refresh now').click()
            expect(status).to_have_text('Fetching feed…')
            page.clock.fast_forward(6100)
            expect(status).to_be_visible()
            pending[0].fulfill(status=500, json={'status': 'error', 'error': 'Source unavailable.'})
            expect(status).to_have_text('Source unavailable.')
            page.clock.fast_forward(6100)
            expect(status).to_be_visible()
            page.unroute(pattern)
            page.get_by_role('button', name='Refresh now').click()
            expect(status).to_contain_text('Already up to date.')
            page.clock.fast_forward(6100)
            expect(status).to_be_hidden()
            page.goto(origin + f'/profiles/{profile.id}?view=rss')
            page.evaluate("Object.defineProperty(navigator, 'clipboard', {value: {writeText: () => Promise.resolve()}, configurable: true})")
            page.get_by_role('button', name='Copy feed URL').click()
            copy = page.locator('[data-copy-status]')
            expect(copy).to_have_text('Feed URL copied.')
            page.clock.fast_forward(6100)
            expect(copy).to_be_hidden()
            page.evaluate("() => { navigator.clipboard.writeText = () => Promise.reject(new Error('denied')); }")
            page.get_by_role('button', name='Copy feed URL').click()
            expect(copy).to_contain_text('Copy unavailable.')
            page.clock.fast_forward(6100)
            expect(copy).to_be_visible()
            page.goto(origin + '/settings?saved=1&test_sent=1')
            expect(page.locator('[data-transient-notice]')).to_have_count(2)
            assert '?' not in page.url
            page.clock.fast_forward(6100)
            expect(page.locator('.settings-notices')).to_be_hidden()
            page.reload()
            expect(page.locator('[data-transient-notice]')).to_have_count(0)
            page.goto(origin + '/settings/downloaders')
            page.locator('#downloader-name').fill('Home')
            page.locator('#downloader-url').fill('http://localhost:8080')
            page.locator('#downloader-auth').select_option('none')
            page.locator('#downloader-extensions').fill('.torrent')
            page.get_by_role('button', name='Save downloader').click()
            test = page.get_by_role('button', name='Test / refresh categories')
            expect(test).to_be_visible()
            page.route('**/settings/downloaders/*/test', lambda route: route.fulfill(json={'version': 'Fixture', 'api_version': '1', 'categories': []}))
            test.click()
            output = page.locator('[data-test-result]')
            expect(output).to_contain_text('Connected:')
            page.clock.fast_forward(6100)
            expect(output).to_be_hidden()
            page.unroute('**/settings/downloaders/*/test')
            page.route('**/settings/downloaders/*/test', lambda route: route.fulfill(status=500, json={'error': 'Connection failed.'}))
            test.click()
            expect(output).to_have_text('Connection failed.')
            page.clock.fast_forward(6100)
            expect(output).to_be_visible()
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
print('PASS: feed/settings/copy/downloader success expiry, reload cleanup, progress timer cancellation, and persistent errors.')
