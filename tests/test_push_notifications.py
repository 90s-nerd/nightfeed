from auth_support import authenticated_client
"""Push preferences, durable summaries, and opt-in delivery protections."""
import base64
import json
import re
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from rss_site_bridge.app import create_app, create_profile, FeedRequest, FeedEntry, refresh_profile
from rss_site_bridge import push_notifications as push


def sample_subscription(suffix='one'):
    key = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    encode = lambda value: base64.urlsafe_b64encode(value).decode().rstrip('=')
    return dict(endpoint='https://fcm.googleapis.com/fcm/send/' + suffix,
                keys=dict(p256dh=encode(key), auth=encode(b'0123456789abcdef')))


class PushTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'app.db'
        self.app = create_app(dict(TESTING=True, START_SCHEDULER=False, DATABASE_PATH=self.db))
        self.client = authenticated_client(self.app)
        html = self.client.get('/settings').text
        self.csrf = re.search(r'name="push-csrf" content="([^"]+)"', html)[1]
        self.headers = {'X-CSRF-Token': self.csrf}
        self.profile = create_profile(self.db, FeedRequest('News', 'https://example.com', 'article', 'a', 'a', '', 100, 0, 'http'))
        self.sub = sample_subscription()
        self.sequence = 0

    def post(self, path, **payload):
        return self.client.post('/api/push/' + path, json=payload, headers=self.headers)

    def subscribe(self, **prefs):
        result = self.post('subscribe', subscription=self.sub, preferences=prefs)
        self.assertEqual(result.status_code, 200, result.text)
        self.token = result.json['device_token']
        return result.json

    def row(self):
        with closing(push.connect(self.db)) as conn:
            return dict(conn.execute('SELECT * FROM push_devices').fetchone())

    def queue(self, new=0, updated=0, status='ok', now=1000, profile=None):
        self.sequence += 1
        push.enqueue(self.db, profile or self.profile, SimpleNamespace(id=self.sequence), status, new, updated, now)

    def test_opt_in_defaults_and_packaged_assets(self):
        with closing(push.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM push_devices').fetchone()[0], 0)
            key = conn.execute('SELECT private_key,public_key FROM push_config').fetchone()
        push.initialize(self.db)
        with closing(push.connect(self.db)) as conn:
            self.assertEqual(tuple(key), tuple(conn.execute('SELECT private_key,public_key FROM push_config').fetchone()))
        defaults = self.subscribe()['preferences']
        self.assertEqual((defaults['new'], defaults['updated'], defaults['failures'], defaults['interval'], defaults['daily_limit']), (True, False, False, 15, 12))
        self.assertIn('no-store', self.client.get('/api/push/config').headers['Cache-Control'])
        manifest = self.client.get('/manifest.webmanifest').json
        self.assertEqual(manifest['display'], 'standalone')
        for icon in manifest['icons']:
            with self.client.get(icon['src']) as response:
                self.assertEqual(response.status_code, 200)
        with self.client.get('/service-worker.js') as response:
            self.assertEqual(response.headers['Service-Worker-Allowed'], '/')

    def test_csrf_origin_endpoint_validation_and_device_isolation(self):
        self.assertEqual(self.client.post('/api/push/subscribe', json={}).status_code, 403)
        self.assertEqual(self.client.post('/api/push/subscribe', json={}, headers={**self.headers, 'Origin': 'https://evil.test'}).status_code, 403)
        for endpoint in ('http://fcm.googleapis.com/x', 'https://127.0.0.1/x', 'https://fcm.googleapis.com.evil.test/x', 'https://fcm.googleapis.com:444/x'):
            self.assertEqual(self.post('subscribe', subscription={**self.sub, 'endpoint': endpoint}).status_code, 400)
        for prefs in ({'new': 'yes'}, {'interval': 0}, {'feeds': [True]}, {'timezone': '../bad'}, {'quiet': True, 'quiet_start': '08:00', 'quiet_end': '08:00'}):
            self.assertEqual(self.post('subscribe', subscription=self.sub, preferences=prefs).status_code, 400)
        self.subscribe()
        self.assertEqual(self.post('preferences', device_token='another-device', preferences={}).status_code, 400)
        self.assertEqual(self.post('subscribe', subscription=sample_subscription()).status_code, 400)

    @patch.object(push, 'deliver')
    def test_logout_preserves_device_and_delivers_summary(self, deliver):
        from rss_site_bridge.auth import COOKIE, fingerprint
        self.subscribe(updated=True)
        self.queue(new=2, updated=3)
        before = self.row()
        old_cookie = self.client.get_cookie(COOKIE).value
        response = self.client.post('/auth/logout')
        self.assertEqual(response.headers['Clear-Site-Data'], '"cache"')
        self.assertEqual(self.row(), before)
        with closing(push.connect(self.db)) as conn:
            self.assertIsNone(conn.execute('SELECT 1 FROM auth_sessions WHERE token_hash=?', (fingerprint(old_cookie),)).fetchone())
        push.dispatch(self.db, 1900)
        self.assertEqual(deliver.call_count, 1)
        payload = deliver.call_args.args[2]
        self.assertEqual(payload['title'], 'Nightfeed · News')
        self.assertEqual(payload['body'], '2 new topics, 3 updated topics')
        self.assertEqual(payload['url'], '/notifications/1')
        self.assertEqual(self.client.get(payload['url']).status_code, 302)
        self.assertEqual(self.client.post('/api/push/device', json=dict(device_token=self.token), headers=self.headers).status_code, 401)

    @patch.object(push, 'deliver')
    def test_multiple_feeds_summary_counts_and_list_link(self, deliver):
        self.subscribe(updated=True, failures=True)
        self.queue(new=2)
        self.queue(new=1, updated=4, now=1100, profile=SimpleNamespace(id=999, feed_title='Other'))
        self.queue(status='error', now=1200)
        push.dispatch(self.db, 1900)
        payload = deliver.call_args.args[2]
        self.assertEqual(payload['title'], 'Nightfeed · 2 feeds')
        self.assertEqual(payload['body'], '3 new topics, 4 updated topics, 1 feed failures')
        self.assertEqual(payload['url'], '/notifications')

    @patch.object(push, 'deliver')
    def test_unchanged_refreshes_do_not_alert_and_changes_batch(self, deliver):
        self.subscribe(updated=True)
        self.queue()
        push.dispatch(self.db, 3000)
        deliver.assert_not_called()
        self.queue(new=2)
        self.queue(updated=3, now=1100)
        push.dispatch(self.db, 1899)
        deliver.assert_not_called()
        push.dispatch(self.db, 1900)
        self.assertEqual(deliver.call_args.args[2]['body'], '2 new topics, 3 updated topics')
        self.assertEqual(deliver.call_args.args[2]['url'], '/notifications')
        push.dispatch(self.db, 2000)
        self.assertEqual(deliver.call_count, 1)

    @patch.object(push, 'deliver')
    def test_types_and_feed_selection(self, deliver):
        self.subscribe(new=False, updated=True, feeds=[self.profile.id])
        self.queue(new=5)
        self.queue(new=2, updated=8, profile=SimpleNamespace(id=999, feed_title='Other'))
        self.queue(updated=1)
        push.dispatch(self.db, 1900)
        self.assertEqual(deliver.call_args.args[2]['body'], '1 updated topics')

    @patch.object(push, 'deliver')
    def test_daily_limit_and_quiet_hours_across_midnight(self, deliver):
        self.subscribe(daily_limit=1, quiet=True, timezone='America/Chicago')
        at = lambda day, hour: datetime(2026, 10, day, hour, tzinfo=timezone.utc).timestamp()
        self.queue(new=1, now=at(4, 4))  # 11 PM CDT
        push.dispatch(self.db, at(4, 12))  # 7 AM, still quiet
        deliver.assert_not_called()
        push.dispatch(self.db, at(4, 13))
        self.assertEqual(deliver.call_count, 1)
        self.queue(new=2, now=at(4, 14))
        push.dispatch(self.db, at(4, 15))
        self.assertEqual(deliver.call_count, 1)
        push.dispatch(self.db, at(5, 13))
        self.assertEqual(deliver.call_count, 2)

    @patch.object(push, 'deliver')
    def test_failures_alert_once_until_recovery(self, deliver):
        self.subscribe(failures=True)
        self.queue(status='error')
        self.queue(status='error', now=1100)
        push.dispatch(self.db, 1900)
        self.assertEqual(deliver.call_args.args[2]['body'], '1 feed failures')
        self.queue(status='error', now=2000)
        push.dispatch(self.db, 3000)
        self.assertEqual(deliver.call_count, 1)
        self.queue(now=3100)
        self.queue(status='error', now=3200)
        push.dispatch(self.db, 4100)
        self.assertEqual(deliver.call_count, 2)

    @patch.object(push, 'deliver')
    def test_recovered_failures_and_disabled_devices_are_not_delivered(self, deliver):
        self.subscribe(failures=True)
        self.queue(status='error')
        self.queue(now=1100)
        push.dispatch(self.db, 1900)
        deliver.assert_not_called()
        self.queue(new=2, now=2000)
        self.post('disable', device_token=self.token)
        push.dispatch(self.db, 3000)
        deliver.assert_not_called()
        self.assertFalse(self.post('device', device_token=self.token).json['enabled'])

    @patch.object(push, 'deliver')
    def test_retry_expiry_and_rate_limits_survive_reregistration(self, deliver):
        self.subscribe()
        self.queue(new=1)
        deliver.side_effect = RuntimeError('offline')
        push.dispatch(self.db, 1900)
        self.assertEqual(self.row()['retries'], 1)
        push.dispatch(self.db, 2499)
        self.assertEqual(deliver.call_count, 1)
        deliver.side_effect = None
        push.dispatch(self.db, 2500)
        self.assertEqual(self.row()['sent'], 1)
        self.subscribe()
        self.assertEqual(self.row()['sent'], 1)
        self.queue(new=1, now=3000)
        deliver.side_effect = push.WebPushException('gone', response=SimpleNamespace(status_code=410))
        push.dispatch(self.db, 3900)
        self.assertFalse(self.row()['enabled'])

    @patch.object(push, 'deliver')
    def test_explicit_test_is_throttled_and_resubscribe_cannot_bypass(self, deliver):
        self.subscribe()
        self.assertEqual(self.post('test', device_token=self.token).status_code, 200)
        self.assertEqual(self.post('test', device_token=self.token).status_code, 429)
        self.subscribe()
        self.assertEqual(self.post('test', device_token=self.token).status_code, 429)
        self.assertEqual(deliver.call_count, 1)

    def test_real_payload_encryption_and_vapid_signing(self):
        self.subscribe()
        response = SimpleNamespace(status_code=201, text='', headers={})
        with patch.object(push.PushSession, 'post', return_value=response) as post:
            push.deliver(self.db, self.row(), {'title': 'Nightfeed', 'body': '1 new topic', 'url': '/notifications'})
        self.assertIn('vapid', post.call_args.kwargs['headers']['authorization'])
        self.assertNotIn(b'1 new topic', post.call_args.kwargs['data'])
        self.assertEqual(post.call_args.kwargs['timeout'], 10)

    @patch.object(push, 'deliver')
    def test_real_refresh_queues_only_changed_items(self, deliver):
        self.subscribe(updated=True)
        entry = FeedEntry('First title', 'https://example.com/one', '', datetime.now(timezone.utc))
        with patch('rss_site_bridge.app.extract_feed_entries', return_value=[entry]):
            refresh_profile(self.db, self.profile.id)
            refresh_profile(self.db, self.profile.id)
        with patch('rss_site_bridge.app.extract_feed_entries', return_value=[FeedEntry('Changed title', entry.link, '', entry.published_at)]):
            refresh_profile(self.db, self.profile.id)
        with closing(push.connect(self.db)) as conn:
            events = conn.execute('SELECT new_count,updated_count FROM push_events ORDER BY id').fetchall()
        self.assertEqual([tuple(row) for row in events], [(1, 0), (0, 1)])


if __name__ == '__main__':
    unittest.main()
