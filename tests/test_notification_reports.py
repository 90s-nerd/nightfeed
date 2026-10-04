from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from unittest.mock import patch, Mock
import json
import unittest
from bs4 import BeautifulSoup

from rss_site_bridge.app import (create_app, create_profile, FeedRequest, FeedEntry,
                                refresh_profile, list_notifications, create_notification,
                                connect_db, count_unread_notifications)


class NotificationReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'reports.db'
        self.app = create_app({'DATABASE_PATH': self.db, 'START_SCHEDULER': False, 'TESTING': True})
        self.client = self.app.test_client()
        self.profile = create_profile(self.db, FeedRequest('News', 'https://example.com', 'article', 'a', 'a', '', 25, 0, 'http'))

    def entry(self, title, summary='', link='https://example.com/one'):
        return FeedEntry(title, link, summary, datetime.now(timezone.utc))

    def refresh(self, entries):
        with patch('rss_site_bridge.app.extract_feed_entries', return_value=entries):
            refresh_profile(self.db, self.profile.id)
        return list_notifications(self.db)[0]

    def test_new_and_updated_entries_are_immutable_snapshots(self):
        first = self.refresh([self.entry('First title', 'First summary')])
        second = self.refresh([self.entry('Second title', 'Second summary'), self.entry('New story', link='https://example.com/two')])
        report = json.loads(second.metadata_json)
        self.assertEqual((1, 1), (report['new_items'], report['updated_items']))
        updated = next(item for item in report['changes'] if item['kind'] == 'updated')
        self.assertEqual('First title', updated['previous_title'])
        self.assertEqual('First summary', updated['previous_summary'])
        self.assertEqual('Second title', updated['title'])
        self.refresh([self.entry('Third title')])
        response = self.client.get(f'/notifications/{second.id}')
        self.assertEqual(200, response.status_code)
        self.assertIn(b'First title', response.data)
        self.assertIn(b'Second title', response.data)
        self.assertIn(b'New story', response.data)
        self.assertNotIn(b'Entry-level changes are unavailable', response.data)
        self.assertNotIn(b'Third title', response.data)
        self.assertIn(b'First title', self.client.get(f'/notifications/{first.id}').data)

    def test_up_to_date_refresh_has_empty_changes(self):
        self.refresh([self.entry('Same')])
        notification = self.refresh([self.entry('Same')])
        self.assertEqual([], json.loads(notification.metadata_json)['changes'])
        response = self.client.get(f'/notifications/{notification.id}')
        self.assertIn(b'No new entries', response.data)
        self.assertNotIn(b'older notification', response.data)

    def test_missing_snapshots_do_not_imply_notification_age_or_invent_entries(self):
        self.refresh([self.entry('Current feed entry')])
        notification = create_notification(self.db, profile_id=self.profile.id, event_type='refresh', severity='info', category='success',
                                           title='Refresh succeeded', message='Nightfeed saved 5 entries for this feed.', metadata={'entry_count': 5})
        response = self.client.get(f'/notifications/{notification.id}')
        self.assertIn(b'Entry-level changes are unavailable for this refresh.', response.data)
        self.assertNotIn(b'older notification', response.data)
        self.assertNotIn(b'Current feed entry', response.data)
        self.assertNotIn(b'No new entries', response.data)

    def test_safe_links_for_new_and_updated_entries_return_to_report(self):
        self.refresh([self.entry('Original')])
        notification = self.refresh([self.entry('Updated'), self.entry('New', link='https://example.com/two')])
        origin = f'/notifications/{notification.id}?status=all'
        soup = BeautifulSoup(self.client.get(origin).data, 'html.parser')
        links = soup.select('.report-entry .safe-link')
        self.assertEqual(2, len(links))
        session = Mock(id='report-session')
        for link in links:
            with patch('rss_site_bridge.app.create_safe_browser_session', return_value=session):
                response = self.client.get(link['href'])
            self.assertEqual(200, response.status_code)
            form = BeautifulSoup(response.data, 'html.parser').find('button', string='Close session').find_parent('form')
            self.assertEqual(origin, form.find('input', attrs={'name': 'return_to'})['value'])
            with patch('rss_site_bridge.app.get_safe_browser_session', return_value=session):
                self.assertEqual(origin, self.client.post(form['action']).location)

    def test_missing_items_have_no_safe_link_and_unrelated_reports_cannot_be_return_targets(self):
        notification = self.refresh([self.entry('Story')])
        with closing(connect_db(self.db)) as conn:
            item_id = conn.execute('SELECT id FROM feed_items').fetchone()['id']
            conn.execute('DELETE FROM feed_items')
            conn.commit()
        soup = BeautifulSoup(self.client.get(f'/notifications/{notification.id}').data, 'html.parser')
        self.assertFalse(soup.select('.safe-link'))
        other = create_profile(self.db, FeedRequest('Other', 'https://example.org', 'article', 'a', 'a', '', 25, 0, 'http'))
        unrelated = create_notification(self.db, profile_id=other.id, event_type='refresh', severity='info', category='success', title='Other', message='Done')
        close = f'/profiles/{self.profile.id}/items/{item_id}/safe/expired/close'
        for destination in [f'/notifications/{unrelated.id}', '/notifications/999999', '//evil.example/notifications/1']:
            self.assertEqual(f'/profiles/{self.profile.id}', self.client.post(close, data={'return_to': destination}).location)

    def test_failure_keeps_stage_cause_http_status_and_configuration(self):
        def failure(config, *, progress):
            progress('Fetching content', 'Downloading source')
            try:
                raise HTTPError(config.source_url, 403, 'Forbidden', {}, None)
            except HTTPError as error:
                raise RuntimeError('Upstream HTTP error: 403 Forbidden') from error
        with patch('rss_site_bridge.app.extract_feed_entries', side_effect=failure):
            with self.assertRaises(RuntimeError):
                refresh_profile(self.db, self.profile.id)
        notification = list_notifications(self.db)[0]
        report = json.loads(notification.metadata_json)
        self.assertEqual('http_status', notification.category)
        self.assertEqual(403, report['errors'][1]['http_status'])
        self.assertEqual('Fetching content', report['stages'][-1]['title'])
        self.assertEqual('article', report['context']['item_selector'])
        self.assertGreaterEqual(report['duration_ms'], 0)
        self.assertNotIn(str(Path(__file__).parent), json.dumps(report['errors']))
        response = self.client.get(f'/notifications/{notification.id}')
        self.assertEqual(200, response.status_code)
        for text in (b'403', b'Forbidden', b'Fetching content', b'HTTPError', b'article'):
            self.assertIn(text, response.data)

    def test_unexpected_exceptions_also_create_failure_reports(self):
        with patch('rss_site_bridge.app.extract_feed_entries', side_effect=ZeroDivisionError('Unexpected failure')):
            with self.assertRaises(RuntimeError):
                refresh_profile(self.db, self.profile.id)
        notification = list_notifications(self.db)[0]
        self.assertEqual('error', notification.severity)
        self.assertEqual('ZeroDivisionError', json.loads(notification.metadata_json)['errors'][0]['type'])

    def test_legacy_metadata_missing_notification_and_escaped_content(self):
        notification = create_notification(self.db, profile_id=self.profile.id, event_type='refresh', severity='error',
                                           category='app', title='Problem', message='<script>alert(1)</script>')
        unread = count_unread_notifications(self.db)
        response = self.client.get(f'/notifications/{notification.id}?status=all')
        self.assertIn(b'Detailed diagnostics are unavailable for this refresh.', response.data)
        self.assertIn(b'&lt;script&gt;', response.data)
        self.assertNotIn(b'<script>alert(1)</script>', response.data)
        self.assertEqual(unread - 1, count_unread_notifications(self.db))
        self.assertIn(f'/notifications/{notification.id}?status=all'.encode(), self.client.get('/notifications?status=all').data)
        self.assertEqual(404, self.client.get('/notifications/999999').status_code)
        with closing(connect_db(self.db)) as conn:
            conn.execute('UPDATE notifications SET metadata_json=? WHERE id=?', ('invalid', notification.id))
            conn.commit()
        self.assertEqual(200, self.client.get(f'/notifications/{notification.id}').status_code)

    def test_opening_report_marks_only_that_notification_read_and_updates_badge(self):
        first = self.refresh([self.entry('First')])
        second = self.refresh([self.entry('Second')])
        self.assertEqual(2, count_unread_notifications(self.db))
        self.assertEqual(200, self.client.head(f'/notifications/{first.id}').status_code)
        self.assertEqual(2, count_unread_notifications(self.db))
        response = self.client.get(f'/notifications/{first.id}')
        soup = BeautifulSoup(response.data, 'html.parser')
        self.assertEqual('1', soup.select_one('[data-unread-notifications]').get_text(strip=True))
        self.assertNotIn('Mark read', soup.select_one('.refresh-report').get_text())
        notifications = {item.id: item for item in list_notifications(self.db)}
        read_at = notifications[first.id].read_at
        self.assertTrue(read_at)
        self.assertFalse(notifications[second.id].read_at)
        self.client.get(f'/notifications/{first.id}')
        self.assertEqual(read_at, next(item for item in list_notifications(self.db) if item.id == first.id).read_at)
        all_page = self.client.get('/notifications')
        self.assertIn(f'/notifications/{first.id}?status=all'.encode(), all_page.data)
        self.assertIn(f'/notifications/{second.id}?status=all'.encode(), all_page.data)
        all_soup = BeautifulSoup(all_page.data, 'html.parser')
        self.assertEqual('All', all_soup.select_one('[aria-current="page"].btn').get_text(strip=True))
        self.assertEqual('/notifications?status=all', soup.select_one('.page-title a')['href'])
        unread_page = self.client.get('/notifications?status=unread')
        self.assertNotIn(f'/notifications/{first.id}?'.encode(), unread_page.data)
        self.assertIn(f'/notifications/{second.id}?'.encode(), unread_page.data)


if __name__ == '__main__':
    unittest.main()
