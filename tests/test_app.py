from datetime import datetime, timedelta, timezone
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import smtplib
import sqlite3
import sys
import types
import unittest
from unittest.mock import patch

try:
    import flask  # noqa: F401
except ModuleNotFoundError:
    flask = None

from rss_site_bridge.app import (
    FeedEntry,
    FetchedDocument,
    FeedRequest,
    AppSettings,
    build_clone_title,
    classify_refresh_error,
    count_unread_notifications,
    create_app,
    create_notification,
    create_profile,
    delete_profile,
    extract_feed_entries,
    fetch_html_browser,
    get_app_settings,
    get_next_refresh_at,
    get_profile_by_token,
    get_profile_by_id,
    humanize_datetime,
    humanize_next_refresh,
    init_db,
    is_safe_browser_url,
    list_feed_items,
    list_notifications,
    list_profiles,
    normalize_source_url,
    normalize_topic_link,
    parse_timezone_name,
    refresh_due_profiles,
    refresh_profile,
    send_test_email,
    render_rss,
    set_profile_active,
    should_refresh,
    update_profile,
)


class AppTestCase(unittest.TestCase):
    def test_rejects_non_http_source_url(self):
        with self.assertRaises(ValueError):
            normalize_source_url("javascript:alert(1)")

    def test_rejects_offsite_topic_links(self):
        with self.assertRaises(ValueError):
            normalize_topic_link("https://example.com/forum", "https://evil.test/topic")

    def test_safe_browser_url_rejects_local_and_private_targets(self):
        self.assertTrue(is_safe_browser_url("https://example.com/topic"))
        self.assertFalse(is_safe_browser_url("http://localhost/admin"))
        self.assertFalse(is_safe_browser_url("http://127.0.0.1/admin"))
        self.assertFalse(is_safe_browser_url("http://192.168.1.1/"))
        self.assertFalse(is_safe_browser_url("file:///etc/passwd"))

    def test_rss_contains_channel_title(self):
        config = FeedRequest(
            feed_title="Forum Feed",
            source_url="https://example.com/forum",
            item_selector=".topic",
            title_selector="a",
            link_selector="a",
            summary_selector="",
            max_items=10,
            refresh_interval_minutes=60,
            fetch_mode="http",
        )
        xml_payload = render_rss(config, [])
        self.assertIn("<title>Forum Feed</title>", xml_payload)

    def test_extract_feed_entries_supports_scope_for_anchor_items(self):
        config = FeedRequest(
            feed_title="Forum Feed",
            source_url="https://example.com/forum",
            item_selector="a[href*='/forums/topic/']",
            title_selector=":scope",
            link_selector=":scope",
            summary_selector="",
            max_items=10,
            refresh_interval_minutes=60,
            fetch_mode="http",
        )
        html = """
        <html>
          <body>
            <div class="recent">
              <a href="/forums/topic/alpha">Alpha Topic</a>
              <a href="/forums/topic/beta">Beta Topic</a>
            </div>
          </body>
        </html>
        """

        with patch("rss_site_bridge.app.fetch_html", return_value=html):
            entries = extract_feed_entries(config)

        self.assertEqual(2, len(entries))
        self.assertEqual("Alpha Topic", entries[0].title)
        self.assertEqual("https://example.com/forums/topic/alpha", entries[0].link)

    def test_extract_feed_entries_supports_container_with_repeated_links(self):
        config = FeedRequest(
            feed_title="Forum Feed",
            source_url="https://example.com/forum",
            item_selector=".banger-container p",
            title_selector="a[href*='/forums/topic/']",
            link_selector="a[href*='/forums/topic/']",
            summary_selector="",
            max_items=10,
            refresh_interval_minutes=60,
            fetch_mode="http",
        )
        html = """
        <html>
          <body>
            <div class="banger-container">
              <p>
                <a href="/forums/topic/alpha">Alpha Topic</a>
                <a href="/forums/topic/beta">Beta Topic</a>
                <a href="/forums/topic/gamma">Gamma Topic</a>
              </p>
            </div>
          </body>
        </html>
        """

        with patch("rss_site_bridge.app.fetch_html", return_value=html):
            entries = extract_feed_entries(config)

        self.assertEqual(3, len(entries))
        self.assertEqual("Alpha Topic", entries[0].title)
        self.assertEqual("Beta Topic", entries[1].title)
        self.assertEqual("Gamma Topic", entries[2].title)

    def test_extract_feed_entries_applies_text_and_wildcard_filters(self):
        config = FeedRequest(
            feed_title="Forum Feed",
            source_url="https://example.com/forum",
            item_selector=".topic",
            title_selector="a",
            link_selector="a",
            summary_selector="",
            max_items=10,
            refresh_interval_minutes=60,
            fetch_mode="http",
            filter_rules="premium\nbuild-2?*",
        )
        html = """
        <html>
          <body>
            <article class="topic"><a href="/forums/topic/1">Product Alpha Premium Edition</a></article>
            <article class="topic"><a href="/forums/topic/2">Toolkit build-21 candidate</a></article>
            <article class="topic"><a href="/forums/topic/3">Generic baseline package</a></article>
          </body>
        </html>
        """

        with patch("rss_site_bridge.app.fetch_html", return_value=html):
            entries = extract_feed_entries(config)

        self.assertEqual(2, len(entries))
        self.assertEqual("Product Alpha Premium Edition", entries[0].title)
        self.assertEqual("Toolkit build-21 candidate", entries[1].title)

    def test_extract_feed_entries_supports_boolean_filter_expressions(self):
        config = FeedRequest(
            feed_title="Forum Feed",
            source_url="https://example.com/forum",
            item_selector=".topic",
            title_selector="a",
            link_selector="a",
            summary_selector="",
            max_items=10,
            refresh_interval_minutes=60,
            fetch_mode="http",
            filter_rules="(alpha OR beta OR gamma) AND premium",
        )
        html = """
        <html>
          <body>
            <article class="topic"><a href="/forums/topic/1">Package Alpha Premium</a></article>
            <article class="topic"><a href="/forums/topic/2">Package Beta Trial</a></article>
            <article class="topic"><a href="/forums/topic/3">Bundle Gamma Premium</a></article>
            <article class="topic"><a href="/forums/topic/4">Bundle Delta Premium</a></article>
          </body>
        </html>
        """

        with patch("rss_site_bridge.app.fetch_html", return_value=html):
            entries = extract_feed_entries(config)

        self.assertEqual(2, len(entries))
        self.assertEqual("Package Alpha Premium", entries[0].title)
        self.assertEqual("Bundle Gamma Premium", entries[1].title)

    def test_extract_feed_entries_supports_wildcards_inside_boolean_filters(self):
        config = FeedRequest(
            feed_title="Forum Feed",
            source_url="https://example.com/forum",
            item_selector=".topic",
            title_selector="a",
            link_selector="a",
            summary_selector="",
            max_items=10,
            refresh_interval_minutes=60,
            fetch_mode="http",
            filter_rules="(alpha* OR gamma) AND premium",
        )
        html = """
        <html>
          <body>
            <article class="topic"><a href="/forums/topic/1">alpha-release premium</a></article>
            <article class="topic"><a href="/forums/topic/2">gamma premium</a></article>
            <article class="topic"><a href="/forums/topic/3">delta premium</a></article>
          </body>
        </html>
        """

        with patch("rss_site_bridge.app.fetch_html", return_value=html):
            entries = extract_feed_entries(config)

        self.assertEqual(2, len(entries))
        self.assertEqual("alpha-release premium", entries[0].title)
        self.assertEqual("gamma premium", entries[1].title)

    def test_extract_feed_entries_applies_exclude_filters_after_include_filters(self):
        config = FeedRequest(
            feed_title="Forum Feed",
            source_url="https://example.com/forum",
            item_selector=".topic",
            title_selector="a",
            link_selector="a",
            summary_selector="",
            max_items=10,
            refresh_interval_minutes=60,
            fetch_mode="http",
            filter_rules="(alpha OR beta OR gamma) AND premium",
            exclude_filter_rules="draft OR preview",
        )
        html = """
        <html>
          <body>
            <article class="topic"><a href="/forums/topic/1">Package Alpha Premium</a></article>
            <article class="topic"><a href="/forums/topic/2">Package Beta Premium Preview</a></article>
            <article class="topic"><a href="/forums/topic/3">Bundle Gamma Premium Draft</a></article>
          </body>
        </html>
        """

        with patch("rss_site_bridge.app.fetch_html", return_value=html):
            entries = extract_feed_entries(config)

        self.assertEqual(1, len(entries))
        self.assertEqual("Package Alpha Premium", entries[0].title)

    def test_extract_feed_entries_rejects_invalid_boolean_filter_expression(self):
        config = FeedRequest(
            feed_title="Forum Feed",
            source_url="https://example.com/forum",
            item_selector=".topic",
            title_selector="a",
            link_selector="a",
            summary_selector="",
            max_items=10,
            refresh_interval_minutes=60,
            fetch_mode="http",
            filter_rules="(alpha OR premium",
        )
        html = """
        <html>
          <body>
            <article class="topic"><a href="/forums/topic/1">Package Alpha Premium</a></article>
          </body>
        </html>
        """

        with patch("rss_site_bridge.app.fetch_html", return_value=html):
            with self.assertRaises(ValueError):
                extract_feed_entries(config)

    def test_extract_feed_entries_combines_inline_prefix_text_for_repeated_links(self):
        config = FeedRequest(
            feed_title="Forum Feed",
            source_url="https://example.com/forum",
            item_selector=".banger-container p",
            title_selector="a[href*='/forums/topic/']",
            link_selector="a[href*='/forums/topic/']",
            summary_selector="",
            max_items=10,
            refresh_interval_minutes=60,
            fetch_mode="http",
        )
        html = """
        <html>
          <body>
            <div class="banger-container">
              <p>
                <a href="/forums/topic/alpha">Alpha Topic</a><br>
                Bundle Release Candidate -
                <a href="/forums/topic/beta">[Premium Tier &amp; Stable Build]</a><br>
                <a href="/forums/topic/gamma">Gamma Topic</a>
              </p>
            </div>
          </body>
        </html>
        """

        with patch("rss_site_bridge.app.fetch_html", return_value=html):
            entries = extract_feed_entries(config)

        self.assertEqual(3, len(entries))
        self.assertEqual(
            "Bundle Release Candidate - [Premium Tier & Stable Build]",
            entries[1].title,
        )

    def test_refresh_profile_persists_items(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            init_db(db_path)
            profile = create_profile(
                db_path,
                FeedRequest(
                    feed_title="Forum Feed",
                    source_url="https://example.com/forum",
                    item_selector=".topic",
                    title_selector="a",
                    link_selector="a",
                    summary_selector=".summary",
                    max_items=10,
                    refresh_interval_minutes=60,
                    fetch_mode="http",
                ),
            )

            now = datetime.now(timezone.utc)
            entries = [
                FeedEntry(
                    title="Topic A",
                    link="https://example.com/forum/topic-a",
                    summary="Summary A",
                    published_at=now,
                ),
                FeedEntry(
                    title="Topic B",
                    link="https://example.com/forum/topic-b",
                    summary="Summary B",
                    published_at=now,
                ),
            ]

            with patch("rss_site_bridge.app.extract_feed_entries", return_value=entries):
                refresh_profile(db_path, profile.id)

            stored_entries = list_feed_items(db_path, profile.id, 10)
            self.assertEqual(2, len(stored_entries))
            self.assertEqual("Topic A", stored_entries[0].title)
            self.assertEqual("Topic B", stored_entries[1].title)

    def test_extract_feed_entries_uses_final_redirect_url_for_relative_links(self):
        config = FeedRequest(
            feed_title="Forum Feed",
            source_url="https://old.example/forum",
            item_selector=".topic",
            title_selector="a",
            link_selector="a",
            summary_selector="",
            max_items=10,
            refresh_interval_minutes=60,
            fetch_mode="http",
        )
        html = """
        <html>
          <body>
            <article class="topic"><a href="/forums/topic/alpha">Alpha Topic</a></article>
          </body>
        </html>
        """

        with patch(
            "rss_site_bridge.app.fetch_html",
            return_value=FetchedDocument(html=html, final_url="https://new.example/forum"),
        ):
            entries = extract_feed_entries(config)

        self.assertEqual(1, len(entries))
        self.assertEqual("https://new.example/forums/topic/alpha", entries[0].link)
        self.assertEqual("https://new.example/forum", config.source_url)

    def test_fetch_html_browser_allows_same_origin_requests(self):
        class FakeResponse:
            ok = True
            status = 200
            status_text = "OK"

        class FakeRoute:
            def __init__(self, url: str):
                self.request = types.SimpleNamespace(resource_type="document", url=url)
                self.continued = False
                self.aborted = False

            def abort(self):
                self.aborted = True

            def continue_(self):
                self.continued = True

        class FakePage:
            def __init__(self):
                self.url = "https://example.com/forum"
                self._handler = None

            def set_default_navigation_timeout(self, _timeout: int):
                return None

            def set_default_timeout(self, _timeout: int):
                return None

            def route(self, _pattern: str, handler):
                self._handler = handler

            def on(self, _event: str, _handler):
                return None

            def goto(self, _url: str, wait_until: str):
                self._handler(FakeRoute("https://example.com/forum"))
                return FakeResponse()

            def wait_for_timeout(self, _timeout: int):
                return None

            def content(self) -> str:
                return "<html><body>ok</body></html>"

        class FakeContext:
            def __init__(self):
                self.page = FakePage()

            def new_page(self):
                return self.page

            def close(self):
                return None

        class FakeBrowser:
            def __init__(self):
                self.context = FakeContext()

            def new_context(self, **_kwargs):
                return self.context

            def close(self):
                return None

        class FakeChromium:
            def launch(self, headless: bool):
                return FakeBrowser()

        class FakePlaywright:
            chromium = FakeChromium()

        class FakePlaywrightManager:
            def __enter__(self):
                return FakePlaywright()

            def __exit__(self, exc_type, exc, tb):
                return False

        fake_sync_api = types.ModuleType("playwright.sync_api")
        fake_sync_api.Error = RuntimeError
        fake_sync_api.sync_playwright = lambda: FakePlaywrightManager()
        fake_playwright = types.ModuleType("playwright")
        fake_playwright.sync_api = fake_sync_api

        with patch.dict(sys.modules, {"playwright": fake_playwright, "playwright.sync_api": fake_sync_api}):
            fetched = fetch_html_browser("https://example.com/forum")

        self.assertEqual("<html><body>ok</body></html>", fetched.html)
        self.assertEqual("https://example.com/forum", fetched.final_url)

    def test_refresh_profile_persists_canonical_source_url_after_redirect(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            init_db(db_path)
            profile = create_profile(
                db_path,
                FeedRequest(
                    feed_title="Forum Feed",
                    source_url="https://old.example/forum",
                    item_selector=".topic",
                    title_selector="a",
                    link_selector="a",
                    summary_selector="",
                    max_items=10,
                    refresh_interval_minutes=60,
                    fetch_mode="http",
                ),
            )

            now = datetime.now(timezone.utc)
            entries = [
                FeedEntry(
                    title="Topic A",
                    link="https://new.example/forum/topic-a",
                    summary="",
                    published_at=now,
                )
            ]

            with patch(
                "rss_site_bridge.app.extract_feed_entries",
                side_effect=lambda request_config: (
                    setattr(request_config, "source_url", "https://new.example/forum") or entries
                ),
            ):
                refresh_profile(db_path, profile.id)

            updated = get_profile_by_id(db_path, profile.id)
            self.assertIsNotNone(updated)
            assert updated is not None
            self.assertEqual("https://new.example/forum", updated.source_url)

    def test_refresh_profile_sends_success_notification_when_smtp_enabled(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            init_db(db_path)
            profile = create_profile(
                db_path,
                FeedRequest(
                    feed_title="Forum Feed",
                    source_url="https://example.com/forum",
                    item_selector=".topic",
                    title_selector="a",
                    link_selector="a",
                    summary_selector="",
                    max_items=10,
                    refresh_interval_minutes=60,
                    fetch_mode="http",
                    notify_on_success=True,
                ),
            )
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute(
                    """
                    UPDATE app_settings
                    SET smtp_enabled = 1, smtp_host = 'smtp.example.com', smtp_port = 587, smtp_username = 'alerts@example.com',
                        smtp_password = 'secret', smtp_use_tls = 1, smtp_to_email = 'user@example.com',
                        smtp_from_email = 'Nightfeed <noreply@example.com>'
                    WHERE id = 1
                    """
                )
                conn.commit()

            entries = [
                FeedEntry(
                    title="Topic A",
                    link="https://example.com/forum/topic-a",
                    summary="",
                    published_at=datetime.now(timezone.utc),
                )
            ]

            with patch("rss_site_bridge.app.extract_feed_entries", return_value=entries), patch(
                "rss_site_bridge.app.maybe_send_refresh_notification"
            ) as mocked_notify:
                refresh_profile(db_path, profile.id)

            mocked_notify.assert_called_once()
            _, kwargs = mocked_notify.call_args
            self.assertEqual("ok", kwargs["status"])
            self.assertEqual(1, kwargs["entry_count"])
            self.assertEqual("https://example.com/forum", kwargs["source_url"])

    def test_refresh_profile_sends_error_notification_when_refresh_fails(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            init_db(db_path)
            profile = create_profile(
                db_path,
                FeedRequest(
                    feed_title="Forum Feed",
                    source_url="https://example.com/forum",
                    item_selector=".topic",
                    title_selector="a",
                    link_selector="a",
                    summary_selector="",
                    max_items=10,
                    refresh_interval_minutes=60,
                    fetch_mode="http",
                ),
            )
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute(
                    """
                    UPDATE app_settings
                    SET smtp_enabled = 1, smtp_host = 'smtp.example.com', smtp_port = 587, smtp_username = 'alerts@example.com',
                        smtp_password = 'secret', smtp_use_tls = 1, smtp_to_email = 'user@example.com',
                        smtp_from_email = 'Nightfeed <noreply@example.com>'
                    WHERE id = 1
                    """
                )
                conn.commit()

            with patch("rss_site_bridge.app.extract_feed_entries", side_effect=ValueError("Upstream exploded")), patch(
                "rss_site_bridge.app.maybe_send_refresh_notification"
            ) as mocked_notify:
                with self.assertRaises(RuntimeError):
                    refresh_profile(db_path, profile.id)

            mocked_notify.assert_called_once()
            _, kwargs = mocked_notify.call_args
            self.assertEqual("error", kwargs["status"])
            self.assertEqual("Upstream exploded", kwargs["error_message"])

    def test_new_profiles_default_to_failure_notifications_only(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            init_db(db_path)
            profile = create_profile(
                db_path,
                FeedRequest(
                    feed_title="Forum Feed",
                    source_url="https://example.com/forum",
                    item_selector=".topic",
                    title_selector="a",
                    link_selector="a",
                    summary_selector="",
                    max_items=10,
                    refresh_interval_minutes=60,
                    fetch_mode="http",
                ),
            )

            self.assertFalse(profile.notify_on_success)
            self.assertTrue(profile.notify_on_failure)

    def test_update_profile_persists_notification_preferences(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            init_db(db_path)
            profile = create_profile(
                db_path,
                FeedRequest(
                    feed_title="Forum Feed",
                    source_url="https://example.com/forum",
                    item_selector=".topic",
                    title_selector="a",
                    link_selector="a",
                    summary_selector="",
                    max_items=10,
                    refresh_interval_minutes=60,
                    fetch_mode="http",
                ),
            )

            updated = update_profile(
                db_path,
                profile.id,
                FeedRequest(
                    feed_title="Updated Feed",
                    source_url="https://example.com/new",
                    item_selector=".row",
                    title_selector=".title",
                    link_selector=".title a",
                    summary_selector=".summary",
                    max_items=20,
                    refresh_interval_minutes=120,
                    fetch_mode="browser",
                    notify_on_success=True,
                    notify_on_failure=False,
                ),
            )

            self.assertTrue(updated.notify_on_success)
            self.assertFalse(updated.notify_on_failure)
            self.assertEqual((), updated.notify_failure_categories)

    def test_profile_notification_categories_round_trip(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            init_db(db_path)
            profile = create_profile(
                db_path,
                FeedRequest(
                    feed_title="Forum Feed",
                    source_url="https://example.com/forum",
                    item_selector=".topic",
                    title_selector="a",
                    link_selector="a",
                    summary_selector="",
                    max_items=10,
                    refresh_interval_minutes=60,
                    fetch_mode="http",
                    notify_failure_categories=("selector", "browser"),
                ),
            )

            self.assertTrue(profile.notify_on_failure)
            self.assertEqual(("selector", "browser"), profile.notify_failure_categories)

    def test_init_db_migrates_existing_failure_notification_boolean(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE profiles (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        feed_token TEXT NOT NULL UNIQUE,
                        feed_title TEXT NOT NULL,
                        source_url TEXT NOT NULL,
                        item_selector TEXT NOT NULL,
                        title_selector TEXT NOT NULL,
                        link_selector TEXT NOT NULL,
                        summary_selector TEXT NOT NULL,
                        filter_rules TEXT NOT NULL DEFAULT '',
                        exclude_filter_rules TEXT NOT NULL DEFAULT '',
                        max_items INTEGER NOT NULL,
                        refresh_interval_minutes INTEGER NOT NULL,
                        fetch_mode TEXT NOT NULL,
                        notify_on_success INTEGER NOT NULL DEFAULT 0,
                        notify_on_failure INTEGER NOT NULL DEFAULT 1,
                        active INTEGER NOT NULL DEFAULT 1,
                        last_status TEXT NOT NULL DEFAULT 'idle',
                        last_error TEXT NOT NULL DEFAULT '',
                        last_refreshed_at TEXT NOT NULL DEFAULT '',
                        refresh_anchor_at TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO profiles (
                        feed_token, feed_title, source_url, item_selector, title_selector, link_selector,
                        summary_selector, max_items, refresh_interval_minutes, fetch_mode, notify_on_failure,
                        created_at, updated_at
                    ) VALUES
                    ('on-token', 'On Feed', 'https://example.com/on', '.topic', 'a', 'a', '', 10, 60, 'http', 1, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'),
                    ('off-token', 'Off Feed', 'https://example.com/off', '.topic', 'a', 'a', '', 10, 60, 'http', 0, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')
                    """
                )
                conn.commit()
            finally:
                conn.close()

            init_db(db_path)
            profiles = {profile.feed_title: profile for profile in list_profiles(db_path)}

            self.assertIn("selector", profiles["On Feed"].notify_failure_categories)
            self.assertEqual((), profiles["Off Feed"].notify_failure_categories)

    def test_refresh_profile_creates_success_notification(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            init_db(db_path)
            profile = create_profile(
                db_path,
                FeedRequest(
                    feed_title="Forum Feed",
                    source_url="https://example.com/forum",
                    item_selector=".topic",
                    title_selector="a",
                    link_selector="a",
                    summary_selector="",
                    max_items=10,
                    refresh_interval_minutes=60,
                    fetch_mode="http",
                ),
            )
            entries = [
                FeedEntry(
                    title="Topic A",
                    link="https://example.com/forum/topic-a",
                    summary="",
                    published_at=datetime.now(timezone.utc),
                )
            ]

            with patch("rss_site_bridge.app.extract_feed_entries", return_value=entries):
                refresh_profile(db_path, profile.id)

            notifications = list_notifications(db_path, unread_only=False)
            self.assertEqual(1, len(notifications))
            self.assertEqual("success", notifications[0].category)
            self.assertEqual("info", notifications[0].severity)
            self.assertEqual(1, count_unread_notifications(db_path))

    def test_failed_refresh_email_is_gated_by_category(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            init_db(db_path)
            profile = create_profile(
                db_path,
                FeedRequest(
                    feed_title="Forum Feed",
                    source_url="https://example.com/forum",
                    item_selector=".topic",
                    title_selector="a",
                    link_selector="a",
                    summary_selector="",
                    max_items=10,
                    refresh_interval_minutes=60,
                    fetch_mode="http",
                    notify_failure_categories=("browser",),
                ),
            )
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    UPDATE app_settings
                    SET smtp_enabled = 1, smtp_host = 'smtp.example.com', smtp_port = 587, smtp_username = 'alerts@example.com',
                        smtp_password = 'secret', smtp_use_tls = 1, smtp_to_email = 'user@example.com',
                        smtp_from_email = 'Nightfeed <noreply@example.com>'
                    WHERE id = 1
                    """
                )
                conn.commit()
            finally:
                conn.close()

            with patch(
                "rss_site_bridge.app.extract_feed_entries",
                side_effect=ValueError("Matched nodes did not contain usable titles and links."),
            ), patch("rss_site_bridge.app.send_refresh_notification_email") as mocked_email:
                with self.assertRaises(RuntimeError):
                    refresh_profile(db_path, profile.id)

            notifications = list_notifications(db_path, unread_only=False)
            self.assertEqual("extraction", notifications[0].category)
            mocked_email.assert_not_called()

    def test_classify_refresh_error_uses_practical_categories(self):
        self.assertEqual("selector", classify_refresh_error(ValueError("No topic nodes matched the item selector.")))
        self.assertEqual("extraction", classify_refresh_error(ValueError("Matched nodes did not contain usable titles and links.")))
        self.assertEqual("link_blocked", classify_refresh_error(ValueError("Off-site topic links are blocked by default.")))

    def test_refresh_profile_skips_success_notification_when_feed_disables_it(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            init_db(db_path)
            profile = create_profile(
                db_path,
                FeedRequest(
                    feed_title="Forum Feed",
                    source_url="https://example.com/forum",
                    item_selector=".topic",
                    title_selector="a",
                    link_selector="a",
                    summary_selector="",
                    max_items=10,
                    refresh_interval_minutes=60,
                    fetch_mode="http",
                    notify_on_success=False,
                    notify_on_failure=True,
                ),
            )
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute(
                    """
                    UPDATE app_settings
                    SET smtp_enabled = 1, smtp_host = 'smtp.example.com', smtp_port = 587, smtp_username = 'alerts@example.com',
                        smtp_password = 'secret', smtp_use_tls = 1, smtp_to_email = 'user@example.com',
                        smtp_from_email = 'Nightfeed <noreply@example.com>'
                    WHERE id = 1
                    """
                )
                conn.commit()

            entries = [
                FeedEntry(
                    title="Topic A",
                    link="https://example.com/forum/topic-a",
                    summary="",
                    published_at=datetime.now(timezone.utc),
                )
            ]

            with patch("rss_site_bridge.app.extract_feed_entries", return_value=entries), patch(
                "rss_site_bridge.app.send_refresh_notification_email"
            ) as mocked_email:
                refresh_profile(db_path, profile.id)

            mocked_email.assert_not_called()

    def test_send_test_email_uses_smtp_sender(self):
        settings = AppSettings(
            timezone_name="America/Chicago",
            smtp_enabled=True,
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_username="alerts@example.com",
            smtp_password="secret",
            smtp_use_tls=True,
            smtp_to_email="user@example.com",
            smtp_from_email="Nightfeed <noreply@example.com>",
        )

        with patch("rss_site_bridge.app.send_smtp_message") as mocked_send:
            send_test_email(settings)

        mocked_send.assert_called_once()
        sent_settings, message = mocked_send.call_args.args
        self.assertEqual(settings, sent_settings)
        self.assertEqual("Nightfeed SMTP test", message["Subject"])
        self.assertEqual("user@example.com", message["To"])
        self.assertEqual("Nightfeed <noreply@example.com>", message["From"])

    def test_app_settings_do_not_prefill_smtp_port_or_from_email(self):
        settings = AppSettings()
        self.assertEqual(0, settings.smtp_port)
        self.assertEqual("", settings.smtp_from_email)

    def test_due_refresh_runs_for_stale_profiles(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            init_db(db_path)
            profile = create_profile(
                db_path,
                FeedRequest(
                    feed_title="Forum Feed",
                    source_url="https://example.com/forum",
                    item_selector=".topic",
                    title_selector="a",
                    link_selector="a",
                    summary_selector="",
                    max_items=10,
                    refresh_interval_minutes=60,
                    fetch_mode="http",
                ),
            )
            stale_anchor = (datetime.now(timezone.utc) - timedelta(minutes=61)).isoformat()
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute(
                    "UPDATE profiles SET refresh_anchor_at = ? WHERE id = ?",
                    (stale_anchor, profile.id),
                )
                conn.commit()

            with patch("rss_site_bridge.app.refresh_profile") as mocked_refresh:
                refresh_due_profiles(db_path)
                mocked_refresh.assert_called_once_with(db_path, profile.id)

    def test_update_profile_rewrites_saved_fields(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            init_db(db_path)
            profile = create_profile(
                db_path,
                FeedRequest(
                    feed_title="Forum Feed",
                    source_url="https://example.com/forum",
                    item_selector=".topic",
                    title_selector="a",
                    link_selector="a",
                    summary_selector="",
                    max_items=10,
                    refresh_interval_minutes=60,
                    fetch_mode="http",
                ),
            )

            updated = update_profile(
                db_path,
                profile.id,
                FeedRequest(
                    feed_title="Updated Feed",
                    source_url="https://example.com/new",
                    item_selector=".row",
                    title_selector=".title",
                    link_selector=".title a",
                    summary_selector=".summary",
                    max_items=20,
                    refresh_interval_minutes=120,
                    fetch_mode="browser",
                ),
            )

            self.assertEqual("Updated Feed", updated.feed_title)
            self.assertEqual("https://example.com/new", updated.source_url)
            self.assertEqual("browser", updated.fetch_mode)
            self.assertEqual("", updated.filter_rules)
            self.assertEqual("", updated.exclude_filter_rules)

    def test_delete_profile_removes_profile_and_items(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            init_db(db_path)
            profile = create_profile(
                db_path,
                FeedRequest(
                    feed_title="Forum Feed",
                    source_url="https://example.com/forum",
                    item_selector=".topic",
                    title_selector="a",
                    link_selector="a",
                    summary_selector="",
                    max_items=10,
                    refresh_interval_minutes=60,
                    fetch_mode="http",
                ),
            )
            with patch(
                "rss_site_bridge.app.extract_feed_entries",
                return_value=[
                    FeedEntry(
                        title="Topic A",
                        link="https://example.com/forum/topic-a",
                        summary="Summary A",
                        published_at=datetime.now(timezone.utc),
                    )
                ],
            ):
                refresh_profile(db_path, profile.id)

            delete_profile(db_path, profile.id)

            self.assertIsNone(get_profile_by_id(db_path, profile.id))
            self.assertEqual([], list_feed_items(db_path, profile.id, 10))

    def test_humanize_datetime_uses_requested_timezone(self):
        rendered = humanize_datetime("2026-02-28T04:44:13+00:00", "America/Chicago")
        self.assertIn("Feb 27, 2026", rendered)
        self.assertIn("CST", rendered)

    def test_utc_timezone_is_supported_without_zoneinfo_data(self):
        self.assertEqual("UTC", parse_timezone_name("UTC"))
        self.assertEqual("UTC", parse_timezone_name(""))

        rendered = humanize_datetime("2026-02-28T04:44:13+00:00", "UTC")
        self.assertIn("Feb 28, 2026", rendered)
        self.assertIn("UTC", rendered)

    def test_new_profile_is_idle_and_not_due_immediately(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            init_db(db_path)
            profile = create_profile(
                db_path,
                FeedRequest(
                    feed_title="Forum Feed",
                    source_url="https://example.com/forum",
                    item_selector=".topic",
                    title_selector="a",
                    link_selector="a",
                    summary_selector="",
                    max_items=10,
                    refresh_interval_minutes=60,
                    fetch_mode="http",
                ),
            )

            self.assertEqual("idle", profile.last_status)
            self.assertFalse(should_refresh(profile))

    def test_next_refresh_uses_refresh_anchor_and_interval(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            init_db(db_path)
            profile = create_profile(
                db_path,
                FeedRequest(
                    feed_title="Forum Feed",
                    source_url="https://example.com/forum",
                    item_selector=".topic",
                    title_selector="a",
                    link_selector="a",
                    summary_selector="",
                    max_items=10,
                    refresh_interval_minutes=60,
                    fetch_mode="http",
                ),
            )
            anchor = "2026-02-28T04:44:13+00:00"
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute(
                    "UPDATE profiles SET refresh_anchor_at = ? WHERE id = ?",
                    (anchor, profile.id),
                )
                conn.commit()

            updated = get_profile_by_id(db_path, profile.id)

            self.assertIsNotNone(updated)
            self.assertEqual(
                datetime(2026, 2, 28, 5, 44, 13, tzinfo=timezone.utc),
                get_next_refresh_at(updated),
            )
            self.assertIn("Feb 28, 2026 05:44 AM UTC", humanize_next_refresh(updated, "UTC"))

    def test_profile_with_zero_refresh_interval_never_auto_refreshes(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            init_db(db_path)
            profile = create_profile(
                db_path,
                FeedRequest(
                    feed_title="Forum Feed",
                    source_url="https://example.com/forum",
                    item_selector=".topic",
                    title_selector="a",
                    link_selector="a",
                    summary_selector="",
                    max_items=10,
                    refresh_interval_minutes=0,
                    fetch_mode="http",
                ),
            )

            self.assertFalse(should_refresh(profile))
            self.assertIsNone(get_next_refresh_at(profile))
            self.assertEqual("Manual only", humanize_next_refresh(profile))

    def test_set_profile_active_disables_and_reenables_with_idle_status(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            init_db(db_path)
            profile = create_profile(
                db_path,
                FeedRequest(
                    feed_title="Forum Feed",
                    source_url="https://example.com/forum",
                    item_selector=".topic",
                    title_selector="a",
                    link_selector="a",
                    summary_selector="",
                    max_items=10,
                    refresh_interval_minutes=60,
                    fetch_mode="http",
                ),
            )

            disabled = set_profile_active(db_path, profile.id, active=False)
            self.assertFalse(disabled.active)
            self.assertEqual("disabled", disabled.last_status)
            self.assertFalse(should_refresh(disabled))
            self.assertIsNone(get_next_refresh_at(disabled))
            self.assertEqual("Disabled", humanize_next_refresh(disabled))

            enabled = set_profile_active(db_path, profile.id, active=True)
            self.assertTrue(enabled.active)
            self.assertEqual("idle", enabled.last_status)
            self.assertFalse(should_refresh(enabled))
            self.assertIsNotNone(get_next_refresh_at(enabled))

    def test_build_clone_title_increments_existing_copy_suffix(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            init_db(db_path)
            create_profile(
                db_path,
                FeedRequest(
                    feed_title="Forum Feed copy",
                    source_url="https://example.com/forum",
                    item_selector=".topic",
                    title_selector="a",
                    link_selector="a",
                    summary_selector="",
                    max_items=10,
                    refresh_interval_minutes=60,
                    fetch_mode="http",
                ),
            )

            self.assertEqual("Forum Feed copy 1", build_clone_title(db_path, "Forum Feed copy"))

    def test_build_clone_title_increments_numbered_copy_suffix(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            init_db(db_path)
            create_profile(
                db_path,
                FeedRequest(
                    feed_title="Forum Feed copy",
                    source_url="https://example.com/forum",
                    item_selector=".topic",
                    title_selector="a",
                    link_selector="a",
                    summary_selector="",
                    max_items=10,
                    refresh_interval_minutes=60,
                    fetch_mode="http",
                ),
            )
            create_profile(
                db_path,
                FeedRequest(
                    feed_title="Forum Feed copy 1",
                    source_url="https://example.com/forum-1",
                    item_selector=".topic",
                    title_selector="a",
                    link_selector="a",
                    summary_selector="",
                    max_items=10,
                    refresh_interval_minutes=60,
                    fetch_mode="http",
                ),
            )

            self.assertEqual("Forum Feed copy 2", build_clone_title(db_path, "Forum Feed copy 1"))

    @unittest.skipIf(flask is None, "Flask is not installed in this environment.")
    def test_feed_route_returns_persisted_rss(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            app = create_app(
                {
                    "TESTING": True,
                    "START_SCHEDULER": False,
                    "DATABASE_PATH": db_path,
                }
            )

            profile = create_profile(
                db_path,
                FeedRequest(
                    feed_title="Forum Feed",
                    source_url="https://example.com/forum",
                    item_selector=".topic",
                    title_selector="a",
                    link_selector="a",
                    summary_selector="",
                    max_items=10,
                    refresh_interval_minutes=60,
                    fetch_mode="http",
                ),
            )
            with patch(
                "rss_site_bridge.app.extract_feed_entries",
                return_value=[
                    FeedEntry(
                        title="Topic A",
                        link="https://example.com/forum/topic-a",
                        summary="Summary A",
                        published_at=datetime.now(timezone.utc),
                    )
                ],
            ):
                refresh_profile(db_path, profile.id)

            saved = get_profile_by_token(db_path, profile.feed_token)
            client = app.test_client()
            response = client.get("/feeds/{0}.xml".format(saved.feed_token))

            self.assertEqual(200, response.status_code)
            self.assertIn(b"Topic A", response.data)

    @unittest.skipIf(flask is None, "Flask is not installed in this environment.")
    def test_request_id_header_is_echoed_in_response(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            app = create_app(
                {
                    "TESTING": True,
                    "START_SCHEDULER": False,
                    "DATABASE_PATH": db_path,
                }
            )

            client = app.test_client()
            response = client.get("/", headers={"X-Request-ID": "trace-123"})

            self.assertEqual(200, response.status_code)
            self.assertEqual("trace-123", response.headers.get("X-Request-ID"))

    @unittest.skipIf(flask is None, "Flask is not installed in this environment.")
    def test_feed_routes_return_404_when_profile_is_disabled(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            app = create_app(
                {
                    "TESTING": True,
                    "START_SCHEDULER": False,
                    "DATABASE_PATH": db_path,
                }
            )

            profile = create_profile(
                db_path,
                FeedRequest(
                    feed_title="Forum Feed",
                    source_url="https://example.com/forum",
                    item_selector=".topic",
                    title_selector="a",
                    link_selector="a",
                    summary_selector="",
                    max_items=10,
                    refresh_interval_minutes=60,
                    fetch_mode="http",
                ),
            )
            set_profile_active(db_path, profile.id, active=False)
            saved = get_profile_by_id(db_path, profile.id)

            client = app.test_client()
            xml_response = client.get(f"/feeds/{saved.feed_token}.xml")
            view_response = client.get(f"/feeds/{saved.feed_token}/view")

            self.assertEqual(404, xml_response.status_code)
            self.assertEqual(404, view_response.status_code)
            self.assertIn(b"Feed not found.", xml_response.data)

    @unittest.skipIf(flask is None, "Flask is not installed in this environment.")
    def test_profile_route_renders_dedicated_feed_page(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            app = create_app(
                {
                    "TESTING": True,
                    "START_SCHEDULER": False,
                    "DATABASE_PATH": db_path,
                }
            )

            profile = create_profile(
                db_path,
                FeedRequest(
                    feed_title="Forum Feed",
                    source_url="https://example.com/forum",
                    item_selector=".topic",
                    title_selector="a",
                    link_selector="a",
                    summary_selector="",
                    max_items=10,
                    refresh_interval_minutes=60,
                    fetch_mode="http",
                ),
            )

            client = app.test_client()
            response = client.get(f"/profiles/{profile.id}")

            self.assertEqual(200, response.status_code)
            self.assertIn(b"Overview", response.data)
            self.assertIn(f"http://localhost/feeds/{profile.feed_token}.xml".encode(), response.data)

    @unittest.skipIf(flask is None, "Flask is not installed in this environment.")
    def test_profile_route_uses_forwarded_https_host_from_reverse_proxy(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            app = create_app(
                {
                    "TESTING": True,
                    "START_SCHEDULER": False,
                    "DATABASE_PATH": db_path,
                }
            )

            profile = create_profile(
                db_path,
                FeedRequest(
                    feed_title="Forum Feed",
                    source_url="https://example.com/forum",
                    item_selector=".topic",
                    title_selector="a",
                    link_selector="a",
                    summary_selector="",
                    max_items=10,
                    refresh_interval_minutes=60,
                    fetch_mode="http",
                ),
            )

            client = app.test_client()
            response = client.get(
                f"/profiles/{profile.id}",
                headers={
                    "X-Forwarded-Proto": "https",
                    "X-Forwarded-Host": "rss.example.com",
                    "X-Forwarded-Port": "443",
                },
            )

            self.assertEqual(200, response.status_code)
            self.assertIn(f"https://rss.example.com/feeds/{profile.feed_token}.xml".encode(), response.data)

    @unittest.skipIf(flask is None, "Flask is not installed in this environment.")
    def test_purge_route_removes_stored_entries_without_deleting_profile(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            app = create_app(
                {
                    "TESTING": True,
                    "START_SCHEDULER": False,
                    "DATABASE_PATH": db_path,
                }
            )

            profile = create_profile(
                db_path,
                FeedRequest(
                    feed_title="Forum Feed",
                    source_url="https://example.com/forum",
                    item_selector=".topic",
                    title_selector="a",
                    link_selector="a",
                    summary_selector="",
                    max_items=10,
                    refresh_interval_minutes=60,
                    fetch_mode="http",
                ),
            )
            with patch(
                "rss_site_bridge.app.extract_feed_entries",
                return_value=[
                    FeedEntry(
                        title="Topic A",
                        link="https://example.com/forum/topic-a",
                        summary="Summary A",
                        published_at=datetime.now(timezone.utc),
                    )
                ],
            ):
                refresh_profile(db_path, profile.id)

            client = app.test_client()
            response = client.post(f"/profiles/{profile.id}/purge")

            self.assertEqual(302, response.status_code)
            self.assertIsNotNone(get_profile_by_id(db_path, profile.id))
            self.assertEqual([], list_feed_items(db_path, profile.id, 10))

    @unittest.skipIf(flask is None, "Flask is not installed in this environment.")
    def test_profile_route_supports_preview_for_edit_form(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            app = create_app(
                {
                    "TESTING": True,
                    "START_SCHEDULER": False,
                    "DATABASE_PATH": db_path,
                }
            )

            profile = create_profile(
                db_path,
                FeedRequest(
                    feed_title="Forum Feed",
                    source_url="https://example.com/forum",
                    item_selector=".topic",
                    title_selector="a",
                    link_selector="a",
                    summary_selector="",
                    max_items=10,
                    refresh_interval_minutes=60,
                    fetch_mode="http",
                ),
            )

            with patch(
                "rss_site_bridge.app.extract_feed_entries",
                return_value=[
                    FeedEntry(
                        title="Preview Topic",
                        link="https://example.com/forum/topic-a",
                        summary="",
                        published_at=datetime.now(timezone.utc),
                    )
                ],
            ):
                client = app.test_client()
                response = client.get(
                    f"/profiles/{profile.id}",
                    query_string={
                        "preview": "1",
                        "feed_title": "Forum Feed",
                        "source_url": "https://example.com/forum",
                        "item_selector": ".topic",
                        "title_selector": "a",
                        "link_selector": "a",
                        "summary_selector": "",
                        "max_items": "10",
                        "refresh_interval_minutes": "60",
                        "fetch_mode": "http",
                    },
                )

            self.assertEqual(200, response.status_code)
            self.assertIn(b"Preview Topic", response.data)

    @unittest.skipIf(flask is None, "Flask is not installed in this environment.")
    def test_profile_preview_route_returns_json(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            app = create_app(
                {
                    "TESTING": True,
                    "START_SCHEDULER": False,
                    "DATABASE_PATH": db_path,
                }
            )

            profile = create_profile(
                db_path,
                FeedRequest(
                    feed_title="Forum Feed",
                    source_url="https://example.com/forum",
                    item_selector=".topic",
                    title_selector="a",
                    link_selector="a",
                    summary_selector="",
                    max_items=10,
                    refresh_interval_minutes=60,
                    fetch_mode="http",
                ),
            )

            with patch(
                "rss_site_bridge.app.extract_feed_entries",
                return_value=[
                    FeedEntry(
                        title="Preview Topic",
                        link="https://example.com/forum/topic-a",
                        summary="",
                        published_at=datetime.now(timezone.utc),
                    )
                ],
            ):
                client = app.test_client()
                response = client.post(
                    f"/profiles/{profile.id}/preview",
                    data={
                        "feed_title": "Forum Feed",
                        "source_url": "https://example.com/forum",
                        "item_selector": ".topic",
                        "title_selector": "a",
                        "link_selector": "a",
                        "summary_selector": "",
                        "max_items": "10",
                        "refresh_interval_minutes": "60",
                        "fetch_mode": "http",
                    },
                )

            self.assertEqual(200, response.status_code)
            self.assertEqual(
                {"items": [{"title": "Preview Topic", "link": "https://example.com/forum/topic-a"}], "error": None},
                response.get_json(),
            )

    @unittest.skipIf(flask is None, "Flask is not installed in this environment.")
    def test_compose_preview_stream_route_returns_stage_events_and_result(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            app = create_app(
                {
                    "TESTING": True,
                    "START_SCHEDULER": False,
                    "DATABASE_PATH": db_path,
                }
            )

            def fake_extract(_config, *, progress=None):
                if progress is not None:
                    progress("Fetching content", "Downloading the page HTML over HTTP.")
                    progress("Preparing preview", "Formatting extracted titles and links.")
                return [
                    FeedEntry(
                        title="Preview Topic",
                        link="https://example.com/forum/topic-a",
                        summary="",
                        published_at=datetime.now(timezone.utc),
                    )
                ]

            with patch("rss_site_bridge.app.extract_feed_entries", side_effect=fake_extract):
                client = app.test_client()
                response = client.get(
                    "/preview/stream",
                    query_string={
                        "feed_title": "Forum Feed",
                        "source_url": "https://example.com/forum",
                        "item_selector": ".topic",
                        "title_selector": "a",
                        "link_selector": "a",
                        "summary_selector": "",
                        "filter_rules": "",
                        "max_items": "10",
                        "refresh_interval_minutes": "60",
                        "fetch_mode": "http",
                    },
                )

            payload = response.get_data(as_text=True)
            self.assertEqual(200, response.status_code)
            self.assertIn("event: stage", payload)
            self.assertIn("Fetching content", payload)
            self.assertIn("event: result", payload)
            self.assertIn("Preview Topic", payload)

    @unittest.skipIf(flask is None, "Flask is not installed in this environment.")
    def test_profile_preview_stream_route_returns_stage_events_and_result(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            app = create_app(
                {
                    "TESTING": True,
                    "START_SCHEDULER": False,
                    "DATABASE_PATH": db_path,
                }
            )

            profile = create_profile(
                db_path,
                FeedRequest(
                    feed_title="Forum Feed",
                    source_url="https://example.com/forum",
                    item_selector=".topic",
                    title_selector="a",
                    link_selector="a",
                    summary_selector="",
                    max_items=10,
                    refresh_interval_minutes=60,
                    fetch_mode="http",
                ),
            )

            def fake_extract(_config, *, progress=None):
                if progress is not None:
                    progress("Matching selectors", "Locating item nodes from the page.")
                return [
                    FeedEntry(
                        title="Preview Topic",
                        link="https://example.com/forum/topic-a",
                        summary="",
                        published_at=datetime.now(timezone.utc),
                    )
                ]

            with patch("rss_site_bridge.app.extract_feed_entries", side_effect=fake_extract):
                client = app.test_client()
                response = client.get(
                    f"/profiles/{profile.id}/preview/stream",
                    query_string={
                        "feed_title": "Forum Feed",
                        "source_url": "https://example.com/forum",
                        "item_selector": ".topic",
                        "title_selector": "a",
                        "link_selector": "a",
                        "summary_selector": "",
                        "filter_rules": "",
                        "max_items": "10",
                        "refresh_interval_minutes": "60",
                        "fetch_mode": "http",
                    },
                )

            payload = response.get_data(as_text=True)
            self.assertEqual(200, response.status_code)
            self.assertIn("event: stage", payload)
            self.assertIn("Matching selectors", payload)
            self.assertIn("event: result", payload)

    @unittest.skipIf(flask is None, "Flask is not installed in this environment.")
    def test_compose_route_renders_create_page(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            app = create_app(
                {
                    "TESTING": True,
                    "START_SCHEDULER": False,
                    "DATABASE_PATH": db_path,
                }
            )

            client = app.test_client()
            response = client.get("/compose")

            self.assertEqual(200, response.status_code)
            self.assertIn(b"Create Feed", response.data)

    @unittest.skipIf(flask is None, "Flask is not installed in this environment.")
    def test_create_route_saves_feed_without_auto_refresh(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            app = create_app(
                {
                    "TESTING": True,
                    "START_SCHEDULER": False,
                    "DATABASE_PATH": db_path,
                }
            )

            client = app.test_client()
            with patch("rss_site_bridge.app.extract_feed_entries") as mocked_extract:
                response = client.post(
                    "/profiles",
                    data={
                        "feed_title": "Forum Feed",
                        "source_url": "https://example.com/forum",
                        "item_selector": ".topic",
                        "title_selector": "a",
                        "link_selector": "a",
                        "summary_selector": "",
                        "filter_rules": "",
                        "exclude_filter_rules": "",
                        "max_items": "10",
                        "refresh_interval_minutes": "60",
                        "fetch_mode": "http",
                    },
                )

            self.assertEqual(302, response.status_code)
            mocked_extract.assert_not_called()

            profiles = list_profiles(db_path)
            self.assertEqual(1, len(profiles))
            self.assertEqual("idle", profiles[0].last_status)
            self.assertEqual(0, profiles[0].item_count)
            self.assertFalse(should_refresh(profiles[0]))

    @unittest.skipIf(flask is None, "Flask is not installed in this environment.")
    def test_compose_preview_route_returns_json(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            app = create_app(
                {
                    "TESTING": True,
                    "START_SCHEDULER": False,
                    "DATABASE_PATH": db_path,
                }
            )

            with patch(
                "rss_site_bridge.app.extract_feed_entries",
                return_value=[
                    FeedEntry(
                        title="Preview Topic",
                        link="https://example.com/forum/topic-a",
                        summary="",
                        published_at=datetime.now(timezone.utc),
                    )
                ],
            ):
                client = app.test_client()
                response = client.post(
                    "/preview",
                    data={
                        "feed_title": "Forum Feed",
                        "source_url": "https://example.com/forum",
                        "item_selector": ".topic",
                        "title_selector": "a",
                        "link_selector": "a",
                        "summary_selector": "",
                        "max_items": "10",
                        "refresh_interval_minutes": "60",
                        "fetch_mode": "http",
                    },
                )

            self.assertEqual(200, response.status_code)
            self.assertEqual(
                {"items": [{"title": "Preview Topic", "link": "https://example.com/forum/topic-a"}], "error": None},
                response.get_json(),
            )

    @unittest.skipIf(flask is None, "Flask is not installed in this environment.")
    def test_clone_route_prefills_compose_page(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            app = create_app(
                {
                    "TESTING": True,
                    "START_SCHEDULER": False,
                    "DATABASE_PATH": db_path,
                }
            )

            profile = create_profile(
                db_path,
                FeedRequest(
                    feed_title="Forum Feed",
                    source_url="https://example.com/forum",
                    item_selector=".topic",
                    title_selector="a",
                    link_selector="a",
                    summary_selector=".summary",
                    max_items=10,
                    refresh_interval_minutes=60,
                    fetch_mode="http",
                ),
            )

            client = app.test_client()
            redirect_response = client.get(f"/profiles/{profile.id}/clone")
            self.assertEqual(302, redirect_response.status_code)

            response = client.get(f"/profiles/{profile.id}/clone", follow_redirects=True)

            self.assertEqual(200, response.status_code)
            self.assertIn(b"Forum Feed copy", response.data)
            self.assertIn(b"https://example.com/forum", response.data)

    @unittest.skipIf(flask is None, "Flask is not installed in this environment.")
    def test_clone_route_increments_copy_name_when_needed(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            app = create_app(
                {
                    "TESTING": True,
                    "START_SCHEDULER": False,
                    "DATABASE_PATH": db_path,
                }
            )

            profile = create_profile(
                db_path,
                FeedRequest(
                    feed_title="Forum Feed",
                    source_url="https://example.com/forum",
                    item_selector=".topic",
                    title_selector="a",
                    link_selector="a",
                    summary_selector=".summary",
                    max_items=10,
                    refresh_interval_minutes=60,
                    fetch_mode="http",
                ),
            )
            create_profile(
                db_path,
                FeedRequest(
                    feed_title="Forum Feed copy",
                    source_url="https://example.com/forum-copy",
                    item_selector=".topic",
                    title_selector="a",
                    link_selector="a",
                    summary_selector=".summary",
                    max_items=10,
                    refresh_interval_minutes=60,
                    fetch_mode="http",
                ),
            )
            create_profile(
                db_path,
                FeedRequest(
                    feed_title="Forum Feed copy 1",
                    source_url="https://example.com/forum-copy-1",
                    item_selector=".topic",
                    title_selector="a",
                    link_selector="a",
                    summary_selector=".summary",
                    max_items=10,
                    refresh_interval_minutes=60,
                    fetch_mode="http",
                ),
            )

            client = app.test_client()
            response = client.get(f"/profiles/{profile.id}/clone", follow_redirects=True)

            self.assertEqual(200, response.status_code)
            self.assertIn(b"Forum Feed copy 2", response.data)

    @unittest.skipIf(flask is None, "Flask is not installed in this environment.")
    def test_settings_route_updates_timezone(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            app = create_app(
                {
                    "TESTING": True,
                    "START_SCHEDULER": False,
                    "DATABASE_PATH": db_path,
                }
            )

            client = app.test_client()
            response = client.post(
                "/settings",
                data={
                    "timezone_name": "America/Chicago",
                    "public_base_url": "https://rss.example.com",
                    "smtp_enabled": "1",
                    "smtp_host": "smtp.example.com",
                    "smtp_port": "587",
                    "smtp_username": "alerts@example.com",
                    "smtp_password": "secret",
                    "smtp_use_tls": "1",
                    "smtp_to_email": "user@example.com",
                    "smtp_from_email": "Nightfeed <noreply@example.com>",
                },
            )

            self.assertEqual(302, response.status_code)
            settings = get_app_settings(db_path)
            self.assertEqual("America/Chicago", settings.timezone_name)
            self.assertEqual("https://rss.example.com", settings.public_base_url)
            self.assertTrue(settings.smtp_enabled)
            self.assertEqual("smtp.example.com", settings.smtp_host)
            self.assertEqual(587, settings.smtp_port)
            self.assertEqual("alerts@example.com", settings.smtp_username)
            self.assertEqual("secret", settings.smtp_password)
            self.assertTrue(settings.smtp_use_tls)
            self.assertEqual("user@example.com", settings.smtp_to_email)
            self.assertEqual("Nightfeed <noreply@example.com>", settings.smtp_from_email)

    @unittest.skipIf(flask is None, "Flask is not installed in this environment.")
    def test_settings_route_rejects_invalid_public_base_url(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            app = create_app(
                {
                    "TESTING": True,
                    "START_SCHEDULER": False,
                    "DATABASE_PATH": db_path,
                }
            )

            client = app.test_client()
            response = client.post(
                "/settings",
                data={
                    "timezone_name": "UTC",
                    "public_base_url": "https://rss.example.com/base",
                },
            )

            self.assertEqual(400, response.status_code)
            self.assertIn(b"Public base URL must include only scheme and host", response.data)

    @unittest.skipIf(flask is None, "Flask is not installed in this environment.")
    def test_settings_route_requires_smtp_values_when_notifications_enabled(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            app = create_app(
                {
                    "TESTING": True,
                    "START_SCHEDULER": False,
                    "DATABASE_PATH": db_path,
                }
            )

            client = app.test_client()
            response = client.post(
                "/settings",
                data={
                    "timezone_name": "UTC",
                    "public_base_url": "",
                    "smtp_enabled": "1",
                    "smtp_port": "587",
                    "smtp_use_tls": "1",
                    "smtp_to_email": "user@example.com",
                    "smtp_from_email": "Nightfeed <noreply@example.com>",
                },
            )

            self.assertEqual(400, response.status_code)
            self.assertIn(b"SMTP host is required.", response.data)

    @unittest.skipIf(flask is None, "Flask is not installed in this environment.")
    def test_compose_route_hides_feed_notification_preferences_when_smtp_disabled(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            app = create_app(
                {
                    "TESTING": True,
                    "START_SCHEDULER": False,
                    "DATABASE_PATH": db_path,
                }
            )

            client = app.test_client()
            response = client.get("/compose")

            self.assertEqual(200, response.status_code)
            self.assertNotIn(b"Success notifications", response.data)
            self.assertNotIn(b"Failure notifications", response.data)

    @unittest.skipIf(flask is None, "Flask is not installed in this environment.")
    def test_compose_route_shows_feed_notification_preferences_when_smtp_enabled(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            app = create_app(
                {
                    "TESTING": True,
                    "START_SCHEDULER": False,
                    "DATABASE_PATH": db_path,
                }
            )
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute(
                    """
                    UPDATE app_settings
                    SET smtp_enabled = 1, smtp_host = 'smtp.example.com', smtp_port = 587, smtp_username = 'alerts@example.com',
                        smtp_password = 'secret', smtp_use_tls = 1, smtp_to_email = 'user@example.com',
                        smtp_from_email = 'Nightfeed <noreply@example.com>'
                    WHERE id = 1
                    """
                )
                conn.commit()

            client = app.test_client()
            response = client.get("/compose")

            self.assertEqual(200, response.status_code)
            self.assertIn(b"Success notifications", response.data)
            self.assertIn(b"Failure notifications", response.data)

    @unittest.skipIf(flask is None, "Flask is not installed in this environment.")
    def test_settings_test_email_route_sends_message(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            app = create_app(
                {
                    "TESTING": True,
                    "START_SCHEDULER": False,
                    "DATABASE_PATH": db_path,
                }
            )

            client = app.test_client()
            with patch("rss_site_bridge.app.send_test_email") as mocked_send:
                response = client.post(
                    "/settings/test-email",
                    data={
                        "timezone_name": "America/Chicago",
                        "public_base_url": "https://rss.example.com",
                        "smtp_enabled": "1",
                        "smtp_host": "smtp.example.com",
                        "smtp_port": "587",
                        "smtp_username": "alerts@example.com",
                        "smtp_password": "secret",
                        "smtp_use_tls": "1",
                        "smtp_to_email": "user@example.com",
                        "smtp_from_email": "Nightfeed <noreply@example.com>",
                    },
                )

            self.assertEqual(200, response.status_code)
            self.assertIn(b"Test email sent", response.data)
            mocked_send.assert_called_once()
            settings = get_app_settings(db_path)
            self.assertFalse(settings.smtp_enabled)
            self.assertEqual("", settings.smtp_host)

    @unittest.skipIf(flask is None, "Flask is not installed in this environment.")
    def test_settings_test_email_route_handles_transport_errors(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            app = create_app(
                {
                    "TESTING": True,
                    "START_SCHEDULER": False,
                    "DATABASE_PATH": db_path,
                }
            )

            client = app.test_client()
            with patch(
                "rss_site_bridge.app.send_test_email",
                side_effect=smtplib.SMTPAuthenticationError(535, b"bad credentials"),
            ):
                response = client.post(
                    "/settings/test-email",
                    data={
                        "timezone_name": "America/Chicago",
                        "public_base_url": "https://rss.example.com",
                        "smtp_enabled": "1",
                        "smtp_host": "smtp.example.com",
                        "smtp_port": "587",
                        "smtp_username": "alerts@example.com",
                        "smtp_password": "secret",
                        "smtp_use_tls": "1",
                        "smtp_to_email": "user@example.com",
                        "smtp_from_email": "Nightfeed <noreply@example.com>",
                    },
                )

            self.assertEqual(400, response.status_code)
            self.assertIn(b"bad credentials", response.data)
            settings = get_app_settings(db_path)
            self.assertFalse(settings.smtp_enabled)
            self.assertEqual("", settings.smtp_host)

    @unittest.skipIf(flask is None, "Flask is not installed in this environment.")
    def test_profile_route_uses_public_base_url_override_for_feed_url(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            app = create_app(
                {
                    "TESTING": True,
                    "START_SCHEDULER": False,
                    "DATABASE_PATH": db_path,
                }
            )

            profile = create_profile(
                db_path,
                FeedRequest(
                    feed_title="Forum Feed",
                    source_url="https://example.com/forum",
                    item_selector=".topic",
                    title_selector="a",
                    link_selector="a",
                    summary_selector="",
                    max_items=10,
                    refresh_interval_minutes=60,
                    fetch_mode="http",
                ),
            )

            client = app.test_client()
            client.post(
                "/settings",
                data={
                    "timezone_name": "UTC",
                    "public_base_url": "https://rss.example.com",
                },
            )

            response = client.get(f"/profiles/{profile.id}")

            self.assertEqual(200, response.status_code)
            self.assertIn(f"https://rss.example.com/feeds/{profile.feed_token}.xml".encode(), response.data)
            self.assertIn(f"/feeds/{profile.feed_token}/view".encode(), response.data)

    @unittest.skipIf(flask is None, "Flask is not installed in this environment.")
    def test_ajax_refresh_route_returns_json_without_redirect(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            app = create_app(
                {
                    "TESTING": True,
                    "START_SCHEDULER": False,
                    "DATABASE_PATH": db_path,
                }
            )

            profile = create_profile(
                db_path,
                FeedRequest(
                    feed_title="Forum Feed",
                    source_url="https://example.com/forum",
                    item_selector=".topic",
                    title_selector="a",
                    link_selector="a",
                    summary_selector="",
                    max_items=10,
                    refresh_interval_minutes=60,
                    fetch_mode="http",
                ),
            )

            with patch(
                "rss_site_bridge.app.extract_feed_entries",
                return_value=[
                    FeedEntry(
                        title="Topic A",
                        link="https://example.com/forum/topic-a",
                        summary="Summary A",
                        published_at=datetime.now(timezone.utc),
                    )
                ],
            ):
                client = app.test_client()
                response = client.post(
                    f"/profiles/{profile.id}/refresh",
                    headers={"X-Requested-With": "XMLHttpRequest"},
                )

            self.assertEqual(200, response.status_code)
            payload = response.get_json()
            self.assertEqual(1, payload["item_count"])
            self.assertIn("UTC", payload["last_refreshed_at"])
            self.assertIn("UTC", payload["next_refresh_at"])
            self.assertEqual("ok", payload["status"])

    @unittest.skipIf(flask is None, "Flask is not installed in this environment.")
    def test_notifications_route_supports_read_and_delete_actions(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            app = create_app(
                {
                    "TESTING": True,
                    "START_SCHEDULER": False,
                    "DATABASE_PATH": db_path,
                }
            )
            notification = create_notification(
                db_path,
                profile_id=None,
                event_type="refresh",
                severity="error",
                category="extraction",
                title="Refresh failed",
                message="Matched nodes did not contain usable titles and links.",
                source_url="https://example.com/forum",
            )

            client = app.test_client()
            response = client.get("/notifications")
            self.assertEqual(200, response.status_code)
            self.assertIn(b"Refresh failed", response.data)
            self.assertIn(b"Selector or extraction problems", response.data)

            read_response = client.post(f"/notifications/{notification.id}/read", data={"status": "all"})
            self.assertEqual(302, read_response.status_code)
            self.assertEqual(0, count_unread_notifications(db_path))

            delete_response = client.post(f"/notifications/{notification.id}/delete", data={"status": "all"})
            self.assertEqual(302, delete_response.status_code)
            self.assertEqual([], list_notifications(db_path, unread_only=False))

    @unittest.skipIf(flask is None, "Flask is not installed in this environment.")
    def test_toggle_active_route_returns_dashboard_state_json(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            app = create_app(
                {
                    "TESTING": True,
                    "START_SCHEDULER": False,
                    "DATABASE_PATH": db_path,
                }
            )

            profile = create_profile(
                db_path,
                FeedRequest(
                    feed_title="Forum Feed",
                    source_url="https://example.com/forum",
                    item_selector=".topic",
                    title_selector="a",
                    link_selector="a",
                    summary_selector="",
                    max_items=10,
                    refresh_interval_minutes=60,
                    fetch_mode="http",
                ),
            )

            client = app.test_client()
            disable_response = client.post(
                f"/profiles/{profile.id}/toggle-active",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            self.assertEqual(200, disable_response.status_code)
            self.assertEqual("disabled", disable_response.get_json()["status"])
            self.assertFalse(disable_response.get_json()["active"])

            enable_response = client.post(
                f"/profiles/{profile.id}/toggle-active",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            self.assertEqual(200, enable_response.status_code)
            self.assertEqual("idle", enable_response.get_json()["status"])
            self.assertTrue(enable_response.get_json()["active"])

    @unittest.skipIf(flask is None, "Flask is not installed in this environment.")
    def test_edit_route_updates_profile(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            app = create_app(
                {
                    "TESTING": True,
                    "START_SCHEDULER": False,
                    "DATABASE_PATH": db_path,
                }
            )
            profile = create_profile(
                db_path,
                FeedRequest(
                    feed_title="Forum Feed",
                    source_url="https://example.com/forum",
                    item_selector=".topic",
                    title_selector="a",
                    link_selector="a",
                    summary_selector="",
                    max_items=10,
                    refresh_interval_minutes=60,
                    fetch_mode="http",
                ),
            )

            client = app.test_client()
            response = client.post(
                f"/profiles/{profile.id}/edit",
                data={
                    "feed_title": "Renamed Feed",
                    "source_url": "https://example.com/updated",
                    "item_selector": ".updated",
                    "title_selector": ".updated-title",
                    "link_selector": ".updated-title a",
                    "summary_selector": ".summary",
                    "max_items": "12",
                    "refresh_interval_minutes": "90",
                    "fetch_mode": "http",
                },
            )

            self.assertEqual(302, response.status_code)
            updated = get_profile_by_id(db_path, profile.id)
            self.assertEqual("Renamed Feed", updated.feed_title)

    @unittest.skipIf(flask is None, "Flask is not installed in this environment.")
    def test_delete_route_removes_profile(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            app = create_app(
                {
                    "TESTING": True,
                    "START_SCHEDULER": False,
                    "DATABASE_PATH": db_path,
                }
            )
            profile = create_profile(
                db_path,
                FeedRequest(
                    feed_title="Forum Feed",
                    source_url="https://example.com/forum",
                    item_selector=".topic",
                    title_selector="a",
                    link_selector="a",
                    summary_selector="",
                    max_items=10,
                    refresh_interval_minutes=60,
                    fetch_mode="http",
                ),
            )

            client = app.test_client()
            response = client.post(f"/profiles/{profile.id}/delete")

            self.assertEqual(302, response.status_code)
            self.assertIsNone(get_profile_by_id(db_path, profile.id))

    @unittest.skipIf(flask is None, "Flask is not installed in this environment.")
    def test_safe_browser_routes_render_control_and_serve_downloads(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            app = create_app(
                {
                    "TESTING": True,
                    "START_SCHEDULER": False,
                    "DATABASE_PATH": db_path,
                }
            )
            profile = create_profile(
                db_path,
                FeedRequest(
                    feed_title="Forum Feed",
                    source_url="https://example.com/forum",
                    item_selector=".topic",
                    title_selector="a",
                    link_selector="a",
                    summary_selector="",
                    max_items=10,
                    refresh_interval_minutes=60,
                    fetch_mode="http",
                ),
            )
            discovered_at = datetime.now(timezone.utc).isoformat()
            with closing(sqlite3.connect(db_path)) as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO feed_items (profile_id, title, link, summary, discovered_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (profile.id, "Topic A", "https://example.com/forum/topic-a", "", discovered_at),
                )
                item_id = cursor.lastrowid
                conn.commit()

            download_path = Path(tmpdir) / "example.torrent"
            download_path.write_bytes(b"torrent-data")

            class FakeSafeSession:
                id = "safe-session"

                def execute(self, action, **_payload):
                    if action == "screenshot":
                        return b"png-data"
                    if action == "download":
                        return {"path": download_path, "name": "example.torrent"}
                    return {
                        "url": "https://example.com/forum/topic-a",
                        "title": "Topic A",
                        "blocked_requests": 3,
                        "popup_attempts": 2,
                        "downloads": [],
                    }

                def stop(self):
                    return None

            safe_session = FakeSafeSession()
            client = app.test_client()
            detail = client.get(f"/profiles/{profile.id}")
            with patch("rss_site_bridge.app.create_safe_browser_session", return_value=safe_session):
                response = client.get(f"/profiles/{profile.id}/items/{item_id}/safe")

            self.assertIn(b'class="danger-link"', detail.data)
            self.assertEqual(200, response.status_code)
            self.assertIn(b"Interactive safe browser viewport", response.data)
            self.assertIn(b"Downloads", response.data)
            self.assertIn(b"data-browser-download-badge", response.data)
            self.assertIn(b"data-browser-download-popover", response.data)
            self.assertIn(b"File downloaded. Check Downloads.", response.data)
            self.assertIn(b"safe-browser-download-extension", response.data)
            self.assertIn(b"name.title = fullName", response.data)
            self.assertIn(b"queueScrollDelta(event.deltaY, event.deltaY, x, y)", response.data)
            self.assertIn(b"flushScrollQueue", response.data)
            self.assertIn(b'touch-action: none', response.data)
            self.assertIn(b'screen.addEventListener("pointermove"', response.data)
            self.assertIn(b'queueScrollDelta(touchDelta * scale, touchDelta, x, y)', response.data)
            self.assertIn(b'suppressNextClick = true', response.data)
            self.assertIn(b'remoteScrollY >= remoteScrollMaxY - 1', response.data)
            self.assertIn(b'window.scrollBy({top: outerDelta', response.data)
            self.assertIn(b'pendingOuterScrollDelta += outerDelta', response.data)
            self.assertIn(b'isRemoteBoundary(pendingScrollDelta, pendingScrollX, pendingScrollY)', response.data)
            self.assertIn(b'JSON.stringify({action: "scroll", delta, x, y})', response.data)
            self.assertIn(b'Number(state.scroll_applied_delta)', response.data)
            self.assertIn(b'outerDelta * unusedRatio', response.data)
            self.assertIn(b'window.matchMedia("(max-width: 720px)").matches', response.data)
            self.assertIn(b'command("viewport", {mode: "mobile"})', response.data)
            self.assertIn(b'data-browser-viewport-mode="desktop"', response.data)
            self.assertIn(b'data-browser-viewport-mode="mobile"', response.data)
            self.assertIn(b'data-mobile-nav-toggle', response.data)
            self.assertIn(b'aria-controls="sidebar-menu"', response.data)
            self.assertIn(b'data-mobile-nav-backdrop', response.data)
            self.assertIn(b'setMobileNavOpen', response.data)
            self.assertIn(b'@media (max-width: 390px)', response.data)
            self.assertIn("frame-src 'none'", response.headers["Content-Security-Policy"])

            base = f"/profiles/{profile.id}/items/{item_id}/safe/{safe_session.id}"
            with patch("rss_site_bridge.app.get_safe_browser_session", return_value=safe_session):
                screenshot = client.get(f"{base}/screenshot")
                command = client.post(f"{base}/command", json={"action": "click", "x": 20, "y": 30})
                viewport = client.post(f"{base}/command", json={"action": "viewport", "mode": "mobile"})
                download = client.get(f"{base}/downloads/file-id")

            self.assertEqual(b"png-data", screenshot.data)
            self.assertEqual(3, command.get_json()["blocked_requests"])
            self.assertEqual(200, viewport.status_code)
            self.assertEqual(b"torrent-data", download.data)
            self.assertEqual("attachment; filename=example.torrent", download.headers["Content-Disposition"])
            download.close()

    @unittest.skipIf(flask is None, "Flask is not installed in this environment.")
    def test_delete_route_returns_json_for_ajax_request(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "rss.db"
            app = create_app(
                {
                    "TESTING": True,
                    "START_SCHEDULER": False,
                    "DATABASE_PATH": db_path,
                }
            )
            profile = create_profile(
                db_path,
                FeedRequest(
                    feed_title="Forum Feed",
                    source_url="https://example.com/forum",
                    item_selector=".topic",
                    title_selector="a",
                    link_selector="a",
                    summary_selector="",
                    max_items=10,
                    refresh_interval_minutes=60,
                    fetch_mode="http",
                ),
            )

            client = app.test_client()
            response = client.post(
                f"/profiles/{profile.id}/delete",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )

            self.assertEqual(200, response.status_code)
            self.assertEqual({"deleted": True}, response.get_json())
            self.assertIsNone(get_profile_by_id(db_path, profile.id))


if __name__ == "__main__":
    unittest.main()
