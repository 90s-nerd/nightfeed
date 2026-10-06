"""Full authentication workflow in an isolated real browser, including mobile layout."""
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import os, sys, time
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ['NIGHTFEED_SECURE_COOKIES']='0'
from rss_site_bridge.app import create_app, create_profile, FeedRequest
from rss_site_bridge.auth import setup_token, settings, connect
from contextlib import closing
import json
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright, expect
with TemporaryDirectory(dir='.test-preview') as temp:
 db=Path(temp)/'ui.db'
 app=create_app(dict(TESTING=True,DATABASE_PATH=db,START_SCHEDULER=False))
 create_profile(db,FeedRequest('Private UI fixture','https://example.com','article','a','a','',10,60,'http'))
 server=make_server('127.0.0.1',0,app,threaded=True)
 thread=Thread(target=server.serve_forever,daemon=True);thread.start()
 address=f'http://127.0.0.1:{server.server_port}'
 try:
  with sync_playwright() as p:
   browser=p.chromium.launch(channel='chrome')
   page=browser.new_page(viewport={'width':1280,'height':900})
   errors=[];page.on('pageerror',lambda e:errors.append(e.stack))
   page.goto(address)
   expect(page.get_by_role('heading',name='Welcome to Nightfeed')).to_be_visible()
   page.screenshot(path='.test-preview/auth-onboarding-desktop.png',full_page=True)
   page.set_viewport_size({'width':390,'height':844})
   assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
   page.screenshot(path='.test-preview/auth-onboarding-mobile.png',full_page=True)
   page.get_by_label('Setup token').fill(setup_token(db))
   page.get_by_label('Name',exact=True).fill('Owner fixture')
   page.get_by_label('Username',exact=True).fill('owner')
   page.get_by_label('Password',exact=True).fill('browser fixture passphrase')
   page.get_by_label('Confirm password',exact=True).fill('browser fixture passphrase')
   page.get_by_role('button',name='Create account').click()
   page.wait_for_url(address+'/')
   page.goto(address+'/settings/profile')
   page.get_by_label('Display name').fill('Updated owner')
   page.get_by_role('button',name='Save profile').click()
   expect(page.get_by_role('status')).to_have_text('Profile saved.')
   expect(page.locator('.account-identity strong')).to_have_text('Updated owner')
   page.screenshot(path='.test-preview/auth-profile-mobile.png',full_page=True)
   page.set_viewport_size({'width':1280,'height':900})
   page.screenshot(path='.test-preview/auth-profile-desktop.png',full_page=True)
   original_config=settings(db)
   with closing(connect(db)) as conn:
    conn.execute('UPDATE auth_config SET value=? WHERE id=1',(json.dumps(original_config | dict(oidc_enabled=True,subject='fixture-subject')),))
    conn.execute("UPDATE auth_users SET oidc_name='Provider fixture'")
    conn.commit()
   page.reload()
   page.get_by_label('Use name from SSO provider').check()
   expect(page.get_by_label('Display name')).to_have_attribute('readonly','')
   expect(page.get_by_label('Display name')).to_have_value('Provider fixture')
   page.get_by_role('button',name='Save profile').click()
   expect(page.get_by_label('Display name')).to_have_attribute('readonly','')
   expect(page.locator('.account-identity strong')).to_have_text('Provider fixture')
   page.emulate_media(color_scheme='dark')
   page.screenshot(path='.test-preview/auth-profile-sso-dark.png',full_page=True)
   page.get_by_label('Use name from SSO provider').uncheck()
   expect(page.get_by_label('Display name')).to_be_editable()
   expect(page.get_by_label('Display name')).to_have_value('Updated owner')
   page.get_by_role('button',name='Save profile').click()
   page.emulate_media(color_scheme='light')
   with closing(connect(db)) as conn:
    conn.execute('UPDATE auth_config SET value=? WHERE id=1',(json.dumps(original_config),))
    conn.commit()
   page.goto(address+'/settings/password')
   assert page.locator('.compact-page').bounding_box()['width'] <= 640
   page.screenshot(path='.test-preview/auth-password-desktop.png',full_page=True)
   page.goto(address+'/settings')
   gap=page.evaluate("() => document.querySelector('.appearance-panel').getBoundingClientRect().top-document.querySelector('.settings-account').getBoundingClientRect().bottom")
   assert gap >= 20, gap
   page.screenshot(path='.test-preview/auth-settings-desktop.png',full_page=True)
   page.set_viewport_size({'width':390,'height':844})
   page.goto(address+'/settings/security')
   expect(page.get_by_role('heading',name='Security and sign in')).to_be_visible()
   assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
   page.screenshot(path='.test-preview/auth-security-mobile.png',full_page=True)
   page.set_viewport_size({'width':1280,'height':900})
   page.screenshot(path='.test-preview/auth-security-desktop.png',full_page=True)
   page.get_by_label('Current password to confirm changes').fill('browser fixture passphrase')
   page.get_by_role('button',name='Save security settings').click()
   expect(page.get_by_role('status').filter(has_text='Security settings saved')).to_be_visible()
   # Exercise JS fetch through the global session CSRF wrapper.
   status=page.evaluate("""async () => (await fetch('/api/topics/1/save', {method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':document.querySelector('meta[name=topic-csrf]').content},body:JSON.stringify({saved:true})})).status""")
   assert status==404, status # Auth and both CSRF checks passed; fixture has no topic.
   page.goto(address+'/settings/api-keys')
   expect(page.get_by_label('Key name')).to_have_attribute('autocomplete','off')
   expect(page.get_by_label('Current password',exact=True)).to_have_attribute('autocomplete','new-password')
   page.get_by_label('Key name').fill('Browser reader')
   page.get_by_label('Read RSS XML').check()
   page.get_by_label('Private UI fixture').check()
   page.get_by_label('Current password',exact=True).fill('browser fixture passphrase')
   page.get_by_role('button',name='Create API key').click()
   expect(page.get_by_role('heading',name='Save your new key')).to_be_visible()
   key=page.locator('[role=status] code').inner_text()
   page.locator('[role=status] code').evaluate('(el)=>el.textContent="Secret shown once (redacted in preview)"')
   page.screenshot(path='.test-preview/auth-api-keys-desktop.png',full_page=True)
   page.set_viewport_size({'width':390,'height':844})
   assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
   page.locator('.account-menu > summary').click()
   expect(page.get_by_role('link',name='Manage profile')).to_be_visible()
   page.screenshot(path='.test-preview/auth-account-menu-mobile.png',full_page=True)
   page.goto(address+'/feeds')
   page.evaluate("""async () => {
     await navigator.serviceWorker.register('/service-worker.js', {scope:'/'});
     await navigator.serviceWorker.ready;
     localStorage.setItem('nightfeed.push.device','push-device-survives-logout');
     sessionStorage.setItem('nightfeed-submissions:private-fixture','private-download-state');
   }""")
   page.locator('.account-menu > summary').click()
   page.get_by_role('button',name='Log out').click()
   page.wait_for_url('**/auth/login?sso=off')
   assert page.evaluate("localStorage.getItem('nightfeed.push.device')") == 'push-device-survives-logout'
   assert page.evaluate("sessionStorage.getItem('nightfeed-submissions:private-fixture')") is None
   assert page.evaluate("async () => !!(await navigator.serviceWorker.getRegistration('/'))")
   expect(page.get_by_role('heading',name='Sign in to Nightfeed')).to_be_visible()
   page.go_back()
   expect(page.get_by_role('heading',name='Sign in to Nightfeed')).to_be_visible()
   assert 'Private UI fixture' not in page.content()
   page.goto(address+'/feeds')
   expect(page.get_by_role('heading',name='Sign in to Nightfeed')).to_be_visible()
   assert 'Private UI fixture' not in page.content()
   page.get_by_label('Username').fill('owner')
   page.get_by_label('Password',exact=True).fill('browser fixture passphrase')
   page.get_by_role('button',name='Sign in',exact=True).click()
   page.wait_for_url(address+'/feeds')
   with closing(connect(db)) as conn:
    conn.execute('UPDATE auth_sessions SET expires=?',(time.time()-1,));conn.commit()
   page.evaluate("window.dispatchEvent(new Event('focus'))")
   page.wait_for_url('**/auth/login?**',timeout=15000)
   expect(page.get_by_role('heading',name='Sign in to Nightfeed')).to_be_visible()
   assert 'Private UI fixture' not in page.content()
   page.get_by_label('Username').fill('owner');page.get_by_label('Password',exact=True).fill('browser fixture passphrase')
   page.get_by_role('button',name='Sign in',exact=True).click();page.wait_for_url(address+'/feeds')
   with closing(connect(db)) as conn:
    conn.execute('UPDATE auth_sessions SET expires=?',(time.time()+2,));conn.commit()
   page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}))")
   page.wait_for_url('**/auth/login?**',timeout=15000)
   expect(page.get_by_role('heading',name='Sign in to Nightfeed')).to_be_visible()
   page.get_by_label('Username').fill('owner');page.get_by_label('Password',exact=True).fill('browser fixture passphrase')
   page.get_by_role('button',name='Sign in',exact=True).click();page.wait_for_url(address+'/feeds')
   page.locator('.account-menu > summary').click()
   page.get_by_role('button',name='Log out').click()
   page.wait_for_url('**/auth/login?sso=off')
   page.set_viewport_size({'width':390,'height':844})
   page.screenshot(path='.test-preview/auth-login-mobile.png',full_page=True)
   assert not errors,errors
   browser.close()
   print('Browser checks passed: onboarding, desktop/mobile layout, profile editing, SSO name policy, dark theme, account menu, settings save, AJAX CSRF, scoped key creation, logout preserves push worker/token and clears private submission state, back navigation and login; no JS errors.')
 finally:
  server.shutdown();thread.join(5)
