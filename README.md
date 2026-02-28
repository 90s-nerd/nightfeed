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

## Limits

- HTTP mode blocks redirects, so use the final canonical listing URL.
- The upstream response is capped at 2 MB.
- Only `http` and `https` URLs are accepted.
- Feed items must stay on the same host as the source page.
- Browser mode is optional and requires Playwright plus a local Chromium install.
- Browser mode also requires a local environment where Playwright can launch Chromium.
