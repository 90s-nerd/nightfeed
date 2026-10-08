from auth_support import authenticated_client
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
import hashlib
import base64
import json
import re
import threading
import time
import unittest

from cryptography.fernet import Fernet
from rss_site_bridge.app import create_app
from rss_site_bridge import downloaders as d
from rss_site_bridge import downloader_adapters as adapters


def bencode(value):
    if isinstance(value, bytes):
        return str(len(value)).encode() + b':' + value
    if isinstance(value, int):
        return b'i' + str(value).encode() + b'e'
    if isinstance(value, list):
        return b'l' + b''.join(bencode(v) for v in value) + b'e'
    return b'd' + b''.join(bencode(k) + bencode(value[k]) for k in sorted(value)) + b'e'


def metadata_file(name=b'Example'):
    return bencode({b'info': {b'name': name, b'length': 1, b'piece length': 16384, b'pieces': b'x' * 20}})


class FakeDownloader:
    identify = staticmethod(adapters.WebAPIv2Downloader.identify)
    extensions = ('.torrent',)
    supports_magnets = True
    version = '5.0.0'
    api_version = '2.11.0'
    present = False
    failure = None
    added = []
    gate = None

    def __init__(self, profile, key):
        self.profile = profile

    def categories(self):
        return [{'name': 'Movies', 'save_path': '/downloads/movies'}]

    def contains(self, hashes):
        return self.present

    def add(self, data, category, start):
        if self.gate:
            self.gate.wait(3)
        self.added.append((data, category, start))
        if self.failure:
            raise self.failure
        type(self).present = True


class DownloaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'test.db'
        self.session = Mock()
        self.session.execute.return_value = {'name': 'example.TORRENT', 'data': metadata_file()}
        lookup = patch('rss_site_bridge.app.get_safe_browser_session', return_value=self.session)
        self.lookup = lookup.start()
        self.addCleanup(lookup.stop)
        self.app = create_app({'TESTING': True, 'START_SCHEDULER': False, 'DATABASE_PATH': self.db})
        self.client = authenticated_client(self.app)
        html = self.client.get('/settings/downloaders').get_data(as_text=True)
        self.token = re.search(r'name="downloader-csrf" content="([^"]+)"', html).group(1)
        self.headers = {'X-CSRF-Token': self.token}
        self.values = dict(name='Home', kind='qbittorrent', enabled='1', base_url='http://localhost:8080/downloader', auth_mode='password', username='admin', secret='private-password', verify_tls='1', timeout='15', extensions='.torrent', default_category='Movies', allowed_categories='', require_category='1', start_immediately='1')
        response = self.client.post('/settings/downloaders/save', data=self.values, headers=self.headers)
        self.assertEqual(response.status_code, 302)
        self.profile = d.profiles(self.db)[0]
        self.url = '/profiles/1/items/2/safe/session/downloads/file/send'
        FakeDownloader.present = False
        FakeDownloader.failure = None
        FakeDownloader.added = []
        FakeDownloader.gate = None
        adapter = patch.dict(d.ADAPTERS, qbittorrent=FakeDownloader)
        adapter.start()
        self.addCleanup(adapter.stop)

    def submit(self, **overrides):
        payload = dict(downloader_id=self.profile['id'], category='Movies', start_immediately=True)
        payload.update(overrides)
        return self.client.post(self.url, json=payload, headers=self.headers)

    def test_standalone_browser_sends_downloads_through_same_workflow(self):
        self.url = '/safe-browser/session/downloads/file/send'
        response = self.submit()
        self.assertEqual(response.status_code, 202)
        self.lookup.assert_called_with('session', None, None)
        result = self.completed(response.json['id'])
        self.assertEqual(result['status'], 'added')
        self.assertEqual(FakeDownloader.added[0][1:], ('Movies', True))

    def test_magnet_submission_uses_existing_torrent_route_and_category(self):
        self.client.post('/settings/downloaders/save', data=self.values | {'id': self.profile['id'], 'enable_magnets': '1'}, headers=self.headers)
        uri = 'magnet:?xt=urn:btih:' + 'a' * 40 + '&dn=Example&tr=https%3A%2F%2Ftracker.example%2Fannounce'
        self.session.execute.return_value = {'name': 'Example', 'kind': 'magnet', 'data': uri}
        self.assertEqual(d.eligible_profiles(self.db, 'Example', 'magnet')[0]['id'], self.profile['id'])
        response = self.submit()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.completed(response.json['id'])['status'], 'added')
        self.assertEqual(FakeDownloader.added, [(uri, 'Movies', True)])

    def test_pending_file_and_magnet_with_same_hash_share_submission(self):
        self.client.post('/settings/downloaders/save', data=self.values | {'id': self.profile['id'], 'enable_magnets': '1'}, headers=self.headers)
        FakeDownloader.gate = threading.Event()
        self.addCleanup(FakeDownloader.gate.set)
        first = self.submit()
        _, hashes = adapters.file_identity(metadata_file())
        uri = 'magnet:?xt=urn:btih:' + hashes[0]
        self.session.execute.return_value = {'name': 'Magnet', 'kind': 'magnet', 'data': uri}
        second = self.submit()
        self.assertEqual(second.json['id'], first.json['id'])
        FakeDownloader.gate.set()
        self.assertEqual(self.completed(first.json['id'])['status'], 'added')
        self.assertEqual(len(FakeDownloader.added), 1)

    def test_magnet_requires_enabled_supported_downloader(self):
        self.session.execute.return_value = {'name': 'Magnet', 'kind': 'magnet', 'data': 'magnet:?xt=urn:btih:' + 'b' * 40}
        with patch.object(FakeDownloader, 'supports_magnets', False):
            self.assertEqual(d.eligible_profiles(self.db, 'Magnet', 'magnet'), [])
            self.assertEqual(self.submit().status_code, 400)
        values = self.values | {'id': self.profile['id'], 'secret': ''}
        values.pop('enabled')
        self.client.post('/settings/downloaders/save', data=values, headers=self.headers)
        self.assertEqual(d.eligible_profiles(self.db, 'Magnet', 'magnet'), [])
        self.assertEqual(self.submit().status_code, 400)

    def test_magnets_are_opt_in_and_use_configured_button_label(self):
        self.session.execute.return_value = {'name': 'Magnet', 'kind': 'magnet', 'data': 'magnet:?xt=urn:btih:' + 'b' * 40}
        self.assertEqual(self.profile['enable_magnets'], 0)
        self.assertEqual(d.eligible_profiles(self.db, 'Magnet', 'magnet'), [])
        self.assertEqual(self.submit().status_code, 400)
        values = self.values | {'id': self.profile['id'], 'enable_magnets': '1', 'button_label': 'Queue at home'}
        self.client.post('/settings/downloaders/save', data=values, headers=self.headers)
        self.assertEqual(d.eligible_profiles(self.db, 'Magnet', 'magnet')[0]['label'], 'Queue at home')
        html = self.client.get('/settings/downloaders?edit=' + str(self.profile['id'])).get_data(as_text=True)
        self.assertRegex(html, r'name="enable_magnets"[^>]*checked')
        values.pop('enable_magnets')
        self.client.post('/settings/downloaders/save', data=values, headers=self.headers)
        self.assertEqual(d.eligible_profiles(self.db, 'Magnet', 'magnet'), [])
        self.assertEqual(self.submit().status_code, 400)
        self.assertEqual(len(d.eligible_profiles(self.db, 'Example.torrent')), 1)

    def test_existing_profiles_upgrade_with_magnets_disabled(self):
        with closing(d.connect(self.db)) as conn:
            conn.execute('ALTER TABLE downloaders DROP COLUMN enable_magnets')
            conn.commit()
        d.initialize(self.db)
        saved = d.get_profile(self.db, self.profile['id'])
        self.assertEqual(saved['name'], 'Home')
        self.assertEqual(saved['enable_magnets'], 0)
        with closing(d.connect(self.db)) as conn:
            conn.execute('UPDATE downloaders SET enable_magnets=1 WHERE id=?', (self.profile['id'],))
            conn.commit()
        d.initialize(self.db)
        self.assertEqual(d.get_profile(self.db, self.profile['id'])['enable_magnets'], 1)

    def test_browser_state_hides_magnets_until_profile_opt_in(self):
        files = [{'id': 'magnet', 'name': 'Example', 'kind': 'magnet', 'status': 'ready'},
                 {'id': 'file', 'name': 'Example.torrent', 'kind': 'file', 'status': 'ready'}]
        self.session.execute.return_value = {'downloads': [dict(row) for row in files]}
        response = self.client.get('/safe-browser/session/state')
        self.assertEqual([row['id'] for row in response.json['downloads']], ['file'])
        self.client.post('/settings/downloaders/save', data=self.values | {'id': self.profile['id'], 'enable_magnets': '1', 'button_label': 'Queue at home'}, headers=self.headers)
        self.session.execute.return_value = {'downloads': [dict(row) for row in files]}
        response = self.client.get('/safe-browser/session/state')
        self.assertEqual([row['id'] for row in response.json['downloads']], ['magnet', 'file'])
        self.assertEqual(response.json['downloads'][0]['downloaders'][0]['label'], 'Queue at home')

    def completed(self, identity):
        for _ in range(100):
            result = self.client.get('/api/downloader-jobs/' + identity).json
            if result['status'] != 'sending':
                return result
            time.sleep(0.02)
        self.fail('Background submission did not complete')

    def test_edit_preserves_punctuation_in_category_names(self):
        from bs4 import BeautifulSoup
        categories = 'Movies, HD\nSeries "Favorites"'
        values = dict(self.values, id=str(self.profile['id']), allowed_categories=categories, default_category='Movies, HD')
        saved = self.client.post('/settings/downloaders/save', data=values, headers=self.headers)
        self.assertEqual(saved.status_code, 302)
        page = BeautifulSoup(self.client.get(f"/settings/downloaders?edit={self.profile['id']}").data, 'html.parser')
        self.assertEqual(page.find('textarea', attrs={'name': 'allowed_categories'}).text, categories)
        self.assertEqual(page.find('input', attrs={'name': 'extensions'})['value'], '.torrent')

    def test_product_wording_is_generic_and_type_choice_is_preserved(self):
        from bs4 import BeautifulSoup
        html = BeautifulSoup(self.client.get('/settings/downloaders').data, 'html.parser')
        selector = html.find('select', attrs={'name': 'kind'})
        self.assertIn('qBittorrent', selector.text)
        selector.decompose()
        text = html.get_text(' ', strip=True).lower()
        self.assertNotIn('qbittorrent', text)
        self.assertNotIn('torrent', text)
        self.assertEqual(self.client.get('/settings/downloaders').status_code, 200)

    def test_secrets_encrypted_and_never_rendered(self):
        self.assertNotIn('private-password', self.profile['secret'])
        self.assertEqual(Fernet(d.encryption_key(self.db)).decrypt(self.profile['secret'].encode()), b'private-password')
        html = self.client.get('/settings/downloaders?edit=' + str(self.profile['id'])).get_data(as_text=True)
        self.assertNotIn('private-password', html)
        self.assertNotIn(self.profile['secret'], html)

    def test_labels_routes_and_disabled_profiles(self):
        self.assertEqual(d.eligible_profiles(self.db, 'EXAMPLE.TORRENT')[0]['label'], 'Send to Home')
        self.assertEqual(d.eligible_profiles(self.db, 'example.pdf'), [])
        values = self.values | {'id': self.profile['id'], 'secret': '', 'name': 'Remote server', 'button_label': 'Queue on server'}
        self.client.post('/settings/downloaders/save', data=values, headers=self.headers)
        self.assertEqual(d.eligible_profiles(self.db, 'x.torrent')[0]['label'], 'Queue on server')
        values['button_label'] = ''
        self.client.post('/settings/downloaders/save', data=values, headers=self.headers)
        self.assertEqual(d.eligible_profiles(self.db, 'x.torrent')[0]['label'], 'Send to Remote server')
        values.pop('enabled')
        self.client.post('/settings/downloaders/save', data=values, headers=self.headers)
        self.assertEqual(d.eligible_profiles(self.db, 'x.torrent'), [])
        self.assertEqual(self.submit().status_code, 400)

    def test_invalid_routes_urls_and_csrf(self):
        self.assertEqual(self.client.post(self.url, json={}).status_code, 403)
        self.assertEqual(self.client.post(self.url, json={}, headers=self.headers | {'Origin': 'https://other.example'}).status_code, 403)
        for field, value in [('extensions', '.pdf'), ('base_url', 'http://admin:password@example.com'), ('timeout', '100'), ('default_category', 'Missing')]:
            form = self.values | {field: value, 'allowed_categories': 'Movies'}
            response = self.client.post('/settings/downloaders/save', data=form, headers=self.headers)
            self.assertEqual(response.status_code, 400)
            self.assertNotIn('private-password', response.get_data(as_text=True))

    def test_categories_and_success(self):
        response = self.client.get(f'/api/downloaders/{self.profile["id"]}/categories')
        self.assertEqual(response.json['categories'][0]['save_path'], '/downloads/movies')
        response = self.submit(start_immediately=False)
        self.assertEqual(response.status_code, 202)
        result = self.completed(response.json['id'])
        self.assertEqual(result['status'], 'added')
        self.assertEqual(FakeDownloader.added[0][1:], ('Movies', False))
        self.session.execute.assert_called_with('download_copy', download_id='file')
        self.assertNotIn('secret', json.dumps(result))

    def test_existing_download_not_added_or_recategorized(self):
        FakeDownloader.present = True
        result = self.completed(self.submit().json['id'])
        self.assertEqual(result['status'], 'already_present')
        self.assertEqual(FakeDownloader.added, [])

    def test_double_tap_uses_same_active_job(self):
        FakeDownloader.gate = threading.Event()
        first = self.submit().json
        try:
            second = self.submit().json
            self.assertEqual(first['id'], second['id'])
            self.assertEqual(second['status'], 'sending')
            self.assertEqual(self.client.post(f'/settings/downloaders/{self.profile["id"]}/delete', headers=self.headers).status_code, 400)
        finally:
            FakeDownloader.gate.set()
        self.assertEqual(self.completed(first['id'])['status'], 'added')

    def test_uncertain_timeout_requires_check_before_retry(self):
        FakeDownloader.failure = ValueError('Connection timed out.')
        first = self.completed(self.submit().json['id'])
        self.assertEqual(first['status'], 'unknown')
        self.assertEqual(self.submit().json['id'], first['id'])
        self.assertEqual(len(FakeDownloader.added), 1)
        checked = self.client.post(f'/api/downloader-jobs/{first["id"]}/check', headers=self.headers)
        self.assertEqual(checked.json['status'], 'error')
        FakeDownloader.failure = None
        self.assertEqual(self.completed(self.submit().json['id'])['status'], 'added')

    def test_timeout_but_remote_accepted_is_confirmed(self):
        def uncertain(adapter, data, category, start):
            FakeDownloader.present = True
            raise ValueError('Timed out')
        with patch.object(FakeDownloader, 'add', uncertain):
            self.assertEqual(self.completed(self.submit().json['id'])['status'], 'added')

    def test_remote_rejection_is_retryable(self):
        FakeDownloader.failure = d.SubmissionRejected('Invalid file')
        self.assertEqual(self.completed(self.submit().json['id'])['status'], 'error')

    def test_expired_session_invalid_file_and_changed_category(self):
        self.lookup.return_value = None
        self.assertEqual(self.submit().status_code, 410)
        self.lookup.return_value = self.session
        self.session.execute.return_value = {'name': 'x.torrent', 'data': b'<html>Error</html>'}
        self.assertEqual(self.submit().status_code, 400)
        self.session.execute.return_value = {'name': 'x.pdf', 'data': metadata_file()}
        self.assertEqual(self.submit().status_code, 400)
        self.session.execute.return_value = {'name': 'x.torrent', 'data': metadata_file()}
        self.assertEqual(self.submit(category='').status_code, 400)
        result = self.completed(self.submit(category='Deleted').json['id'])
        self.assertEqual(result['status'], 'error')
        self.assertEqual(FakeDownloader.added, [])

    def test_disabled_checkbox_preserved_on_validation_error(self):
        values = self.values | {'extensions': '.pdf'}
        values.pop('enabled')
        html = self.client.post('/settings/downloaders/save', data=values, headers=self.headers).get_data(as_text=True)
        checkbox = re.search(r'<input type="checkbox" name="enabled"[^>]*>', html).group()
        self.assertNotIn('checked', checkbox)

    def test_secret_preserved_environment_reference_and_clear_confirmation(self):
        old = self.profile['secret']
        values = self.values | {'id': self.profile['id'], 'secret': '', 'secret_env': 'QBIT_PASSWORD'}
        self.assertEqual(self.client.post('/settings/downloaders/save', data=values, headers=self.headers).status_code, 302)
        saved = d.get_profile(self.db, self.profile['id'])
        self.assertEqual(saved['secret'], old)
        self.assertEqual(saved['secret_env'], 'QBIT_PASSWORD')
        values['clear_secret'] = '1'
        self.client.post('/settings/downloaders/save', data=values, headers=self.headers)
        self.assertEqual(d.get_profile(self.db, self.profile['id'])['secret'], '')
        html = self.client.get('/settings/downloaders').get_data(as_text=True)
        self.assertIn('data-confirm="Delete Home?', html)
        self.assertIn("window.confirm('Clear the saved downloader secret?", html)

    def test_submission_pool_is_bounded_and_released(self):
        acquired = []
        try:
            for _ in range(4):
                self.assertTrue(d._slots.acquire(blocking=False))
                acquired.append(True)
            self.assertEqual(self.submit().status_code, 429)
        finally:
            for _ in acquired:
                d._slots.release()
        self.assertEqual(self.completed(self.submit().json['id'])['status'], 'added')

    def test_deleted_downloader_preserves_history_and_expiry_is_json(self):
        result = self.completed(self.submit().json['id'])
        self.assertEqual(self.client.post(f'/settings/downloaders/{self.profile["id"]}/delete', headers=self.headers).status_code, 302)
        self.assertEqual(self.client.get('/api/downloader-jobs/' + result['id']).json['destination'], 'Home')
        self.session.execute.side_effect = RuntimeError('Browser is closed')
        self.assertEqual(self.submit().status_code, 400)  # Destination was removed.

    def test_browser_state_only_exposes_eligible_public_destinations(self):
        self.session.execute.return_value = {'downloads': [{'id':'one','name':'x.TORRENT','size':10},{'id':'two','name':'x.pdf','size':10}]}
        state = self.client.get('/profiles/1/items/2/safe/session/state').json
        self.assertEqual(state['downloads'][0]['downloaders'][0]['label'], 'Send to Home')
        self.assertEqual(state['downloads'][1]['downloaders'], [])
        self.assertNotIn('secret', json.dumps(state))
        self.assertNotIn('base_url', json.dumps(state))


class MetadataAndAdapterTests(unittest.TestCase):
    def test_magnet_v1_hex_base32_v2_and_hybrid(self):
        v1, v2 = 'ab' * 20, 'cd' * 32
        hex_uri = 'magnet:?xt=urn:btih:' + v1 + '&dn=My%20download&tr=https%3A%2F%2Ftracker.example%2Fannounce'
        magnet = adapters.parse_magnet(hex_uri)
        self.assertEqual(magnet['name'], 'My download')
        self.assertEqual(magnet['uri'], hex_uri)
        self.assertEqual(magnet['hashes'], [v1])
        b32 = base64.b32encode(bytes.fromhex(v1)).decode()
        self.assertEqual(adapters.parse_magnet('magnet:?xt=urn:btih:' + b32)['fingerprint'], magnet['fingerprint'])
        self.assertEqual(adapters.parse_magnet('magnet:?xt=urn:btmh:1220' + v2)['hashes'], sorted([v2[:40], v2]))
        hybrid = adapters.parse_magnet('magnet:?xt=urn:btih:' + v1 + '&xt=urn:btmh:1220' + v2)
        self.assertEqual(hybrid['hashes'], sorted([v1, v2[:40], v2]))

    def test_invalid_magnets_reject_empty_hashes_and_multiple_url_injection(self):
        valid = 'magnet:?xt=urn:btih:' + 'a' * 40
        for uri in ['', 'https://example.com', 'magnet:?dn=Missing', 'magnet:?xt=urn:btih:bad',
                    'magnet:?xt=urn:btmh:1220abc', valid + '\nhttps://evil.example',
                    valid + '&dn=%0Ahttps://evil.example', valid + '#fragment', valid + '&dn=' + 'x' * 16384,
                    valid + '&a=b' * 129]:
            with self.subTest(uri=uri[:70]), self.assertRaises(ValueError):
                adapters.parse_magnet(uri)

    def test_magnet_adapter_preserves_uri_category_and_version_start_field(self):
        uri = 'magnet:?xt=urn:btih:' + 'a' * 40 + '&tr=https%3A%2F%2Ftracker.example%2Fannounce'
        adapter = object.__new__(adapters.WebAPIv2Downloader)
        adapter.call = Mock(return_value=b'Ok.')
        for version, field in [('4.6.7', 'paused'), ('v5.0.0', 'stopped')]:
            adapter.version = version
            adapter.add(uri, 'Movies', False)
            adapter.call.assert_called_with('torrents/add', {'urls': uri, 'category': 'Movies', 'autoTMM': 'true', field: 'true'})
        adapter.call.return_value = b'Fails.'
        with self.assertRaises(adapters.SubmissionRejected):
            adapter.add(uri, 'Movies', True)

    def test_categories_allowlist_and_invalid_responses(self):
        adapter = object.__new__(adapters.WebAPIv2Downloader)
        adapter.profile = {'allowed_categories': '["Movies"]'}
        adapter.call = Mock(return_value=b'{"Movies":{"savePath":"/movies"},"Other":{"savePath":"/other"}}')
        self.assertEqual(adapter.categories(), [{'name':'Movies','save_path':'/movies'}])
        adapter.call.return_value = b'{"Movies": "invalid"}'
        with self.assertRaises(ValueError):
            adapter.categories()

    def test_v1_and_v2_exact_info_hashes(self):
        data = metadata_file()
        info = bencode({b'name': b'Example', b'length': 1, b'piece length': 16384, b'pieces': b'x'*20})
        fingerprint, hashes = adapters.file_identity(data)
        self.assertEqual(fingerprint, hashlib.sha256(info).hexdigest())
        self.assertEqual(hashes, [hashlib.sha1(info).hexdigest()])
        v2 = {b'name': b'Example', b'piece length': 16384, b'meta version': 2, b'file tree': {b'Example': {b'': {b'length': 0}}}}
        _, hashes = adapters.file_identity(bencode({b'info': v2}))
        self.assertEqual(hashes, [hashlib.sha256(bencode(v2)).hexdigest()[:40], hashlib.sha256(bencode(v2)).hexdigest()])

    def test_invalid_and_oversized_metadata(self):
        for value in [b'', b'not metadata', b'd4:infodee', metadata_file()+b'junk', b'x'*(d.MAX_SUBMISSION_BYTES+1)]:
            with self.assertRaises(ValueError):
                adapters.file_identity(value)

    def test_multipart_category_tmm_and_version_start_field(self):
        adapter = object.__new__(adapters.WebAPIv2Downloader)
        adapter.call = Mock(return_value=b'Ok.')
        for version, field in [('4.6.7', b'name="paused"'), ('v5.0.0', b'name="stopped"')]:
            adapter.version = version
            adapter.add(metadata_file(), 'Movies', False)
            body = adapter.call.call_args.args[1]
            self.assertIn(field, body)
            self.assertIn(b'name="autoTMM"\r\n\r\ntrue', body)
            self.assertIn(b'name="category"\r\n\r\nMovies', body)
            self.assertNotIn(b'name="savepath"', body)

    def test_login_api_key_env_secret_and_no_redirects(self):
        key = Fernet.generate_key()
        profile = dict(base_url='https://example.com/downloader', verify_tls=True, ca_path='', auth_mode='password', username='admin', secret=Fernet(key).encrypt(b'password').decode(), secret_env='', timeout=15)
        with patch.object(adapters.WebAPIv2Downloader, 'call', side_effect=[b'Ok.',b'v5.0.0',b'2.11.0']) as call:
            adapter = adapters.WebAPIv2Downloader(profile, key)
            self.assertEqual(call.call_args_list[0].args[1], {'username':'admin','password':'password'})
            self.assertEqual(adapter.headers['Origin'], 'https://example.com')
        with patch.dict('os.environ', {'TEST_DOWNLOADER_KEY':'test_secret'}), patch.object(adapters.WebAPIv2Downloader, 'call', side_effect=[b'v5.2.0',b'2.14.1']):
            adapter = adapters.WebAPIv2Downloader(profile | {'auth_mode':'api_key','secret_env':'TEST_DOWNLOADER_KEY'}, key)
            self.assertEqual(adapter.headers['Authorization'], 'Bearer test_secret')
        self.assertIsNone(adapters.NoRedirect().redirect_request(None,None,302,'',{},'https://evil.example'))
        with patch.object(adapters.WebAPIv2Downloader, 'call', return_value=b'Fails.'), self.assertRaises(ValueError):
            adapters.WebAPIv2Downloader(profile, key)
