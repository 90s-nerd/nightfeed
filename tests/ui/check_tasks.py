"""Topic watch setup and management UI, with no live provider or deliveries."""
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import patch
import hashlib
import json
import logging
import os
import sys
import time

ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
os.environ['NIGHTFEED_SECURE_COOKIES']='0'
from auth_support import authenticated_client
from rss_site_bridge import app as core, tasks, assistant as ai
from rss_site_bridge.push_notifications import DEFAULTS
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright, expect

logging.disable(logging.CRITICAL)
def provider(config,history,tools,system,on_delta=None):
    if history[-1].get('content')=='Notify me when Spider Man comes in':
        return dict(role='assistant',content='Which Spider-Man topic: a movie, comic, or news? This will help create your notification task.',tool_calls=[])
    return dict(role='assistant',content='',tool_calls=[dict(id='watch',type='function',function=dict(name='prepare_topic_watch',arguments=json.dumps(dict(topic='Spider Man'))))])

def scope_fixture(config,history,context):
    # A standalone classifier would reject this fragment; workflow continuity
    # must recognize it before asking that classifier.
    return dict(decision='redirect' if history[-1].get('content')=='about the new movie' else 'allow',usage={})

with TemporaryDirectory(dir=ROOT/'.test-preview') as temp:
    db=Path(temp)/'ui.db';app=core.create_app(dict(TESTING=True,DATABASE_PATH=db,START_SCHEDULER=False));client=authenticated_client(app)
    ai.save_connection(db,dict(name='Fixture',api_type='compatible',base_url='https://example.com/v1',model='fixture',api_key='fixture',speech_key='',speech_url='',speech_model='',timeout=45,max_tokens=1000),None,tested=True)
    feed=core.create_profile(db,core.FeedRequest(feed_title='Movies',source_url='https://example.com/topics',item_selector='article',title_selector='a',link_selector='a',summary_selector='p',max_items=100,refresh_interval_minutes=60,fetch_mode='http'))
    with closing(core.connect_db(db)) as conn:
        conn.execute("UPDATE app_settings SET timezone_name='America/Chicago',smtp_enabled=1,smtp_host='smtp.example.com',smtp_port=587,smtp_username='test',smtp_password='test',smtp_from_email='sender@example.com',smtp_to_email='owner@example.com'")
        conn.execute('INSERT INTO push_devices(id,secret_hash,endpoint_hash,subscription,preferences,user_id) VALUES(?,?,?,?,?,?)',('device',hashlib.sha256(b'ui-device-secret').hexdigest(),'endpoint','{}',json.dumps(DEFAULTS),1));conn.commit()
    server=make_server('127.0.0.1',0,app,threaded=True);Thread(target=server.serve_forever,daemon=True).start();address=f'http://127.0.0.1:{server.server_port}'
    try:
        with patch('rss_site_bridge.assistant_scope.classify',side_effect=scope_fixture),patch('rss_site_bridge.assistant_provider.complete',side_effect=provider),patch.object(core,'send_smtp_message'),patch('rss_site_bridge.push_notifications.deliver'):
            with sync_playwright() as playwright:
                browser=playwright.chromium.launch(channel='chrome');context=browser.new_context(viewport=dict(width=1440,height=1000));context.add_cookies([dict(name='nightfeed_auth',value=client.get_cookie('nightfeed_auth').value,url=address)])
                page=context.new_page();page.add_init_script("localStorage.setItem('nightfeed.push.device','ui-device-secret')");errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(address+'/');page.get_by_role('button',name='Open Nightfeed assistant').click();page.get_by_label('Message',exact=True).fill('Notify me when Spider Man comes in');page.get_by_role('button',name='Send message').click()
                expect(page.get_by_text('Which Spider-Man topic: a movie, comic, or news? This will help create your notification task.',exact=True)).to_be_visible()
                page.get_by_label('Message',exact=True).fill('about the new movie');page.get_by_role('button',name='Send message').click()
                expect(page.get_by_role('button',name='Any matching title')).to_be_visible()
                expect(page.locator('.task-setup')).to_have_count(0)
                page.set_viewport_size(dict(width=390,height=844));page.emulate_media(color_scheme='dark')
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                page.screenshot(path=str(ROOT/'.test-preview/tasks-question-mobile-dark.png'))
                page.set_viewport_size(dict(width=1440,height=1000));page.emulate_media(color_scheme='light')
                page.get_by_role('button',name='Any matching title').click()
                expect(page.get_by_role('button',name='All feeds',exact=True)).to_be_visible()
                page.reload();expect(page.get_by_role('button',name='All feeds',exact=True)).to_be_visible()
                page.get_by_role('button',name='All feeds',exact=True).click()
                page.get_by_role('button',name='Every new match',exact=True).click()
                expect(page.get_by_role('button',name='Nightfeed',exact=True)).to_be_disabled()
                page.get_by_role('button',name='Push',exact=True).click()
                page.get_by_role('button',name='Email',exact=True).click()
                expect(page.get_by_role('button',name='Push',exact=True)).to_have_attribute('aria-pressed','true')
                page.get_by_role('button',name='Continue',exact=True).click()
                page.get_by_role('button',name='7 days',exact=True).click()
                page.get_by_role('button',name='No extra filters',exact=True).click()
                page.locator('[data-assistant-messages]').evaluate('(el)=>el.scrollTop=el.scrollHeight');page.screenshot(path=str(ROOT/'.test-preview/task-setup-desktop.png'))
                page.get_by_role('button',name='✓ Approve',exact=True).click();expect(page.get_by_text('Task created. Watching from now on.',exact=True)).to_be_visible();expect(page.locator('.task-setup')).to_have_count(0)
                page.reload();expect(page.locator('.task-setup')).to_have_count(0)
                page.get_by_role('tab',name='Tasks',exact=True).click();expect(page.get_by_role('heading',name='Watch for Spider Man',exact=True)).to_be_visible();expect(page.get_by_text('Nightfeed + Push + Email',exact=False)).to_be_visible()
                page.get_by_role('button',name='Pause',exact=True).click();expect(page.locator('.task-state')).to_have_text('Paused');page.get_by_role('button',name='Resume',exact=True).click();expect(page.locator('.task-state')).to_have_text('Active')
                page.screenshot(path=str(ROOT/'.test-preview/tasks-assistant.png'))
                core.refresh_profile(db,feed.id,document=core.FetchedDocument('<article><a href="/movie">Spider-Man Tamil</a></article>',feed.source_url));tasks.dispatch(db)
                page.get_by_role('button',name='Close assistant').click()
                page.goto(address+'/tasks');expect(page.get_by_text('1 match',exact=False)).to_be_visible()
                page.get_by_role('button',name='New task',exact=True).click()
                expect(page.get_by_role('button',name='Create task',exact=True)).to_have_class('btn btn-primary')
                expect(page.get_by_role('button',name='Cancel',exact=True)).to_have_class('btn btn-secondary')
                expect(page.get_by_role('button',name='Preview existing matches',exact=True)).to_have_class('btn btn-secondary')
                page.locator('.editor-actions').scroll_into_view_if_needed();page.screenshot(path=str(ROOT/'.test-preview/task-page-create-actions.png'))
                page.get_by_role('button',name='Cancel',exact=True).click()
                page.get_by_role('button',name='Details',exact=True).click();expect(page.get_by_role('heading',name='Delivery history')).to_be_visible();expect(page.get_by_text('Email · sent',exact=False)).to_be_visible()
                page.screenshot(path=str(ROOT/'.test-preview/tasks-details-desktop.png'),full_page=True)
                page.get_by_role('button',name='Edit task').click();page.get_by_label('Task name',exact=True).fill('Spider-Man alerts');page.get_by_role('radio',name='Once, then complete').check();page.get_by_role('button',name='Save changes',exact=True).click();expect(page.get_by_role('heading',name='Spider-Man alerts')).to_be_visible()
                page.get_by_role('button',name='Details',exact=True).click();page.get_by_role('button',name='Archive',exact=True).click();expect(page.locator('.task-state')).to_have_text('Archived')
                page.set_viewport_size(dict(width=390,height=844));page.emulate_media(color_scheme='dark');page.screenshot(path=str(ROOT/'.test-preview/tasks-mobile-dark.png'));assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                identity=tasks.list_tasks(db,__import__('rss_site_bridge.assistant_services',fromlist=['Access']).Access('user:1'))[0]['id']
                with closing(core.connect_db(db)) as conn:conn.execute("UPDATE topic_tasks SET state='active',expires=? WHERE id=?",(time.time()-1,identity));conn.commit()
                page.reload();expect(page.get_by_text('Expired',exact=True)).to_be_visible()
                with closing(core.connect_db(db)) as conn:conn.execute('UPDATE assistant_providers SET tested=0');conn.commit()
                page.reload();expect(page.get_by_role('heading',name='Tasks',exact=True)).to_be_visible();expect(page.get_by_role('button',name='Open Nightfeed assistant')).to_have_count(0)
                assert not errors,errors;browser.close()
        print('Tasks UI: chat choices, multiselect delivery, expiry, preview, one result, persistence, pause/resume, edit/archive, delivery history, mobile/dark and provider-independent management verified.')
    finally:server.shutdown()
