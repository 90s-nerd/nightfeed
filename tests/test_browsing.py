from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch, Mock
from bs4 import BeautifulSoup
from rss_site_bridge.app import (bind_download_handles, create_app, create_profile, update_profile, get_profile_by_id,
    extract_feed_entries, FeedEntry, FeedRequest, humanize_next_refresh, get_next_refresh_at, select_node_with_scope, validate_cron, list_feed_items, connect_db)


class BrowsingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "feed.db"
        self.app = create_app({"TESTING": True, "DATABASE_PATH": str(self.db), "START_SCHEDULER": False})
        self.client = self.app.test_client()
        self.config = FeedRequest("Alpha", "https://example.com", "article", "a", "a", "", 1, 60, "http", cron_expression="0 9 * * mon-fri", schedule_timezone="America/Chicago", priority=80)
        self.profile = create_profile(self.db, self.config)

    def test_safe_browser_returns_to_exact_origin_without_confirmation(self):
        self.seed()
        timeline = f"/?q=Topic&sort=priority&feed={self.profile.id}&feed=999&page=2"
        html = BeautifulSoup(self.client.get(timeline).data, "html.parser")
        link = html.find("a", string="Open safely")["href"]
        session = Mock(id="test-session")
        with patch("rss_site_bridge.app.create_safe_browser_session", return_value=session):
            page = self.client.get(link)
        form = BeautifulSoup(page.data, "html.parser").find("button", string="Close session").find_parent("form")
        self.assertNotIn("data-confirm", form.attrs)
        self.assertEqual(form.find("input", attrs={"name": "return_to"})["value"], timeline)
        with patch("rss_site_bridge.app.get_safe_browser_session", return_value=session):
            result = self.client.post(form["action"])
        self.assertEqual(result.location, timeline)
        session.stop.assert_called_once()

    def test_expired_safe_browser_return_and_untrusted_destination(self):
        close = f"/profiles/{self.profile.id}/items/1/safe/expired/close"
        origin = f"/profiles/{self.profile.id}?page=3"
        self.assertEqual(self.client.post(close, data={"return_to": origin}).location, origin)
        for destination in ["https://evil.example", "//evil.example", "/\\evil.example", "/settings", "/%2fevil.example"]:
            result = self.client.post(close, data={"return_to": destination})
            self.assertEqual(result.location, f"/profiles/{self.profile.id}")

    def test_safe_browser_unchanged_frame_skips_transfer(self):
        session = Mock()
        session.execute.return_value = b"jpeg-frame"
        url = f"/profiles/{self.profile.id}/items/1/safe/test/screenshot"
        with patch("rss_site_bridge.app.get_safe_browser_session", return_value=session):
            first = self.client.get(url)
            unchanged = self.client.get(url, headers={"If-None-Match": first.headers["ETag"]})
        self.assertEqual(first.mimetype, "image/jpeg")
        self.assertEqual(unchanged.status_code, 304)
        self.assertEqual(unchanged.data, b"")

    def test_same_name_download_handles_survive_reverse_completion_order(self):
        first, second = Mock(suggested_filename='same.bin'), Mock(suggested_filename='same.bin')
        downloads = {
            'first': {'name': 'same.bin', 'status': 'downloading'},
            'second': {'name': 'same.bin', 'status': 'finalizing'},
        }
        pending = [first, second]
        bind_download_handles(downloads, pending)
        self.assertIs(downloads['first']['handle'], first)
        self.assertIs(downloads['second']['handle'], second)
        self.assertEqual(pending, [])
        third = Mock(suggested_filename='same.bin')
        downloads['second']['status'] = 'ready'
        downloads['third'] = {'name': 'same.bin', 'status': 'downloading'}
        bind_download_handles(downloads, [third])
        self.assertIs(downloads['second']['handle'], second)
        self.assertIs(downloads['third']['handle'], third)

    def test_schedule_persistence_and_alignment(self):
        self.assertEqual(self.profile.priority, 80)
        self.assertNotEqual(humanize_next_refresh(replace(self.profile, refresh_interval_minutes=0)), "Manual only")
        profile = replace(self.profile, created_at="2026-10-01T12:00:00+00:00", refresh_anchor_at="2026-10-01T12:00:00+00:00", last_refreshed_at="2026-10-01T13:13:00+00:00")
        self.assertEqual(get_next_refresh_at(profile), datetime(2026, 10, 1, 14, tzinfo=timezone.utc))
        saved = update_profile(self.db, self.profile.id, replace(self.config, cron_expression="", priority=5))
        self.assertEqual(saved.cron_expression, "")
        self.assertEqual(saved.priority, 5)
        self.assertIsNone(get_next_refresh_at(replace(profile, active=False)))

    def test_invalid_schedules(self):
        for value in ["bad", "70 * * * *", "0 0 31 2 *"]:
            with self.assertRaises(ValueError):
                validate_cron(value)

    def test_parent_and_nth_child(self):
        soup = BeautifulSoup('<article><span class="title">Title</span><a href="/1">One</a><a href="/2">Two</a></article>', 'html.parser')
        node = soup.select_one('.title')
        self.assertEqual(select_node_with_scope(node, ':scope >> parent >> a:nth-of-type(2)')['href'], '/2')
        self.assertIsNone(select_node_with_scope(node, '.missing >> parent'))

    def seed(self):
        with closing(connect_db(self.db)) as conn:
            for i in range(31):
                conn.execute("INSERT INTO feed_items (profile_id, title, link, summary, discovered_at) VALUES (?, ?, ?, ?, ?)", (self.profile.id, f"Topic {i:02}", f"https://example.com/{i}", "needle" if i == 0 else "", "2026-10-01T12:00:00+00:00"))
            conn.commit()

    def test_pagination_search_and_filters(self):
        self.seed()
        self.assertEqual(len(list_feed_items(self.db, self.profile.id, 25, 25)), 6)
        for url in ['/', '/feeds', f'/profiles/{self.profile.id}?page=2']:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
        html = self.client.get('/?q=needle&sort=priority').get_data(as_text=True)
        self.assertIn('Topic 00', html)
        self.assertNotIn('Topic 01', html)
        self.assertIn('0 items', self.client.get('/?feed=999').get_data(as_text=True))
        page2 = self.client.get('/?page=2').get_data(as_text=True)
        self.assertIn('Page 2 of 2', page2)
        self.assertNotIn('Topic 30', page2)

    def test_inline_refresh_hook_and_error_json(self):
        html = self.client.get(f'/profiles/{self.profile.id}').get_data(as_text=True)
        self.assertIn('data-async-refresh', html)
        self.assertIn("event.preventDefault()", html)
        with patch('rss_site_bridge.app.extract_feed_entries', side_effect=RuntimeError('Upstream unavailable')):
            response = self.client.post(f'/profiles/{self.profile.id}/refresh', headers={'X-Requested-With': 'XMLHttpRequest'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json['error'], 'Upstream unavailable')

    def test_refresh_reports_actual_changes(self):
        entry = FeedEntry("First title", "https://example.com/topic", "Summary", datetime.now(timezone.utc))
        url = f"/profiles/{self.profile.id}/refresh"
        headers = {"X-Requested-With": "XMLHttpRequest"}
        with patch('rss_site_bridge.app.extract_feed_entries', return_value=[entry]):
            first = self.client.post(url, headers=headers).json
            same = self.client.post(url, headers=headers).json
        self.assertEqual(first['new_items'], 1)
        self.assertEqual(first['message'], 'Refresh complete: 1 new item.')
        self.assertEqual(same['item_count'], 1)
        self.assertEqual(same['new_items'], 0)
        self.assertEqual(same['updated_items'], 0)
        self.assertEqual(same['message'], 'Already up to date.')
        with patch('rss_site_bridge.app.extract_feed_entries', return_value=[replace(entry, title="Changed title")]):
            changed = self.client.post(url, headers=headers).json
        self.assertEqual(changed['new_items'], 0)
        self.assertEqual(changed['updated_items'], 1)
        self.assertEqual(changed['message'], 'Refresh complete: 1 updated item.')
        with patch('rss_site_bridge.app.extract_feed_entries', return_value=[]):
            empty = self.client.post(url, headers=headers).json
        self.assertEqual(empty['message'], 'Already up to date.')

    def test_multiple_grouped_containers_preserve_all_links_and_wrapped_titles(self):
        html = '<div class="banger-container">'
        for group in range(2):
            html += '<div class="banger-row">'
            for i in range(3):
                number = group * 3 + i
                html += f'<strong>Sardar {number} (2026) Tamil UHD -</strong><strong><a href="/forums/topic/{number}">[4K, 1080p]</a> <a href="https://other.example/file">[W]</a></strong><br>'
            html += '</div>'
        html += '</div>'
        config = replace(self.config, item_selector='.banger-container div.banger-row', title_selector=':scope', link_selector='a[href*="/forums/topic/"]', max_items=100)
        with patch('rss_site_bridge.app.fetch_html', return_value=html):
            entries = extract_feed_entries(config)
        self.assertEqual(len(entries), 6)
        for i, entry in enumerate(entries):
            self.assertTrue(entry.title.startswith(f'Sardar {i} (2026) Tamil UHD -'), entry.title)
            self.assertIn('[4K, 1080p]', entry.title)

    def test_shared_wrapper_does_not_merge_adjacent_titles(self):
        html = """<div class="group"><strong>
          <span>Ohh My Dog (2026) - </span><a href="/forums/topic/dog">[1080p]</a><br>
          <span>Chumbak (2026) - </span><a href="/forums/topic/chumbak">[720p]</a><br>
          <span>East of Eden (2026) - </span><a href="/forums/topic/eden">[1080p]</a>
        </strong></div>"""
        config = replace(self.config, item_selector='.group', title_selector=':scope', link_selector='a', max_items=100)
        with patch('rss_site_bridge.app.fetch_html', return_value=html):
            entries = extract_feed_entries(config)
        self.assertEqual([entry.title for entry in entries], ['Ohh My Dog (2026) - [1080p]', 'Chumbak (2026) - [720p]', 'East of Eden (2026) - [1080p]'])

    def test_nested_rows_and_links_are_title_boundaries(self):
        html = '<div class="group"><div><strong>First -</strong><strong><a href="/forums/topic/first">[4K]</a><a href="https://other.example">[W]</a></strong></div><div><strong>Second -</strong><strong><a href="/forums/topic/second">[720p]</a></strong></div></div>'
        config = replace(self.config, item_selector='.group', title_selector=':scope', link_selector='a[href*="/forums/topic/"]', max_items=100)
        with patch('rss_site_bridge.app.fetch_html', return_value=html):
            entries = extract_feed_entries(config)
        self.assertEqual([entry.title for entry in entries], ['First - [4K]', 'Second - [720p]'])
