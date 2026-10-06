"""Bounded public-page fetching with DNS pinned to the validated addresses."""
from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import time
from urllib.parse import urljoin, urlsplit

MAX_BYTES = 2 * 1024 * 1024


def public_addresses(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Use a public HTTP or HTTPS URL without credentials.')
    if parsed.port not in (None, 80, 443):
        raise ValueError('Source pages must use port 80 or 443.')
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80), type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError('The source hostname could not be resolved.') from exc
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError('Source pages must resolve only to public addresses.')
    return parsed, addresses


def fetch_public(url, *, html_only=True):
    """No proxies, cookies, credential forwarding, or second DNS lookup at connect."""
    deadline = time.monotonic() + 25
    for _ in range(6):
        parsed, addresses = public_addresses(url)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError('Source request timed out.')
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=min(10, remaining))
        sock = None
        try:
            for family, socktype, proto, _, target in addresses:
                remaining = deadline - time.monotonic()
                if remaining <= 0: raise ValueError('Source request timed out.')
                candidate = socket.socket(family, socktype, proto)
                candidate.settimeout(min(10, remaining))
                try:
                    candidate.connect(target)
                    sock = candidate
                    break
                except OSError:
                    candidate.close()
            if sock is None:
                raise ValueError('Could not connect to the source page.')
            if parsed.scheme == 'https':
                remaining = deadline - time.monotonic()
                if remaining <= 0: raise ValueError('Source request timed out.')
                sock.settimeout(min(10, remaining))
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parsed.hostname)
            connection.sock = sock
            path = parsed.path or '/'
            if parsed.query:
                path += '?' + parsed.query
            connection.request('GET', path, headers={'User-Agent': 'Nightfeed/assistant', 'Accept-Encoding': 'identity'})
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader('Location')
                if not location:
                    raise ValueError('Source redirect has no destination.')
                url = urljoin(url, location)
                continue
            if not 200 <= response.status < 300:
                raise ValueError(f'Source returned HTTP {response.status}.')
            mime = response.getheader('Content-Type', '').split(';')[0].lower()
            if html_only and mime not in ('text/html', 'application/xhtml+xml'):
                raise ValueError('The source did not return HTML.')
            chunks, size = [], 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError('Source request timed out.')
                sock.settimeout(min(10, remaining))
                chunk = response.read(min(65536, MAX_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError('Source exceeded the 2 MB limit.')
            headers = {k.lower(): v for k, v in response.getheaders() if k.lower() in ('content-type', 'access-control-allow-origin')}
            charset = response.headers.get_content_charset() or 'utf-8'
            return b''.join(chunks), url, headers, charset
        except (OSError, http.client.HTTPException) as exc:
            raise ValueError('Source request failed or timed out.') from exc
        finally:
            connection.close()
            if sock is not None:
                sock.close()
    raise ValueError('The source redirected too many times.')


def fetch_document(url, mode='http'):
    from .app import FetchedDocument
    if mode == 'http':
        data, final, _, charset = fetch_public(url)
        return FetchedDocument(data.decode(charset, errors='replace'), final)
    if mode != 'browser':
        raise ValueError('Fetch mode must be http or browser.')
    initial, final_url, _, charset = fetch_public(url)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ValueError('Browser extraction requires the browser extra and Chromium.') from exc
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(service_workers='block', accept_downloads=False)
                page = context.new_page()
                deadline, count = time.monotonic() + 35, 0

                def route_request(route):
                    nonlocal count
                    count += 1
                    if count > 80 or time.monotonic() > deadline or route.request.method != 'GET' or route.request.resource_type in ('image', 'media', 'font'):
                        route.abort()
                        return
                    try:
                        if route.request.is_navigation_request() and route.request.url == final_url:
                            route.fulfill(status=200, content_type='text/html; charset=utf-8', body=initial.decode(charset, errors='replace'))
                            return
                        data, _, headers, _ = fetch_public(route.request.url, html_only=False)
                        route.fulfill(status=200, headers=headers, body=data)
                    except ValueError:
                        route.abort()

                context.route('**/*', route_request)
                # WebSockets bypass HTTP routes; never allow them during inspection.
                context.route_web_socket('**/*', lambda ws: ws.close())
                page.goto(final_url, wait_until='domcontentloaded', timeout=35000)
                page.wait_for_timeout(1000)
                public_addresses(page.url)
                return FetchedDocument(page.content(), page.url)
            finally:
                browser.close()
    except Exception as exc:
        raise ValueError('Browser inspection failed. Check Chromium availability or try HTTP mode.') from exc
