import asyncio
import json
import logging
import re
import secrets
import threading
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, urlunparse

from fastapi import Request
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)

STATUSES = ('online', 'idle', 'dnd', 'offline')
PRESENCE_TIMEOUT = 90
IDLE_AFTER = 300
MAX_TEXT = 500
MAX_STATUS_TEXT = 60
KEEP_PER_CHANNEL = 500
SEND_INTERVAL = 1.0
TYPING_TTL = 5.0
KEY_RE = re.compile(r'^(email|discord):[0-9]{1,20}$')

DEFAULT_GUILD_ID = 1
DEFAULT_GUILD_NAME = 'Lounge'
MAX_GUILDS_OWNED = 5
MAX_GUILDS_JOINED = 25
MAX_GUILD_MEMBERS = 500
MAX_GIFT_QTY = 999
MAX_GIFT_NOTE = 100
REACTIONS = ('👍', '❤️', '😂', '😮', '😢', '🔥', '🎉', '⚔️')

GIF_HOSTS = {
    'media.giphy.com', 'media0.giphy.com', 'media1.giphy.com', 'media2.giphy.com',
    'media3.giphy.com', 'media4.giphy.com', 'i.giphy.com', 'media.tenor.com', 'c.tenor.com',
}
GIF_EXT = ('.gif', '.webp')
GIPHY_PAGE_RE = re.compile(r'^https://giphy\.com/(?:gifs|stickers)/(?:[^/?#]*-)?([A-Za-z0-9]{5,30})/?(?:[?#].*)?$')
CTRL_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
INVITE_RE = re.compile(r'^[A-Za-z0-9_-]{6,32}$')

_lock = threading.Lock()
_DB = None
_last_send = {}
_last_gif_search = {}
_last_gift = {}
_gif_cache = {}
_typing = {}   # channel -> {key: (name, monotonic)}


def _now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def normalize_gif_url(url):
    """Return a safe https GIF/sticker URL or None. Only allowlisted hosts are accepted."""
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


def _clean(text, limit):
    return CTRL_RE.sub('', str(text or '')).strip()[:limit]


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


# ───────────────────────────── inventory adapter (gifts)
class SqlInventory:
    """Reference inventory adapter for a table with one row per (owner, item) and a quantity column.

    Example:  SqlInventory('inventory', owner_col='user_id', item_col='item_id',
                           name_col='item_name', qty_col='quantity',
                           owner_fn=lambda key: key.split(':', 1)[1])
    owner_fn maps a community account key ('email:12' / 'discord:34') to the value stored in owner_col.
    Table/column names are developer-supplied constants, never user input.
    """

    def __init__(self, table, owner_col, item_col, name_col, qty_col, owner_fn=None, extra_cols=()):
        for ident in (table, owner_col, item_col, name_col, qty_col, *extra_cols):
            if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', ident):
                raise ValueError(f'bad identifier: {ident}')
        self.t, self.o, self.i, self.n, self.q = table, owner_col, item_col, name_col, qty_col
        self.owner_fn = owner_fn or (lambda key: key)

    def list_items(self, conn, key):
        rows = conn.execute(
            f"SELECT {self.i} AS item_id, {self.n} AS name, {self.q} AS qty FROM {self.t} "
            f"WHERE {self.o}=? AND {self.q}>0 ORDER BY {self.n}", (self.owner_fn(key),)).fetchall()
        return [{'item_id': str(r['item_id']), 'name': str(r['name']), 'qty': int(r['qty'])} for r in rows]

    def transfer(self, conn, from_key, to_key, item_id, qty):
        """Move qty of item_id between players. Runs inside the caller's transaction. Returns item name."""
        src, dst = self.owner_fn(from_key), self.owner_fn(to_key)
        cur = conn.execute(
            f"UPDATE {self.t} SET {self.q}={self.q}-? WHERE {self.o}=? AND {self.i}=? AND {self.q}>=?",
            (qty, src, item_id, qty))
        if cur.rowcount != 1:
            raise ValueError("You don't have enough of that item.")
        row = conn.execute(f"SELECT {self.n} AS name FROM {self.t} WHERE {self.o}=? AND {self.i}=?",
                           (src, item_id)).fetchone()
        name = str(row['name'])
        have = conn.execute(f"SELECT 1 FROM {self.t} WHERE {self.o}=? AND {self.i}=?", (dst, item_id)).fetchone()
        if have:
            conn.execute(f"UPDATE {self.t} SET {self.q}={self.q}+? WHERE {self.o}=? AND {self.i}=?", (qty, dst, item_id))
        else:
            conn.execute(f"INSERT INTO {self.t} ({self.o}, {self.i}, {self.n}, {self.q}) VALUES (?,?,?,?)",
                         (dst, item_id, name, qty))
        conn.execute(f"DELETE FROM {self.t} WHERE {self.o}=? AND {self.i}=? AND {self.q}<=0", (src, item_id))
        return name


# ───────────────────────────── schema
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
        if 'edited' not in cols:
            conn.execute("ALTER TABLE community_messages ADD COLUMN edited INTEGER NOT NULL DEFAULT 0")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cm_channel ON community_messages(channel, id)")
        conn.execute("""CREATE TABLE IF NOT EXISTS community_reads (
            account_key TEXT NOT NULL, channel TEXT NOT NULL, last_id INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (account_key, channel))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS community_blocks (
            blocker_key TEXT NOT NULL, blocked_key TEXT NOT NULL, PRIMARY KEY (blocker_key, blocked_key))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS community_reactions (
            message_id INTEGER NOT NULL, account_key TEXT NOT NULL, emoji TEXT NOT NULL,
            PRIMARY KEY (message_id, account_key, emoji))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS community_guilds (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, owner_key TEXT,
            is_default INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS community_guild_members (
            guild_id INTEGER NOT NULL, account_key TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'member',
            joined_at TEXT NOT NULL, PRIMARY KEY (guild_id, account_key))""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cgm_key ON community_guild_members(account_key)")
        conn.execute("""CREATE TABLE IF NOT EXISTS community_invites (
            code TEXT PRIMARY KEY, guild_id INTEGER NOT NULL, created_by TEXT NOT NULL,
            created_at TEXT NOT NULL, expires_at TEXT, max_uses INTEGER NOT NULL DEFAULT 0,
            uses INTEGER NOT NULL DEFAULT 0, revoked INTEGER NOT NULL DEFAULT 0)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS community_gifts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, from_key TEXT NOT NULL, to_key TEXT NOT NULL,
            item_id TEXT NOT NULL, item_name TEXT NOT NULL, qty INTEGER NOT NULL,
            note TEXT, created_at TEXT NOT NULL)""")
        # the default guild always has id 1 and uses the legacy 'lounge' channel
        conn.execute("INSERT OR IGNORE INTO community_guilds (id, name, owner_key, is_default, created_at) "
                     "VALUES (?,?,NULL,1,?)", (DEFAULT_GUILD_ID, DEFAULT_GUILD_NAME, _now_iso()))
        conn.commit()
    finally:
        conn.close()


def _touch(conn, ident, active=True):
    """Create/refresh the caller's profile and make sure they belong to the default guild."""
    now = time.time()
    conn.execute(
        "INSERT INTO community_profiles (account_key, name, avatar, status, last_seen, last_active, created_at) "
        "VALUES (?,?,?, 'online', ?, ?, ?) "
        "ON CONFLICT(account_key) DO UPDATE SET name=excluded.name, "
        "avatar=CASE WHEN excluded.avatar != '' THEN excluded.avatar ELSE community_profiles.avatar END, "
        "last_seen=excluded.last_seen, "
        "last_active=CASE WHEN ? THEN excluded.last_active ELSE community_profiles.last_active END",
        (ident['key'], ident['name'], ident['avatar'], now, now, _now_iso(), 1 if active else 0))
    conn.execute("INSERT OR IGNORE INTO community_guild_members (guild_id, account_key, role, joined_at) "
                 "VALUES (?,?, 'member', ?)", (DEFAULT_GUILD_ID, ident['key'], _now_iso()))
    return conn.execute("SELECT * FROM community_profiles WHERE account_key=?", (ident['key'],)).fetchone()


def _member(row, now, unread=0, blocked=False):
    eff = _effective(row, now)
    return {'key': row['account_key'], 'name': row['name'] or 'Adventurer',
            'avatar': _safe_avatar(row['avatar']), 'status': eff,
            'status_text': (row['status_text'] or '') if eff != 'offline' else '',
            'unread': unread, 'blocked': blocked}


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


# ───────────────────────────── channels & guilds
def _guild_channel(gid):
    return 'lounge' if gid == DEFAULT_GUILD_ID else f'guild:{gid}'


def _guild_id_of(chan):
    if chan == 'lounge':
        return DEFAULT_GUILD_ID
    if chan.startswith('guild:'):
        try:
            return int(chan[6:])
        except ValueError:
            return None
    return None


def _guild_role(conn, gid, key):
    r = conn.execute("SELECT role FROM community_guild_members WHERE guild_id=? AND account_key=?", (gid, key)).fetchone()
    return r['role'] if r else None


def _parse_channel(conn, ident, raw):
    """'lounge' | 'guild:<id>' | 'dm:<otherKey>' -> (canonical_channel, other_key|None, error_response|None)."""
    raw = str(raw or 'lounge')
    if raw == 'lounge':
        return 'lounge', None, None
    if raw.startswith('guild:'):
        gid = _guild_id_of(raw)
        if gid is None or gid == DEFAULT_GUILD_ID or _guild_role(conn, gid, ident['key']) is None:
            return None, None, _err('You are not a member of that group.', 404)
        return f'guild:{gid}', None, None
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


def _guild_json(conn, g, key):
    n = conn.execute("SELECT COUNT(*) FROM community_guild_members WHERE guild_id=?", (g['id'],)).fetchone()[0]
    return {'id': g['id'], 'name': g['name'], 'channel': _guild_channel(g['id']), 'is_default': bool(g['is_default']),
            'members': n, 'role': _guild_role(conn, g['id'], key) or 'member'}


def _can_moderate(conn, ident, chan):
    if _is_admin(conn, ident):
        return True
    gid = _guild_id_of(chan)
    return gid is not None and _guild_role(conn, gid, ident['key']) in ('owner', 'admin')


# ───────────────────────────── message serialisation
def _decorate(conn, rows, me_key):
    """Turn message rows (joined with avatar) into JSON incl. reactions and the replied-to snippet."""
    rows = list(rows)
    if not rows:
        return []
    ids = [r['id'] for r in rows]
    marks = ','.join('?' * len(ids))
    react = {}
    for r in conn.execute(f"SELECT message_id, emoji, account_key FROM community_reactions WHERE message_id IN ({marks})", ids):
        d = react.setdefault(r['message_id'], {})
        e = d.setdefault(r['emoji'], {'emoji': r['emoji'], 'count': 0, 'mine': False})
        e['count'] += 1
        if r['account_key'] == me_key:
            e['mine'] = True
    reply_ids = [r['reply_to'] for r in rows if r['reply_to']]
    replies = {}
    if reply_ids:
        m2 = ','.join('?' * len(reply_ids))
        for r in conn.execute(f"SELECT id, sender_name, kind, body, deleted FROM community_messages WHERE id IN ({m2})", reply_ids):
            if r['deleted']:
                snippet = 'Original message was deleted'
            elif r['kind'] == 'text':
                snippet = r['body'][:90]
            elif r['kind'] == 'gift':
                snippet = '🎁 Gift'
            else:
                snippet = '[GIF]' if r['kind'] == 'gif' else '[Sticker]'
            replies[r['id']] = {'id': r['id'], 'name': r['sender_name'] or 'Adventurer', 'text': snippet}
    out = []
    for r in rows:
        out.append({
            'id': r['id'], 'sender': r['sender_key'], 'name': r['sender_name'] or 'Adventurer',
            'avatar': _safe_avatar(r['avatar']) if 'avatar' in r.keys() else '',
            'kind': r['kind'], 'body': r['body'], 'ts': r['created_at'], 'mine': r['sender_key'] == me_key,
            'edited': bool(r['edited']), 'reply': replies.get(r['reply_to']) if r['reply_to'] else None,
            'reactions': sorted(react.get(r['id'], {}).values(), key=lambda e: -e['count'])})
    return out


_MSG_SELECT = ("SELECT m.*, p.avatar AS avatar FROM community_messages m "
               "LEFT JOIN community_profiles p ON p.account_key=m.sender_key ")


# ───────────────────────────── routes
def register_community_routes(app, db_factory, inventory=None, friends_check=None, giphy_key=''):
    """
    inventory:     object with list_items(conn, key) and transfer(conn, from_key, to_key, item_id, qty) (see SqlInventory).
                   If None, the gift endpoints answer 501 and the Gift button explains it is not set up.
    friends_check: optional callable (conn, key_a, key_b) -> bool. If None, any community member can receive gifts.
    giphy_key:     GIPHY API key (enables GIF + sticker search).
    """
    global _DB
    _DB = db_factory
    try:
        _init_tables()
    except Exception:
        log.exception('[community] could not create tables')

    GIPHY_KEY = giphy_key or ''

    def _with_conn(request, fn):
        ident = _ident(request)
        if not ident:
            return JSONResponse({'ok': False, 'error': 'unauthorized'}, status_code=401)
        conn = _DB()
        try:
            with _lock:
                row = _touch(conn, ident, active=False)
                result = fn(conn, ident, row)
                if isinstance(result, JSONResponse) and result.status_code >= 400:
                    conn.rollback()
                else:
                    conn.commit()
            return result
        except Exception:
            log.exception('[community] request failed')
            try:
                conn.rollback()
            except Exception:
                pass
            return _err('Server error. Please try again.', 500)
        finally:
            conn.close()

    async def _body(request):
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
            me = _member(row, now)
            me['chosen_status'] = row['status']
            me['status_text'] = row['status_text'] or ''
            me['key'] = ident['key']
            return {'ok': True, 'me': me, 'is_admin': _is_admin(conn, ident),
                    'gif_search': bool(GIPHY_KEY), 'gifts': inventory is not None,
                    'reactions': list(REACTIONS),
                    'limits': {'text': MAX_TEXT, 'status_text': MAX_STATUS_TEXT}}
        return _with_conn(request, go)

    @app.post('/api/community/status')
    async def community_status(request: Request):
        body = await _body(request)

        def go(conn, ident, row):
            status = str(body.get('status', '')).lower()
            if status not in STATUSES:
                return _err('Pick Online, Idle, Do Not Disturb or Offline.')
            text = _clean(body.get('text'), MAX_STATUS_TEXT)
            now = time.time()
            conn.execute("UPDATE community_profiles SET status=?, status_text=?, last_seen=?, last_active=? "
                         "WHERE account_key=?", (status, text, now, now, ident['key']))
            row = conn.execute("SELECT * FROM community_profiles WHERE account_key=?", (ident['key'],)).fetchone()
            me = _member(row, now)
            me['chosen_status'] = row['status']
            me['status_text'] = row['status_text']
            return {'ok': True, 'me': me}
        return _with_conn(request, go)

    @app.post('/api/community/heartbeat')
    async def community_heartbeat(request: Request):
        body = await _body(request)
        active = bool(body.get('active', True))

        def go(conn, ident, row):
            now = time.time()
            conn.execute("UPDATE community_profiles SET last_seen=?, last_active=CASE WHEN ? THEN ? ELSE last_active END "
                         "WHERE account_key=?", (now, 1 if active else 0, now, ident['key']))
            return {'ok': True}
        return _with_conn(request, go)

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
        return _with_conn(request, go)

    # ---- guilds & invites -----------------------------------------------------
    @app.get('/api/community/guilds')
    async def guilds_list(request: Request):
        def go(conn, ident, row):
            gs = conn.execute(
                "SELECT g.* FROM community_guilds g JOIN community_guild_members m ON m.guild_id=g.id "
                "WHERE m.account_key=? ORDER BY g.is_default DESC, g.name COLLATE NOCASE", (ident['key'],)).fetchall()
            return {'ok': True, 'guilds': [_guild_json(conn, g, ident['key']) for g in gs]}
        return _with_conn(request, go)

    @app.post('/api/community/guilds')
    async def guilds_create(request: Request):
        body = await _body(request)

        def go(conn, ident, row):
            name = _clean(body.get('name'), 32)
            if len(name) < 2:
                return _err('Give your group a name (at least 2 characters).')
            owned = conn.execute("SELECT COUNT(*) FROM community_guilds WHERE owner_key=?", (ident['key'],)).fetchone()[0]
            if owned >= MAX_GUILDS_OWNED:
                return _err(f'You can own up to {MAX_GUILDS_OWNED} groups.', 403)
            joined = conn.execute("SELECT COUNT(*) FROM community_guild_members WHERE account_key=?", (ident['key'],)).fetchone()[0]
            if joined >= MAX_GUILDS_JOINED:
                return _err(f'You can be in up to {MAX_GUILDS_JOINED} groups.', 403)
            cur = conn.execute("INSERT INTO community_guilds (name, owner_key, is_default, created_at) VALUES (?,?,0,?)",
                               (name, ident['key'], _now_iso()))
            gid = cur.lastrowid
            conn.execute("INSERT INTO community_guild_members (guild_id, account_key, role, joined_at) VALUES (?,?, 'owner', ?)",
                         (gid, ident['key'], _now_iso()))
            g = conn.execute("SELECT * FROM community_guilds WHERE id=?", (gid,)).fetchone()
            return {'ok': True, 'guild': _guild_json(conn, g, ident['key'])}
        return _with_conn(request, go)

    @app.post('/api/community/guilds/{gid}/invite')
    async def guild_invite(gid: int, request: Request):
        body = await _body(request)

        def go(conn, ident, row):
            if _guild_role(conn, gid, ident['key']) is None:
                return _err('You are not a member of that group.', 404)
            try:
                hours = int(body.get('expires_hours', 168))
                max_uses = int(body.get('max_uses', 0))
            except (TypeError, ValueError):
                return _err('Invalid invite settings.')
            hours = max(0, min(hours, 24 * 30))        # 0 = never expires
            max_uses = max(0, min(max_uses, 1000))     # 0 = unlimited
            code = secrets.token_urlsafe(8)
            expires = None if hours == 0 else (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime('%Y-%m-%dT%H:%M:%SZ')
            conn.execute("INSERT INTO community_invites (code, guild_id, created_by, created_at, expires_at, max_uses) "
                         "VALUES (?,?,?,?,?,?)", (code, gid, ident['key'], _now_iso(), expires, max_uses))
            return {'ok': True, 'code': code, 'path': f'/community?invite={code}', 'expires_at': expires, 'max_uses': max_uses}
        return _with_conn(request, go)

    @app.delete('/api/community/invites/{code}')
    async def invite_revoke(code: str, request: Request):
        def go(conn, ident, row):
            inv = conn.execute("SELECT * FROM community_invites WHERE code=?", (code,)).fetchone() if INVITE_RE.match(code) else None
            if not inv:
                return _err('Invite not found.', 404)
            role = _guild_role(conn, inv['guild_id'], ident['key'])
            if inv['created_by'] != ident['key'] and role not in ('owner', 'admin') and not _is_admin(conn, ident):
                return _err('Only the creator or a group admin can revoke this invite.', 403)
            conn.execute("UPDATE community_invites SET revoked=1 WHERE code=?", (code,))
            return {'ok': True}
        return _with_conn(request, go)

    def _invite_state(conn, code):
        if not INVITE_RE.match(code or ''):
            return None, 'This invite link is not valid.'
        inv = conn.execute("SELECT * FROM community_invites WHERE code=?", (code,)).fetchone()
        if not inv or inv['revoked']:
            return None, 'This invite link is not valid any more.'
        if inv['expires_at'] and inv['expires_at'] < _now_iso():
            return None, 'This invite link has expired.'
        if inv['max_uses'] and inv['uses'] >= inv['max_uses']:
            return None, 'This invite link has reached its use limit.'
        return inv, None

    @app.get('/api/community/invites/{code}')
    async def invite_preview(code: str, request: Request):
        def go(conn, ident, row):
            inv, msg = _invite_state(conn, code)
            if not inv:
                return _err(msg, 404)
            g = conn.execute("SELECT * FROM community_guilds WHERE id=?", (inv['guild_id'],)).fetchone()
            if not g:
                return _err('That group no longer exists.', 404)
            return {'ok': True, 'guild': {'id': g['id'], 'name': g['name'],
                                           'members': _guild_json(conn, g, ident['key'])['members']},
                    'already_member': _guild_role(conn, g['id'], ident['key']) is not None}
        return _with_conn(request, go)

    @app.post('/api/community/invites/{code}/join')
    async def invite_join(code: str, request: Request):
        def go(conn, ident, row):
            inv, msg = _invite_state(conn, code)
            if not inv:
                return _err(msg, 404)
            g = conn.execute("SELECT * FROM community_guilds WHERE id=?", (inv['guild_id'],)).fetchone()
            if not g:
                return _err('That group no longer exists.', 404)
            if _guild_role(conn, g['id'], ident['key']) is None:
                joined = conn.execute("SELECT COUNT(*) FROM community_guild_members WHERE account_key=?", (ident['key'],)).fetchone()[0]
                if joined >= MAX_GUILDS_JOINED:
                    return _err(f'You can be in up to {MAX_GUILDS_JOINED} groups.', 403)
                size = conn.execute("SELECT COUNT(*) FROM community_guild_members WHERE guild_id=?", (g['id'],)).fetchone()[0]
                if size >= MAX_GUILD_MEMBERS:
                    return _err('That group is full.', 403)
                conn.execute("INSERT INTO community_guild_members (guild_id, account_key, role, joined_at) VALUES (?,?, 'member', ?)",
                             (g['id'], ident['key'], _now_iso()))
                conn.execute("UPDATE community_invites SET uses=uses+1 WHERE code=?", (code,))
            return {'ok': True, 'guild': _guild_json(conn, g, ident['key'])}
        return _with_conn(request, go)

    @app.post('/api/community/guilds/{gid}/leave')
    async def guild_leave(gid: int, request: Request):
        def go(conn, ident, row):
            role = _guild_role(conn, gid, ident['key'])
            if gid == DEFAULT_GUILD_ID:
                return _err('Everyone stays in the default guild.', 403)
            if role is None:
                return _err('You are not in that group.', 404)
            if role == 'owner':
                return _err('Owners cannot leave their own group. Delete it instead.', 403)
            conn.execute("DELETE FROM community_guild_members WHERE guild_id=? AND account_key=?", (gid, ident['key']))
            return {'ok': True}
        return _with_conn(request, go)

    @app.delete('/api/community/guilds/{gid}')
    async def guild_delete(gid: int, request: Request):
        def go(conn, ident, row):
            if gid == DEFAULT_GUILD_ID:
                return _err('The default guild cannot be deleted.', 403)
            role = _guild_role(conn, gid, ident['key'])
            if role != 'owner' and not _is_admin(conn, ident):
                return _err('Only the owner can delete this group.', 403)
            chan = f'guild:{gid}'
            conn.execute("DELETE FROM community_reactions WHERE message_id IN (SELECT id FROM community_messages WHERE channel=?)", (chan,))
            conn.execute("DELETE FROM community_messages WHERE channel=?", (chan,))
            conn.execute("DELETE FROM community_invites WHERE guild_id=?", (gid,))
            conn.execute("DELETE FROM community_guild_members WHERE guild_id=?", (gid,))
            conn.execute("DELETE FROM community_guilds WHERE id=?", (gid,))
            return {'ok': True}
        return _with_conn(request, go)

    # ---- messages -------------------------------------------------------------
    @app.get('/api/community/messages')
    async def community_messages(request: Request, channel: str = 'lounge', after: int = 0, limit: int = 60):
        def go(conn, ident, row):
            chan, other, bad = _parse_channel(conn, ident, channel)
            if bad:
                return bad
            limit_n = max(1, min(int(limit), 100))
            base = _MSG_SELECT + "WHERE m.channel=? AND m.deleted=0 "
            if after and after > 0:
                rows = conn.execute(base + "AND m.id>? ORDER BY m.id ASC LIMIT 100", (chan, after)).fetchall()
            else:
                rows = list(reversed(conn.execute(base + "ORDER BY m.id DESC LIMIT ?", (chan, limit_n)).fetchall()))
            mine_blocked, theirs = _blocks(conn, ident['key'])
            rows = [r for r in rows if r['sender_key'] not in mine_blocked]
            msgs = _decorate(conn, rows, ident['key'])
            deleted = [r[0] for r in conn.execute(
                "SELECT id FROM community_messages WHERE channel=? AND deleted=1 AND id > "
                "(SELECT COALESCE(MAX(id),0) FROM community_messages WHERE channel=?) - 200", (chan, chan))]
            # state that can change on existing messages (reactions, edits) for the last 60 messages
            recent = conn.execute(_MSG_SELECT + "WHERE m.channel=? AND m.deleted=0 ORDER BY m.id DESC LIMIT 60", (chan,)).fetchall()
            recent = [r for r in recent if r['sender_key'] not in mine_blocked]
            updates = [{'id': m['id'], 'reactions': m['reactions'], 'edited': m['edited'], 'body': m['body']}
                       for m in _decorate(conn, recent, ident['key'])]
            now_m = time.monotonic()
            typers = [name for k, (name, ts) in _typing.get(chan, {}).items()
                      if k != ident['key'] and k not in mine_blocked and now_m - ts < TYPING_TTL]
            out = {'ok': True, 'channel': channel, 'messages': msgs, 'deleted': deleted,
                   'updates': updates, 'typing': typers[:3]}
            if other:
                top = conn.execute("SELECT COALESCE(MAX(id),0) FROM community_messages WHERE channel=?", (chan,)).fetchone()[0]
                conn.execute("INSERT INTO community_reads (account_key, channel, last_id) VALUES (?,?,?) "
                             "ON CONFLICT(account_key, channel) DO UPDATE SET last_id=MAX(community_reads.last_id, excluded.last_id)",
                             (ident['key'], chan, top))
                prow = conn.execute("SELECT * FROM community_profiles WHERE account_key=?", (other,)).fetchone()
                info = _member(prow, time.time(), 0, other in mine_blocked)
                info['blocked_me'] = ident['key'] in {r[0] for r in conn.execute(
                    "SELECT blocked_key FROM community_blocks WHERE blocker_key=?", (other,))}
                out['other'] = info
            elif chan.startswith('guild:') or chan == 'lounge':
                g = conn.execute("SELECT * FROM community_guilds WHERE id=?", (_guild_id_of(chan),)).fetchone()
                if g:
                    out['guild'] = _guild_json(conn, g, ident['key'])
            return out
        return _with_conn(request, go)

    @app.post('/api/community/messages')
    async def community_send(request: Request):
        body = await _body(request)

        def go(conn, ident, row):
            chan, other, bad = _parse_channel(conn, ident, body.get('channel'))
            if bad:
                return bad
            now_m = time.monotonic()
            last = _last_send.get(ident['key'])
            if last is not None and now_m - last < SEND_INTERVAL:
                return _err('Slow down a little.', 429)

            kind = str(body.get('kind', 'text'))
            if kind in ('gif', 'sticker'):
                url = normalize_gif_url(body.get('url'))
                if not url:
                    return _err('That link is not allowed. Use a GIPHY or Tenor link, or search for one.')
                payload = url
            elif kind == 'text':
                payload = CTRL_RE.sub('', str(body.get('text', ''))).strip()
                if not payload:
                    return _err('Write something first.')
                if len(payload) > MAX_TEXT:
                    return _err(f'Messages can be up to {MAX_TEXT} characters.')
            else:
                return _err('Unknown message type.')

            reply_to = None
            if body.get('reply_to') not in (None, ''):
                try:
                    rid = int(body['reply_to'])
                except (TypeError, ValueError):
                    return _err('Invalid reply.')
                tgt = conn.execute("SELECT id FROM community_messages WHERE id=? AND channel=? AND deleted=0", (rid, chan)).fetchone()
                if not tgt:
                    return _err('The message you are replying to is gone.', 404)
                reply_to = rid

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

            _last_send[ident['key']] = now_m
            _typing.get(chan, {}).pop(ident['key'], None)
            cur = conn.execute(
                "INSERT INTO community_messages (channel, sender_key, sender_name, kind, body, created_at, reply_to) "
                "VALUES (?,?,?,?,?,?,?)", (chan, ident['key'], ident['name'], kind, payload, _now_iso(), reply_to))
            new_id = cur.lastrowid
            conn.execute("DELETE FROM community_messages WHERE channel=? AND id <= "
                         "(SELECT id FROM community_messages WHERE channel=? ORDER BY id DESC LIMIT 1 OFFSET ?)",
                         (chan, chan, KEEP_PER_CHANNEL))
            conn.execute("DELETE FROM community_reactions WHERE message_id NOT IN (SELECT id FROM community_messages)")
            if other:
                conn.execute("INSERT INTO community_reads (account_key, channel, last_id) VALUES (?,?,?) "
                             "ON CONFLICT(account_key, channel) DO UPDATE SET last_id=MAX(community_reads.last_id, excluded.last_id)",
                             (ident['key'], chan, new_id))
            r = conn.execute(_MSG_SELECT + "WHERE m.id=?", (new_id,)).fetchone()
            return {'ok': True, 'message': _decorate(conn, [r], ident['key'])[0], 'notice': notice}
        return _with_conn(request, go)

    @app.patch('/api/community/messages/{msg_id}')
    async def community_edit(msg_id: int, request: Request):
        body = await _body(request)

        def go(conn, ident, row):
            m = conn.execute("SELECT * FROM community_messages WHERE id=? AND deleted=0", (msg_id,)).fetchone()
            if not m:
                return _err('Message not found.', 404)
            if m['sender_key'] != ident['key']:
                return _err('You can only edit your own messages.', 403)
            if m['kind'] != 'text':
                return _err('Only text messages can be edited.')
            text = CTRL_RE.sub('', str(body.get('text', ''))).strip()
            if not text:
                return _err('A message cannot be empty. Delete it instead.')
            if len(text) > MAX_TEXT:
                return _err(f'Messages can be up to {MAX_TEXT} characters.')
            conn.execute("UPDATE community_messages SET body=?, edited=1 WHERE id=?", (text, msg_id))
            r = conn.execute(_MSG_SELECT + "WHERE m.id=?", (msg_id,)).fetchone()
            return {'ok': True, 'message': _decorate(conn, [r], ident['key'])[0]}
        return _with_conn(request, go)

    @app.delete('/api/community/messages/{msg_id}')
    async def community_delete(msg_id: int, request: Request):
        def go(conn, ident, row):
            m = conn.execute("SELECT id, sender_key, channel FROM community_messages WHERE id=? AND deleted=0", (msg_id,)).fetchone()
            if not m:
                return _err('Message not found.', 404)
            if m['sender_key'] != ident['key'] and not (not m['channel'].startswith('dm:') and _can_moderate(conn, ident, m['channel'])):
                return _err('You can only delete your own messages.', 403)
            conn.execute("UPDATE community_messages SET deleted=1, body='' WHERE id=?", (msg_id,))
            conn.execute("DELETE FROM community_reactions WHERE message_id=?", (msg_id,))
            return {'ok': True}
        return _with_conn(request, go)

    # ---- reactions & typing -----------------------------------------------------
    @app.post('/api/community/messages/{msg_id}/react')
    async def community_react(msg_id: int, request: Request):
        body = await _body(request)

        def go(conn, ident, row):
            emoji = str(body.get('emoji', ''))
            if emoji not in REACTIONS:
                return _err('That reaction is not available.')
            m = conn.execute("SELECT channel FROM community_messages WHERE id=? AND deleted=0", (msg_id,)).fetchone()
            if not m:
                return _err('Message not found.', 404)
            ch = m['channel']
            if ch.startswith('dm:'):
                if ident['key'] not in ch[3:].split('|'):
                    return _err('Message not found.', 404)
            elif ch != 'lounge' and (_guild_id_of(ch) is None or _guild_role(conn, _guild_id_of(ch), ident['key']) is None):
                return _err('Message not found.', 404)
            have = conn.execute("SELECT 1 FROM community_reactions WHERE message_id=? AND account_key=? AND emoji=?",
                                (msg_id, ident['key'], emoji)).fetchone()
            if have:
                conn.execute("DELETE FROM community_reactions WHERE message_id=? AND account_key=? AND emoji=?",
                             (msg_id, ident['key'], emoji))
            else:
                n = conn.execute("SELECT COUNT(*) FROM community_reactions WHERE message_id=? AND account_key=?",
                                 (msg_id, ident['key'])).fetchone()[0]
                if n >= 5:
                    return _err('You can add up to 5 reactions per message.')
                conn.execute("INSERT INTO community_reactions (message_id, account_key, emoji) VALUES (?,?,?)",
                             (msg_id, ident['key'], emoji))
            r = conn.execute(_MSG_SELECT + "WHERE m.id=?", (msg_id,)).fetchone()
            return {'ok': True, 'reactions': _decorate(conn, [r], ident['key'])[0]['reactions']}
        return _with_conn(request, go)

    @app.post('/api/community/typing')
    async def community_typing(request: Request):
        body = await _body(request)

        def go(conn, ident, row):
            chan, other, bad = _parse_channel(conn, ident, body.get('channel'))
            if bad:
                return bad
            if len(_typing) > 500:
                _typing.clear()
            _typing.setdefault(chan, {})[ident['key']] = (ident['name'], time.monotonic())
            return {'ok': True}
        return _with_conn(request, go)

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
        return _with_conn(request, go)

    # ---- gifts -----------------------------------------------------------------
    @app.get('/api/community/inventory')
    async def community_inventory(request: Request):
        def go(conn, ident, row):
            if inventory is None:
                return _err('Gifting is not connected to the inventory yet.', 501)
            return {'ok': True, 'items': inventory.list_items(conn, ident['key'])}
        return _with_conn(request, go)

    @app.post('/api/community/gifts')
    async def community_gift(request: Request):
        body = await _body(request)

        def go(conn, ident, row):
            if inventory is None:
                return _err('Gifting is not connected to the inventory yet.', 501)
            to_key = str(body.get('to', ''))
            if not KEY_RE.match(to_key) or to_key == ident['key']:
                return _err('Choose a friend to gift.', 404)
            prow = conn.execute("SELECT * FROM community_profiles WHERE account_key=?", (to_key,)).fetchone()
            if not prow:
                return _err('That adventurer has not joined the Community yet.', 404)
            mine_blocked, theirs = _blocks(conn, ident['key'])
            if to_key in mine_blocked or to_key in theirs:
                return _err('You cannot gift this adventurer.', 403)
            if friends_check is not None and not friends_check(conn, ident['key'], to_key):
                return _err('You can only gift your friends.', 403)
            item_id = _clean(body.get('item_id'), 64)
            try:
                qty = int(body.get('qty', 1))
            except (TypeError, ValueError):
                return _err('Invalid quantity.')
            if not item_id or qty < 1 or qty > MAX_GIFT_QTY:
                return _err(f'Choose an item and a quantity from 1 to {MAX_GIFT_QTY}.')
            now_m = time.monotonic()
            last = _last_gift.get(ident['key'])
            if last is not None and now_m - last < 2.0:
                return _err('Slow down a little.', 429)
            note = _clean(body.get('note'), MAX_GIFT_NOTE)
            try:
                name = inventory.transfer(conn, ident['key'], to_key, item_id, qty)   # same transaction as the log + message
            except ValueError as e:
                return _err(str(e) or "You can't gift that.", 400)
            _last_gift[ident['key']] = now_m
            conn.execute("INSERT INTO community_gifts (from_key, to_key, item_id, item_name, qty, note, created_at) "
                         "VALUES (?,?,?,?,?,?,?)", (ident['key'], to_key, item_id, name, qty, note, _now_iso()))
            chan = _canon(ident['key'], to_key)
            payload = json.dumps({'item': name, 'qty': qty, 'note': note}, ensure_ascii=False)
            cur = conn.execute(
                "INSERT INTO community_messages (channel, sender_key, sender_name, kind, body, created_at) VALUES (?,?,?,?,?,?)",
                (chan, ident['key'], ident['name'], 'gift', payload, _now_iso()))
            conn.execute("INSERT INTO community_reads (account_key, channel, last_id) VALUES (?,?,?) "
                         "ON CONFLICT(account_key, channel) DO UPDATE SET last_id=MAX(community_reads.last_id, excluded.last_id)",
                         (ident['key'], chan, cur.lastrowid))
            r = conn.execute(_MSG_SELECT + "WHERE m.id=?", (cur.lastrowid,)).fetchone()
            return {'ok': True, 'message': _decorate(conn, [r], ident['key'])[0], 'items': inventory.list_items(conn, ident['key'])}
        return _with_conn(request, go)

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
        ck = ('s' if sticker else 'g') + ':' + q
        hit = _gif_cache.get(ck)
        if hit and time.time() - hit[0] < 120:
            return {'ok': True, 'enabled': True, 'gifs': hit[1]}
        try:
            gifs = await asyncio.to_thread(_fetch_giphy, q, sticker)
        except Exception:
            log.exception('[community] GIPHY request failed')
            return _err('GIF search is unavailable right now.', 502)
        if len(_gif_cache) > 200:
            _gif_cache.clear()
        _gif_cache[ck] = (time.time(), gifs)
        return {'ok': True, 'enabled': True, 'gifs': gifs}
