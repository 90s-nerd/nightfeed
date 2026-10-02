"""Real Flask/browser smoke check. Run with Python + Playwright + installed Chrome.

Uses a temporary SQLite database, never the configured app database. Screenshots
and a JSON report are written under the ignored .test-preview directory.
"""
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from contextlib import closing
import json
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server
from rss_site_bridge.app import create_app, create_profile, FeedRequest, connect_db


def run():
    output = ROOT / '.test-preview' / 'refreshed-ui'
    output.mkdir(parents=True, exist_ok=True)
    checks, errors = [], []
    with TemporaryDirectory() as temp:
        db = Path(temp) / 'ui.db'
        app = create_app({'DATABASE_PATH': db, 'START_SCHEDULER': False, 'TESTING': True})
        profile = create_profile(db, FeedRequest('Design & technology', 'https://example.com/topics', 'article', 'a', 'a', '', 25, 0, 'http'))
        with closing(connect_db(db)) as conn:
            for index in range(30):
                conn.execute('INSERT INTO feed_items (profile_id,title,link,summary,discovered_at) VALUES (?,?,?,?,?)',
                             (profile.id, f'A thoughtful update {index + 1}', f'https://example.com/topics/{index}',
                              'Ideas, useful discoveries, and stories from around the web.', '2026-10-01T12:00:00+00:00'))
            conn.commit()
        server = make_server('127.0.0.1', 0, app)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(channel='chrome', headless=True)
                page = browser.new_page()
                page.on('pageerror', lambda error: errors.append(str(error)))
                routes = {'timeline': '/', 'feeds': '/feeds', 'settings': '/settings', 'compose': '/compose',
                          'detail': f'/profiles/{profile.id}', 'notifications': '/notifications',
                          'downloaders': '/settings/downloaders', 'xml': f'/feeds/{profile.feed_token}/view',
                          'configuration': f'/profiles/{profile.id}?view=configuration',
                          'rss': f'/profiles/{profile.id}?view=rss'}
                for theme in ['light', 'dark']:
                    page.goto(origin + '/settings')
                    page.locator('[data-appearance]').select_option(theme)
                    for width in [320, 390, 768, 1024, 1440]:
                        page.set_viewport_size({'width': width, 'height': 1000})
                        for name, route in routes.items():
                            response = page.goto(origin + route)
                            assert response.status == 200, (route, response.status)
                            assert page.locator('html').get_attribute('data-theme') == theme
                            overflow = page.evaluate('document.documentElement.scrollWidth > innerWidth + 1')
                            assert not overflow, (name, width, theme, 'horizontal overflow')
                            panel_spacing = page.locator('.surface.section, .surface.settings-frame').evaluate_all('''panels => panels.filter(panel => panel.getClientRects().length).map(panel => {
                                const visible = [...panel.children].filter(child => !['SCRIPT', 'TEMPLATE', 'NOSCRIPT'].includes(child.tagName) && child.checkVisibility() && getComputedStyle(child).position !== 'absolute');
                                const last = visible.at(-1);
                                const style = getComputedStyle(panel);
                                return {panel: panel.className, extra: last && getComputedStyle(last).position !== 'sticky' ? panel.getBoundingClientRect().bottom - last.getBoundingClientRect().bottom - parseFloat(style.paddingBottom) - parseFloat(style.borderBottomWidth) : 0};
                            })''')
                            assert all(abs(panel['extra']) < 2 for panel in panel_spacing), (name, width, theme, panel_spacing)
                            nav = page.get_by_role('navigation', name='Primary navigation')
                            assert nav.is_visible(), (name, width, 'hidden navigation')
                            for label in ['Timeline', 'Feeds', 'Notifications', 'Settings']:
                                link = nav.get_by_role('link', name=label, exact=True)
                                assert link.is_visible(), (label, width)
                                box = link.bounding_box()
                                assert box['height'] >= 44 and box['x'] >= 0 and box['x'] + box['width'] <= width + 1
                                assert box['y'] >= 0 and box['y'] + box['height'] <= 1001, (label, width, 'navigation outside viewport')
                                assert link.evaluate('(el) => { const r = el.getBoundingClientRect(); return el.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)); }'), (label, width, 'navigation occluded')
                            checks.append({'screen': name, 'width': width, 'theme': theme, 'overflow': False})
                            if name in ['timeline', 'feeds', 'settings', 'xml', 'compose', 'detail', 'configuration', 'rss'] and width in [390, 1440]:
                                page.screenshot(path=str(output / f'{name}-{width}-{theme}.png'))
                page.set_viewport_size({'width': 390, 'height': 1000})
                page.goto(origin + '/feeds')
                more = page.locator('[data-feed-more]').first
                assert more.locator('.ellipsis-icon').inner_text() == '⋮'
                assert more.evaluate("el => getComputedStyle(el).borderTopWidth === '0px'")
                card = page.locator('[data-profile-card]').first
                assert card.evaluate("el => el.dispatchEvent(new MouseEvent('contextmenu', {bubbles: true, cancelable: true}))")
                assert not page.locator('#feed-context-menu').is_visible()
                more.focus()
                page.keyboard.press('Enter')
                assert page.locator('#feed-context-refresh').evaluate('(el) => el === document.activeElement')
                page.keyboard.press('End')
                assert page.locator('#feed-context-delete').evaluate('(el) => el === document.activeElement')
                page.keyboard.press('Escape')
                assert more.evaluate('(el) => el === document.activeElement')
                assert more.get_attribute('aria-expanded') == 'false'
                more.click()
                page.locator('#feed-context-toggle').click()
                page.locator('[data-status-label]').get_by_text('Disabled', exact=True).wait_for()
                assert page.locator('[data-feed-availability]').is_visible()
                more.click()
                assert not page.locator('#feed-context-refresh').is_visible()
                page.locator('#feed-context-toggle').click()
                page.locator('[data-status-label]').get_by_text('Not refreshed yet', exact=True).wait_for()
                page.goto(origin + f'/?q=thoughtful&feed={profile.id}&sort=oldest')
                page.get_by_role('link', name='Next', exact=True).click()
                assert 'page=2' in page.url and 'sort=oldest' in page.url and 'q=thoughtful' in page.url
                assert page.locator('input[name=feed]').is_checked()
                page.goto(origin + '/settings')
                page.locator('[data-appearance]').select_option('system')
                page.emulate_media(color_scheme='dark')
                assert page.locator('body').evaluate('(el) => getComputedStyle(el).backgroundColor') == 'rgb(17, 17, 19)'
                page.emulate_media(color_scheme='light')
                assert page.locator('body').evaluate('(el) => getComputedStyle(el).backgroundColor') == 'rgb(245, 245, 247)'
                page.locator('[data-appearance]').select_option('dark')
                page.reload()
                assert page.locator('[data-appearance]').input_value() == 'dark'
                denied = browser.new_context()
                denied.add_init_script("Object.defineProperty(window, 'localStorage', {get() {throw new Error('Storage denied')}})")
                denied_page = denied.new_page()
                denied_page.on('pageerror', lambda error: errors.append(str(error)))
                denied_page.goto(origin + '/settings?preview_debug=1')
                denied_page.locator('[data-appearance]').select_option('light')
                assert denied_page.locator('html').get_attribute('data-theme') == 'light'
                denied.close()
                assert not errors, errors
                (output / 'report.json').write_text(json.dumps({'checks': checks, 'runtimeErrors': errors,
                    'interactions': ['keyboard More actions and Escape focus restoration', 'disable/enable through real endpoints',
                                     'query-preserving pagination', 'appearance persistence', 'live system appearance changes', 'storage denial'],
                    'limitations': ['No screen-reader or touch-device audit', 'Remaining screen-specific redesign packages pending']}, indent=2))
                browser.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)
    print(f'PASS: {len(checks)} real-app responsive/theme checks and interaction checks. Artifacts: {output}')


if __name__ == '__main__':
    run()
