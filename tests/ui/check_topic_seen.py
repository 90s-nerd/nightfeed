"""Real viewport tracking with fixture topics and an isolated database."""
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import sys
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server
from rss_site_bridge.app import create_app, create_profile, FeedRequest, connect_db

with TemporaryDirectory() as temp:
    db = Path(temp) / 'seen.db'
    app = create_app({'DATABASE_PATH': db, 'START_SCHEDULER': False, 'TESTING': True})
    profile = create_profile(db, FeedRequest('News', 'https://example.com', 'article', 'a', 'a', '', 100, 0, 'http'))
    with closing(connect_db(db)) as conn:
        for i in range(60):
            conn.execute('INSERT INTO feed_items(profile_id,title,link,summary,discovered_at) VALUES(?,?,?,?,?)',
                         (profile.id, f'Topic {i:02}', f'https://example.com/{i}', 'A summary of the topic.', '2026-10-01T12:00:00+00:00'))
        conn.commit()
    def seen(identity):
        with closing(connect_db(db)) as conn:
            return conn.execute('SELECT seen_at FROM feed_items WHERE id=?', (identity,)).fetchone()[0]
    server = make_server('127.0.0.1', 0, app, threaded=True)
    Thread(target=server.serve_forever, daemon=True).start()
    origin = f'http://127.0.0.1:{server.server_port}'
    errors = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='chrome', headless=True)
            context = browser.new_context(viewport={'width': 390, 'height': 700})
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(origin)
            page.bring_to_front()
            # Open synchronously before locator/assertion waits can consume dwell time.
            page.evaluate("document.querySelector('.search-settings summary').click()")
            assert page.locator('dialog:modal').count() == 1
            first = page.locator('[data-topic-id]').first
            identity = int(first.get_attribute('data-topic-id'))
            distant = int(page.locator('[data-topic-id]').last.get_attribute('data-topic-id'))
            expect(first.locator('.topic-new')).to_be_visible()
            # Opening the sheet cancels the dwell timer and pauses observations.
            page.wait_for_timeout(3000)
            assert seen(identity) is None
            page.get_by_role('button', name='Close search settings').click()
            page.wait_for_timeout(3000)
            assert seen(identity)
            assert seen(distant) is None
            expect(first.locator('.topic-new')).to_be_visible()
            # A fast pass over an offscreen topic must not mark it seen.
            last = page.locator('[data-topic-id]').last
            last.scroll_into_view_if_needed()
            page.wait_for_timeout(200)
            page.evaluate('window.scrollTo(0, 0)')
            page.wait_for_timeout(2800)
            assert seen(distant) is None
            last.scroll_into_view_if_needed()
            page.wait_for_timeout(3000)
            assert seen(distant)
            # A fresh visit no longer labels an acknowledged topic NEW.
            page.goto(origin + f'/profiles/{profile.id}')
            expect(page.locator(f'[data-topic-id="{identity}"] .topic-new')).to_have_count(0)
            # Async results are observed, too; request a topic never on the first page.
            page.goto(origin)
            page.locator('#search').fill('Topic 00')
            page.wait_for_url('**/*q=Topic+00*')
            page.wait_for_timeout(3000)
            assert seen(1)
            # Failed acknowledgements are retried without removing the badge.
            page.goto(origin)
            page.route('**/api/topics/seen', lambda route: route.abort())
            page.locator('#search').fill('Topic 01')
            page.wait_for_url('**/*q=Topic+01*')
            page.wait_for_timeout(3000)
            assert seen(2) is None
            page.unroute('**/api/topics/seen')
            page.wait_for_timeout(6000)
            assert seen(2)
            expect(page.locator('[data-topic-id="2"] .topic-new')).to_be_visible()
            # A background tab cannot acknowledge topics.
            page.goto('about:blank')
            page.wait_for_timeout(500)
            # Headless Chrome reports all tabs visible/focused. Override its browser
            # signals before scripts run, then dispatch the real lifecycle event.
            page.add_init_script("""if (location.protocol !== 'about:') {
                window.topicTestInactive = true;
                Object.defineProperty(document, 'hidden', {get: () => window.topicTestInactive});
                document.hasFocus = () => !window.topicTestInactive;
            }""")
            page.goto(origin)
            identity = int(page.locator('[data-topic-id]').first.get_attribute('data-topic-id'))
            page.wait_for_timeout(3000)
            assert seen(identity) is None
            page.evaluate("window.topicTestInactive = false; document.dispatchEvent(new Event('visibilitychange'))")
            page.wait_for_timeout(3000)
            assert seen(identity)
            # The fixed mobile header must reduce the effective observation viewport.
            page.goto(origin)
            page.evaluate("window.topicTestInactive = false; document.dispatchEvent(new Event('visibilitychange'))")
            identity = int(page.locator('[data-topic-id]').first.get_attribute('data-topic-id'))
            title = page.locator('[data-topic-title]').first
            title.evaluate('el => window.scrollTo(0, el.getBoundingClientRect().top + scrollY - 54)')
            page.wait_for_timeout(3000)
            assert seen(identity) is None
            title.evaluate('el => window.scrollTo(0, el.getBoundingClientRect().top + scrollY - 76)')
            page.wait_for_timeout(3000)
            assert seen(identity)
            output = ROOT / '.test-preview/refreshed-ui'
            for width in (320, 390, 1440):
                page.set_viewport_size({'width': width, 'height': 900})
                page.goto(origin)
                page.evaluate("window.topicTestInactive = false; document.dispatchEvent(new Event('visibilitychange'))")
                assert not page.evaluate('document.documentElement.scrollWidth > innerWidth + 1')
                page.screenshot(path=str(output / f'unseen-timeline-{width}.png'))
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
print('PASS: viewport dwell, modal pause, quick scroll, offscreen topics, async results, stable badges, retries, simulated inactive tabs, header occlusion, and responsive layouts.')
