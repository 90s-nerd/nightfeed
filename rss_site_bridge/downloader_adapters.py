"""Protocol adapters for optional download destinations."""
from __future__ import annotations
from hashlib import sha1, sha256
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPCookieProcessor, HTTPSHandler, HTTPRedirectHandler
import json
import os
import re
import secrets
import ssl
import base64
from cryptography.fernet import Fernet, InvalidToken

MAX_SUBMISSION_BYTES = 10 * 1024 * 1024
MAX_MAGNET_LENGTH = 16384


def parse_magnet(uri):
    """Validate one bounded BitTorrent magnet and normalize its torrent identifiers."""
    if not isinstance(uri, str) or not uri or len(uri) > MAX_MAGNET_LENGTH or any(ord(c) < 32 or ord(c) == 127 for c in uri):
        raise ValueError('Invalid or overly long magnet link.')
    parsed = urlsplit(uri)
    if parsed.scheme.lower() != 'magnet' or parsed.netloc or parsed.path or parsed.fragment or not parsed.query:
        raise ValueError('A BitTorrent magnet link is required.')
    try:
        pairs = parse_qsl(parsed.query, max_num_fields=128)
    except ValueError:
        raise ValueError('Magnet link has too many parameters.') from None
    hashes, name = [], ''
    for key, value in pairs:
        if any(ord(c) < 32 or ord(c) == 127 for c in key + value):
            raise ValueError('Magnet link contains invalid characters.')
        if key == 'dn' and not name:
            name = value[:200].strip()
        if key != 'xt':
            continue
        value = value.lower()
        if value.startswith('urn:btih:'):
            digest = value[9:]
            if re.fullmatch(r'[0-9a-f]{40}', digest):
                hashes.append(digest)
            elif re.fullmatch(r'[a-z2-7]{32}', digest):
                hashes.append(base64.b32decode(digest.upper()).hex())
            else:
                raise ValueError('Magnet link has an invalid v1 info hash.')
        elif value.startswith('urn:btmh:'):
            digest = value[9:]
            if not re.fullmatch(r'1220[0-9a-f]{64}', digest):
                raise ValueError('Magnet link has an invalid v2 info hash.')
            hashes.extend([digest[4:44], digest[4:]])
    hashes = sorted(set(hashes))
    if not hashes:
        raise ValueError('Magnet link must contain a supported BitTorrent info hash.')
    fingerprint = 'magnet:' + sha256('|'.join(hashes).encode()).hexdigest()
    return {'uri': uri, 'name': name or 'Magnet ' + hashes[0][:12], 'fingerprint': fingerprint, 'hashes': hashes}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class SubmissionRejected(ValueError):
    """The remote server explicitly rejected the request; it can be retried."""


class WebAPIv2Downloader:
    extensions = ('.torrent',)
    supports_magnets = True

    @staticmethod
    def identify(data):
        if isinstance(data, str):
            magnet = parse_magnet(data)
            return magnet['fingerprint'], magnet['hashes']
        return file_identity(data)

    def __init__(self, profile, key):
        self.profile = profile
        self.base = profile['base_url'] + '/'
        if profile['verify_tls']:
            try:
                context = ssl.create_default_context(cafile=profile['ca_path'] or None)
            except (OSError, ssl.SSLError):
                raise ValueError('Custom CA certificate could not be loaded on the Nightfeed server.')
        else:
            context = ssl._create_unverified_context()
        self.opener = build_opener(NoRedirect(), HTTPSHandler(context=context), HTTPCookieProcessor(CookieJar()))
        origin = urlsplit(self.base)
        self.headers = {'Referer': self.base, 'Origin': origin.scheme + '://' + origin.netloc, 'User-Agent': 'Nightfeed'}
        secret = ''
        if profile['auth_mode'] != 'none':
            if profile['secret_env']:
                secret = os.environ.get(profile['secret_env'], '')
                if not secret:
                    raise ValueError('The configured secret environment variable is missing or empty.')
            elif profile['secret']:
                try:
                    secret = Fernet(key).decrypt(profile['secret'].encode()).decode()
                except InvalidToken:
                    raise ValueError('Downloader secret cannot be decrypted. Restore the encryption key or re-enter the secret.')
        if profile['auth_mode'] == 'api_key':
            self.headers['Authorization'] = 'Bearer ' + secret
        elif profile['auth_mode'] == 'password':
            if self.call('auth/login', {'username': profile['username'], 'password': secret}).strip() != b'Ok.':
                raise ValueError('Downloader rejected the username or password.')
        self.version = self.call('app/version').decode('utf-8', 'replace').strip()
        self.api_version = self.call('app/webapiVersion').decode('utf-8', 'replace').strip()
        if not re.fullmatch(r'2\.[0-9]+(?:\.[0-9]+)?', self.api_version):
            raise ValueError('This integration requires Web API v2. Check the selected downloader type and server version.')
        if not re.match(r'^v?[0-9]+\.', self.version):
            raise ValueError('The configured URL did not return a valid downloader version. Check its base URL and reverse proxy.')

    def call(self, endpoint, data=None, content_type=None):
        body = urlencode(data).encode() if isinstance(data, dict) else data
        headers = dict(self.headers)
        if body is not None:
            headers['Content-Type'] = content_type or 'application/x-www-form-urlencoded'
        req = Request(self.base + 'api/v2/' + endpoint, data=body, headers=headers)
        try:
            with self.opener.open(req, timeout=self.profile['timeout']) as response:
                result = response.read(2 * 1024 * 1024 + 1)
                if len(result) > 2 * 1024 * 1024:
                    raise ValueError('Downloader response exceeded the size limit.')
                return result
        except HTTPError as exc:
            messages = {401: 'Downloader authentication failed.', 403: 'Downloader denied access; check credentials, IP bans and WebUI host/origin settings.', 415: 'Downloader rejected the file.'}
            error_type = SubmissionRejected if 400 <= exc.code < 500 else ValueError
            raise error_type(messages.get(exc.code, f'Downloader returned HTTP {exc.code}. Redirects are not followed; use the final base URL.')) from None
        except ssl.SSLError:
            raise ValueError('Downloader TLS verification failed. Configure a trusted CA certificate.') from None
        except (URLError, OSError, TimeoutError):
            raise ValueError('Downloader connection failed or timed out. Check its address, network and TLS settings.') from None

    def categories(self):
        try:
            result = json.loads(self.call('torrents/categories'))
            if not isinstance(result, dict):
                raise ValueError()
            allowed = json.loads(self.profile['allowed_categories'])
            if any(not isinstance(name, str) or not isinstance(value, dict) for name, value in result.items()):
                raise ValueError('Downloader returned invalid categories.')
            return [{'name': name, 'save_path': str(value.get('savePath', ''))} for name, value in sorted(result.items()) if not allowed or name in allowed]
        except (TypeError, json.JSONDecodeError, AttributeError):
            raise ValueError('Downloader returned invalid categories.') from None

    def contains(self, hashes):
        try:
            rows = json.loads(self.call('torrents/info?' + urlencode({'hashes': '|'.join(hashes)})))
            if not isinstance(rows, list):
                raise ValueError('Downloader returned an invalid download list.')
            return any(row.get('hash', '').lower() in hashes or row.get('infohash_v1', '').lower() in hashes or row.get('infohash_v2', '').lower() in hashes for row in rows)
        except (json.JSONDecodeError, AttributeError):
            raise ValueError('Downloader returned an invalid download list.') from None

    def add(self, data, category, start):
        boundary = 'Nightfeed' + secrets.token_hex(16)
        fields = {'category': category, 'autoTMM': 'true', 'stopped' if self.version.lstrip('v').split('.')[0].isdigit() and int(self.version.lstrip('v').split('.')[0]) >= 5 else 'paused': str(not start).lower()}
        if isinstance(data, str):
            fields['urls'] = parse_magnet(data)['uri']
            response = self.call('torrents/add', fields)
            if response.strip() not in {b'Ok.', b''}:
                raise SubmissionRejected('Downloader did not accept the magnet link.')
            return
        parts = []
        for name, value in fields.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="torrents"; filename="download.torrent"\r\nContent-Type: application/x-bittorrent\r\n\r\n'.encode() + data + b'\r\n')
        parts.append(f'--{boundary}--\r\n'.encode())
        response = self.call('torrents/add', b''.join(parts), 'multipart/form-data; boundary=' + boundary)
        if response.strip() not in {b'Ok.', b''}:
            raise SubmissionRejected('Downloader did not accept the file.')


ADAPTERS = {'qbittorrent': WebAPIv2Downloader}
ADAPTER_LABELS = {'qbittorrent': 'qBittorrent'}


def file_identity(data):
    """Validate bounded bencode and hash the exact info bytes, including version 2 metadata."""
    if not data or len(data) > MAX_SUBMISSION_BYTES:
        raise ValueError('File must be nonempty and no larger than 10 MB.')
    position, info_span, budget = 0, None, 200000

    def parse(depth=0):
        nonlocal position, info_span, budget
        budget -= 1
        if depth > 64 or budget < 0 or position >= len(data):
            raise ValueError('Invalid or overly complex download metadata.')
        token = data[position:position+1]
        if token == b'i':
            end = data.index(b'e', position)
            raw = data[position+1:end]
            if not re.fullmatch(rb'0|-?[1-9][0-9]*', raw) or len(raw) > 30:
                raise ValueError('Invalid metadata integer.')
            position = end + 1
            return int(raw)
        if token in {b'l', b'd'}:
            position += 1
            result = [] if token == b'l' else {}
            while position < len(data) and data[position:position+1] != b'e':
                if token == b'l':
                    result.append(parse(depth+1))
                else:
                    key = parse(depth+1)
                    if not isinstance(key, bytes) or key in result:
                        raise ValueError('Invalid metadata dictionary.')
                    start = position
                    result[key] = parse(depth+1)
                    if depth == 0 and key == b'info':
                        info_span = (start, position)
            if position >= len(data):
                raise ValueError('Incomplete download metadata.')
            position += 1
            return result
        colon = data.index(b':', position)
        raw = data[position:colon]
        if not re.fullmatch(rb'0|[1-9][0-9]*', raw) or len(raw) > 8:
            raise ValueError('Invalid metadata string.')
        length = int(raw)
        position = colon + 1
        if position + length > len(data):
            raise ValueError('Incomplete metadata string.')
        value = data[position:position+length]
        position += length
        return value

    try:
        metadata = parse()
        info = metadata.get(b'info') if isinstance(metadata, dict) else None
        if position != len(data) or not isinstance(info, dict) or not info_span or not isinstance(info.get(b'name'), bytes) or not info[b'name']:
            raise ValueError('File does not contain valid download metadata.')
        if not isinstance(info.get(b'piece length'), int) or info[b'piece length'] <= 0:
            raise ValueError('File has no valid piece length.')
        raw_info = data[info_span[0]:info_span[1]]
        hashes = []
        if b'pieces' in info:
            if not isinstance(info[b'pieces'], bytes) or len(info[b'pieces']) % 20 or not (b'length' in info or b'files' in info):
                raise ValueError('Invalid v1 download metadata.')
            # BitTorrent v1 defines this identifier as SHA-1; it is not an authentication primitive.
            hashes.append(sha1(raw_info, usedforsecurity=False).hexdigest())
        if info.get(b'meta version') == 2 and isinstance(info.get(b'file tree'), dict):
            hashes.extend([sha256(raw_info).hexdigest()[:40], sha256(raw_info).hexdigest()])
        if not hashes:
            raise ValueError('File does not contain supported v1/v2 download metadata.')
        return sha256(raw_info).hexdigest(), hashes
    except (IndexError, TypeError, AttributeError, OverflowError):
        raise ValueError('File does not contain valid download metadata.') from None
