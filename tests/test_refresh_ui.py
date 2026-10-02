"""Persistence and validation contracts for the refreshed feed editor."""
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from werkzeug.datastructures import MultiDict
from bs4 import BeautifulSoup

from rss_site_bridge.app import create_app, create_profile, connect_db, FeedRequest, get_profile_by_id, list_profiles


class RefreshEditorTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.db = Path(self.temp.name) / 'app.db'
        self.app = create_app({'TESTING': True, 'START_SCHEDULER': False, 'DATABASE_PATH': self.db})
        self.client = self.app.test_client()
        self.profile = create_profile(self.db, FeedRequest('Original', 'https://example.com/topics', 'article', 'a', 'a', '', 25, 60, 'http', notify_on_success=True))
        with closing(connect_db(self.db)) as conn:
            conn.execute('INSERT INTO feed_items (profile_id,title,link,summary,discovered_at) VALUES (?,?,?,?,?)',
                         (self.profile.id, 'Stored story', 'https://example.com/story', '', '2026-10-01T12:00:00+00:00'))
            conn.commit()
        self.values = dict(feed_title='Updated', source_url='https://example.com/topics', item_selector='article',
                           title_selector='a', link_selector='a', summary_selector='', max_items='25',
                           refresh_interval_minutes='60', cron_expression='', schedule_timezone='UTC', priority='0', fetch_mode='http')

    def tearDown(self):
        self.temp.cleanup()

    def test_smtp_editor_controls_and_hints_stay_inside_the_form(self):
        with closing(connect_db(self.db)) as conn:
            conn.execute('UPDATE app_settings SET smtp_enabled=1')
            conn.commit()
        for route in ['/compose', f'/profiles/{self.profile.id}?view=configuration']:
            with self.subTest(route=route):
                soup = BeautifulSoup(self.client.get(route).data, 'html.parser')
                form = soup.select_one('[data-feed-editor]')
                preferences = form.select_one('.notification-preferences')
                self.assertIsNotNone(preferences.select_one('.notification-failures'))
                self.assertIsNone(preferences.select_one(':scope > .ui-hint'))
                for choice in preferences.select('.notification-choice'):
                    self.assertIsNotNone(choice.select_one('.ui-hint'))
                    self.assertNotEqual('', choice.select_one('.ui-hint-content').get_text(strip=True))
                for name in ['fetch_mode', 'max_items', 'priority', 'notify_on_success', 'notify_failure_categories']:
                    self.assertIsNotNone(form.find(attrs={'name': name}), name)
                self.assertIsNotNone(form.select_one('[data-editor-save]'))
                self.assertIsNotNone(form.select_one('[data-editor-preview]'))

    def test_submission_list_is_below_its_heading(self):
        soup = BeautifulSoup(self.client.get('/settings/downloaders').data, 'html.parser')
        heading = soup.find('h2', string='Recent submissions').parent
        self.assertEqual('help-heading', heading.get('class')[0])
        self.assertIsNone(heading.select_one('.list-stack'))
        self.assertEqual(heading.parent, heading.find_next_sibling('div', class_='list-stack').parent)

    def test_detail_views_and_rss_do_not_fetch_source(self):
        with patch('rss_site_bridge.app.extract_feed_entries') as extract:
            items = self.client.get(f'/profiles/{self.profile.id}?view=items')
            self.assertIn(b'Stored story', items.data)
            self.assertNotIn(b'id="feed-settings-form"', items.data)
            config = self.client.get(f'/profiles/{self.profile.id}?view=configuration')
            self.assertIn(b'id="feed-settings-form"', config.data)
            self.assertNotIn(b'Stored story', config.data)
            rss = self.client.get(f'/profiles/{self.profile.id}?view=rss')
            self.assertIn(self.profile.feed_token.encode(), rss.data)
            self.assertNotIn(b'Stored story', rss.data)
            self.assertNotIn(b'class="xml-output"', rss.data)
            self.assertIn(b'target="_blank"', rss.data)
            extract.assert_not_called()
        fallback = self.client.get(f'/profiles/{self.profile.id}?view=unknown')
        self.assertIn(b'data-items-view', fallback.data)

    def test_invalid_edit_preserves_history_and_notification_choices(self):
        with closing(connect_db(self.db)) as conn:
            conn.execute('UPDATE app_settings SET smtp_enabled=1')
            conn.commit()
        data = MultiDict(self.values | {'cron_expression': 'invalid calendar', 'notify_failure_categories_present': '1'})
        data.setlist('notify_failure_categories', ['fetch', 'selector'])
        response = self.client.post(f'/profiles/{self.profile.id}/edit', data=data)
        self.assertEqual(response.status_code, 400)
        self.assertIn(b'value="invalid calendar"', response.data)
        self.assertIn(b'value="Updated"', response.data)
        self.assertIn(b'data-editor-errors', response.data)
        document = BeautifulSoup(response.data, 'html.parser')
        selected = document.select('input[name="notify_failure_categories"][checked]')
        self.assertEqual([input['value'] for input in selected], ['fetch', 'selector'])
        self.assertFalse(document.select('input[name="notify_on_success"][checked]'))
        current = get_profile_by_id(self.db, self.profile.id)
        self.assertEqual(current.feed_title, 'Original')
        self.assertEqual(current.feed_token, self.profile.feed_token)
        self.assertEqual(current.item_count, 1)

    def test_edit_can_clear_success_email_without_changing_token_or_history(self):
        response = self.client.post(f'/profiles/{self.profile.id}/edit', data=self.values | {'notify_failure_categories_present': '1'})
        self.assertEqual(response.status_code, 302)
        current = get_profile_by_id(self.db, self.profile.id)
        self.assertFalse(current.notify_on_success)
        self.assertEqual(current.feed_title, 'Updated')
        self.assertEqual(current.feed_token, self.profile.feed_token)
        self.assertEqual(current.item_count, 1)

    def test_create_modes_remain_idle_without_fetching(self):
        for values in [dict(refresh_interval_minutes='30'), dict(cron_expression='0 9 * * mon-fri', schedule_timezone='America/Chicago'), dict(refresh_interval_minutes='0')]:
            with self.subTest(values=values), patch('rss_site_bridge.app.extract_feed_entries') as extract:
                response = self.client.post('/profiles', data=self.values | values)
                self.assertEqual(response.status_code, 302)
                self.assertIn('created=1', response.headers['Location'])
                new = max(list_profiles(self.db), key=lambda profile: profile.id)
                self.assertEqual(new.last_status, 'idle')
                self.assertEqual(new.item_count, 0)
                self.assertEqual(new.cron_expression, values.get('cron_expression', ''))
                self.assertEqual(new.refresh_interval_minutes, int(values.get('refresh_interval_minutes', '60')))
                extract.assert_not_called()

    def test_schedule_timezone_is_global_and_changes_existing_feeds(self):
        response = self.client.post('/settings', data={'timezone_name': 'America/Chicago'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(get_profile_by_id(self.db, self.profile.id).schedule_timezone, 'America/Chicago')
        response = self.client.post('/profiles', data=self.values | {'cron_expression': '0 9 * * *', 'schedule_timezone': 'invalid-posted-timezone'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(max(list_profiles(self.db), key=lambda p: p.id).schedule_timezone, 'America/Chicago')
        document = BeautifulSoup(self.client.get('/compose').data, 'html.parser')
        self.assertIsNone(document.select_one('[name="schedule_timezone"]'))

    def test_timestamps_expose_absolute_instants_for_browser_locale(self):
        document = BeautifulSoup(self.client.get('/').data, 'html.parser')
        self.assertEqual(document.select_one('time[data-local-time]')['datetime'], '2026-10-01T12:00:00+00:00')

    def test_empty_unread_badge_is_available_for_async_refresh(self):
        document = BeautifulSoup(self.client.get('/feeds').data, 'html.parser')
        badge = document.select_one('[data-unread-notifications]')
        self.assertIsNotNone(badge)
        self.assertTrue(badge.has_attr('hidden'))
        self.assertEqual('0', badge.get_text())

    def test_settings_error_never_renders_submitted_password(self):
        response = self.client.post('/settings', data={'timezone_name':'invalid-zone', 'smtp_password':'secret-never-render'})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(b'secret-never-render', response.data)
        document = BeautifulSoup(response.data, 'html.parser')
        self.assertFalse(document.select_one('[name="smtp_password"]').get('value'))

    def test_missing_profile_is_still_not_found(self):
        self.assertEqual(self.client.post('/profiles/999/edit', data=self.values).status_code, 404)


if __name__ == '__main__':
    unittest.main()
