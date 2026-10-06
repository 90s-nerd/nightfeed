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
python tests/ui/check_push_notifications.py
python tests/ui/check_auth.py
python tests/ui/check_assistant.py
```

The checks cover responsive layouts and themes, feed creation/editing and
preview, search timing, browser-local timestamps, download submission states,
hint alignment, and notification menus. Screenshots and reports are written to
the ignored `.test-preview/` directory. The regular backend suite remains
`python -m unittest discover -s tests -q`.

Feature fixtures complete real owner onboarding and supply an authenticated session
to each browser context. Authentication is never disabled. `check_auth.py` exercises
the unauthenticated boundary, onboarding, login/logout, security settings, session
CSRF and scoped API-key creation in desktop and mobile layouts. It also checks
profile edits, read-only SSO-managed names, account menus, card spacing and compact
form widths, including the profile editor in the dark theme.

`check_assistant.py` verifies provider activation, real preview cards, approved
creation, persistent conversation history, internal search, context/cost display,
audit access, MCP settings and the composer on mobile and desktop. Synthetic audio
exercises speech-pause detection, transcription, resumed listening and stopping
continuous voice without requiring physical microphone access or a live AI key.
