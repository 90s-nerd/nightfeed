"""Exercise real editor routes/SSE/save flow with fixture extraction, no source access."""
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import patch
import json
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server
from rss_site_bridge.app import create_app, create_profile, connect_db, FeedRequest, FeedEntry, get_profile_by_id, list_profiles


def fixture_extract(config, progress=None):
    if progress:
        progress('Matching selectors', 'Checking the fixture listing.')
    if config.item_selector == '.error':
        raise RuntimeError('Fixture network failure. Try again.')
    if config.item_selector == '.zero':
        return []
    return [FeedEntry('A previewed story', 'https://example.com/story', 'Fixture summary', datetime.now(timezone.utc))]


def run():
    output = ROOT / '.test-preview' / 'refreshed-ui'
    output.mkdir(parents=True, exist_ok=True)
    checks, errors = [], []
    with TemporaryDirectory() as temp:
        db = Path(temp) / 'ui.db'
        app = create_app({'TESTING': True, 'START_SCHEDULER': False, 'DATABASE_PATH': db})
        original = create_profile(db, FeedRequest('Original feed', 'https://example.com/topics', 'article', 'a', 'a', '', 25, 0, 'http'))
        with closing(connect_db(db)) as conn:
            conn.execute('UPDATE app_settings SET smtp_enabled=1')
            conn.execute('INSERT INTO feed_items (profile_id,title,link,summary,discovered_at) VALUES (?,?,?,?,?)',
                         (original.id, 'Stored story', 'https://example.com/stored', '', '2026-10-01T12:00:00+00:00'))
            conn.commit()
        server = make_server('127.0.0.1', 0, app)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = f'http://127.0.0.1:{server.server_port}'
        try:
            with patch('rss_site_bridge.app.extract_feed_entries', side_effect=fixture_extract), sync_playwright() as pw:
                browser = pw.chromium.launch(channel='chrome', headless=True)
                for theme in ['light', 'dark']:
                    for width in [390, 1440]:
                        page = browser.new_page(viewport={'width': width, 'height': 1000})
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        page.goto(origin + '/settings')
                        page.locator('[data-appearance]').select_option(theme)
                        page.goto(origin + '/compose')
                        assert page.locator('.notification-failures').evaluate('el => Boolean(el.closest("[data-feed-editor]"))')
                        assert page.locator('[data-editor-save]').evaluate('el => Boolean(el.closest("[data-feed-editor]"))')
                        assert page.locator('.notification-preferences > .ui-hint').count() == 0
                        page.locator('[data-editor-next]').click()
                        assert page.locator('[data-editor-errors]').is_visible()
                        assert page.locator('[data-editor-errors]').evaluate('(el) => el === document.activeElement')
                        page.locator('[name=feed_title]').fill(f'Guided {theme} {width}')
                        page.locator('[name=source_url]').fill('https://example.com/topics')
                        page.locator('[data-editor-next]').click()
                        assert page.locator('[data-editor-step="1"]').is_visible()
                        for name, value in [('item_selector', 'article'), ('title_selector', 'a'), ('link_selector', 'a')]:
                            page.locator(f'[name={name}]').fill(value)
                        page.locator('[data-editor-preview]').click()
                        page.locator('[data-editor-preview-status]').get_by_text('1 matching items. Preview is current.', exact=True).wait_for()
                        assert page.locator('[data-editor-preview-list]').get_by_text('A previewed story').is_visible()
                        page.screenshot(path=str(output / f'extraction-{width}-{theme}.png'))
                        page.locator('[name=item_selector]').fill('.zero')
                        assert 'Settings changed' in page.locator('[data-editor-preview-status]').inner_text()
                        page.locator('[data-editor-preview]').click()
                        page.locator('[data-editor-preview-empty]').wait_for(state='visible')
                        page.locator('[name=item_selector]').fill('.error')
                        page.locator('[data-editor-preview]').click()
                        page.locator('[data-editor-preview-error]').get_by_text('Fixture network failure. Try again.', exact=True).wait_for()
                        page.locator('[data-editor-next]').click()
                        page.locator('[data-schedule-mode]').select_option('calendar')
                        page.locator('[name=cron_expression]').fill('0 9 * * mon-fri')
                        page.locator('[data-schedule-mode]').select_option('interval')
                        assert page.locator('[name=cron_expression]').input_value() == ''
                        page.locator('[data-schedule-mode]').select_option('calendar')
                        assert page.locator('[name=cron_expression]').input_value() == '0 9 * * mon-fri'
                        page.locator('[data-schedule-mode]').select_option('manual')
                        assert page.locator('[name=refresh_interval_minutes]').input_value() == '0'
                        page.locator('[data-editor-back]').click()
                        assert page.locator('[name=item_selector]').input_value() == '.error'
                        page.locator('[data-editor-back]').click()
                        assert page.locator('[name=feed_title]').input_value() == f'Guided {theme} {width}'
                        page.locator('[data-editor-next]').click()
                        page.locator('[data-editor-next]').click()
                        page.screenshot(path=str(output / f'schedule-{width}-{theme}.png'))
                        count = len(list_profiles(db))
                        page.locator('[data-editor-save]').click()
                        assert page.locator('[data-editor-save-dialog]').is_visible()
                        assert len(list_profiles(db)) == count
                        page.keyboard.press('Escape')
                        assert page.locator('[data-editor-save]').evaluate('(el) => el === document.activeElement')
                        page.locator('[data-editor-save]').click()
                        page.locator('[data-dialog-save]').click()
                        page.get_by_text('Feed created. Refresh now to collect items.', exact=True).wait_for()
                        assert page.get_by_text('Feed created. Refresh now to collect items.', exact=True).is_visible()
                        new = max(list_profiles(db), key=lambda profile: profile.id)
                        assert new.last_status == 'idle' and new.item_count == 0 and new.refresh_interval_minutes == 0
                        # Edit cancellation, validation, and save preserve the existing record/history.
                        page.goto(origin + f'/profiles/{original.id}?view=configuration')
                        name = page.locator('[name=feed_title]')
                        saved_name = name.input_value()
                        name.fill('Unsaved edit')
                        page.get_by_text('Manage feed', exact=True).click()
                        page.get_by_role('button', name='Delete feed', exact=True).click()
                        assert page.locator('[data-editor-discard-dialog]').is_visible()
                        page.once('dialog', lambda dialog: dialog.dismiss())
                        page.locator('[data-dialog-discard]').click()
                        assert get_profile_by_id(db, original.id) is not None
                        page.get_by_role('link', name='Items', exact=True).click()
                        page.locator('[data-editor-discard-dialog] [data-dialog-stay]').click()
                        assert name.input_value() == 'Unsaved edit'
                        assert get_profile_by_id(db, original.id).feed_title == saved_name
                        page.get_by_role('link', name='Items', exact=True).click()
                        page.locator('[data-dialog-discard]').click()
                        page.wait_for_url('**?view=items')
                        assert get_profile_by_id(db, original.id).feed_title == saved_name
                        page.get_by_role('link', name='Configuration', exact=True).click()
                        page.locator('[name=feed_title]').fill(f'Edited {theme} {width}')
                        page.locator('[data-schedule-mode]').select_option('calendar')
                        page.locator('[name=cron_expression]').fill('invalid calendar')
                        page.locator('[data-editor-save]').click()
                        with page.expect_response(lambda response: response.url.endswith(f'/profiles/{original.id}/edit') and response.status == 400):
                            page.locator('[data-dialog-save]').click()
                        page.locator('[data-editor-errors]').wait_for(state='visible')
                        assert page.locator('[name=cron_expression]').input_value() == 'invalid calendar'
                        assert page.locator('[name=feed_title]').input_value() == f'Edited {theme} {width}'
                        assert get_profile_by_id(db, original.id).feed_title == saved_name
                        page.get_by_role('link', name='Items', exact=True).click()
                        page.locator('[data-editor-discard-dialog] [data-dialog-stay]').click()
                        page.locator('[data-schedule-mode]').select_option('manual')
                        if not page.locator('[name=max_items]').is_visible():
                            page.get_by_text('Item limits and priority', exact=True).click()
                        page.locator('[name=max_items]').fill('101')
                        page.locator('[data-editor-save]').click()
                        assert page.locator('[data-editor-errors]').is_visible()
                        assert page.locator('[name=max_items]').get_attribute('aria-invalid') == 'true'
                        page.locator('[name=max_items]').fill('25')
                        page.locator('[data-editor-save]').click()
                        page.locator('[data-dialog-save]').click()
                        page.get_by_text('Configuration saved.', exact=True).wait_for()
                        current = get_profile_by_id(db, original.id)
                        assert current.feed_token == original.feed_token and current.item_count == 1
                        page.get_by_role('link', name='RSS', exact=True).click()
                        assert current.feed_token in page.locator('#permanent-feed-url').input_value()
                        page.evaluate("Object.defineProperty(navigator, 'clipboard', {value: {writeText: () => Promise.reject(new Error('denied'))}, configurable: true})")
                        page.get_by_role('button', name='Copy feed URL').click()
                        page.locator('[data-copy-status]').get_by_text('Copy unavailable.', exact=False).wait_for()
                        assert page.locator('#permanent-feed-url').evaluate('(el) => el.selectionEnd === el.value.length')
                        # Refresh failure is inline; a successful refresh updates stored rows in place.
                        page.get_by_role('link', name='Items', exact=True).click()
                        page.wait_for_load_state('load')
                        with patch('rss_site_bridge.app.extract_feed_entries', side_effect=RuntimeError('Fixture refresh failed')):
                            page.get_by_role('button', name='Refresh now').click()
                            try:
                                page.locator('[data-refresh-result]').get_by_text('Fixture refresh failed', exact=False).wait_for()
                            except Exception:
                                print('Refresh diagnostic:', page.url, errors, page.locator('body').inner_text()[-1800:])
                                raise
                            assert page.get_by_text('Stored story', exact=True).is_visible()
                        page.get_by_role('button', name='Refresh now').click()
                        page.locator('[data-items-view]').get_by_text('A previewed story', exact=True).wait_for()
                        assert page.locator('[data-unread-notifications]').is_visible()
                        assert int(page.locator('[data-unread-notifications]').inner_text()) > 0
                        assert page.url.endswith('view=items')
                        # Refresh inserts an item, so remove only the fixture insertion between runs.
                        with closing(connect_db(db)) as conn:
                            conn.execute("DELETE FROM feed_items WHERE profile_id=? AND link='https://example.com/story'", (original.id,))
                            conn.commit()
                        checks.append({'theme': theme, 'width': width, 'flows': 'validation, preview success/zero/error/stale, draft steps, schedule modes, save acknowledgment, edit discard/save, copy fallback, inline refresh'})
                        page.close()
                # A current successful preview saves directly without a confirmation.
                page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(origin + '/compose')
                page.locator('[name=feed_title]').fill('Previewed calendar feed')
                page.locator('[name=source_url]').fill('https://example.com/topics')
                page.locator('[name=source_url]').press('Enter')
                for name, value in [('item_selector', 'article'), ('title_selector', 'a'), ('link_selector', 'a')]:
                    page.locator(f'[name={name}]').fill(value)
                page.locator('[data-editor-preview]').click()
                page.locator('[data-editor-preview-status]').get_by_text('1 matching items. Preview is current.', exact=True).wait_for()
                page.locator('[data-editor-next]').click()
                page.locator('[data-schedule-mode]').select_option('calendar')
                page.locator('[name=cron_expression]').fill('0 9 * * mon-fri')
                assert page.locator('[name=schedule_timezone]').count() == 0
                page.locator('[data-editor-save]').click()
                page.get_by_text('Feed created. Refresh now to collect items.', exact=True).wait_for()
                new = max(list_profiles(db), key=lambda profile: profile.id)
                assert new.cron_expression == '0 9 * * mon-fri' and new.schedule_timezone == 'UTC'
                assert new.last_status == 'idle' and new.item_count == 0
                page.close()
                assert not errors, errors
                browser.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)
    (output / 'editor-report.json').write_text(json.dumps({'checks': checks, 'runtimeErrors': errors,
        'fixture': 'Extraction is stubbed; real Flask validation, SSE transport, SQLite persistence and browser UI are exercised.',
        'limitations': ['No source site requests', 'No screen-reader or physical touch-device audit']}, indent=2))
    print(f'PASS: guided editor and detail flows in {len(checks)} desktop/mobile theme combinations.')


if __name__ == '__main__':
    run()
