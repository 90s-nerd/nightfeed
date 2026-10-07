"""Real-browser chat/settings checks with an isolated database and fake AI provider."""
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import patch
import base64
import json
import logging
import os
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
os.environ['NIGHTFEED_SECURE_COOKIES'] = '0'

from auth_support import authenticated_client
from rss_site_bridge import app as core
from rss_site_bridge import assistant as ai
from rss_site_bridge.assistant_services import Services, Access
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright, expect

logging.disable(logging.CRITICAL)
CONFIG = dict(feed_title='Release tracker', source_url='https://example.com/releases', item_selector='article', title_selector='a', link_selector='a', summary_selector='p', refresh_interval_minutes=360)
HTML = '<article><a href="/one">Linux release one</a><p>A stable release</p></article><article><a href="/two">Linux release two</a></article><article><a href="/three">Linux release three</a></article>'


def fake_complete(config, history, tools, system, on_delta=None):
    if history[-1]['role'] == 'tool':
        inspecting=any(call.get('id')==history[-1].get('tool_call_id') and call.get('function',{}).get('name')=='inspect_source' for message in history for call in message.get('tool_calls',[]))
        text = 'I inspected the listing. What should we call the feed?' if inspecting else 'I found 3 matching items in your stored content.'
        if on_delta:
            on_delta(text[:20]); on_delta(text[20:])
        return dict(role='assistant', content=text, tool_calls=[], _usage=dict(available=True,input_tokens=1200,output_tokens=80,cached_tokens=100,reasoning_tokens=0,context_window=16000,estimated_usd=.001))
    text = history[-1]['content'].lower()
    if text == 'help me set up a feed':
        return dict(role='assistant',content='Would you like me to proceed with this basic feed setup now?',tool_calls=[])
    if text == 'yes' and any(message.get('content')=='Would you like me to proceed with this basic feed setup now?' for message in history):
        return dict(role='assistant',content='',tool_calls=[dict(id='inspect-setup',type='function',function=dict(name='inspect_source',arguments=json.dumps(dict(source_url=CONFIG['source_url']))))])
    if text == 'who is the us president?':
        return dict(role='assistant',content=ai.scope.REDIRECT,tool_calls=[])
    name, args = ('search_topics', dict(query='Linux')) if 'search' in text else ('propose_feed_change', dict(config=CONFIG))
    narration = 'I’ll search your stored content.' if name == 'search_topics' else ''
    if on_delta and narration: on_delta(narration)
    return dict(role='assistant', content=narration, tool_calls=[dict(id='fixture-' + str(len(history)), type='function', function=dict(name=name, arguments=json.dumps(args)))], _usage=dict(available=True,input_tokens=1200,output_tokens=80,cached_tokens=100,reasoning_tokens=0,context_window=16000,estimated_usd=.001))




with TemporaryDirectory(dir=ROOT / '.test-preview') as temp:
    db = Path(temp) / 'ui.db'
    app = core.create_app(dict(TESTING=True, DATABASE_PATH=db, START_SCHEDULER=False))
    client = authenticated_client(app)
    server = make_server('127.0.0.1', 0, app, threaded=True)
    Thread(target=server.serve_forever, daemon=True).start()
    address = f'http://127.0.0.1:{server.server_port}'
    try:
        with patch('rss_site_bridge.assistant_provider.test_connection', return_value=True), patch('rss_site_bridge.assistant_provider.complete', side_effect=fake_complete), patch('rss_site_bridge.assistant_provider.transcribe', return_value='Search my saved Linux content'), patch('rss_site_bridge.assistant_services.fetch_document', return_value=core.FetchedDocument(HTML, CONFIG['source_url'])):
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel='chrome')
                context = browser.new_context(viewport={'width':1440,'height':1000})
                context.add_cookies([dict(name='nightfeed_auth', value=client.get_cookie('nightfeed_auth').value, url=address)])
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(address + '/settings')
                control_style = '(el) => { const s=getComputedStyle(el); return [s.minHeight,s.borderRadius,s.fontSize,s.backgroundImage,s.borderColor,s.paddingLeft,s.paddingRight]; }'
                reference_select = page.get_by_label('Color theme',exact=True).evaluate(control_style)
                settings_cards=page.locator('.settings-account')
                card_gap= settings_cards.evaluate_all('(els)=>els[1].getBoundingClientRect().top-els[0].getBoundingClientRect().bottom')
                assert card_gap == 24, card_gap
                page.screenshot(path=str(ROOT / '.test-preview/assistant-settings-entry-desktop.png'),full_page=True)
                page.goto(address + '/settings/ai/audit')
                assert page.get_by_label('Event type',exact=True).evaluate(control_style) == reference_select
                expect(page.get_by_text('No audit events yet.',exact=True)).to_be_visible()
                page.screenshot(path=str(ROOT / '.test-preview/assistant-audit-empty-desktop.png'),full_page=True)
                page.goto(address + '/settings/ai')
                expect(page.get_by_role('heading', name='AI and MCP', exact=True)).to_be_visible()
                expect(page.get_by_role('button', name='Open Nightfeed assistant')).to_have_count(0)
                assert page.get_by_label('API type',exact=True).evaluate(control_style) == reference_select
                field_styles=page.locator('.ai-form .field input').evaluate_all('(els)=>els.map(el=>{const s=getComputedStyle(el);return [s.minHeight,s.borderRadius,s.fontSize,s.borderColor]})')
                assert all(value == [reference_select[i] for i in (0,1,2,4)] for value in field_styles), field_styles
                header=page.locator('.account-page-heading')
                assert header.locator('.button-row a').evaluate_all('(els)=>Math.abs(els[0].getBoundingClientRect().top-els[1].getBoundingClientRect().top)<1')
                page.get_by_label('Connection name', exact=True).fill('Home model')
                page.get_by_label('API base URL', exact=True).fill('http://ollama:11434/v1')
                page.get_by_label('Model', exact=True).fill('fixture-model')
                page.get_by_text('Voice input', exact=True).click()
                page.get_by_label('Transcription API base URL',exact=True).fill('https://speech.example/v1')
                page.get_by_label('Transcription model',exact=True).fill('fixture-speech')
                page.get_by_role('button', name='Test and activate', exact=True).click()
                expect(page.get_by_role('status')).to_have_text('Settings saved.')
                expect(page.get_by_role('button', name='Open Nightfeed assistant')).to_be_visible()
                page.screenshot(path=str(ROOT / '.test-preview/assistant-settings-desktop.png'), full_page=True)
                page.goto(address + '/')
                page.get_by_role('button', name='Open Nightfeed assistant').click()
                expect(page.locator('[data-assistant-panel]')).to_be_visible()
                page.get_by_label('Message',exact=True).fill('Help me set up a feed');page.get_by_role('button',name='Send message').click()
                expect(page.get_by_text('Would you like me to proceed with this basic feed setup now?',exact=True)).to_be_visible()
                page.reload()
                expect(page.locator('[data-assistant-panel]')).to_be_visible()
                page.get_by_label('Message',exact=True).fill('Yes');page.get_by_role('button',name='Send message').click()
                expect(page.get_by_text('I inspected the listing. What should we call the feed?',exact=True)).to_be_visible()
                expect(page.locator('[data-assistant-messages]')).not_to_contain_text('There is no active proposal')
                expect(page.locator('.assistant-proposal')).to_have_count(0)
                page.get_by_role('button',name='New chat',exact=True).click()
                for question in ['how are you',"what's your name",'what is a feed']:
                    page.get_by_label('Message',exact=True).fill(question);page.get_by_role('button',name='Send message').click()
                    expect(page.get_by_text(ai.scope.local_reply(dict(content=question)),exact=True)).to_be_visible()
                page.get_by_role('button',name='New chat',exact=True).click()
                image_bytes = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aS8sAAAAASUVORK5CYII=')
                page.get_by_label('Chat shortcuts',exact=True).click()
                with page.expect_file_chooser() as chooser:
                    page.get_by_role('button',name='Add images',exact=True).click()
                chooser.value.set_files(dict(name='Upload.png',mimeType='image/png',buffer=image_bytes))
                expect(page.locator('[data-assistant-attachments] img')).to_have_count(1)
                page.get_by_role('button',name='Remove Upload.png',exact=True).click()
                expect(page.locator('[data-assistant-attachments]')).to_be_hidden()
                page.get_by_label('Message',exact=True).evaluate('''(el) => {
                    const bytes=Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aS8sAAAAASUVORK5CYII='), c=>c.charCodeAt(0));
                    const clipboard=new DataTransfer(); clipboard.items.add(new File([bytes],'Pasted.png',{type:'image/png'}));
                    el.dispatchEvent(new ClipboardEvent('paste',{clipboardData:clipboard,bubbles:true,cancelable:true}));
                }''')
                expect(page.locator('[data-assistant-attachments] img')).to_have_count(1)
                page.get_by_label('Message',exact=True).fill('Explain this image')
                rejected_path='**/api/assistant/conversations/*/messages'
                page.route(rejected_path,lambda route:route.fulfill(status=413,content_type='application/json',body=json.dumps(dict(error='Request is too large.'))))
                for attempt in range(2):
                    page.get_by_role('button',name='Send message',exact=True).click()
                    expect(page.locator('[data-assistant-status]')).to_have_text('Not sent. Request is too large.')
                    expect(page.locator('[data-role=user]')).to_have_count(0)
                    expect(page.get_by_label('Message',exact=True)).to_have_value('Explain this image')
                    expect(page.locator('[data-assistant-attachments] img')).to_have_count(1)
                page.unroute(rejected_path)
                page.get_by_label('Message', exact=True).fill('Create a release feed every six hours with no filters.')
                page.get_by_role('button', name='Send message', exact=True).click()
                expect(page.get_by_role('button', name='✓ Approve')).to_be_enabled(timeout=15000)
                expect(page.locator('.assistant-proposal')).to_be_visible()
                expect(page.get_by_role('button', name='✕ Deny')).to_be_enabled()
                self_count = len(core.list_profiles(db))
                assert self_count == 0, self_count
                page.locator('[data-assistant-messages]').evaluate('(el) => el.scrollTop = 0')
                page.screenshot(path=str(ROOT / '.test-preview/assistant-chat-desktop.png'), full_page=True)
                page.get_by_role('button', name='✓ Approve').click()
                expect(page.get_by_role('link', name='Open in Nightfeed ↗')).to_be_visible()
                assert len(core.list_profiles(db)) == 1
                selected_conversation = page.locator('[data-assistant-conversations]').input_value()
                assert selected_conversation
                page.reload()
                expect(page.locator('[data-assistant-messages] img[alt="Pasted.png"]')).to_be_visible()
                expect(page.locator('[data-assistant-panel]')).to_be_visible()
                expect(page.locator('.assistant-card').get_by_text('Feed created.', exact=True)).to_be_visible()
                expect(page.locator('[data-assistant-conversations]')).to_have_value(selected_conversation)
                page.get_by_role('button', name='Close assistant').click()
                page.get_by_role('button', name='Open Nightfeed assistant').click()
                expect(page.locator('[data-assistant-conversations]')).to_have_value(selected_conversation)
                page.get_by_label('Message', exact=True).fill('Search my saved Linux content')
                page.get_by_role('button', name='Send message', exact=True).click()
                expect(page.locator('[data-assistant-messages]')).to_contain_text('3 matching items')
                expect(page.get_by_role('button', name='New chat', exact=True)).to_be_enabled()
                activity = page.locator('.assistant-activity').last
                expect(activity).not_to_have_attribute('open','')
                activity.locator('summary').focus(); page.keyboard.press('Enter')
                expect(activity).to_contain_text('I’ll search your stored content.')
                expect(activity).to_contain_text('Matching results: 3')
                expect(page.locator('.assistant-message').get_by_text('I’ll search your stored content.',exact=True)).to_have_count(0)
                page.reload()
                activity = page.locator('.assistant-activity').last
                activity.locator('summary').click()
                expect(activity).to_contain_text('I’ll search your stored content.')
                expect(activity).to_contain_text('Matching results: 3')
                page.get_by_label('Context usage',exact=True).click()
                expect(page.locator('[data-context-usage]')).to_contain_text('1,200 input')
                expect(page.locator('[data-context-usage]')).to_contain_text('Estimated $0.001000')
                page.get_by_label('Context usage',exact=True).click()
                page.get_by_label('Message',exact=True).fill('Who is the US president?');page.get_by_role('button',name='Send message').click()
                expect(page.get_by_text(ai.scope.REDIRECT,exact=True)).to_be_visible()
                expect(page.locator('.assistant-proposal')).to_have_count(0)
                page.get_by_label('Message',exact=True).fill('Search my saved Linux content');page.get_by_role('button',name='Send message').click()
                expect(page.locator('[data-assistant-stop]')).to_be_hidden()
                page.get_by_label('Context usage',exact=True).click()
                expect(page.locator('[data-context-usage]')).to_contain_text('1,200 input')
                previous_chat=page.locator('[data-assistant-conversations]').input_value()
                page.get_by_role('button',name='New chat',exact=True).click()
                expect(page.locator('[data-assistant-conversations]')).to_have_value('')
                expect(page.locator('[data-context-usage]')).to_have_text("Usage appears after this conversation's next reply.")
                expect(page.locator('[data-context-ring]')).to_have_attribute('stroke-dasharray','0 63')
                page.locator('[data-assistant-conversations]').select_option(previous_chat)
                expect(page.locator('[data-context-usage]')).to_contain_text('1,200 input')
                page.get_by_label('Context usage',exact=True).click()

                page.get_by_label('Context usage',exact=True).click()
                expect(page.locator('[data-assistant-voice]')).to_have_count(0)
                page.evaluate('''() => {
                  navigator.mediaDevices.getUserMedia=async () => {
                    const context=new AudioContext(), destination=context.createMediaStreamDestination();
                    destination.stream.getTracks().forEach(track=>track.addEventListener('ended',()=>context.close()));
                    return destination.stream;
                  };
                }''')
                page.get_by_role('button', name='Dictate message',exact=True).click()
                expect(page.get_by_role('button',name='Finish dictation',exact=True)).to_be_visible()
                page.wait_for_timeout(500)
                page.get_by_role('button',name='Finish dictation',exact=True).click()
                expect(page.get_by_role('button',name='Dictate message',exact=True)).to_be_visible()
                expect(page.locator('[data-assistant-status]')).to_have_text('',timeout=20000)
                composer=page.get_by_label('Message',exact=True)
                composer.fill('How many new topics got added today?')
                composer.press('Enter')
                expect(page.locator('[data-assistant-messages]')).to_contain_text('3 items were added today')
                expect(page.locator('[data-assistant-status]')).to_have_text('')
                composer.fill('Show');composer.press('Enter')
                expect(page.locator('[data-assistant-messages]')).to_contain_text('I found 3 matching items')
                expect(page.locator('[data-assistant-status]')).to_have_text('')
                composer.fill('Who is the US president?')
                composer.press('Enter')
                expect(page.locator('[data-assistant-status]')).to_have_text('')
                composer.fill('Which one are those? Just show me.')
                composer.press('Enter')
                expect(page.locator('[data-assistant-messages]')).to_contain_text('I found 3 matching items')
                expect(page.locator('[data-assistant-messages]')).to_contain_text('Added ')
                page.set_viewport_size({'width':390,'height':844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.locator('[data-assistant-messages]').evaluate('(el) => el.scrollTop = el.scrollHeight')
                page.screenshot(path=str(ROOT / '.test-preview/assistant-chat-mobile.png'), full_page=True)
                page.emulate_media(color_scheme='dark')
                page.wait_for_function("getComputedStyle(document.querySelector('[data-assistant-panel]')).backgroundColor !== 'rgb(255, 255, 255)'")
                page.screenshot(path=str(ROOT / '.test-preview/assistant-chat-mobile-dark.png'), full_page=True)
                panel_colors = page.locator('[data-assistant-panel]').evaluate('(el) => [getComputedStyle(el).backgroundColor, getComputedStyle(el).color]')
                assert panel_colors[0] != 'rgb(255, 255, 255)', panel_colors
                activity = page.locator('.assistant-activity').last
                activity.locator('summary').click()
                activity.scroll_into_view_if_needed()
                expect(activity).to_contain_text('Completed')
                assert activity.evaluate('(el)=>el.scrollWidth <= el.clientWidth')
                page.screenshot(path=str(ROOT / '.test-preview/assistant-activity-mobile-dark.png'),full_page=True)
                # Real refresh replies must render escaped feed labels as links,
                # including brackets, backslashes and a literal ]( in the name.
                titles=['[Mal] Top releases this week','[Mal] Recently Added',r'Slash \\ Path ](Part)']
                for title in titles:
                    config,_=ai.Services(db,ai.Access('user:1',chat=True)).config(dict(CONFIG,feed_title=title))
                    core.create_profile(db,config)
                composer.fill('Refresh all feeds');composer.press('Enter')
                expect(page.locator('[data-assistant-status]')).to_have_text('',timeout=20000)
                for title in titles:
                    expect(page.locator('[data-assistant-messages]').get_by_role('link',name=title,exact=True)).to_be_visible()
                assert '[\\[Mal\\]' not in page.locator('[data-assistant-messages]').inner_text()
                page.reload();expect(page.locator('[data-assistant-panel]')).to_be_visible()
                for title in titles:
                    expect(page.locator('[data-assistant-messages]').get_by_role('link',name=title,exact=True)).to_be_visible()
                page.get_by_role('button', name='Close assistant').click()
                expect(page.locator('[data-assistant-panel]')).to_be_hidden()
                page.goto(address + '/settings/ai/audit')
                expect(page.get_by_role('heading',name='AI audit history')).to_be_visible()
                expect(page.get_by_text('transcription',exact=False).first).to_be_visible()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(ROOT / '.test-preview/assistant-audit-mobile-dark.png'),full_page=True)
                page.goto(address + '/settings/ai')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(ROOT / '.test-preview/assistant-settings-mobile-dark.png'), full_page=True)
                page.get_by_text('Usage and cost estimates',exact=True).click()
                expect(page.get_by_label('Input USD / million tokens',exact=True)).to_be_visible()
                page.get_by_text('Voice input',exact=True).click()
                expect(page.get_by_label('Transcription model',exact=True)).to_be_visible()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(ROOT / '.test-preview/assistant-settings-expanded-mobile-dark.png'),full_page=True)
                page.get_by_label('Help with API base URL',exact=True).click()
                page.wait_for_function('document.documentElement.scrollWidth <= innerWidth',timeout=3000)
                page.screenshot(path=str(ROOT / '.test-preview/assistant-endpoint-help-mobile-dark.png'))
                page.get_by_label('Help with API base URL',exact=True).click()
                page.get_by_label('Enable MCP endpoint').check()
                page.get_by_role('button', name='Save MCP settings').click()
                assert ai.settings(db)['mcp_enabled'] == 1
                assert errors == [], errors
                browser.close()
        print('Assistant UI: provider activation, previews, approval, persistence, search, MCP settings, mobile, dark theme and JavaScript errors checked.')
    finally:
        server.shutdown()
