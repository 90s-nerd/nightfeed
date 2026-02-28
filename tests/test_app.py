from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import unittest
from unittest.mock import patch

try:
    import flask  # noqa: F401
except ModuleNotFoundError:
    flask = None

from rss_site_bridge.app import (
    FeedEntry,
    FeedRequest,
    build_clone_title,
    create_app,
    create_profile,
    delete_profile,
    extract_feed_entries,
    get_app_settings,
    get_profile_by_token,
    get_profile_by_id,
    humanize_datetime,
    init_db,
    list_feed_items,
    list_profiles,
    normalize_source_url,
    normalize_topic_link,
    refresh_due_profiles,
    refresh_profile,
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
            with sqlite3.connect(db_path) as conn:
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

            enabled = set_profile_active(db_path, profile.id, active=True)
            self.assertTrue(enabled.active)
            self.assertEqual("idle", enabled.last_status)
            self.assertFalse(should_refresh(enabled))

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
                },
            )

            self.assertEqual(302, response.status_code)
            settings = get_app_settings(db_path)
            self.assertEqual("America/Chicago", settings.timezone_name)
            self.assertEqual("https://rss.example.com", settings.public_base_url)

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
            self.assertEqual("ok", payload["status"])

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
