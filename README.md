# Nightfeed

`nightfeed` saves site-specific extraction profiles, refreshes them on a schedule, and publishes stable RSS feed URLs.

## What it does

- Stores source profiles in SQLite.
- Persists discovered feed items so each source has a permanent feed URL.
- Labels unseen topics NEW and puts them first in the default timeline. A title visible for one continuous second is recorded as seen; opening a topic also records it. Badges and pagination stay stable during a browsing session. Existing topics are marked seen on the initial upgrade. Seen state is shared across devices and users of the same Nightfeed instance.
- Filter the timeline to new topics or saved bookmarks, and mark all topics seen to catch up. Saving a topic keeps it available after it is seen. Previously seen topics show UPDATED when their title or summary changes, with a before/after summary accumulated until the update is seen. Bookmarks are shared across the instance.
- Refreshes sources on demand and on a background timer.
- Opens refresh notifications as saved reports showing new entries, before/after updates, and failure diagnostics. Opening a report marks it as read; changed entries include Open safely links. Reports created before this feature retain their existing summary; entry-level history is recorded for subsequent refreshes.
- Sends optional Web Push summaries to individual devices, with feed selection, quiet hours, and daily limits. Includes a short Home Screen installation guide in Settings.
- Uses HTTP-only fetching by default.
- Offers an optional hardened browser mode for JavaScript-rendered pages.
- Opens stored topics in an optional interactive, isolated browser with popup and ad-request controls.
- Captures user-triggered browser downloads and serves them from a temporary Nightfeed download tray.
- Rejects off-site topic links when building the feed.

The default mode is the safest path for noisy sites because it never opens a browser. If a site renders the topic list with JavaScript, browser mode uses Playwright in a locked-down context that blocks popups, third-party requests, downloads, and off-site navigations.

## Run

### AI assistant and MCP

Open **Settings → AI assistant and MCP → Manage AI and MCP** to add a named AI
connection. Choose **OpenAI Responses API** with `https://api.openai.com/v1`, or
**OpenAI-compatible chat API** for another service. Examples include
`http://ollama:11434/v1` and `http://openwebui:8080/api`; use hostnames reachable
from the Nightfeed server. In Docker, `localhost` refers to the Nightfeed container.
Native **Anthropic Messages** (`https://api.anthropic.com/v1`) and **Google Gemini**
(`https://generativelanguage.googleapis.com/v1beta`) adapters are also available.
Enter the provider's model identifier and any required API key, then choose
**Test and activate**. The test makes one request to verify tool calling. Chat
is available only for a tested active connection; editing a connection requires
a fresh test. Multiple named connections can be saved and switched by testing
and activating the desired connection. Local endpoints may omit authentication.

**Ask Nightfeed** opens a persistent chat panel. Supply a listing URL and explain
which titles you want to follow. The assistant inspects the page, tests selectors
with Nightfeed's real extractor, and displays up to three actual items. It can
prepare feed creation/edits, pause/resume, immediate refreshes, and global schedule
timezone, public feed URL and non-secret SMTP settings changes. Saved passwords
are preserved; configure credentials in Settings. Review the complete proposal and choose **Approve**, or
explicitly say **create it** / **apply changes**. Proposals expire after one hour,
reject intervening changes, and cannot create duplicate feeds when retried. Saved
feeds use ordinary extraction and scheduling without AI calls. The manual editor
remains available.
Chat can also propose a color theme change on this device and edit registered
browser-push preferences (notification types, feed selection, digest interval,
daily limit and quiet hours). Register/enable push in Settings first. These device
tools are only exposed to built-in chat, and approval is bound to that device.

The assistant reads live app counts, including unread notifications, separately from
unread timeline topics. Ask it to list notifications, read notification details, mark
one or all read, or delete a notification/read notifications. It can also save or
unsave a topic and mark one or all unread topics seen. An explicit chat request to
refresh an existing feed runs immediately; it does not require a second approval.
Other changes use review proposals; confirm a single pending proposal with “yes” or
“go ahead” in chat, or click Approve. Bulk proposals cover only the items
captured before approval, excluding later arrivals.

Search covers **saved Nightfeed feeds and stored topic content only**. The assistant
can explain settings and open an explicitly requested saved item in the existing
isolated browser. There is no general web search or arbitrary-URL safe browsing.
Source inspection for feed setup is limited to public HTTP/HTTPS destinations on
ports 80/443 and validates DNS and redirects. Browser inspection requires the
browser extra and Chromium and blocks WebSockets, downloads and private network
requests; sites needing login, POST requests, or unsupported interactions may
need manual setup.

**Image attachments:** choose **+ → Add images**, paste a clipboard image into
the message field, or drop images onto the composer. Preview and remove attachments
before sending; a message may contain images without text. PNG, JPEG and WebP are
supported, up to four images at 2 MB each. Your selected model must support vision.
Images are sent to the configured provider and retained with their conversation
until it is deleted; audit events retain attachment metadata, not image data.

For **voice input**, configure a separate compatible audio-transcription API base
URL, model and optional key under the connection's Voice input options. The microphone
icon records dictation (up to 60 seconds). The waveform icon starts continuous voice
conversation: speech is submitted after a pause, replies are read aloud, then listening
resumes. Tap the waveform again or close chat to stop. Microphone access requires
HTTPS or localhost. Spoken replies use your device's speech synthesis voices.
This uses transcription, chat and speech synthesis in sequence, so latency depends
on your configured services. The shortcut menu also includes a read-replies option.

While a reply is running, the send arrow becomes a Stop button. Stop releases the
conversation so another message can be sent, including after a page reload. It
prevents further assistant steps; actions already committed remain completed.

The composer context gauge displays usage from the latest provider request.
Set your model's context window in AI settings to show its percentage. Configure
optional USD prices per million input, output, cached and cache-write tokens for
cost estimates. Estimates exclude audio, search, and other non-token fees and are
not provider invoices. Missing usage or pricing is shown as unavailable.

**Settings → AI and MCP → Audit history** lists messages, provider calls, connection
tests, transcription, tool calls and approvals, including failed operations. Expand
an event for usage, timings and action details; filter by type and export a page as
JSON. Provider usage includes cached/reasoning tokens when returned. Request sizes
are captured when token usage is unavailable. Credentials and raw source HTML are
redacted; audio is not retained. Audit records survive conversation deletion and
are included in database backups. Only the signed-in owner can view/export audits;
MCP keys cannot read them.

Chat messages, attached images and relevant source HTML are sent to the selected AI provider.
Recordings are sent only to the configured transcription endpoint. Conversations
are stored in the installation's database; delete a conversation in the panel to
remove its messages and proposals. Provider keys are encrypted with the existing
installation key (`*.downloaders.key` or `NIGHTFEED_DOWNLOADER_KEY`); back up that
key with the database. Provider errors never expose response bodies or credentials.
Configure model/token limits and provider-side spending controls as needed.

**MCP works independently of AI configuration.** Enable it in the same settings
page and create a dedicated API key under **Settings → API keys**. Use:

- Transport: **Streamable HTTP** (stateless, JSON responses)
- Endpoint: `https://YOUR_NIGHTFEED_HOST/mcp`
- Header: `Authorization: Bearer YOUR_API_KEY`
- Permissions: **MCP read**, optionally **MCP write**, **Refresh feeds**, and/or
  **MCP settings** for non-secret global app settings.

Clients must support custom Bearer headers; automatic OAuth discovery and the
legacy HTTP+SSE transport are not provided. Send `Accept: application/json,
text/event-stream`, and the negotiated `MCP-Protocol-Version` on subsequent
requests. The server supports initialization, ping, tool discovery and calls.
MCP exposes the same validated feed, preview, internal-search and help services,
but **does not expose safe-browser opening**. Write workflows return a draft;
the external agent must obtain user approval before calling `apply_draft`.

Feed-restricted keys can read/search/edit their permitted feeds and preview their
existing source URLs. Creating feeds, inspecting arbitrary source pages and
changing source URLs require unrestricted feed access. Global settings cannot be
combined with feed restrictions. Scope checks also run when drafts are applied.
Disable MCP or revoke its API key to remove external access. Use HTTPS outside a
trusted local network.

Streaming chat keeps an HTTP connection open while provider/tool work runs. Reverse
proxies should allow a several-minute request timeout and disable response buffering
for `/api/assistant/`. Interrupted connections do not discard completed replies;
reopen the panel to reload the persisted conversation. A server restart may leave
a conversation busy until its 15-minute work lease expires.

### Authentication and upgrading

Nightfeed requires authentication for all private pages, RSS XML, downloads, previews,
browser sessions and APIs. Only the sign-in/setup pages and generic static/PWA assets
are public. Upgrading keeps your feeds and settings, but previously unauthenticated
RSS readers must be configured with an API key. Back up your database and the
installation's `*.downloaders.key` file before upgrading.

On first start, create the single owner account at `/auth/setup`. Setup requires the
server-only token in `data/rss_site_bridge.setup-token` (next to the configured database).
For Docker, read it with `docker compose exec nightfeed cat /app/data/rss_site_bridge.setup-token`.
Alternatively supply a random `NIGHTFEED_SETUP_TOKEN` of at least 32 characters through
your deployment's secret management. The generated file is removed after setup. Never
expose the data directory through a web server. Passwords are salted with scrypt;
use a unique passphrase of 15–256 characters. There is no default password.

Serve production instances over HTTPS. Cookies default to Secure, HttpOnly and SameSite
Lax. For **local HTTP development only**, set `NIGHTFEED_SECURE_COOKIES=0`; otherwise
an HTTP browser cannot retain the setup/login cookie. Session defaults are 12 hours
maximum and 30 minutes of inactivity. Change these under **Settings → Security**.
Password changes revoke other sessions. Security changes also sign out other sessions.

Open **Settings → Manage profile** (also available in the account menu) to change
your display name. After linking your SSO identity, **Use name from SSO provider**
makes the name read-only and refreshes it from the verified ID token's `name` claim
on SSO sign-in. Your local name is retained for switching back. If the provider
does not supply a valid name, Nightfeed keeps the last provider name, or shows your
local name until the provider supplies one.
Logout revokes the server session, clears browser cache and stops the session from
being reused. It preserves push registration, notification preferences and the device
management token, so notifications continue after logout or session expiry without
re-enabling them. Use **Settings → Mobile notifications → Turn off** to stop delivery
on a device. Private responses use `no-store`. Push notices include feed names and
new/updated topic counts (or failure counts), including while signed out. Opening
the notification list or report requires a valid session; otherwise Nightfeed shows
the sign-in page, even when automatic SSO is enabled. After sign-in, the requested
report opens.

**Settings → Security** also controls password lockout (default: five failures,
15-minute window/lockout, automatic unlock), comma-separated IP/CIDR exclusions and
manual unlock. Both IP and account limits apply; excluded addresses bypass both.
There are no exclusions by default. Exempt only networks you control.

If all sessions are locked out, stop Nightfeed and run an offline recovery command
against its persisted database, then restart it:

```bash
python -m rss_site_bridge.auth unlock --database data/rss_site_bridge.db
python -m rss_site_bridge.auth reset-password --database data/rss_site_bridge.db
```

Password reset prompts securely; neither command takes a password on the command line.
Both revoke every browser session. API keys are independent credentials: revoke them
separately if a key may have been exposed.

### OpenID Connect / Authelia

Under **Settings → Security**, enter the exact HTTPS issuer, client ID, client secret,
and callback URL (`https://your-nightfeed-host/auth/oidc/callback`). Register a
confidential authorization-code client at your provider with that exact redirect URI,
`openid profile` scopes and PKCE S256. The default token authentication method is
`client_secret_basic`. See [Authelia client configuration](https://www.authelia.com/configuration/identity-providers/openid-connect/clients/).

Discovery uses `<issuer>/.well-known/openid-configuration`. Alternatively turn off
discovery and supply HTTPS authorization, token and JWKS endpoints. TLS certificate
verification is required. Save the settings, then **Link owner SSO identity** after
confirming your Nightfeed password and signing into your own provider account. You
may also enter the exact owner `sub` manually. Only that issuer/subject pair can log
in; usernames and email addresses do not grant access. Authelia subjects are stable
identifiers whose behavior depends on its client configuration; see its
[claims documentation](https://www.authelia.com/integration/openid-connect/openid-connect-1.0-claims/).

Once the identity is linked, you can enable automatic SSO redirects, hide the local
login form, customize button text, and configure an optional HTTPS provider logout
URL (including any required provider parameters). Logout always revokes Nightfeed's
session first; provider sign-out depends on that provider's logout URL. Protect SSO
with the provider's MFA policy. The authorization flow validates state, PKCE, nonce,
signature, issuer, audience and expiry; provider tokens are not persisted.

Keep the local password for security changes and recovery. Set
`NIGHTFEED_FORCE_LOGIN_FORM=1` and restart the container to restore local login even
when the form is hidden and automatic SSO is enabled. This restores the form without
bypassing password verification or lockout. `/auth/login?sso=off` pauses auto-redirect
for that visit but does not override a disabled form.

### Scoped API keys and RSS readers

Create keys under **Settings → API keys**. Select only the required permissions and,
when practical, restrict the key to particular feeds. Empty feed selection means all
current and future feeds. Each key can have a UTC expiry or no expiry; revoke it at
any time. Creation requires your current password. Keys contain 256 bits of random
entropy, are shown once, and are stored only as SHA-256 hashes.

| Permission | Accessible endpoints |
| --- | --- |
| `rss:read` | `GET /feeds/<feed-token>.xml` |
| `feeds:read` | `GET /api/v1/feeds` |
| `topics:read` | `GET /api/v1/topics` (up to 100 recent permitted topics) |
| `notifications:read` | `GET /api/v1/notifications` (up to 100 recent permitted feed notifications) |
| `feeds:refresh` | `POST /api/v1/feeds/<feed-id>/refresh` |

Keys cannot access browser pages, account settings, passwords, SSO configuration,
downloader credentials or key administration. Each permission has an explicit endpoint
allowlist; future routes are denied automatically. Feed restrictions apply to every
permission. General notifications without a feed are omitted from the API.

Use `Authorization: Bearer YOUR_KEY` or `X-API-Key: YOUR_KEY` over HTTPS. RSS readers
that support HTTP Basic can use username `apikey` and the key as the password.
Basic authentication is for reads; use Bearer or X-API-Key for refresh requests.
Keep the existing RSS URL and configure credentials separately. Query-string keys
are deliberately unsupported because URLs can leak through logs/history/referrers.
The included Gunicorn configuration omits query strings and headers from access logs;
configure your reverse proxy to redact credentials and OIDC callback queries too.

### Trusted proxies

Forwarded headers are ignored by default. Configure proxy IPs/CIDRs and the number of
forwarded hops under **Settings → Security**, or explicitly override them with
`NIGHTFEED_TRUSTED_PROXIES` and `NIGHTFEED_TRUSTED_PROXY_HOPS`. Only connections from
a listed direct proxy may supply `X-Forwarded-For` and `X-Forwarded-Proto`. Your proxy
must preserve the original Host header and overwrite incoming forwarded headers.
Forwarded host, port and prefix headers are never trusted. A typical single-proxy
deployment uses one hop and the proxy's exact address. Use `Public base URL` for a
fixed external feed hostname. Do not expose the backend port publicly when deploying
behind a reverse proxy.

The schema keeps users, OIDC identities, sessions and API keys separate, with keys
and sessions tied to a user ID. Only one owner can be created. Supporting additional
users will require resource ownership and authorization migrations before enabling
account creation; the current owner model does not imply shared multi-user access.

### Mobile notifications

Serve Nightfeed over HTTPS with a certificate trusted by your phone. Open **Settings → Mobile notifications** on each device. On iPhone/iPad (iOS/iPadOS 16.4 or later), first use **Add to Home Screen** for the short Safari guide, then open the installed app to enable notifications. Android and desktop browsers can enable push directly; the guide also explains installing Nightfeed as an app.

Permission is requested only when you press **Enable on this device**. The default sends new-topic summaries at most every 15 minutes, with a maximum of 12 automatic notifications per device per local day. Choose new topics, updated topics, refresh failures, selected feeds, a 5/15/60-minute interval, a daily limit of 1–24, and optional quiet hours. Preferences and quiet hours use the timezone of the device when saved. Unchanged refreshes never alert; a failing feed alerts once until it recovers. Changes are grouped across feeds. During quiet hours or after the daily limit, pending events are held for up to 24 hours; older events expire rather than creating a backlog of alerts. **Send test** is an explicit exception to these preferences and is limited to once a minute. **Turn off** stops delivery for this device.

The server needs outbound HTTPS access to the browser's push provider (Apple, Google, or Mozilla); your phone needs internet to receive the notice. Notices show the feed name (or number of feeds) and new-topic, updated-topic and failure counts. These summaries can appear while signed out or on the lock screen. Opening their details requires authentication and access to your Nightfeed address. The service worker does not cache private pages or fetch the server to display notifications.

Optionally set `NIGHTFEED_PUSH_CONTACT=mailto:you@example.com` to provide an administrator contact for VAPID authentication. Otherwise Nightfeed uses its HTTPS hostname. Persist and back up the database **and** its existing `rss_site_bridge.downloaders.key` file together: the encrypted push signing key uses the same installation encryption key as downloader credentials. Losing that key requires restoring it and re-enabling notifications. Expired subscriptions are disabled; Settings lets the affected device enable them again. There is no external push account to configure, and no incoming public port is required for delivery.

### Starting the app

macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install .
export NIGHTFEED_SECURE_COOKIES=0 # local HTTP development only
flask --app rss_site_bridge.app:create_app run --debug
```

Windows PowerShell:

Install Python 3.10 or newer first. During installation, enable **Add python.exe to PATH**, then restart PowerShell and verify it with `python --version`.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
$env:NIGHTFEED_SECURE_COOKIES = "0" # local HTTP development only
.\.venv\Scripts\python.exe -m flask --app rss_site_bridge.app:create_app run --debug
```

These commands use the virtual environment's Python directly, so they do not require activating the environment. If `python` is not found but the Windows Python launcher is installed, use `py -m venv .venv` instead. You can also replace `python` with the full path to your installed `python.exe`.

If `.venv` already exists, start from the install command:

```powershell
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\python.exe -m flask --app rss_site_bridge.app:create_app run --debug
```

Open `http://127.0.0.1:5000`.

Optional browser mode:

macOS/Linux:

```bash
pip install ".[browser]"
playwright install chromium
```

The same optional Playwright installation powers **Open safely** on stored topics. Safe browser sessions run for up to ten minutes after the user stops interacting. New windows are suppressed, common advertising hosts and non-public network targets are blocked, and files initiated by the user appear in the session's Downloads tray. Session cookies and downloaded temporary files are removed when the session ends.

### Safe-browser streaming roadmap

The current safe browser sends input to Playwright and refreshes a remote screenshot. Wheel events are queued and combined to reduce choppy scrolling, but the experience is still limited by HTTP and screenshot round trips. A future upgrade can replace screenshot polling with a persistent WebSocket carrying Chrome DevTools Protocol screencast frames, or use WebRTC/noVNC for a continuously streamed viewport. Input events should travel over the same persistent channel for browser-like scrolling, typing, and animation.

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pip install ".[browser]"
.\.venv\Scripts\python.exe -m playwright install chromium
```

## Docker

Build and run with Docker Compose:

```bash
docker compose up --build
```

The app listens on port 5000. Use an HTTPS reverse proxy for production. For local
HTTP testing, set `NIGHTFEED_SECURE_COOKIES=0` in your shell or Compose `.env` file
before starting it, then open `http://127.0.0.1:5000`.

Files are persisted by mounting the local `./data` directory into the container:

- host: `./data`
- container: `/app/data`

If the container was previously started with a different image and failed to open the SQLite database, rebuild and recreate it:

```bash
docker compose down
docker compose up --build -d
```

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

Reverse proxy note:

- Nightfeed accepts forwarded client IP and HTTPS headers only from explicitly configured trusted proxies. Configure them under Settings → Security; preserve the Host header at your proxy.
- If you want feed URLs to always use a fixed public host, set `Public base URL` in the Nightfeed settings page.

## Older Pip Fallback

If your local `pip` or setuptools environment is too old to build directly from `pyproject.toml`, use the compatibility fallback:

macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install "setuptools>=68" "wheel>=0.43"
pip install .
```

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install "setuptools>=68" "wheel>=0.43"
.\.venv\Scripts\python.exe -m pip install .
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

- Top-level page redirects are followed, and successful refreshes save the final canonical listing URL.
- The upstream response is capped at 2 MB.
- Only `http` and `https` URLs are accepted.
- Feed items must stay on the same host as the source page.
- Browser mode is optional and requires Playwright plus a local Chromium install.
- Browser mode also requires a local environment where Playwright can launch Chromium.

## Timeline and browsing

The home page combines all stored feed items. Search titles, summaries and links, select feeds, and sort by newest, oldest, title or feed priority. Higher priority values sort first. Both the timeline and feed detail pages show 25 items per page with Previous/Next links. Max items still controls extraction per refresh and RSS output, not access to historical stored items. Dates reflect when Nightfeed discovered an item.

## Calendar schedules and selector traversal

An optional five-field cron expression overrides the refresh interval. Set the shared refresh schedule timezone in **Settings**, for example `America/Chicago`. Every feed uses that timezone; changing it updates existing calendar schedules. `0 */2 * * *` runs on every second hour; `0 9 * * mon-fri` runs at 9 AM on weekdays. Manual refreshes leave calendar times aligned. Clear the cron field to return to interval scheduling, or also set the interval to zero for manual-only refresh. Disabled feeds do not refresh. The scheduler checks every 30 seconds, so runs may start shortly after the scheduled minute. Displayed timestamps use your browser’s locale and current timezone, including when you travel. Settings timezone controls calendar scheduling, not date display. Email timestamps use UTC because email has no browser timezone. Cron calculation uses [croniter](https://github.com/pallets-eco/croniter).

Title, link and summary selectors support CSS nth-child/nth-of-type selectors and traversal steps separated by `>>`. For example, `:scope >> parent` selects the item's parent, `.title >> parent >> a:nth-of-type(2)` selects the second anchor inside the title's parent, and repeated `parent` steps climb multiple levels. Preview these selectors before saving.

Manual refresh on feed detail pages stays on the page, shows a spinner, updates stored content on success, and displays failures inline.

## Downloader integrations

Use **Settings → Manage downloaders** to configure optional destinations for files downloaded through Open Safely. Each profile has a name, downloader type, server URL, credentials, file-extension routing, category preferences, and an optional custom button label.

The server URL must be reachable from Nightfeed. In Docker, `localhost` refers to the Nightfeed container; use a service name such as `http://downloader:8080`, a LAN address, or a configured host gateway. Files are uploaded directly, so shared download directories are unnecessary.

Supported authentication depends on the selected downloader type: username/password, API key, or explicitly configured trusted-network access. Use **Test / refresh categories** after saving to verify the connection and load available categories. Keep HTTPS certificate verification enabled, or configure a trusted CA certificate path inside the Nightfeed server/container. Redirects are not followed; configure the final server URL.

A blank button label defaults to `Send to {profile name}` and follows profile renames. You can override it. Destination dropdowns show profile names. Send actions appear only for extensions configured on an enabled profile and supported by its adapter.

Categories and directories come from your downloader. An optional allowlist limits available categories; a default can be preselected. Category selection is required by default, with an optional Uncategorized choice. Existing downloads keep their category and state.

In **Open Safely → Downloads**, choose a send action, review the destination/category/start settings, and choose **Send file**. Save file remains available. Sending runs asynchronously with a limit of four concurrent submissions. Status is retained across polling and reloads of the same browser session and appears in submission history.

Files are validated against the selected adapter, limited to 10 MB, and copied into the job before browser-session cleanup. Job records retain metadata rather than file contents. Nightfeed checks remote identity before adding a file and does not automatically repeat an uncertain submission. Use **Check status** before explicitly retrying. Interrupted submissions are marked uncertain after restart. Retain the single-worker deployment; multiple workers are unsupported by the in-process scheduler and submission pool.

### Credentials and access

Saved credentials are encrypted in SQLite using a Fernet key stored next to the database as `rss_site_bridge.downloaders.key`. Persist the whole `/app/data` directory and back up the database and key together. Alternatively provide a stable `NIGHTFEED_DOWNLOADER_KEY`; generate one using `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Losing or changing the key requires restoring it or re-entering saved credentials.

To manage credentials externally, configure an environment variable such as `DOWNLOADER_PASSWORD` on the Nightfeed server and enter its name in the profile. This takes precedence over the saved secret. A blank credential input preserves the saved secret; the clear checkbox removes it.

Nightfeed has no user-account system: users with access can configure destinations and send files. Protect exposed deployments with an authenticated reverse proxy or a trusted private network. Downloader mutation endpoints require a signed page token and reject cross-origin requests. Reload pages older than 24 hours before making changes.
