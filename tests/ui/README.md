# Browser checks

These optional checks run a real Flask app with a temporary SQLite database and
fixture extraction/downloaders. They do not use your configured feeds, database,
SMTP server, or downloader connections.

Install the browser extra (`pip install ".[browser]"`) and Google Chrome, then
run the scripts from the repository root:

```bash
python tests/ui/check_refresh.py
python tests/ui/check_feed_editor.py
python tests/ui/check_feedback.py
python tests/ui/check_hints.py
python tests/ui/check_topic_dates.py
python tests/ui/check_notification_menu.py
python tests/ui/check_async_timeline.py
python tests/ui/check_notification_reports.py
python tests/ui/check_topic_seen.py
python tests/ui/check_topic_features.py
python tests/ui/check_notices.py
```

The checks cover responsive layouts and themes, feed creation/editing and
preview, search timing, browser-local timestamps, download submission states,
hint alignment, and notification menus. Screenshots and reports are written to
the ignored `.test-preview/` directory. The regular backend suite remains
`python -m unittest discover -s tests -q`.
