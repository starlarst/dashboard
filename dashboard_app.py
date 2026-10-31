import os
import sys
import json
import hmac
import time
import socket
import logging
import secrets
import threading
from contextlib import asynccontextmanager
from collections import defaultdict
from datetime import datetime, timedelta, timezone, UTC
from urllib.parse import urlencode

import uvicorn
import requests as http_requests
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

load_dotenv()

# If this file is started directly (python dashboard_app.py), make `import dashboard_app` in the route
# modules return THIS module instead of importing the file a second time.
if __name__ == '__main__':
    sys.modules.setdefault('dashboard_app', sys.modules['__main__'])

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(name)s — %(message)s")
log = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────
def _require_env(name):
    val = os.getenv(name)
    if not val:
        raise ValueError(f"{name} not found in .env file!")
    return val.strip()

DISCORD_CLIENT_ID = _require_env('DISCORD_CLIENT_ID')
DISCORD_CLIENT_SECRET = _require_env('DISCORD_CLIENT_SECRET')
DISCORD_REDIRECT_URI = _require_env('DISCORD_REDIRECT_URI')
DISCORD_API = 'https://discord.com/api'

GOOGLE_CLIENT_ID = os.getenv(
    'GOOGLE_CLIENT_ID',
    '1039889826782-sbk3rm2io1a6r3eiu6oh1cgs7chrlkac.apps.googleusercontent.com').strip()
GOOGLE_CLIENT_SECRET = _require_env('GOOGLE_CLIENT_SECRET')
# Must be the Google handler (/callback/google), NOT /callback (that's Discord's), and must match
# the "Authorized redirect URIs" entry in the Google Cloud console exactly.
GOOGLE_REDIRECT_URI = os.getenv('GOOGLE_REDIRECT_URI', 'https://rpghost.qzz.io/callback/google').strip()

GOOGLE_AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
GOOGLE_TOKEN_URL = 'https://oauth2.googleapis.com/token'
GOOGLE_USERINFO_URL = 'https://www.googleapis.com/oauth2/v3/userinfo'

ADMIN_EMAIL = os.getenv('ADMIN_EMAIL')
if not ADMIN_EMAIL:
    log.warning("[Admin] ADMIN_EMAIL not set — no account will have admin access.")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = BASE_DIR
HTTP_TIMEOUT = 10

# ── Database (Neon / PostgreSQL) ─────────────────────────────────────────────
from db_pg import PGConnection, fix_sequences 

def _db():
    return PGConnection()

DEFAULT_STATS = {
    'level': 1, 'gold': 100, 'gems': 10, 'special_coins': 0,
    'xp': 0, 'xp_needed': 100, 'char_class': 'Adventurer',
    'battles_won': 0, 'prestige_level': 0, 'guild': '—',
    'name': None, 'avatar': None, 'bio': '',
}
EDITABLE_STAT_FIELDS = {
    'level': int, 'gold': int, 'gems': int, 'special_coins': int,
    'xp': int, 'xp_needed': int, 'char_class': str,
    'battles_won': int, 'prestige_level': int, 'guild': str, 'name': str,
    'avatar': str, 'bio': str,
}

def init_db():
    """Create all necessary tables if they don't exist (runs once at startup)."""
    conn = _db()
    try:
        for ddl in (
            """CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY, gold INTEGER DEFAULT 0, gems INTEGER DEFAULT 0,
                special_coins INTEGER DEFAULT 0, xp INTEGER DEFAULT 0, level INTEGER DEFAULT 1,
                last_daily TIMESTAMP, last_weekly TIMESTAMP, pity_counter INTEGER DEFAULT 0,
                total_pulls INTEGER DEFAULT 0, unlocked_characters TEXT DEFAULT '[]',
                unlocked_weapons TEXT DEFAULT '[]')""",
            """CREATE TABLE IF NOT EXISTS players (
                user_id BIGINT PRIMARY KEY, data TEXT, last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
            """CREATE TABLE IF NOT EXISTS guilds (
                name TEXT PRIMARY KEY, data TEXT, last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
            """CREATE TABLE IF NOT EXISTS world_state (
                id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
            """CREATE TABLE IF NOT EXISTS account_links (
                discord_id TEXT PRIMARY KEY, linked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_login TIMESTAMP)""",
            """CREATE TABLE IF NOT EXISTS activity_log (
                id BIGSERIAL PRIMARY KEY, user_id TEXT, action TEXT, detail TEXT,
                ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
            """CREATE TABLE IF NOT EXISTS player_stats (
                account_key TEXT PRIMARY KEY, data TEXT NOT NULL,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
            """CREATE TABLE IF NOT EXISTS announcements (
                id BIGSERIAL PRIMARY KEY, message TEXT NOT NULL, author TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
            """CREATE TABLE IF NOT EXISTS auctions (
                id BIGSERIAL PRIMARY KEY, item_name TEXT NOT NULL, seller_id BIGINT NOT NULL,
                current_bid INTEGER NOT NULL DEFAULT 0, highest_bidder BIGINT,
                status TEXT NOT NULL DEFAULT 'active', end_time TIMESTAMP NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
            """CREATE TABLE IF NOT EXISTS guild_members (
                account_key TEXT NOT NULL, guild_name TEXT NOT NULL,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (account_key))""",
        ):
            conn.execute(ddl)
        conn.commit()
        # Rows copied over from SQLite keep their ids, but the serial counters start at 1 and would collide.
        fix_sequences(conn, ['activity_log', 'announcements', 'auctions'])
    finally:
        conn.close()

# ── Player stats ─────────────────────────────────────────────────────────────
def _stats_key(auth_type, uid):
    return f"{auth_type}:{uid}"

def get_stats(auth_type, uid):
    """Return stats for an account, creating a default row if needed."""
    key = _stats_key(auth_type, uid)
    conn = _db()
    try:
        row = conn.execute("SELECT data FROM player_stats WHERE account_key=?", (key,)).fetchone()
        if row:
            try:
                return {**DEFAULT_STATS, **json.loads(row['data'])}
            except Exception:
                pass
        conn.execute(
            "INSERT OR IGNORE INTO player_stats (account_key, data) VALUES (?, ?)",
            (key, json.dumps(DEFAULT_STATS)),
        )
        conn.commit()
        return dict(DEFAULT_STATS)
    finally:
        conn.close()

def save_stats(auth_type, uid, updates, deltas=None):
    """Merge validated `updates` into stored stats and return the result.

    Read-modify-write happens on ONE connection with the row locked (SELECT ... FOR UPDATE), so two
    requests changing the same player can't overwrite each other. `deltas` adds to numeric fields
    ({'gold': 50}) inside that same lock, which is what you want for "give gold"."""
    clean = {}
    for k, v in (updates or {}).items():
        caster = EDITABLE_STAT_FIELDS.get(k)
        if not caster:
            continue
        try:
            clean[k] = max(0, caster(v)) if caster is int else str(v)[:500]
        except (ValueError, TypeError):
            continue
    key = _stats_key(auth_type, uid)
    conn = _db()
    try:
        row = conn.execute("SELECT data FROM player_stats WHERE account_key=? FOR UPDATE", (key,)).fetchone()
        try:
            current = {**DEFAULT_STATS, **json.loads(row['data'])} if row else dict(DEFAULT_STATS)
        except Exception:
            current = dict(DEFAULT_STATS)
        merged = {**current, **clean}
        for field, delta in (deltas or {}).items():
            if EDITABLE_STAT_FIELDS.get(field) is int:
                merged[field] = max(0, int(merged.get(field) or 0) + int(delta))
        conn.execute(
            "INSERT INTO player_stats (account_key, data, last_updated) VALUES (?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(account_key) DO UPDATE SET data=excluded.data, last_updated=CURRENT_TIMESTAMP",
            (key, json.dumps(merged)),
        )
        conn.commit()
        return merged
    finally:
        conn.close()

def _apply_live_stats(session_user):
    """Overlay fresh DB stats on a session user dict (fixes stale profile data)."""
    stats = get_stats(session_user.get('auth_type', 'discord'), session_user['discord_id'])
    for k in ('level', 'gold', 'gems', 'special_coins', 'xp', 'xp_needed', 'char_class'):
        session_user[k] = stats.get(k, session_user.get(k))
    session_user['battles_won'] = stats.get('battles_won', 0)
    session_user['prestige_level'] = stats.get('prestige_level', 0)
    session_user['guild'] = stats.get('guild', '—')
    session_user['bio'] = stats.get('bio', session_user.get('bio', ''))
    return session_user

# ── In-memory storage (ephemeral by design — rate limits, bans, sessions) ────
_rate_limits = {}
_session_log = {}
_ip_connections = defaultdict(int)
_ip_banned = {}
_global_req_count = {'n': 0}
_req_id_counter = 0

# ── DDoS protection settings ─────────────────────────────────────────────────
DDOS_BURST_LIMIT = 40
DDOS_GLOBAL_CAP = 500
DDOS_BAN_DURATION = 3600
DDOS_MAX_BODY_BYTES = 65536
DDOS_BODY_LIMITS = {
    '/api/community/upload': 55 * 1024 * 1024,
    '/api/community/emojis': 400 * 1024,
}
DDOS_BLOCKED_UA_FRAGS = [
    'sqlmap', 'nikto', 'nmap', 'masscan', 'zgrab',
    'python-requests/2.18', 'curl/7.29', 'dirbuster',
    'go-http-client/1', 'libwww-perl',
]
DDOS_HONEYPOT_PATHS = [
    '/wp-admin', '/wp-login', '/.env', '/admin.php',
    '/phpinfo', '/config.php', '/xmlrpc', '/shell',
    '/.git', '/etc/passwd', '/proc/self',
]

# ── Secret key ───────────────────────────────────────────────────────────────
def _get_or_create_secret_key():
    """Use FLASK_SECRET if set, otherwise persist a generated key to disk."""
    env_secret = os.environ.get('FLASK_SECRET')
    if env_secret:
        return env_secret
    log.warning("[secret_key] FLASK_SECRET is not set. A key file on a host with an ephemeral disk "
                "(Render free tier) is regenerated on every deploy and logs everyone out — set FLASK_SECRET.")
    secret_file = os.path.join(DATA_DIR, '.flask_secret_key')
    try:
        if os.path.exists(secret_file):
            with open(secret_file, 'r') as f:
                existing = f.read().strip()
                if existing:
                    return existing
    except Exception as e:
        log.warning(f"[secret_key] could not read {secret_file}: {e}")
    new_secret = secrets.token_hex(32)
    try:
        with open(secret_file, 'w') as f:
            f.write(new_secret)
    except Exception as e:
        log.warning(f"[secret_key] could not persist {secret_file}: {e}")
    return new_secret

SECRET_KEY = _get_or_create_secret_key()
SESSION_LIFETIME_SECONDS = int(timedelta(days=30).total_seconds())

UPDATE_INTERVAL = int(os.getenv('UPDATE_INTERVAL', '60'))   # seconds; every refresh wakes the Neon compute
cached_data = {}
last_updated = None
_lock = threading.Lock()

# ── Small helpers ────────────────────────────────────────────────────────────
def _log_activity(user_id, action, detail=""):
    try:
        conn = _db()
        try:
            conn.execute(
                "INSERT INTO activity_log (user_id, action, detail) VALUES (?,?,?)",
                (str(user_id), action, detail),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.warning(f"[activity_log] write failed: {e}")

def _get_real_ip(request: Request):
    xff = request.headers.get('X-Forwarded-For', '')
    if xff:
        parts = [p.strip() for p in xff.split(',') if p.strip()]
        if parts:
            return parts[-1]
    return request.client.host if request.client else '0.0.0.0'

def _is_banned(ip):
    expires = _ip_banned.get(ip)
    if expires is None:
        return False
    if time.time() > expires:
        _ip_banned.pop(ip, None)
        return False
    return True

def _ban_ip(ip, reason='', duration=DDOS_BAN_DURATION):
    _ip_banned[ip] = time.time() + duration
    log.warning(f"[DDoS] BANNED {ip} for {duration}s — {reason}")

def _strict_rate_limit(ip, max_calls=3, window=60, scope='default'):
    now = time.time()
    key = f'strict:{scope}:{ip}'
    hits = [t for t in _rate_limits.get(key, []) if now - t < window]
    _rate_limits[key] = hits
    if len(hits) >= max_calls:
        if len(hits) >= max_calls * 3:
            _ban_ip(ip, f"rate abuse on sensitive endpoint ({len(hits)} hits)")
        return True
    _rate_limits[key].append(now)
    return False

def _json(content: dict, status_code: int = 200):
    return JSONResponse(content=content, status_code=status_code)

async def _get_json(request: Request) -> dict:
    try:
        b = await request.json()
        return b if isinstance(b, dict) else {}
    except Exception:
        return {}

def _is_admin_email(email: str) -> bool:
    if not ADMIN_EMAIL or not email:
        return False
    return email.strip().lower() == ADMIN_EMAIL.strip().lower()

def _compute_admin(email: str, email_verified: bool) -> bool:
    """Admin = the provider says this email is verified AND it equals ADMIN_EMAIL.
    Without the verified check, anyone could put your admin address on a Discord account and get in."""
    return bool(email_verified) and _is_admin_email(email)

def _session_is_admin(user) -> bool:
    return bool(user) and _compute_admin(user.get('email', ''), user.get('email_verified', False))

def _split_account(raw):
    if ':' in raw:
        t, u = raw.split(':', 1)
        return (t if t in ('discord', 'google') else 'discord'), u
    return 'discord', raw

def _may_access_account(user, user_id: str) -> bool:
    """A logged-in user may read their own data (by plain id or full account key); admins may read anyone's."""
    if not user:
        return False
    if _session_is_admin(user):
        return True
    own_id = str(user.get('discord_id', ''))
    own_key = _stats_key(user.get('auth_type', 'discord'), own_id)
    return user_id in (own_id, own_key)

# ── Game data (leaderboard / dashboard cache) ────────────────────────────────
def load_game_data():
    """Rebuild the in-memory leaderboard/guild cache from the database."""
    global cached_data, last_updated
    try:
        conn = _db()
        fixes = []
        try:
            players = {}
            for row in conn.execute("SELECT account_key, data FROM player_stats").fetchall():
                try:
                    d = json.loads(row['data'])
                except Exception:
                    continue
                d.setdefault('name', row['account_key'])
                players[row['account_key']] = d

            # Guild membership comes from guild_members (survives restarts); player_stats.guild is
            # brought back in line with it.
            guilds = {}
            for gm_row in conn.execute(
                "SELECT gm.account_key, gm.guild_name, ps.data "
                "FROM guild_members gm "
                "LEFT JOIN player_stats ps ON gm.account_key = ps.account_key"
            ).fetchall():
                guild_name = gm_row['guild_name']
                if not guild_name or guild_name == '—':
                    continue
                try:
                    pd = json.loads(gm_row['data']) if gm_row['data'] else {}
                except Exception:
                    pd = {}
                member_name = pd.get('name', gm_row['account_key'])
                guilds.setdefault(guild_name, {'members': []})['members'].append(member_name)

                if pd.get('guild') != guild_name:
                    fixes.append((gm_row['account_key'], guild_name))
                    if gm_row['account_key'] in players:
                        players[gm_row['account_key']]['guild'] = guild_name

            for pid, pd in players.items():
                g = pd.get('guild')
                if g and g != '—':
                    guilds.setdefault(g, {'members': []})
                    member_name = pd.get('name', pid)
                    if member_name not in guilds[g]['members']:
                        guilds[g]['members'].append(member_name)
                        try:
                            conn.execute(
                                "INSERT OR IGNORE INTO guild_members (account_key, guild_name) VALUES (?, ?)",
                                (pid, g)
                            )
                        except Exception:
                            conn.rollback()
            conn.commit()
        finally:
            conn.close()

        # Done after the connection above is released (never hold one pool slot while asking for another).
        for account_key, guild_name in fixes:
            atype, uid = _split_account(account_key)
            save_stats(atype, uid, {'guild': guild_name})

        with _lock:
            cached_data = {'players': players, 'guilds': guilds, 'world_state': cached_data.get('world_state', {})}
            last_updated = datetime.now(timezone.utc)
    except Exception as e:
        log.error(f"[Dashboard] load_game_data error: {e}")

def _bg_updater():
    while True:
        time.sleep(UPDATE_INTERVAL)
        load_game_data()

# ── Guild helpers ────────────────────────────────────────────────────────────
from guild_routes import register_guild_routes, remove_account_from_guild  # noqa: E402

def join_guild(auth_type: str, uid: str, guild_name: str):
    """Add a player to a guild, persisting to both player_stats and guild_members."""
    key = _stats_key(auth_type, uid)
    save_stats(auth_type, uid, {'guild': guild_name})
    conn = _db()
    try:
        conn.execute(
            "INSERT INTO guild_members (account_key, guild_name) VALUES (?, ?) "
            "ON CONFLICT(account_key) DO UPDATE SET guild_name=excluded.guild_name, joined_at=CURRENT_TIMESTAMP",
            (key, guild_name)
        )
        conn.commit()
    finally:
        conn.close()

def leave_guild(auth_type: str, uid: str):
    """Remove a player from their guild (also fixes the guild's own member list / leader)."""
    key = _stats_key(auth_type, uid)
    conn = _db()
    try:
        remove_account_from_guild(conn, key)
        conn.commit()
    finally:
        conn.close()

# ── FastAPI app setup ────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    await run_in_threadpool(load_game_data)
    threading.Thread(target=_bg_updater, daemon=True).start()
    yield

app = FastAPI(lifespan=lifespan)

SESSION_HTTPS_ONLY = os.getenv('SESSION_HTTPS_ONLY', 'true').strip().lower() not in ('false', '0', 'no')
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    max_age=SESSION_LIFETIME_SECONDS,
    same_site="lax",
    https_only=SESSION_HTTPS_ONLY,
)

# Tables must exist before the route modules (which also create their own) are registered.
init_db()

from routes_profile import register_profile_routes  # noqa: E402
register_profile_routes(app)

from game_engine import register_game_routes  # noqa: E402
register_game_routes(app, _db)

from community import register_community_routes  # noqa: E402
register_community_routes(app, _db)

register_guild_routes(app, _db)

templates = Jinja2Templates(directory=os.path.join(BASE_DIR, 'templates'))

STATIC_DIR = os.path.join(BASE_DIR, 'static')
if not os.path.isdir(STATIC_DIR):
    try:
        os.makedirs(STATIC_DIR, exist_ok=True)
        log.info(f"[static] Created missing static directory at {STATIC_DIR}")
    except Exception as e:
        log.warning(f"[static] Could not create static dir {STATIC_DIR}: {e}")
app.mount('/static', StaticFiles(directory=STATIC_DIR), name='static')

# ── DDoS protection middleware ───────────────────────────────────────────────
def _add_security_headers(response, req_id=None):
    if req_id:
        response.headers['X-Request-ID'] = req_id
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    return response

@app.middleware("http")
async def ddos_guard(request: Request, call_next):
    global _req_id_counter
    ip = _get_real_ip(request)

    if _is_banned(ip):
        log.info(f"[DDoS] Blocked banned IP: {ip}")
        return _add_security_headers(PlainTextResponse("Forbidden", status_code=403))

    is_health_check = request.method == 'HEAD'

    ua = (request.headers.get('user-agent', '') or '').lower()
    if not is_health_check and any(frag in ua for frag in DDOS_BLOCKED_UA_FRAGS):
        _ban_ip(ip, f"bad UA: {ua[:60]}")
        return _add_security_headers(PlainTextResponse("Forbidden", status_code=403))

    if not is_health_check and any(request.url.path.startswith(p) for p in DDOS_HONEYPOT_PATHS):
        _ban_ip(ip, f"honeypot: {request.url.path}")
        return _add_security_headers(PlainTextResponse("Not Found", status_code=404))

    if _global_req_count['n'] >= DDOS_GLOBAL_CAP:
        log.warning(f"[DDoS] Global cap hit ({_global_req_count['n']} reqs)")
        return _add_security_headers(
            PlainTextResponse("Service Unavailable", status_code=503, headers={'Retry-After': '5'})
        )

    # From here on the counters MUST be released on every exit path (including exceptions), or they
    # drift upward until everybody gets 503s.
    _ip_connections[ip] += 1
    _global_req_count['n'] += 1
    try:
        if _ip_connections[ip] > DDOS_BURST_LIMIT:
            _ban_ip(ip, f"burst: {_ip_connections[ip]} concurrent")
            return _add_security_headers(
                PlainTextResponse("Too Many Requests", status_code=429, headers={'Retry-After': '60'})
            )

        cl = request.headers.get('content-length')
        body_limit = DDOS_BODY_LIMITS.get(request.url.path, DDOS_MAX_BODY_BYTES)
        try:
            too_big = bool(cl) and int(cl) > body_limit
        except Exception:
            too_big = True
        if too_big:
            log.warning(f"[DDoS] Body too large from {ip}: {cl} bytes (limit {body_limit})")
            return _add_security_headers(PlainTextResponse("request too big", status_code=413))

        _req_id_counter += 1
        request.state.req_id = f"{_req_id_counter:08x}"
        request.state.req_ip = ip
        request.state.req_start = time.monotonic()

        response = await call_next(request)
    finally:
        left = _ip_connections.get(ip, 1) - 1
        if left > 0:
            _ip_connections[ip] = left
        else:
            _ip_connections.pop(ip, None)
        _global_req_count['n'] = max(0, _global_req_count['n'] - 1)

    return _add_security_headers(response, req_id=getattr(request.state, 'req_id', None))

# ── Sign-in bookkeeping shared by both providers ─────────────────────────────
def _finish_login(request, *, uid, username, email, email_verified, avatar, auth_type, ip):
    game_data = get_stats(auth_type, uid)
    request.session['user'] = {
        'discord_id': str(uid),        # legacy key name — holds the account id regardless of provider
        'username': username,
        'email': email,
        'email_verified': bool(email_verified),
        'avatar': avatar,
        'level': game_data.get('level', 1),
        'gold': game_data.get('gold', 0),
        'gems': game_data.get('gems', 0),
        'special_coins': game_data.get('special_coins', 0),
        'char_class': game_data.get('char_class', '—'),
        'xp': game_data.get('xp', 0),
        'xp_needed': game_data.get('xp_needed', 100),
        'login_at': datetime.now(UTC).isoformat(),
        'ip': ip,
        'auth_type': auth_type,
        'is_admin': _compute_admin(email, email_verified),
    }
    save_stats(auth_type, str(uid), {'name': username, 'avatar': avatar})

    sid = str(uid) if auth_type == 'discord' else f'{auth_type}:{uid}'
    _session_log.setdefault(sid, []).append({
        'ip': ip,
        'ua': request.headers.get('user-agent', '')[:120],
        'at': datetime.now(UTC).isoformat(),
    })
    _session_log[sid] = _session_log[sid][-10:]

    try:
        conn = _db()
        try:
            conn.execute(
                "INSERT INTO account_links (discord_id, last_login) VALUES (?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(discord_id) DO UPDATE SET last_login=CURRENT_TIMESTAMP",
                (str(uid),),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.warning(f"[login] account_links update failed: {e}")

    # activity_log uses the plain account id everywhere (profile + community read it that way)
    _log_activity(uid, "login", f"from {ip}")
    log.info(f"[OAuth/{auth_type}] Logged in: {username} (ID: {uid})")

# ── Discord OAuth ────────────────────────────────────────────────────────────
@app.get('/login/discord')
async def discord_login(request: Request):
    state = secrets.token_urlsafe(32)
    request.session['oauth_state'] = state
    params = {
        'client_id': DISCORD_CLIENT_ID,
        'redirect_uri': DISCORD_REDIRECT_URI,
        'response_type': 'code',
        'scope': 'identify email',
        'state': state,
    }
    return RedirectResponse(f"{DISCORD_API}/oauth2/authorize?{urlencode(params)}")

@app.get('/callback')
def discord_callback(request: Request):      # plain def: blocking HTTP + DB run in FastAPI's threadpool
    ip = _get_real_ip(request)
    code = request.query_params.get('code')
    error = request.query_params.get('error')
    state = request.query_params.get('state', '')

    expected_state = request.session.pop('oauth_state', None)
    if not expected_state or not hmac.compare_digest(state, expected_state):
        log.warning(f"[OAuth] state mismatch/missing from {ip} — possible CSRF, rejecting callback")
        return RedirectResponse('/?auth=failed')

    if error or not code:
        log.warning(f"[OAuth] callback error param={error} code_present={bool(code)}")
        return RedirectResponse('/?auth=failed')

    token_res = http_requests.post(
        f'{DISCORD_API}/oauth2/token',
        data={
            'client_id': DISCORD_CLIENT_ID,
            'client_secret': DISCORD_CLIENT_SECRET,
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': DISCORD_REDIRECT_URI,
        },
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        timeout=HTTP_TIMEOUT,
    )
    if token_res.status_code != 200:
        log.warning(f'[OAuth] Token exchange failed ({token_res.status_code}): {token_res.text}')
        return RedirectResponse('/?auth=failed')

    access_token = token_res.json().get('access_token')
    user_res = http_requests.get(
        f'{DISCORD_API}/users/@me',
        headers={'Authorization': f'Bearer {access_token}'},
        timeout=HTTP_TIMEOUT,
    )
    if user_res.status_code != 200:
        log.warning(f'[OAuth] /users/@me failed ({user_res.status_code}): {user_res.text}')
        return RedirectResponse('/?auth=failed')

    discord_user = user_res.json()
    user_id = int(discord_user['id'])
    avatar_hash = discord_user.get('avatar')
    avatar_url = (
        f"https://cdn.discordapp.com/avatars/{user_id}/{avatar_hash}.png"
        if avatar_hash
        else f"https://cdn.discordapp.com/embed/avatars/{user_id % 5}.png"
    )
    _finish_login(
        request, uid=user_id,
        username=discord_user.get('global_name') or discord_user.get('username', 'Player'),
        email=discord_user.get('email') or '',
        email_verified=discord_user.get('verified', False),
        avatar=avatar_url, auth_type='discord', ip=ip,
    )
    return RedirectResponse('/?auth=success')

# ── Google OAuth ─────────────────────────────────────────────────────────────
@app.get('/login/google')
async def google_login(request: Request):
    state = secrets.token_urlsafe(32)
    request.session['oauth_state'] = state
    params = {
        'client_id': GOOGLE_CLIENT_ID,
        'redirect_uri': GOOGLE_REDIRECT_URI,
        'response_type': 'code',
        'scope': 'openid email profile',
        'state': state,
        'access_type': 'online',
        'prompt': 'select_account',
    }
    return RedirectResponse(f"{GOOGLE_AUTH_URL}?{urlencode(params)}")

@app.get('/callback/google')
def google_callback(request: Request):
    ip = _get_real_ip(request)
    code = request.query_params.get('code')
    error = request.query_params.get('error')
    state = request.query_params.get('state', '')

    expected_state = request.session.pop('oauth_state', None)
    if not expected_state or not hmac.compare_digest(state, expected_state):
        log.warning(f"[OAuth/Google] state mismatch/missing from {ip} — possible CSRF, rejecting callback")
        return RedirectResponse('/?auth=failed')

    if error or not code:
        log.warning(f"[OAuth/Google] callback error param={error} code_present={bool(code)}")
        return RedirectResponse('/?auth=failed')

    token_res = http_requests.post(
        GOOGLE_TOKEN_URL,
        data={
            'client_id': GOOGLE_CLIENT_ID,
            'client_secret': GOOGLE_CLIENT_SECRET,
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': GOOGLE_REDIRECT_URI,
        },
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        timeout=HTTP_TIMEOUT,
    )
    if token_res.status_code != 200:
        log.warning(f'[OAuth/Google] Token exchange failed ({token_res.status_code}): {token_res.text}')
        return RedirectResponse('/?auth=failed')

    access_token = token_res.json().get('access_token')
    user_res = http_requests.get(
        GOOGLE_USERINFO_URL,
        headers={'Authorization': f'Bearer {access_token}'},
        timeout=HTTP_TIMEOUT,
    )
    if user_res.status_code != 200:
        log.warning(f'[OAuth/Google] userinfo failed ({user_res.status_code}): {user_res.text}')
        return RedirectResponse('/?auth=failed')

    google_user = user_res.json()
    user_id = google_user.get('sub')
    if not user_id:
        log.warning('[OAuth/Google] userinfo response missing sub (unique id)')
        return RedirectResponse('/?auth=failed')

    email = google_user.get('email', '')
    name = google_user.get('name') or (email.split('@')[0] if email else 'Player')
    avatar_url = google_user.get('picture') or f"https://ui-avatars.com/api/?name={name}&background=4285F4&color=fff&size=64"
    _finish_login(
        request, uid=user_id, username=name, email=email,
        email_verified=google_user.get('email_verified', False),
        avatar=avatar_url, auth_type='google', ip=ip,
    )
    return RedirectResponse('/?auth=success')

# ── Session routes ───────────────────────────────────────────────────────────
@app.get('/api/me')
def api_me(request: Request):
    user = request.session.get('user')
    if not user:
        return {'logged_in': False}

    # Re-check admin status against the current ADMIN_EMAIL each time, in case it changed since login.
    is_admin = _session_is_admin(user)
    if is_admin != user.get('is_admin', False):
        session_user = dict(request.session['user'])
        session_user['is_admin'] = is_admin
        request.session['user'] = session_user

    live = _apply_live_stats(dict(request.session['user']))
    return {'logged_in': True, **live, 'is_admin': is_admin}

@app.get('/api/check-auth')
def api_check_auth(request: Request):
    user = request.session.get('user')
    if not user:
        return {'logged_in': False}
    return {'logged_in': True, 'user': _apply_live_stats(dict(user))}

@app.get('/logout')
def logout(request: Request):
    u = request.session.pop('user', None)
    if u:
        _log_activity(u.get('discord_id', ''), "logout")
    request.session.clear()
    return RedirectResponse('/')

@app.post('/api/profile')
async def api_update_profile(request: Request):
    user = request.session.get('user')
    if not user:
        return _json({'ok': False, 'error': 'Not logged in'}, 401)

    data = await _get_json(request)
    nickname = str(data.get('nickname', '')).strip()[:50]
    bio = str(data.get('bio', '')).strip()[:500]

    def work():
        updates = {}
        if nickname:
            updates['name'] = nickname
        updates['bio'] = bio
        save_stats(user.get('auth_type', 'discord'), user['discord_id'], updates)
        _log_activity(user.get('discord_id', ''), 'profile_update')

    try:
        await run_in_threadpool(work)
        session_user = dict(request.session['user'])
        session_user['username'] = nickname or user.get('username')
        session_user['bio'] = bio
        request.session['user'] = session_user
        return {'ok': True, 'message': 'Profile updated.'}
    except Exception as e:
        log.error(f"[profile update] Error: {e}")
        return _json({'ok': False, 'error': 'Something went wrong.'}, 500)

@app.delete('/api/delete-account')
def api_delete_account(request: Request):
    ip = _get_real_ip(request)
    user = request.session.get('user')
    if not user:
        return _json({'ok': False, 'error': 'Not logged in'}, 401)

    if _session_is_admin(user):
        return _json({'ok': False, 'error': 'Admin accounts cannot self-delete.'}, 403)

    if _strict_rate_limit(ip, max_calls=5, window=300, scope='delete'):
        return _json({'ok': False, 'error': 'Too many attempts. Please wait a moment.'}, 429)

    user_id = user.get('discord_id')
    auth_type = user.get('auth_type') or 'discord'
    if not user_id:
        return _json({'ok': False, 'error': 'Invalid session.'}, 400)

    conn = None
    try:
        conn = _db()
        account_key = _stats_key(auth_type, user_id)
        remove_account_from_guild(conn, account_key)      # fixes the guild's own member list first
        conn.execute("DELETE FROM player_stats WHERE account_key = ?", (account_key,))
        conn.execute("DELETE FROM guild_members WHERE account_key = ?", (account_key,))
        conn.execute("DELETE FROM account_links WHERE discord_id = ?", (str(user_id),))
        conn.commit()

        _log_activity(user_id, 'account_deleted', f'self-delete from {ip}')
        log.info(f"[Account] User {user_id} deleted their own account")

        request.session.clear()
        return {'ok': True, 'message': 'Account deleted.'}
    except Exception as e:
        log.error(f"[delete-account] Error: {e}")
        return _json({'ok': False, 'error': 'Something went wrong. Please try again.'}, 500)
    finally:
        if conn:
            conn.close()

# ── Admin routes ─────────────────────────────────────────────────────────────
def _admin_denied(user):
    if not _session_is_admin(user):
        return _json({'ok': False, 'error': 'Admin access required'}, 403)
    return None

@app.get('/api/admin/users')
def api_admin_users(request: Request):
    """Account list for the admin panel, built from player_stats (accounts are Discord/Google only)."""
    user = request.session.get('user')
    denied = _admin_denied(user)
    if denied:
        return denied

    conn = None
    try:
        conn = _db()
        rows = conn.execute("SELECT account_key, data, last_updated FROM player_stats").fetchall()
        links = {
            r['discord_id']: r['last_login']
            for r in conn.execute("SELECT discord_id, last_login FROM account_links").fetchall()
        }

        user_list = []
        for r in rows:
            try:
                d = json.loads(r['data'])
            except Exception:
                d = {}
            atype, uid = _split_account(r['account_key'])
            sid = uid if atype == 'discord' else f'google:{uid}'
            user_list.append({
                'id': r['account_key'],
                'nickname': d.get('name') or uid,
                'avatar': d.get('avatar') or 'https://api.dicebear.com/7.x/avataaars/svg?seed=default',
                'email': '—',  # email isn't persisted outside the session
                'is_admin': False,  # computed live on login from ADMIN_EMAIL; not stored per-account
                'created_at': links.get(uid) or r['last_updated'],
                'last_login': links.get(uid) or 'Never',
                'is_online': sid in _session_log and bool(_session_log.get(sid)),
                'provider': atype,
            })

        return {
            'ok': True,
            'users': user_list,
            'stats': {
                'total': len(user_list),
                'online': sum(1 for u in user_list if u.get('is_online')),
                'admins': 1 if ADMIN_EMAIL else 0,
            }
        }
    except Exception as e:
        log.error(f"[Admin/users] Error: {e}")
        return _json({'ok': False, 'error': str(e)}, 500)
    finally:
        if conn:
            conn.close()

@app.post('/api/admin/users/{user_id}/toggle-admin')
def api_admin_toggle_admin(user_id: str, request: Request):
    denied = _admin_denied(request.session.get('user'))
    if denied:
        return denied
    return _json({
        'ok': False,
        'error': 'Admin status is set by the ADMIN_EMAIL environment variable, '
                 'not per-account — update ADMIN_EMAIL to change who has admin access.'
    }, 400)

@app.delete('/api/admin/users/{user_id}')
def api_admin_delete_user(user_id: str, request: Request):
    user = request.session.get('user')
    denied = _admin_denied(user)
    if denied:
        return denied

    own_key = _stats_key(user.get('auth_type', 'discord'), user.get('discord_id', ''))
    if user_id == own_key:
        return _json({'ok': False, 'error': 'Cannot delete your own account'}, 400)

    conn = None
    try:
        conn = _db()
        atype, uid = _split_account(user_id)
        key = _stats_key(atype, uid)
        remove_account_from_guild(conn, key)
        conn.execute("DELETE FROM player_stats WHERE account_key = ?", (key,))
        conn.execute("DELETE FROM guild_members WHERE account_key = ?", (key,))
        conn.execute("DELETE FROM account_links WHERE discord_id = ?", (uid,))
        conn.commit()
        log.info(f"[Admin] Account {key} deleted by admin")
        return {'ok': True, 'message': 'Account deleted successfully'}
    except Exception as e:
        log.error(f"[Admin/delete] Error: {e}")
        return _json({'ok': False, 'error': str(e)}, 500)
    finally:
        if conn:
            conn.close()

@app.post('/api/admin/announce')
async def api_admin_announce(request: Request):
    user = request.session.get('user')
    denied = _admin_denied(user)
    if denied:
        return denied
    body = await _get_json(request)
    message = str(body.get('message') or '').strip()[:1000]
    if not message:
        return _json({'ok': False, 'error': 'message required'}, 400)

    def work():
        conn = _db()
        try:
            conn.execute("INSERT INTO announcements (message, author) VALUES (?, ?)",
                         (message, user.get('username')))
            conn.commit()
        finally:
            conn.close()
    await run_in_threadpool(work)
    return {'ok': True}

@app.get('/api/announcements')
def api_announcements():
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT message, author, created_at FROM announcements ORDER BY id DESC LIMIT 10"
        ).fetchall()
    finally:
        conn.close()
    return {'items': [dict(r) for r in rows]}

@app.post('/api/admin/give-gold')
async def api_admin_give_gold(request: Request):
    denied = _admin_denied(request.session.get('user'))
    if denied:
        return denied
    body = await _get_json(request)
    user_id = body.get('user_id')
    amount = body.get('amount')
    if not user_id or isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return _json({'ok': False, 'error': 'user_id and amount required'}, 400)
    atype, uid = _split_account(str(user_id))

    def work():
        new = save_stats(atype, uid, {}, deltas={'gold': int(amount)})   # atomic add, no lost updates
        load_game_data()
        return new['gold']
    gold = await run_in_threadpool(work)
    return {'ok': True, 'gold': gold}

@app.get('/api/admin/players')
def api_admin_players(request: Request):
    denied = _admin_denied(request.session.get('user'))
    if denied:
        return denied
    with _lock:
        data = cached_data.copy()      # refreshed every UPDATE_INTERVAL and after every admin edit
    q = (request.query_params.get('q') or '').strip().lower()
    players = []
    for pid, pd in data.get('players', {}).items():
        name = pd.get('name', 'Unknown')
        if q and q not in str(name).lower() and q not in str(pid).lower():
            continue
        players.append({
            'id': pid, 'name': name, 'level': pd.get('level', 1),
            'class': pd.get('char_class', '—'), 'gold': pd.get('gold', 0),
            'gems': pd.get('gems', 0), 'special_coins': pd.get('special_coins', 0),
            'xp': pd.get('xp', 0), 'guild': pd.get('guild', '—'),
            'wins': pd.get('battles_won', 0),
        })
    players.sort(key=lambda p: (-p['level'], str(p['name'])))
    return {'ok': True, 'players': players, 'total': len(players)}

@app.get('/api/admin/players/{user_id}')
def api_admin_player_detail(user_id: str, request: Request):
    denied = _admin_denied(request.session.get('user'))
    if denied:
        return denied
    atype, uid = _split_account(user_id)
    return {'ok': True, 'player': get_stats(atype, uid)}

@app.post('/api/admin/players/{user_id}')
async def api_admin_update_player(user_id: str, request: Request):
    denied = _admin_denied(request.session.get('user'))
    if denied:
        return denied
    body = await _get_json(request)
    updates = body.get('updates')
    if not isinstance(updates, dict) or not updates:
        return _json({'ok': False, 'error': 'updates object required'}, 400)
    atype, uid = _split_account(user_id)

    def work():
        if 'guild' in updates:
            new_guild = str(updates['guild']).strip()
            if new_guild and new_guild != '—':
                join_guild(atype, uid, new_guild)
            else:
                leave_guild(atype, uid)
        merged = save_stats(atype, uid, updates)
        load_game_data()
        _log_activity(uid, "admin_edit_player", f"Admin updated fields: {', '.join(updates.keys())}")
        return merged
    merged = await run_in_threadpool(work)
    return {'ok': True, 'player': merged}

@app.get('/api/admin/stats')
def api_admin_stats(request: Request):
    denied = _admin_denied(request.session.get('user'))
    if denied:
        return denied

    conn = None
    try:
        conn = _db()
        recent_activity = conn.execute(
            "SELECT action, detail, ts FROM activity_log ORDER BY id DESC LIMIT 20"
        ).fetchall()
        return {
            'ok': True,
            'stats': {
                'active_sessions': len(_session_log),
                'total_requests': _global_req_count.get('n', 0),
                'banned_ips': len([v for v in _ip_banned.values() if time.time() < v])
            },
            'recent_activity': [
                {'action': r['action'], 'detail': r['detail'], 'ts': r['ts']}
                for r in recent_activity
            ]
        }
    except Exception as e:
        return _json({'ok': False, 'error': str(e)}, 500)
    finally:
        if conn:
            conn.close()

# ── Page routes ──────────────────────────────────────────────────────────────
@app.head('/')
@app.get('/')
@app.get('/dashboard')
async def index(request: Request):
    with _lock:
        data = cached_data.copy()
        updated = last_updated

    world_state = data.get('world_state', {})
    players_list = []

    for pid, pd in data.get('players', {}).items():
        xp = pd.get('xp', 0)
        xp_needed = pd.get('xp_needed', 100) or 100
        players_list.append({
            'id': pid,
            'name': pd.get('name', 'Unknown'),
            'level': pd.get('level', 0),
            'class': pd.get('char_class', '—'),
            'gold': pd.get('gold', 0),
            'guild': pd.get('guild', '—'),
            'xp': xp,
            'xp_needed': xp_needed,
            'xp_pct': round(xp / xp_needed * 100, 1),
        })

    players_list.sort(key=lambda p: (-p['level'], str(p['name'])))

    guilds_list = [
        {'name': g, 'member_count': len(d.get('members', []))}
        for g, d in data.get('guilds', {}).items()
    ]
    guilds_list.sort(key=lambda g: (-g['member_count'], g['name']))

    return templates.TemplateResponse(request, 'dashboard.html', {
        'num_players': len(players_list),
        'num_guilds': len(guilds_list),
        'players': players_list,
        'guilds': guilds_list,
        'invasion_active': world_state.get('invasion_active', False),
        'invasion_details': world_state.get('current_invasion', 'None'),
        'king': world_state.get('king', '—'),
        'season': world_state.get('season', 'Normal'),
        'weather': world_state.get('weather', 'Clear'),
        'active_events': world_state.get('active_events', []),
        'last_updated': updated.strftime('%Y-%m-%d %H:%M:%S') if updated else 'Never',
    })

def _page(path, template):
    async def handler(request: Request):
        return templates.TemplateResponse(request, template, {})
    handler.__name__ = 'page_' + template.replace('.html', '').replace('-', '_')
    app.get(path)(handler)

for _path, _tpl in (
    ('/battle-log', 'battle-log.html'), ('/profile', 'profile.html'),
    ('/achievements', 'achievements.html'), ('/leaderboard', 'leaderboard.html'),
    ('/inventory', 'inventory.html'), ('/quest-board', 'quest-board.html'),
    ('/guild-hall', 'guild-hall.html'), ('/settings', 'settings.html'),
    ('/hunting-grounds', 'hunting-grounds.html'), ('/tower', 'tower.html'),
    ('/shop', 'shop.html'), ('/community', 'community.html'),
):
    _page(_path, _tpl)

# ── API routes ───────────────────────────────────────────────────────────────
@app.get('/api/stats')
async def api_stats():
    with _lock:
        data = cached_data.copy()
        updated = last_updated

    ws = data.get('world_state', {})
    return {
        'num_players': len(data.get('players', {})),
        'num_guilds': len(data.get('guilds', {})),
        'last_updated': updated.strftime('%Y-%m-%d %H:%M:%S') if updated else None,
        'total_cmds': ws.get('total_commands', 0),
        'battles': ws.get('total_battles', 0),
        'uptime': ws.get('uptime', '—'),
        'cmd_count': ws.get('command_count', 0),
    }

@app.get('/api/leaderboard')
async def api_leaderboard():
    with _lock:
        data = cached_data.copy()

    pl = []
    for pid, pd in data.get('players', {}).items():
        xp = pd.get('xp', 0)
        xp_needed = pd.get('xp_needed', 100) or 100
        pl.append({
            'id': pid,
            'name': pd.get('name', 'Unknown'),
            'level': pd.get('level', 0),
            'class': pd.get('char_class', '—'),
            'gold': pd.get('gold', 0),
            'gems': pd.get('gems', pd.get('Gems', 0)),
            'guild': pd.get('guild', '—'),
            'xp': xp,
            'xp_needed': xp_needed,
            'xp_pct': round(xp / xp_needed * 100, 1),
            'wins': pd.get('battles_won', 0),
            'prestige': pd.get('prestige_level', 0),
            'avatar': pd.get('avatar'),
        })

    pl.sort(key=lambda p: (-p['level'], -p['xp'], str(p['name'])))
    return {'leaderboard': pl[:50], 'total': len(pl)}

@app.get('/api/activity/{user_id}')
def api_activity(user_id: str, request: Request):
    # Contains login IPs: only the account owner (or an admin) may read it.
    user = request.session.get('user')
    if not user:
        return _json({'items': []}, 401)
    if not _may_access_account(user, user_id):
        return _json({'items': []}, 403)
    # activity_log is keyed by the plain account id
    plain_id = _split_account(user_id)[1]
    conn = None
    try:
        conn = _db()
        rows = conn.execute(
            "SELECT action, detail, ts FROM activity_log "
            "WHERE user_id=? ORDER BY id DESC LIMIT 30",
            (str(plain_id),),
        ).fetchall()
        return {'items': [{'action': r['action'], 'detail': r['detail'], 'ts': r['ts']} for r in rows]}
    except Exception as e:
        log.error(f"[api/activity] error: {e}")
        return _json({'items': []}, 500)
    finally:
        if conn:
            conn.close()

@app.get('/api/inventory/{user_id}')
def api_inventory(user_id: str, request: Request):
    user = request.session.get('user')
    if not user:
        return _json({'items': []}, 401)
    if not _may_access_account(user, user_id):
        return _json({'items': []}, 403)

    # A bare id means "this user's own account type"; an admin passing a bare id gets a discord account.
    if ':' in user_id:
        atype, uid = _split_account(user_id)
    else:
        atype, uid = user.get('auth_type', 'discord'), user_id
    game = get_stats(atype, uid)
    if not game:
        return _json({'items': []}, 404)

    raw_inventory = game.get('inventory')
    items = []

    if isinstance(raw_inventory, dict):
        for name, info in raw_inventory.items():
            if isinstance(info, dict):
                items.append({
                    'name': info.get('name', name),
                    'quantity': info.get('quantity', info.get('qty', 1)),
                    'rarity': info.get('rarity', 'common'),
                })
            else:
                items.append({'name': name, 'quantity': info, 'rarity': 'common'})
    elif isinstance(raw_inventory, list):
        for entry in raw_inventory:
            if isinstance(entry, dict):
                items.append({
                    'name': entry.get('name', 'Unknown Item'),
                    'quantity': entry.get('quantity', entry.get('qty', 1)),
                    'rarity': entry.get('rarity', 'common'),
                })
            else:
                items.append({'name': str(entry), 'quantity': 1, 'rarity': 'common'})

    return {'items': items}

@app.get('/api/auctions')
def api_auctions():
    conn = None
    try:
        conn = _db()
        rows = conn.execute(
            "SELECT * FROM auctions WHERE status='active' ORDER BY end_time ASC LIMIT 50"
        ).fetchall()

        auctions = []
        now = datetime.now(UTC)
        for r in rows:
            try:
                end = r['end_time']
                if isinstance(end, str):                 # e.g. a TEXT column left over from the migration
                    end = datetime.fromisoformat(end)
                if end.tzinfo is None:                   # TIMESTAMP columns come back naive (stored as UTC)
                    end = end.replace(tzinfo=UTC)
                tl = max(0, int((end - now).total_seconds()))
            except Exception:
                tl = 0

            auctions.append({
                'id': r['id'],
                'name': r['item_name'],
                'icon': '⚔️',
                'rarity': 'common',
                'category': 'weapon',
                'seller': str(r['seller_id']),
                'current_bid': r['current_bid'],
                'bids': 0,
                'time_left': tl,
                'top_bidder': str(r['highest_bidder']) if r['highest_bidder'] else None,
                'bid_history': [],
            })
        return {'auctions': auctions}
    except Exception as e:
        log.warning(f'[api/auctions] {e}')
        return _json({'auctions': []}, 200)
    finally:
        if conn:
            conn.close()

@app.get('/api/profile')
def api_profile(request: Request):
    user = request.session.get('user')
    if not user:
        return {'logged_in': False}

    stats = get_stats(user.get('auth_type', 'discord'), user['discord_id'])
    nickname = stats.get('name') or user.get('username')
    avatar = stats.get('avatar') or user.get('avatar')
    bio = stats.get('bio', '')
    is_admin = _session_is_admin(user)

    return {'logged_in': True, 'nickname': nickname, 'avatar': avatar, 'bio': bio,
            'is_admin': is_admin, 'stats': stats, **{k: stats.get(k) for k in
            ('level', 'gold', 'gems', 'special_coins', 'xp', 'xp_needed', 'char_class')}}

def run_dashboard():
    host = "0.0.0.0"
    port = int(os.environ.get("PORT", 25507))

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
    except OSError:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind((host, 0))
                free_port = s.getsockname()[1]
            log.warning(f"[Dashboard] Port {port} in use; switching to {free_port}")
            port = free_port
        except Exception as e:
            log.error(f"[Dashboard] Failed to find free port: {e}")
            raise SystemExit(1)

    uvicorn.run(app, host=host, port=port, log_level="info")

if __name__ == '__main__':
    run_dashboard()
