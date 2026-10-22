import os
import json
import urllib.request
import urllib.error
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from dotenv import load_dotenv
import json, os, threading, time, secrets, hashlib, hmac
from urllib.parse import urlencode
from datetime import datetime, timedelta, timezone,UTC
from collections import defaultdict
import sqlite3
import requests as http_requests
import logging
import socket
import uvicorn

load_dotenv()

# Setup logging
log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(name)s — %(message)s")

EMAILJS_PUBLIC_KEY = ("eMQUadu5q-_pONmwc")
EMAILJS_SERVICE_ID = ("service_miabeqg")
EMAILJS_TEMPLATE_ID = ("template_tntozpq")



def send_reset_email(email: str, otp: str):
    email = email.strip().lower()

    if not email:
        raise ValueError("Cannot send reset email: recipient is empty")

    payload = {
        "service_id": EMAILJS_SERVICE_ID,
        "template_id": EMAILJS_TEMPLATE_ID,
        "user_id": EMAILJS_PUBLIC_KEY,
        "template_params": {
            "to_email": email,
            "otp_code": otp,
            "from_name": "RPG Ghost",
        },
    }

    request = urllib.request.Request(
        "https://api.emailjs.com/api/v1.0/email/send",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            result = response.read().decode("utf-8", errors="replace")
            log.info("[EmailJS] HTTP %s: %s", response.status, result)

    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        log.error("[EmailJS] HTTP %s: %s", exc.code, details)
        log.info("[EmailJS] Recipient provided: %s", bool(email.strip()))
        raise RuntimeError(
            f"EmailJS failed ({exc.code}): {details[:500]}"
        ) from exc


# ── Load Configuration from .env ──────────────────────────────────────────────
DISCORD_CLIENT_ID = os.getenv('DISCORD_CLIENT_ID')
if not DISCORD_CLIENT_ID:
    raise ValueError("DISCORD_CLIENT_ID not found in .env file!")

DISCORD_CLIENT_SECRET = os.getenv('DISCORD_CLIENT_SECRET')
if not DISCORD_CLIENT_SECRET:
    raise ValueError("DISCORD_CLIENT_SECRET not found in .env file!")

DISCORD_REDIRECT_URI = os.getenv('DISCORD_REDIRECT_URI')
if not DISCORD_REDIRECT_URI:
    raise ValueError("DISCORD_REDIRECT_URI not found in .env file!")
DISCORD_REDIRECT_URI = DISCORD_REDIRECT_URI.strip()

DISCORD_API = 'https://discord.com/api'
RECAPTCHA_SECRET = os.getenv('RECAPTCHA_SECRET', '')
RECAPTCHA_VERIFY_URL = 'https://www.google.com/recaptcha/api/siteverify'
if not RECAPTCHA_SECRET:
    log.warning("[reCAPTCHA] RECAPTCHA_SECRET not set — reCAPTCHA verification is disabled.")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'veyra.db')

BOT_LINK_SECRET = os.getenv('BOT_LINK_SECRET')
if not BOT_LINK_SECRET:
    raise ValueError("BOT_LINK_SECRET not found in .env file!")

DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN', '')
DISCORD_GUILD_ID = '1466740140163731488'
DISCORD_BID_CHANNEL = 1466835513074778383
BOT_API_URL = os.getenv('BOT_API_URL', 'http://bot.rpghost.qzz.io:25509')

async def _call_bot(method: str, path: str, json_body: dict = None):
    """Call the bot's own HTTP API directly (two-way comms)."""
    import aiohttp
    headers = {"Authorization": f"Bearer {BOT_LINK_SECRET}"}
    async with aiohttp.ClientSession() as session:
        async with session.request(
            method, f"{BOT_API_URL}{path}", json=json_body, headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as res:
            return res.status, await res.json()

# ── In-memory storage ──────────────────────────────────────────────────
_link_tokens = {}
_rate_limits = {}
_session_log = {}
_ip_connections = defaultdict(int)
_ip_banned = {}
_global_req_count = {'n': 0}
_req_id_counter = 0

# ── DDoS Protection Settings ──────────────────────────────────────────────────
DDOS_BURST_LIMIT = 40
DDOS_GLOBAL_CAP = 500
DDOS_BAN_DURATION = 3600
DDOS_MAX_BODY_BYTES = 65536
DDOS_SYNC_MAX_BODY_BYTES = 8 * 1024 * 1024  # 8MB — /api/sync carries the bot's full game-data dump
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

# ── Database Initialization ──────────────────────────────────────────────────
def init_db():
    """Create all necessary tables if they don't exist."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")

    cur = conn.cursor()

    # User game data
    cur.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        gold INTEGER DEFAULT 0,
        gems INTEGER DEFAULT 0,
        special_coins INTEGER DEFAULT 0,
        xp INTEGER DEFAULT 0,
        level INTEGER DEFAULT 1,
        last_daily TIMESTAMP,
        last_weekly TIMESTAMP,
        pity_counter INTEGER DEFAULT 0,
        total_pulls INTEGER DEFAULT 0,
        unlocked_characters TEXT DEFAULT '[]',
        unlocked_weapons TEXT DEFAULT '[]'
    )""")

    # Player data (JSON blob)
    cur.execute("""CREATE TABLE IF NOT EXISTS players (
        user_id INTEGER PRIMARY KEY,
        data TEXT,
        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    # Guild data
    cur.execute("""CREATE TABLE IF NOT EXISTS guilds (
        name TEXT PRIMARY KEY,
        data TEXT,
        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    # World state
    cur.execute("""CREATE TABLE IF NOT EXISTS world_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        data TEXT,
        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    # Account links
    cur.execute("""CREATE TABLE IF NOT EXISTS account_links (
        discord_id TEXT PRIMARY KEY,
        linked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_login TIMESTAMP
    )""")

    # Activity log
    cur.execute("""CREATE TABLE IF NOT EXISTS activity_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        action TEXT,
        detail TEXT,
        ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    # Email/password authentication with admin column
    cur.execute("""CREATE TABLE IF NOT EXISTS auth_users (
        auth_id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        nickname TEXT,
        avatar TEXT,
        bio TEXT,
        is_admin INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_login TIMESTAMP
    )""")

    # Migrate older auth_users tables that predate newer columns.
    # CREATE TABLE IF NOT EXISTS does not alter an existing table, so if this
    # table was created before a column existed, it has to be added here.
    existing_cols = {row[1] for row in cur.execute("PRAGMA table_info(auth_users)").fetchall()}
    required_cols = {
        'nickname': "TEXT",
        'avatar': "TEXT",
        'bio': "TEXT",
        'is_admin': "INTEGER DEFAULT 0",
        'created_at': "TIMESTAMP",
        'last_login': "TIMESTAMP",
    }
    for col_name, col_type in required_cols.items():
        if col_name not in existing_cols:
            cur.execute(f"ALTER TABLE auth_users ADD COLUMN {col_name} {col_type}")
            log.info(f"[DB Migration] Added missing column '{col_name}' to auth_users")

    # Auctions
    cur.execute("""CREATE TABLE IF NOT EXISTS auctions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_name TEXT NOT NULL,
        seller_id INTEGER NOT NULL,
        current_bid INTEGER NOT NULL DEFAULT 0,
        highest_bidder INTEGER,
        status TEXT NOT NULL DEFAULT 'active',
        end_time TIMESTAMP NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    conn.commit()
    conn.close()

# ── Secret Key ─────────────────────────────────────────────────────────────
def _get_or_create_secret_key():
    """Use FLASK_SECRET if set, otherwise persist a generated key to disk."""
    env_secret = os.environ.get('FLASK_SECRET')
    if env_secret:
        return env_secret

    secret_file = os.path.join(BASE_DIR, '.flask_secret_key')
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

UPDATE_INTERVAL = 30
cached_data = {}
last_updated = None
_lock = threading.Lock()

# ── Password Helpers ──────────────────────────────────────────────────────────
def _hash_password(password, salt=None):
    """PBKDF2-HMAC-SHA256 password hashing."""
    if salt is None:
        salt = secrets.token_hex(16)
    iterations = 260_000
    derived = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), iterations)
    return f"pbkdf2${iterations}${salt}${derived.hex()}"

def _verify_password(password, stored):
    """Verify a password against a stored hash."""
    try:
        if stored.startswith('pbkdf2$'):
            _, iterations, salt, hex_digest = stored.split('$', 3)
            iterations = int(iterations)
            derived = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), iterations)
            return hmac.compare_digest(derived.hex(), hex_digest)
        if ':' in stored:
            salt, hash_value = stored.split(':', 1)
            computed = hashlib.sha256((salt + password).encode()).hexdigest()
            return hmac.compare_digest(computed, hash_value)
        return hmac.compare_digest(hashlib.sha256(password.encode()).hexdigest(), stored)
    except Exception as e:
        log.warning(f"[auth] password verify error: {e}")
        return False

def _db():
    """Get a database connection."""
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL;")
    c.row_factory = sqlite3.Row
    return c

def _log_activity(user_id, action, detail=""):
    """Log user activity."""
    try:
        conn = _db()
        conn.execute(
            "INSERT INTO activity_log (user_id, action, detail) VALUES (?,?,?)",
            (str(user_id), action, detail),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f"[activity_log] write failed: {e}")

def _get_real_ip(request: Request):
    """Get the real client IP.

    SECURITY: X-Forwarded-For is attacker-controlled input — a direct client
    can set it to anything. Behind a single trusted reverse proxy (Render),
    the proxy appends the actual client IP as the *last* hop, so we read the
    last entry (what our proxy observed) rather than the first (whatever the
    client claimed), which a client cannot forge away. If the header is
    missing entirely we fall back to the raw socket peer address.
    """
    xff = request.headers.get('X-Forwarded-For', '')
    if xff:
        parts = [p.strip() for p in xff.split(',') if p.strip()]
        if parts:
            return parts[-1]
    return request.client.host if request.client else '0.0.0.0'

def _is_banned(ip):
    """Check if an IP is currently banned."""
    expires = _ip_banned.get(ip)
    if expires is None:
        return False
    if time.time() > expires:
        del _ip_banned[ip]
        return False
    return True

def _ban_ip(ip, reason='', duration=DDOS_BAN_DURATION):
    """Ban an IP address."""
    _ip_banned[ip] = time.time() + duration
    log.warning(f"[DDoS] BANNED {ip} for {duration}s — {reason}")

def _strict_rate_limit(ip, max_calls=3, window=60):
    """Strict rate limiter for sensitive endpoints."""
    now = time.time()
    key = f'strict:{ip}'
    hits = [t for t in _rate_limits.get(key, []) if now - t < window]
    _rate_limits[key] = hits
    if len(hits) >= max_calls:
        if len(hits) >= max_calls * 3:
            _ban_ip(ip, f"rate abuse on sensitive endpoint ({len(hits)} hits)")
        return True
    _rate_limits[key].append(now)
    return False

def _json(content: dict, status_code: int = 200):
    """Helper mirroring Flask's `jsonify(...), status_code` pattern."""
    return JSONResponse(content=content, status_code=status_code)

async def _get_json(request: Request) -> dict:
    """Helper mirroring Flask's `request.get_json(silent=True) or {}`."""
    try:
        return await request.json()
    except Exception:
        return {}

ADMIN_EMAIL = os.getenv('ADMIN_EMAIL')
_ADMIN_PASSWORD_ENV = os.getenv('ADMIN_PASSWORD')
_ADMIN_PASSWORD_GENERATED = False
if not _ADMIN_PASSWORD_ENV:
    _ADMIN_PASSWORD_ENV = secrets.token_urlsafe(18)
    _ADMIN_PASSWORD_GENERATED = True
ADMIN_PASSWORD = _ADMIN_PASSWORD_ENV
ADMIN_PASSWORD_HASH = _hash_password(ADMIN_PASSWORD) if ADMIN_EMAIL else None

def ensure_admin_user():
    """Create the admin user if it doesn't exist yet. No-op if ADMIN_EMAIL
    is not configured, since there is no hardcoded fallback account anymore."""
    if not ADMIN_EMAIL:
        log.warning("[Admin] ADMIN_EMAIL not set — skipping admin account bootstrap.")
        return

    conn = None
    try:
        conn = _db()
        existing = conn.execute(
            "SELECT auth_id FROM auth_users WHERE email = ?", (ADMIN_EMAIL,)
        ).fetchone()

        if not existing:
            conn.execute(
                """INSERT INTO auth_users (email, password_hash, nickname, avatar, bio, is_admin, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (ADMIN_EMAIL, ADMIN_PASSWORD_HASH, "Admin",
                 "https://api.dicebear.com/7.x/avataaars/svg?seed=Admin&backgroundColor=bc13fe",
                 "System Administrator", 1, datetime.now(timezone.utc).isoformat())
            )
            conn.commit()
            log.info(f"[Admin] Admin account created with email: {ADMIN_EMAIL}")
            if _ADMIN_PASSWORD_GENERATED:
                log.warning(
                    "[Admin] ADMIN_PASSWORD was not set — generated a random one-time "
                    f"password for {ADMIN_EMAIL}: {ADMIN_PASSWORD}  "
                    "Log in now and change it, then set ADMIN_PASSWORD/remove this from logs."
                )
    except Exception as e:
        log.error(f"[Admin] Failed to create admin account: {e}")
    finally:
        if conn:
            conn.close()

# ── Game Data Helpers ────────────────────────────────────────────────────────
def get_player_game_data(user_id):
    """Get player game data from database."""
    conn = None
    try:
        conn = _db()
        row = conn.execute("SELECT data FROM players WHERE user_id=?", (user_id,)).fetchone()
        if row:
            return json.loads(row['data'])

        row = conn.execute(
            "SELECT gold, gems, special_coins, xp, level FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row:
            return {k: row[k] for k in row.keys()}
    except Exception as e:
        log.error(f'[DB] get_player_game_data({user_id}) error: {e}')
    finally:
        if conn:
            conn.close()
    return {}

def load_game_data():
    """Load game data from database into cache."""
    global cached_data, last_updated
    try:
        init_db()
        conn = _db()

        players = {}
        for row in conn.execute("SELECT user_id, data FROM players").fetchall():
            try:
                players[str(row['user_id'])] = json.loads(row['data'])
            except Exception:
                pass

        guilds = {}
        for row in conn.execute("SELECT name, data FROM guilds").fetchall():
            try:
                guilds[row['name']] = json.loads(row['data'])
            except Exception:
                pass

        world_state = {}
        row = conn.execute("SELECT data FROM world_state WHERE id=1").fetchone()
        if row:
            try:
                world_state = json.loads(row['data'])
            except Exception:
                pass

        conn.close()

        with _lock:
            cached_data = {'players': players, 'guilds': guilds, 'world_state': world_state}
            last_updated = datetime.now(timezone.utc)

        log.info(f"[Dashboard] Refreshed — {len(players)} players, {len(guilds)} guilds")
    except Exception as e:
        log.error(f"[Dashboard] load_game_data error: {e}")

def _bg_updater():
    """Background thread to refresh game data."""
    while True:
        load_game_data()
        time.sleep(UPDATE_INTERVAL)

# ── Password Reset (in-memory) ────────────────────────────────────────────
_reset_tokens = {}

def _purge_reset_tokens():
    """Remove expired password-reset tokens."""
    now = datetime.now(timezone.utc)
    expired = [k for k, v in _reset_tokens.items() if now > v['expires_at']]
    for k in expired:
        del _reset_tokens[k]

# ── FastAPI App Setup ─────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle, replacing Flask's imperative run_dashboard() setup."""
    init_db()
    ensure_admin_user()
    load_game_data()
    threading.Thread(target=_bg_updater, daemon=True).start()
    yield
    # (no shutdown cleanup needed)

app = FastAPI(lifespan=lifespan)

# SECURITY: default to HTTPS-only session cookies so the signed session (which
# carries login state) can't be sniffed over plain HTTP. Opt out explicitly via
# SESSION_HTTPS_ONLY=false only for local non-TLS development.
SESSION_HTTPS_ONLY = os.getenv('SESSION_HTTPS_ONLY', 'true').strip().lower() not in ('false', '0', 'no')
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    max_age=SESSION_LIFETIME_SECONDS,
    same_site="lax",
    https_only=SESSION_HTTPS_ONLY,
)

from routes_profile import register_profile_routes
register_profile_routes(app)


templates = Jinja2Templates(directory=os.path.join(BASE_DIR, 'templates'))

STATIC_DIR = os.path.join(BASE_DIR, 'static')
if not os.path.isdir(STATIC_DIR):
    try:
        os.makedirs(STATIC_DIR, exist_ok=True)
        log.info(f"[static] Created missing static directory at {STATIC_DIR}")
    except Exception as e:
        log.warning(f"[static] Could not create static dir {STATIC_DIR}: {e}")
app.mount('/static', StaticFiles(directory=STATIC_DIR), name='static')

# ── DDoS Protection Middleware ──────────────────────────────────────────────
def _add_security_headers(response, req_id=None):
    """Shared header injection, applied on every exit path (mirrors Flask's
    after_request, which runs even when before_request short-circuits)."""
    if req_id:
        response.headers['X-Request-ID'] = req_id
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    return response

@app.middleware("http")
async def ddos_guard(request: Request, call_next):
    """Protect the app from DDoS and abuse (replaces before_request/after_request)."""
    global _req_id_counter
    ip = _get_real_ip(request)

    if _is_banned(ip):
        log.info(f"[DDoS] Blocked banned IP: {ip}")
        return _add_security_headers(PlainTextResponse("Forbidden", status_code=403))

    # Render's own health checker (and most uptime monitors) sends HEAD requests
    # with generic HTTP-library user-agents. Those would otherwise get caught by
    # DDOS_BLOCKED_UA_FRAGS below and get Render's health check IP banned,
    # which stops the app from ever passing Render's port/startup check.
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

    _ip_connections[ip] += 1
    if _ip_connections[ip] > DDOS_BURST_LIMIT:
        _ban_ip(ip, f"burst: {_ip_connections[ip]} concurrent")
        _ip_connections[ip] = 0
        return _add_security_headers(
            PlainTextResponse("Too Many Requests", status_code=429, headers={'Retry-After': '60'})
        )

    cl = request.headers.get('content-length')
    # /api/sync carries the bot's full players/guilds/world_state dump, which
    # easily exceeds the normal per-request cap as the player count grows.
    # It's already authenticated separately (BOT_LINK_SECRET) inside the route,
    # so it gets a much higher ceiling instead of the generic DDoS limit.
    body_limit = DDOS_SYNC_MAX_BODY_BYTES if request.url.path == '/api/sync' else DDOS_MAX_BODY_BYTES
    if cl and int(cl) > body_limit:
        log.warning(f"[DDoS] Body too large from {ip}: {cl} bytes (limit {body_limit})")
        # Roll back the connection count we already incremented above.
        _ip_connections[ip] = max(0, _ip_connections.get(ip, 1) - 1)
        return _add_security_headers(PlainTextResponse("Request Too Large", status_code=413))

    _global_req_count['n'] += 1
    _req_id_counter += 1
    request.state.req_id = f"{_req_id_counter:08x}"
    request.state.req_ip = ip
    request.state.req_start = time.monotonic()

    response = await call_next(request)

    _ip_connections[ip] = max(0, _ip_connections.get(ip, 1) - 1)
    _global_req_count['n'] = max(0, _global_req_count['n'] - 1)

    return _add_security_headers(response, req_id=getattr(request.state, 'req_id', None))

# ── Bot → Dashboard Sync ──────────────────────────────────────────────────────
# The bot and dashboard now run on separate hosts (AxisNodes + Render) and no
# longer share a filesystem, so the old approach of both processes reading the
# same local veyra.db file no longer works. Instead, the bot pushes its
# players/guilds/world_state data here over HTTPS, authenticated with the same
# BOT_LINK_SECRET already used for account linking. This endpoint writes that
# data into this dashboard's own local DB and refreshes the in-memory cache.
@app.post('/api/sync')
async def api_sync(request: Request):
    """Receive a full game-data sync push from the bot."""
    data = await _get_json(request)

    if not hmac.compare_digest(str(data.get('bot_secret', '')), BOT_LINK_SECRET):
        log.warning("[SYNC] rejected push with invalid bot_secret")
        return _json({'ok': False, 'error': 'unauthorized'}, 401)

    players = data.get('players', {})
    guilds = data.get('guilds', {})
    world_state = data.get('world_state', {})

    if not isinstance(players, dict) or not isinstance(guilds, dict) or not isinstance(world_state, dict):
        return _json({'ok': False, 'error': 'malformed payload'}, 400)

    conn = None
    try:
        conn = _db()
        for user_id, pdata in players.items():
            conn.execute(
                "INSERT INTO players (user_id, data, last_updated) VALUES (?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(user_id) DO UPDATE SET data=excluded.data, last_updated=CURRENT_TIMESTAMP",
                (int(user_id), json.dumps(pdata)),
            )
        for name, gdata in guilds.items():
            conn.execute(
                "INSERT INTO guilds (name, data, last_updated) VALUES (?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(name) DO UPDATE SET data=excluded.data, last_updated=CURRENT_TIMESTAMP",
                (name, json.dumps(gdata)),
            )
        conn.execute(
            "INSERT INTO world_state (id, data, last_updated) VALUES (1, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(id) DO UPDATE SET data=excluded.data, last_updated=CURRENT_TIMESTAMP",
            (json.dumps(world_state),),
        )
        conn.commit()
    except Exception as e:
        log.error(f"[SYNC] failed to write payload: {e}")
        return _json({'ok': False, 'error': 'internal error'}, 500)
    finally:
        if conn:
            conn.close()

    load_game_data()  # refresh in-memory cache immediately instead of waiting for the 30s poll
    log.info(f"[SYNC] received push: {len(players)} players, {len(guilds)} guilds")
    return _json({'ok': True, 'received': {'players': len(players), 'guilds': len(guilds)}})

# NOTE: unauthenticated /api/admin/give-gold and /api/admin/announce handlers
# used to live here. They had no session/is_admin check and, because Starlette
# matches routes in registration order, they silently shadowed the properly
# admin-protected handlers of the same name defined later in this file —
# giving *any* anonymous caller the ability to grant gold or broadcast bot
# announcements. They have been removed; see api_admin_give_gold and
# api_admin_announce below for the real, auth-checked implementations.

# ── Discord OAuth ─────────────────────────────────────────────────────────────
@app.get('/login/discord')
async def discord_login(request: Request):
    """Redirect user to Discord for OAuth."""
    state = secrets.token_urlsafe(32)
    request.session['oauth_state'] = state
    params = {
        'client_id': DISCORD_CLIENT_ID,
        'redirect_uri': DISCORD_REDIRECT_URI,
        'response_type': 'code',
        'scope': 'identify email',
        'state': state,
    }
    oauth_url = f"{DISCORD_API}/oauth2/authorize?{urlencode(params)}"
    return RedirectResponse(oauth_url)

@app.get('/callback')
async def discord_callback(request: Request):
    """Handle Discord OAuth callback."""
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
    )

    if token_res.status_code != 200:
        log.warning(f'[OAuth] Token exchange failed ({token_res.status_code}): {token_res.text}')
        return RedirectResponse('/?auth=failed')

    access_token = token_res.json().get('access_token')

    user_res = http_requests.get(
        f'{DISCORD_API}/users/@me',
        headers={'Authorization': f'Bearer {access_token}'},
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

    game_data = get_player_game_data(user_id)

    request.session['user'] = {
        'discord_id': str(user_id),
        'username': discord_user.get('global_name') or discord_user.get('username', 'Player'),
        'email': discord_user.get('email', ''),
        'avatar': avatar_url,
        'level': game_data.get('level', 1),
        'gold': game_data.get('gold', 0),
        'gems': game_data.get('gems', 0),
        'special_coins': game_data.get('special_coins', 0),
        'char_class': game_data.get('char_class', '—'),
        'xp': game_data.get('xp', 0),
        'xp_needed': game_data.get('xp_needed', 100),
        'login_at': datetime.now(UTC).isoformat(),
        'ip': ip,
        'auth_type': 'discord',
        'is_admin': False
    }

    sid = str(user_id)
    _session_log.setdefault(sid, []).append({
        'ip': ip,
        'ua': request.headers.get('user-agent', '')[:120],
        'at': datetime.now(UTC).isoformat(),
    })
    _session_log[sid] = _session_log[sid][-10:]

    try:
        conn = _db()
        conn.execute(
            "INSERT INTO account_links (discord_id, last_login) VALUES (?,?) "
            "ON CONFLICT(discord_id) DO UPDATE SET last_login=excluded.last_login",
            (sid, datetime.now(UTC).isoformat()),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f"[login] account_links update failed: {e}")

    _log_activity(user_id, "login", f"from {ip}")
    log.info(f"[OAuth] Logged in: {request.session['user']['username']} (ID: {user_id})")

    return RedirectResponse('/?auth=success')

# ── Auth Routes ──────────────────────────────────────────────────────────────
@app.post('/api/signup')
async def api_signup(request: Request):
    """Register a new email/password account."""
    ip = _get_real_ip(request)

    if _strict_rate_limit(ip, max_calls=8, window=600):
        return _json({
            'ok': False,
            'error': 'Too many signup attempts. Please wait a few minutes.'
        }, 429)

    data = await _get_json(request)
    email = str(data.get('email', '')).strip().lower()
    password = str(data.get('password', '')).strip()
    nickname = str(data.get('nickname', '')).strip() or email.split('@')[0]
    recaptcha_token = data.get('recaptcha_token')

    if not email or '@' not in email:
        return _json({'ok': False, 'error': 'Please enter a valid email address.'}, 400)

    if not password or len(password) < 6:
        return _json({'ok': False, 'error': 'Password must be at least 6 characters.'}, 400)

    if len(password) > 128:
        return _json({'ok': False, 'error': 'Password is too long.'}, 400)

    common_passwords = ['password', 'password1', '12345678', '123456789', 'qwerty123',
                        'letmein', 'iloveyou', 'admin123', 'welcome1', 'monkey123']
    if password.lower() in common_passwords:
        return _json({'ok': False, 'error': 'That password is too common.'}, 400)

    conn = None
    try:
        conn = _db()
        existing = conn.execute(
            "SELECT auth_id FROM auth_users WHERE email = ?", (email,)
        ).fetchone()

        if existing:
            return _json({
                'ok': False,
                'error': 'This email is already registered.'
            }, 400)

        password_hash = _hash_password(password)
        avatar_url = f"https://ui-avatars.com/api/?name={nickname}&background=5865F2&color=fff&size=64"

        cursor = conn.execute(
            """INSERT INTO auth_users (email, password_hash, nickname, avatar, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (email, password_hash, nickname, avatar_url, datetime.now(UTC).isoformat())
        )
        auth_id = cursor.lastrowid
        conn.commit()

        _log_activity(auth_id, 'signup', f'email={email}')
        log.info(f"[Signup] New account created: {email} (ID: {auth_id})")

        request.session['user'] = {
            'discord_id': str(auth_id),
            'username': nickname,
            'email': email,
            'avatar': avatar_url,
            'level': 1,
            'gold': 100,
            'gems': 10,
            'special_coins': 0,
            'char_class': 'Adventurer',
            'xp': 0,
            'xp_needed': 100,
            'login_at': datetime.now(UTC).isoformat(),
            'ip': ip,
            'auth_type': 'email',
            'is_admin': False
        }

        sid = str(auth_id)
        _session_log.setdefault(sid, []).append({
            'ip': ip,
            'ua': request.headers.get('user-agent', '')[:120],
            'at': datetime.now(UTC).isoformat(),
        })
        _session_log[sid] = _session_log[sid][-10:]

        return _json({
            'ok': True,
            'logged_in': True,
            'message': 'Welcome to RPG Ghost! 🎮',
            'user': request.session['user'],
            'is_admin': False
        }, 201)

    except Exception as e:
        log.error(f"[Signup] Database error: {e}")
        return _json({
            'ok': False,
            'error': 'Something went wrong. Please try again.'
        }, 500)
    finally:
        if conn:
            conn.close()

@app.post('/api/login')
async def api_login(request: Request):
    """Log in with email and password."""
    ip = _get_real_ip(request)

    if _strict_rate_limit(ip, max_calls=10, window=300):
        return _json({
            'ok': False,
            'error': 'Too many login attempts. Please wait a moment.'
        }, 429)

    data = await _get_json(request)
    email = str(data.get('email', '')).strip()
    password = str(data.get('password', '')).strip()
    keep_signed_in = bool(data.get('keep_signed_in', False))  # noqa: F841 (see session note above)

    if not email or not password:
        return _json({'ok': False, 'error': 'Please fill in all fields.'}, 400)

    conn = None
    try:
        conn = _db()
        user = conn.execute(
            "SELECT auth_id, email, password_hash, nickname, avatar, bio, is_admin, created_at, last_login "
            "FROM auth_users WHERE email = ?",
            (email,)
        ).fetchone()

        if not user:
            return _json({
                'ok': False,
                'error': 'Invalid email or password.'
            }, 401)

        if not _verify_password(password, user['password_hash']):
            return _json({
                'ok': False,
                'error': 'Invalid email or password.'
            }, 401)

        # Update last login
        conn.execute(
            "UPDATE auth_users SET last_login = ? WHERE auth_id = ?",
            (datetime.now(UTC).isoformat(), user['auth_id'])
        )
        conn.commit()

        nickname = user['nickname'] or email.split('@')[0]
        avatar_url = user['avatar'] or f"https://ui-avatars.com/api/?name={nickname}&background=5865F2&color=fff&size=64"

        request.session['user'] = {
            'discord_id': str(user['auth_id']),
            'username': nickname,
            'email': user['email'],
            'avatar': avatar_url,
            'bio': user['bio'] or '',
            'level': 1,
            'gold': 100,
            'gems': 10,
            'special_coins': 0,
            'char_class': 'Adventurer',
            'xp': 0,
            'xp_needed': 100,
            'login_at': datetime.now(UTC).isoformat(),
            'ip': ip,
            'auth_type': 'email',
            'is_admin': bool(user['is_admin'])
        }

        sid = str(user['auth_id'])
        _session_log.setdefault(sid, []).append({
            'ip': ip,
            'ua': request.headers.get('user-agent', '')[:120],
            'at': datetime.now(UTC).isoformat(),
        })
        _session_log[sid] = _session_log[sid][-10:]

        _log_activity(user['auth_id'], 'login', f'from {ip}')
        log.info(f"[Login] {email} logged in successfully")

        return _json({
            'ok': True,
            'logged_in': True,
            'message': f'Welcome back, {nickname}! 🎉',
            'user': request.session['user'],
            'is_admin': bool(user['is_admin'])
        })

    except Exception as e:
        log.error(f"[Login] Error: {e}")
        return _json({
            'ok': False,
            'error': 'Something went wrong. Please try again.'
        }, 500)
    finally:
        if conn:
            conn.close()

@app.post('/api/register')
async def api_register_alias(request: Request):
    """Alias for /api/signup."""
    return await api_signup(request)

@app.get('/api/me')
async def api_me(request: Request):
    """Get current user info."""
    user = request.session.get('user')
    if not user:
        return {'logged_in': False}

    # The session's 'is_admin' flag is only set at login time, so an old
    # session cookie (or one created via Discord OAuth, which always logs in
    # as a non-admin) could report stale/incorrect admin status forever.
    # Re-check the database here so admin access always reflects reality,
    # the same way /api/profile already does.
    is_admin = bool(user.get('is_admin', False))
    if user.get('auth_type') == 'email':
        try:
            conn = _db()
            row = conn.execute(
                "SELECT is_admin FROM auth_users WHERE auth_id = ?",
                (int(user['discord_id']),)
            ).fetchone()
            conn.close()
            if row is not None:
                is_admin = bool(row['is_admin'])
        except Exception as e:
            log.warning(f"[api/me] is_admin re-check failed: {e}")

    if is_admin != user.get('is_admin', False):
        # Keep the session in sync so subsequent requests see the same value.
        session_user = dict(request.session['user'])
        session_user['is_admin'] = is_admin
        request.session['user'] = session_user

    return {'logged_in': True, **user, 'is_admin': is_admin}

@app.get('/api/check-auth')
async def api_check_auth(request: Request):
    """Check if user is logged in."""
    user = request.session.get('user')
    if not user:
        return {'logged_in': False}
    return {'logged_in': True, 'user': user}

@app.get('/logout')
async def logout(request: Request):
    """Log out the current user."""
    u = request.session.pop('user', None)
    if u:
        _log_activity(u.get('discord_id', ''), "logout")
    request.session.clear()
    return RedirectResponse('/')

@app.post('/api/profile')
async def api_update_profile(request: Request):
    """Update the logged-in user's nickname/bio."""
    user = request.session.get('user')
    if not user:
        return _json({'ok': False, 'error': 'Not logged in'}, 401)

    data = await _get_json(request)
    nickname = str(data.get('nickname', '')).strip()[:50]
    bio = str(data.get('bio', '')).strip()[:500]

    conn = None
    try:
        conn = _db()
        if user.get('auth_type') == 'email':
            conn.execute(
                "UPDATE auth_users SET nickname = ?, bio = ? WHERE auth_id = ?",
                (nickname or None, bio or None, int(user['discord_id']))
            )
            conn.commit()

        # Keep the session in sync so the sidebar/UI reflect the change immediately
        session_user = dict(request.session['user'])
        session_user['username'] = nickname or user.get('username')
        session_user['bio'] = bio
        request.session['user'] = session_user

        _log_activity(user.get('discord_id', ''), 'profile_update')
        return {'ok': True, 'message': 'Profile updated.'}
    except Exception as e:
        log.error(f"[profile update] Error: {e}")
        return _json({'ok': False, 'error': 'Something went wrong.'}, 500)
    finally:
        if conn:
            conn.close()

@app.delete('/api/delete-account')
async def api_delete_account(request: Request):
    """Allow a logged-in user to permanently delete their own account."""
    ip = _get_real_ip(request)
    user = request.session.get('user')
    if not user:
        return _json({'ok': False, 'error': 'Not logged in'}, 401)

    if user.get('is_admin', False):
        return _json({
            'ok': False,
            'error': 'Admin accounts cannot self-delete. Ask another admin to revoke your admin status first.'
        }, 403)

    if _strict_rate_limit(ip, max_calls=5, window=300):
        return _json({'ok': False, 'error': 'Too many attempts. Please wait a moment.'}, 429)

    data = await _get_json(request)
    password = str(data.get('password', ''))
    user_id = user.get('discord_id')
    auth_type = user.get('auth_type')

    if not user_id:
        return _json({'ok': False, 'error': 'Invalid session.'}, 400)

    conn = None
    try:
        conn = _db()

        if auth_type == 'email':
            row = conn.execute(
                "SELECT password_hash FROM auth_users WHERE auth_id = ?", (int(user_id),)
            ).fetchone()
            if not row:
                return _json({'ok': False, 'error': 'Account not found.'}, 404)
            if not password or not _verify_password(password, row['password_hash']):
                return _json({'ok': False, 'error': 'Incorrect password.'}, 401)
            conn.execute("DELETE FROM auth_users WHERE auth_id = ?", (int(user_id),))

        # Remove any game data tied to this account, regardless of auth type
        conn.execute("DELETE FROM players WHERE user_id = ?", (int(user_id),))
        conn.execute("DELETE FROM users WHERE user_id = ?", (int(user_id),))
        conn.execute("DELETE FROM account_links WHERE discord_id = ?", (str(user_id),))
        conn.commit()

        _log_activity(user_id, 'account_deleted', f'self-delete from {ip}')
        log.info(f"[Account] User {user_id} deleted their own account")

        request.session.pop('user', None)
        request.session.clear()

        return {'ok': True, 'message': 'Account deleted.'}
    except Exception as e:
        log.error(f"[delete-account] Error: {e}")
        return _json({'ok': False, 'error': 'Something went wrong. Please try again.'}, 500)
    finally:
        if conn:
            conn.close()


@app.post('/api/password/request-reset')
async def api_password_request_reset(request: Request):
    """Request a password reset code and send it through EmailJS."""
    ip = _get_real_ip(request)

    if _strict_rate_limit(ip, max_calls=5, window=600):
        return _json({
            'ok': False,
            'error': 'Too many requests. Please wait a few minutes.'
        }, 429)

    data = await _get_json(request)
    email = str(data.get('email', '')).strip().lower()

    if not email or '@' not in email:
        return _json({
            'ok': False,
            'error': 'Please enter a valid email address.'
        }, 400)

    _purge_reset_tokens()

    # Check whether this email belongs to an account.
    conn = None
    try:
        conn = _db()
        account = conn.execute(
            "SELECT auth_id FROM auth_users WHERE email = ?",
            (email,)
        ).fetchone()
    except Exception:
        log.exception("[Password Reset] Database lookup failed")
        return _json({
            'ok': False,
            'error': 'Unable to process the request right now.'
        }, 500)
    finally:
        if conn:
            conn.close()

    # Use a generic response so the endpoint doesn't reveal
    # whether an account exists.
    generic_response = {
        'ok': True,
        'message': 'If that email is registered, you will receive a reset code.'
    }

    if not account:
        return generic_response

    # Generate a six-digit OTP.
    otp = str(secrets.randbelow(900000) + 100000)

    # Send the OTP through EmailJS before storing it.
    try:
        await asyncio.to_thread(send_reset_email, email, otp)
    except Exception:
        log.exception(
            "[Password Reset] EmailJS delivery failed"
        )
        return _json({
            'ok': False,
            'error': 'Unable to send the reset email. Please try again later.'
        }, 500)

    # Store the OTP only after EmailJS accepts the email.
    _reset_tokens[email] = {
        'otp': otp,
        'expires_at': datetime.now(UTC) + timedelta(minutes=5)
    }

    log.info("[Password Reset] Reset email accepted by EmailJS")

    return generic_response

@app.post('/api/password/verify-reset')
async def api_password_reset(request: Request):
    """Reset password using OTP."""
    ip = _get_real_ip(request)

    if _strict_rate_limit(ip, max_calls=8, window=300):
        return _json({'ok': False, 'error': 'Too many attempts. Please wait a moment.'}, 429)

    data = await _get_json(request)
    email = str(data.get('email', '')).strip().lower()
    otp = str(data.get('otp', '')).strip()
    new_password = str(data.get('new_password', '')).strip()

    if not email:
        return _json({'ok': False, 'error': 'Email address is required.'}, 400)

    if not otp or len(otp) != 6:
        return _json({'ok': False, 'error': 'Please enter the 6-digit code.'}, 400)

    if len(new_password) < 6:
        return _json({'ok': False, 'error': 'Password must be at least 6 characters.'}, 400)

    _purge_reset_tokens()
    record = _reset_tokens.get(email)
    if not record:
        return _json({'ok': False, 'error': 'No reset request found.'}, 400)

    if datetime.now(UTC) > record['expires_at']:
        _reset_tokens.pop(email, None)
        return _json({'ok': False, 'error': 'The code has expired.'}, 400)

    if not hmac.compare_digest(record['otp'], otp):
        return _json({'ok': False, 'error': 'Incorrect code.'}, 400)

    conn = None
    try:
        conn = _db()
        existing = conn.execute("SELECT auth_id FROM auth_users WHERE email = ?", (email,)).fetchone()
        if not existing:
            return _json({'ok': False, 'error': 'No account found for that email.'}, 404)

        new_hash = _hash_password(new_password)
        conn.execute("UPDATE auth_users SET password_hash = ? WHERE email = ?", (new_hash, email))
        conn.commit()

        _reset_tokens.pop(email, None)
        _log_activity(existing['auth_id'], 'password_reset')

        return {'ok': True, 'message': 'Password updated successfully!'}

    except Exception as e:
        log.error(f"[Password Reset] Error: {e}")
        return _json({'ok': False, 'error': 'Something went wrong.'}, 500)
    finally:
        if conn:
            conn.close()

# ── Admin Routes ─────────────────────────────────────────────────────────────
@app.get('/api/admin/users')
async def api_admin_users(request: Request):
    """Get all users for admin panel."""
    user = request.session.get('user')
    if not user:
        return _json({'ok': False, 'error': 'Not logged in'}, 401)

    if not user.get('is_admin', False):
        return _json({'ok': False, 'error': 'Admin access required'}, 403)

    conn = None
    try:
        conn = _db()
        users = conn.execute(
            """SELECT auth_id, email, nickname, avatar, bio, is_admin,
                      created_at, last_login
               FROM auth_users
               ORDER BY auth_id DESC"""
        ).fetchall()

        user_list = []
        for u in users:
            user_list.append({
                'id': u['auth_id'],
                'email': u['email'],
                'nickname': u['nickname'] or '—',
                'avatar': u['avatar'] or 'https://api.dicebear.com/7.x/avataaars/svg?seed=default',
                'bio': u['bio'] or '—',
                'is_admin': bool(u['is_admin']),
                'created_at': u['created_at'],
                'last_login': u['last_login'] or 'Never',
                'is_online': str(u['auth_id']) in _session_log and bool(_session_log.get(str(u['auth_id'])))
            })

        total_users = len(user_list)
        online_users = sum(1 for u in user_list if u.get('is_online'))

        conn.close()
        return {
            'ok': True,
            'users': user_list,
            'stats': {
                'total': total_users,
                'online': online_users,
                'admins': sum(1 for u in user_list if u['is_admin'])
            }
        }

    except Exception as e:
        log.error(f"[Admin/users] Error: {e}")
        return _json({'ok': False, 'error': str(e)}, 500)
    finally:
        if conn:
            conn.close()

@app.delete('/api/admin/users/{user_id}')
async def api_admin_delete_user(user_id: int, request: Request):
    """Delete a user (admin only)."""
    user = request.session.get('user')
    if not user or not user.get('is_admin', False):
        return _json({'ok': False, 'error': 'Admin access required'}, 403)

    if str(user_id) == user.get('discord_id'):
        return _json({'ok': False, 'error': 'Cannot delete your own account'}, 400)

    conn = None
    try:
        conn = _db()
        existing = conn.execute(
            "SELECT auth_id, is_admin FROM auth_users WHERE auth_id = ?", (user_id,)
        ).fetchone()

        if not existing:
            return _json({'ok': False, 'error': 'User not found'}, 404)

        if existing['is_admin']:
            return _json({'ok': False, 'error': 'Cannot delete admin accounts'}, 403)

        conn.execute("DELETE FROM auth_users WHERE auth_id = ?", (user_id,))
        conn.commit()

        log.info(f"[Admin] User {user_id} deleted by admin")
        return {'ok': True, 'message': 'User deleted successfully'}

    except Exception as e:
        log.error(f"[Admin/delete] Error: {e}")
        return _json({'ok': False, 'error': str(e)}, 500)
    finally:
        if conn:
            conn.close()
@app.post('/api/admin/announce')
async def api_admin_announce(request: Request):
    """Admin-only: broadcast a message via the bot."""
    user = request.session.get('user')
    if not user or not user.get('is_admin'):
        return JSONResponse({'ok': False, 'error': 'forbidden'}, status_code=403)

    body = await request.json()
    message = (body.get('message') or '').strip()
    if not message:
        return JSONResponse({'ok': False, 'error': 'message required'}, status_code=400)

    status, resp = await _call_bot("POST", "/api/bot/announce", {
        "channel_id": DISCORD_BID_CHANNEL,
        "message": message,
    })
    return JSONResponse(resp, status_code=status)


@app.post('/api/admin/give-gold')
async def api_admin_give_gold(request: Request):
    """Admin-only: grant gold to a user via the bot."""
    user = request.session.get('user')
    if not user or not user.get('is_admin'):
        return JSONResponse({'ok': False, 'error': 'forbidden'}, status_code=403)

    body = await request.json()
    user_id = body.get('user_id')
    amount = body.get('amount')
    if not user_id or not isinstance(amount, (int, float)):
        return JSONResponse({'ok': False, 'error': 'user_id and amount required'}, status_code=400)

    status, resp = await _call_bot("POST", "/api/bot/give-gold", {
        "user_id": user_id,
        "amount": amount,
    })
    return JSONResponse(resp, status_code=status) 

@app.post('/api/admin/users/{user_id}/toggle-admin')
async def api_admin_toggle_admin(user_id: int, request: Request):
    """Toggle admin status for a user."""
    user = request.session.get('user')
    if not user or not user.get('is_admin', False):
        return _json({'ok': False, 'error': 'Admin access required'}, 403)

    if str(user_id) == user.get('discord_id'):
        return _json({'ok': False, 'error': 'Cannot modify your own admin status'}, 400)

    conn = None
    try:
        conn = _db()
        existing = conn.execute(
            "SELECT auth_id, is_admin FROM auth_users WHERE auth_id = ?", (user_id,)
        ).fetchone()

        if not existing:
            return _json({'ok': False, 'error': 'User not found'}, 404)

        new_status = 0 if existing['is_admin'] else 1
        conn.execute(
            "UPDATE auth_users SET is_admin = ? WHERE auth_id = ?",
            (new_status, user_id)
        )
        conn.commit()

        return {
            'ok': True,
            'is_admin': bool(new_status),
            'message': f"Admin status {'granted' if new_status else 'revoked'}"
        }

    except Exception as e:
        log.error(f"[Admin/toggle-admin] Error: {e}")
        return _json({'ok': False, 'error': str(e)}, 500)
    finally:
        if conn:
            conn.close()

@app.get('/api/admin/players')
async def api_admin_players(request: Request):
    """Get all game players (from the bot's synced data) for the admin panel."""
    user = request.session.get('user')
    if not user or not user.get('is_admin', False):
        return _json({'ok': False, 'error': 'Admin access required'}, 403)

    with _lock:
        data = cached_data.copy()

    q = (request.query_params.get('q') or '').strip().lower()

    players = []
    for pid, pd in data.get('players', {}).items():
        if not isinstance(pd, dict):
            continue
        name = pd.get('name', 'Unknown')
        if q and q not in name.lower() and q not in str(pid):
            continue
        players.append({
            'id': pid,
            'name': name,
            'level': pd.get('level', 0),
            'class': pd.get('char_class', '—'),
            'gold': pd.get('gold', 0),
            'gems': pd.get('gems', pd.get('Gems', 0)),
            'special_coins': pd.get('special_coins', 0),
            'xp': pd.get('xp', 0),
            'guild': pd.get('guild', '—'),
            'wins': pd.get('battles_won', 0),
        })

    players.sort(key=lambda p: (-p['level'], p['name']))
    return {'ok': True, 'players': players, 'total': len(players)}

@app.get('/api/admin/players/{user_id}')
async def api_admin_player_detail(user_id: str, request: Request):
    """Get a single player's full game-data blob (admin only), for editing."""
    user = request.session.get('user')
    if not user or not user.get('is_admin', False):
        return _json({'ok': False, 'error': 'Admin access required'}, 403)

    with _lock:
        data = cached_data.copy()

    pdata = data.get('players', {}).get(str(user_id))
    if pdata is None:
        return _json({'ok': False, 'error': 'Player not found'}, 404)

    return {'ok': True, 'player': pdata}

@app.post('/api/admin/players/{user_id}')
async def api_admin_update_player(user_id: str, request: Request):
    """Admin-only: update a player's stats/balance. Writes straight to this
    dashboard's DB (so the change shows immediately here) and also pushes the
    change live to the running bot, so the player's in-memory state updates
    without waiting for the bot's next periodic /api/sync push."""
    user = request.session.get('user')
    if not user or not user.get('is_admin', False):
        return _json({'ok': False, 'error': 'Admin access required'}, 403)

    try:
        uid = int(user_id)
    except (ValueError, TypeError):
        return _json({'ok': False, 'error': 'Invalid user id'}, 400)

    body = await _get_json(request)
    updates = body.get('updates')
    if not isinstance(updates, dict) or not updates:
        return _json({'ok': False, 'error': 'updates object required'}, 400)

    with _lock:
        current = cached_data.get('players', {}).get(str(uid))
    if current is None:
        current = get_player_game_data(uid)
    if not isinstance(current, dict):
        current = {}

    merged = {**current, **updates}

    conn = None
    try:
        conn = _db()
        conn.execute(
            "INSERT INTO players (user_id, data, last_updated) VALUES (?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(user_id) DO UPDATE SET data=excluded.data, last_updated=CURRENT_TIMESTAMP",
            (uid, json.dumps(merged)),
        )
        conn.commit()
    except Exception as e:
        log.error(f"[Admin/players] failed to save player {uid}: {e}")
        return _json({'ok': False, 'error': 'Failed to save player data'}, 500)
    finally:
        if conn:
            conn.close()

    load_game_data()  # refresh in-memory cache so it shows immediately in the dashboard

    # Push the change to the live bot process so it's reflected in-game right away.
    # Requires the bot to expose a matching POST /api/bot/update-player route,
    # the same way it already exposes /api/bot/give-gold and /api/bot/announce.
    bot_synced = False
    bot_error = None
    try:
        status, resp = await _call_bot("POST", "/api/bot/update-player", {
            "user_id": uid,
            "updates": updates,
        })
        bot_synced = status == 200 and (resp or {}).get('ok', False)
        if not bot_synced:
            bot_error = (resp or {}).get('error', f'bot returned status {status}')
    except Exception as e:
        bot_error = str(e)
        log.warning(f"[Admin/players] could not push live update to bot for {uid}: {e}")

    _log_activity(uid, "admin_edit_player", f"Admin updated fields: {', '.join(updates.keys())}")
    log.info(f"[Admin] Player {uid} stats updated by admin (bot_synced={bot_synced})")

    return {
        'ok': True,
        'player': merged,
        'bot_synced': bot_synced,
        'bot_error': None if bot_synced else bot_error,
    }

@app.get('/api/admin/stats')
async def api_admin_stats(request: Request):
    """Get admin statistics."""
    user = request.session.get('user')
    if not user or not user.get('is_admin', False):
        return _json({'ok': False, 'error': 'Admin access required'}, 403)

    try:
        conn = _db()
        total_users = conn.execute("SELECT COUNT(*) FROM auth_users").fetchone()[0]
        active_sessions = len(_session_log)

        recent_activity = conn.execute(
            "SELECT action, detail, ts FROM activity_log ORDER BY ts DESC LIMIT 20"
        ).fetchall()

        conn.close()

        return {
            'ok': True,
            'stats': {
                'total_users': total_users,
                'active_sessions': active_sessions,
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

# ── Page Routes ──────────────────────────────────────────────────────────────
@app.head('/')
@app.get('/')
@app.get('/dashboard')
async def index(request: Request):
    """Main dashboard page."""
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

    players_list.sort(key=lambda p: (-p['level'], p['name']))

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

@app.get('/battle-log')
async def battle_log(request: Request):
    return templates.TemplateResponse(request, 'battle-log.html', {})

@app.get('/profile')
async def profile_page(request: Request):
    return templates.TemplateResponse(request, 'profile.html', {})

@app.get('/achievements')
async def achievements_page(request: Request):
    return templates.TemplateResponse(request, 'achievements.html', {})

@app.get('/leaderboard')
async def leaderboards_page(request: Request):
    return templates.TemplateResponse(request, 'leaderboard.html', {})

@app.get('/inventory')
async def inventory(request: Request):
    return templates.TemplateResponse(request, 'inventory.html', {})

@app.get('/quest-board')
async def quest_board(request: Request):
    return templates.TemplateResponse(request, 'quest-board.html', {})

@app.get('/guild-hall')
async def guild_hall(request: Request):
    return templates.TemplateResponse(request, 'guild-hall.html', {})

@app.get('/settings')
async def settings(request: Request):
    return templates.TemplateResponse(request, 'settings.html', {})

# ── API Routes ──────────────────────────────────────────────────────────────
@app.get('/api/stats')
async def api_stats():
    """Get server stats."""
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
    """Get full leaderboard."""
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
        })

    pl.sort(key=lambda p: (-p['level'], -p['xp'], p['name']))
    return {'leaderboard': pl[:50], 'total': len(pl)}

@app.get('/api/activity/{user_id}')
async def api_activity(user_id: str):
    """Get user activity log."""
    try:
        conn = _db()
        rows = conn.execute(
            "SELECT action, detail, ts FROM activity_log "
            "WHERE user_id=? ORDER BY ts DESC LIMIT 30",
            (str(user_id),),
        ).fetchall()
        conn.close()
        return {
            'items': [{'action': r['action'], 'detail': r['detail'], 'ts': r['ts']} for r in rows]
        }
    except Exception as e:
        log.error(f"[api/activity] error: {e}")
        return _json({'items': []}, 500)

@app.get('/api/inventory/{user_id}')
async def api_inventory(user_id: str):
    """Get a player's inventory items."""
    try:
        uid = int(user_id)
    except (ValueError, TypeError):
        return _json({'items': []}, 400)

    game = get_player_game_data(uid)
    if not game:
        return _json({'items': []}, 404)

    raw_inventory = game.get('inventory', {})
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
async def api_auctions():
    """Get active auctions."""
    try:
        conn = _db()
        rows = conn.execute(
            "SELECT * FROM auctions WHERE status='active' ORDER BY end_time ASC LIMIT 50"
        ).fetchall()
        conn.close()

        auctions = []
        for r in rows:
            try:
                tl = max(0, int((datetime.fromisoformat(r['end_time']) - datetime.now(UTC)).total_seconds()))
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

@app.get('/api/profile')
async def api_profile(request: Request):
    """Get logged-in user's profile."""
    user = request.session.get('user')
    if not user:
        return {'logged_in': False}

    nickname = user.get('username')
    avatar = user.get('avatar')
    bio = user.get('bio', '')
    is_admin = bool(user.get('is_admin', False))

    if user.get('auth_type') == 'email':
        try:
            conn = _db()
            row = conn.execute(
                "SELECT nickname, avatar, bio, is_admin FROM auth_users WHERE auth_id = ?",
                (int(user['discord_id']),)
            ).fetchone()
            conn.close()
            if row:
                nickname = row['nickname'] or nickname
                avatar = row['avatar'] or avatar
                bio = row['bio'] or bio
                is_admin = bool(row['is_admin'])
        except Exception as e:
            log.warning(f"[profile] lookup failed: {e}")

    return {'logged_in': True, 'nickname': nickname, 'avatar': avatar, 'bio': bio, 'is_admin': is_admin}
def run_dashboard():
    """Start the dashboard server."""
    host = "0.0.0.0"
    port = int(os.environ.get("PORT", 25507))

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
    except OSError:
        # desired port is in use; pick an ephemeral free port
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
