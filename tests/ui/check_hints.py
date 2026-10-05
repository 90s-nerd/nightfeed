"""Check optional guidance stays hidden and works with keyboard and touch."""
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from contextlib import closing
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server
from rss_site_bridge.app import create_app, create_profile, FeedRequest, connect_db

with TemporaryDirectory() as temp:
    db = Path(temp) / 'hints.db'
    app = create_app({'TESTING': True, 'START_SCHEDULER': False, 'DATABASE_PATH': db})
    feed = create_profile(db, FeedRequest('Example feed', 'https://example.com', 'article', 'a', 'a', '', 25, 60, 'http'))
    with closing(connect_db(db)) as conn:
        conn.execute('UPDATE app_settings SET smtp_enabled=1')
        conn.commit()
    server = make_server('127.0.0.1', 0, app)
    Thread(target=server.serve_forever, daemon=True).start()
    origin = f'http://127.0.0.1:{server.server_port}'
    errors = []
    checks = 0
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='chrome', headless=True)
            page = browser.new_page()
            page.on('pageerror', lambda e: errors.append(str(e)))
            for theme in ['light', 'dark']:
                page.goto(origin + '/settings')
                page.locator('[data-appearance]').select_option(theme)
                for width in [320, 390, 1440]:
                    page.set_viewport_size({'width': width, 'height': 1000})
                    for route in ['/settings', '/settings/downloaders', f'/profiles/{feed.id}?view=configuration']:
                        page.goto(origin + route)
                        assert page.locator('.ui-hint[open]').count() == 0
                        for content in page.locator('.ui-hint-content').all():
                            expect(content).to_be_hidden()
                        # Open actual configuration groups, never help disclosures.
                        page.locator('.editor-disclosure, .feed-settings-advanced').evaluate_all('(els) => els.forEach(el => el.open = true)')
                        for summary in page.locator('.ui-hint > summary').all():
                            if not summary.is_visible():
                                continue
                            assert summary.bounding_box()['width'] <= 20
                            assert summary.evaluate("el => getComputedStyle(el).backgroundColor === 'rgba(0, 0, 0, 0)'")
                            adjacency = summary.evaluate('''el => {
                                const row = el.closest('.field-label, .help-heading');
                                const label = row?.querySelector('label, h1, h2');
                                if (!label) return true;
                                return el.getBoundingClientRect().left - label.getBoundingClientRect().right <= 8;
                            }''')
                            assert adjacency, (route, width, summary.get_attribute('aria-label'))
                            baseline_delta = summary.evaluate('''el => {
                                const row = el.closest('.field-label, .help-heading, .notification-choice, .settings-tls-row');
                                const label = row?.querySelector('strong, label span, label, h1, h2');
                                if (!label) return 0;
                                const baseline = node => {
                                    const marker = document.createElement('span');
                                    marker.style.cssText = 'display:inline-block;width:0;height:0;padding:0;margin:0;vertical-align:baseline';
                                    node.prepend(marker);
                                    const y = marker.getBoundingClientRect().top;
                                    marker.remove(); return y;
                                };
                                return Math.abs(baseline(label) - baseline(el));
                            }''')
                            assert baseline_delta < 1.5, (route, width, summary.get_attribute('aria-label'), baseline_delta)
                            summary.focus()
                            page.keyboard.press('Enter')
                            hint = summary.locator('..')
                            expect(hint).to_have_attribute('open', '')
                            content = hint.locator('.ui-hint-content')
                            expect(content).to_be_visible()
                            # Native toggle events run after the opening action; allow positioning to settle.
                            page.wait_for_function("() => { const el = document.querySelector('.ui-hint[open] .ui-hint-content'); if (!el) return false; const b = el.getBoundingClientRect(); return b.left >= 0 && b.right <= innerWidth + 1; }", timeout=2000)
                            box = content.bounding_box()
                            assert box['x'] >= 0 and box['x'] + box['width'] <= width + 1, (route, width, box)
                            page.keyboard.press('Escape')
                            expect(content).to_be_hidden()
                            expect(summary).to_be_focused()
                            summary.click()
                            page.locator('h1:not(.sr-only), .surface h2').first.click()
                            expect(content).to_be_hidden()
                            checks += 1
                        assert not page.evaluate('document.documentElement.scrollWidth > innerWidth + 1')
                        if width in [390, 1440] and route == '/settings/downloaders':
                            page.screenshot(path=f'.test-preview/refreshed-ui/clean-help-{width}-{theme}.png', full_page=True)
                        if width in [390, 1440] and 'configuration' in route:
                            page.locator('[data-editor-step="1"]').screenshot(path=f'.test-preview/refreshed-ui/subtle-extraction-help-{width}-{theme}.png')
                            panel = page.locator('.notification-preferences')
                            assert panel.locator('.notification-failures').count() == 1
                            assert panel.locator('.notification-choice > .ui-hint').count() == panel.locator('.notification-choice').count()
                            panel.screenshot(path=f'.test-preview/refreshed-ui/repaired-email-options-{width}-{theme}.png')
                        if route == '/settings':
                            tls = page.locator('.settings-tls-row')
                            label = tls.locator('label span').bounding_box()
                            mark = tls.locator('summary').bounding_box()
                            assert abs((label['y'] + label['height']/2) - (mark['y'] + mark['height']/2)) < 5
                            if width in [390, 1440]:
                                tls.screenshot(path=f'.test-preview/refreshed-ui/repaired-starttls-{width}-{theme}.png')
            # Native disclosures work without JavaScript too.
            context = browser.new_context(java_script_enabled=False)
            plain = context.new_page()
            plain.goto(origin + '/settings')
            expect(plain.locator('#appearance-help')).to_be_hidden()
            plain.locator('.ui-hint > summary').first.click()
            expect(plain.locator('#appearance-help')).to_be_visible()
            context.close()
            assert not errors, errors
            browser.close()
        print(f'PASS: {checks} help interactions across mobile/desktop and both themes; no-JavaScript disclosure verified.')
    finally:
        server.shutdown()
