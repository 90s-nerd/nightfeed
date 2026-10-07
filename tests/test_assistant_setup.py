"""Setup consent and proposal approval have different, durable referents."""
from contextlib import closing
import json
import unittest
from unittest.mock import patch

import test_assistant as fixtures
from rss_site_bridge import assistant as ai, assistant_provider as provider, app as core


class SetupConfirmationTests(unittest.TestCase):
    setUp=fixtures.AssistantTests.setUp
    seed_notifications=fixtures.AssistantTests.seed_notifications

    def test_setup_confirmations_continue_source_workflow_without_a_draft(self):
        for answer in ('Yes','Do it','Go ahead','create it'):
            with self.subTest(answer=answer):
                history=[dict(role='user',content='Can you setup a feed for '+fixtures.CONFIG['source_url']),
                         dict(role='assistant',content='Would you like me to proceed with that basic manual refresh feed setup now?'),
                         dict(role='user',content=answer)]
                call=dict(id='inspect',type='function',function=dict(name='inspect_source',arguments=json.dumps(dict(source_url=fixtures.CONFIG['source_url']))))
                replies=[dict(role='assistant',content='',tool_calls=[call]),
                         dict(role='assistant',content='I inspected the listing. Which title should I use for the feed?',tool_calls=[])]
                with patch.object(provider,'complete',side_effect=replies) as complete:
                    ai.run_turn(self.db,self.access,dict(name='Fixture',model='fixture'),history,{},lambda *args:None)
                    self.assertEqual(complete.call_count,2)
                    self.assertIn('answering a setup question',complete.call_args.args[3])
                self.assertIn('I inspected the listing',history[-1]['content'])
                self.assertNotIn('error',json.loads(next(m['content'] for m in history if m['role']=='tool')))
                self.assertEqual(core.list_profiles(self.db),[])

    def test_unpresented_or_older_proposal_does_not_steal_setup_confirmation(self):
        self.seed_notifications(None,2)
        pending=self.services.call('propose_notification_action',dict(action='mark_all_read'))
        for prefix in ([],[dict(role='assistant',content='Review this proposal.',_cards=[dict(kind='draft',data=pending)])]):
            history=[*prefix,dict(role='user',content='First help me set up a feed'),
                     dict(role='assistant',content='Would you like me to proceed with setup now?'),dict(role='user',content='yes')]
            with patch.object(provider,'complete',return_value=dict(role='assistant',content='Which source URL should I use?',tool_calls=[])) as complete:
                ai.run_turn(self.db,self.access,dict(name='Fixture',model='fixture'),history,{},lambda *args:None)
                complete.assert_called_once()
        with closing(core.connect_db(self.db)) as conn:
            self.assertIsNone(conn.execute('SELECT result FROM assistant_drafts WHERE id=?',(pending['draft_id'],)).fetchone()[0])

    def test_legacy_missing_proposal_reply_does_not_trap_do_it(self):
        history=[dict(role='user',content='Set up a feed'),dict(role='assistant',content='Would you like me to proceed with setup?'),
                 dict(role='user',content='yes'),dict(role='assistant',content='There is no active proposal. It may have expired or already been handled. Ask me to prepare the action again if needed.'),dict(role='user',content='Do it')]
        with patch.object(provider,'complete',return_value=dict(role='assistant',content='Let’s continue setup. What is the listing URL?',tool_calls=[])) as complete:
            ai.run_turn(self.db,self.access,dict(name='Fixture',model='fixture'),history,{},lambda *args:None)
            complete.assert_called_once()
        self.assertIn('continue setup',history[-1]['content'])

    def test_legacy_review_card_applies_only_its_presented_draft(self):
        self.seed_notifications(None,2)
        presented=self.services.call('propose_notification_action',dict(action='mark_all_read'))
        unrelated=self.services.call('propose_notification_action',dict(action='mark_all_read'))
        history=[dict(role='user',content='Mark notifications read'),
                 dict(role='assistant',content='',_card_only=True,_cards=[dict(kind='draft',data=presented)]),
                 dict(role='tool',content='{}'),dict(role='assistant',content='Please approve or deny this proposal.',_card_only=True),
                 dict(role='user',content='yes')]
        with patch.object(provider,'complete') as complete:
            ai.run_turn(self.db,self.access,dict(name='Fixture',model='fixture'),history,{},lambda *args:None)
            complete.assert_not_called()
        self.assertEqual(history[-1]['_cards'][0]['data']['draft_id'],presented['draft_id'])
        with closing(core.connect_db(self.db)) as conn:
            self.assertIsNone(conn.execute('SELECT result FROM assistant_drafts WHERE id=?',(unrelated['draft_id'],)).fetchone()[0])

    def test_expired_presented_proposal_is_not_silently_recreated(self):
        self.seed_notifications(None,2)
        draft=self.services.call('propose_notification_action',dict(action='mark_all_read'))
        with closing(core.connect_db(self.db)) as conn:
            conn.execute('UPDATE assistant_drafts SET expires=0 WHERE id=?',(draft['draft_id'],));conn.commit()
        history=[dict(role='assistant',content='Please approve or deny this proposal.',_proposal_ids=[draft['draft_id']]),dict(role='user',content='yes')]
        with patch.object(provider,'complete') as complete:
            ai.run_turn(self.db,self.access,dict(name='Fixture',model='fixture'),history,{},lambda *args:None)
            complete.assert_not_called()
        self.assertIn('no active proposal',history[-1]['content'])


if __name__=='__main__': unittest.main()
