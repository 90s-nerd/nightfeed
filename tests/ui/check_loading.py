"""Delayed navigation and API actions show local feedback and recover cleanly."""
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

with TemporaryDirectory() as temp:
    db = Path(temp) / 'loading.db'
    app = create_ui_app({'DATABASE_PATH': db, 'START_SCHEDULER': False, 'TESTING': True})
    profile = create_profile(db, FeedRequest('News', 'https://example.com', 'article', 'a', 'a', '', 100, 0, 'http'))
    with closing(connect_db(db)) as conn:
        conn.execute('INSERT INTO feed_items(profile_id,title,link,summary,discovered_at) VALUES(?,?,?,?,?)',
                     (profile.id, 'Topic', 'https://example.com/topic', '', '2026-10-01T12:00:00+00:00'))
        conn.commit()
    server = make_server('127.0.0.1', 0, app, threaded=True)
    Thread(target=server.serve_forever, daemon=True).start()
    origin = f'http://127.0.0.1:{server.server_port}'
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='chrome', headless=True)
            for width in (390, 1440):
                page = browser.new_page(viewport={'width': width, 'height': 850})
                authenticate_page(page, app, origin)
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(origin)
                pending = []
                page.route('**/api/topics/*/save', lambda route: pending.append(route))
                save = page.locator('[data-topic-save]').first
                save.click()
                expect(save).to_have_attribute('aria-busy', 'true')
                expect(save).to_be_disabled()
                expect(page.locator('.nav-link').first).not_to_have_attribute('aria-busy', 'true')
                # Error removes the spinner, preserves the action and permits retry.
                page.wait_for_timeout(100)
                pending.pop().fulfill(status=500, json={'error': 'Unavailable'})
                expect(save).not_to_have_attribute('aria-busy', 'true')
                expect(save).to_be_enabled()
                save.click()
                expect(save).to_have_attribute('aria-busy', 'true')
                page.wait_for_timeout(100)
                pending.pop().fulfill(json={'saved': True})
                expect(save).to_have_attribute('aria-pressed', 'true')
                expect(save).to_contain_text('Saved')
                expect(save).not_to_have_class('action-pending')
                # Hold navigation at the browser boundary to inspect the initiating page.
                page.evaluate('''window.addEventListener('click', event => {
                  const link = event.target.closest('a'); if (!link) return;
                  window.loadingSnapshot = {busy:link.getAttribute('aria-busy'), text:link.textContent.trim(), spinner:getComputedStyle(link, '::after').content};
                  event.preventDefault();
                })''')
                for selector in ('.safe-link', '.nav-link[href="/safe-browser"]'):
                    link = page.locator(selector).first
                    link.click()
                    state = page.evaluate('window.loadingSnapshot')
                    assert state['busy'] == 'true', state
                    assert state['text'] == ('Opening…' if selector == '.safe-link' else 'Safe Browser'), state
                    assert state['spinner'] == '\"\"', state
                    # Cancelled navigation must restore the link for another attempt.
                    expect(page.locator('.action-pending')).to_have_count(0)
                    page.evaluate("selector => nightfeedBusy(document.querySelector(selector), true, selector === '.safe-link' ? 'Opening…' : '')", selector)
                    # The repeated event is cancelled before reaching any action handler.
                    assert not link.evaluate("el => el.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true}))")
                    page.screenshot(path=f"/tmp/nightfeed-loading-{width}-{'open' if selector == '.safe-link' else 'nav'}.png")
                    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted:true}))")
                    expect(page.locator('.action-pending')).to_have_count(0)
                    expect(page.locator('.safe-link').first).to_have_text('Open safely')
                # Modified/new-tab actions and same-page anchors stay unchanged.
                page.evaluate("""() => {
                  const link = document.querySelector('.safe-link');
                  link.addEventListener('click', e => e.preventDefault(), {once:true});
                  link.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, ctrlKey:true}));
                }""")
                expect(page.locator('.action-pending')).to_have_count(0)
                page.emulate_media(reduced_motion='reduce')
                page.evaluate("nightfeedBusy(document.querySelector('.safe-link'))")
                assert page.locator('.safe-link').first.evaluate("el => getComputedStyle(el, '::after').animationName") == 'none'
                page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted:true}))")
                expect(page.locator('.action-pending')).to_have_count(0)
                assert not errors, errors
                page.close()
            browser.close()
    finally:
        server.shutdown()
print('Loading feedback checks passed on mobile and desktop.')
