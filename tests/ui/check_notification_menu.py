"""Compact notification rows and real read/delete actions in a temporary app."""
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server
from rss_site_bridge.app import create_app, create_notification

with TemporaryDirectory() as temp:
    db=Path(temp)/'notifications.db'
    app=create_app({'TESTING':True,'START_SCHEDULER':False,'DATABASE_PATH':db})
    for index in range(8):
        create_notification(db,profile_id=None,event_type='refresh',severity='info',category='success',title=f'Refresh complete {index}',message='No new entries. This feed is up to date.',source_url='https://example.com')
    server=make_server('127.0.0.1',0,app)
    Thread(target=server.serve_forever,daemon=True).start()
    origin=f'http://127.0.0.1:{server.server_port}'
    errors=[]
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='chrome',headless=True)
            page=browser.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
            for theme in ['light','dark']:
                page.goto(origin+'/settings');page.locator('[data-appearance]').select_option(theme)
                for width in [320,390,1440]:
                    page.set_viewport_size({'width':width,'height':1000})
                    page.goto(origin+'/notifications?status=all')
                    rows=page.locator('.notification-card')
                    first=rows.first;summary=first.locator('summary');menu=first.locator('.row-menu-content')
                    assert not first.get_by_role('button',name='Delete',exact=True).is_visible()
                    assert first.bounding_box()['height']<=125,(width,first.bounding_box())
                    summary.focus();page.keyboard.press('Enter')
                    expect(first.get_by_role('button',name='Mark read',exact=True)).to_be_focused()
                    page.keyboard.press('End')
                    expect(first.get_by_role('button',name='Delete',exact=True)).to_be_focused()
                    assert first.get_by_role('button',name='Delete',exact=True).evaluate("el => getComputedStyle(el).color !== getComputedStyle(el.closest('.row-menu-content').querySelector('button')).color")
                    page.keyboard.press('Escape');expect(menu).to_be_hidden();expect(summary).to_be_focused()
                    summary.click();page.locator('.page-title p').click();expect(menu).to_be_hidden()
                    summary.click();rows.nth(1).locator('summary').focus();page.keyboard.press('Enter');expect(menu).to_be_hidden()
                    page.locator('.page-title p').click()
                    assert not page.evaluate('document.documentElement.scrollWidth>innerWidth+1')
                    page.screenshot(path=f'.test-preview/refreshed-ui/compact-notifications-{width}-{theme}.png')
            page.goto(origin+'/notifications?status=all')
            first=page.locator('.notification-card').first
            title=first.locator('h3').inner_text()
            first.locator('summary').click()
            first.get_by_role('button',name='Mark read',exact=True).click()
            row=page.locator('.notification-card').filter(has=page.get_by_role('heading',name=title,exact=True))
            expect(row.locator('.notification-dot')).to_have_count(0)
            row.locator('summary').click()
            expect(row.get_by_role('button',name='Mark read',exact=True)).to_have_count(0)
            page.once('dialog',lambda dialog:dialog.dismiss())
            row.get_by_role('button',name='Delete',exact=True).click()
            expect(row).to_have_count(1)
            page.once('dialog',lambda dialog:dialog.accept())
            row.get_by_role('button',name='Delete',exact=True).click()
            expect(row).to_have_count(0)
            context=browser.new_context(java_script_enabled=False)
            plain=context.new_page();plain.goto(origin+'/notifications?status=all')
            plain.locator('.notification-card summary').first.click()
            expect(plain.locator('.notification-card').first.get_by_role('button',name='Delete',exact=True)).to_be_visible()
            context.close();assert not errors,errors;browser.close()
        print('PASS: compact rows at 320/390/1440px in both themes; keyboard/context menus, native fallback, read and confirmed/cancelled delete verified.')
    finally:
        server.shutdown()
