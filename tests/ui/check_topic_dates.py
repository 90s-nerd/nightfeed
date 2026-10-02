"""Browser-local calendar labels and compact topic rows, using only fixtures."""
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from contextlib import closing
from datetime import datetime, timezone
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server
from rss_site_bridge.app import create_app, create_profile, FeedRequest, connect_db, create_notification

with TemporaryDirectory() as temp:
    db=Path(temp)/'dates.db'
    app=create_app({'TESTING':True,'START_SCHEDULER':False,'DATABASE_PATH':db})
    feed=create_profile(db,FeedRequest('Example feed','https://example.com','article','a','a','',25,60,'http'))
    title='A long topic title with enough words to test truncation ' * 6
    with closing(connect_db(db)) as conn:
        conn.execute('INSERT INTO feed_items(profile_id,title,link,summary,discovered_at) VALUES(?,?,?,?,?)',(feed.id,title,'https://example.com/topic','','2026-10-01T18:56:00+00:00'))
        conn.commit()
    create_notification(db,profile_id=feed.id,event_type='refresh',severity='info',category='success',title='Refresh complete',message='1 new entry.',source_url='https://example.com')
    server=make_server('127.0.0.1',0,app)
    Thread(target=server.serve_forever,daemon=True).start()
    origin=f'http://127.0.0.1:{server.server_port}'
    errors=[]
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='chrome',headless=True)
            for zone,locale,expected in [('America/Chicago','en-US','Today 01:56 PM'),('Asia/Tokyo','en-US','Tomorrow 03:56 AM'),('America/Chicago','en-GB','Today 13:56')]:
                ctx=browser.new_context(timezone_id=zone,locale=locale)
                page=ctx.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
                page.clock.set_fixed_time(datetime(2026,10,1,12,0,tzinfo=timezone.utc))
                page.goto(origin+'/')
                expect(page.locator('.topic-time')).to_have_text(expected)
                page.clock.set_fixed_time(datetime(2026,10,2,18,0,tzinfo=timezone.utc))
                page.evaluate("window.dispatchEvent(new Event('focus'))")
                expect(page.locator('.topic-time')).to_contain_text('Yesterday')
                page.clock.set_fixed_time(datetime(2026,10,3,18,0,tzinfo=timezone.utc))
                page.evaluate("window.dispatchEvent(new Event('focus'))")
                expect(page.locator('.topic-time')).to_contain_text('Oct')
                ctx.close()
            page=browser.new_page(locale='en-US',timezone_id='America/Chicago')
            page.clock.set_fixed_time(datetime(2026,10,1,20,0,tzinfo=timezone.utc))
            for theme in ['light','dark']:
                page.goto(origin+'/settings');page.locator('[data-appearance]').select_option(theme)
                for width in [320,390,1440]:
                    page.set_viewport_size({'width':width,'height':1000})
                    for route in ['/',f'/profiles/{feed.id}']:
                        page.goto(origin+route)
                        heading=page.locator('.topic-heading')
                        text=heading.locator('.topic-title');stamp=heading.locator('.topic-time')
                        assert text.evaluate("el => el.scrollWidth > el.clientWidth && getComputedStyle(el).textOverflow === 'ellipsis'")
                        assert abs(text.bounding_box()['y']-stamp.bounding_box()['y']) < 10
                        assert heading.locator('.topic-link').get_attribute('title')==title
                        assert 'Discovered' not in heading.inner_text()
                        assert not page.evaluate('document.documentElement.scrollWidth > innerWidth + 1')
                        if route=='/' and width in [390,1440]:
                            page.screenshot(path=f'.test-preview/refreshed-ui/topic-dates-{width}-{theme}.png')
                    for route in ['/','/feeds','/notifications']:
                        page.goto(origin+route)
                        link=page.locator('.feed-name-link').first
                        assert link.evaluate("el => getComputedStyle(el).color === getComputedStyle(document.documentElement).getPropertyValue('--nf-primary').trim() || getComputedStyle(el).color === (() => {const probe=document.createElement('span');probe.style.color='var(--nf-primary)';el.append(probe);const c=getComputedStyle(probe).color;probe.remove();return c;})()")
                        before=link.evaluate('el => getComputedStyle(el).color');link.hover()
                        assert link.evaluate('el => getComputedStyle(el).color')!=before
                        page.mouse.move(0,0)
            assert not errors,errors
            browser.close()
        print('PASS: browser-local Today/Yesterday/Tomorrow and older dates; 12 compact row layouts; orange feed links and hover colors across three screens.')
    finally:
        server.shutdown()
