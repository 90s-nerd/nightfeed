"""Notification reports using temporary data and fixture refreshes."""
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import patch
from urllib.error import HTTPError
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server
from rss_site_bridge.app import (create_app, create_profile, FeedRequest, FeedEntry, refresh_profile,
                                list_notifications, create_notification)


with TemporaryDirectory() as temp:
    db = Path(temp) / 'reports.db'
    app = create_app({'DATABASE_PATH': db, 'START_SCHEDULER': False, 'TESTING': True})
    profile = create_profile(db, FeedRequest('Sample news', 'https://example.com', 'article', 'a', 'a', '', 25, 0, 'http'))
    now = datetime.now(timezone.utc)
    with patch('rss_site_bridge.app.extract_feed_entries', return_value=[FeedEntry('Original title', 'https://example.com/one', 'Original summary', now)]):
        refresh_profile(db, profile.id)
    entries = [FeedEntry('Updated title', 'https://example.com/one', 'Updated summary', now),
               FeedEntry('A newly discovered story', 'https://example.com/two', 'A new summary.', now)]
    with patch('rss_site_bridge.app.extract_feed_entries', return_value=entries):
        refresh_profile(db, profile.id)
    success = list_notifications(db)[0]
    def fail(config, *, progress):
        progress('Fetching content', 'Downloading the source page over HTTP.')
        try:
            raise HTTPError(config.source_url, 403, 'Forbidden', {}, None)
        except HTTPError as error:
            raise RuntimeError('Upstream HTTP error: 403 Forbidden. The source denied this request.') from error
    with patch('rss_site_bridge.app.extract_feed_entries', side_effect=fail):
        try:
            refresh_profile(db, profile.id)
        except RuntimeError:
            pass
    failure = list_notifications(db)[0]
    legacy = create_notification(db, profile_id=profile.id, event_type='refresh', severity='info', category='success',
                                 title='An older refresh', message='Nightfeed saved 5 entries for this feed.')
    server = make_server('127.0.0.1', 0, app)
    Thread(target=server.serve_forever, daemon=True).start()
    origin = f'http://127.0.0.1:{server.server_port}'
    errors = []
    output = ROOT / '.test-preview/refreshed-ui'
    output.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='chrome', headless=True)
            page = browser.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            for theme in ('light', 'dark'):
                page.goto(origin + '/settings')
                page.locator('[data-appearance]').select_option(theme)
                for width in (320, 390, 1440):
                    page.set_viewport_size({'width': width, 'height': 1000})
                    for notification, kind in ((success, 'success'), (failure, 'failure'), (legacy, 'legacy')):
                        page.goto(origin + f'/notifications/{notification.id}?status=all')
                        assert not page.evaluate('document.documentElement.scrollWidth > innerWidth + 1'), (width, theme, kind)
                        if kind == 'success':
                            expect(page.locator('.refresh-report')).to_contain_text('Original title')
                            expect(page.locator('.refresh-report')).to_contain_text('Updated summary')
                            expect(page.locator('.refresh-report')).to_contain_text('A newly discovered story')
                            assert page.locator('.report-entry .external-icon').count() == 2
                            assert page.locator('.report-entry .safe-link').count() == 2
                            assert page.locator('.report-entry .privacy-icon').count() == 2
                            assert page.locator('.report-entry .safe-link').first.evaluate("el => getComputedStyle(el).backgroundColor === 'rgba(0, 0, 0, 0)' && getComputedStyle(el).borderWidth === '0px'")
                        if kind == 'failure':
                            expect(page.locator('.refresh-report')).to_contain_text('HTTP 403')
                            expect(page.locator('.refresh-report')).to_contain_text('Fetching content')
                            page.get_by_text('Troubleshooting', exact=True).click()
                            expect(page.locator('.refresh-report')).to_contain_text('Check whether the source is accessible')
                            page.get_by_text('Troubleshooting', exact=True).click()
                        if width in (390, 1440) and kind != 'legacy':
                            page.screenshot(path=str(output / f'notification-report-{kind}-{width}-{theme}.png'), full_page=True)
                    page.goto(origin + '/notifications?status=all')
                    row = page.locator('.notification-card').filter(has_text=failure.title)
                    message = row.locator('.notification-message').bounding_box()
                    page.mouse.click(message['x'] + message['width'] / 2, message['y'] + message['height'] / 2)
                    page.wait_for_url(f'**/notifications/{failure.id}?status=all')
                    page.get_by_role('link', name='← Notifications').click()
                    row = page.locator('.notification-card').filter(has=page.locator(f'a[href="/notifications/{success.id}?status=all"]'))
                    row.locator('summary').click()
                    expect(row.get_by_role('button', name='Mark read', exact=True)).to_be_hidden()
                    expect(row.get_by_role('button', name='Delete', exact=True)).to_be_visible()
                    assert 'notification-unread' not in row.get_attribute('class')
                    page.keyboard.press('Escape')
                    row.get_by_role('link', name='Sample news', exact=True).click()
                    page.wait_for_url(f'**/profiles/{profile.id}')
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
print('PASS: notification reports, snapshot content, failure diagnostics, row/menu/feed clicks, and 18 responsive/theme layouts.')
