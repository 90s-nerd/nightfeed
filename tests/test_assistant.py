"""Assistant integration checks use fixture HTML and simulated providers, never live APIs."""
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch, Mock
import base64
import json
import io
import socket
import time
import unittest
from datetime import datetime, timezone
from threading import Event

from auth_support import authenticated_client
from rss_site_bridge import app as core
from rss_site_bridge import assistant as ai
from rss_site_bridge import assistant_provider as provider
from rss_site_bridge.assistant_network import public_addresses, fetch_public
from rss_site_bridge.assistant_services import Access, Services, definitions
from rss_site_bridge.auth import fingerprint

HTML = '<html><body><nav><a href="/login">Login</a></nav><article><a href="/one">Linux stable</a><p>First release</p></article><article><a href="/two">Linux beta</a></article><article><a href="/three">Windows stable</a></article></body></html>'
CONFIG = dict(feed_title='Releases', source_url='https://example.com/releases', item_selector='article', title_selector='a', link_selector='a', summary_selector='p', refresh_interval_minutes=60)


class AssistantTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'db'
        self.app = core.create_app(dict(TESTING=True, DATABASE_PATH=self.db, START_SCHEDULER=False))
        self.client = authenticated_client(self.app)
        self.access = Access('user:1', chat=True, conversation='test')
        self.services = Services(self.db, self.access)
        self.fetch = patch('rss_site_bridge.assistant_services.fetch_document', return_value=core.FetchedDocument(HTML, CONFIG['source_url']))
        self.fetch.start()
        self.addCleanup(self.fetch.stop)

    def activate(self):
        config = dict(name='Fixture', api_type='compatible', base_url='https://provider.example/v1', model='fixture', api_key='secret-fixture',
                      speech_key='', speech_url='', speech_model='', timeout=45, max_tokens=2000)
        ai.save_connection(self.db, config, None, tested=True)
        return config

    def create_feed(self, **changes):
        values = dict(CONFIG, **changes)
        config, _ = self.services.config(values)
        return core.create_profile(self.db, config)

    def key(self, scopes=('mcp:read', 'mcp:write'), feed_ids=()):
        raw = 'nf_test_' + str(time.time_ns())
        with closing(core.connect_db(self.db)) as conn:
            conn.execute('INSERT INTO auth_api_keys(user_id,name,token_hash,prefix,scopes,feed_ids,created) VALUES(?,?,?,?,?,?,?)',
                         (1, 'MCP fixture', fingerprint(raw), raw[:11], json.dumps(scopes), json.dumps(feed_ids), time.time()))
            conn.execute('UPDATE assistant_config SET mcp_enabled=1')
            conn.commit()
        return raw

    def mcp(self, key, method, params=None):
        return self.app.test_client().post('/mcp', json=dict(jsonrpc='2.0', id=1, method=method, params=params or {}),
                    headers={'Authorization': 'Bearer ' + key, 'Accept': 'application/json, text/event-stream'})

    def test_chat_requires_tested_provider_and_csrf(self):
        self.assertEqual(self.client.post('/api/assistant/conversations').status_code, 409)
        self.assertNotIn(b'data-assistant-toggle', self.client.get('/').data)
        self.activate()
        self.assertIn(b'data-assistant-toggle', self.client.get('/').data)
        self.assertEqual(self.client.post('/api/assistant/conversations').status_code, 200)
        self.assertEqual(self.app.test_client().post('/api/assistant/conversations').status_code, 401)

    def test_audit_records_actions_failures_and_redacts_credentials(self):
        from rss_site_bridge.assistant_audit import record
        self.services.call('list_feeds', {})
        with self.assertRaises(ValueError): self.services.call('get_feed', dict(feed_id=999))
        record(self.db, 'user:1', 'user_message', message='Bearer token-value', api_key='never-log-this', source_url='https://example.com/?token=private')
        response = self.client.get('/settings/ai/audit?export=1')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'never-log-this', response.data)
        self.assertNotIn(b'token-value', response.data)
        self.assertNotIn(b'token=private', response.data)
        events = response.json
        self.assertTrue(any(e['kind']=='tool_call' and e['status']=='error' for e in events))
        key = self.key()
        denied = self.app.test_client().get('/settings/ai/audit', headers={'Authorization':'Bearer '+key})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(self.client.get('/settings/ai/audit').status_code, 200)

    def test_settings_patch_preserves_password_and_detects_stale_draft(self):
        with closing(core.connect_db(self.db)) as conn:
            conn.execute('UPDATE app_settings SET smtp_password="saved-private",smtp_host="old.example"'); conn.commit()
        draft = self.services.call('propose_settings_change', dict(settings=dict(timezone_name='America/Chicago', public_base_url='https://nightfeed.example')))
        self.assertNotIn('saved-private', json.dumps(draft))
        self.services.apply(draft['draft_id'])
        settings = core.get_app_settings(self.db)
        self.assertEqual(settings.smtp_password, 'saved-private')
        self.assertEqual(settings.smtp_host, 'old.example')
        self.assertEqual(settings.timezone_name, 'America/Chicago')
        next_draft = self.services.call('propose_settings_change', dict(settings=dict(public_base_url='https://second.example')))
        with closing(core.connect_db(self.db)) as conn:
            conn.execute('UPDATE app_settings SET smtp_host="changed.example"'); conn.commit()
        with self.assertRaisesRegex(ValueError, 'changed since'): self.services.apply(next_draft['draft_id'])
        with self.assertRaises(ValueError): self.services.call('propose_settings_change', dict(settings=dict(smtp_password='secret')))

    def test_device_preferences_are_bound_to_registered_device_and_chat(self):
        from rss_site_bridge import push_notifications as push
        with closing(core.connect_db(self.db)) as conn:
            conn.execute('INSERT INTO push_devices(id,secret_hash,endpoint_hash,subscription,preferences,user_id) VALUES(?,?,?,?,?,?)', ('device1','hash1','endpoint1','{}',json.dumps(push.DEFAULTS),1)); conn.commit()
        self.access.device_id='device1'
        draft=self.services.call('propose_push_preferences',dict(preferences=dict(updated=True, interval=60)))
        wrong=Services(self.db,Access('user:1',chat=True,conversation='test',device_id='other'))
        with self.assertRaisesRegex(ValueError,'original registered device'): wrong.apply(draft['draft_id'])
        self.services.apply(draft['draft_id'])
        prefs=self.services.call('get_device_preferences',{})['push_preferences']
        self.assertTrue(prefs['updated']); self.assertEqual(prefs['interval'],60); self.assertEqual(prefs['daily_limit'],12)
        appearance=self.services.call('propose_appearance',dict(appearance='dark'))
        self.assertEqual(self.services.apply(appearance['draft_id'])['browser_action']['appearance'],'dark')
        names={t['name'] for t in definitions(Access('key:1'))}
        self.assertNotIn('get_device_preferences',names); self.assertNotIn('propose_appearance',names); self.assertNotIn('propose_push_preferences',names)

    def test_encrypted_provider_settings_and_activation(self):
        self.activate()
        with closing(core.connect_db(self.db)) as conn:
            stored = conn.execute('SELECT config FROM assistant_providers').fetchone()[0]
        self.assertNotIn('secret-fixture', stored)
        self.assertNotIn(b'secret-fixture', self.client.get('/settings/ai').data)
        config = ai.connection(self.db, secrets_visible=True)
        self.assertEqual(config['api_key'], 'secret-fixture')
        self.assertEqual(ai.connection(self.db)['api_key'], '')
        form = dict(provider_id=config['id'], name='Changed', api_type='compatible', base_url=config['base_url'], model='changed', timeout=45, max_tokens=2000, action='save')
        self.assertEqual(self.client.post('/settings/ai', data=form).status_code, 302)
        self.assertFalse(ai.connection(self.db)['tested'])
        self.assertEqual(ai.connection(self.db, secrets_visible=True)['api_key'], 'secret-fixture')

    def test_failed_connection_does_not_reflect_key(self):
        form = dict(name='Broken', api_type='compatible', base_url='https://provider.example/v1', model='fixture', timeout=45, max_tokens=2000, api_key='never-reflect-this', action='test')
        with patch.object(provider, 'test_connection', side_effect=ValueError('Connection failed')):
            response = self.client.post('/settings/ai', data=form)
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(b'never-reflect-this', response.data)
        self.assertIsNone(ai.connection(self.db))

    def test_preview_uses_actual_extractor_and_filters(self):
        result = self.services.call('preview_feed', dict(config=dict(CONFIG, filter_rules='Linux', exclude_filter_rules='beta')))
        self.assertEqual([item['title'] for item in result['items']], ['Linux stable'])
        self.assertEqual(result['items'][0]['link'], 'https://example.com/one')
        self.assertEqual(core.list_profiles(self.db), [])
        with self.assertRaises(ValueError):
            self.services.call('preview_feed', dict(config=dict(CONFIG, item_selector='.missing')))

    def test_apply_is_atomic_idempotent_and_scoped_to_conversation(self):
        draft = self.services.call('propose_feed_change', dict(config=CONFIG))
        self.assertEqual(core.list_profiles(self.db), [])
        with self.assertRaises(ValueError):
            Services(self.db, Access('user:1', chat=True, conversation='different')).apply(draft['draft_id'])
        first = self.services.apply(draft['draft_id'])
        self.assertEqual(self.services.apply(draft['draft_id']), first)
        self.assertEqual(len(core.list_profiles(self.db)), 1)
        with closing(core.connect_db(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM assistant_actions').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT count(*) FROM feed_items').fetchone()[0], 3)

    def test_edit_preserves_unrelated_fields_and_rejects_stale_draft(self):
        feed = self.create_feed(filter_rules='Linux', priority=12)
        draft = self.services.call('propose_feed_change', dict(feed_id=feed.id, config=dict(refresh_interval_minutes=180)))
        self.services.apply(draft['draft_id'])
        updated = core.get_profile_by_id(self.db, feed.id)
        self.assertEqual(updated.filter_rules, 'Linux')
        self.assertEqual(updated.priority, 12)
        self.assertEqual(updated.refresh_interval_minutes, 180)
        draft = self.services.call('propose_feed_change', dict(feed_id=feed.id, config=dict(refresh_interval_minutes=360)))
        config = updated.to_feed_request()
        config.feed_title = 'Concurrent edit'
        core.update_profile(self.db, feed.id, config)
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.services.apply(draft['draft_id'])

    def test_timezone_is_global_and_stale_feed_schedule_is_rejected(self):
        feed = self.create_feed()
        draft = self.services.call('propose_feed_change', dict(feed_id=feed.id, config=dict(refresh_interval_minutes=180)))
        timezone = self.services.call('propose_timezone', dict(timezone_name='America/Chicago'))
        self.services.apply(timezone['draft_id'])
        self.assertEqual(core.get_app_settings(self.db).timezone_name, 'America/Chicago')
        self.assertEqual(core.get_profile_by_id(self.db, feed.id).schedule_timezone, 'America/Chicago')
        with self.assertRaises(ValueError):
            self.services.apply(draft['draft_id'])

    def test_empty_filters_and_unknown_fields_do_not_save(self):
        with self.assertRaises(ValueError):
            self.services.call('propose_feed_change', dict(config=dict(CONFIG, filter_rules='never matches')))
        with self.assertRaises(ValueError):
            self.services.call('propose_feed_change', dict(config=dict(CONFIG, execute_shell='no')))
        with self.assertRaises(ValueError):
            self.services.call('preview_feed', dict(config=dict(CONFIG, max_items=True)))

    def seed_notifications(self, feed_id, count):
        with closing(core.connect_db(self.db)) as conn:
            conn.executemany('INSERT INTO notifications(profile_id,event_type,severity,category,title,message,source_url,created_at,metadata_json) VALUES(?,?,?,?,?,?,?,?,?)', [(feed_id,'refresh','info','success','Refresh '+str(i),'Updated','',core.utcnow_text(),'{}') for i in range(count)])
            conn.commit()

    def test_notification_counts_bulk_snapshot_and_idempotence(self):
        feed = self.create_feed()
        self.seed_notifications(feed.id, 111)
        state = self.services.call('get_app_state', {})
        self.assertEqual(state['unread_notifications'], 111)
        self.assertEqual(state['unread_topics'], 0)
        notices = self.services.call('list_notifications', {})
        self.assertEqual(notices['unread_count'], 111)
        self.assertEqual(notices['total_count'], 111)
        self.assertEqual(notices['returned_count'], 25)
        self.assertTrue(notices['truncated'])
        first = notices['items'][0]['id']
        self.services.call('get_notification', dict(notification_id=first))
        self.assertEqual(core.count_unread_notifications(self.db), 111)
        draft = self.services.call('propose_notification_action', dict(action='mark_all_read'))
        self.seed_notifications(feed.id, 1)
        self.assertEqual(core.count_unread_notifications(self.db), 112)
        result = self.services.apply(draft['draft_id'])
        self.assertEqual(result['changed_count'], 111)
        self.assertEqual(result['browser_action']['unread_notifications'], 1)
        self.assertEqual(core.count_unread_notifications(self.db), 1)
        self.assertEqual(self.services.apply(draft['draft_id']), result)
        delete = self.services.call('propose_notification_action', dict(action='delete_read'))
        self.assertEqual(delete['payload']['count'], 111)
        self.assertEqual(self.services.apply(delete['draft_id'])['changed_count'], 111)
        self.assertEqual(self.services.call('get_app_state', {})['total_notifications'], 1)

    def test_notification_and_topic_actions_enforce_scopes(self):
        feed = self.create_feed()
        private = self.create_feed(feed_title='Private')
        self.seed_notifications(feed.id, 2); self.seed_notifications(private.id, 3)
        restricted = Services(self.db, Access('key:1', ('mcp:read','mcp:write'), (feed.id,)))
        self.assertEqual(restricted.call('get_app_state', {})['unread_notifications'], 2)
        notices = restricted.call('list_notifications', {})
        for name, args in [('get_notification',dict(notification_id=5)), ('propose_notification_action',dict(action='delete',notification_id=5)), ('list_notifications',dict(feed_id=private.id))]:
            with self.assertRaises(ValueError): restricted.call(name,args)
        draft = restricted.call('propose_notification_action',dict(action='mark_all_read'))
        restricted.apply(draft['draft_id'])
        self.assertEqual(core.count_unread_notifications(self.db), 3)
        readonly = Services(self.db, Access('key:2', ('mcp:read',)))
        with self.assertRaises(ValueError): readonly.call('propose_notification_action',dict(action='mark_all_read'))
        self.assertEqual(self.services.call('get_notification',dict(notification_id=notices['items'][0]['id']))['read'], True)
        with closing(core.connect_db(self.db)) as conn:
            conn.execute('INSERT INTO feed_items(profile_id,title,link,summary,discovered_at) VALUES(?,?,?,?,?)',(feed.id,'Topic','https://example.com/topic','',core.utcnow_text()))
            item_id=conn.execute('SELECT id FROM feed_items').fetchone()[0];conn.commit()
        for action in ('save','unsave','mark_read'):
            proposal=restricted.call('propose_topic_action',dict(action=action,item_id=item_id))
            restricted.apply(proposal['draft_id'])
            topic=restricted.call('get_topic',dict(item_id=item_id))
            self.assertEqual(topic['saved'], action=='save')
            self.assertEqual(restricted.call('count_topics',dict(status='saved'))['total_count'], 1 if action=='save' else 0)
        state=restricted.call('get_app_state',{})
        self.assertEqual(state['unread_topics'],0);self.assertEqual(state['saved_topics'],0)
        self.assertEqual(core.count_unread_notifications(self.db),3)

    def test_counts_use_all_stored_items_and_search_reports_its_limit(self):
        first = self.create_feed()
        other = self.create_feed(feed_title='Other')
        with closing(core.connect_db(self.db)) as conn:
            for index in range(104):
                conn.execute('INSERT INTO feed_items(profile_id,title,link,summary,discovered_at) VALUES(?,?,?,?,?)', (first.id, 'Linux ' + str(index), 'https://example.com/' + str(index), '', core.utcnow_text()))
            conn.execute('INSERT INTO feed_items(profile_id,title,link,summary,discovered_at) VALUES(?,?,?,?,?)', (other.id, 'Linux private', 'https://example.com/private', '', core.utcnow_text()))
            conn.commit()
        self.assertEqual(self.services.call('get_feed', dict(feed_id=first.id))['stored_item_count'], 104)
        self.assertEqual(self.services.call('count_topics', dict(feed_id=first.id))['total_count'], 104)
        result = self.services.call('search_topics', dict(feed_id=first.id, query='Linux'))
        self.assertEqual(result['total_count'], 104)
        self.assertEqual(result['returned_count'], 25)
        self.assertTrue(result['truncated'])
        restricted = Services(self.db, Access('key:1', ('mcp:read',), (first.id,)))
        self.assertEqual(restricted.call('count_topics', {})['total_count'], 104)
        with self.assertRaises(ValueError):
            restricted.call('count_topics', dict(feed_id=other.id))

    def test_added_day_queries_use_discovery_time_timezone_and_feed_permissions(self):
        feed=self.create_feed();other=self.create_feed(feed_title='Other')
        with closing(core.connect_db(self.db)) as conn:
            conn.execute("UPDATE app_settings SET timezone_name='America/New_York'")
            for index,stamp in enumerate(['2026-03-08T04:59:59+00:00','2026-03-08T05:00:00+00:00','2026-03-09T03:59:59+00:00','2026-03-09T04:00:00+00:00']):
                conn.execute('INSERT INTO feed_items(profile_id,title,link,summary,discovered_at) VALUES(?,?,?,?,?)',(feed.id,'Linux',f'https://example.com/{index}','',stamp))
            conn.execute('INSERT INTO feed_items(profile_id,title,link,summary,discovered_at) VALUES(?,?,?,?,?)',(other.id,'Linux','https://example.com/private','','2026-03-08T12:00:00+00:00'));conn.commit()
        restricted=Services(self.db,Access('key:1',('mcp:read',),(feed.id,)))
        counted=restricted.call('count_topics',dict(added_on='2026-03-08'))
        found=restricted.call('search_topics',dict(query='',added_on='2026-03-08'))
        self.assertEqual(counted['total_count'],2);self.assertEqual(found['total_count'],2)
        self.assertEqual(counted['timezone'],'America/New_York')
        self.assertEqual(counted['time_field'],'discovered_at')
        self.assertEqual({item['feed_id'] for item in found['items']},{feed.id})
        self.assertEqual(self.services.call('count_topics',dict(added_on='2026-03-08'))['total_count'],3)
        with self.assertRaises(ValueError):restricted.call('count_topics',dict(added_on='2026-03-08',feed_id=other.id))
        with self.assertRaises(ValueError):restricted.call('count_topics',dict(added_on='not-a-date'))

    def test_audit_conversation_groups_keep_complete_usage_and_deleted_history(self):
        from rss_site_bridge.assistant_audit import record, grouped_page
        with closing(core.connect_db(self.db)) as conn:
            conn.execute("INSERT INTO assistant_conversations(id,principal,title,created,updated) VALUES('chat-a','user:1','Movie watch',?,?)",(time.time(),time.time()));conn.commit()
        for index in range(105):
            record(self.db,'user:1','provider_request',conversation='chat-a',usage=dict(available=True,input_tokens=2,output_tokens=1,estimated_usd=.0001))
        record(self.db,'user:1','user_message',conversation='deleted-chat',message='Old feed setup')
        record(self.db,'user:1','turn_error',conversation='deleted-chat',status='error')
        record(self.db,'user:1','connection_test')
        groups,events,more=grouped_page(self.db,1,'')
        self.assertFalse(more);self.assertEqual(len(groups),3)
        chat=next(g for g in groups if g['conversation']=='chat-a')
        self.assertEqual(chat['title'],'Movie watch');self.assertEqual(chat['event_count'],105)
        self.assertEqual(len(chat['events']),5);self.assertEqual(chat['usage']['input_tokens'],210)
        self.assertEqual(chat['usage']['requests'],105);self.assertAlmostEqual(chat['usage']['estimated_usd'],.0105)
        deleted=next(g for g in groups if g['conversation']=='deleted-chat')
        self.assertTrue(deleted['deleted']);self.assertEqual(deleted['title'],'Old feed setup')
        self.assertEqual(deleted['errors'],1)
        with closing(core.connect_db(self.db)) as conn:
            conn.execute("UPDATE assistant_conversations SET title=? WHERE id='chat-a'",('Movie watch sk-'+('s'*20),));conn.commit()
        response=self.client.get('/settings/ai/audit')
        self.assertEqual(response.status_code,200);self.assertIn(b'Movie watch',response.data)
        self.assertNotIn(('sk-'+('s'*20)).encode(),response.data)
        first=self.client.get('/settings/ai/audit?conversation=chat-a&export=1').json
        second=self.client.get('/settings/ai/audit?conversation=chat-a&page=2&export=1').json
        self.assertEqual(len(first),100);self.assertEqual(len(second),5)
        self.assertEqual(len({e['id'] for e in first+second}),105)
        selected=self.client.get('/settings/ai/audit?conversation=chat-a')
        self.assertEqual(selected.status_code,200);self.assertIn(b'name="conversation" value="chat-a"',selected.data)
        self.assertIn(b'Older events',selected.data)
        filtered=self.client.get('/settings/ai/audit?kind=turn_error&export=1').json
        self.assertEqual([e['conversation'] for e in filtered],['deleted-chat'])
        for index in range(18):record(self.db,'user:1','local_reply',conversation=f'new-chat-{index}')
        newer,_,more=grouped_page(self.db,1,'');older,_,last_more=grouped_page(self.db,2,'')
        self.assertEqual(len(newer),20);self.assertTrue(more);self.assertFalse(last_more)
        self.assertEqual(len({g['conversation'] for g in newer+older}),21)

    def test_internal_search_honors_feed_restrictions(self):
        first = self.create_feed()
        second = self.create_feed(feed_title='Private')
        with closing(core.connect_db(self.db)) as conn:
            for feed in (first, second):
                conn.execute('INSERT INTO feed_items(profile_id,title,link,summary,discovered_at) VALUES(?,?,?,?,?)', (feed.id, 'Linux', 'https://example.com/' + str(feed.id), '', core.utcnow_text()))
            conn.commit()
        restricted = Services(self.db, Access('key:1', ('mcp:read', 'mcp:write'), (first.id,)))
        self.assertEqual(len(restricted.call('search_topics', dict(query='Linux'))['items']), 1)
        with self.assertRaises(ValueError):
            restricted.call('get_feed', dict(feed_id=second.id))
        with self.assertRaises(ValueError):
            restricted.call('propose_feed_change', dict(config=CONFIG))
        with self.assertRaises(ValueError):
            restricted.call('inspect_source', dict(source_url=CONFIG['source_url']))
        with self.assertRaises(ValueError):
            restricted.call('preview_feed', dict(feed_id=first.id, config=dict(source_url='https://other.example/')))

    def test_mcp_initialize_tools_permissions_and_no_browser(self):
        key = self.key(('mcp:read',))
        result = self.mcp(key, 'initialize', dict(protocolVersion='2025-06-18', capabilities={}, clientInfo=dict(name='test', version='1')))
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['result']['protocolVersion'], '2025-06-18')
        names = {t['name'] for t in self.mcp(key, 'tools/list').json['result']['tools']}
        self.assertIn('search_topics', names)
        self.assertNotIn('apply_draft', names)
        self.assertNotIn('open_safe_browser', names)
        self.assertNotIn('web_search', names)
        response = self.mcp(key, 'tools/call', dict(name='propose_feed_change', arguments=dict(config=CONFIG)))
        self.assertTrue(response.json['result']['isError'])
        self.assertEqual(self.client.post('/mcp', json={}).status_code, 401)

    def test_mcp_write_roundtrip_without_ai_configured(self):
        key = self.key()
        self.assertIsNone(ai.connection(self.db))
        draft = self.mcp(key, 'tools/call', dict(name='propose_feed_change', arguments=dict(config=CONFIG))).json['result']['structuredContent']
        result = self.mcp(key, 'tools/call', dict(name='apply_draft', arguments=dict(draft_id=draft['draft_id']))).json['result']
        self.assertFalse(result['isError'])
        self.assertEqual(len(core.list_profiles(self.db)), 1)

    def test_settings_only_key_can_apply_timezone_but_not_feed(self):
        key = self.key(('mcp:read', 'mcp:settings'))
        draft = self.mcp(key, 'tools/call', dict(name='propose_timezone', arguments=dict(timezone_name='Europe/London'))).json['result']['structuredContent']
        response = self.mcp(key, 'tools/call', dict(name='apply_draft', arguments=dict(draft_id=draft['draft_id'])))
        self.assertFalse(response.json['result']['isError'])

    def test_mcp_bad_protocol_origin_revocation_and_disabled(self):
        key = self.key()
        headers = {'Authorization': 'Bearer ' + key, 'Accept':'application/json, text/event-stream'}
        client = self.app.test_client()
        self.assertEqual(client.post('/mcp', json={}, headers=dict(headers, Origin='https://evil.example')).status_code, 403)
        self.assertEqual(client.post('/mcp', json={}, headers=dict(headers, **{'MCP-Protocol-Version':'bad'})).status_code, 400)
        self.assertEqual(client.get('/mcp', headers=headers).status_code, 405)
        with closing(core.connect_db(self.db)) as conn:
            conn.execute('UPDATE assistant_config SET mcp_enabled=0'); conn.commit()
        self.assertEqual(self.mcp(key, 'ping').status_code, 404)
        with closing(core.connect_db(self.db)) as conn:
            conn.execute('UPDATE auth_api_keys SET revoked=1'); conn.commit()
        self.assertEqual(self.mcp(key, 'ping').status_code, 401)

    def test_stop_releases_busy_state_and_fences_late_worker_actions(self):
        self.activate()
        token=self.client.post('/api/assistant/conversations').json['id']
        started,release,returned=Event(),Event(),Event()
        def stalled(config,history,tools,system,on_delta=None):
            if history[-1]['content']=='Stall':
                started.set();release.wait(30);returned.set()
                return dict(role='assistant',content='',tool_calls=[dict(id='late',type='function',function=dict(name='propose_notification_action',arguments=json.dumps(dict(action='mark_all_read'))))])
            return dict(role='assistant',content='New reply.',tool_calls=[])
        self.seed_notifications(None,2)
        try:
            with patch.object(provider,'complete',side_effect=stalled):
                response=self.client.post(f'/api/assistant/conversations/{token}/messages',json=dict(message='Stall'),buffered=False)
                self.assertTrue(started.wait(2))
                response.close()
                self.assertTrue(self.client.get(f'/api/assistant/conversations/{token}').json['busy'])
                self.assertEqual(self.client.post(f'/api/assistant/conversations/{token}/stop').status_code,200)
                self.assertFalse(self.client.get(f'/api/assistant/conversations/{token}').json['busy'])
                result=self.client.post(f'/api/assistant/conversations/{token}/messages',json=dict(message='Continue'),buffered=True)
                self.assertIn(b'New reply.',result.data)
                release.set();self.assertTrue(returned.wait(2))
                for _ in range(100):
                    with closing(core.connect_db(self.db)) as conn:
                        ended=conn.execute("SELECT COUNT(*) FROM assistant_audit WHERE conversation=? AND kind='turn_error'",(token,)).fetchone()[0]
                    if ended:break
                    time.sleep(.01)
                self.assertTrue(ended)
                history=self.client.get(f'/api/assistant/conversations/{token}').json
                self.assertFalse(history['busy']);self.assertEqual(history['messages'][-1]['content'],'New reply.')
                self.assertEqual(core.count_unread_notifications(self.db),2)
                with closing(core.connect_db(self.db)) as conn:
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM assistant_drafts WHERE conversation=?',(token,)).fetchone()[0],0)
                self.assertEqual(self.client.post('/api/assistant/conversations/unknown/stop').status_code,404)
        finally:release.set()

    def test_denied_proposal_cannot_be_applied_and_has_one_summary(self):
        self.activate();self.seed_notifications(None,4)
        token=self.client.post('/api/assistant/conversations').json['id']
        reply=dict(role='assistant',content='I can do that.',tool_calls=[dict(id='notify',type='function',function=dict(name='propose_notification_action',arguments=json.dumps(dict(action='mark_all_read'))))])
        with patch.object(provider,'complete',return_value=reply) as complete:
            self.client.post(f'/api/assistant/conversations/{token}/messages',json=dict(message='Mark all read'),buffered=True)
            self.assertEqual(complete.call_count,1)
        history=self.client.get(f'/api/assistant/conversations/{token}').json['messages']
        self.assertEqual(len([m for m in history if m['role']=='assistant']),1)
        self.assertEqual(history[-1]['content'],'')
        draft=history[-1]['cards'][0]['data']['draft_id']
        self.assertEqual(self.client.post(f'/api/assistant/conversations/{token}/deny/{draft}').status_code,200)
        self.assertEqual(self.client.post(f'/api/assistant/conversations/{token}/apply/{draft}').status_code,409)
        self.assertEqual(core.count_unread_notifications(self.db),4)
        self.assertEqual(self.client.post(f'/api/assistant/conversations/unknown/deny/{draft}').status_code,404)
        with closing(core.connect_db(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM assistant_audit WHERE kind='proposal_denied'").fetchone()[0],1)

    def test_yes_applies_the_single_pending_notification_proposal(self):
        self.activate()
        feed=self.create_feed();self.seed_notifications(feed.id,112)
        token=self.client.post('/api/assistant/conversations').json['id']
        replies=[dict(role='assistant',content='',tool_calls=[dict(id='notify',type='function',function=dict(name='propose_notification_action',arguments=json.dumps(dict(action='mark_all_read'))))]),dict(role='assistant',content='Would you like me to proceed with this action?',tool_calls=[])]
        with patch.object(provider,'complete',side_effect=replies):
            response=self.client.post(f'/api/assistant/conversations/{token}/messages',json=dict(message='Mark all notifications read'),buffered=True)
        self.assertEqual(core.count_unread_notifications(self.db),112)
        with patch.object(provider,'complete') as complete:
            response=self.client.post(f'/api/assistant/conversations/{token}/messages',json=dict(message='Yes!'),buffered=True)
            complete.assert_not_called()
        self.assertIn(b'Marked read: 112 notifications.',response.data)
        self.assertIn(b'draft_id',response.data)
        self.assertEqual(core.count_unread_notifications(self.db),0)
        with closing(core.connect_db(self.db)) as conn:
            source=json.loads(conn.execute("SELECT details FROM assistant_audit WHERE kind='approval' ORDER BY id DESC LIMIT 1").fetchone()[0])['approval_source']
        self.assertEqual(source,'chat_confirmation')

    def test_ambiguous_confirmation_and_unrelated_yes_do_not_apply(self):
        feed=self.create_feed();self.seed_notifications(feed.id,2)
        access=Access('user:1',chat=True,conversation='approval-test')
        service=Services(self.db,access)
        for action in ('mark_all_read','mark_all_read'):
            # Two separate pending review choices, without relying on model output.
            service.call('propose_notification_action',dict(action=action))
        history=[dict(role='assistant',content='Would you like me to proceed?'),dict(role='user',content='yes')]
        with patch.object(provider,'complete') as complete:
            ai.run_turn(self.db,access,dict(name='Fixture',model='fixture'),history,{},lambda *args:None);complete.assert_not_called()
        self.assertIn('More than one',history[-1]['content'])
        self.assertEqual(core.count_unread_notifications(self.db),2)
        history=[dict(role='assistant',content='Would you like help finding settings?'),dict(role='user',content='yes')]
        with patch.object(provider,'complete',return_value=dict(role='assistant',content='Open Settings.',tool_calls=[])) as complete:
            ai.run_turn(self.db,access,dict(name='Fixture',model='fixture'),history,{},lambda *args:None);complete.assert_called_once()
        self.assertEqual(core.count_unread_notifications(self.db),2)

    def test_explicit_refresh_runs_without_proposal_and_only_once_per_turn(self):
        self.activate()
        feed=self.create_feed()
        token=self.client.post('/api/assistant/conversations').json['id']
        def reply(config,history,tools,system,on_delta=None):
            names={t['name'] for t in tools}
            self.assertIn('refresh_feed',names);self.assertNotIn('propose_refresh',names)
            if history[-1]['role']=='user':
                calls=[dict(id='refresh-'+str(i),type='function',function=dict(name='refresh_feed',arguments=json.dumps(dict(feed_id=feed.id)))) for i in range(2)]
                return dict(role='assistant',content='',tool_calls=calls)
            return dict(role='assistant',content='Feed refreshed.',tool_calls=[])
        with patch.object(provider,'complete',side_effect=reply), patch.object(core,'refresh_profile',wraps=core.refresh_profile) as refresh:
            response=self.client.post(f'/api/assistant/conversations/{token}/messages',json=dict(message='Can you refresh this feed?',path=f'/profiles/{feed.id}'),buffered=True)
            self.assertEqual(response.status_code,200);self.assertEqual(refresh.call_count,1)
            self.assertNotIn(b'"kind": "draft"',response.data)
            self.assertEqual(response.data.count(b'"kind": "result"'),0)
        visible=self.client.get(f'/api/assistant/conversations/{token}').json['messages']
        self.assertFalse(any(m['role']=='assistant' and m['content']=='Feed refreshed.' for m in visible))
        self.assertEqual(sum('Refreshed 1 feed.' in m['content'] for m in visible if m['role']=='assistant'),1)
        self.assertEqual(core.get_profile_by_id(self.db,feed.id).item_count,3)
        with closing(core.connect_db(self.db)) as conn:
            sources=[json.loads(r['details']).get('approval_source') for r in conn.execute("SELECT details FROM assistant_audit WHERE kind='approval'")]
        self.assertIn('explicit_refresh_request',sources)

    def test_refresh_authorization_requires_an_explicit_request(self):
        for text in ['refresh this feed','Can you refresh this feed?','please refresh feed 2',"refresh this feed but don't change its filters"]:
            self.assertTrue(ai.explicit_refresh_request(text),text)
        for text in ['How do I refresh a feed?',"Don't refresh this feed",'Are there new items?', 'Can you explain how to refresh?']:
            self.assertFalse(ai.explicit_refresh_request(text),text)
        self.assertNotIn('refresh_feed',{t['name'] for t in definitions(self.access)})
        self.assertIn('refresh_feed',{t['name'] for t in definitions(Access('key:1',('feeds:refresh',)))})
        self.assertNotIn('refresh_feed',{t['name'] for t in definitions(Access('key:1',('mcp:read',)))})
        with self.assertRaises(ValueError):self.services.call('refresh_feed',dict(feed_id=1))

    def test_chat_image_request_over_64kb_is_accepted_and_invalid_images_are_not_saved(self):
        self.activate()
        token = self.client.post('/api/assistant/conversations').json['id']
        image = dict(name='Screenshot.png', data_url='data:image/png;base64,' + base64.b64encode(b'\x89PNG\r\n\x1a\n' + b'x' * 100000).decode())
        with patch.object(provider,'complete',return_value=dict(role='assistant',content='Image received.',tool_calls=[])):
            response=self.client.post(f'/api/assistant/conversations/{token}/messages',json=dict(message='Explain the image',images=[image]),buffered=True)
        self.assertEqual(response.status_code,200)
        before=self.client.get(f'/api/assistant/conversations/{token}').json['messages']
        oversized=dict(image,data_url='data:image/png;base64,' + base64.b64encode(b'\x89PNG\r\n\x1a\n' + b'x' * (2*1024*1024)).decode())
        response=self.client.post(f'/api/assistant/conversations/{token}/messages',json=dict(message='Rejected',images=[oversized]))
        self.assertEqual(response.status_code,400)
        self.assertEqual(self.client.get(f'/api/assistant/conversations/{token}').json['messages'],before)
        response=self.client.post(f'/api/assistant/conversations/{token}/messages',data=b'x'*(12*1024*1024+1),content_type='application/json')
        self.assertEqual(response.status_code,413)
        self.assertEqual(self.client.get(f'/api/assistant/conversations/{token}').json['messages'],before)
        self.assertEqual(self.client.post('/api/assistant/conversations',json=dict(message='x'*70000)).status_code,413)

    def test_image_only_chat_persists_images_without_copying_data_to_audit(self):
        self.activate()
        token = self.client.post('/api/assistant/conversations').json['id']
        image = dict(name='Screenshot.png', data_url='data:image/png;base64,' + base64.b64encode(b'\x89PNG\r\n\x1a\nfixture').decode())
        with patch.object(provider, 'complete', return_value=dict(role='assistant', content='Image received.', tool_calls=[])) as complete:
            response = self.client.post(f'/api/assistant/conversations/{token}/messages', json=dict(message='', images=[image]), buffered=True)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(complete.call_args.args[1][0]['_images'][0]['data_url'], image['data_url'])
        messages = self.client.get(f'/api/assistant/conversations/{token}').json['messages']
        self.assertEqual(messages[0]['images'][0]['name'], image['name'])
        with closing(core.connect_db(self.db)) as conn:
            events = conn.execute('SELECT details FROM assistant_audit').fetchall()
        self.assertNotIn(image['data_url'], '\n'.join(row['details'] for row in events))
        for images in [[dict(data_url='data:image/svg+xml;base64,PHN2Zz4=')], [dict(data_url='data:image/png;base64,YmFk')], [image] * 5]:
            response = self.client.post(f'/api/assistant/conversations/{token}/messages', json=dict(message='Look', images=images))
            self.assertEqual(response.status_code, 400)

    def test_chat_stream_and_durable_history(self):
        self.activate()
        token = self.client.post('/api/assistant/conversations').json['id']
        outputs = [dict(role='assistant', content='', tool_calls=[dict(id='c1', type='function', function=dict(name='propose_feed_change', arguments=json.dumps(dict(config=CONFIG))))]),
                   dict(role='assistant', content='Review this proposal.', tool_calls=[])]
        with patch.object(provider, 'complete', side_effect=outputs):
            response = self.client.post(f'/api/assistant/conversations/{token}/messages', json=dict(message='Create a releases feed'), buffered=True)
            self.assertIn(b'event: card', response.data)
            self.assertIn(b'event: done', response.data)
        messages = self.client.get(f'/api/assistant/conversations/{token}').json['messages']
        self.assertTrue(any(m['cards'] for m in messages))
        self.assertEqual(core.list_profiles(self.db), [])
        with patch.object(provider, 'complete') as complete:
            response = self.client.post(f'/api/assistant/conversations/{token}/messages', json=dict(message='create it'), buffered=True)
            self.assertIn(b'Feed created', response.data)
            complete.assert_not_called()
        self.assertEqual(len(core.list_profiles(self.db)), 1)
        self.assertEqual(self.client.delete(f'/api/assistant/conversations/{token}').status_code, 200)
        self.assertEqual(self.client.get(f'/api/assistant/conversations/{token}').status_code, 404)

    def test_model_cannot_apply_and_safe_browser_only_saved_items(self):
        names = {t['name'] for t in definitions(self.access)}
        self.assertNotIn('apply_draft', names)
        self.assertIn('open_safe_browser', names)
        with self.assertRaises(ValueError):
            self.services.call('apply_draft', dict(draft_id='anything'))
        feed = self.create_feed()
        with self.assertRaises(ValueError):
            self.services.call('open_safe_browser', dict(feed_id=feed.id, item_id=123))

    def test_expired_and_wrong_principal_drafts_rejected(self):
        draft = self.services.call('propose_feed_change', dict(config=CONFIG))
        with self.assertRaises(ValueError):
            Services(self.db, Access('user:2')).apply(draft['draft_id'])
        with closing(core.connect_db(self.db)) as conn:
            conn.execute('UPDATE assistant_drafts SET expires=0'); conn.commit()
        with self.assertRaisesRegex(ValueError, 'expired'):
            self.services.apply(draft['draft_id'])

    def test_voice_uses_separate_provider_and_records_no_audio(self):
        config = self.activate()
        token = self.client.post('/api/assistant/conversations').json['id']
        self.assertEqual(self.client.post('/api/assistant/transcribe').status_code, 409)
        existing = ai.connection(self.db, secrets_visible=True)
        config.update(speech_url='http://speech.local/v1', speech_model='whisper', speech_key='speech-secret')
        ai.save_connection(self.db, config, existing, tested=True)
        with patch.object(provider, 'transcribe', return_value='Find Linux topics') as call:
            response = self.client.post('/api/assistant/transcribe', data={'audio': (io.BytesIO(b'fixture audio'), 'recording.m4a'), 'conversation':token})
            with closing(core.connect_db(self.db)) as conn:
                self.assertEqual(conn.execute("SELECT conversation FROM assistant_audit WHERE kind='transcription' ORDER BY id DESC LIMIT 1").fetchone()[0],token)
            denied=self.client.post('/api/assistant/transcribe',data={'audio':(io.BytesIO(b'fixture audio'),'recording.m4a'),'conversation':'another-users-chat'})
            self.assertEqual(denied.status_code,404);self.assertEqual(call.call_count,1)
        self.assertEqual(response.json['text'], 'Find Linux topics')
        self.assertEqual(call.call_args.args[0]['base_url'], 'http://speech.local/v1')
        self.assertEqual(call.call_args.args[0]['api_key'], 'speech-secret')
        self.assertEqual(call.call_args.args[2], 'recording.m4a')
        self.assertEqual(self.client.get(f'/api/assistant/conversations/{token}').json['messages'], [])

    def test_busy_conversation_and_stale_pending_proposals(self):
        self.activate()
        token = self.client.post('/api/assistant/conversations').json['id']
        with closing(core.connect_db(self.db)) as conn:
            conn.execute('UPDATE assistant_conversations SET busy_until=? WHERE id=?', (time.time() + 60, token)); conn.commit()
        self.assertEqual(self.client.post(f'/api/assistant/conversations/{token}/messages', json=dict(message='Hi')).status_code, 409)
        self.assertEqual(self.client.delete(f'/api/assistant/conversations/{token}').status_code, 409)

    def test_request_limits_and_mcp_permission_creation(self):
        response = self.client.post('/api/assistant/conversations', data='x' * 65537, content_type='application/json')
        self.assertEqual(response.status_code, 413)
        self.assertIn('too large', response.json['error'])
        response = self.client.post('/settings/api-keys', data=dict(key_label='Invalid MCP', current_password='test-owner-passphrase-only', scopes=['mcp:write']))
        self.assertEqual(response.status_code, 400)
        self.assertIn(b'also require MCP read', response.data)

    def test_creation_seeds_all_matches_and_edit_does_not_import_preview(self):
        html = ''.join(f'<article><a href="/{index}">Item {index}</a></article>' for index in range(8))
        with patch('rss_site_bridge.assistant_services.fetch_document', return_value=core.FetchedDocument(html, CONFIG['source_url'])):
            draft = self.services.call('propose_feed_change', dict(config=CONFIG))
            self.assertEqual(len(draft['payload']['preview']['items']), 3)
            self.assertNotIn('_initial_items', draft['payload'])
            result = self.services.apply(draft['draft_id'])
        with closing(core.connect_db(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM feed_items').fetchone()[0], 8)
        draft = self.services.call('propose_feed_change', dict(feed_id=result['feed_id'], config=dict(feed_title='Rename')))
        self.services.apply(draft['draft_id'])
        with closing(core.connect_db(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM feed_items').fetchone()[0], 8)


class ProviderTests(unittest.TestCase):
    def test_images_are_serialized_for_each_provider(self):
        image = dict(name='Screenshot', mime_type='image/png', data_url='data:image/png;base64,aW1hZ2U=')
        history = [dict(role='user', content='Inspect this', _images=[image])]
        for api_type in ['responses', 'compatible', 'anthropic', 'gemini']:
            result = dict(output=[], choices=[dict(message=dict(content='OK'))], content=[dict(type='text', text='OK')], candidates=[dict(content=dict(parts=[dict(text='OK')]))])
            with patch.object(provider, 'request_provider', return_value=result) as call:
                provider.complete(dict(api_type=api_type, model='fixture', max_tokens=2000), history, [], 'Help')
            payload = call.call_args.kwargs['json']
            if api_type == 'responses':
                self.assertEqual(payload['input'][0]['content'][1]['type'], 'input_image')
            elif api_type == 'compatible':
                self.assertEqual(payload['messages'][1]['content'][1]['image_url']['url'], image['data_url'])
            elif api_type == 'anthropic':
                self.assertEqual(payload['messages'][0]['content'][1]['source']['media_type'], 'image/png')
            else:
                self.assertEqual(payload['contents'][0]['parts'][1]['inlineData']['mimeType'], 'image/png')
        self.assertEqual(history[0]['content'], 'Inspect this')

    def test_usage_cost_categories_and_unknown_values(self):
        from rss_site_bridge.assistant_audit import usage
        config = dict(api_type='compatible', input_price=2, output_price=10, cache_price=1, context_window=10000)
        measured = usage(config, dict(prompt_tokens=1000, completion_tokens=100, prompt_tokens_details=dict(cached_tokens=200), completion_tokens_details=dict(reasoning_tokens=40)))
        self.assertAlmostEqual(measured['estimated_usd'], .0028)
        self.assertEqual(measured['reasoning_tokens'], 40)
        self.assertIsNone(usage(dict(api_type='compatible'), dict(prompt_tokens=1000, completion_tokens=100))['estimated_usd'])
        self.assertFalse(usage(config, {})['available'])
        measured = usage(dict(config, api_type='anthropic', cache_write_price=3), dict(input_tokens=800, cache_read_input_tokens=200, cache_creation_input_tokens=100, output_tokens=100))
        self.assertEqual(measured['input_tokens'], 1100)
        self.assertAlmostEqual(measured['estimated_usd'], .0031)

    def test_native_anthropic_preserves_tool_history_and_usage(self):
        config = dict(api_type='anthropic', model='fixture', max_tokens=2000)
        parts = [dict(type='thinking', thinking='private', signature='signed'), dict(type='tool_use', id='call1', name='get_feed', input=dict(feed_id=1))]
        with patch.object(provider, 'request_provider', return_value=dict(content=parts, usage=dict(input_tokens=10, output_tokens=5))):
            message = provider.complete(config, [dict(role='user', content='Read my feed')], [], 'Help')
        self.assertEqual(message['tool_calls'][0]['function']['name'], 'get_feed')
        history = [dict(role='user', content='Read my feed'), message, dict(role='tool', tool_call_id='call1', content='{}')]
        with patch.object(provider, 'request_provider', return_value=dict(content=[dict(type='text', text='Done')])) as call:
            provider.complete(config, history, [], 'Help')
        wire = call.call_args.kwargs['json']['messages']
        self.assertEqual(wire[1]['content'][0], parts[0])
        self.assertEqual(wire[-1]['content'][0]['tool_use_id'], 'call1')
        self.assertEqual(message['_usage']['input_tokens'], 10)

    def test_native_gemini_preserves_signature_and_function_response(self):
        config = dict(api_type='gemini', model='models/fixture', max_tokens=2000)
        parts = [dict(functionCall=dict(name='get_feed', args=dict(feed_id=1)), thoughtSignature='signed')]
        result = dict(candidates=[dict(content=dict(parts=parts))], usageMetadata=dict(promptTokenCount=100, candidatesTokenCount=10, thoughtsTokenCount=5))
        with patch.object(provider, 'request_provider', return_value=result):
            message = provider.complete(config, [dict(role='user', content='Read feed')], [], 'Help')
        history = [dict(role='user', content='Read feed'), message, dict(role='tool', tool_call_id=message['tool_calls'][0]['id'], content='{"feed_id":1}')]
        with patch.object(provider, 'request_provider', return_value=dict(candidates=[dict(content=dict(parts=[dict(text='Done')]))])) as call:
            provider.complete(config, history, [], 'Help')
        wire = call.call_args.kwargs['json']['contents']
        self.assertEqual(wire[1]['parts'][0]['thoughtSignature'], 'signed')
        self.assertEqual(wire[-1]['parts'][0]['functionResponse']['name'], 'get_feed')
        self.assertEqual(message['_usage']['output_tokens'], 15)

    def test_native_history_is_not_mutated_and_responses_convert_other_tools(self):
        history = [dict(role='user',content='Help'),dict(role='assistant',content='Earlier',_anthropic_content=[dict(type='text',text='Earlier')]),dict(role='assistant',content='Approved change'),dict(role='user',content='Continue')]
        original = json.dumps(history)
        with patch.object(provider,'request_provider',return_value=dict(content=[dict(type='text',text='Done')])):
            provider.complete(dict(api_type='anthropic',model='fixture',max_tokens=2000),history,[],'Help')
        self.assertEqual(json.dumps(history),original)
        history = [dict(role='user',content='Read feed'),dict(role='assistant',content='',tool_calls=[dict(id='a',type='function',function=dict(name='get_feed',arguments='{"feed_id":1}'))]),dict(role='tool',tool_call_id='a',content='{}')]
        with patch.object(provider,'request_provider',return_value=dict(output=[])) as call:
            provider.complete(dict(api_type='responses',model='fixture',max_tokens=2000),history,[],'Help')
        wire=call.call_args.kwargs['json']['input']
        self.assertEqual(wire[-2]['type'],'function_call'); self.assertEqual(wire[-1]['call_id'],'a')

    def test_gemini_stream_preserves_parts_usage_and_truncation(self):
        response=Mock()
        events=[dict(candidates=[dict(content=dict(parts=[dict(text='Hello',thoughtSignature='signed')]))]),dict(candidates=[dict(content=dict(parts=[dict(text=' world')]),finishReason='MAX_TOKENS')],usageMetadata=dict(promptTokenCount=20,candidatesTokenCount=10))]
        response.iter_lines.return_value=[b'data: '+json.dumps(event).encode() for event in events]
        chunks=[]
        result=provider.read_stream(response,'/models/fixture:streamGenerateContent?alt=sse',45,chunks.append)
        self.assertEqual(chunks,['Hello',' world'])
        self.assertEqual(result['candidates'][0]['content']['parts'][0]['thoughtSignature'],'signed')
        self.assertEqual(result['usageMetadata']['promptTokenCount'],20)
        with patch.object(provider,'request_provider',return_value=result):
            with self.assertRaisesRegex(ValueError,'token limit'): provider.complete(dict(api_type='gemini',model='fixture',max_tokens=2000),[dict(role='user',content='Hi')],[],'Help')

    def test_anthropic_stream_collects_usage_and_tool_fragments(self):
        response = Mock()
        events = [dict(type='message_start', message=dict(content=[], usage=dict(input_tokens=20))),
                  dict(type='content_block_start', index=0, content_block=dict(type='tool_use', id='a', name='get_feed', input={})),
                  dict(type='content_block_delta', index=0, delta=dict(type='input_json_delta', partial_json='{"feed_id":1}')),
                  dict(type='message_delta', delta=dict(stop_reason='tool_use'), usage=dict(output_tokens=6)), dict(type='message_stop')]
        response.iter_lines.return_value = [b'data: '+json.dumps(event).encode() for event in events]
        result = provider.read_stream(response, '/messages', 45, lambda text: None)
        self.assertEqual(result['content'][0]['input'], dict(feed_id=1))
        self.assertEqual(result['usage'], dict(input_tokens=20, output_tokens=6))

    def test_compatible_stream_assembles_fragmented_tool_arguments(self):
        events = [dict(choices=[dict(delta=dict(content='Checking '))]),
                  dict(choices=[dict(delta=dict(tool_calls=[dict(index=0, id='call', function=dict(name='get_feed', arguments='{"feed_'))]))]),
                  dict(choices=[dict(delta=dict(tool_calls=[dict(index=0, function=dict(arguments='id":1}'))]))])]
        response = Mock()
        response.iter_lines.return_value = [b'data: ' + json.dumps(event).encode() for event in events] + [b'data: [DONE]']
        text = []
        result = provider.read_stream(response, '/chat/completions', 45, text.append)
        message = result['choices'][0]['message']
        self.assertEqual(message['content'], 'Checking ')
        self.assertEqual(json.loads(message['tool_calls'][0]['function']['arguments']), dict(feed_id=1))
        self.assertEqual(text, ['Checking '])

    def test_responses_stream_keeps_final_reasoning_and_rejects_incomplete(self):
        response = Mock()
        response.iter_lines.return_value = [b'data: {"type":"response.output_text.delta","delta":"Hello"}',
            b'data: {"type":"response.completed","response":{"output":[]}}']
        text = []
        self.assertEqual(provider.read_stream(response, '/responses', 45, text.append), dict(output=[]))
        self.assertEqual(text, ['Hello'])
        response.iter_lines.return_value = [b'data: {"type":"response.incomplete"}']
        with self.assertRaises(ValueError):
            provider.read_stream(response, '/responses', 45, text.append)

    def test_responses_replays_outputs_and_disables_remote_storage(self):
        config = dict(api_type='responses', base_url='https://api.openai.com/v1', model='fixture', max_tokens=2000, timeout=45)
        output = [dict(type='function_call', name='get_feed', call_id='a', arguments='{"feed_id":1}'), dict(type='reasoning', encrypted_content='encrypted')]
        history = [dict(role='user', content='Read feed'), dict(role='assistant', content='', _response_output=output), dict(role='tool', tool_call_id='a', content='{}')]
        with patch.object(provider, 'request_provider', return_value=dict(output=[dict(type='message', content=[dict(type='output_text', text='Done')])])) as call:
            result = provider.complete(config, history, [], 'Help')
        self.assertEqual(result['content'], 'Done')
        payload = call.call_args.kwargs['json']
        self.assertFalse(payload['store'])
        self.assertIn(output[1], payload['input'])
        self.assertEqual(payload['input'][-1]['type'], 'function_call_output')

    def test_compatible_provider_and_tool_capability_test(self):
        config = dict(api_type='compatible', base_url='http://ollama:11434/v1', model='fixture', max_tokens=2000, timeout=45)
        with patch.object(provider, 'request_provider', return_value=dict(choices=[dict(message=dict(content='Hello'))])):
            with self.assertRaisesRegex(ValueError, 'tool support'):
                provider.test_connection(config)

    def test_private_dns_and_credentials_are_rejected(self):
        for url in ('file:///etc/passwd', 'http://user:pass@example.com', 'http://example.com:8080'):
            with self.assertRaises(ValueError):
                public_addresses(url)
        for ip in ('127.0.0.1', '10.0.0.1', '169.254.169.254', '::1', '::ffff:127.0.0.1'):
            with patch('socket.getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 80))]):
                with self.assertRaises(ValueError):
                    public_addresses('https://example.com')

    def test_fetch_pins_public_dns_and_rejects_redirect_to_private(self):
        response = Mock(status=302)
        response.getheader.side_effect = lambda key, *args: 'http://127.0.0.1/' if key == 'Location' else ''
        connection = Mock()
        connection.getresponse.return_value = response
        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 80))]
        private = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 80))]
        with patch('socket.getaddrinfo', side_effect=[public, private]), patch('socket.socket') as sock, patch('http.client.HTTPConnection', return_value=connection):
            with self.assertRaises(ValueError):
                fetch_public('http://example.com')
            sock.return_value.connect.assert_called_once_with(('93.184.216.34', 80))


if __name__ == '__main__':
    unittest.main()
