"""Configured downloader adapters, encrypted secrets and bounded submission jobs."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
from threading import BoundedSemaphore, Thread
from urllib.parse import urlsplit
import json
import os
import re
import secrets
import sqlite3
import time

from cryptography.fernet import Fernet
from flask import Blueprint, current_app, jsonify, render_template, request, redirect, url_for
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from .downloader_adapters import ADAPTERS, ADAPTER_LABELS, MAX_SUBMISSION_BYTES, SubmissionRejected
_slots = BoundedSemaphore(4)


def connect(db):
    conn = sqlite3.connect(str(db), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def initialize(db):
    with closing(connect(db)) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS downloaders (
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
          enabled INTEGER NOT NULL, button_label TEXT NOT NULL DEFAULT '',
          base_url TEXT NOT NULL, auth_mode TEXT NOT NULL, username TEXT NOT NULL,
          secret TEXT NOT NULL, secret_env TEXT NOT NULL, verify_tls INTEGER NOT NULL,
          ca_path TEXT NOT NULL, timeout INTEGER NOT NULL, extensions TEXT NOT NULL,
          default_category TEXT NOT NULL, allowed_categories TEXT NOT NULL,
          require_category INTEGER NOT NULL, start_immediately INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS downloader_jobs (
          id TEXT PRIMARY KEY, downloader_id INTEGER REFERENCES downloaders(id) ON DELETE SET NULL,
          destination TEXT NOT NULL, file_name TEXT NOT NULL, category TEXT NOT NULL,
          fingerprint TEXT NOT NULL, hashes TEXT NOT NULL, status TEXT NOT NULL,
          message TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
          updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
        CREATE INDEX IF NOT EXISTS downloader_job_identity ON downloader_jobs(downloader_id, fingerprint, status);
        """)
        # Interrupted submissions may have reached the remote client. Never retry blindly.
        conn.execute("UPDATE downloader_jobs SET status='unknown', message='Nightfeed restarted during submission. Check the downloader before retrying.' WHERE status='sending'")
        conn.commit()


def encryption_key(db):
    configured = os.environ.get("NIGHTFEED_DOWNLOADER_KEY", "")
    if configured:
        Fernet(configured.encode())
        return configured.encode()
    path = Path(db).with_suffix('.downloaders.key')
    try:
        descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return path.read_bytes().strip()
    key = Fernet.generate_key()
    with os.fdopen(descriptor, 'wb') as handle:
        handle.write(key)
    return key


def profiles(db, enabled_only=False):
    with closing(connect(db)) as conn:
        rows = conn.execute('SELECT * FROM downloaders' + (' WHERE enabled=1' if enabled_only else '') + ' ORDER BY name COLLATE NOCASE, id').fetchall()
    return [dict(row) for row in rows]


def get_profile(db, identity):
    with closing(connect(db)) as conn:
        row = conn.execute('SELECT * FROM downloaders WHERE id=?', (identity,)).fetchone()
    if row is None:
        raise ValueError('Downloader profile not found.')
    return dict(row)


def public_profile(profile):
    return {key: value for key, value in profile.items() if key not in {'secret', 'secret_env', 'username', 'base_url', 'ca_path'}} | {
        'label': profile['button_label'] or 'Send to ' + profile['name'],
        'extensions': json.loads(profile['extensions']),
    }


def eligible_profiles(db, filename):
    extension = Path(filename).suffix.lower()
    return [public_profile(p) for p in profiles(db, True) if extension in json.loads(p['extensions']) and extension in ADAPTERS[p['kind']].extensions]


def parse_profile(values, existing, key):
    def text(name, limit=500):
        value = str(values.get(name, '')).strip()
        if len(value) > limit or any(ord(c) < 32 for c in value):
            raise ValueError(f'Invalid {name.replace("_", " ")}.')
        return value

    name, kind = text('name', 100), text('kind') or next(iter(ADAPTERS))
    if not name or kind not in ADAPTERS:
        raise ValueError('Enter a name and select a supported downloader type.')
    base = text('base_url').rstrip('/')
    parsed = urlsplit(base)
    try:
        parsed.port
    except ValueError:
        raise ValueError('Invalid downloader port.')
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Use an http/https base URL without credentials, query or fragment.')
    mode = text('auth_mode') or 'password'
    if mode not in {'password', 'api_key', 'none'}:
        raise ValueError('Unsupported authentication mode.')
    env = text('secret_env', 100)
    if env and not re.fullmatch(r'[A-Z_][A-Z0-9_]*', env):
        raise ValueError('Secret environment variable must use uppercase letters, digits and underscores.')
    secret = str(values.get('secret', ''))
    if len(secret) > 4096:
        raise ValueError('Secret is too long.')
    encrypted = Fernet(key).encrypt(secret.encode()).decode() if secret else (existing or {}).get('secret', '')
    if values.get('clear_secret'):
        encrypted = ''
    if mode != 'none' and not env and not encrypted:
        raise ValueError('Provide a password/API key or a secret environment variable.')
    if mode == 'password' and not text('username'):
        raise ValueError('Username is required for password authentication.')
    try:
        timeout = int(values.get('timeout', '15'))
    except ValueError:
        raise ValueError('Timeout must be from 3 to 60 seconds.')
    if not 3 <= timeout <= 60:
        raise ValueError('Timeout must be from 3 to 60 seconds.')
    extensions = sorted(set('.' + part.lstrip('.').lower() for part in re.split(r'[\s,]+', text('extensions')) if part))
    if not extensions or any(ext not in ADAPTERS[kind].extensions for ext in extensions):
        raise ValueError('Enter file extensions supported by the selected downloader type.')
    allowed = [line.strip() for line in str(values.get('allowed_categories', '')).splitlines() if line.strip()]
    if len(allowed) > 200 or any(len(line) > 200 for line in allowed):
        raise ValueError('Category allowlist is too long.')
    default = text('default_category', 200)
    if default and allowed and default not in allowed:
        raise ValueError('Default category must be in the allowed categories.')
    return dict(name=name, kind=kind, enabled=int(bool(values.get('enabled'))), button_label=text('button_label', 100),
                base_url=base, auth_mode=mode, username=text('username'), secret=encrypted, secret_env=env,
                verify_tls=int(bool(values.get('verify_tls'))), ca_path=text('ca_path'), timeout=timeout,
                extensions=json.dumps(extensions), default_category=default, allowed_categories=json.dumps(allowed),
                require_category=int(bool(values.get('require_category'))), start_immediately=int(bool(values.get('start_immediately'))))



def job(db, identity):
    with closing(connect(db)) as conn:
        row = conn.execute('SELECT * FROM downloader_jobs WHERE id=?', (identity,)).fetchone()
    if row is None:
        raise ValueError('Submission not found.')
    return dict(row)


def job_public(value):
    return {key: value[key] for key in ('id', 'destination', 'file_name', 'category', 'status', 'message', 'created_at')}


def finish(db, identity, status, message):
    with closing(connect(db)) as conn:
        conn.execute("UPDATE downloader_jobs SET status=?, message=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (status, message, identity))
        conn.commit()


def submit_worker(db, key, profile, identity, data, hashes, category, start):
    attempted = False
    try:
        adapter = ADAPTERS[profile['kind']](profile, key)
        categories = {c['name'] for c in adapter.categories()}
        if category and category not in categories:
            raise ValueError('Selected category is no longer available. Choose another category.')
        if adapter.contains(hashes):
            finish(db, identity, 'already_present', 'Already present in ' + profile['name'] + '. Its category and files were left unchanged.')
            return
        attempted = True
        adapter.add(data, category, start)
        for _ in range(6):
            if adapter.contains(hashes):
                finish(db, identity, 'added', 'Added to ' + profile['name'] + (': ' + category if category else ' (uncategorized)') + '.')
                return
            time.sleep(0.5)
        finish(db, identity, 'unknown', 'Submission was sent, but acceptance could not be confirmed. Check status before retrying.')
    except Exception as exc:
        message = str(exc) if isinstance(exc, ValueError) else 'Downloader submission failed.'
        if attempted and not isinstance(exc, SubmissionRejected):
            try:
                if adapter.contains(hashes):
                    finish(db, identity, 'added', 'Confirmed in ' + profile['name'] + '.')
                    return
            except Exception:
                pass
        uncertain = attempted and not isinstance(exc, SubmissionRejected)
        finish(db, identity, 'unknown' if uncertain else 'error', message + (' Acceptance is uncertain; check status before retrying.' if uncertain else ''))
    finally:
        _slots.release()


def register(app, safe_session_lookup):
    db = Path(app.config['DATABASE_PATH'])
    initialize(db)
    key = encryption_key(db)
    signer = URLSafeTimedSerializer(key, salt='nightfeed-downloaders-csrf')
    bp = Blueprint('downloaders', __name__)

    @app.context_processor
    def downloader_context():
        return {'downloader_csrf': signer.dumps('downloaders'), 'downloader_types': ADAPTER_LABELS}

    @bp.before_request
    def protect_mutations():
        from .auth import same_origin
        if request.method != 'POST':
            return
        try:
            token = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token', '')
            if signer.loads(token, max_age=86400) != 'downloaders':
                raise BadSignature('Invalid token')
        except (BadSignature, SignatureExpired):
            return jsonify(error='Page token expired or invalid. Reload the page and retry.'), 403
        if not same_origin():
            return jsonify(error='Cross-origin requests are not allowed.'), 403

    @bp.errorhandler(ValueError)
    def invalid(exc):
        return jsonify(error=str(exc)), 400

    @bp.errorhandler(RuntimeError)
    def expired(exc):
        return jsonify(error='Safe browser session is unavailable or expired. Download the file again.'), 410

    @bp.get('/settings/downloaders')
    def settings():
        editing = get_profile(db, request.args['edit']) if request.args.get('edit') else None
        safe_profiles = [{k: v for k, v in p.items() if k != 'secret'} | {'has_secret': bool(p['secret'])} for p in profiles(db)]
        if editing:
            editing = {k: v for k, v in editing.items() if k != 'secret'} | {'has_secret': bool(editing['secret'])}
            editing['extensions'] = ', '.join(json.loads(editing['extensions']))
            editing['allowed_categories'] = '\n'.join(json.loads(editing['allowed_categories']))
        with closing(connect(db)) as conn:
            history = conn.execute('SELECT * FROM downloader_jobs ORDER BY created_at DESC LIMIT 50').fetchall()
        return render_template('downloaders.html', profiles=[], downloaders=safe_profiles, editing=editing, history=history)

    @bp.post('/settings/downloaders/save')
    def save():
        existing = get_profile(db, request.form['id']) if request.form.get('id') else None
        try:
            values = parse_profile(request.form, existing, key)
        except ValueError as exc:
            # Preserve editable fields, never echo a newly entered secret back into HTML.
            form = request.form.to_dict()
            form.pop('secret', None)
            form['has_secret'] = bool(existing and existing['secret'])
            for checkbox in ('enabled', 'verify_tls', 'require_category', 'start_immediately'):
                form[checkbox] = int(bool(request.form.get(checkbox)))
            return render_template('downloaders.html', profiles=[], downloaders=[{k: v for k, v in p.items() if k != 'secret'} for p in profiles(db)], editing=form, history=[], error=str(exc)), 400
        with closing(connect(db)) as conn:
            if existing:
                conn.execute('UPDATE downloaders SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?', list(values.values()) + [existing['id']])
            else:
                conn.execute('INSERT INTO downloaders (' + ','.join(values) + ') VALUES (' + ','.join('?' for _ in values) + ')', list(values.values()))
            conn.commit()
        return redirect(url_for('downloaders.settings'))

    @bp.post('/settings/downloaders/<int:identity>/delete')
    def delete(identity):
        with closing(connect(db)) as conn:
            if conn.execute("SELECT 1 FROM downloader_jobs WHERE downloader_id=? AND status='sending'", (identity,)).fetchone():
                raise ValueError('Wait for active submissions to finish before deleting this profile.')
            conn.execute('DELETE FROM downloaders WHERE id=?', (identity,))
            conn.commit()
        return redirect(url_for('downloaders.settings'))

    @bp.post('/settings/downloaders/<int:identity>/test')
    def test(identity):
        profile = get_profile(db, identity)
        adapter = ADAPTERS[profile['kind']](profile, key)
        return jsonify(version=adapter.version, api_version=adapter.api_version, categories=adapter.categories())

    @bp.get('/api/downloaders/<int:identity>/categories')
    def categories(identity):
        profile = get_profile(db, identity)
        if not profile['enabled']:
            raise ValueError('Downloader is disabled.')
        adapter = ADAPTERS[profile['kind']](profile, key)
        return jsonify(categories=adapter.categories(), default_category=profile['default_category'], require_category=bool(profile['require_category']), start_immediately=bool(profile['start_immediately']))

    @bp.post('/profiles/<int:profile_id>/items/<int:item_id>/safe/<session_id>/downloads/<download_id>/send')
    def send(profile_id, item_id, session_id, download_id):
        session = safe_session_lookup(session_id, profile_id, item_id)
        if session is None:
            return jsonify(error='Safe browser session expired. Download the file again.'), 410
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            raise ValueError('Submission must be a JSON object.')
        profile = get_profile(db, payload.get('downloader_id'))
        if not profile['enabled']:
            raise ValueError('Downloader is disabled.')
        # Copy inside the browser worker before its temporary directory can be removed.
        downloaded = session.execute('download_copy', download_id=download_id)
        if not any(p['id'] == profile['id'] for p in eligible_profiles(db, downloaded['name'])):
            raise ValueError('No matching file-extension route for this downloader.')
        fingerprint, hashes = ADAPTERS[profile['kind']].identify(downloaded['data'])
        category = payload.get('category', '')
        start = payload.get('start_immediately', bool(profile['start_immediately']))
        if not isinstance(category, str) or len(category) > 200 or any(ord(c) < 32 for c in category) or not isinstance(start, bool):
            raise ValueError('Invalid category or start option.')
        allowed = json.loads(profile['allowed_categories'])
        if (profile['require_category'] and not category) or (category and allowed and category not in allowed):
            raise ValueError('Select an allowed category.')
        with closing(connect(db)) as conn:
            conn.execute('BEGIN IMMEDIATE')
            existing = conn.execute("SELECT * FROM downloader_jobs WHERE downloader_id=? AND fingerprint=? AND status IN ('sending','added','already_present','unknown') ORDER BY created_at DESC LIMIT 1", (profile['id'], fingerprint)).fetchone()
            if existing and existing['status'] in {'sending', 'unknown'}:
                return jsonify(job_public(dict(existing))), 202
            if not _slots.acquire(blocking=False):
                return jsonify(error='All downloader slots are busy. Try again shortly.'), 429
            identity = secrets.token_urlsafe(24)
            try:
                conn.execute('INSERT INTO downloader_jobs (id,downloader_id,destination,file_name,category,fingerprint,hashes,status,message) VALUES (?,?,?,?,?,?,?,?,?)', (identity,profile['id'],profile['name'],downloaded['name'],category,fingerprint,json.dumps(hashes),'sending','Sending to ' + profile['name'] + '…'))
                conn.commit()
                Thread(target=submit_worker, args=(db,key,profile,identity,downloaded['data'],hashes,category,start), daemon=True).start()
            except Exception:
                _slots.release()
                raise
        return jsonify(job_public(job(db, identity))), 202

    @bp.get('/api/downloader-jobs/<identity>')
    def job_status(identity):
        return jsonify(job_public(job(db, identity)))

    @bp.post('/api/downloader-jobs/<identity>/check')
    def check(identity):
        value = job(db, identity)
        if value['status'] == 'sending':
            return jsonify(job_public(value)), 202
        profile = get_profile(db, value['downloader_id'])
        if not profile['enabled']:
            raise ValueError('Downloader is disabled.')
        adapter = ADAPTERS[profile['kind']](profile, key)
        if adapter.contains(json.loads(value['hashes'])):
            finish(db, identity, 'added', 'Confirmed in ' + profile['name'] + '.')
        else:
            finish(db, identity, 'error', 'Download is not currently present. You may explicitly submit it again.')
        return jsonify(job_public(job(db, identity)))

    app.register_blueprint(bp)
