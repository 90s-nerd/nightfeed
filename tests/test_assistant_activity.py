"""Observable tool activity persists separately from the final answer."""
import json
import time
from contextlib import closing
import unittest
from unittest.mock import patch

import test_assistant as fixtures
from rss_site_bridge import assistant as ai, assistant_provider as provider
from rss_site_bridge import app as core


class ActivityTests(unittest.TestCase):
    setUp = fixtures.AssistantTests.setUp
    activate = fixtures.AssistantTests.activate

    def test_stop_preserves_completed_steps_and_marks_running_step_stopped(self):
        self.activate()
        token = self.client.post('/api/assistant/conversations').json['id']
        history = [dict(role='user',content='Search',_activity=[
            dict(id='0',label='Inspect source',status='complete',detail='Completed.'),
            dict(id='1',label='Search topics',status='running',detail='')])]
        with closing(core.connect_db(self.db)) as conn:
            conn.execute('UPDATE assistant_conversations SET history=?,busy_until=? WHERE id=?',(json.dumps(history),time.time()+900,token))
            conn.commit()
        self.assertEqual(self.client.post(f'/api/assistant/conversations/{token}/stop').status_code,200)
        result = self.client.get(f'/api/assistant/conversations/{token}').json
        self.assertEqual([step['status'] for step in result['messages'][0]['activity']],['complete','stopped'])
        self.assertFalse(result['busy'])

    def test_narration_and_safe_outcomes_survive_reload(self):
        history = [dict(role='user', content='Find something interesting in my stored content')]
        events = []
        replies = [dict(role='assistant', content='I’ll check your stored content.',
                        _gemini_parts=[dict(text='PRIVATE REASONING', thought=True)],
                        tool_calls=[dict(id='search', function=dict(name='search_topics', arguments='{"query":"example"}'))]),
                   dict(role='assistant', content='No matches found.', tool_calls=[])]
        result = dict(total_count=0, returned_count=0, items=[], filters=dict(query='example'),
                      untrusted_html='<script>SECRET</script>', smtp_password='SECRET')
        original = ai.Services.call
        def call(service, name, args):
            return result if name == 'search_topics' else original(service, name, args)
        with patch.object(provider, 'complete', side_effect=replies), patch.object(ai.Services, 'call', call):
            ai.run_turn(self.db, self.access, dict(name='Fixture', model='fixture'), history, {}, lambda event, value: events.append((event,value)))
        visible = ai.visible_history(json.loads(json.dumps(history)))
        self.assertEqual([m['content'] for m in visible if m['role']=='assistant'], ['No matches found.'])
        steps = visible[0]['activity']
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0]['detail'], 'I’ll check your stored content.')
        self.assertEqual(steps[1]['status'], 'complete')
        self.assertIn('Matching results: 0', steps[1]['detail'])
        self.assertNotIn('SECRET', json.dumps(visible))
        self.assertNotIn('PRIVATE REASONING', json.dumps(visible))
        without_activity = [{key:value for key,value in message.items() if key != '_activity'} for message in history]
        self.assertEqual(ai.text_context_size(history), ai.text_context_size(without_activity))
        self.assertEqual([value['status'] for event,value in events if event=='activity'], ['complete','running','complete'])

    def test_failed_step_is_not_reported_as_completed(self):
        history = [dict(role='user',content='Find something interesting')]
        replies = [dict(role='assistant',content='',tool_calls=[dict(id='bad',function=dict(name='unknown_tool',arguments='{}'))]),
                   dict(role='assistant',content='That operation is unavailable.',tool_calls=[])]
        with patch.object(provider,'complete',side_effect=replies):
            ai.run_turn(self.db,self.access,dict(name='Fixture',model='fixture'),history,{},lambda *args:None)
        self.assertEqual(history[0]['_activity'][0]['status'],'error')


if __name__ == '__main__': unittest.main()
