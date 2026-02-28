from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from fnmatch import fnmatchcase
from html import escape
from pathlib import Path
from queue import Queue
from threading import Event, Lock, Thread
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
import json
import os
import secrets
import re
import sqlite3
import xml.etree.ElementTree as ET
from xml.dom import minidom
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    from flask import Flask, Response, g, jsonify, redirect, render_template, request, stream_with_context, url_for
except ModuleNotFoundError:
    Flask = Any  # type: ignore[assignment]
    Response = Any  # type: ignore[assignment]
    g = None
    jsonify = None
    redirect = None
    render_template = None
    request = None
    stream_with_context = None
    url_for = None

try:
    from werkzeug.middleware.proxy_fix import ProxyFix
except ModuleNotFoundError:
    ProxyFix = None


MAX_RESPONSE_BYTES = 2 * 1024 * 1024
DEFAULT_USER_AGENT = "rss-site-bridge/0.2 (+https://localhost)"
SCHEDULER_INTERVAL_SECONDS = 30
_scheduler_lock = Lock()
_scheduler_started = False


class RedirectBlocked(URLError):
    pass


class BlockRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RedirectBlocked(f"Blocked redirect to {newurl}")


@dataclass
class FeedRequest:
    feed_title: str
    source_url: str
    item_selector: str
    title_selector: str
    link_selector: str
    summary_selector: str
    max_items: int
    refresh_interval_minutes: int
    fetch_mode: str
    filter_rules: str = ""
    exclude_filter_rules: str = ""


@dataclass
class FeedEntry:
    title: str
    link: str
    summary: str
    published_at: datetime


@dataclass
class StoredProfile:
    id: int
    feed_token: str
    feed_title: str
    source_url: str
    item_selector: str
    title_selector: str
    link_selector: str
    summary_selector: str
    filter_rules: str
    exclude_filter_rules: str
    max_items: int
    refresh_interval_minutes: int
    fetch_mode: str
    active: bool
    last_status: str
    last_error: str
    last_refreshed_at: str
    refresh_anchor_at: str
    created_at: str
    updated_at: str
    item_count: int = 0

    def to_feed_request(self) -> FeedRequest:
        return FeedRequest(
            feed_title=self.feed_title,
            source_url=self.source_url,
            item_selector=self.item_selector,
            title_selector=self.title_selector,
            link_selector=self.link_selector,
            summary_selector=self.summary_selector,
            filter_rules=self.filter_rules,
            exclude_filter_rules=self.exclude_filter_rules,
            max_items=self.max_items,
            refresh_interval_minutes=self.refresh_interval_minutes,
            fetch_mode=self.fetch_mode,
        )


@dataclass
class AppSettings:
    timezone_name: str = "UTC"
    public_base_url: str = ""


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    if (
        render_template is None
        or request is None
        or redirect is None
        or url_for is None
        or g is None
        or jsonify is None
    ):
        raise RuntimeError("Flask is required to run the web application.")

    app = Flask(__name__)
    if ProxyFix is not None:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)

    db_path = Path(os.environ.get("NIGHTFEED_DATABASE_PATH", "data/rss_site_bridge.db"))
    start_scheduler = os.environ.get("NIGHTFEED_START_SCHEDULER", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    app.config.update(
        DATABASE_PATH=db_path,
        START_SCHEDULER=start_scheduler,
        TESTING=False,
    )
    if test_config:
        app.config.update(test_config)

    init_db(Path(app.config["DATABASE_PATH"]))
    if app.config.get("START_SCHEDULER") and not app.config.get("TESTING"):
        ensure_scheduler(app)

    @app.before_request
    def load_app_settings_into_context() -> None:
        g.app_settings = get_app_settings(Path(app.config["DATABASE_PATH"]))

    @app.template_filter("human_datetime")
    def human_datetime_filter(value: str | datetime) -> str:
        settings = getattr(g, "app_settings", AppSettings())
        return humanize_datetime(value, settings.timezone_name)

    def render_compose_page(
        *,
        form: dict[str, str],
        preview: list[FeedEntry],
        error: str | None,
    ) -> str:
        return render_template(
            "compose.html",
            profiles=list_profiles(Path(app.config["DATABASE_PATH"])),
            form=form,
            preview=preview,
            error=error,
        )

    def serialize_preview(entries: list[FeedEntry]) -> list[dict[str, str]]:
        return [
            {
                "title": entry.title,
                "link": entry.link,
            }
            for entry in entries
        ]

    def sse_event(event_name: str, payload: dict[str, Any]) -> str:
        return f"event: {event_name}\ndata: {json.dumps(payload)}\n\n"

    def stream_preview_response(config: FeedRequest) -> Response:
        event_queue: Queue[str | None] = Queue()

        def emit_progress_event(title: str, detail: str) -> None:
            event_queue.put(
                sse_event(
                    "stage",
                    {
                        "title": title,
                        "detail": detail,
                    },
                )
            )

        def run_preview_job() -> None:
            try:
                event_queue.put(
                    sse_event(
                        "stage",
                        {
                            "title": "Validating request",
                            "detail": "Checking the source URL, selectors, and filters.",
                        },
                    )
                )
                preview = extract_feed_entries(
                    config,
                    progress=emit_progress_event,
                )
                event_queue.put(
                    sse_event(
                        "result",
                        {
                            "items": serialize_preview(preview),
                            "error": None,
                        },
                    )
                )
            except (ValueError, RuntimeError) as exc:
                event_queue.put(sse_event("error", {"error": str(exc)}))
            finally:
                event_queue.put(sse_event("done", {}))
                event_queue.put(None)

        Thread(target=run_preview_job, daemon=True).start()

        @stream_with_context
        def generate():
            while True:
                chunk = event_queue.get()
                if chunk is None:
                    break
                yield chunk

        return Response(
            generate(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    def render_detail_page(
        *,
        profile: StoredProfile,
        edit_form: dict[str, str] | None = None,
        edit_error: str | None = None,
        preview: list[FeedEntry] | None = None,
        preview_error: str | None = None,
        purged: bool = False,
    ) -> str:
        settings = getattr(g, "app_settings", AppSettings())
        return render_template(
            "detail.html",
            profiles=list_profiles(Path(app.config["DATABASE_PATH"])),
            profile=profile,
            items=list_feed_items(Path(app.config["DATABASE_PATH"]), profile.id, profile.max_items),
            feed_url=build_feed_url(request.url_root, profile.feed_token, settings.public_base_url),
            edit_error=edit_error,
            edit_form=edit_form or {},
            preview=preview or [],
            preview_error=preview_error,
            purged=purged,
        )

    @app.get("/")
    def index() -> str:
        profiles = list_profiles(Path(app.config["DATABASE_PATH"]))
        return render_template("index.html", profiles=profiles)

    @app.get("/compose")
    def compose_route() -> str:
        form = load_form()
        preview: list[FeedEntry] = []
        error = None
        form_values = request.args.to_dict()
        form_values.pop("preview", None)
        if form_values:
            form = form | form_values
        if request.args.get("preview") == "1":
            try:
                config = parse_request_values(form_values)
                preview = extract_feed_entries(config)
            except (ValueError, RuntimeError) as exc:
                error = str(exc)

        return render_compose_page(form=form, preview=preview, error=error)

    @app.post("/preview")
    def compose_preview_route() -> Response:
        try:
            config = parse_request_values(request.form)
            preview = extract_feed_entries(config)
            return jsonify({"items": serialize_preview(preview), "error": None})
        except (ValueError, RuntimeError) as exc:
            return jsonify({"items": [], "error": str(exc)}), 400

    @app.get("/preview/stream")
    def compose_preview_stream_route() -> Response:
        try:
            config = parse_request_values(request.args)
        except (ValueError, RuntimeError) as exc:
            return Response(
                sse_event("error", {"error": str(exc)}) + sse_event("done", {}),
                mimetype="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )
        return stream_preview_response(config)

    @app.get("/settings")
    def settings_route() -> str:
        return render_template(
            "settings.html",
            settings=getattr(g, "app_settings", AppSettings()),
            error=None,
            saved=request.args.get("saved") == "1",
        )

    @app.post("/settings")
    def update_settings_route() -> Response | tuple[str, int]:
        try:
            settings = update_app_settings(
                Path(app.config["DATABASE_PATH"]),
                timezone_name=request.form.get("timezone_name", ""),
                public_base_url=request.form.get("public_base_url", ""),
            )
            g.app_settings = settings
            return redirect(url_for("settings_route", saved=1))
        except ValueError as exc:
            return (
                render_template(
                    "settings.html",
                    settings=AppSettings(
                        timezone_name=request.form.get("timezone_name", "").strip() or "UTC",
                        public_base_url=request.form.get("public_base_url", "").strip(),
                    ),
                    error=str(exc),
                    saved=False,
                ),
                400,
            )

    @app.post("/profiles")
    def create_profile_route() -> Response:
        try:
            config = parse_request_values(request.form)
            profile = create_profile(Path(app.config["DATABASE_PATH"]), config)
            return redirect(url_for("profile_detail", profile_id=profile.id))
        except (ValueError, RuntimeError) as exc:
            return (
                render_compose_page(
                    form=load_form() | request.form.to_dict(),
                    preview=[],
                    error=str(exc),
                ),
                400,
            )

    @app.get("/profiles/<int:profile_id>/clone")
    def clone_profile_route(profile_id: int) -> Response:
        profile = get_profile_by_id(Path(app.config["DATABASE_PATH"]), profile_id)
        if profile is None:
            return Response("Profile not found.", status=404, mimetype="text/plain; charset=utf-8")
        clone_title = build_clone_title(Path(app.config["DATABASE_PATH"]), profile.feed_title)
        return redirect(
            url_for(
                "compose_route",
                feed_title=clone_title,
                source_url=profile.source_url,
                item_selector=profile.item_selector,
                title_selector=profile.title_selector,
                link_selector=profile.link_selector,
                summary_selector=profile.summary_selector,
                filter_rules=profile.filter_rules,
                exclude_filter_rules=profile.exclude_filter_rules,
                max_items=profile.max_items,
                refresh_interval_minutes=profile.refresh_interval_minutes,
                fetch_mode=profile.fetch_mode,
            )
        )

    @app.post("/profiles/<int:profile_id>/refresh")
    def refresh_profile_route(profile_id: int) -> Response:
        try:
            refresh_profile(Path(app.config["DATABASE_PATH"]), profile_id)
        except ValueError as exc:
            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return jsonify({"error": str(exc), "status": "error"}), 404
            return Response(str(exc), status=404, mimetype="text/plain; charset=utf-8")
        except RuntimeError as exc:
            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return jsonify({"error": str(exc), "status": "error"}), 400
            return Response(str(exc), status=400, mimetype="text/plain; charset=utf-8")
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            profile = get_profile_by_id(Path(app.config["DATABASE_PATH"]), profile_id)
            if profile is None:
                return jsonify({"error": "Profile not found.", "status": "error"}), 404
            settings = getattr(g, "app_settings", AppSettings())
            return jsonify(
                {
                    "item_count": profile.item_count,
                    "last_refreshed_at": humanize_datetime(profile.last_refreshed_at, settings.timezone_name),
                    "status": profile.last_status,
                    "active": profile.active,
                }
            )
        return redirect(url_for("profile_detail", profile_id=profile_id))

    @app.post("/profiles/<int:profile_id>/edit")
    def edit_profile_route(profile_id: int) -> Response | tuple[str, int]:
        try:
            config = parse_request_values(request.form)
            update_profile(Path(app.config["DATABASE_PATH"]), profile_id, config)
            return redirect(url_for("profile_detail", profile_id=profile_id))
        except ValueError as exc:
            return Response(str(exc), status=404, mimetype="text/plain; charset=utf-8")
        except RuntimeError as exc:
            profile = get_profile_by_id(Path(app.config["DATABASE_PATH"]), profile_id)
            if profile is None:
                return Response("Profile not found.", status=404, mimetype="text/plain; charset=utf-8")
            return (
                render_detail_page(
                    profile=profile,
                    edit_error=str(exc),
                    edit_form=request.form.to_dict(),
                ),
                400,
            )

    @app.post("/profiles/<int:profile_id>/delete")
    def delete_profile_route(profile_id: int) -> Response:
        try:
            delete_profile(Path(app.config["DATABASE_PATH"]), profile_id)
        except ValueError as exc:
            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return jsonify({"error": str(exc)}), 404
            return Response(str(exc), status=404, mimetype="text/plain; charset=utf-8")
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"deleted": True})
        return redirect(url_for("index"))

    @app.post("/profiles/<int:profile_id>/purge")
    def purge_profile_items_route(profile_id: int) -> Response:
        try:
            purge_feed_items(Path(app.config["DATABASE_PATH"]), profile_id)
        except ValueError as exc:
            return Response(str(exc), status=404, mimetype="text/plain; charset=utf-8")
        return redirect(url_for("profile_detail", profile_id=profile_id, purged=1))

    @app.post("/profiles/<int:profile_id>/toggle-active")
    def toggle_profile_active_route(profile_id: int) -> Response:
        profile = get_profile_by_id(Path(app.config["DATABASE_PATH"]), profile_id)
        if profile is None:
            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return jsonify({"error": "Profile not found."}), 404
            return Response("Profile not found.", status=404, mimetype="text/plain; charset=utf-8")

        toggled = set_profile_active(Path(app.config["DATABASE_PATH"]), profile_id, active=not profile.active)
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            settings = getattr(g, "app_settings", AppSettings())
            return jsonify(
                {
                    "active": toggled.active,
                    "status": toggled.last_status,
                    "last_refreshed_at": humanize_datetime(toggled.last_refreshed_at, settings.timezone_name),
                }
            )
        return redirect(url_for("profile_detail", profile_id=profile_id))

    @app.get("/profiles/<int:profile_id>")
    def profile_detail(profile_id: int) -> Response | str:
        profile = get_profile_by_id(Path(app.config["DATABASE_PATH"]), profile_id)
        if profile is None:
            return Response("Profile not found.", status=404, mimetype="text/plain; charset=utf-8")
        edit_form: dict[str, str] = {}
        preview: list[FeedEntry] = []
        preview_error = None
        if request.args.get("preview") == "1":
            edit_form = request.args.to_dict()
            edit_form.pop("preview", None)
            try:
                config = parse_request_values(edit_form)
                preview = extract_feed_entries(config)
            except (ValueError, RuntimeError) as exc:
                preview_error = str(exc)
        return render_detail_page(
            profile=profile,
            edit_error=None,
            edit_form=edit_form,
            preview=preview,
            preview_error=preview_error,
            purged=request.args.get("purged") == "1",
        )

    @app.post("/profiles/<int:profile_id>/preview")
    def profile_preview_route(profile_id: int) -> Response:
        profile = get_profile_by_id(Path(app.config["DATABASE_PATH"]), profile_id)
        if profile is None:
            return jsonify({"items": [], "error": "Profile not found."}), 404
        try:
            config = parse_request_values(request.form)
            preview = extract_feed_entries(config)
            return jsonify({"items": serialize_preview(preview), "error": None})
        except (ValueError, RuntimeError) as exc:
            return jsonify({"items": [], "error": str(exc)}), 400

    @app.get("/profiles/<int:profile_id>/preview/stream")
    def profile_preview_stream_route(profile_id: int) -> Response:
        profile = get_profile_by_id(Path(app.config["DATABASE_PATH"]), profile_id)
        if profile is None:
            return Response(
                sse_event("error", {"error": "Profile not found."}) + sse_event("done", {}),
                mimetype="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )
        try:
            config = parse_request_values(request.args)
        except (ValueError, RuntimeError) as exc:
            return Response(
                sse_event("error", {"error": str(exc)}) + sse_event("done", {}),
                mimetype="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )
        return stream_preview_response(config)

    @app.get("/feeds/<token>.xml")
    def feed_route(token: str) -> Response:
        profile, entries = load_feed_payload(Path(app.config["DATABASE_PATH"]), token)
        if profile is None:
            return Response("Feed not found.", status=404, mimetype="text/plain; charset=utf-8")
        if not profile.active:
            return Response("Feed not found.", status=404, mimetype="text/plain; charset=utf-8")
        xml_payload = render_rss(profile.to_feed_request(), entries)
        return Response(xml_payload, mimetype="application/rss+xml; charset=utf-8")

    @app.get("/feeds/<token>/view")
    def feed_view_route(token: str) -> Response | str:
        profile, entries = load_feed_payload(Path(app.config["DATABASE_PATH"]), token)
        if profile is None:
            return Response("Feed not found.", status=404, mimetype="text/plain; charset=utf-8")
        if not profile.active:
            return Response("Feed not found.", status=404, mimetype="text/plain; charset=utf-8")
        xml_payload = render_rss(profile.to_feed_request(), entries)
        return render_template(
            "xml_view.html",
            profile=profile,
            feed_url=build_feed_url(request.url_root, profile.feed_token, getattr(g, "app_settings", AppSettings()).public_base_url),
            xml_payload=highlight_xml(format_xml(xml_payload)),
        )

    return app


def ensure_scheduler(app: Flask) -> None:
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return

        stop_event = Event()
        app.config["SCHEDULER_STOP_EVENT"] = stop_event
        thread = Thread(
            target=run_scheduler_loop,
            args=(Path(app.config["DATABASE_PATH"]), stop_event),
            daemon=True,
            name="rss-site-bridge-scheduler",
        )
        thread.start()
        app.config["SCHEDULER_THREAD"] = thread
        _scheduler_started = True


def run_scheduler_loop(db_path: Path, stop_event: Event) -> None:
    while not stop_event.is_set():
        refresh_due_profiles(db_path)
        stop_event.wait(SCHEDULER_INTERVAL_SECONDS)


def load_form() -> dict[str, str]:
    return {
        "feed_title": "Example Topic Feed",
        "source_url": "",
        "item_selector": "article, li, .topic-row",
        "title_selector": "a",
        "link_selector": "a",
        "summary_selector": "",
        "filter_rules": "",
        "exclude_filter_rules": "",
        "max_items": "25",
        "refresh_interval_minutes": "60",
        "fetch_mode": "http",
    }


def build_clone_title(db_path: Path, original_title: str) -> str:
    existing_titles = {profile.feed_title.casefold() for profile in list_profiles(db_path)}
    match = re.fullmatch(r"(?P<base>.*?)(?: copy(?: (?P<suffix>\d+))?)?", original_title.strip(), re.IGNORECASE)
    base_name = match.group("base").strip() if match else original_title.strip()
    base_title = f"{base_name} copy"
    if base_title.casefold() not in existing_titles:
        return base_title

    suffix = 1
    current_suffix_match = re.fullmatch(rf"{re.escape(base_title)} (?P<suffix>\d+)", original_title.strip(), re.IGNORECASE)
    if current_suffix_match:
        suffix = int(current_suffix_match.group("suffix")) + 1
    while True:
        candidate = f"{base_title} {suffix}"
        if candidate.casefold() not in existing_titles:
            return candidate
        suffix += 1


def parse_request_values(values: Any) -> FeedRequest:
    source_url = normalize_source_url(values.get("source_url", ""))
    fetch_mode = values.get("fetch_mode", "http").strip() or "http"
    if fetch_mode not in {"http", "browser"}:
        raise ValueError("Fetch mode must be http or browser.")

    return FeedRequest(
        feed_title=require_text(values.get("feed_title", ""), "Feed title"),
        source_url=source_url,
        item_selector=require_text(values.get("item_selector", ""), "Item selector"),
        title_selector=require_text(values.get("title_selector", ""), "Title selector"),
        link_selector=require_text(values.get("link_selector", ""), "Link selector"),
        summary_selector=values.get("summary_selector", "").strip(),
        filter_rules=normalize_filter_rules(values.get("filter_rules", "")),
        exclude_filter_rules=normalize_filter_rules(values.get("exclude_filter_rules", "")),
        max_items=parse_max_items(values.get("max_items", "25")),
        refresh_interval_minutes=parse_refresh_interval(values.get("refresh_interval_minutes", "60")),
        fetch_mode=fetch_mode,
    )


def normalize_source_url(source_url: str) -> str:
    source_url = source_url.strip()
    parsed = urlparse(source_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Source URL must be a full http or https URL.")
    return source_url


def require_text(value: str, label: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{label} is required.")
    return value


def parse_max_items(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("Max items must be a number.") from exc
    if value < 1 or value > 100:
        raise ValueError("Max items must be between 1 and 100.")
    return value


def parse_refresh_interval(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("Refresh interval must be a number of minutes.") from exc
    if value < 0 or value > 1440:
        raise ValueError("Refresh interval must be between 0 and 1440 minutes.")
    return value


def normalize_filter_rules(raw: str) -> str:
    lines = [line.strip() for line in raw.splitlines()]
    return "\n".join(line for line in lines if line)


def pick_selected_profile(profiles: list[StoredProfile], selected_profile_id: int | None) -> StoredProfile | None:
    if not profiles:
        return None
    if selected_profile_id is None:
        return profiles[0]
    for profile in profiles:
        if profile.id == selected_profile_id:
            return profile
    return profiles[0]


def emit_progress(progress: Callable[[str, str], None] | None, title: str, detail: str) -> None:
    if progress is not None:
        progress(title, detail)


def extract_feed_entries(
    config: FeedRequest,
    *,
    progress: Callable[[str, str], None] | None = None,
) -> list[FeedEntry]:
    try:
        from bs4 import BeautifulSoup
    except ModuleNotFoundError as exc:
        raise RuntimeError("beautifulsoup4 is required to extract topics from HTML.") from exc

    emit_progress(progress, "Accessing website", "Opening the source page.")
    html = fetch_html(config.source_url, config.fetch_mode, progress=progress)
    emit_progress(progress, "Analyzing document", "Parsing the returned HTML.")
    soup = BeautifulSoup(html, "html.parser")
    emit_progress(progress, "Matching selectors", "Locating item nodes from the page.")
    nodes = soup.select(config.item_selector)
    if not nodes:
        raise ValueError("No topic nodes matched the item selector.")
    emit_progress(progress, "Matching selectors", f"Matched {len(nodes)} item nodes.")

    parsed_filter_rules = parse_filter_rules(config.filter_rules)
    parsed_exclude_filter_rules = parse_filter_rules(config.exclude_filter_rules)
    now = datetime.now(timezone.utc)
    emit_progress(progress, "Extracting entries", "Reading titles and links from matched nodes.")
    entries = extract_entries_from_item_nodes(
        nodes,
        config,
        now,
        progress=progress,
        parsed_filter_rules=parsed_filter_rules,
        parsed_exclude_filter_rules=parsed_exclude_filter_rules,
    )
    if should_use_container_link_fallback(nodes, entries, config):
        emit_progress(progress, "Expanding repeated links", "Detected grouped links inside a container. Expanding them.")
        container_entries = extract_entries_from_link_collections(
            nodes,
            config,
            now,
            progress=progress,
            parsed_filter_rules=parsed_filter_rules,
            parsed_exclude_filter_rules=parsed_exclude_filter_rules,
        )
        if len(container_entries) > len(entries):
            entries = container_entries

    if not entries:
        raise ValueError("Matched nodes did not contain usable titles and links.")
    emit_progress(progress, "Applying filters", "Evaluating filter rules against extracted titles.")
    filtered_entries = apply_entry_filters(entries, parsed_filter_rules, parsed_exclude_filter_rules)[: config.max_items]
    emit_progress(progress, "Preparing preview", "Formatting extracted titles and links.")
    return filtered_entries


def select_node_with_scope(node: Any, selector: str) -> Any:
    normalized_selector = selector.strip()
    if normalized_selector in {":scope", "self"}:
        return node
    return node.select_one(selector)


def select_nodes_with_scope(node: Any, selector: str) -> list[Any]:
    normalized_selector = selector.strip()
    if normalized_selector in {":scope", "self"}:
        return [node]
    return list(node.select(selector))


def extract_entries_from_item_nodes(
    nodes: list[Any],
    config: FeedRequest,
    now: datetime,
    *,
    progress: Callable[[str, str], None] | None = None,
    parsed_filter_rules: list[Any] | None = None,
    parsed_exclude_filter_rules: list[Any] | None = None,
) -> list[FeedEntry]:
    entries: list[FeedEntry] = []
    total = len(nodes)
    parsed_filter_rules = parsed_filter_rules or []
    parsed_exclude_filter_rules = parsed_exclude_filter_rules or []
    for index, node in enumerate(nodes, start=1):
        if index == 1 or index % 25 == 0 or index == total:
            emit_progress(progress, "Extracting entries", f"Processed {index} of {total} matched nodes.")
        entry = build_entry_from_nodes(
            container_node=node,
            title_node=select_node_with_scope(node, config.title_selector),
            link_node=select_node_with_scope(node, config.link_selector),
            config=config,
            now=now,
        )
        if entry is None:
            continue
        if parsed_filter_rules and not entry_matches_any_filter_rule(entry, parsed_filter_rules):
            continue
        if parsed_exclude_filter_rules and entry_matches_any_filter_rule(entry, parsed_exclude_filter_rules):
            continue
        entries.append(entry)
        if len(entries) >= config.max_items:
            break
    return entries


def should_use_container_link_fallback(nodes: list[Any], entries: list[FeedEntry], config: FeedRequest) -> bool:
    if len(entries) > 1:
        return False
    for node in nodes:
        if len(select_nodes_with_scope(node, config.link_selector)) > 1:
            return True
    return False


def extract_entries_from_link_collections(
    nodes: list[Any],
    config: FeedRequest,
    now: datetime,
    *,
    progress: Callable[[str, str], None] | None = None,
    parsed_filter_rules: list[Any] | None = None,
    parsed_exclude_filter_rules: list[Any] | None = None,
) -> list[FeedEntry]:
    entries: list[FeedEntry] = []
    seen_links: set[str] = set()
    parsed_filter_rules = parsed_filter_rules or []
    parsed_exclude_filter_rules = parsed_exclude_filter_rules or []
    link_groups = [select_nodes_with_scope(node, config.link_selector) for node in nodes]
    total_links = sum(len(link_nodes) for link_nodes in link_groups)
    processed_links = 0
    for node, link_nodes in zip(nodes, link_groups):
        for link_node in link_nodes:
            processed_links += 1
            title_node = resolve_repeated_link_title_node(link_node, config)
            title_text = extract_repeated_link_title_text(node, link_node, title_node, config)
            entry = build_entry_from_nodes(
                container_node=node,
                title_node=title_node,
                link_node=link_node,
                title_text=title_text,
                config=config,
                now=now,
            )
            if entry is None or entry.link in seen_links:
                continue
            if parsed_filter_rules and not entry_matches_any_filter_rule(entry, parsed_filter_rules):
                continue
            if parsed_exclude_filter_rules and entry_matches_any_filter_rule(entry, parsed_exclude_filter_rules):
                continue
            seen_links.add(entry.link)
            entries.append(entry)
            if processed_links == 1 or processed_links % 25 == 0 or processed_links == total_links:
                emit_progress(
                    progress,
                    "Expanding repeated links",
                    f"Processed {processed_links} of {total_links} linked topics.",
                )
            if len(entries) >= config.max_items:
                return entries
    return entries


def parse_filter_rule_lines(filter_rules: str) -> list[str]:
    return [line.strip() for line in filter_rules.splitlines() if line.strip()]


def parse_filter_rules(filter_rules: str) -> list[Any]:
    return [parse_filter_expression(rule) for rule in parse_filter_rule_lines(filter_rules)]


def entry_matches_filter_term(entry: FeedEntry, term: str) -> bool:
    title = entry.title.casefold()
    normalized_term = term.casefold()
    if "*" in normalized_term or "?" in normalized_term:
        pattern = normalized_term
        if not pattern.startswith("*"):
            pattern = f"*{pattern}"
        if not pattern.endswith("*"):
            pattern = f"{pattern}*"
        return fnmatchcase(title, pattern)
    return normalized_term in title


def parse_filter_expression(expression: str) -> Any:
    normalized_expression = expression.strip()
    if not normalized_expression:
        raise ValueError("Filter expressions cannot be empty.")
    if not re.search(r"\b(?:AND|OR)\b|[()]", normalized_expression, re.IGNORECASE):
        return ("TERM", normalized_expression)

    tokens = tokenize_filter_expression(normalized_expression)
    position = 0

    def current() -> tuple[str, str] | None:
        if position >= len(tokens):
            return None
        return tokens[position]

    def consume(expected_type: str | None = None) -> tuple[str, str]:
        nonlocal position
        token = current()
        if token is None:
            raise ValueError("Incomplete filter expression.")
        if expected_type is not None and token[0] != expected_type:
            raise ValueError(f"Expected {expected_type.lower()} in filter expression.")
        position += 1
        return token

    def parse_or_expression() -> Any:
        node = parse_and_expression()
        while current() is not None and current()[0] == "OR":
            consume("OR")
            node = ("OR", node, parse_and_expression())
        return node

    def parse_and_expression() -> Any:
        node = parse_primary()
        while current() is not None and current()[0] == "AND":
            consume("AND")
            node = ("AND", node, parse_primary())
        return node

    def parse_primary() -> Any:
        token = current()
        if token is None:
            raise ValueError("Incomplete filter expression.")
        if token[0] == "TERM":
            return ("TERM", consume("TERM")[1])
        if token[0] == "LPAREN":
            consume("LPAREN")
            node = parse_or_expression()
            consume("RPAREN")
            return node
        raise ValueError("Expected a filter term or parenthesized group.")

    tree = parse_or_expression()
    if current() is not None:
        raise ValueError("Unexpected token in filter expression.")
    return tree


def tokenize_filter_expression(expression: str) -> list[tuple[str, str]]:
    token_pattern = re.compile(
        r"""
        \s*
        (?:
            (?P<LPAREN>\() |
            (?P<RPAREN>\)) |
            (?P<QUOTED>"[^"]*"|'[^']*') |
            (?P<OP>\bAND\b|\bOR\b) |
            (?P<TERM>[^()\s]+)
        )
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    tokens: list[tuple[str, str]] = []
    position = 0
    while position < len(expression):
        match = token_pattern.match(expression, position)
        if match is None:
            raise ValueError("Invalid filter expression.")
        position = match.end()
        if match.lastgroup == "LPAREN":
            tokens.append(("LPAREN", "("))
        elif match.lastgroup == "RPAREN":
            tokens.append(("RPAREN", ")"))
        elif match.lastgroup == "QUOTED":
            tokens.append(("TERM", match.group(match.lastgroup)[1:-1]))
        elif match.lastgroup == "OP":
            tokens.append((match.group(match.lastgroup).upper(), match.group(match.lastgroup).upper()))
        elif match.lastgroup == "TERM":
            tokens.append(("TERM", match.group(match.lastgroup)))
    return tokens


def evaluate_filter_expression(tree: Any, entry: FeedEntry) -> bool:
    node_type = tree[0]
    if node_type == "TERM":
        return entry_matches_filter_term(entry, tree[1])
    if node_type == "AND":
        return evaluate_filter_expression(tree[1], entry) and evaluate_filter_expression(tree[2], entry)
    if node_type == "OR":
        return evaluate_filter_expression(tree[1], entry) or evaluate_filter_expression(tree[2], entry)
    raise ValueError("Unsupported filter expression node.")


def entry_matches_any_filter_rule(entry: FeedEntry, parsed_rules: list[Any]) -> bool:
    return any(evaluate_filter_expression(rule, entry) for rule in parsed_rules)


def apply_entry_filters(
    entries: list[FeedEntry],
    include_rules: list[Any],
    exclude_rules: list[Any],
) -> list[FeedEntry]:
    if not include_rules and not exclude_rules:
        return entries
    return [
        entry
        for entry in entries
        if (not include_rules or entry_matches_any_filter_rule(entry, include_rules))
        and (not exclude_rules or not entry_matches_any_filter_rule(entry, exclude_rules))
    ]


def resolve_repeated_link_title_node(link_node: Any, config: FeedRequest) -> Any:
    normalized_title_selector = config.title_selector.strip()
    normalized_link_selector = config.link_selector.strip()
    if normalized_title_selector in {":scope", "self"}:
        return link_node
    if normalized_title_selector == normalized_link_selector:
        return link_node
    title_node = select_node_with_scope(link_node, config.title_selector)
    if title_node is not None:
        return title_node
    return link_node


def extract_repeated_link_title_text(
    container_node: Any,
    link_node: Any,
    title_node: Any,
    config: FeedRequest,
) -> str | None:
    normalized_title_selector = config.title_selector.strip()
    normalized_link_selector = config.link_selector.strip()
    if normalized_title_selector not in {normalized_link_selector, ":scope", "self"}:
        return None

    if title_node is not None and title_node is not link_node:
        title_text = title_node.get_text(" ", strip=True)
        if title_text:
            return title_text

    contextual_title = extract_inline_title_text(container_node, link_node)
    if contextual_title:
        return contextual_title
    return None


def extract_inline_title_text(container_node: Any, link_node: Any) -> str:
    parent = getattr(link_node, "parent", None)
    if parent is None:
        return link_node.get_text(" ", strip=True)

    anchor_text = link_node.get_text(" ", strip=True)
    siblings = list(getattr(parent, "contents", []))
    try:
        link_index = siblings.index(link_node)
    except ValueError:
        return anchor_text

    title_parts: list[str] = []
    for sibling in reversed(siblings[:link_index]):
        if getattr(sibling, "name", None) == "br":
            break
        if getattr(sibling, "name", None) == "a":
            break
        text = sibling.get_text(" ", strip=True) if hasattr(sibling, "get_text") else str(sibling).strip()
        if text:
            title_parts.insert(0, text)

    if anchor_text:
        title_parts.append(anchor_text)

    for sibling in siblings[link_index + 1 :]:
        if getattr(sibling, "name", None) == "br":
            break
        if getattr(sibling, "name", None) == "a":
            break
        text = sibling.get_text(" ", strip=True) if hasattr(sibling, "get_text") else str(sibling).strip()
        if text:
            title_parts.append(text)

    return " ".join(" ".join(title_parts).split()) or anchor_text


def build_entry_from_nodes(
    *,
    container_node: Any,
    title_node: Any,
    link_node: Any,
    title_text: str | None = None,
    config: FeedRequest,
    now: datetime,
) -> FeedEntry | None:
    if title_node is None or link_node is None:
        return None

    href = link_node.get("href")
    if not href:
        return None

    absolute_link = normalize_topic_link(config.source_url, href)
    title = title_text or title_node.get_text(" ", strip=True)
    if not title:
        return None

    summary = ""
    if config.summary_selector:
        summary_node = select_node_with_scope(container_node, config.summary_selector)
        if summary_node is not None:
            summary = summary_node.get_text(" ", strip=True)

    return FeedEntry(
        title=title,
        link=absolute_link,
        summary=summary,
        published_at=now,
    )


def normalize_topic_link(source_url: str, href: str) -> str:
    joined = urljoin(source_url, href)
    parsed_source = urlparse(source_url)
    parsed_joined = urlparse(joined)

    if parsed_joined.scheme not in {"http", "https"}:
        raise ValueError("Topic links must resolve to http or https URLs.")
    if parsed_joined.netloc != parsed_source.netloc:
        raise ValueError("Off-site topic links are blocked by default.")
    return joined


def fetch_html(
    source_url: str,
    fetch_mode: str,
    *,
    progress: Callable[[str, str], None] | None = None,
) -> str:
    if fetch_mode == "browser":
        return fetch_html_browser(source_url, progress=progress)
    return fetch_html_http(source_url, progress=progress)


def fetch_html_http(
    source_url: str,
    *,
    progress: Callable[[str, str], None] | None = None,
) -> str:
    opener = build_opener(BlockRedirects)
    req = Request(
        source_url,
        headers={
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
        },
        method="GET",
    )

    emit_progress(progress, "Fetching content", "Downloading the page HTML over HTTP.")
    try:
        with opener.open(req, timeout=15) as response:
            content_type = response.headers.get_content_type()
            if content_type not in {"text/html", "application/xhtml+xml"}:
                raise ValueError(f"Expected HTML but received {content_type}.")
            data = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        raise ValueError(f"Upstream HTTP error: {exc.code} {exc.reason}") from exc
    except RedirectBlocked as exc:
        raise ValueError(str(exc)) from exc
    except URLError as exc:
        raise ValueError(f"Upstream connection error: {exc.reason}") from exc

    if len(data) > MAX_RESPONSE_BYTES:
        raise ValueError("Source page exceeded the 2 MB response limit.")

    return data.decode("utf-8", errors="replace")


def fetch_html_browser(
    source_url: str,
    *,
    progress: Callable[[str, str], None] | None = None,
) -> str:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Browser mode requires Playwright. Install the optional browser dependency first."
        ) from exc

    parsed_source = urlparse(source_url)

    try:
        with sync_playwright() as playwright:
            emit_progress(progress, "Launching browser", "Starting a headless browser session.")
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(
                java_script_enabled=True,
                service_workers="block",
            )
            page = context.new_page()
            page.set_default_navigation_timeout(15000)
            page.set_default_timeout(15000)

            def handle_route(route):
                resource_type = route.request.resource_type
                parsed_request = urlparse(route.request.url)
                if resource_type in {"image", "media", "font", "websocket"}:
                    route.abort()
                    return
                if parsed_request.scheme not in {"http", "https"}:
                    route.abort()
                    return
                if parsed_request.netloc and parsed_request.netloc != parsed_source.netloc:
                    route.abort()
                    return
                route.continue_()

            page.route("**/*", handle_route)
            page.on("popup", lambda popup: popup.close())

            emit_progress(progress, "Fetching content", "Loading the rendered page in the browser.")
            response = page.goto(source_url, wait_until="domcontentloaded")
            if response is None:
                raise ValueError("Browser mode did not receive a document response.")
            if not response.ok:
                raise ValueError(f"Upstream HTTP error: {response.status} {response.status_text}")

            final_url = urlparse(page.url)
            if final_url.netloc != parsed_source.netloc:
                raise ValueError("Browser navigation left the source host and was blocked.")

            emit_progress(progress, "Rendering page", "Waiting briefly for the topic list to settle.")
            page.wait_for_timeout(1000)
            html = page.content()
            if len(html.encode("utf-8")) > MAX_RESPONSE_BYTES:
                raise ValueError("Source page exceeded the 2 MB response limit.")

            context.close()
            browser.close()
            return html
    except PlaywrightError as exc:
        raise RuntimeError(f"Browser mode failed: {exc}") from exc


def render_rss(config: FeedRequest, entries: Iterable[FeedEntry]) -> str:
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = config.feed_title
    ET.SubElement(channel, "link").text = config.source_url
    ET.SubElement(
        channel, "description"
    ).text = f"Generated from {config.source_url} using {config.fetch_mode} mode."
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(datetime.now(timezone.utc))

    for entry in entries:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = entry.title
        ET.SubElement(item, "link").text = entry.link
        ET.SubElement(item, "guid").text = entry.link
        ET.SubElement(item, "pubDate").text = format_datetime(entry.published_at)
        if entry.summary:
            ET.SubElement(item, "description").text = entry.summary

    return ET.tostring(rss, encoding="utf-8", xml_declaration=True).decode("utf-8")


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS profiles (
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
            CREATE TABLE IF NOT EXISTS feed_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                link TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                discovered_at TEXT NOT NULL,
                UNIQUE(profile_id, link),
                FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                timezone_name TEXT NOT NULL,
                public_base_url TEXT NOT NULL DEFAULT ''
            )
            """
        )
        ensure_column(conn, "app_settings", "public_base_url", "TEXT NOT NULL DEFAULT ''")
        conn.execute(
            """
            INSERT OR IGNORE INTO app_settings (id, timezone_name, public_base_url)
            VALUES (1, 'UTC', '')
            """
        )
        ensure_column(conn, "profiles", "filter_rules", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "profiles", "exclude_filter_rules", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "profiles", "refresh_anchor_at", "TEXT NOT NULL DEFAULT ''")
        conn.commit()


def connect_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_column(conn: sqlite3.Connection, table_name: str, column_name: str, definition: str) -> None:
    columns = {
        row[1]
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }
    if column_name not in columns:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def get_app_settings(db_path: Path) -> AppSettings:
    with closing(connect_db(db_path)) as conn:
        row = conn.execute(
            "SELECT timezone_name, public_base_url FROM app_settings WHERE id = 1"
        ).fetchone()
    if row is None:
        return AppSettings()
    return AppSettings(
        timezone_name=row["timezone_name"],
        public_base_url=row["public_base_url"],
    )


def update_app_settings(db_path: Path, *, timezone_name: str, public_base_url: str) -> AppSettings:
    normalized_timezone = parse_timezone_name(timezone_name)
    normalized_public_base_url = parse_public_base_url(public_base_url)
    with closing(connect_db(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO app_settings (id, timezone_name, public_base_url)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                timezone_name = excluded.timezone_name,
                public_base_url = excluded.public_base_url
            """,
            (normalized_timezone, normalized_public_base_url),
        )
        conn.commit()
    return AppSettings(
        timezone_name=normalized_timezone,
        public_base_url=normalized_public_base_url,
    )


def create_profile(db_path: Path, config: FeedRequest) -> StoredProfile:
    now = utcnow_text()
    token = secrets.token_urlsafe(18)
    with closing(connect_db(db_path)) as conn:
        cursor = conn.execute(
            """
            INSERT INTO profiles (
                feed_token, feed_title, source_url, item_selector, title_selector, link_selector,
                summary_selector, filter_rules, exclude_filter_rules, max_items, refresh_interval_minutes, fetch_mode, active,
                last_status, last_error, last_refreshed_at, refresh_anchor_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'idle', '', '', ?, ?, ?)
            """,
            (
                token,
                config.feed_title,
                config.source_url,
                config.item_selector,
                config.title_selector,
                config.link_selector,
                config.summary_selector,
                config.filter_rules,
                config.exclude_filter_rules,
                config.max_items,
                config.refresh_interval_minutes,
                config.fetch_mode,
                now,
                now,
                now,
            ),
        )
        conn.commit()
        profile_id = int(cursor.lastrowid)
    profile = get_profile_by_id(db_path, profile_id)
    if profile is None:
        raise RuntimeError("Failed to load the saved profile.")
    return profile


def update_profile(db_path: Path, profile_id: int, config: FeedRequest) -> StoredProfile:
    if get_profile_by_id(db_path, profile_id) is None:
        raise ValueError("Profile not found.")

    now = utcnow_text()
    with closing(connect_db(db_path)) as conn:
        conn.execute(
            """
            UPDATE profiles
            SET feed_title = ?, source_url = ?, item_selector = ?, title_selector = ?, link_selector = ?,
                summary_selector = ?, filter_rules = ?, exclude_filter_rules = ?, max_items = ?, refresh_interval_minutes = ?, fetch_mode = ?,
                last_status = 'idle', last_error = '', updated_at = ?
            WHERE id = ?
            """,
            (
                config.feed_title,
                config.source_url,
                config.item_selector,
                config.title_selector,
                config.link_selector,
                config.summary_selector,
                config.filter_rules,
                config.exclude_filter_rules,
                config.max_items,
                config.refresh_interval_minutes,
                config.fetch_mode,
                now,
                profile_id,
            ),
        )
        conn.commit()

    profile = get_profile_by_id(db_path, profile_id)
    if profile is None:
        raise RuntimeError("Failed to load the updated profile.")
    return profile


def delete_profile(db_path: Path, profile_id: int) -> None:
    if get_profile_by_id(db_path, profile_id) is None:
        raise ValueError("Profile not found.")

    with closing(connect_db(db_path)) as conn:
        conn.execute("DELETE FROM feed_items WHERE profile_id = ?", (profile_id,))
        conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        conn.commit()


def purge_feed_items(db_path: Path, profile_id: int) -> None:
    if get_profile_by_id(db_path, profile_id) is None:
        raise ValueError("Profile not found.")

    now = utcnow_text()
    with closing(connect_db(db_path)) as conn:
        conn.execute("DELETE FROM feed_items WHERE profile_id = ?", (profile_id,))
        conn.execute(
            """
            UPDATE profiles
            SET updated_at = ?, last_status = 'idle', last_error = ''
            WHERE id = ?
            """,
            (now, profile_id),
        )
        conn.commit()


def set_profile_active(db_path: Path, profile_id: int, *, active: bool) -> StoredProfile:
    profile = get_profile_by_id(db_path, profile_id)
    if profile is None:
        raise ValueError("Profile not found.")

    now = utcnow_text()
    with closing(connect_db(db_path)) as conn:
        if active:
            conn.execute(
                """
                UPDATE profiles
                SET active = 1, last_status = 'idle', last_error = '', refresh_anchor_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, now, profile_id),
            )
        else:
            conn.execute(
                """
                UPDATE profiles
                SET active = 0, last_status = 'disabled', last_error = '', updated_at = ?
                WHERE id = ?
                """,
                (now, profile_id),
            )
        conn.commit()

    updated = get_profile_by_id(db_path, profile_id)
    if updated is None:
        raise RuntimeError("Failed to load the updated profile.")
    return updated


def list_profiles(db_path: Path) -> list[StoredProfile]:
    with closing(connect_db(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT p.*, COUNT(fi.id) AS item_count
            FROM profiles p
            LEFT JOIN feed_items fi ON fi.profile_id = p.id
            GROUP BY p.id
            ORDER BY p.created_at DESC
            """
        ).fetchall()
    return [profile_from_row(row) for row in rows]


def get_profile_by_id(db_path: Path, profile_id: int) -> StoredProfile | None:
    with closing(connect_db(db_path)) as conn:
        row = conn.execute(
            """
            SELECT p.*, COUNT(fi.id) AS item_count
            FROM profiles p
            LEFT JOIN feed_items fi ON fi.profile_id = p.id
            WHERE p.id = ?
            GROUP BY p.id
            """,
            (profile_id,),
        ).fetchone()
    return profile_from_row(row) if row else None


def get_profile_by_token(db_path: Path, token: str) -> StoredProfile | None:
    with closing(connect_db(db_path)) as conn:
        row = conn.execute(
            """
            SELECT p.*, COUNT(fi.id) AS item_count
            FROM profiles p
            LEFT JOIN feed_items fi ON fi.profile_id = p.id
            WHERE p.feed_token = ?
            GROUP BY p.id
            """,
            (token,),
        ).fetchone()
    return profile_from_row(row) if row else None


def refresh_due_profiles(db_path: Path) -> None:
    for profile in list_profiles(db_path):
        if profile.active and should_refresh(profile):
            try:
                refresh_profile(db_path, profile.id)
            except RuntimeError:
                continue


def should_refresh(profile: StoredProfile) -> bool:
    if not profile.active:
        return False
    if profile.refresh_interval_minutes == 0:
        return False
    base_candidates = [value for value in (profile.last_refreshed_at, profile.refresh_anchor_at) if value]
    if not base_candidates and profile.created_at:
        base_candidates = [profile.created_at]
    if not base_candidates:
        return False
    due_base = max(datetime.fromisoformat(value) for value in base_candidates)
    due_at = due_base + timedelta(minutes=profile.refresh_interval_minutes)
    return datetime.now(timezone.utc) >= due_at


def refresh_profile(db_path: Path, profile_id: int) -> None:
    profile = get_profile_by_id(db_path, profile_id)
    if profile is None:
        raise ValueError("Profile not found.")
    if not profile.active:
        raise RuntimeError("Feed is disabled.")

    now = utcnow_text()
    try:
        entries = extract_feed_entries(profile.to_feed_request())
    except (ValueError, RuntimeError) as exc:
        with closing(connect_db(db_path)) as conn:
            conn.execute(
                """
                UPDATE profiles
                SET last_status = 'error', last_error = ?, last_refreshed_at = ?, refresh_anchor_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (str(exc), now, now, now, profile_id),
            )
            conn.commit()
        raise RuntimeError(str(exc)) from exc

    with closing(connect_db(db_path)) as conn:
        for entry in entries:
            conn.execute(
                """
                INSERT INTO feed_items (profile_id, title, link, summary, discovered_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(profile_id, link)
                DO UPDATE SET title = excluded.title, summary = excluded.summary
                """,
                (
                    profile_id,
                    entry.title,
                    entry.link,
                    entry.summary,
                    entry.published_at.isoformat(),
                ),
            )
        conn.execute(
            """
            UPDATE profiles
            SET last_status = 'ok', last_error = '', last_refreshed_at = ?, refresh_anchor_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (now, now, now, profile_id),
        )
        conn.commit()


def list_feed_items(db_path: Path, profile_id: int, limit: int) -> list[FeedEntry]:
    with closing(connect_db(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT title, link, summary, discovered_at
            FROM feed_items
            WHERE profile_id = ?
            ORDER BY discovered_at DESC, id ASC
            LIMIT ?
            """,
            (profile_id, limit),
        ).fetchall()
    return [
        FeedEntry(
            title=row["title"],
            link=row["link"],
            summary=row["summary"],
            published_at=datetime.fromisoformat(row["discovered_at"]),
        )
        for row in rows
    ]


def profile_from_row(row: sqlite3.Row) -> StoredProfile:
    return StoredProfile(
        id=row["id"],
        feed_token=row["feed_token"],
        feed_title=row["feed_title"],
        source_url=row["source_url"],
        item_selector=row["item_selector"],
        title_selector=row["title_selector"],
        link_selector=row["link_selector"],
        summary_selector=row["summary_selector"],
        filter_rules=row["filter_rules"],
        exclude_filter_rules=row["exclude_filter_rules"],
        max_items=row["max_items"],
        refresh_interval_minutes=row["refresh_interval_minutes"],
        fetch_mode=row["fetch_mode"],
        active=bool(row["active"]),
        last_status=row["last_status"],
        last_error=row["last_error"],
        last_refreshed_at=row["last_refreshed_at"],
        refresh_anchor_at=row["refresh_anchor_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        item_count=row["item_count"],
    )


def utcnow_text() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_feed_url(root_url: str, token: str, public_base_url: str = "") -> str:
    base_url = public_base_url.strip() or root_url
    normalized_base = base_url.rstrip("/") + "/"
    return f"{normalized_base}feeds/{token}.xml"


def load_feed_payload(db_path: Path, token: str) -> tuple[StoredProfile | None, list[FeedEntry]]:
    profile = get_profile_by_token(db_path, token)
    if profile is None:
        return None, []

    if should_refresh(profile):
        try:
            refresh_profile(db_path, profile.id)
            profile = get_profile_by_token(db_path, token)
        except RuntimeError:
            profile = get_profile_by_token(db_path, token)

    if profile is None:
        return None, []

    entries = list_feed_items(db_path, profile.id, profile.max_items)
    return profile, entries


def format_xml(xml_payload: str) -> str:
    return minidom.parseString(xml_payload.encode("utf-8")).toprettyxml(indent="  ")


def parse_timezone_name(value: str) -> str:
    timezone_name = value.strip() or "UTC"
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("Timezone must be a valid IANA name such as America/Chicago.") from exc
    return timezone_name


def parse_public_base_url(value: str) -> str:
    public_base_url = value.strip()
    if not public_base_url:
        return ""
    parsed = urlparse(public_base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Public base URL must be a valid URL such as https://rss.example.com.")
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise ValueError("Public base URL must include only scheme and host, without a path or query string.")
    return f"{parsed.scheme}://{parsed.netloc}"


def humanize_datetime(value: str | datetime, timezone_name: str = "UTC") -> str:
    if not value:
        return "Never"
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return value

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    tz = ZoneInfo(parse_timezone_name(timezone_name))
    return dt.astimezone(tz).strftime("%b %d, %Y %I:%M %p %Z")


def highlight_xml(xml_payload: str) -> str:
    highlighted_lines: list[str] = []
    for line in xml_payload.splitlines():
        stripped = line.lstrip(" ")
        indent = line[: len(line) - len(stripped)]
        highlighted_indent = indent.replace(" ", "&nbsp;")
        if not stripped:
            highlighted_lines.append("")
            continue
        highlighted_lines.append(highlighted_indent + highlight_xml_line(stripped))
    return "\n".join(highlighted_lines)


def highlight_xml_line(line: str) -> str:
    line = escape(line)
    line = re.sub(
        r'(&lt;/?)([A-Za-z0-9_:\-]+)',
        r'\1<span class="xml-tag-name">\2</span>',
        line,
    )
    line = re.sub(
        r'([A-Za-z_:][-A-Za-z0-9_:.]*)(=)(&quot;.*?&quot;)',
        r'<span class="xml-attr-name">\1</span><span class="xml-punct">\2</span><span class="xml-attr-value">\3</span>',
        line,
    )
    line = re.sub(
        r'(&gt;)([^<][^&]*?|[^<].*?)(?=&lt;/)',
        lambda match: match.group(1) + f'<span class="xml-text">{match.group(2)}</span>',
        line,
    )
    line = re.sub(
        r'(&lt;\?xml)(.*?)(\?&gt;)',
        r'<span class="xml-prolog">\1\2\3</span>',
        line,
    )
    return line


app = None
