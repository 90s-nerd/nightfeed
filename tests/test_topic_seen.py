from auth_support import authenticated_client
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import sqlite3
import unittest
from bs4 import BeautifulSoup
from rss_site_bridge.app import create_app, create_profile, FeedRequest, FeedEntry, connect_db, init_db, refresh_profile


class TopicSeenTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'seen.db'
        self.app = create_app({'DATABASE_PATH': self.db, 'START_SCHEDULER': False, 'TESTING': True})
        self.client = authenticated_client(self.app)
        self.profile = create_profile(self.db, FeedRequest('News', 'https://example.com', 'article', 'a', 'a', '', 100, 0, 'http'))
        with closing(connect_db(self.db)) as conn:
            for i in range(40):
                conn.execute('INSERT INTO feed_items(profile_id,title,link,discovered_at) VALUES(?,?,?,?)',
                             (self.profile.id, f'Topic {i}', f'https://example.com/{i}', '2026-10-01T12:00:00+00:00'))
            conn.commit()

    def document(self, url='/'):
        return BeautifulSoup(self.client.get(url).data, 'html.parser')

    def mark(self, ids):
        token = self.document().select_one('meta[name=topic-csrf]')['content']
        return self.client.post('/api/topics/seen', json={'ids': ids}, headers={'X-CSRF-Token': token})

    def test_new_first_and_explicit_sorts(self):
        self.mark([40])
        self.assertEqual('39', self.document().select_one('[data-topic-id]')['data-topic-id'])
        self.assertEqual('40', self.document('/?sort=recent').select_one('[data-topic-id]')['data-topic-id'])
        self.assertEqual('1', self.document('/?sort=oldest').select_one('[data-topic-id]')['data-topic-id'])
        self.assertTrue(self.document().select_one('.topic-new'))

    def test_seen_pagination_keeps_order_and_new_arrivals_wait_for_next_visit(self):
        first = self.document()
        first_ids = [int(row['data-topic-id']) for row in first.select('[data-topic-id]')]
        next_url = first.select_one('.pagination-actions a')['href']
        self.mark(first_ids)
        with closing(connect_db(self.db)) as conn:
            conn.execute('INSERT INTO feed_items(profile_id,title,link,discovered_at) VALUES(?,?,?,?)',
                         (self.profile.id, 'Later', 'https://example.com/later', '2026-10-02T12:00:00+00:00'))
            conn.commit()
        second_ids = [int(row['data-topic-id']) for row in self.document(next_url).select('[data-topic-id]')]
        self.assertEqual(15, len(second_ids))
        self.assertFalse(set(first_ids) & set(second_ids))
        self.assertEqual(set(range(1, 41)), set(first_ids + second_ids))
        fresh = self.document()
        self.assertEqual('41', fresh.select_one('[data-topic-id]')['data-topic-id'])
        self.assertNotIn(40, [int(row['data-topic-id']) for row in fresh.select('[data-topic-id]')][:16])

    def test_api_validation_csrf_idempotence_and_updates(self):
        self.assertEqual(403, self.client.post('/api/topics/seen', json={'ids': [1]}).status_code)
        token = self.document().select_one('meta[name=topic-csrf]')['content']
        for ids in [[], [True], [-1], ['1'], [1] * 101, [2**64]]:
            self.assertEqual(400, self.client.post('/api/topics/seen', json={'ids': ids}, headers={'X-CSRF-Token': token}).status_code)
        self.assertEqual(403, self.client.post('/api/topics/seen', json={'ids': [1]}, headers={'X-CSRF-Token': token, 'Origin': 'https://other.example'}).status_code)
        self.assertEqual([1], self.mark([1, 9999]).json['seen'])
        with closing(connect_db(self.db)) as conn:
            seen = conn.execute('SELECT seen_at FROM feed_items WHERE id=1').fetchone()[0]
        self.mark([1])
        with patch('rss_site_bridge.app.extract_feed_entries', return_value=[FeedEntry('Updated', 'https://example.com/0', '', datetime.now(timezone.utc))]):
            refresh_profile(self.db, self.profile.id)
        with closing(connect_db(self.db)) as conn:
            self.assertEqual(seen, conn.execute('SELECT seen_at FROM feed_items WHERE id=1').fetchone()[0])

    def test_upgrade_marks_old_items_seen_only_once(self):
        legacy = Path(self.temp.name) / 'legacy.db'
        with closing(sqlite3.connect(legacy)) as conn:
            conn.execute('CREATE TABLE feed_items(id INTEGER PRIMARY KEY, profile_id INTEGER, title TEXT, link TEXT, summary TEXT, discovered_at TEXT)')
            conn.execute("INSERT INTO feed_items VALUES(1,1,'Old','https://example.com/old','','2026-01-01T00:00:00+00:00')")
            conn.commit()
        init_db(legacy)
        with closing(connect_db(legacy)) as conn:
            self.assertTrue(conn.execute('SELECT seen_at FROM feed_items WHERE id=1').fetchone()[0])
            conn.execute("INSERT INTO feed_items(id,profile_id,title,link,summary,discovered_at) VALUES(2,1,'New','https://example.com/new','','2026-01-01T00:00:00+00:00')")
            conn.commit()
        init_db(legacy)
        with closing(connect_db(legacy)) as conn:
            self.assertIsNone(conn.execute('SELECT seen_at FROM feed_items WHERE id=2').fetchone()[0])

    def test_explicit_safe_open_marks_seen_but_head_does_not(self):
        url = f'/profiles/{self.profile.id}/items/1/safe'
        with patch('rss_site_bridge.app.create_safe_browser_session', side_effect=RuntimeError('Browser unavailable')):
            self.client.head(url)
            with closing(connect_db(self.db)) as conn:
                self.assertIsNone(conn.execute('SELECT seen_at FROM feed_items WHERE id=1').fetchone()[0])
            self.client.get(url)
            with closing(connect_db(self.db)) as conn:
                self.assertTrue(conn.execute('SELECT seen_at FROM feed_items WHERE id=1').fetchone()[0])

    def test_saved_topics_persist_after_seen_and_refresh(self):
        token = self.document().select_one('meta[name=topic-csrf]')['content']
        url = '/api/topics/1/save'
        self.assertEqual(403, self.client.post(url, json={'saved': True}).status_code)
        headers = {'X-CSRF-Token': token}
        self.assertEqual(400, self.client.post(url, json={'saved': 'yes'}, headers=headers).status_code)
        self.assertEqual(404, self.client.post('/api/topics/999/save', json={'saved': True}, headers=headers).status_code)
        self.assertTrue(self.client.post(url, json={'saved': True}, headers=headers).json['saved'])
        self.mark([1])
        with patch('rss_site_bridge.app.extract_feed_entries', return_value=[FeedEntry('Updated', 'https://example.com/0', '', datetime.now(timezone.utc))]):
            refresh_profile(self.db, self.profile.id)
        saved = self.document('/?saved_only=1')
        self.assertEqual(['1'], [row['data-topic-id'] for row in saved.select('[data-topic-id]')])
        self.assertEqual('true', saved.select_one('[data-topic-save]')['aria-pressed'])
        self.assertEqual(0, len(self.document('/?saved_only=1&new_only=1').select('[data-topic-id]')))
        self.client.post(url, json={'saved': False}, headers=headers)
        self.assertFalse(self.document('/?saved_only=1').select('[data-topic-id]'))

    def test_new_only_pagination_is_stable_and_fresh_visits_hide_seen(self):
        self.mark([40])
        first = self.document('/?new_only=1')
        identities = [int(row['data-topic-id']) for row in first.select('[data-topic-id]')]
        self.mark(identities)
        next_url = first.select_one('.pagination-actions a')['href']
        self.assertIn('new_only=1', next_url)
        second = [int(row['data-topic-id']) for row in self.document(next_url).select('[data-topic-id]')]
        self.assertEqual(set(range(1,40)), set(identities + second))
        fresh = [int(row['data-topic-id']) for row in self.document('/?new_only=1').select('[data-topic-id]')]
        self.assertEqual(set(second), set(fresh))

    def test_updated_badge_summary_and_revision_safe_acknowledgements(self):
        self.mark([1])
        def refresh(title, summary):
            with patch('rss_site_bridge.app.extract_feed_entries', return_value=[FeedEntry(title, 'https://example.com/0', summary, datetime.now(timezone.utc))]):
                refresh_profile(self.db, self.profile.id)
        refresh('Changed title', 'Changed summary')
        doc = self.document('/?q=Changed')
        row = doc.select_one('[data-topic-id="1"]')
        self.assertIsNone(row.select_one('.topic-new'))
        self.assertEqual('UPDATED', row.select_one('.topic-updated').get_text())
        self.assertIn('Topic 0', row.select_one('.topic-changes').get_text())
        revision = row['data-topic-revision']
        refresh('Changed title', 'Changed summary')
        self.assertEqual(revision, self.document('/?q=Changed').select_one('[data-topic-id]')['data-topic-revision'])
        refresh('Changed again', 'Changed summary')
        token = doc.select_one('meta[name=topic-csrf]')['content']
        self.client.post('/api/topics/seen', json={'ids': [1], 'revisions': {'1': revision}}, headers={'X-CSRF-Token': token})
        current = self.document('/?q=Changed').select_one('[data-topic-id]')
        self.assertIsNotNone(current.select_one('.topic-updated'))
        self.assertIn('Topic 0', current.select_one('.topic-changes').get_text())
        self.client.post('/api/topics/seen', json={'ids': [1], 'revisions': {'1': current['data-topic-revision']}}, headers={'X-CSRF-Token': token})
        self.assertIsNone(self.document('/?q=Changed').select_one('.topic-updated'))
        self.assertIsNone(self.document(f'/profiles/{self.profile.id}').select_one('[data-topic-id="1"] .topic-updated'))

    def test_bulk_catch_up_excludes_later_arrivals_and_requires_tokens(self):
        doc = self.document()
        token = doc.select_one('meta[name=topic-csrf]')['content']
        browse = doc.select_one('[name=browse]')['value']
        with closing(connect_db(self.db)) as conn:
            conn.execute("INSERT INTO feed_items(profile_id,title,link,discovered_at) VALUES(?,?,?,?)", (self.profile.id, 'Later', 'https://example.com/later', '2026-10-02T12:00:00+00:00'))
            conn.commit()
        url = '/api/topics/seen-all'
        self.assertEqual(403, self.client.post(url, json={'browse': browse}).status_code)
        headers = {'X-CSRF-Token': token}
        self.assertEqual(403, self.client.post(url, json={'browse': 123}, headers=headers).status_code)
        self.assertEqual(40, self.client.post(url, json={'browse': browse}, headers=headers).json['seen'])
        self.assertEqual(['41'], [row['data-topic-id'] for row in self.document('/?new_only=1').select('[data-topic-id]')])
