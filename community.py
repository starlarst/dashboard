import asyncio
import base64
import copy
from collections import deque
import io
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urlunparse

from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse, Response

log = logging.getLogger(__name__)

try:                           # optional: pip install pillow  (extra validation of custom emoji images)
    from PIL import Image
except Exception:
    Image = None

STATUSES = ('online', 'idle', 'dnd', 'offline')
PRESENCE_TIMEOUT = 90
IDLE_AFTER = 300
MAX_TEXT = 500
MAX_STATUS_TEXT = 60
# The Lounge keeps its newest KEEP_LOUNGE messages on screen. Older ones are MOVED to
# community_messages_archive, never deleted. Direct messages are never pruned.
KEEP_LOUNGE = 1000
SEND_INTERVAL = 1.0
TYPING_TTL = 5.0               # seconds a "typing" signal stays alive
REACTIONS = ('👍', '❤️', '😂', '😮', '😢', '🔥', '🎉', '⚔️')
KEY_RE = re.compile(r'^(email|discord):[0-9]{1,20}$')
MAX_PINS = 25                  # pinned messages kept per channel

# ── where data lives ───────────────────────────────────────────────────────
# On a host with a temporary filesystem (Render's free tier, Heroku, most containers) anything
# written next to the code is ERASED on every deploy/restart. Point DATA_DIR at a persistent
# disk (for example /var/data) and the database, uploads and backups all survive.
DATA_DIR = os.getenv('DATA_DIR') or os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.getenv('COMMUNITY_UPLOAD_DIR') or os.path.join(DATA_DIR, 'community_uploads')
MIN_FREE_BYTES = int(os.getenv('MIN_FREE_MB', '300')) * 1024 * 1024   # refuse uploads below this much free disk

# ── custom emojis ──────────────────────────────────────────────────────────
MAX_EMOJI_BYTES = 256 * 1024
MAX_EMOJI_PX = 512
MAX_EMOJIS_PER_USER = 20
MAX_EMOJIS_ADMIN = 200
EMOJI_NAME_RE = re.compile(r'^[a-z0-9_]{2,24}$')

# ── photo / video uploads ──────────────────────────────────────────────────
# Keep dashboard_app.py's DDOS_BODY_LIMITS a little above this number.
MAX_UPLOAD_MB = int(os.getenv('MAX_UPLOAD_MB', '50'))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
MAX_USER_STORAGE = 500 * 1024 * 1024
UPLOAD_INTERVAL = 3.0
UPLOAD_BODY_RE = re.compile(r'^(image|video):([0-9]{1,12})$')
HEAD_BYTES = 1024 * 1024       # the start of each upload is kept to read its image size
MAX_IMAGE_SIDE = 12000
MAX_IMAGE_PIXELS = 100_000_000
RANGE_CHUNK = 8 * 1024 * 1024
UPLOAD_SLOTS = asyncio.Semaphore(3)     # at most 3 uploads in flight, so memory and disk stay safe

# ── gifts ──────────────────────────────────────────────────────────────────
MAX_GIFT_GOLD = 1_000_000
MAX_GIFT_ITEM_QTY = 999
MAX_GIFT_NOTE = 100
GIFT_INTERVAL = 3.0
# New accounts (first seen in the Community less than NEW_ACCOUNT_DAYS ago) get much lower daily gift caps.
NEW_ACCOUNT_DAYS = 3
GIFT_DAILY_GOLD, GIFT_DAILY_ITEMS = 100_000, 100
NEW_DAILY_GOLD, NEW_DAILY_ITEMS = 5_000, 10
REPORTS_PER_HOUR = 10
# ── anti-abuse
FLOOD_MAX = 20                 # messages per minute per person
DUP_MAX = 3                    # the same text this many times in 2 minutes is blocked
NEW_DM_PEOPLE = 5              # new accounts can start this many new conversations per day
NEW_UPLOADS_PER_DAY = 5
AUTO_MUTE_REPORTS = 3          # different established accounts reporting someone within 24 h...
AUTO_MUTE_HOURS = 1            # ...mutes them for this long until a moderator reviews
LINK_RE = re.compile(r'(https?://|www\.|discord\.gg/|discord(?:app)?\.com/invite)', re.I)
XMP_UUID = bytes.fromhex('BE7ACFCB97A942E89C71999491E3AFAC')
NO_GIFT = {'Adventurer Guild Card'}     # bound to the character that earned it

GIF_HOSTS = {
    'media.giphy.com', 'media0.giphy.com', 'media1.giphy.com', 'media2.giphy.com',
    'media3.giphy.com', 'media4.giphy.com', 'i.giphy.com', 'media.tenor.com', 'c.tenor.com',
}
GIF_EXT = ('.gif', '.webp')
GIPHY_PAGE_RE = re.compile(r'^https://giphy\.com/gifs/(?:[^/?#]*-)?([A-Za-z0-9]{5,30})/?(?:[?#].*)?$')
CTRL_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')

_lock = threading.Lock()
_tls = threading.local()       # files to delete only AFTER the database transaction has committed
_DB = None
_last_send = {}
_last_react = {}
_last_emoji = {}
_last_gif_search = {}
_last_upload = {}
_last_gift = {}
_last_search = {}
_recent = {}                   # account_key -> deque of send times (flood control)
_last_text = {}                # account_key -> (text, repeat count, time)
_prune_at = 0.0
_gif_cache = {}
_typing = {}                   # channel -> {account_key: (name, monotonic_time)}


def _now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def normalize_gif_url(url):
    """Return a safe https GIF URL or None. Only allowlisted GIF hosts are accepted."""
    if not isinstance(url, str):
        return None
    url = url.strip()
    if not url or len(url) > 400 or re.search(r'\s', url):
        return None
    m = GIPHY_PAGE_RE.match(url)
    if m:
        return f'https://media.giphy.com/media/{m.group(1)}/giphy.gif'
    try:
        p = urlparse(url)
    except ValueError:
        return None
    if p.scheme != 'https' or p.hostname not in GIF_HOSTS:
        return None
    if p.username or p.password or p.port not in (None, 443):
        return None
    if not p.path.lower().endswith(GIF_EXT):
        return None
    return urlunparse((p.scheme, p.hostname, p.path, '', p.query, ''))


def _safe_avatar(url):
    if isinstance(url, str) and url.startswith('https://') and len(url) < 300 and not re.search(r'[\s<>"\']', url):
        return url
    return ''


def _effective(row, now):
    if row['status'] == 'offline':
        return 'offline'
    if now - (row['last_seen'] or 0) > PRESENCE_TIMEOUT:
        return 'offline'
    if row['status'] == 'online' and now - (row['last_active'] or 0) > IDLE_AFTER:
        return 'idle'
    return row['status']


def _canon(a, b):
    x, y = sorted([a, b])
    return f'dm:{x}|{y}'


def _err(msg, code=400):
    return JSONResponse({'ok': False, 'error': msg}, status_code=code)


def _ident(request: Request):
    su = request.session.get('user') if hasattr(request, 'session') else None
    if not su or su.get('discord_id') is None:
        return None
    auth_type = su.get('auth_type', 'email')
    uid = str(su['discord_id'])
    key = f'{auth_type}:{uid}'
    if not KEY_RE.match(key):
        return None
    name = str(su.get('nickname') or su.get('username') or su.get('name') or 'Adventurer')[:40]
    return {'key': key, 'uid': uid, 'auth_type': auth_type, 'name': name,
            'avatar': _safe_avatar(su.get('avatar')), 'is_admin': bool(su.get('is_admin'))}


def _init_tables():
    conn = _DB()
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS community_profiles (
            account_key TEXT PRIMARY KEY, name TEXT, avatar TEXT,
            status TEXT NOT NULL DEFAULT 'online', status_text TEXT NOT NULL DEFAULT '',
            last_seen REAL NOT NULL DEFAULT 0, last_active REAL NOT NULL DEFAULT 0, created_at TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS community_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL, sender_key TEXT NOT NULL,
            sender_name TEXT, kind TEXT NOT NULL DEFAULT 'text', body TEXT NOT NULL,
            created_at TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0)""")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(community_messages)")}
        if 'reply_to' not in cols:
            conn.execute("ALTER TABLE community_messages ADD COLUMN reply_to INTEGER")
        if 'edited_at' not in cols:
            conn.execute("ALTER TABLE community_messages ADD COLUMN edited_at TEXT")
        if 'pinned_at' not in cols:
            conn.execute("ALTER TABLE community_messages ADD COLUMN pinned_at TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cm_channel ON community_messages(channel, id)")
        # Lounge messages that scrolled out of the live window are moved here, not deleted.
        conn.execute("""CREATE TABLE IF NOT EXISTS community_messages_archive (
            id INTEGER PRIMARY KEY, channel TEXT NOT NULL, sender_key TEXT NOT NULL, sender_name TEXT,
            kind TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,
            reply_to INTEGER, edited_at TEXT, archived_at TEXT NOT NULL)""")
        # Every edit keeps the previous text, so a wrong or abusive edit can be recovered.
        conn.execute("""CREATE TABLE IF NOT EXISTS community_message_edits (
            id INTEGER PRIMARY KEY AUTOINCREMENT, message_id INTEGER NOT NULL, old_body TEXT NOT NULL,
            edited_at TEXT NOT NULL, editor_key TEXT NOT NULL)""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cme_msg ON community_message_edits(message_id)")
        # Permanent ledger of every gift, so a lost or disputed item can always be traced and restored.
        conn.execute("""CREATE TABLE IF NOT EXISTS community_gift_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, from_key TEXT NOT NULL,
            to_key TEXT NOT NULL, kind TEXT NOT NULL, amount INTEGER, item_id TEXT, item_name TEXT,
            qty INTEGER, note TEXT, message_id INTEGER)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS community_reads (
            account_key TEXT NOT NULL, channel TEXT NOT NULL, last_id INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (account_key, channel))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS community_blocks (
            blocker_key TEXT NOT NULL, blocked_key TEXT NOT NULL, PRIMARY KEY (blocker_key, blocked_key))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS community_reactions (
            message_id INTEGER NOT NULL, account_key TEXT NOT NULL, emoji TEXT NOT NULL,
            PRIMARY KEY (message_id, account_key, emoji))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS community_emojis (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            owner_key TEXT NOT NULL, mime TEXT NOT NULL, data BLOB NOT NULL,
            animated INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS community_uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT, uploader_key TEXT NOT NULL,
            mime TEXT NOT NULL, kind TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL)""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cu_uploader ON community_uploads(uploader_key)")
        conn.execute("""CREATE TABLE IF NOT EXISTS community_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT, message_id INTEGER NOT NULL, reporter_key TEXT NOT NULL,
            reporter_name TEXT, sender_key TEXT NOT NULL, sender_name TEXT, channel TEXT NOT NULL,
            kind TEXT NOT NULL, snapshot TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open', resolved_by TEXT, resolved_at TEXT,
            UNIQUE (message_id, reporter_key))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS community_sanctions (
            account_key TEXT PRIMARY KEY, kind TEXT NOT NULL, until REAL NOT NULL DEFAULT 0,
            reason TEXT, by_key TEXT, created_at TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS community_admin_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, admin_key TEXT NOT NULL,
            action TEXT NOT NULL, target_key TEXT, detail TEXT)""")
        # Per-channel notification mute. Muting never affects the unread counters, only sound/toast/push.
        conn.execute("""CREATE TABLE IF NOT EXISTS community_mutes (
            account_key TEXT NOT NULL, channel TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY (account_key, channel))""")
        conn.commit()
    finally:
        conn.close()


def _touch(conn, ident, active=True):
    now = time.time()
    conn.execute(
        "INSERT INTO community_profiles (account_key, name, avatar, status, last_seen, last_active, created_at) "
        "VALUES (?,?,?, 'online', ?, ?, ?) "
        "ON CONFLICT(account_key) DO UPDATE SET name=excluded.name, "
        "avatar=CASE WHEN excluded.avatar != '' THEN excluded.avatar ELSE community_profiles.avatar END, "
        "last_seen=excluded.last_seen, "
        "last_active=CASE WHEN ? THEN excluded.last_active ELSE community_profiles.last_active END",
        (ident['key'], ident['name'], ident['avatar'], now, now, _now_iso(), 1 if active else 0))
    return conn.execute("SELECT * FROM community_profiles WHERE account_key=?", (ident['key'],)).fetchone()


def _member(row, now, unread=0, blocked=False):
    eff = _effective(row, now)
    return {'key': row['account_key'], 'name': row['name'] or 'Adventurer',
            'avatar': _safe_avatar(row['avatar']), 'status': eff,
            'status_text': (row['status_text'] or '') if eff != 'offline' else '',
            'unread': unread, 'blocked': blocked}


def _reactions_for(conn, ids, me_key):
    ids = list(ids)
    if not ids:
        return {}
    ph = ','.join('?' * len(ids))
    agg = {}
    for r in conn.execute(
            f"SELECT message_id, emoji, account_key FROM community_reactions "
            f"WHERE message_id IN ({ph}) ORDER BY rowid", ids):
        per_msg = agg.setdefault(r['message_id'], {})
        item = per_msg.setdefault(r['emoji'], {'emoji': r['emoji'], 'count': 0, 'mine': False})
        item['count'] += 1
        if r['account_key'] == me_key:
            item['mine'] = True
    return {mid: list(d.values()) for mid, d in agg.items()}


def _preview_text(kind, body):
    if kind == 'gif':
        return 'GIF'
    if kind == 'sticker':
        return 'Sticker'
    if kind == 'upload':
        return 'Video' if str(body or '').startswith('video:') else 'Photo'
    if kind == 'gift':
        return 'Gift'
    if kind == 'item':
        return 'Item'
    return ' '.join((body or '').split())[:80]


def _serialize(conn, rows, me_key):
    rows = list(rows)
    if not rows:
        return []
    reacts = _reactions_for(conn, [r['id'] for r in rows], me_key)
    parent_ids = list({r['reply_to'] for r in rows if r['reply_to']})
    parents = {}
    if parent_ids:
        ph = ','.join('?' * len(parent_ids))
        for p in conn.execute(
                f"SELECT id, sender_name, kind, body, deleted FROM community_messages WHERE id IN ({ph})",
                parent_ids):
            parents[p['id']] = p
    missing = [i for i in parent_ids if i not in parents]      # replied-to messages that were archived
    if missing:
        ph2 = ','.join('?' * len(missing))
        for p in conn.execute(
                f"SELECT id, sender_name, kind, body, deleted FROM community_messages_archive WHERE id IN ({ph2})",
                missing):
            parents[p['id']] = p
    keys = rows[0].keys()
    out = []
    for r in rows:
        reply = None
        if r['reply_to']:
            p = parents.get(r['reply_to'])
            if p is None or p['deleted']:
                reply = {'id': r['reply_to'], 'name': '', 'preview': '', 'deleted': True}
            else:
                reply = {'id': p['id'], 'name': p['sender_name'] or 'Adventurer',
                         'preview': _preview_text(p['kind'], p['body']), 'deleted': False}
        out.append({'id': r['id'], 'sender': r['sender_key'], 'name': r['sender_name'] or 'Adventurer',
                    'avatar': _safe_avatar(r['avatar']) if 'avatar' in keys else '',
                    'kind': r['kind'], 'body': r['body'], 'ts': r['created_at'],
                    'mine': r['sender_key'] == me_key,
                    'edited': bool(r['edited_at']),
                    'pinned': bool(r['pinned_at']) if 'pinned_at' in keys else False,
                    'reply': reply,
                    'reactions': reacts.get(r['id'], [])})
    return out


def _updates(conn, chan, me_key, hidden):
    rows = conn.execute(
        "SELECT id, sender_key, body, edited_at FROM community_messages "
        "WHERE channel=? AND deleted=0 ORDER BY id DESC LIMIT 60", (chan,)).fetchall()
    if not rows:
        return []
    reacts = _reactions_for(conn, [r['id'] for r in rows], me_key)
    return [{'id': r['id'], 'body': r['body'], 'edited': bool(r['edited_at']),
             'reactions': reacts.get(r['id'], [])}
            for r in rows if r['sender_key'] not in hidden]


def _can_access(ident, channel):
    if channel == 'lounge':
        return True
    if channel.startswith('dm:'):
        return ident['key'] in channel[3:].split('|')
    return False


def _is_admin(conn, ident):
    if ident['is_admin']:
        return True
    if ident['auth_type'] == 'email':
        try:
            r = conn.execute("SELECT is_admin FROM auth_users WHERE auth_id=?", (int(ident['uid']),)).fetchone()
            return bool(r and r['is_admin'])
        except Exception:
            return False
    return False


def _parse_channel(conn, ident, raw):
    raw = str(raw or 'lounge')
    if raw == 'lounge':
        return 'lounge', None, None
    if raw.startswith('dm:'):
        other = raw[3:]
        if not KEY_RE.match(other) or other == ident['key']:
            return None, None, _err('Unknown conversation.', 404)
        if not conn.execute("SELECT 1 FROM community_profiles WHERE account_key=?", (other,)).fetchone():
            return None, None, _err('That adventurer has not joined the Community yet.', 404)
        return _canon(ident['key'], other), other, None
    return None, None, _err('Unknown conversation.', 404)


def _blocks(conn, me):
    mine = {r[0] for r in conn.execute("SELECT blocked_key FROM community_blocks WHERE blocker_key=?", (me,))}
    theirs = {r[0] for r in conn.execute("SELECT blocker_key FROM community_blocks WHERE blocked_key=?", (me,))}
    return mine, theirs


# ───────────────────────────── bans, mutes, gift limits
def _until_text(until):
    if not until:
        return ''
    return ' until ' + datetime.fromtimestamp(until, timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


def _sanction(conn, key):
    r = conn.execute("SELECT kind, until, reason FROM community_sanctions WHERE account_key=?", (key,)).fetchone()
    if r and r['until'] and r['until'] < time.time():          # expired
        conn.execute("DELETE FROM community_sanctions WHERE account_key=?", (key,))
        return None
    return r


def _sanction_err(s):
    what = 'muted' if s['kind'] == 'mute' else 'banned from the Community'
    msg = f"You are {what}{_until_text(s['until'])}."
    return _err(msg + (f" Reason: {s['reason']}" if s['reason'] else ''), 403)


def _ban_block(conn, ident):
    s = _sanction(conn, ident['key'])
    if s and s['kind'] == 'ban' and not _is_admin(conn, ident):
        return _sanction_err(s)
    return None


def _muted(conn, ident):
    s = _sanction(conn, ident['key'])
    if s and not _is_admin(conn, ident):
        return _sanction_err(s)
    return None


def _admin_only(conn, ident):
    return None if _is_admin(conn, ident) else _err('Not allowed.', 403)


def _gift_limits(conn, key):
    r = conn.execute("SELECT created_at FROM community_profiles WHERE account_key=?", (key,)).fetchone()
    try:
        joined = datetime.strptime(r['created_at'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
        is_new = datetime.now(timezone.utc) - joined < timedelta(days=NEW_ACCOUNT_DAYS)
    except Exception:
        is_new = True
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime('%Y-%m-%dT%H:%M:%SZ')
    gold = conn.execute("SELECT COALESCE(SUM(amount),0) FROM community_gift_log "
                        "WHERE from_key=? AND kind='gold' AND created_at>=?", (key, cutoff)).fetchone()[0]
    items = conn.execute("SELECT COALESCE(SUM(qty),0) FROM community_gift_log "
                         "WHERE from_key=? AND kind='item' AND created_at>=?", (key, cutoff)).fetchone()[0]
    cap_g, cap_i = (NEW_DAILY_GOLD, NEW_DAILY_ITEMS) if is_new else (GIFT_DAILY_GOLD, GIFT_DAILY_ITEMS)
    return {'new': is_new, 'gold_left': max(0, cap_g - gold), 'items_left': max(0, cap_i - items)}


# ───────────────────────────── strip location/camera metadata from photos
def _exif_orientation(t):
    try:
        bo = 'little' if t[:2] == b'II' else 'big' if t[:2] == b'MM' else None
        if not bo:
            return None
        off = int.from_bytes(t[4:8], bo)
        for k in range(int.from_bytes(t[off:off + 2], bo)):
            e = t[off + 2 + 12 * k: off + 14 + 12 * k]
            if int.from_bytes(e[:2], bo) == 0x0112:
                v = int.from_bytes(e[8:10], bo)
                return v if 1 <= v <= 8 else None
    except Exception:
        pass
    return None


def _clean_jpeg(d):
    # Drops EXIF/XMP (APP1), Photoshop (APP13) and comment segments, so GPS and camera data go away.
    # Only the rotation flag is kept, otherwise phone photos would show up sideways.
    if d[:3] != b'\xff\xd8\xff':
        return d
    out, i, n, orient = bytearray(b'\xff\xd8'), 2, len(d), None
    while i + 4 <= n:
        if d[i] != 0xff:
            break
        m = d[i + 1]
        if m == 0xff:
            i += 1
            continue
        if m == 0xda:
            break
        if m in (0xd8, 0x01) or 0xd0 <= m <= 0xd7:
            out += d[i:i + 2]
            i += 2
            continue
        ln = int.from_bytes(d[i + 2:i + 4], 'big')
        seg = d[i:i + 2 + ln]
        if m == 0xe1 and seg[4:10] == b'Exif\x00\x00':
            orient = _exif_orientation(seg[10:])
        if m not in (0xe1, 0xed, 0xfe):
            out += seg
        i += 2 + ln
    out += d[i:]
    if orient and orient != 1:
        exif = (b'Exif\x00\x00MM\x00*\x00\x00\x00\x08\x00\x01\x01\x12\x00\x03\x00\x00\x00\x01'
                + orient.to_bytes(2, 'big') + b'\x00\x00\x00\x00\x00\x00')
        out[2:2] = b'\xff\xe1' + (len(exif) + 2).to_bytes(2, 'big') + exif
    return bytes(out)


def _clean_png(d):
    if d[:8] != b'\x89PNG\r\n\x1a\n':
        return d
    out, i, n = bytearray(d[:8]), 8, len(d)
    while i + 12 <= n:
        ln = int.from_bytes(d[i:i + 4], 'big')
        typ, end = d[i + 4:i + 8], i + 12 + ln
        if end > n:
            return d
        if typ != b'eXIf':
            out += d[i:end]
        i = end
        if typ == b'IEND':
            break
    out += d[i:]
    return bytes(out)


def _strip_metadata(path, mime):
    if mime in ('video/mp4', 'video/quicktime'):
        _strip_mp4(path)
        return
    with open(path, 'rb') as f:
        data = f.read()
    cleaned = {'image/jpeg': _clean_jpeg, 'image/webp': _clean_webp}.get(mime, _clean_png)(data)
    if cleaned != data:
        tmp2 = path + '.clean.part'
        with open(tmp2, 'wb') as f:
            f.write(cleaned)
        os.replace(tmp2, path)


# ───────────────────────── account age, audit log, memory housekeeping
def _iso_ago(hours=0, days=0):
    return (datetime.now(timezone.utc) - timedelta(hours=hours, days=days)).strftime('%Y-%m-%dT%H:%M:%SZ')


def _is_new(conn, key):
    r = conn.execute("SELECT created_at FROM community_profiles WHERE account_key=?", (key,)).fetchone()
    try:
        return r['created_at'] > _iso_ago(days=NEW_ACCOUNT_DAYS)
    except Exception:
        return True


def _audit(conn, admin_key, action, target=None, detail=''):
    conn.execute("INSERT INTO community_admin_log (created_at, admin_key, action, target_key, detail) VALUES (?,?,?,?,?)",
                 (_now_iso(), admin_key, action, target, str(detail)[:300]))


def _gift_received(conn, key):
    cutoff = _iso_ago(hours=24)
    g = conn.execute("SELECT COALESCE(SUM(amount),0) FROM community_gift_log WHERE to_key=? AND kind='gold' AND created_at>=?",
                     (key, cutoff)).fetchone()[0]
    i = conn.execute("SELECT COALESCE(SUM(qty),0) FROM community_gift_log WHERE to_key=? AND kind='item' AND created_at>=?",
                     (key, cutoff)).fetchone()[0]
    return g, i


def _prune_state():
    # The per-user rate-limit dictionaries only ever grow; trim old entries every 10 minutes.
    global _prune_at
    now = time.monotonic()
    if now < _prune_at:
        return
    _prune_at = now + 600
    for d in (_last_send, _last_react, _last_emoji, _last_gif_search, _last_upload, _last_gift, _last_search):
        for k in [k for k, t in d.items() if now - t > 3600]:
            d.pop(k, None)
    for k in [k for k, v in _last_text.items() if now - v[2] > 3600]:
        _last_text.pop(k, None)
    for k in list(_recent):
        dq = _recent[k]
        while dq and now - dq[0] > 60:
            dq.popleft()
        if not dq:
            del _recent[k]
    for ch in list(_typing):
        b = _typing[ch]
        for k in [k for k, (_n, t) in b.items() if now - t > 60]:
            b.pop(k, None)
        if not b:
            del _typing[ch]


# ───────────────────────── WebP and MP4/MOV metadata
def _clean_webp(d):
    # Removes EXIF and XMP chunks and clears their flags in the VP8X header.
    if d[:4] != b'RIFF' or d[8:12] != b'WEBP':
        return d
    out, i, n = bytearray(b'WEBP'), 12, len(d)
    while i + 8 <= n:
        tag, ln = d[i:i + 4], int.from_bytes(d[i + 4:i + 8], 'little')
        end = i + 8 + ln + (ln & 1)
        if end > n:
            return d
        if tag in (b'EXIF', b'XMP '):
            pass
        elif tag == b'VP8X' and ln >= 10:
            chunk = bytearray(d[i:end])
            chunk[8] &= 0xF3                       # clear the EXIF (0x08) and XMP (0x04) flags
            out += chunk
        else:
            out += d[i:end]
        i = end
    return b'RIFF' + len(out).to_bytes(4, 'little') + bytes(out)


def _scrub_boxes(buf, start, end):
    # Phones store GPS in 'udta' (Android) and 'meta' (iPhone) boxes inside moov/trak. Each one is turned into a
    # same-size 'free' box full of zeros, so every offset in the file stays valid and the video still plays.
    i = start
    while i + 8 <= end:
        size = int.from_bytes(buf[i:i + 4], 'big')
        typ, hdr = bytes(buf[i + 4:i + 8]), 8
        if size == 1:
            if i + 16 > end:
                break
            size, hdr = int.from_bytes(buf[i + 8:i + 16], 'big'), 16
        elif size == 0:
            size = end - i
        if size < hdr or i + size > end:
            break
        if typ in (b'udta', b'meta'):
            buf[i + 4:i + 8] = b'free'
            buf[i + hdr:i + size] = bytes(size - hdr)
        elif typ in (b'moov', b'trak'):
            _scrub_boxes(buf, i + hdr, i + size)
        i += size


def _strip_mp4(path):
    with open(path, 'r+b') as f:
        f.seek(0, 2)
        total, pos = f.tell(), 0
        while pos + 8 <= total:
            f.seek(pos)
            h = f.read(32)
            size, typ, hdr = int.from_bytes(h[:4], 'big'), h[4:8], 8
            if size == 1:
                size, hdr = int.from_bytes(h[8:16], 'big'), 16
            elif size == 0:
                size = total - pos
            if size < hdr or pos + size > total:
                break
            if typ == b'moov' and size <= 64 * 1024 * 1024:
                f.seek(pos)
                buf = bytearray(f.read(size))
                orig = bytes(buf)
                _scrub_boxes(buf, hdr, size)
                if bytes(buf) != orig:
                    f.seek(pos)
                    f.write(buf)
            elif typ in (b'udta', b'meta') or (typ == b'uuid' and h[hdr:hdr + 16] == XMP_UUID):
                f.seek(pos + 4)
                f.write(b'free')
                left = size - hdr
                f.seek(pos + hdr)
                while left > 0:
                    k = min(left, 1 << 20)
                    f.write(bytes(k))
                    left -= k
            pos += size


def _clean_text(raw):
    return CTRL_RE.sub('', str(raw or '')).strip()


_ARCHIVE_COLS = 'id, channel, sender_key, sender_name, kind, body, created_at, deleted, reply_to, edited_at'


def _archive_lounge(conn):
    """Move Lounge messages older than the newest KEEP_LOUNGE into the archive table. Nothing is deleted.
    Pinned messages are left in the live table (whatever their age) so a pinned announcement never
    disappears from the main channel while it is still pinned."""
    row = conn.execute("SELECT id FROM community_messages WHERE channel='lounge' "
                       "ORDER BY id DESC LIMIT 1 OFFSET ?", (KEEP_LOUNGE,)).fetchone()
    if not row:
        return
    cutoff = row[0]
    conn.execute(f"INSERT OR IGNORE INTO community_messages_archive ({_ARCHIVE_COLS}, archived_at) "
                 f"SELECT {_ARCHIVE_COLS}, ? FROM community_messages WHERE channel='lounge' AND id<=? "
                 f"AND pinned_at IS NULL",
                 (_now_iso(), cutoff))
    conn.execute("DELETE FROM community_messages WHERE channel='lounge' AND id<=? AND pinned_at IS NULL "
                 "AND id IN (SELECT id FROM community_messages_archive)", (cutoff,))


def _post_message(conn, chan, ident, kind, payload, reply_id=None):
    cur = conn.execute(
        "INSERT INTO community_messages (channel, sender_key, sender_name, kind, body, created_at, reply_to) "
        "VALUES (?,?,?,?,?,?,?)",
        (chan, ident['key'], ident['name'], kind, payload, _now_iso(), reply_id))
    new_id = cur.lastrowid
    if chan == 'lounge':
        _archive_lounge(conn)
    if chan.startswith('dm:'):   # my own message counts as read
        conn.execute("INSERT INTO community_reads (account_key, channel, last_id) VALUES (?,?,?) "
                     "ON CONFLICT(account_key, channel) DO UPDATE SET last_id=MAX(community_reads.last_id, excluded.last_id)",
                     (ident['key'], chan, new_id))
    r = conn.execute("SELECT m.*, p.avatar AS avatar FROM community_messages m "
                     "LEFT JOIN community_profiles p ON p.account_key=m.sender_key WHERE m.id=?", (new_id,)).fetchone()
    return _serialize(conn, [r], ident['key'])[0]


# ───────────────────────────── file sniffing and image size
def _sniff_image(data):
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return 'image/gif', data.count(b'\x21\xf9\x04') > 1
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        actl = data.find(b'acTL')
        idat = data.find(b'IDAT')
        return 'image/png', (actl != -1 and (idat == -1 or actl < idat))
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        animated = data[12:16] == b'VP8X' and len(data) > 20 and bool(data[20] & 0x02)
        return 'image/webp', animated
    return None


def _sniff_upload(head):
    head = bytes(head[:64])
    if head[:6] in (b'GIF87a', b'GIF89a'):
        return 'image/gif', 'image'
    if head[:8] == b'\x89PNG\r\n\x1a\n':
        return 'image/png', 'image'
    if head[:3] == b'\xff\xd8\xff':
        return 'image/jpeg', 'image'
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'image/webp', 'image'
    if head[4:8] == b'ftyp':
        return ('video/quicktime' if head[8:12] == b'qt  ' else 'video/mp4'), 'video'
    if head[:4] == b'\x1aE\xdf\xa3' and b'webm' in head:
        return 'video/webm', 'video'
    return None


def _image_dims(d):
    """(width, height) read from the file header without decoding the picture, or None.
    This stops "decompression bombs": tiny files that unpack to enormous images and freeze the browsers that open them."""
    try:
        if d[:8] == b'\x89PNG\r\n\x1a\n' and d[12:16] == b'IHDR':
            return int.from_bytes(d[16:20], 'big'), int.from_bytes(d[20:24], 'big')
        if d[:6] in (b'GIF87a', b'GIF89a'):
            return int.from_bytes(d[6:8], 'little'), int.from_bytes(d[8:10], 'little')
        if d[:4] == b'RIFF' and d[8:12] == b'WEBP':
            tag = d[12:16]
            if tag == b'VP8X':
                return 1 + int.from_bytes(d[24:27], 'little'), 1 + int.from_bytes(d[27:30], 'little')
            if tag == b'VP8 ' and d[23:26] == b'\x9d\x01\x2a':
                return int.from_bytes(d[26:28], 'little') & 0x3fff, int.from_bytes(d[28:30], 'little') & 0x3fff
            if tag == b'VP8L' and d[20] == 0x2f:
                bits = int.from_bytes(d[21:25], 'little')
                return (bits & 0x3fff) + 1, ((bits >> 14) & 0x3fff) + 1
            return None
        if d[:3] == b'\xff\xd8\xff':
            i, n = 2, len(d)
            while i + 4 <= n:
                if d[i] != 0xff:
                    i += 1
                    continue
                marker = d[i + 1]
                if marker == 0xff:
                    i += 1
                    continue
                if marker in (0xd8, 0x01) or 0xd0 <= marker <= 0xd7:
                    i += 2
                    continue
                if marker in (0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf):
                    return int.from_bytes(d[i + 7:i + 9], 'big'), int.from_bytes(d[i + 5:i + 7], 'big')
                i += 2 + int.from_bytes(d[i + 2:i + 4], 'big')
            return None
    except Exception:
        return None
    return None


def _dims_ok(dims, side_cap, pixel_cap):
    if not dims:
        return False
    w, h = dims
    return 0 < w <= side_cap and 0 < h <= side_cap and w * h <= pixel_cap


# ───────────────────────────── upload storage helpers
def _upload_path(upload_id):
    return os.path.join(UPLOAD_DIR, f'{int(upload_id)}.bin')


def _remove_file(upload_id):
    try:
        os.remove(_upload_path(upload_id))
    except FileNotFoundError:
        pass
    except OSError:
        log.exception('[community] could not remove upload file %s', upload_id)


def _defer_file_removal(upload_id):
    """Delete the file only if the surrounding transaction actually commits (see _exec)."""
    if not hasattr(_tls, 'pending'):
        _tls.pending = []
    _tls.pending.append(upload_id)


def _drop_upload(conn, upload_id):
    conn.execute("DELETE FROM community_uploads WHERE id=?", (upload_id,))
    _defer_file_removal(upload_id)


def _drop_upload_for_body(conn, body):
    m = UPLOAD_BODY_RE.match(str(body or ''))
    if m:
        _drop_upload(conn, int(m.group(2)))


def _purge_orphan_uploads(conn):
    """Remove uploads that were never sent. A file referenced by a live OR archived message is always kept."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
    rows = conn.execute(
        "SELECT u.id FROM community_uploads u WHERE u.created_at < ? "
        "AND NOT EXISTS (SELECT 1 FROM community_messages m WHERE m.kind='upload' AND m.deleted=0 "
        "AND m.body = u.kind || ':' || u.id) "
        "AND NOT EXISTS (SELECT 1 FROM community_messages_archive a WHERE a.kind='upload' AND a.deleted=0 "
        "AND a.body = u.kind || ':' || u.id)", (cutoff,)).fetchall()
    for r in rows:
        _drop_upload(conn, r[0])


def _sweep_temp_files():
    """Delete half-finished upload temp files older than an hour. Real uploads are never touched."""
    try:
        cutoff = time.time() - 3600
        for name in os.listdir(UPLOAD_DIR):
            if name.startswith('tmp_') and name.endswith('.part'):
                p = os.path.join(UPLOAD_DIR, name)
                if os.path.getmtime(p) < cutoff:
                    os.remove(p)
    except OSError:
        pass


def _fsync_path(path):
    """Flush a finished temp file to disk, best effort.

    The file MUST be opened with write access ('r+b'): on Windows os.fsync() on a read-only handle
    fails with "[Errno 9] Bad file descriptor". A failed fsync is not fatal (some filesystems
    don't support it), so it is logged and ignored instead of failing the whole upload."""
    try:
        with open(path, 'r+b') as f:
            os.fsync(f.fileno())
    except OSError:
        log.debug('[community] fsync not supported for %s', path, exc_info=True)


async def _receive_to_file(request, tmp):
    """Stream the request body straight to a temp file (never the whole upload in memory)."""
    size = 0
    head = bytearray()
    with open(tmp, 'wb') as f:
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                raise OverflowError
            if len(head) < HEAD_BYTES:
                head.extend(chunk[:HEAD_BYTES - len(head)])
            f.write(chunk)
    return size, bytes(head)


def _file_response(path, mime, request, headers):
    """Serve a stored file, with HTTP Range support (iPhones and Safari will not play video without it)."""
    size = os.path.getsize(path)
    headers = dict(headers, **{'Accept-Ranges': 'bytes'})
    rng = (request.headers.get('range') or '').strip()
    if not rng:
        return FileResponse(path, media_type=mime, headers=headers)
    m = re.match(r'^bytes=(\d*)-(\d*)$', rng)
    if not m or (m.group(1) == '' and m.group(2) == ''):
        return Response(status_code=416, headers={'Content-Range': f'bytes */{size}'})
    if m.group(1) == '':
        start = max(0, size - int(m.group(2)))
        end = size - 1
    else:
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else size - 1
    if start >= size or end < start:
        return Response(status_code=416, headers={'Content-Range': f'bytes */{size}'})
    end = min(end, size - 1, start + RANGE_CHUNK - 1)
    with open(path, 'rb') as f:
        f.seek(start)
        data = f.read(end - start + 1)
    headers['Content-Range'] = f'bytes {start}-{end}/{size}'
    return Response(content=data, status_code=206, media_type=mime, headers=headers)


# ───────────────────────────── gift helpers (read/write the dashboard's player_stats store)
def _load_stats(conn, key):
    r = conn.execute("SELECT data FROM player_stats WHERE account_key=?", (key,)).fetchone()
    if not r:
        return None
    try:
        d = json.loads(r['data'])
    except Exception:
        return None
    return d if isinstance(d, dict) else None


def _save_stats(conn, key, data):
    conn.execute("UPDATE player_stats SET data=?, last_updated=CURRENT_TIMESTAMP WHERE account_key=?",
                 (json.dumps(data), key))


def _qty_of(info):
    if isinstance(info, dict):
        info = info.get('quantity', info.get('qty', 1))
    try:
        return max(0, int(info))
    except (TypeError, ValueError):
        return 1


def _set_qty(d, n):
    if 'qty' in d and 'quantity' not in d:
        d['qty'] = n
    else:
        d['quantity'] = n


def _entry_name(e):
    return str(e.get('name', 'Unknown Item')) if isinstance(e, dict) else str(e)


def _giftable(e, name):
    """Equipped items and character-bound items cannot be gifted (unequip first)."""
    if name in NO_GIFT:
        return False
    return not (isinstance(e, dict) and e.get('equipped'))


def _inv_list(inv):
    """Normalise an inventory (dict or list) to [{'id', 'name', 'qty', 'rarity'}] of GIFTABLE items."""
    items = []
    if isinstance(inv, dict):
        for key, info in inv.items():
            q = _qty_of(info)
            d = info if isinstance(info, dict) else {}
            name = str(d.get('name', key))
            if q <= 0 or not _giftable(info, name) or not _giftable(info, str(key)):
                continue
            items.append({'id': str(key), 'name': name[:60], 'qty': q,
                          'rarity': str(d.get('rarity', 'common'))[:20]})
    elif isinstance(inv, list):
        agg = {}
        for e in inv:
            nm = _entry_name(e)
            q = _qty_of(e) if isinstance(e, dict) else 1
            if q <= 0 or not _giftable(e, nm):
                continue
            rar = str(e.get('rarity', 'common')) if isinstance(e, dict) else 'common'
            a = agg.setdefault(nm, {'id': nm, 'name': nm[:60], 'qty': 0, 'rarity': rar[:20]})
            a['qty'] += q
        items = list(agg.values())
    return items


def _inv_take(inv, item_id, qty):
    """Remove `qty` giftable items in place. Returns a copy of the item (for the receiver) or None if short."""
    if isinstance(inv, dict):
        info = inv.get(item_id)
        if info is None or not _giftable(info, item_id):
            return None
        have = _qty_of(info)
        if have < qty:
            return None
        template = copy.deepcopy(info)
        left = have - qty
        if left <= 0:
            del inv[item_id]
        elif isinstance(info, dict):
            _set_qty(info, left)
        else:
            inv[item_id] = left
        return template
    if isinstance(inv, list):
        matches = [i for i, e in enumerate(inv) if _entry_name(e) == item_id and _giftable(e, item_id)]
        have = sum(_qty_of(inv[i]) if isinstance(inv[i], dict) else 1 for i in matches)
        if not matches or have < qty:
            return None
        template = copy.deepcopy(inv[matches[0]])
        need = qty
        for i in reversed(matches):
            e = inv[i]
            q = _qty_of(e) if isinstance(e, dict) else 1
            if q <= need:
                need -= q
                del inv[i]
            else:
                _set_qty(e, q - need)
                need = 0
            if need == 0:
                break
        return template
    return None


def _inv_give(inv, item_id, qty, template):
    """Add `qty` of an item to the receiver's inventory in place. Received items always arrive unequipped."""
    if isinstance(template, dict):
        template = copy.deepcopy(template)
        if 'equipped' in template:
            template['equipped'] = False
    if isinstance(inv, dict):
        cur = inv.get(item_id)
        if cur is None:
            if isinstance(template, dict):
                _set_qty(template, qty)
                inv[item_id] = template
            else:
                inv[item_id] = qty
        elif isinstance(cur, dict):
            _set_qty(cur, _qty_of(cur) + qty)
        else:
            inv[item_id] = _qty_of(cur) + qty
        return
    if isinstance(inv, list):
        if isinstance(template, dict):
            for e in inv:
                if isinstance(e, dict) and _entry_name(e) == item_id and ('quantity' in e or 'qty' in e):
                    _set_qty(e, _qty_of(e) + qty)
                    return
            if 'quantity' in template or 'qty' in template:
                _set_qty(template, qty)
                inv.append(template)
            else:
                inv.extend(copy.deepcopy(template) for _ in range(qty))
        else:
            inv.extend([item_id] * qty)


# ───────────────────────────── transaction runner
def _exec(ident, fn):
    """Run fn(conn, ident, profile_row) inside ONE database transaction taken with BEGIN IMMEDIATE.
    Everything fn writes either all commits or none of it does, and no other writer (including the
    game engine, which uses the same lock) can change the rows it read in between."""
    conn = _DB()
    _prune_state()
    conn.isolation_level = None            # we control BEGIN / COMMIT / ROLLBACK ourselves
    _tls.pending = []
    try:
        try:
            conn.execute('PRAGMA busy_timeout=8000')
        except sqlite3.Error:
            pass
        with _lock:
            conn.execute('BEGIN IMMEDIATE')
            try:
                row = _touch(conn, ident, active=False)
                result = _ban_block(conn, ident) or fn(conn, ident, row)
                conn.execute('COMMIT')
            except BaseException:
                if conn.in_transaction:
                    conn.execute('ROLLBACK')
                _tls.pending = []
                raise
        for upload_id in _tls.pending:     # only now that the commit succeeded
            _remove_file(upload_id)
        _tls.pending = []
        return result
    except sqlite3.OperationalError as e:
        msg = str(e).lower()
        if 'locked' in msg or 'busy' in msg:
            log.warning('[community] database busy: %s', e)
            return _err('The server is busy right now. Please try again in a moment.', 503)
        log.exception('[community] database error')
        return _err('Server error. Please try again.', 500)
    except Exception:
        log.exception('[community] request failed')
        return _err('Server error. Please try again.', 500)
    finally:
        conn.close()


# ───────────────────────────── routes
def register_community_routes(app, db_factory):
    global _DB
    _DB = db_factory
    try:
        _init_tables()
    except Exception:
        log.exception('[community] could not create tables')
    try:
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        _sweep_temp_files()
    except OSError:
        log.exception('[community] could not create upload folder %s', UPLOAD_DIR)

    GIPHY_KEY = ('blmRPBedfYWeU8GXbYZlL9X5PQ8t02gK')

    async def _with_conn(request, fn):
        ident = _ident(request)
        if not ident:
            return JSONResponse({'ok': False, 'error': 'unauthorized'}, status_code=401)
        return await asyncio.to_thread(_exec, ident, fn)

    async def _body(request):
        if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
            return {}
        try:
            b = await request.json()
            return b if isinstance(b, dict) else {}
        except Exception:
            return {}

    # ---- me / status / heartbeat ------------------------------------------
    @app.get('/api/community/me')
    async def community_me(request: Request):
        def go(conn, ident, row):
            now = time.time()
            unread_total = 0
            mine = ident['key']
            for r in conn.execute(
                    "SELECT COUNT(*) FROM community_messages m LEFT JOIN community_reads r "
                    "ON r.account_key=? AND r.channel=m.channel "
                    "WHERE m.channel LIKE 'dm:%' AND (m.channel LIKE ? OR m.channel LIKE ?) "
                    "AND m.sender_key != ? AND m.deleted=0 AND m.id > COALESCE(r.last_id,0)",
                    (mine, f'dm:{mine}|%', f'dm:%|{mine}', mine)):
                unread_total = r[0]
            me = _member(row, now)
            me['chosen_status'] = row['status']
            me['status_text'] = row['status_text'] or ''
            muted = [r[0] for r in conn.execute(
                "SELECT channel FROM community_mutes WHERE account_key=?", (ident['key'],))]
            return {'ok': True, 'me': me, 'is_admin': _is_admin(conn, ident),
                    'gif_search': bool(GIPHY_KEY), 'unread_total': unread_total,
                    'reactions': list(REACTIONS), 'muted_channels': muted,
                    'limits': {'text': MAX_TEXT, 'status_text': MAX_STATUS_TEXT,
                               'upload_mb': MAX_UPLOAD_MB, 'gift_note': MAX_GIFT_NOTE, 'max_pins': MAX_PINS}}
        return await _with_conn(request, go)

    @app.post('/api/community/status')
    async def community_status(request: Request):
        body = await _body(request)

        def go(conn, ident, row):
            status = str(body.get('status', '')).lower()
            if status not in STATUSES:
                return _err('Pick Online, Idle, Do Not Disturb or Offline.')
            text = CTRL_RE.sub('', str(body.get('text', '') or '')).strip()[:MAX_STATUS_TEXT]
            now = time.time()
            conn.execute("UPDATE community_profiles SET status=?, status_text=?, last_seen=?, last_active=? "
                         "WHERE account_key=?", (status, text, now, now, ident['key']))
            row = conn.execute("SELECT * FROM community_profiles WHERE account_key=?", (ident['key'],)).fetchone()
            me = _member(row, now)
            me['chosen_status'] = row['status']
            me['status_text'] = row['status_text']
            return {'ok': True, 'me': me}
        return await _with_conn(request, go)

    @app.post('/api/community/heartbeat')
    async def community_heartbeat(request: Request):
        body = await _body(request)
        active = bool(body.get('active', True))

        def go(conn, ident, row):
            now = time.time()
            conn.execute("UPDATE community_profiles SET last_seen=?, last_active=CASE WHEN ? THEN ? ELSE last_active END "
                         "WHERE account_key=?", (now, 1 if active else 0, now, ident['key']))
            return {'ok': True}
        return await _with_conn(request, go)

    # ---- members ------------------------------------------------------------
    @app.get('/api/community/members')
    async def community_members(request: Request, q: str = ''):
        def go(conn, ident, row):
            now = time.time()
            q_clean = CTRL_RE.sub('', q or '').strip().lower()[:40]
            sql = "SELECT * FROM community_profiles WHERE account_key != ?"
            args = [ident['key']]
            if q_clean:
                sql += " AND lower(name) LIKE ? ESCAPE '\\'"
                args.append('%' + q_clean.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%')
            sql += " ORDER BY name COLLATE NOCASE LIMIT 300"
            rows = conn.execute(sql, args).fetchall()
            mine_blocked, _theirs = _blocks(conn, ident['key'])
            unread = {}
            for r in conn.execute(
                    "SELECT m.channel, COUNT(*) FROM community_messages m LEFT JOIN community_reads r "
                    "ON r.account_key=? AND r.channel=m.channel "
                    "WHERE m.channel LIKE 'dm:%' AND (m.channel LIKE ? OR m.channel LIKE ?) "
                    "AND m.sender_key != ? AND m.deleted=0 AND m.id > COALESCE(r.last_id,0) GROUP BY m.channel",
                    (ident['key'], f"dm:{ident['key']}|%", f"dm:%|{ident['key']}", ident['key'])):
                unread[r[0]] = r[1]
            order = {'online': 0, 'idle': 1, 'dnd': 2, 'offline': 3}
            members = [_member(m, now, unread.get(_canon(ident['key'], m['account_key']), 0),
                               m['account_key'] in mine_blocked) for m in rows]
            members.sort(key=lambda m: (0 if m['unread'] else 1, order[m['status']], m['name'].lower()))
            counts = {s: sum(1 for m in members if m['status'] == s) for s in STATUSES}
            return {'ok': True, 'members': members, 'counts': counts}
        return await _with_conn(request, go)

    # ---- profile card -----------------------------------------------------
    @app.get('/api/community/profile/{key}')
    async def community_profile(key: str, request: Request):
        def go(conn, ident, row):
            if not KEY_RE.match(key):
                return _err('Unknown adventurer.', 404)
            prow = conn.execute("SELECT * FROM community_profiles WHERE account_key=?", (key,)).fetchone()
            if not prow:
                return _err('That adventurer has not joined the Community yet.', 404)
            mine_blocked, theirs = _blocks(conn, ident['key'])
            member = _member(prow, time.time(), 0, key in mine_blocked)
            data = _load_stats(conn, key) or {}

            def _g(*names):
                for n in names:
                    v = data.get(n) if isinstance(data, dict) else None
                    if v not in (None, ''):
                        return v
                return None

            badges = data.get('badges') if isinstance(data, dict) else None
            badges = [str(b)[:40] for b in badges][:12] if isinstance(badges, list) else []
            return {'ok': True, 'profile': {
                'key': key, 'name': member['name'], 'avatar': member['avatar'],
                'status': member['status'], 'status_text': member['status_text'],
                'blocked': key in mine_blocked, 'blocked_me': key in theirs,
                'level': _g('level'), 'class': _g('class', 'character_class'),
                'guild': _g('guild', 'guild_name'), 'badges': badges,
                'joined': prow['created_at'] or '',
                'is_self': key == ident['key'],
            }}
        return await _with_conn(request, go)

    # ---- messages -------------------------------------------------------------
    @app.get('/api/community/messages')
    async def community_messages(request: Request, channel: str = 'lounge', after: int = 0, limit: int = 60, before: int = 0):
        def go(conn, ident, row):
            chan, other, bad = _parse_channel(conn, ident, channel)
            if bad:
                return bad
            limit_n = max(1, min(int(limit), 100))
            base = ("SELECT m.*, p.avatar AS avatar FROM community_messages m "
                    "LEFT JOIN community_profiles p ON p.account_key=m.sender_key "
                    "WHERE m.channel=? AND m.deleted=0 ")
            if after and after > 0:
                rows = conn.execute(base + "AND m.id>? ORDER BY m.id ASC LIMIT 100", (chan, after)).fetchall()
            else:
                if before and before > 0:          # scrolling up: older live messages, then the Lounge archive
                    rows = list(reversed(conn.execute(base + "AND m.id<? ORDER BY m.id DESC LIMIT ?",
                                                      (chan, before, limit_n)).fetchall()))
                    if len(rows) < limit_n and chan == 'lounge':
                        cut = rows[0]['id'] if rows else before
                        arch = conn.execute(
                            "SELECT a.*, p.avatar AS avatar FROM community_messages_archive a "
                            "LEFT JOIN community_profiles p ON p.account_key=a.sender_key "
                            "WHERE a.channel='lounge' AND a.deleted=0 AND a.id<? ORDER BY a.id DESC LIMIT ?",
                            (cut, limit_n - len(rows))).fetchall()
                        rows = list(reversed(arch)) + rows
                else:
                    rows = list(reversed(conn.execute(base + "ORDER BY m.id DESC LIMIT ?", (chan, limit_n)).fetchall()))
            mine_blocked, theirs = _blocks(conn, ident['key'])
            msgs = [m for m in _serialize(conn, rows, ident['key']) if m['sender'] not in mine_blocked]
            deleted = [r[0] for r in conn.execute(
                "SELECT id FROM community_messages WHERE channel=? AND deleted=1 AND id > "
                "(SELECT COALESCE(MAX(id),0) FROM community_messages WHERE channel=?) - 200", (chan, chan))]

            now_m = time.monotonic()
            bucket = _typing.get(chan, {})
            for k in [k for k, (_n, t) in bucket.items() if now_m - t >= TYPING_TTL]:
                bucket.pop(k, None)
            typing_names = [n for k, (n, _t) in bucket.items()
                            if k != ident['key'] and k not in mine_blocked and k not in theirs]

            out = {'ok': True, 'channel': channel, 'messages': msgs, 'deleted': deleted, 'typing': typing_names}
            if after and after > 0:
                out['updates'] = _updates(conn, chan, ident['key'], mine_blocked)
            else:
                out['has_more'] = len(rows) >= limit_n
                if not (before and before > 0):     # a true initial load of the channel, not "load older"
                    prev = conn.execute("SELECT last_id FROM community_reads WHERE account_key=? AND channel=?",
                                        (ident['key'], chan)).fetchone()
                    out['last_read_id'] = prev[0] if prev else 0
                    top = conn.execute("SELECT COALESCE(MAX(id),0) FROM community_messages WHERE channel=?",
                                       (chan,)).fetchone()[0]
                    conn.execute("INSERT INTO community_reads (account_key, channel, last_id) VALUES (?,?,?) "
                                 "ON CONFLICT(account_key, channel) DO UPDATE SET last_id=MAX(community_reads.last_id, excluded.last_id)",
                                 (ident['key'], chan, top))
            if other:
                prow = conn.execute("SELECT * FROM community_profiles WHERE account_key=?", (other,)).fetchone()
                info = _member(prow, time.time(), 0, other in mine_blocked)
                info['blocked_me'] = ident['key'] in {r[0] for r in conn.execute(
                    "SELECT blocked_key FROM community_blocks WHERE blocker_key=?", (other,))}
                out['other'] = info
            return out
        return await _with_conn(request, go)

    # ---- pinned messages ----------------------------------------------------
    @app.get('/api/community/pinned')
    async def community_pinned(request: Request, channel: str = 'lounge'):
        def go(conn, ident, row):
            chan, other, bad = _parse_channel(conn, ident, channel)
            if bad:
                return bad
            rows = conn.execute(
                "SELECT m.*, p.avatar AS avatar FROM community_messages m "
                "LEFT JOIN community_profiles p ON p.account_key=m.sender_key "
                "WHERE m.channel=? AND m.deleted=0 AND m.pinned_at IS NOT NULL "
                "ORDER BY m.pinned_at DESC LIMIT 50", (chan,)).fetchall()
            mine_blocked, _t = _blocks(conn, ident['key'])
            msgs = [m for m in _serialize(conn, rows, ident['key']) if m['sender'] not in mine_blocked]
            return {'ok': True, 'pinned': msgs}
        return await _with_conn(request, go)

    @app.post('/api/community/messages/{msg_id}/pin')
    async def community_pin(msg_id: int, request: Request):
        body = await _body(request)

        def go(conn, ident, row):
            deny = _admin_only(conn, ident)
            if deny:
                return deny
            m = conn.execute("SELECT id, channel, pinned_at FROM community_messages WHERE id=? AND deleted=0",
                             (msg_id,)).fetchone()
            if not m:
                return _err('Message not found.', 404)
            pin = bool(body.get('pinned', True))
            if pin:
                if not m['pinned_at']:
                    cnt = conn.execute(
                        "SELECT COUNT(*) FROM community_messages WHERE channel=? AND pinned_at IS NOT NULL",
                        (m['channel'],)).fetchone()[0]
                    if cnt >= MAX_PINS:
                        return _err(f'You can pin up to {MAX_PINS} messages per channel. Unpin one first.')
                conn.execute("UPDATE community_messages SET pinned_at=? WHERE id=?", (_now_iso(), msg_id))
                _audit(conn, ident['key'], 'pin_message', None, f"message {msg_id} in {m['channel']}")
            else:
                conn.execute("UPDATE community_messages SET pinned_at=NULL WHERE id=?", (msg_id,))
                _audit(conn, ident['key'], 'unpin_message', None, f"message {msg_id} in {m['channel']}")
            return {'ok': True, 'pinned': pin}
        return await _with_conn(request, go)

    # ---- per-channel notification mute ---------------------------------------
    @app.post('/api/community/mute')
    async def community_mute(request: Request):
        body = await _body(request)

        def go(conn, ident, row):
            chan, other, bad = _parse_channel(conn, ident, body.get('channel'))
            if bad:
                return bad
            if body.get('muted'):
                conn.execute("INSERT OR IGNORE INTO community_mutes (account_key, channel, created_at) VALUES (?,?,?)",
                             (ident['key'], chan, _now_iso()))
            else:
                conn.execute("DELETE FROM community_mutes WHERE account_key=? AND channel=?", (ident['key'], chan))
            return {'ok': True, 'muted': bool(body.get('muted'))}
        return await _with_conn(request, go)

    @app.post('/api/community/messages')
    async def community_send(request: Request):
        body = await _body(request)

        def go(conn, ident, row):
            chan, other, bad = _parse_channel(conn, ident, body.get('channel'))
            if bad:
                return bad
            now_m = time.monotonic()
            m_err = _muted(conn, ident)
            if m_err:
                return m_err
            last = _last_send.get(ident['key'])
            if last is not None and now_m - last < SEND_INTERVAL:
                return _err('Slow down a little.', 429)

            kind = str(body.get('kind', 'text'))
            if kind in ('gif', 'sticker'):
                url = normalize_gif_url(body.get('url'))
                if not url:
                    return _err('That GIF link is not allowed. Use a GIPHY or Tenor GIF link, or search for one.')
                payload = url
            elif kind == 'text':
                payload = _clean_text(body.get('text', ''))
                if not payload:
                    return _err('Write something first.')
                if len(payload) > MAX_TEXT:
                    return _err(f'Messages can be up to {MAX_TEXT} characters.')
            elif kind == 'upload':
                try:
                    upload_id = int(body.get('upload_id'))
                except (TypeError, ValueError):
                    return _err('Invalid attachment.')
                up = conn.execute("SELECT id, kind FROM community_uploads WHERE id=? AND uploader_key=?",
                                  (upload_id, ident['key'])).fetchone()
                if not up or not os.path.isfile(_upload_path(upload_id)):
                    return _err('That upload expired. Choose the file again.', 404)
                payload = f"{up['kind']}:{up['id']}"
                if conn.execute("SELECT 1 FROM community_messages WHERE kind='upload' AND body=?",
                                (payload,)).fetchone():
                    return _err('That attachment was already sent.')
            elif kind == 'item':
                item_id = str(body.get('item', ''))
                if not item_id:
                    return _err('Choose an item to share.')
                data = _load_stats(conn, ident['key'])
                if data is None:
                    return _err('You do not have a character yet.', 404)
                info = next((i for i in _inv_list(data.get('inventory')) if i['id'] == item_id), None)
                if not info:
                    return _err('You do not have that item. (Equipped items must be unequipped first.)')
                payload = json.dumps({'item_id': info['id'], 'name': info['name'],
                                      'rarity': info['rarity'], 'qty': info['qty']})
            else:
                return _err('Unknown message type.')

            reply_id = None
            raw_reply = body.get('reply_to')
            if raw_reply not in (None, '', 0):
                try:
                    reply_id = int(raw_reply)
                except (TypeError, ValueError):
                    return _err('Invalid reply.')
                parent = conn.execute(
                    "SELECT 1 FROM community_messages WHERE id=? AND channel=? AND deleted=0",
                    (reply_id, chan)).fetchone()
                if not parent:
                    return _err('The message you are replying to no longer exists.', 404)

            notice = None
            if other:
                mine_blocked, theirs = _blocks(conn, ident['key'])
                if other in mine_blocked:
                    return _err('You blocked this adventurer. Unblock them to send a message.')
                if other in theirs:
                    return _err('You cannot message this adventurer.', 403)
                prow = conn.execute("SELECT * FROM community_profiles WHERE account_key=?", (other,)).fetchone()
                eff = _effective(prow, time.time())
                if eff == 'dnd':
                    notice = f"{prow['name']} has Do Not Disturb on. They'll see your message later."
                elif eff == 'offline':
                    notice = f"{prow['name']} is offline. They'll see your message when they return."

            me_k = ident['key']
            dq = _recent.setdefault(me_k, deque())
            while dq and now_m - dq[0] > 60:
                dq.popleft()
            if len(dq) >= FLOOD_MAX:
                return _err('You are sending messages too fast. Wait a minute and try again.', 429)
            new_acct = _is_new(conn, me_k)
            if kind == 'text' and new_acct and LINK_RE.search(payload):
                return _err('New accounts cannot post links yet. Try again in a few days.', 403)
            if other and new_acct:
                known = conn.execute("SELECT 1 FROM community_messages WHERE sender_key=? AND channel=? LIMIT 1",
                                     (me_k, chan)).fetchone()
                started = conn.execute("SELECT COUNT(DISTINCT channel) FROM community_messages WHERE sender_key=? "
                                       "AND channel LIKE 'dm:%' AND created_at>=?", (me_k, _iso_ago(hours=24))).fetchone()[0]
                if not known and started >= NEW_DM_PEOPLE:
                    return _err(f'New accounts can start up to {NEW_DM_PEOPLE} new conversations per day.', 403)
            if kind == 'text':
                prev = _last_text.get(me_k)
                same = prev is not None and prev[0] == payload.lower() and now_m - prev[2] < 120
                repeats = prev[1] + 1 if same else 1
                if repeats >= DUP_MAX:
                    return _err('Please do not repeat the same message.', 429)
                _last_text[me_k] = (payload.lower(), repeats, now_m)
            dq.append(now_m)
            _last_send[ident['key']] = now_m
            _typing.get(chan, {}).pop(ident['key'], None)
            message = _post_message(conn, chan, ident, kind, payload, reply_id)
            return {'ok': True, 'message': message, 'notice': notice}
        return await _with_conn(request, go)

    @app.patch('/api/community/messages/{msg_id}')
    async def community_edit(msg_id: int, request: Request):
        body = await _body(request)

        def go(conn, ident, row):
            m = conn.execute("SELECT id, sender_key, kind, body FROM community_messages WHERE id=? AND deleted=0",
                             (msg_id,)).fetchone()
            if not m:
                return _err('Message not found.', 404)
            if m['sender_key'] != ident['key']:
                return _err('You can only edit your own messages.', 403)
            m_err = _muted(conn, ident)
            if m_err:
                return m_err
            if m['kind'] != 'text':
                return _err('Only text messages can be edited.')
            text = _clean_text(body.get('text', ''))
            if not text:
                return _err('A message cannot be empty. Delete it instead.')
            if LINK_RE.search(text) and _is_new(conn, ident['key']):
                return _err('New accounts cannot post links yet. Try again in a few days.', 403)
            if len(text) > MAX_TEXT:
                return _err(f'Messages can be up to {MAX_TEXT} characters.')
            conn.execute("INSERT INTO community_message_edits (message_id, old_body, edited_at, editor_key) "
                         "VALUES (?,?,?,?)", (msg_id, m['body'], _now_iso(), ident['key']))
            conn.execute("UPDATE community_messages SET body=?, edited_at=? WHERE id=?",
                         (text, _now_iso(), msg_id))
            r = conn.execute("SELECT m.*, p.avatar AS avatar FROM community_messages m "
                             "LEFT JOIN community_profiles p ON p.account_key=m.sender_key WHERE m.id=?",
                             (msg_id,)).fetchone()
            return {'ok': True, 'message': _serialize(conn, [r], ident['key'])[0]}
        return await _with_conn(request, go)

    @app.delete('/api/community/messages/{msg_id}')
    async def community_delete(msg_id: int, request: Request):
        def go(conn, ident, row):
            m = conn.execute("SELECT id, sender_key, kind, body FROM community_messages WHERE id=? AND deleted=0",
                             (msg_id,)).fetchone()
            if not m:
                return _err('Message not found.', 404)
            admin = _is_admin(conn, ident)
            if m['sender_key'] != ident['key'] and not admin:
                return _err('You can only delete your own messages.', 403)
            if m['kind'] == 'gift' and not admin:
                return _err('Gift messages are a record of the trade and can’t be deleted.', 403)
            if m['kind'] == 'text':      # keep what was said, privately, so moderation mistakes can be undone
                conn.execute("INSERT INTO community_message_edits (message_id, old_body, edited_at, editor_key) "
                             "VALUES (?,?,?,?)", (msg_id, m['body'], _now_iso(), ident['key']))
            if m['sender_key'] != ident['key']:
                _audit(conn, ident['key'], 'delete_message', m['sender_key'], f"message {msg_id} ({m['kind']})")
            conn.execute("UPDATE community_messages SET deleted=1, body='', pinned_at=NULL WHERE id=?", (msg_id,))
            conn.execute("DELETE FROM community_reactions WHERE message_id=?", (msg_id,))
            if m['kind'] == 'upload':
                _drop_upload_for_body(conn, m['body'])
            return {'ok': True}
        return await _with_conn(request, go)

    # ---- reactions ------------------------------------------------------------
    @app.post('/api/community/messages/{msg_id}/react')
    async def community_react(msg_id: int, request: Request):
        body = await _body(request)

        def go(conn, ident, row):
            emoji = str(body.get('emoji', ''))
            if emoji not in REACTIONS:
                return _err('That reaction is not available.')
            now_m = time.monotonic()
            last = _last_react.get(ident['key'])
            if last is not None and now_m - last < 0.25:
                return _err('Slow down a little.', 429)
            _last_react[ident['key']] = now_m

            m = conn.execute("SELECT id, channel FROM community_messages WHERE id=? AND deleted=0",
                             (msg_id,)).fetchone()
            if not m or not _can_access(ident, m['channel']):
                return _err('Message not found.', 404)
            if m['channel'].startswith('dm:'):
                other = next((k for k in m['channel'][3:].split('|') if k != ident['key']), None)
                mine_blocked, theirs = _blocks(conn, ident['key'])
                if other in mine_blocked or other in theirs:
                    return _err('You cannot react in this conversation.', 403)

            exists = conn.execute(
                "SELECT 1 FROM community_reactions WHERE message_id=? AND account_key=? AND emoji=?",
                (msg_id, ident['key'], emoji)).fetchone()
            if exists:
                conn.execute("DELETE FROM community_reactions WHERE message_id=? AND account_key=? AND emoji=?",
                             (msg_id, ident['key'], emoji))
            else:
                conn.execute("INSERT INTO community_reactions (message_id, account_key, emoji) VALUES (?,?,?)",
                             (msg_id, ident['key'], emoji))
            return {'ok': True, 'reactions': _reactions_for(conn, [msg_id], ident['key']).get(msg_id, [])}
        return await _with_conn(request, go)

    # ---- typing indicator -----------------------------------------------------
    @app.post('/api/community/typing')
    async def community_typing(request: Request):
        body = await _body(request)

        def go(conn, ident, row):
            chan, other, bad = _parse_channel(conn, ident, body.get('channel'))
            if bad:
                return bad
            if other:
                mine_blocked, theirs = _blocks(conn, ident['key'])
                if other in mine_blocked or other in theirs:
                    return {'ok': True}
            _typing.setdefault(chan, {})[ident['key']] = (ident['name'], time.monotonic())
            return {'ok': True}
        return await _with_conn(request, go)

    # ---- photo / video uploads ------------------------------------------------
    # The browser sends the raw file as the request body. It is streamed to a temp file on disk,
    # checked, fsynced, and only then atomically renamed into place, so a crash or a dropped
    # connection can never leave a half-written file that looks real.
    @app.post('/api/community/upload')
    async def community_upload(request: Request):
        ident = _ident(request)                       # check login BEFORE reading any bytes
        if not ident:
            return JSONResponse({'ok': False, 'error': 'unauthorized'}, status_code=401)
        if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/octet-stream':
            return _err('Bad request.', 415)
        now_m = time.monotonic()
        last = _last_upload.get(ident['key'])
        if last is not None and now_m - last < UPLOAD_INTERVAL:
            return _err('Slow down a little.', 429)
        _last_upload[ident['key']] = now_m

        too_big = _err(f'Files can be up to {MAX_UPLOAD_MB} MB.', 413)
        try:
            declared = int(request.headers.get('content-length') or 0)
        except ValueError:
            return _err('Bad request.')
        if declared > MAX_UPLOAD_BYTES:
            return too_big
        if UPLOAD_SLOTS.locked():
            return _err('The server is busy with other uploads. Try again in a moment.', 503)

        async with UPLOAD_SLOTS:
            try:
                os.makedirs(UPLOAD_DIR, exist_ok=True)
                free = shutil.disk_usage(UPLOAD_DIR).free
            except OSError:
                log.exception('[community] upload folder is not usable')
                return _err('Uploads are unavailable right now.', 500)
            if free - declared < MIN_FREE_BYTES:
                log.error('[community] refusing upload: only %d MB free', free // (1024 * 1024))
                return _err('The server is low on storage, so uploads are paused for now.', 507)

            tmp = os.path.join(UPLOAD_DIR, f'tmp_{secrets.token_hex(8)}.part')
            try:
                size, head = await _receive_to_file(request, tmp)
                await asyncio.to_thread(_fsync_path, tmp)
            except OverflowError:
                _silent_remove(tmp)
                return too_big
            except Exception:
                _silent_remove(tmp)
                log.warning('[community] upload interrupted', exc_info=True)
                return _err('The upload was interrupted. Please try again.')
            if size == 0:
                _silent_remove(tmp)
                return _err('That file is empty.')

            sniffed = _sniff_upload(head[:64])
            if not sniffed:
                _silent_remove(tmp)
                return _err('Use a JPEG, PNG, GIF, WebP, MP4, MOV or WebM file.')
            mime, kind = sniffed
            if kind == 'image' and not _dims_ok(_image_dims(head), MAX_IMAGE_SIDE, MAX_IMAGE_PIXELS):
                _silent_remove(tmp)
                return _err(f'That image is too large or could not be read (limit {MAX_IMAGE_SIDE} px per side).')

            if mime in ('image/jpeg', 'image/png', 'image/webp', 'video/mp4', 'video/quicktime'):   # strip GPS/camera metadata
                try:
                    await asyncio.to_thread(_strip_metadata, tmp, mime)
                    size = os.path.getsize(tmp)
                except Exception:
                    log.warning('[community] could not strip metadata', exc_info=True)

            def go(conn, ident, row):
                m_err = _muted(conn, ident)
                if m_err:
                    return m_err
                if _is_new(conn, ident['key']) and conn.execute(
                        "SELECT COUNT(*) FROM community_uploads WHERE uploader_key=? AND created_at>=?",
                        (ident['key'], _iso_ago(hours=24))).fetchone()[0] >= NEW_UPLOADS_PER_DAY:
                    return _err(f'New accounts can upload up to {NEW_UPLOADS_PER_DAY} files per day.', 403)
                _purge_orphan_uploads(conn)
                used = conn.execute("SELECT COALESCE(SUM(size),0) FROM community_uploads WHERE uploader_key=?",
                                    (ident['key'],)).fetchone()[0]
                if used + size > MAX_USER_STORAGE:
                    return _err(f'You have used your {MAX_USER_STORAGE // (1024 * 1024)} MB of upload space. '
                                'Delete some of your older photo or video messages first.', 507)
                cur = conn.execute(
                    "INSERT INTO community_uploads (uploader_key, mime, kind, size, created_at) VALUES (?,?,?,?,?)",
                    (ident['key'], mime, kind, size, _now_iso()))
                upload_id = cur.lastrowid
                os.replace(tmp, _upload_path(upload_id))          # atomic rename into place
                return {'ok': True, 'upload': {'id': upload_id, 'mime': mime, 'kind': kind, 'size': size}}

            result = await _with_conn(request, go)
            if isinstance(result, JSONResponse):                  # anything but success: leave no temp file behind
                _silent_remove(tmp)
            return result

    @app.get('/api/community/upload/{upload_id}/file')
    async def community_upload_file(upload_id: int, request: Request):
        ident = _ident(request)
        if not ident:
            return Response(status_code=401)
        conn = _DB()
        try:
            u = conn.execute("SELECT id, uploader_key, mime, kind FROM community_uploads WHERE id=?",
                             (upload_id,)).fetchone()
            if not u:
                return Response(status_code=404)
            m = conn.execute("SELECT channel FROM community_messages WHERE kind='upload' AND deleted=0 "
                             "AND body=? ORDER BY id DESC LIMIT 1", (f"{u['kind']}:{u['id']}",)).fetchone()
        finally:
            conn.close()
        if m:
            if not _can_access(ident, m['channel']):    # a sent upload follows the privacy of its channel
                return Response(status_code=404)
        elif u['uploader_key'] != ident['key']:
            return Response(status_code=404)
        path = _upload_path(upload_id)
        if not os.path.isfile(path):
            return Response(status_code=404)
        return await asyncio.to_thread(_file_response, path, u['mime'], request, {
            'Cache-Control': 'private, max-age=86400',
            'X-Content-Type-Options': 'nosniff',
            'Content-Security-Policy': "default-src 'none'; sandbox",
        })

    # ---- gifts: gold or an inventory item, sent from a direct message -----------
    @app.get('/api/community/gift/inventory')
    async def community_gift_inventory(request: Request):
        def go(conn, ident, row):
            data = _load_stats(conn, ident['key'])
            if data is None:
                return _err('You do not have a character yet.', 404)
            try:
                gold = max(0, int(data.get('gold', 0) or 0))
            except (TypeError, ValueError):
                gold = 0
            return {'ok': True, 'gold': gold, 'items': _inv_list(data.get('inventory')),
                    'max_gold': MAX_GIFT_GOLD, 'max_qty': MAX_GIFT_ITEM_QTY, 'max_note': MAX_GIFT_NOTE,
                    'limits': _gift_limits(conn, ident['key'])}
        return await _with_conn(request, go)

    @app.post('/api/community/gift')
    async def community_gift(request: Request):
        body = await _body(request)

        def go(conn, ident, row):
            # This whole function is one BEGIN IMMEDIATE transaction (see _exec): the stats it reads can't
            # be changed by the game while it works, and the gold/items, ledger entry and chat message are
            # saved together or not at all.
            now_m = time.monotonic()
            m_err = _muted(conn, ident)
            if m_err:
                return m_err
            last = _last_gift.get(ident['key'])
            if last is not None and now_m - last < GIFT_INTERVAL:
                return _err('Slow down a little.', 429)

            to = str(body.get('to', ''))
            if not KEY_RE.match(to) or to == ident['key']:
                return _err('Pick another adventurer to gift.', 404)
            if not conn.execute("SELECT 1 FROM community_profiles WHERE account_key=?", (to,)).fetchone():
                return _err('That adventurer has not joined the Community yet.', 404)
            mine_blocked, theirs = _blocks(conn, ident['key'])
            if to in mine_blocked:
                return _err('You blocked this adventurer. Unblock them to send a gift.')
            if to in theirs:
                return _err('You cannot gift this adventurer.', 403)

            sender = _load_stats(conn, ident['key'])
            receiver = _load_stats(conn, to)
            if sender is None:
                return _err('You do not have a character yet.', 404)
            if receiver is None:
                return _err('That adventurer does not have a character yet.', 404)

            note = _clean_text(body.get('note', ''))[:MAX_GIFT_NOTE]
            kind = str(body.get('kind', ''))
            lim = _gift_limits(conn, ident['key'])
            ledger = {'kind': kind, 'amount': None, 'item_id': None, 'item_name': None, 'qty': None}
            if kind == 'gold':
                raw = body.get('amount')
                if isinstance(raw, bool) or not isinstance(raw, (int, str)):
                    return _err('Enter a whole number of gold.')
                try:
                    amount = int(raw)
                except (TypeError, ValueError):
                    return _err('Enter a whole number of gold.')
                if amount < 1:
                    return _err('Enter at least 1 gold.')
                if amount > MAX_GIFT_GOLD:
                    return _err(f'You can gift up to {MAX_GIFT_GOLD:,} gold at a time.')
                have = int(sender.get('gold', 0) or 0)
                if have < amount:
                    return _err(f'You only have {have:,} gold.')
                if amount > lim['gold_left']:
                    return _err(f"Daily gift limit{' for new accounts' if lim['new'] else ''}: "
                                f"you can send {lim['gold_left']:,} more gold in the next 24 hours.", 403)
                if _is_new(conn, to) and _gift_received(conn, to)[0] + amount > NEW_DAILY_GOLD:
                    return _err('That adventurer is new and cannot receive that much gold today.', 403)
                sender['gold'] = have - amount
                receiver['gold'] = int(receiver.get('gold', 0) or 0) + amount
                gift = {'type': 'gold', 'amount': amount}
                log_detail = f'{amount} gold'
                ledger['amount'] = amount
            elif kind == 'item':
                item_id = str(body.get('item', ''))
                raw_q = body.get('quantity', 1)
                if isinstance(raw_q, bool):
                    return _err('Enter a whole number for the quantity.')
                try:
                    qty = int(raw_q)
                except (TypeError, ValueError):
                    return _err('Enter a whole number for the quantity.')
                if qty < 1 or qty > MAX_GIFT_ITEM_QTY:
                    return _err(f'You can gift between 1 and {MAX_GIFT_ITEM_QTY} at a time.')
                if qty > lim['items_left']:
                    return _err(f"Daily gift limit{' for new accounts' if lim['new'] else ''}: "
                                f"you can send {lim['items_left']} more items in the next 24 hours.", 403)
                if _is_new(conn, to) and _gift_received(conn, to)[1] + qty > NEW_DAILY_ITEMS:
                    return _err('That adventurer is new and cannot receive more items today.', 403)
                s_inv = sender.get('inventory')
                if not isinstance(s_inv, (dict, list)):
                    return _err('Your inventory is empty.')
                info = next((i for i in _inv_list(s_inv) if i['id'] == item_id), None)
                if not info:
                    return _err('You do not have that item to gift. (Equipped items must be unequipped first.)')
                template = _inv_take(s_inv, item_id, qty)
                if template is None:
                    return _err(f'You only have {info["qty"]} of that item.')
                r_inv = receiver.get('inventory')
                if not isinstance(r_inv, (dict, list)):
                    r_inv = {} if isinstance(s_inv, dict) else []
                    receiver['inventory'] = r_inv
                _inv_give(r_inv, item_id, qty, template)
                gift = {'type': 'item', 'name': info['name'], 'qty': qty, 'rarity': info['rarity']}
                log_detail = f'{qty}x {info["name"]}'
                ledger.update(item_id=item_id, item_name=info['name'], qty=qty)
            else:
                return _err('Choose gold or an item to gift.')

            if note:
                gift['note'] = note
            _save_stats(conn, ident['key'], sender)
            _save_stats(conn, to, receiver)
            _last_gift[ident['key']] = now_m
            message = _post_message(conn, _canon(ident['key'], to), ident, 'gift', json.dumps(gift))
            conn.execute(
                "INSERT INTO community_gift_log (created_at, from_key, to_key, kind, amount, item_id, item_name, qty, note, message_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (_now_iso(), ident['key'], to, ledger['kind'], ledger['amount'], ledger['item_id'],
                 ledger['item_name'], ledger['qty'], note or None, message['id']))
            try:
                conn.execute("INSERT INTO activity_log (user_id, action, detail) VALUES (?,?,?)",
                             (ident['uid'], 'gift_sent', f'{log_detail} to {to}'))
                conn.execute("INSERT INTO activity_log (user_id, action, detail) VALUES (?,?,?)",
                             (to.split(':', 1)[1], 'gift_received', f'{log_detail} from {ident["key"]}'))
            except sqlite3.Error:
                log.warning('[community] could not write gift activity log', exc_info=True)
            return {'ok': True, 'message': message, 'gold': int(sender.get('gold', 0) or 0)}
        return await _with_conn(request, go)

    # ---- custom emojis (static or animated) -----------------------------------
    @app.get('/api/community/emojis')
    async def community_emojis(request: Request):
        def go(conn, ident, row):
            admin = _is_admin(conn, ident)
            rows = conn.execute(
                "SELECT e.id, e.name, e.animated, e.owner_key, p.name AS owner_name FROM community_emojis e "
                "LEFT JOIN community_profiles p ON p.account_key=e.owner_key "
                "ORDER BY e.name COLLATE NOCASE LIMIT 500").fetchall()
            return {'ok': True,
                    'limit': MAX_EMOJIS_ADMIN if admin else MAX_EMOJIS_PER_USER,
                    'max_kb': MAX_EMOJI_BYTES // 1024,
                    'emojis': [{'id': r['id'], 'name': r['name'], 'animated': bool(r['animated']),
                                'mine': r['owner_key'] == ident['key'], 'owner': r['owner_name'] or 'Adventurer'}
                               for r in rows]}
        return await _with_conn(request, go)

    @app.post('/api/community/emojis')
    async def community_emoji_add(request: Request):
        body = await _body(request)

        def go(conn, ident, row):
            now_m = time.monotonic()
            m_err = _muted(conn, ident)
            if m_err:
                return m_err
            last = _last_emoji.get(ident['key'])
            if last is not None and now_m - last < 3.0:
                return _err('Slow down a little.', 429)

            name = str(body.get('name', '')).strip().strip(':').lower()
            if not EMOJI_NAME_RE.match(name):
                return _err('Names use 2 to 24 letters, numbers or underscores.')

            raw = str(body.get('data', ''))
            if ',' in raw[:100]:
                raw = raw.split(',', 1)[1]
            if len(raw) > MAX_EMOJI_BYTES * 4 // 3 + 16:
                return _err(f'Emoji files can be up to {MAX_EMOJI_BYTES // 1024} KB.')
            try:
                data = base64.b64decode(raw, validate=True)
            except Exception:
                return _err('That file could not be read.')
            if not data or len(data) > MAX_EMOJI_BYTES:
                return _err(f'Emoji files can be up to {MAX_EMOJI_BYTES // 1024} KB.')

            sniffed = _sniff_image(data)
            if not sniffed:
                return _err('Use a GIF, PNG or WebP image.')
            mime, animated = sniffed
            data = _clean_png(data) if mime == 'image/png' else _clean_webp(data) if mime == 'image/webp' else data
            # Size is read from the header, so this protects against "image bombs" even without Pillow.
            if not _dims_ok(_image_dims(data), MAX_EMOJI_PX, MAX_EMOJI_PX * MAX_EMOJI_PX):
                return _err(f'Emojis can be at most {MAX_EMOJI_PX}x{MAX_EMOJI_PX} pixels.')
            if Image is not None:
                try:
                    with Image.open(io.BytesIO(data)) as im:
                        im.verify()
                except Exception:
                    return _err('That file is not a valid image.')

            cap = MAX_EMOJIS_ADMIN if _is_admin(conn, ident) else MAX_EMOJIS_PER_USER
            owned = conn.execute("SELECT COUNT(*) FROM community_emojis WHERE owner_key=?", (ident['key'],)).fetchone()[0]
            if owned >= cap:
                return _err(f'You can have up to {cap} custom emojis. Delete one first.')
            if conn.execute("SELECT 1 FROM community_emojis WHERE name=?", (name,)).fetchone():
                return _err('That emoji name is already taken.', 409)

            _last_emoji[ident['key']] = now_m
            cur = conn.execute(
                "INSERT INTO community_emojis (name, owner_key, mime, data, animated, created_at) VALUES (?,?,?,?,?,?)",
                (name, ident['key'], mime, data, 1 if animated else 0, _now_iso()))
            return {'ok': True, 'emoji': {'id': cur.lastrowid, 'name': name, 'animated': animated, 'mine': True}}
        return await _with_conn(request, go)

    @app.delete('/api/community/emojis/{emoji_id}')
    async def community_emoji_delete(emoji_id: int, request: Request):
        def go(conn, ident, row):
            e = conn.execute("SELECT id, owner_key FROM community_emojis WHERE id=?", (emoji_id,)).fetchone()
            if not e:
                return _err('Emoji not found.', 404)
            if e['owner_key'] != ident['key'] and not _is_admin(conn, ident):
                return _err('You can only delete your own emojis.', 403)
            conn.execute("DELETE FROM community_emojis WHERE id=?", (emoji_id,))
            return {'ok': True}
        return await _with_conn(request, go)

    @app.get('/api/community/emojis/{emoji_id}/img')
    async def community_emoji_img(emoji_id: int, request: Request):
        if not _ident(request):
            return Response(status_code=401)
        conn = _DB()
        try:
            r = conn.execute("SELECT mime, data FROM community_emojis WHERE id=?", (emoji_id,)).fetchone()
        finally:
            conn.close()
        if not r:
            return Response(status_code=404)
        return Response(content=bytes(r['data']), media_type=r['mime'], headers={
            'Cache-Control': 'private, max-age=86400',
            'X-Content-Type-Options': 'nosniff',
            'Content-Security-Policy': "default-src 'none'; sandbox",
        })

    # ---- block / unblock ------------------------------------------------------
    @app.post('/api/community/block')
    async def community_block(request: Request):
        body = await _body(request)

        def go(conn, ident, row):
            key = str(body.get('key', ''))
            if not KEY_RE.match(key) or key == ident['key']:
                return _err('Unknown adventurer.', 404)
            if body.get('blocked'):
                conn.execute("INSERT OR IGNORE INTO community_blocks (blocker_key, blocked_key) VALUES (?,?)", (ident['key'], key))
            else:
                conn.execute("DELETE FROM community_blocks WHERE blocker_key=? AND blocked_key=?", (ident['key'], key))
            return {'ok': True, 'blocked': bool(body.get('blocked'))}
        return await _with_conn(request, go)

    # ---- unread badge for the rest of the site (read-only, does not change presence) ----
    @app.get('/api/community/unread')
    async def community_unread(request: Request):
        ident = _ident(request)
        if not ident:
            return JSONResponse({'ok': False, 'error': 'unauthorized'}, status_code=401)

        def q():
            conn = _DB()
            try:
                mine = ident['key']
                return conn.execute(
                    "SELECT COUNT(*) FROM community_messages m LEFT JOIN community_reads r "
                    "ON r.account_key=? AND r.channel=m.channel "
                    "WHERE m.channel LIKE 'dm:%' AND (m.channel LIKE ? OR m.channel LIKE ?) "
                    "AND m.sender_key != ? AND m.deleted=0 AND m.id > COALESCE(r.last_id,0)",
                    (mine, f'dm:{mine}|%', f'dm:%|{mine}', mine)).fetchone()[0]
            finally:
                conn.close()
        return {'ok': True, 'unread_total': await asyncio.to_thread(q)}

    # ---- search ---------------------------------------------------------------
    @app.get('/api/community/search')
    async def community_search(request: Request, q: str = '', channel: str = 'lounge'):
        def go(conn, ident, row):
            chan, other, bad = _parse_channel(conn, ident, channel)
            if bad:
                return bad
            now_m = time.monotonic()
            last = _last_search.get(ident['key'])
            if last is not None and now_m - last < 0.4:
                return _err('Searching too fast.', 429)
            _last_search[ident['key']] = now_m
            term = CTRL_RE.sub('', q or '').strip().lower()[:60]
            if len(term) < 2:
                return {'ok': True, 'results': []}
            like = '%' + term.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
            rows = conn.execute(
                "SELECT m.*, p.avatar AS avatar FROM community_messages m "
                "LEFT JOIN community_profiles p ON p.account_key=m.sender_key "
                "WHERE m.channel=? AND m.deleted=0 AND m.kind='text' AND lower(m.body) LIKE ? ESCAPE '\\' "
                "ORDER BY m.id DESC LIMIT 40", (chan, like)).fetchall()
            if chan == 'lounge' and len(rows) < 40:
                rows = rows + conn.execute(
                    "SELECT a.*, p.avatar AS avatar FROM community_messages_archive a "
                    "LEFT JOIN community_profiles p ON p.account_key=a.sender_key "
                    "WHERE a.channel='lounge' AND a.deleted=0 AND a.kind='text' AND lower(a.body) LIKE ? ESCAPE '\\' "
                    "ORDER BY a.id DESC LIMIT ?", (like, 40 - len(rows))).fetchall()
            mine_blocked, _t = _blocks(conn, ident['key'])
            return {'ok': True, 'results': [m for m in _serialize(conn, rows, ident['key'])
                                            if m['sender'] not in mine_blocked]}
        return await _with_conn(request, go)

    # ---- gift history (your own sent and received gifts) ------------------------
    _GIFT_SELECT = ("SELECT g.*, pf.name AS from_name, pt.name AS to_name FROM community_gift_log g "
                    "LEFT JOIN community_profiles pf ON pf.account_key=g.from_key "
                    "LEFT JOIN community_profiles pt ON pt.account_key=g.to_key ")

    @app.get('/api/community/gift/history')
    async def community_gift_history(request: Request):
        def go(conn, ident, row):
            me_k = ident['key']
            rows = conn.execute(_GIFT_SELECT + "WHERE g.from_key=? OR g.to_key=? ORDER BY g.id DESC LIMIT 100",
                                (me_k, me_k)).fetchall()
            out = []
            for g in rows:
                sent = g['from_key'] == me_k
                out.append({'id': g['id'], 'ts': g['created_at'], 'direction': 'sent' if sent else 'received',
                            'other_name': (g['to_name'] if sent else g['from_name']) or 'Adventurer',
                            'kind': g['kind'], 'amount': g['amount'], 'item_name': g['item_name'],
                            'qty': g['qty'], 'note': g['note'] or ''})
            return {'ok': True, 'gifts': out}
        return await _with_conn(request, go)

    # ---- reports ----------------------------------------------------------------
    @app.post('/api/community/messages/{msg_id}/report')
    async def community_report(msg_id: int, request: Request):
        body = await _body(request)

        def go(conn, ident, row):
            m = conn.execute("SELECT id, channel, sender_key, sender_name, kind, body FROM community_messages "
                             "WHERE id=? AND deleted=0", (msg_id,)).fetchone()
            if not m or not _can_access(ident, m['channel']):
                return _err('Message not found.', 404)
            if m['sender_key'] == ident['key']:
                return _err('You cannot report your own message.')
            hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
            n = conn.execute("SELECT COUNT(*) FROM community_reports WHERE reporter_key=? AND created_at>=?",
                             (ident['key'], hour_ago)).fetchone()[0]
            if n >= REPORTS_PER_HOUR:
                return _err('You have sent a lot of reports. Please wait a while.', 429)
            snap = m['body'] if m['kind'] == 'text' else '[' + _preview_text(m['kind'], m['body']) + ']'
            conn.execute(
                "INSERT OR IGNORE INTO community_reports (message_id, reporter_key, reporter_name, sender_key, "
                "sender_name, channel, kind, snapshot, reason, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (msg_id, ident['key'], ident['name'], m['sender_key'], m['sender_name'], m['channel'],
                 m['kind'], snap, _clean_text(body.get('reason', ''))[:200], _now_iso()))
            n_rep = conn.execute(
                "SELECT COUNT(DISTINCT r.reporter_key) FROM community_reports r "
                "JOIN community_profiles p ON p.account_key=r.reporter_key "
                "WHERE r.sender_key=? AND r.created_at>=? AND p.created_at<=?",
                (m['sender_key'], _iso_ago(hours=24), _iso_ago(days=NEW_ACCOUNT_DAYS))).fetchone()[0]
            if n_rep >= AUTO_MUTE_REPORTS and not _sanction(conn, m['sender_key']):
                conn.execute("INSERT OR REPLACE INTO community_sanctions (account_key, kind, until, reason, by_key, created_at) "
                             "VALUES (?,?,?,?,?,?)",
                             (m['sender_key'], 'mute', time.time() + AUTO_MUTE_HOURS * 3600,
                              'Auto-muted after several reports. A moderator will review.', 'system', _now_iso()))
                _audit(conn, 'system', 'auto_mute', m['sender_key'], f'{n_rep} different reporters')
            return {'ok': True}
        return await _with_conn(request, go)

    # ---- moderation (admins only) -------------------------------------------------
    @app.get('/api/community/admin/reports')
    async def community_admin_reports(request: Request, status: str = 'open'):
        def go(conn, ident, row):
            deny = _admin_only(conn, ident)
            if deny:
                return deny
            st = 'resolved' if status == 'resolved' else 'open'
            rows = conn.execute("SELECT * FROM community_reports WHERE status=? ORDER BY id DESC LIMIT 100",
                                (st,)).fetchall()
            return {'ok': True, 'reports': [
                {'id': r['id'], 'message_id': r['message_id'], 'reporter_name': r['reporter_name'] or 'Adventurer',
                 'sender_key': r['sender_key'], 'sender_name': r['sender_name'] or 'Adventurer',
                 'dm': r['channel'].startswith('dm:'), 'snapshot': r['snapshot'], 'reason': r['reason'] or '',
                 'ts': r['created_at']} for r in rows]}
        return await _with_conn(request, go)

    @app.post('/api/community/admin/reports/{report_id}/resolve')
    async def community_admin_resolve(report_id: int, request: Request):
        def go(conn, ident, row):
            deny = _admin_only(conn, ident)
            if deny:
                return deny
            _audit(conn, ident['key'], 'resolve_report', None, f'report {report_id}')
            conn.execute("UPDATE community_reports SET status='resolved', resolved_by=?, resolved_at=? WHERE id=?",
                         (ident['key'], _now_iso(), report_id))
            return {'ok': True}
        return await _with_conn(request, go)

    @app.get('/api/community/admin/history/{msg_id}')
    async def community_admin_history(msg_id: int, request: Request):
        def go(conn, ident, row):
            deny = _admin_only(conn, ident)
            if deny:
                return deny
            m = (conn.execute("SELECT body, deleted FROM community_messages WHERE id=?", (msg_id,)).fetchone() or
                 conn.execute("SELECT body, deleted FROM community_messages_archive WHERE id=?", (msg_id,)).fetchone())
            edits = conn.execute(
                "SELECT e.old_body, e.edited_at, p.name AS editor FROM community_message_edits e "
                "LEFT JOIN community_profiles p ON p.account_key=e.editor_key WHERE e.message_id=? ORDER BY e.id DESC",
                (msg_id,)).fetchall()
            return {'ok': True, 'current': m['body'] if m else '', 'deleted': bool(m and m['deleted']),
                    'edits': [{'old_body': e['old_body'], 'ts': e['edited_at'], 'editor': e['editor'] or 'Adventurer'}
                              for e in edits]}
        return await _with_conn(request, go)

    @app.get('/api/community/admin/gifts')
    async def community_admin_gifts(request: Request, key: str = ''):
        def go(conn, ident, row):
            deny = _admin_only(conn, ident)
            if deny:
                return deny
            if key and KEY_RE.match(key):
                rows = conn.execute(_GIFT_SELECT + "WHERE g.from_key=? OR g.to_key=? ORDER BY g.id DESC LIMIT 200",
                                    (key, key)).fetchall()
            else:
                rows = conn.execute(_GIFT_SELECT + "ORDER BY g.id DESC LIMIT 200").fetchall()
            return {'ok': True, 'gifts': [
                {'id': g['id'], 'ts': g['created_at'], 'from_name': g['from_name'] or g['from_key'],
                 'to_name': g['to_name'] or g['to_key'], 'amount': g['amount'], 'item_name': g['item_name'],
                 'qty': g['qty'], 'note': g['note'] or ''} for g in rows]}
        return await _with_conn(request, go)

    @app.post('/api/community/admin/sanction')
    async def community_admin_sanction(request: Request):
        body = await _body(request)

        def go(conn, ident, row):
            deny = _admin_only(conn, ident)
            if deny:
                return deny
            key = str(body.get('key', ''))
            kind = str(body.get('kind', ''))
            if not KEY_RE.match(key) or key == ident['key']:
                return _err('Unknown adventurer.', 404)
            if kind == 'clear':
                conn.execute("DELETE FROM community_sanctions WHERE account_key=?", (key,))
                _audit(conn, ident['key'], 'lift_sanction', key)
                return {'ok': True}
            if kind not in ('mute', 'ban'):
                return _err('Choose mute, ban or lift.')
            try:
                hours = int(body.get('hours') or 0)
            except (TypeError, ValueError):
                return _err('Hours must be a whole number.')
            if hours < 0 or hours > 8760:
                return _err('Hours must be between 1 and 8760 (one year). Leave empty for a permanent ban.')
            if kind == 'mute' and hours == 0:
                hours = 24
            _audit(conn, ident['key'], kind, key,
                   (f'{hours}h' if hours else 'permanent') + ': ' + _clean_text(body.get('reason', ''))[:150])
            conn.execute("INSERT OR REPLACE INTO community_sanctions (account_key, kind, until, reason, by_key, created_at) "
                         "VALUES (?,?,?,?,?,?)",
                         (key, kind, time.time() + hours * 3600 if hours else 0,
                          _clean_text(body.get('reason', ''))[:200], ident['key'], _now_iso()))
            return {'ok': True}
        return await _with_conn(request, go)

    @app.get('/api/community/admin/log')
    async def community_admin_log(request: Request):
        def go(conn, ident, row):
            deny = _admin_only(conn, ident)
            if deny:
                return deny
            rows = conn.execute(
                "SELECT l.*, pa.name AS admin_name, pt.name AS target_name FROM community_admin_log l "
                "LEFT JOIN community_profiles pa ON pa.account_key=l.admin_key "
                "LEFT JOIN community_profiles pt ON pt.account_key=l.target_key ORDER BY l.id DESC LIMIT 200").fetchall()
            return {'ok': True, 'log': [
                {'ts': r['created_at'], 'admin': r['admin_name'] or r['admin_key'], 'action': r['action'],
                 'target': r['target_name'] or r['target_key'] or '', 'detail': r['detail'] or ''} for r in rows]}
        return await _with_conn(request, go)

    # ---- unread badge script for the site navigation (include it from your layout, see the docs) ----
    BADGE_JS = """(function () {
    if (document.body && document.body.dataset.page === 'community') return;
    var SEL = 'a[href="/community"], a[href="/community.html"]';
    function paint(n) {
        document.querySelectorAll(SEL).forEach(function (a) {
            var b = a.querySelector('.cm-nav-badge');
            if (!n) { if (b) b.remove(); return; }
            if (!b) {
                b = document.createElement('span');
                b.className = 'cm-nav-badge';
                b.style.cssText = 'margin-left:8px;min-width:18px;height:18px;padding:0 6px;border-radius:99px;font-size:.7rem;font-weight:700;display:inline-grid;place-items:center;background:#ef4444;color:#fff';
                a.appendChild(b);
            }
            b.textContent = n > 99 ? '99+' : String(n);
            b.setAttribute('aria-label', n + ' unread Community messages');
        });
    }
    function poll() {
        if (document.hidden) return;
        fetch('/api/community/unread', { credentials: 'same-origin' })
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (d) { if (d) paint(d.unread_total || 0); })
            .catch(function () {});
    }
    setTimeout(poll, 1500);
    setInterval(poll, 45000);
    document.addEventListener('visibilitychange', function () { if (!document.hidden) poll(); });
})();
"""

    @app.get('/api/community/badge.js')
    async def community_badge_js():
        return Response(content=BADGE_JS, media_type='application/javascript',
                        headers={'Cache-Control': 'public, max-age=3600', 'X-Content-Type-Options': 'nosniff'})

    # ---- GIF / sticker search (server-side proxy so the API key never reaches the browser) ----
    def _fetch_giphy(q, sticker):
        import requests
        base = 'stickers' if sticker else 'gifs'
        endpoint = 'search' if q else 'trending'
        params = {'api_key': GIPHY_KEY, 'limit': 24, 'rating': 'pg-13'}
        if q:
            params['q'] = q
        r = requests.get(f'https://api.giphy.com/v1/{base}/{endpoint}', params=params, timeout=6)
        r.raise_for_status()
        out = []
        for g in (r.json().get('data') or []):
            imgs = g.get('images') or {}
            full = normalize_gif_url((imgs.get('downsized') or imgs.get('original') or {}).get('url', ''))
            prev = normalize_gif_url((imgs.get('fixed_height_small') or imgs.get('fixed_height') or {}).get('url', ''))
            if full and prev:
                out.append({'id': g.get('id'), 'url': full, 'preview': prev, 'title': str(g.get('title') or '')[:80]})
        return out

    @app.get('/api/community/gifs')
    async def community_gifs(request: Request, q: str = '', type: str = 'gif'):
        ident = _ident(request)
        if not ident:
            return JSONResponse({'ok': False, 'error': 'unauthorized'}, status_code=401)
        if not GIPHY_KEY:
            return {'ok': True, 'enabled': False, 'gifs': []}
        sticker = type == 'sticker'
        now = time.monotonic()
        last = _last_gif_search.get(ident['key'])
        if last is not None and now - last < 0.5:
            return _err('Searching too fast.', 429)
        _last_gif_search[ident['key']] = now
        q = CTRL_RE.sub('', q or '').strip().lower()[:50]
        cache_key = (sticker, q)
        hit = _gif_cache.get(cache_key)
        if hit and time.time() - hit[0] < 120:
            return {'ok': True, 'enabled': True, 'gifs': hit[1]}
        try:
            gifs = await asyncio.to_thread(_fetch_giphy, q, sticker)
        except Exception:
            log.exception('[community] GIPHY request failed')
            return _err('GIF search is unavailable right now.', 502)
        if len(_gif_cache) > 200:
            _gif_cache.clear()
        _gif_cache[cache_key] = (time.time(), gifs)
        return {'ok': True, 'enabled': True, 'gifs': gifs}


def _silent_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass