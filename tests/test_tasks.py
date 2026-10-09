from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import time
import unittest

from auth_support import authenticated_client
from rss_site_bridge import app as core, tasks, assistant as ai
from rss_site_bridge.assistant_services import Access, Services
from rss_site_bridge.push_notifications import DEFAULTS

FEED=dict(feed_title='Movies',source_url='https://example.com/topics',item_selector='article',title_selector='a',link_selector='a',summary_selector='p',refresh_interval_minutes=60,max_items=100,fetch_mode='http')
WATCH=dict(name='Watch for Spider-Man',terms=['spider man'],feed_ids=[],mode='every',channels=['nightfeed'])


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.db=Path(self.tmp.name)/'tasks.db'
        self.app=core.create_app(dict(TESTING=True,DATABASE_PATH=self.db,START_SCHEDULER=False));self.client=authenticated_client(self.app)
        self.access=Access('user:1',chat=True);self.feed=core.create_profile(self.db,core.FeedRequest(**FEED))

    def save(self,**changes):
        result=tasks.save(self.db,self.access,dict(WATCH,**changes));return result['task_id']

    def refresh(self,html,feed=None):
        document=core.FetchedDocument(html,FEED['source_url']);return core.refresh_profile(self.db,(feed or self.feed).id,document=document)

    def alerts(self):
        with closing(core.connect_db(self.db)) as conn:return conn.execute("SELECT * FROM notifications WHERE event_type='topic_match'").fetchall()

    def test_variants_boundaries_refinements_and_exact_mode(self):
        config=tasks.normalize(self.db,dict(WATCH,required_terms=['Tamil'],exclude_terms=['trailer']),self.access)
        for title in ['Spider-Man (2026) Tamil','SPIDERMAN Tamil','Spider Man Tamil']:self.assertTrue(tasks.matches(config,title))
        for title in ['Spider Man Hindi','Spider Man Tamil trailer','Spidermansion Tamil']:self.assertFalse(tasks.matches(config,title))
        config['match_mode']='exact';self.assertFalse(tasks.matches(config,'Spider-Man Tamil'));self.assertTrue(tasks.matches(config,'Spider Man Tamil'))

    def test_existing_preview_does_not_alert_and_refresh_deduplicates_across_feeds(self):
        html='<article><a href="/old">Spider-Man old</a></article>'
        self.refresh(html);identity=self.save()
        self.assertEqual(tasks.preview(self.db,self.access,WATCH)['total_count'],1);self.assertEqual(len(self.alerts()),0)
        self.refresh(html+'<article><a href="/new">Spiderman new</a></article>');self.refresh(html)
        second=core.create_profile(self.db,core.FeedRequest(**dict(FEED,feed_title='Other')))
        self.refresh('<article><a href="/new">Spider Man new</a></article>',second)
        self.assertEqual(len(self.alerts()),1);self.assertEqual(tasks.get_task(self.db,self.access,identity)['match_count'],1)

    def test_once_groups_first_refresh_and_completes(self):
        identity=self.save(mode='once')
        self.refresh('<article><a href="/a">Spider Man one</a></article><article><a href="/b">Spider-Man two</a></article>')
        self.refresh('<article><a href="/c">Spider Man three</a></article>')
        task=tasks.get_task(self.db,self.access,identity);self.assertEqual(task['state'],'completed');self.assertEqual(task['match_count'],2);self.assertEqual(len(self.alerts()),1)

    def test_expiry_archives_even_without_a_refresh_and_reactivation_requires_new_expiry(self):
        identity=self.save(expires_at=time.time()+30)
        with closing(core.connect_db(self.db)) as conn:conn.execute('UPDATE topic_tasks SET expires=? WHERE id=?',(time.time()-1,identity));conn.commit()
        tasks.dispatch(self.db)
        task=tasks.get_task(self.db,self.access,identity);self.assertEqual(task['state'],'archived');self.assertEqual(task['reason'],'expired');self.assertEqual(task['match_count'],0)
        with self.assertRaises(ValueError):tasks.change_state(self.db,self.access,identity,'resume')
        config={k:v for k,v in task['config'].items() if k in WATCH or k in ('fields','match_mode','required_terms','exclude_terms','expires_at')};config['expires_at']=None
        tasks.save(self.db,self.access,config,identity,task['revision']);tasks.change_state(self.db,self.access,identity,'resume')
        self.assertEqual(tasks.get_task(self.db,self.access,identity)['state'],'active')

    def test_scope_pause_edit_and_owner_permissions(self):
        second=core.create_profile(self.db,core.FeedRequest(**dict(FEED,feed_title='Other')));identity=self.save(feed_ids=[self.feed.id])
        self.refresh('<article><a href="/a">Spider Man</a></article>',second);self.assertEqual(len(self.alerts()),0)
        tasks.change_state(self.db,self.access,identity,'pause');self.refresh('<article><a href="/b">Spider Man</a></article>');self.assertEqual(len(self.alerts()),0)
        tasks.change_state(self.db,self.access,identity,'resume');self.refresh('<article><a href="/c">Spider Man</a></article>');self.assertEqual(len(self.alerts()),1)
        with self.assertRaises(ValueError):tasks.get_task(self.db,Access('user:2'),identity)
        self.assertEqual(tasks.list_tasks(self.db,Access('user:1',feed_ids=(second.id,))),[])
        with self.assertRaises(ValueError):tasks.save(self.db,Access('user:1',feed_ids=(self.feed.id,)),WATCH)
        task=tasks.get_task(self.db,self.access,identity)
        with self.assertRaises(ValueError):tasks.save(self.db,self.access,dict(WATCH,feed_ids=[self.feed.id]),identity,task['revision']-1)

    def test_api_persists_setup_completion_without_duplicates_and_validates_security(self):
        ai.save_connection(self.db,dict(name='fixture',api_type='compatible',base_url='https://example.com/v1',model='fixture',api_key='fixture',speech_key='',speech_url='',speech_model='',timeout=45,max_tokens=1000),None,tested=True)
        token=self.client.post('/api/assistant/conversations').json['id']
        reply=dict(role='assistant',content='',tool_calls=[dict(id='watch',type='function',function=dict(name='prepare_topic_watch',arguments=json.dumps(dict(topic='Spider Man'))))])
        with patch('rss_site_bridge.assistant_provider.complete',return_value=reply):self.client.post(f'/api/assistant/conversations/{token}/messages',json=dict(message='Notify me for Spider Man'),buffered=True)
        question=self.client.get(f'/api/assistant/conversations/{token}').json['messages'][-1]
        self.assertTrue(question['choices']);self.assertEqual(question['cards'],[])
        # Preserve the authenticated legacy setup API contract for upgraded histories.
        setup=Services(self.db,self.access).call('prepare_topic_watch',dict(topic='Spider Man'))
        with closing(core.connect_db(self.db)) as conn:
            history=json.loads(conn.execute('SELECT history FROM assistant_conversations WHERE id=?',(token,)).fetchone()[0])
            history.append(dict(role='assistant',content='',_cards=[dict(kind='task_setup',data=setup)]))
            conn.execute('UPDATE assistant_conversations SET history=? WHERE id=?',(json.dumps(history),token));conn.commit()
        payload=dict(config=WATCH,conversation=token,setup_id=setup['setup_id'])
        first=self.client.post('/api/tasks',json=payload);second=self.client.post('/api/tasks',json=payload)
        self.assertEqual(first.status_code,200);self.assertEqual(first.json['task_id'],second.json['task_id']);self.assertEqual(len(tasks.list_tasks(self.db,self.access)),1)
        history=self.client.get(f'/api/assistant/conversations/{token}').json['messages']
        self.assertEqual(sum(c['kind']=='result' for m in history for c in m['cards']),1)
        self.assertEqual(self.client.get('/api/tasks/'+first.json['task_id']).status_code,200)
        self.assertEqual(self.app.test_client().post('/api/tasks',json=dict(config=WATCH)).status_code,401)
        self.assertEqual(self.client.post('/api/tasks',json=[]).status_code,400)

    def test_mcp_proposals_share_task_service_and_require_approval(self):
        service=Services(self.db,self.access);draft=service.call('propose_task',dict(config=WATCH));self.assertEqual(tasks.list_tasks(self.db,self.access),[])
        result=service.apply(draft['draft_id']);self.assertEqual(len(tasks.list_tasks(self.db,self.access)),1)
        task=tasks.get_task(self.db,self.access,result['task_id'])
        state=service.call('propose_task_state',dict(task_id=task['id'],action='pause'));service.apply(state['draft_id']);self.assertEqual(tasks.get_task(self.db,self.access,task['id'])['state'],'paused')

    def test_http_mcp_tasks_inherit_owner_access(self):
        from oauth_support import connect_client
        raw=connect_client(self.app,self.client,self.db)
        def call(name,arguments):
            response=self.app.test_client().post('/mcp',base_url='https://localhost',json=dict(jsonrpc='2.0',id=1,method='tools/call',params=dict(name=name,arguments=arguments)),headers={'Authorization':'Bearer '+raw,'Accept':'application/json, text/event-stream'})
            self.assertEqual(response.status_code,200);return response.json['result']
        self.assertFalse(call('propose_task',dict(config=WATCH))['isError'])
        draft=call('propose_task',dict(config=dict(WATCH,feed_ids=[self.feed.id])))['structuredContent']
        self.assertEqual(tasks.list_tasks(self.db,self.access),[])
        applied=call('apply_draft',dict(draft_id=draft['draft_id']));self.assertFalse(applied['isError'])
        self.assertEqual(tasks.list_tasks(self.db,self.access)[0]['id'],applied['structuredContent']['task_id'])
        self.assertEqual(len(call('list_tasks',{})['structuredContent']['tasks']),1)

    def configure_delivery(self):
        with closing(core.connect_db(self.db)) as conn:
            conn.execute("UPDATE app_settings SET smtp_enabled=1,smtp_host='smtp.example.com',smtp_port=587,smtp_username='test',smtp_password='test',smtp_from_email='sender@example.com',smtp_to_email='owner@example.com'")
            conn.execute('INSERT INTO push_devices(id,secret_hash,endpoint_hash,subscription,preferences,user_id) VALUES(?,?,?,?,?,?)',('device','secret','endpoint','{}',json.dumps(DEFAULTS),1));conn.commit()
        self.access.device_id='device'

    def test_multi_delivery_retries_only_failed_channel_even_after_once_completed(self):
        self.configure_delivery();identity=self.save(mode='once',channels=['nightfeed','push','email']);self.refresh('<article><a href="/a">Spider Man</a></article>')
        with patch('rss_site_bridge.push_notifications.deliver') as push,patch.object(core,'send_smtp_message',side_effect=[OSError('offline'),None]) as email:
            tasks.dispatch(self.db);task=tasks.get_task(self.db,self.access,identity);self.assertEqual(task['state'],'completed');self.assertEqual(task['delivery_issues'],1)
            tasks.dispatch(self.db,now=time.time()+120);self.assertEqual(push.call_count,1);self.assertEqual(email.call_count,2)
            self.assertTrue(all(d['state']=='sent' for d in tasks.get_task(self.db,self.access,identity)['deliveries']))

    def test_push_setup_and_edit_preview_work_without_current_device_token(self):
        self.configure_delivery();identity=self.save(channels=['push'])
        other_device=Access('user:1',chat=True)
        config=dict(WATCH,channels=['push'])
        self.assertEqual(tasks.preview(self.db,other_device,config,identity)['total_count'],0)
        self.assertEqual(tasks.preview(self.db,other_device,config)['total_count'],0)
        with self.assertRaises(ValueError):tasks.preview(self.db,Access('user:2',chat=True),config,identity)
        self.assertFalse(tasks.options(self.db,'device','user:2')['push_available'])

    def test_push_targets_all_owner_devices_and_includes_later_registrations(self):
        self.configure_delivery()
        def add(identity,user=1,enabled=1):
            with closing(core.connect_db(self.db)) as conn:
                conn.execute('INSERT INTO push_devices(id,secret_hash,endpoint_hash,subscription,preferences,user_id,enabled) VALUES(?,?,?,?,?,?,?)',(identity,identity,identity,'{}',json.dumps(DEFAULTS),user,enabled));conn.commit()
        add('second');add('disabled',enabled=0);add('foreign',user=2)
        self.assertEqual(tasks.options(self.db,None,'user:1')['push_device_count'],2)
        identity=self.save(channels=['push']);self.refresh('<article><a href="/a">Spider Man</a></article>')
        def delivery(db,device,payload):
            if device['id']=='device':raise OSError('offline')
        with patch('rss_site_bridge.push_notifications.deliver',side_effect=delivery) as push:
            tasks.dispatch(self.db)
            self.assertEqual({call.args[1]['id'] for call in push.call_args_list},{'device','second'})
            states={d['device_id']:d['state'] for d in tasks.get_task(self.db,self.access,identity)['deliveries'] if d['channel']=='push'}
            self.assertEqual(states,dict(device='retry',second='sent'))
            push.side_effect=None;tasks.dispatch(self.db,now=time.time()+120)
            self.assertEqual(push.call_count,3)
            self.assertEqual(push.call_args.args[1]['id'],'device')
        add('later');self.refresh('<article><a href="/b">Spider-Man new movie</a></article>')
        with patch('rss_site_bridge.push_notifications.deliver') as push:
            tasks.dispatch(self.db)
            self.assertEqual({call.args[1]['id'] for call in push.call_args_list},{'device','second','later'})

    def test_unavailable_channels_and_timezone_validation(self):
        for channel in ('push','email'):
            with self.assertRaises(ValueError):self.save(channels=[channel])
        with closing(core.connect_db(self.db)) as conn:conn.execute("UPDATE app_settings SET timezone_name='America/Chicago'");conn.commit()
        config=tasks.normalize(self.db,dict(WATCH,expires_at='2030-01-01T18:00'),self.access)
        self.assertEqual(datetime.fromtimestamp(config['expires_at'],timezone.utc).hour,0)
        with self.assertRaises(ValueError):self.save(expires_at='2030-03-10T02:30')

    def test_push_waits_for_device_lease_quiet_hours_and_shared_daily_limit(self):
        self.configure_delivery();identity=self.save(channels=['push'])
        self.refresh('<article><a href="/a">Spider Man</a></article>')
        now=time.time();day=datetime.fromtimestamp(now,timezone.utc).date().isoformat()
        def update(**values):
            with closing(core.connect_db(self.db)) as conn:
                conn.execute('UPDATE push_devices SET '+','.join(k+'=?' for k in values)+' WHERE id=?',[*values.values(),'device']);conn.commit()
                conn.execute("UPDATE task_deliveries SET due=? WHERE state='pending'",(now,));conn.commit()
        with patch('rss_site_bridge.push_notifications.deliver') as delivery:
            update(lease_until=now+60);tasks.dispatch(self.db,now);delivery.assert_not_called()
            update(lease_until=0,day=day,sent=12);tasks.dispatch(self.db,now);delivery.assert_not_called()
            update(sent=0,preferences=json.dumps(dict(DEFAULTS,quiet=True,quiet_start='00:00',quiet_end='00:00')))
            tasks.dispatch(self.db,now);delivery.assert_not_called()
            update(preferences=json.dumps(DEFAULTS));tasks.dispatch(self.db,now);self.assertEqual(delivery.call_count,1)
        self.assertTrue(all(d['state']=='sent' for d in tasks.get_task(self.db,self.access,identity)['deliveries']))
        with closing(core.connect_db(self.db)) as conn:self.assertEqual(conn.execute('SELECT lease_until FROM push_devices').fetchone()[0],0)


from datetime import datetime, timezone
if __name__=='__main__':unittest.main()
