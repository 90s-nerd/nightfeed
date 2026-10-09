"""Stored item links resolve independently of pagination and MCP sort order."""
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup
from auth_support import authenticated_client
from oauth_support import connect_client
from rss_site_bridge.app import create_app, create_profile, connect_db, FeedRequest


class ItemLinkTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / 'feed.db'
        self.app = create_app(dict(TESTING=True, DATABASE_PATH=self.db, START_SCHEDULER=False))
        self.client = authenticated_client(self.app)
        self.profile = create_profile(self.db, FeedRequest('News', 'https://example.com', 'article', 'a', 'a', '', 100, 0, 'http'))
        self.ids = []
        with closing(connect_db(self.db)) as conn:
            for i in range(60):
                self.ids.append(conn.execute(
                    'INSERT INTO feed_items(profile_id,title,link,summary,discovered_at) VALUES(?,?,?,?,?)',
                    (self.profile.id, f'Story {i:02}', f'https://example.com/{i}', '', '2026-10-01T12:00:00+00:00')).lastrowid)
            conn.commit()

    def assert_item(self, url, item_id, page):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.data, 'html.parser')
        selected = soup.select('[data-selected-item]')
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]['data-topic-id'], str(item_id))
        self.assertEqual(selected[0]['tabindex'], '-1')
        self.assertIn('item-deep-link', selected[0]['class'])
        self.assertIn(f'Page {page} of 3', soup.get_text())
        return soup

    def test_direct_navigation_and_repeated_load_at_page_boundaries(self):
        for index in (0, 24, 25, 49, 50, 59):
            with self.subTest(index=index):
                item_id = self.ids[index]
                url = f'/profiles/{self.profile.id}?item={item_id}'
                for _ in range(2):
                    self.assert_item(url, item_id, index // 25 + 1)

    def test_item_overrides_stale_page_and_view_and_recomputes_after_insert(self):
        item_id = self.ids[24]
        url = f'/profiles/{self.profile.id}?item={item_id}&page=3&view=rss'
        self.assert_item(url, item_id, 1)
        with closing(connect_db(self.db)) as conn:
            conn.execute('UPDATE feed_items SET discovered_at=? WHERE id=?', ('2026-10-02T12:00:00+00:00', self.ids[59]))
            conn.commit()
        soup = self.assert_item(url, item_id, 2)
        links = [link['href'] for link in soup.select('.pagination-actions a')]
        self.assertEqual(links, ['?view=items&page=1', '?view=items&page=3'])

    def test_invalid_missing_and_other_feed_items(self):
        for value in ('', '0', '-1', 'abc', '9223372036854775808', '9' * 100):
            self.assertEqual(self.client.get(f'/profiles/{self.profile.id}?item={value}').status_code, 400)
        self.assertEqual(self.client.get(f'/profiles/{self.profile.id}?item=999999').status_code, 404)
        other = create_profile(self.db, FeedRequest('Other', 'https://other.example', 'article', 'a', 'a', '', 25, 0, 'http'))
        self.assertEqual(self.client.get(f'/profiles/{other.id}?item={self.ids[0]}').status_code, 404)

    def test_normal_feed_and_timeline_pagination_are_unchanged(self):
        for url in (f'/profiles/{self.profile.id}?page=2', '/?page=2'):
            soup = BeautifulSoup(self.client.get(url).data, 'html.parser')
            self.assertIn('Page 2 of 3', soup.get_text())
            self.assertEqual(len(soup.select('.item-card')), 25)
            self.assertFalse(soup.select('[data-selected-item]'))

    def test_login_redirect_preserves_item_link(self):
        url = f'/profiles/{self.profile.id}?item={self.ids[59]}'
        response = self.app.test_client().get(url)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(parse_qs(urlparse(response.location).query)['next'], [url])

    def test_mcp_search_and_get_topic_urls_resolve_to_requested_item(self):
        token = connect_client(self.app, self.client, self.db)
        item_id = self.ids[59]
        for name, arguments in [('search_topics', dict(query='Story 59')), ('get_topic', dict(item_id=item_id))]:
            response = self.app.test_client().post('/mcp', base_url='https://localhost',
                headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json, text/event-stream'},
                json=dict(jsonrpc='2.0', id=1, method='tools/call', params=dict(name=name, arguments=arguments)))
            self.assertEqual(response.status_code, 200)
            result = response.json['result']['structuredContent']
            topic = result['items'][0] if name == 'search_topics' else result
            self.assertEqual(topic['url'], f'/profiles/{self.profile.id}?item={item_id}')
            self.assert_item(topic['url'], item_id, 3)
