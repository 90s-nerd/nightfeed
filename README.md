# Nightfeed

`nightfeed` saves site-specific extraction profiles, refreshes them on a schedule, and publishes stable RSS feed URLs.

## What it does

- Stores source profiles in SQLite.
- Persists discovered feed items so each source has a permanent feed URL.
- Refreshes sources on demand and on a background timer.
- Uses HTTP-only fetching by default.
- Offers an optional hardened browser mode for JavaScript-rendered pages.
- Rejects off-site topic links when building the feed.

The default mode is the safest path for noisy sites because it never opens a browser. If a site renders the topic list with JavaScript, browser mode uses Playwright in a locked-down context that blocks popups, third-party requests, downloads, and off-site navigations.

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install .
flask --app rss_site_bridge.app:create_app run --debug
```

Open `http://127.0.0.1:5000`.

Optional browser mode:

```bash
pip install ".[browser]"
playwright install chromium
```

## Docker

Build and run with Docker Compose:

```bash
docker compose up --build
```

The app will be available at `http://127.0.0.1:5000`.

Files are persisted by mounting the local `./data` directory into the container:

- host: `./data`
- container: `/app/data`

Environment variables supported by the container:

- `NIGHTFEED_DATABASE_PATH`: SQLite database path inside the container. Default: `/app/data/rss_site_bridge.db`
- `NIGHTFEED_START_SCHEDULER`: set to `1` or `0` to enable or disable the built-in background scheduler

Important deployment note:

- Nightfeed currently runs its refresh scheduler inside the web process.
- Because of that, the Docker image is configured with a single Gunicorn worker.
- Running multiple web workers would start multiple scheduler threads and can lead to duplicate refresh attempts.

## GHCR Publishing

This repo includes a GitHub Actions workflow that publishes container images to GitHub Container Registry.

Published tags:

- push to `main`: `ghcr.io/90s-nerd/nightfeed:edge`
- push a release tag like `v0.1.0`:
  - `ghcr.io/90s-nerd/nightfeed:v0.1.0`
  - `ghcr.io/90s-nerd/nightfeed:sha-<commit>`
  - `ghcr.io/90s-nerd/nightfeed:latest`

How to use it:

1. Push commits to `main` when you want an `edge` image.
2. Create and push a tag when you want a stable release image:

```bash
git tag v0.1.0
git push origin v0.1.0
```

3. In your homelab, pin the compose file to a release tag instead of `latest`:

```yaml
services:
  nightfeed:
    image: ghcr.io/90s-nerd/nightfeed:v0.1.0
    ports:
      - "5000:5000"
    environment:
      NIGHTFEED_DATABASE_PATH: /app/data/rss_site_bridge.db
      NIGHTFEED_START_SCHEDULER: "1"
    volumes:
      - /opt/nightfeed/data:/app/data
    restart: unless-stopped
```

4. Deploy or update on the homelab host with:

```bash
docker compose pull
docker compose up -d
```

GitHub setup notes:

- The workflow uses the built-in `GITHUB_TOKEN`; no extra registry password is required for publishing to GHCR from this repo.
- Make sure GitHub Actions is enabled for the repository.
- If you want anonymous pulls in the homelab, set the published package visibility to public in the GitHub package settings.

## Older Pip Fallback

If your local `pip` or setuptools environment is too old to build directly from `pyproject.toml`, use the compatibility fallback:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install "setuptools>=68" "wheel>=0.43"
pip install .
```

The repo also includes a minimal `setup.py` fallback for older local packaging tools.

## Usage

Create a source profile with:

- `Source URL`: the final HTML listing page URL you want to convert.
- `Item selector`: CSS selector that matches each topic row or card.
- `Title selector`: selector, inside each item, for the visible topic title.
- `Link selector`: selector, inside each item, that contains the topic `href`.
- `Summary selector`: optional selector, inside each item, for snippet text.
- `Include filters`: optional rules, one per line, used to keep matching titles.
- `Exclude filters`: optional rules, one per line, used to remove matching titles.
- `Refresh interval (minutes)`: how often the background scheduler should refresh the source. Use `0` for manual-only refresh.
- `Fetch mode`: `http` for raw HTML only, `browser` for the hardened Playwright fallback.

If the matched item is the link element itself, use `:scope` for `Title selector` and `Link selector`.

Filter rules are case-insensitive:
- plain text matches anywhere in the title
- `*` and `?` work as wildcards
- each line can be a boolean expression using `AND`, `OR`, and parentheses
- quoted phrases are supported, for example `"release candidate" AND stable`
- multiple lines are treated as OR rules
- exclude filters are applied after include filters

Example starting selectors for forum-like pages:

```text
Item selector: article, li, .topic-row
Title selector: a
Link selector: a
Summary selector: .excerpt, .summary
```

Example when each topic is already an anchor element:

```text
Item selector: a[href*='/forums/topic/']
Title selector: :scope
Link selector: :scope
Summary selector:
```

Example when one matched container holds many topic links:

```text
Item selector: .banger-container p
Title selector: a[href*='/forums/topic/']
Link selector: a[href*='/forums/topic/']
Summary selector:
```

After you save a source, the app shows a permanent feed URL in the form:

```text
/feeds/<token>.xml
```

That URL can be added directly to an RSS reader.

## Refresh Behavior

Nightfeed has a built-in background scheduler. When the web app is running, it checks for due feeds every 30 seconds. There is no separate cron job required for the current setup.

Automatic refresh only runs when all of these are true:

- the feed is enabled
- `Refresh interval (minutes)` is greater than `0`
- the current time is past the next due time for that feed

The next due time is based on the most recent of these timestamps:

- feed creation time for a newly created feed
- enable time when a disabled feed is enabled again
- the most recent manual refresh time
- the most recent automatic refresh time

Current lifecycle rules:

- New feeds are saved as `idle` and are not fetched immediately.
- A new feed will first auto-refresh after its configured interval from creation time.
- If the user manually refreshes a feed, the next automatic refresh is scheduled from that manual refresh time.
- Disabling a feed stops automatic refresh and hides refresh actions in the dashboard.
- Enabling a feed moves it back to `idle`, and the next automatic refresh is scheduled from the time it was enabled.
- Setting `Refresh interval (minutes)` to `0` disables automatic refresh completely and makes the feed manual-only.
- Disabled feeds are not served from the XML endpoints.

Feed requests also perform a due-check on access, so if a feed URL is opened after it becomes due, Nightfeed may refresh it on demand before returning XML.

## Limits

- HTTP mode blocks redirects, so use the final canonical listing URL.
- The upstream response is capped at 2 MB.
- Only `http` and `https` URLs are accepted.
- Feed items must stay on the same host as the source page.
- Browser mode is optional and requires Playwright plus a local Chromium install.
- Browser mode also requires a local environment where Playwright can launch Chromium.
