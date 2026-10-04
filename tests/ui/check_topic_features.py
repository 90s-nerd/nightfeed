"""Exercise bookmarks, New only, change summaries, and reset on desktop/mobile."""
from contextlib import closing
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
from rss_site_bridge.app import create_app, create_profile, FeedRequest, FeedEntry, connect_db, refresh_profile, mark_topics_seen

with TemporaryDirectory() as temp:
    db = Path(temp) / 'topics.db'
    app = create_app({'DATABASE_PATH': db, 'START_SCHEDULER': False, 'TESTING': True})
    profile = create_profile(db, FeedRequest('News', 'https://example.com', 'article', 'a', 'a', '', 100, 0, 'http'))
    server = make_server('127.0.0.1', 0, app, threaded=True)
    Thread(target=server.serve_forever, daemon=True).start()
    origin = f'http://127.0.0.1:{server.server_port}'
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='chrome', headless=True)
            for width in (390, 1440):
                with closing(connect_db(db)) as conn:
                    conn.execute('DELETE FROM feed_items')
                    for i in range(30):
                        conn.execute('INSERT INTO feed_items(id,profile_id,title,link,summary,discovered_at) VALUES(?,?,?,?,?,?)',
                                     (i+1, profile.id, f'Topic {i:02}', f'https://example.com/{i}', '', '2026-10-01T12:00:00+00:00'))
                    conn.commit()
                page = browser.new_page(viewport={'width': width, 'height': 850})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                # Control seen acknowledgements independently of browser-action latency.
                page.add_init_script("document.hasFocus = () => !!window.topicTestActive")
                page.goto(origin)
                topic = page.locator('[data-topic-id="30"]')
                topic.get_by_role('button', name='Save for later').click()
                expect(topic.get_by_role('button', name='Remove from saved')).to_have_attribute('aria-pressed', 'true')
                page.locator('.search-settings summary').click()
                page.locator('[name=saved_only]').check()
                expect(page.locator('[data-topic-id]')).to_have_count(1)
                page.locator('[name=new_only]').check()
                page.wait_for_url('**/*new_only=1*')
                expect(page.locator('[data-topic-id]')).to_have_count(1)
                page.get_by_role('button', name='Close search settings').click()
                page.get_by_role('button', name='Reset search and filters').click()
                expect(page.locator('[data-topic-id]')).to_have_count(25)
                expect(page.locator('[name=new_only]')).not_to_be_checked()
                expect(page.locator('[name=saved_only]')).not_to_be_checked()
                page.go_back()
                expect(page.locator('[data-topic-id]')).to_have_count(1)
                expect(page.locator('[name=new_only]')).to_be_checked()
                expect(page.locator('[name=saved_only]')).to_be_checked()
                # A seen topic retains its bookmark and gets a separate update badge.
                mark_topics_seen(db, [30])
                with patch('rss_site_bridge.app.extract_feed_entries', return_value=[FeedEntry('Changed topic', 'https://example.com/29', 'New summary', datetime.now(timezone.utc))]):
                    refresh_profile(db, profile.id)
                page.goto(origin + '?saved_only=1')
                expect(page.locator('.topic-updated')).to_have_text('UPDATED')
                expect(page.locator('.topic-new')).to_have_count(0)
                page.get_by_text('What changed', exact=True).click()
                expect(page.locator('.topic-changes')).to_contain_text('Topic 29')
                expect(page.locator('.topic-changes')).to_contain_text('New summary')
                assert not page.evaluate('document.documentElement.scrollWidth > innerWidth + 1')
                page.screenshot(path=str(ROOT / f'.test-preview/refreshed-ui/topic-features-{width}.png'))
                page.evaluate("window.topicTestActive = true; window.dispatchEvent(new Event('focus'))")
                page.wait_for_timeout(3200)
                page.reload()
                expect(page.locator('.topic-updated')).to_have_count(0)
                expect(page.get_by_role('button', name='Remove from saved')).to_have_count(1)
                page.get_by_role('button', name='Remove from saved').click()
                expect(page.get_by_role('button', name='Save for later')).to_have_count(1)
                page.reload()
                expect(page.locator('[data-topic-id]')).to_have_count(0)
                page.goto(origin + '?new_only=1')
                page.locator('.search-settings summary').click()
                page.get_by_role('button', name='Mark all topics seen').click()
                expect(page.locator('[data-topic-id]')).to_have_count(0)
                assert not errors, errors
                page.close()
            browser.close()
    finally:
        server.shutdown()
print('PASS: mobile/desktop bookmarks, filters, reset/history, update summaries, seen acknowledgements, bulk catch-up, and layout.')
