"""Browser checks for feedback and remaining management screens; no live source access."""
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from contextlib import closing, ExitStack
from unittest.mock import patch
from io import BytesIO
import json
import sys
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from PIL import Image
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server
from rss_site_bridge.app import create_app, create_profile, FeedRequest, connect_db
from ui_auth_support import create_ui_app, authenticate_page
from rss_site_bridge import downloaders as d
from test_downloaders import FakeDownloader, metadata_file

class BrowserFixture:
    id = 'fixture'
    expired = False
    return_to = '/'
    def execute(self, action, **kwargs):
        if self.expired: raise RuntimeError('Session expired')
        if action == 'screenshot': return self.frame
        if action in ['download','download_copy']: return {'name':'Example.torrent', 'data':metadata_file()}
        return {'url':'https://example.com/story', 'title':'Fixture page', 'viewport_width':1280,
                'viewport_height':800, 'viewport_mode':kwargs.get('mode','desktop'),
                'downloads':[{'id':'file', 'name':'Example.torrent', 'status':'ready', 'size':100}]}
    def stop(self): pass


def run():
    out = ROOT / '.test-preview/refreshed-ui'
    out.mkdir(parents=True, exist_ok=True)
    report, errors = [], []
    with TemporaryDirectory() as temp, ExitStack() as stack:
        db = Path(temp) / 'fixture.db'
        fixture = BrowserFixture()
        stack.enter_context(patch('rss_site_bridge.app.get_safe_browser_session', return_value=fixture))
        app = create_ui_app({'DATABASE_PATH':db,'START_SCHEDULER':False,'TESTING':True})
        profile = create_profile(db, FeedRequest('Sample feed','https://example.com/topics','article','a','a','',25,60,'http'))
        with closing(connect_db(db)) as conn:
            conn.execute("UPDATE app_settings SET timezone_name='Asia/Tokyo'")
            conn.execute('INSERT INTO feed_items(profile_id,title,link,summary,discovered_at) VALUES(?,?,?,?,?)',
                         (profile.id,'Sample story','https://example.com/story','A brief summary.','2026-10-01T12:00:00+00:00'))
            conn.execute("INSERT INTO notifications(profile_id,event_type,severity,category,title,message,source_url,created_at) VALUES(?,?,?,?,?,?,?,?)", (profile.id,'refresh','info','success','Refresh complete','Collected one item.','https://example.com/topics','2026-10-01T12:00:00+00:00'))
            conn.commit()
        image = Image.new('RGB',(1280,800),(240,242,244)); buf = BytesIO(); image.save(buf,'JPEG'); fixture.frame = buf.getvalue()
        stack.enter_context(patch('rss_site_bridge.app.create_safe_browser_session', return_value=fixture))
        stack.enter_context(patch.dict(d.ADAPTERS, qbittorrent=FakeDownloader))
        FakeDownloader.present=False; FakeDownloader.failure=None; FakeDownloader.added=[]
        server = make_server('127.0.0.1',0,app,threaded=True)
        Thread(target=server.serve_forever,daemon=True).start()
        origin=f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(channel='chrome',headless=True)
                for timezone, expected in [('America/Chicago','07:00 AM'),('Asia/Tokyo','09:00 PM')]:
                    ctx=browser.new_context(timezone_id=timezone,locale='en-US',viewport={'width':1440,'height':1000})
                    page=ctx.new_page(); authenticate_page(page, app, origin); page.on('pageerror',lambda e: errors.append(str(e)))
                    page.goto(origin+'/')
                    expect(page.locator('time').first).to_contain_text(expected)
                    assert not page.get_by_role('link',name='Create feed',exact=True).count()
                    assert not page.get_by_role('button',name='Apply filters').count()
                    row=page.locator('.item-card').first; before=row.bounding_box(); row.hover(); page.wait_for_timeout(200)
                    assert row.bounding_box()==before
                    assert page.locator('.item-card h3 .external-icon').count()==1
                    assert page.locator('.safe-link .privacy-icon').count()==1
                    assert page.locator('.safe-link').evaluate("el => getComputedStyle(el).backgroundColor === 'rgba(0, 0, 0, 0)' && getComputedStyle(el).borderWidth === '0px'")
                    assert page.locator('a').evaluate_all("links => links.every(link => !getComputedStyle(link).textDecorationLine.includes('underline'))")
                    page.locator('.safe-link').hover()
                    assert page.locator('.safe-link').evaluate("el => !getComputedStyle(el).textDecorationLine.includes('underline')")
                    # Observe real submit events without navigation to distinguish immediate changes from debounce.
                    page.evaluate("""() => {
                        window.searchSubmissions = [];
                        window.observeSearchSubmit = event => { event.preventDefault(); window.searchSubmissions.push(performance.now()); };
                        document.querySelector('[data-auto-search]').addEventListener('submit', window.observeSearchSubmit, true);
                    }""")
                    page.locator('#search').fill('Sample')
                    page.wait_for_timeout(150)
                    assert page.evaluate('searchSubmissions.length') == 0
                    page.wait_for_function('searchSubmissions.length === 1')
                    page.locator('#search').fill('Sample story')
                    page.locator('.reading-filters summary').click()
                    page.locator('#sort').select_option('title')
                    page.wait_for_function('searchSubmissions.length === 2')
                    page.wait_for_timeout(500)
                    assert page.evaluate('searchSubmissions.length') == 2
                    page.locator('input[name=feed]').check()
                    assert page.evaluate('searchSubmissions.length') == 3
                    page.wait_for_timeout(500)
                    assert page.evaluate('searchSubmissions.length') == 3
                    page.goto(origin+'/')
                    timeline_bounds=page.locator('.topbar').bounding_box()
                    page.locator('#search').fill('Sample'); page.wait_for_function("new URLSearchParams(location.search).get('q') === 'Sample'")
                    expect(page.locator('#search')).to_be_focused()
                    page.locator('.reading-filters summary').click()
                    page.locator('#sort').select_option('title'); page.wait_for_url('**/*sort=title*')
                    page.goto(origin+'/feeds'); feed_bounds=page.locator('.topbar').bounding_box()
                    assert feed_bounds['x']==timeline_bounds['x'] and feed_bounds['width']==timeline_bounds['width']
                    expect(page.locator('[data-feed-more]')).to_have_text('⋮')
                    page.goto(origin+f'/profiles/{profile.id}?view=rss'); assert not page.locator('.xml-output').count()
                    assert page.get_by_role('link',name='Open raw XML').get_attribute('target')=='_blank'
                    report.append({'timezone':timezone,'displayed':expected,'hover':'stable','search':'automatic','rss':'links only'})
                    ctx.close()
                ctx=browser.new_context(locale='en-US',viewport={'width':1440,'height':1000}); page=ctx.new_page(); authenticate_page(page, app, origin)
                cdp=ctx.new_cdp_session(page); cdp.send('Emulation.setTimezoneOverride', {'timezoneId':'America/Chicago'})
                page.goto(origin+'/'); expect(page.locator('time').first).to_contain_text('07:00 AM')
                cdp.send('Emulation.setTimezoneOverride', {'timezoneId':''})
                cdp.send('Emulation.setTimezoneOverride', {'timezoneId':'Asia/Tokyo'})
                page.evaluate("window.dispatchEvent(new Event('focus'))")
                expect(page.locator('time').first).to_contain_text('09:00 PM')
                page.on('pageerror',lambda e: errors.append(str(e)))
                page.goto(origin+'/settings')
                page.locator('#timezone_name').fill('America/Chicago')
                page.get_by_role('link',name='Manage downloaders').click()
                expect(page.locator('[data-management-discard]')).to_be_visible()
                page.locator('[data-management-stay]').click()
                expect(page.locator('#timezone_name')).to_have_value('America/Chicago')
                page.get_by_role('link',name='Manage downloaders').click()
                page.locator('[data-management-leave]').click()
                page.wait_for_url('**/settings/downloaders')
                page.locator('#downloader-name').fill('Home'); page.locator('#downloader-url').fill('http://localhost:8080')
                page.locator('#downloader-auth').select_option('none'); assert not page.locator('#downloader-secret').is_visible()
                page.locator('#downloader-auth').select_option('api_key'); assert not page.locator('#downloader-user').is_visible()
                page.locator('#downloader-secret').fill('fixture-secret'); page.locator('#downloader-extensions').fill('.torrent')
                page.get_by_role('button',name='Save downloader').click()
                page.wait_for_load_state('load')
                page.get_by_role('button',name='Test / refresh categories').click()
                expect(page.locator('[data-test-result]')).to_contain_text('Connected:')
                page.locator('.downloader-editor > summary').click()
                page.locator('#downloader-name').fill('Unsaved destination')
                page.get_by_role('button',name='Delete',exact=True).click()
                expect(page.locator('[data-management-discard]')).to_be_visible()
                page.once('dialog',lambda dialog:dialog.dismiss())
                page.locator('[data-management-leave]').click()
                page.get_by_role('link',name='Back to settings').click()
                expect(page.locator('[data-management-discard]')).to_be_visible()
                page.locator('[data-management-stay]').click()
                expect(page.locator('#downloader-name')).to_have_value('Unsaved destination')
                page.get_by_role('link',name='Back to settings').click()
                page.locator('[data-management-leave]').click()
                page.wait_for_url('**/settings')
                for theme in ['light','dark']:
                    page.goto(origin+'/settings'); page.locator('[data-appearance]').select_option(theme)
                    for width in [390,1440]:
                        page.set_viewport_size({'width':width,'height':1000})
                        for screen, route in [('settings','/settings'),('notifications','/notifications'),('downloaders','/settings/downloaders')]:
                            page.goto(origin+route)
                            assert not page.evaluate('document.documentElement.scrollWidth>innerWidth+1')
                            page.screenshot(path=str(out/f'{screen}-{width}-{theme}.png'))
                        fixture.expired=False
                        page.goto(origin+f'/profiles/{profile.id}/items/1/safe')
                        expect(page.get_by_role('button',name='Downloads, 1 ready of 1')).to_be_visible()
                        for small_width in ([320, 390] if width == 390 else [1440]):
                            page.set_viewport_size({'width':small_width,'height':1000})
                            assert not page.evaluate('document.documentElement.scrollWidth>innerWidth+1')
                            if small_width < 768:
                                nav_box=page.locator('.safe-browser-nav').bounding_box(); actions_box=page.locator('.safe-browser-toolbar-actions').bounding_box()
                                assert abs(nav_box['y']-actions_box['y']) < 2
                            page.set_viewport_size({'width':width,'height':1000})
                        if not page.get_by_role('link',name='Save file').is_visible(): page.locator('[data-browser-download-toggle]').click()
                        assert not page.evaluate('document.documentElement.scrollWidth>innerWidth+1')
                        page.screenshot(path=str(out/f'isolated-tray-{width}-{theme}.png'))
                        page.get_by_role('button',name='Send to Home',exact=True).click()
                        expect(page.locator('[data-send-category]')).to_be_enabled()
                        page.locator('[data-send-category]').select_option('Movies')
                        page.screenshot(path=str(out/f'send-review-{width}-{theme}.png'))
                        page.keyboard.press('Escape'); assert not page.locator('[data-downloader-dialog]').is_visible()
                        page.once('dialog',lambda dialog:dialog.dismiss())
                        page.get_by_role('button',name='Close session').click(); assert '/safe' in page.url
                        fixture.expired=True
                        expect(page.locator('[data-session-ended]')).to_be_visible(timeout=10000)
                        expect(page.get_by_role('link',name='Start new session')).to_be_visible()
                        report.append({'theme':theme,'width':width,'screens':'management/tray/review','close':'cancelled','expiry':'return/restart available'})
                fixture.expired=False
                page.goto(origin+f'/profiles/{profile.id}/items/1/safe')
                expect(page.get_by_role('button',name='Downloads, 1 ready of 1')).to_be_visible()
                page.get_by_role('button',name='Send to Home',exact=True).click()
                expect(page.locator('[data-send-category]')).to_be_enabled()
                page.locator('[data-send-category]').select_option('Movies')
                FakeDownloader.failure=ValueError('Connection timed out.')
                page.locator('[data-send-submit]').click()
                expect(page.locator('[data-downloader-dialog]')).not_to_be_visible()
                if not page.locator('[data-browser-download-popover]').is_visible(): page.locator('[data-browser-download-toggle]').click()
                expect(page.get_by_role('button',name='Check status',exact=True)).to_be_visible(timeout=10000)
                assert page.get_by_role('button',name='Send to Home',exact=True).is_disabled()
                FakeDownloader.failure=None; FakeDownloader.present=False
                page.get_by_role('button',name='Check status',exact=True).click()
                expect(page.get_by_role('button',name='Send to Home',exact=True)).to_be_enabled(timeout=10000)
                page.get_by_role('button',name='Send to Home',exact=True).click()
                expect(page.locator('[data-send-caution]')).to_be_visible()
                expect(page.locator('[data-send-category]')).to_be_enabled()
                page.locator('[data-send-category]').select_option('Movies'); page.locator('[data-send-submit]').click()
                expect(page.locator('[data-download-submission]')).to_contain_text('Added to Home',timeout=10000)
                assert len(FakeDownloader.added)==2
                page.goto(origin+'/settings/downloaders')
                history=page.locator('[data-submission]').first
                expect(history.locator('.submission-meta')).to_contain_text('ProfileHome')
                expect(history.locator('.submission-meta')).to_contain_text('CategoryMovies')
                expect(history.locator('[data-submission-state]')).to_have_text('Accepted')
                assert not history.locator('[data-submission-message]').is_visible()

                report.append({'submission':'unknown/check/explicit retry/accepted','automatic_resend':False})
                assert not errors, errors
                browser.close()
        finally: server.shutdown()
    (out/'feedback-report.json').write_text(json.dumps(report,indent=2))
    print(f'{len(report)} feedback and management flow groups passed')

if __name__=='__main__': run()
