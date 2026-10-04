"""Exercise async timeline filtering against a temporary Flask database."""
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


def run():
    with TemporaryDirectory() as temp:
        db = Path(temp) / 'timeline.db'
        app = create_app({'DATABASE_PATH': db, 'START_SCHEDULER': False, 'TESTING': True})
        profile = create_profile(db, FeedRequest('Sample feed', 'https://example.com', 'article', 'a', 'a', '', 25, 0, 'http'))
        with closing(connect_db(db)) as conn:
            for i in range(30):
                conn.execute('INSERT INTO feed_items(profile_id,title,link,summary,discovered_at) VALUES(?,?,?,?,?)',
                             (profile.id, f'Story {i:02}', f'https://example.com/{i}', '', '2026-10-01T12:00:00+00:00'))
            conn.commit()
        server = make_server('127.0.0.1', 0, app, threaded=True)
        Thread(target=server.serve_forever, daemon=True).start()
        origin = f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(channel='chrome', headless=True)
                for width in (390, 1440):
                    page = browser.new_page(viewport={'width': width, 'height': 900})
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(origin)
                    page.evaluate("window.originalSearch = document.querySelector('#search'); window.marker = 'same document';")
                    search = page.locator('#search')
                    reset = page.get_by_role('button', name='Reset search and filters')
                    expect(reset).to_be_hidden()
                    search.fill('Story 0')
                    page.wait_for_timeout(150)
                    assert page.locator('.item-card').count() == 25
                    expect(page.locator('.item-card')).to_have_count(10)
                    expect(search).to_be_focused()
                    assert page.evaluate("originalSearch === document.querySelector('#search') && marker === 'same document'")
                    assert page.evaluate('originalSearch.selectionStart') == 7
                    assert page.locator('.external-icon').count() == 10
                    assert page.locator('.privacy-icon').count() == 10
                    page.locator('.reading-filters summary').click()
                    expect(page.get_by_role('dialog', name='Search settings')).to_be_visible()
                    feed_option = page.locator('.search-settings-panel .check-option').filter(has=page.locator('[name=feed]'))
                    sort_label = page.locator('.search-settings-panel label[for=sort]').bounding_box()
                    for legend in page.locator('.search-settings-panel legend').all():
                        assert abs(legend.bounding_box()['x'] - sort_label['x']) < 1
                    assert feed_option.bounding_box()['height'] <= 48
                    assert feed_option.locator('span').bounding_box()['width'] > 200
                    if width == 390:
                        bounds = page.get_by_role('dialog').bounding_box()
                        assert round(bounds['y'] + bounds['height']) == 900
                    else:
                        bounds = page.get_by_role('dialog').bounding_box()
                        query_bounds = search.bounding_box()
                        assert bounds['y'] == query_bounds['y'] + query_bounds['height'] + 8
                        assert bounds['x'] + bounds['width'] == query_bounds['x'] + query_bounds['width']
                        assert not page.get_by_role('dialog').evaluate("el => el.matches(':modal')")
                    page.locator('#sort').select_option('title')
                    page.wait_for_url('**/*sort=title*')
                    expect(page.locator('.topic-title').first).to_have_text('Story 00')
                    page.locator('[name=feed]').check()
                    page.wait_for_url('**/*feed=*')
                    expect(page.locator('.search-filter-dot')).to_be_visible()
                    page.get_by_role('button', name='Close search settings').click()
                    expect(page.locator('.reading-filters summary')).to_be_focused()
                    reset.click()
                    expect(page.locator('.item-card')).to_have_count(25)
                    expect(search).to_have_value('')
                    expect(reset).to_be_hidden()
                    expect(page.locator('.search-filter-dot')).to_be_hidden()
                    expect(page.locator('#sort')).to_have_value('new')
                    assert not page.locator('[name=feed]').is_checked()
                    page.get_by_role('link', name='Next', exact=True).click()
                    expect(page.locator('.item-card')).to_have_count(5)
                    page.wait_for_url('**/*page=2*')
                    page.go_back()
                    expect(page.locator('.item-card')).to_have_count(25)
                    page.go_back()
                    expect(search).to_have_value('Story 0')
                    expect(page.locator('.item-card')).to_have_count(10)
                    # A response already in flight must not replace newer input, even before its debounce fires.
                    page.evaluate("""() => {
                        window.realFetch = window.fetch;
                        window.fetch = async (...args) => {
                            const response = await realFetch(...args);
                            if (String(args[0]).includes('q=missing')) await new Promise(resolve => setTimeout(resolve, 900));
                            return response;
                        };
                    }""")
                    search.fill('missing')
                    page.wait_for_timeout(550)
                    search.fill('Story 1')
                    expect(page.locator('.item-card')).to_have_count(10)
                    page.wait_for_url('**/*q=Story+1*')
                    page.wait_for_timeout(600)
                    expect(page.locator('.topic-title').first).to_contain_text('Story 1')
                    expect(search).to_be_focused()
                    page.evaluate("() => { window.fetch = () => Promise.reject(new Error('offline')); }")
                    search.fill('Story 2')
                    expect(page.locator('[data-search-status]')).to_contain_text('Could not update')
                    expect(page.locator('.item-card')).to_have_count(10)
                    expect(search).to_be_focused()
                    page.evaluate('() => { window.fetch = window.realFetch; }')
                    page.get_by_role('button', name='Retry', exact=True).click()
                    page.wait_for_url('**/*q=Story+2*')
                    expect(page.locator('.topic-title').first).to_contain_text('Story 2')
                    assert page.evaluate("originalSearch === document.querySelector('#search') && marker === 'same document'")
                    assert not errors, errors
                    # Sort alone makes reset available; Escape dismisses the sheet and restores focus.
                    reset.click()
                    page.locator('.reading-filters summary').click()
                    page.locator('#sort').select_option('oldest')
                    page.wait_for_url('**/*sort=oldest*')
                    page.keyboard.press('Escape')
                    expect(page.locator('.reading-filters summary')).to_be_focused()
                    expect(reset).to_be_visible()
                    reset.click()
                    expect(reset).to_be_hidden()
                    output = ROOT / '.test-preview/refreshed-ui'
                    output.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(output / f'timeline-search-{width}.png'))
                    page.locator('.reading-filters summary').click()
                    page.screenshot(path=str(output / f'timeline-search-settings-{width}.png'))
                    if width == 1440:
                        search.click()
                        expect(page.get_by_role('dialog')).to_be_hidden()
                        expect(search).to_be_focused()
                        page.locator('.reading-filters summary').click()
                        page.set_viewport_size({'width': 390, 'height': 900})
                        expect(page.get_by_role('dialog')).to_be_hidden()
                        page.locator('.reading-filters summary').click()
                        assert page.get_by_role('dialog').evaluate("el => el.matches(':modal')")
                    page.close()
                # Native submission remains available without JavaScript.
                context = browser.new_context(java_script_enabled=False)
                page = context.new_page()
                page.goto(origin)
                page.locator('#search').fill('Story 0')
                page.get_by_role('button', name='Search', exact=True).click()
                expect(page.locator('.item-card')).to_have_count(10)
                browser.close()
        finally:
            server.shutdown()
    print('Async timeline: focus, debounce, filters, pagination, history, stale responses, retry, and no-JS passed.')


if __name__ == '__main__':
    run()
