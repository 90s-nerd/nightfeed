"""Direct item links, reload, history, and pagination on desktop and mobile."""
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server
from rss_site_bridge.app import create_profile, FeedRequest, connect_db
from ui_auth_support import create_ui_app, authenticate_page


def run():
    with TemporaryDirectory() as temp:
        db = Path(temp) / 'links.db'
        app = create_ui_app(dict(TESTING=True, DATABASE_PATH=db, START_SCHEDULER=False))
        profile = create_profile(db, FeedRequest('News', 'https://example.com', 'article', 'a', 'a', '', 100, 0, 'http'))
        ids = []
        with closing(connect_db(db)) as conn:
            for i in range(60):
                ids.append(conn.execute('INSERT INTO feed_items(profile_id,title,link,summary,discovered_at) VALUES(?,?,?,?,?)',
                    (profile.id, f'Story {i:02}', f'https://example.com/{i}', '', '2026-10-01T12:00:00+00:00')).lastrowid)
            conn.commit()
        server = make_server('127.0.0.1', 0, app, threaded=True)
        Thread(target=server.serve_forever, daemon=True).start()
        origin = f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(channel='chrome', headless=True)
                for width in (390, 1440):
                    page = browser.new_page(viewport=dict(width=width, height=900))
                    authenticate_page(page, app, origin)
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))

                    def check(index):
                        selected = page.locator('[data-selected-item]')
                        expect(selected).to_have_count(1)
                        expect(selected).to_have_attribute('data-topic-id', str(ids[index]))
                        expect(selected).to_be_focused()
                        expect(selected).to_be_in_viewport()
                        assert selected.evaluate("el => getComputedStyle(el).outlineStyle") == 'solid'
                        expect(page.locator('[data-items-view]')).to_contain_text(f'Page {index // 25 + 1} of 3')

                    for index in (20, 45, 59):
                        url = f'{origin}/profiles/{profile.id}?item={ids[index]}'
                        page.goto(url)
                        check(index)
                        page.evaluate('window.scrollTo(0, 0)')
                        page.reload()
                        check(index)
                        assert page.url == url

                    page.get_by_role('link', name='Previous', exact=True).click()
                    expect(page.locator('[data-items-view]')).to_contain_text('Page 2 of 3')
                    expect(page.locator('[data-selected-item]')).to_have_count(0)
                    page.go_back()
                    check(59)
                    page.goto(f'{origin}/profiles/{profile.id}?item={ids[45]}&page=1&view=configuration')
                    check(45)
                    # Exercise the replacement DOM after an inline feed refresh.
                    page.route(f'**/profiles/{profile.id}/refresh', lambda route: route.fulfill(
                        json=dict(status='ok', message='Already up to date.', unread_notifications=0)))
                    page.get_by_role('button', name='Refresh now', exact=True).click()
                    expect(page.locator('[data-refresh-result]')).to_have_text('Already up to date.')
                    check(45)
                    page.goto(f'{origin}/?page=2')
                    expect(page.locator('.item-card')).to_have_count(25)
                    expect(page.locator('[data-selected-item]')).to_have_count(0)
                    assert not errors, errors
                    page.close()
                browser.close()
        finally:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    run()
    print('PASS: desktop/mobile direct item links, reload, history, highlighting, and feed/timeline pagination.')
