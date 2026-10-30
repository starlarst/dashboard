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



EMAILJS_SERVICE_ID  = os.getenv("EMAILJS_SERVICE_ID",  "service_miabeqg")
EMAILJS_TEMPLATE_ID = os.getenv("EMAILJS_TEMPLATE_ID", "template_tntozpq")
EMAILJS_PUBLIC_KEY  = os.getenv("EMAILJS_PUBLIC_KEY",  "eMQUadu5q-_pONmwc")
EMAILJS_PRIVATE_KEY = os.getenv("EMAILJS_PRIVATE_KEY", "4buatakEvr4OCHJz37e4g")

def _emailjs_configured() -> bool:
    return not any(
        v.startswith("YOUR_") or not v.strip()
        for v in (EMAILJS_SERVICE_ID, EMAILJS_TEMPLATE_ID,
                  EMAILJS_PUBLIC_KEY, EMAILJS_PRIVATE_KEY)
    )


def send_reset_email(email: str, otp: str):
    email = email.strip().lower()

    if not email:
        raise ValueError("Cannot send reset email: recipient is empty")

    if not _emailjs_configured():
        raise RuntimeError(
            "EmailJS is not configured. Set EMAILJS_SERVICE_ID, EMAILJS_TEMPLATE_ID, "
            "EMAILJS_PUBLIC_KEY and EMAILJS_PRIVATE_KEY in your .env file."
        )

    payload = {
        "service_id": EMAILJS_SERVICE_ID,
        "template_id": EMAILJS_TEMPLATE_ID,
        "user_id": EMAILJS_PUBLIC_KEY,
        "accessToken": EMAILJS_PRIVATE_KEY,
        "template_params": {
            "to_email": email,
            "otp_code": otp,
            "from_name": "RPG Ghost",
        },
    }

    request = urllib.request.Request(
        "https://api.emailjs.com/api/v1.0/email/send",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; RPGGhostDashboard/1.0)",
            "Origin": "http://localhost",
        },
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

# ── FIX: Use a persistent data directory so the DB survives Render restarts.
# On Render, mount a Disk at /data and set DATA_DIR=/data in env vars.
# Locally it falls back to BASE_DIR so nothing changes for dev.
DATA_DIR = os.getenv('DATA_DIR', BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, 'veyra.db')
log.info(f"[DB] Using database at: {DB_PATH}")

# ── Standalone stats store ─────────────────────────────────────────────────────
DEFAULT_STATS = {
    'level': 1, 'gold': 100, 'gems': 10, 'special_coins': 0,
    'xp': 0, 'xp_needed': 100, 'char_class': 'Adventurer',
    'battles_won': 0, 'prestige_level': 0, 'guild': '—',
}
EDITABLE_STAT_FIELDS = {
    'level': int, 'gold': int, 'gems': int, 'special_coins': int,
    'xp': int, 'xp_needed': int, 'char_class': str,
    'battles_won': int, 'prestige_level': int, 'guild': str, 'name': str,
}

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

def save_stats(auth_type, uid, updates):
    """Merge validated `updates` into stored stats and return the result."""
    clean = {}
    for k, v in (updates or {}).items():
        caster = EDITABLE_STAT_FIELDS.get(k)
        if not caster:
            continue
        try:
            clean[k] = max(0, caster(v)) if caster is int else str(v)[:60]
        except (ValueError, TypeError):
            continue
    key = _stats_key(auth_type, uid)
    merged = {**get_stats(auth_type, uid), **clean}
    conn = _db()
    try:
        conn.execute(
            "INSERT INTO player_stats (account_key, data, last_updated) VALUES (?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(account_key) DO UPDATE SET data=excluded.data, last_updated=CURRENT_TIMESTAMP",
            (key, json.dumps(merged)),
        )
        conn.commit()
    finally:
        conn.close()
    return merged

def _apply_live_stats(session_user):
    """Overlay fresh DB stats on a session user dict (fixes stale profile data)."""
    stats = get_stats(session_user.get('auth_type', 'email'), session_user['discord_id'])
    for k in ('level', 'gold', 'gems', 'special_coins', 'xp', 'xp_needed', 'char_class'):
        session_user[k] = stats.get(k, session_user.get(k))
    session_user['battles_won'] = stats.get('battles_won', 0)
    session_user['prestige_level'] = stats.get('prestige_level', 0)
    session_user['guild'] = stats.get('guild', '—')
    return session_user

# ── In-memory storage (ephemeral by design — rate limits, bans, sessions) ─────
_link_tokens = {}
_rate_limits = {}
_session_log = {}
_ip_connections = defaultdict(int)
_ip_banned = {}
_global_req_count = {'n': 0}
_req_id_counter = 0

# ── FIX: Persist password reset tokens in the DB so they survive restarts ─────
# (In-memory _reset_tokens is kept as a fast cache; DB is the source of truth)
_reset_tokens = {}

def _store_reset_token(email: str, otp: str, expires_at: datetime):
    """Persist a reset token to the DB."""
    conn = _db()
    try:
        conn.execute(
            "INSERT INTO password_reset_tokens (email, otp, expires_at) VALUES (?, ?, ?) "
            "ON CONFLICT(email) DO UPDATE SET otp=excluded.otp, expires_at=excluded.expires_at",
            (email, otp, expires_at.isoformat())
        )
        conn.commit()
    finally:
        conn.close()

def _load_reset_token(email: str):
    """Load a reset token from the DB."""
    conn = _db()
    try:
        row = conn.execute(
            "SELECT otp, expires_at FROM password_reset_tokens WHERE email=?", (email,)
        ).fetchone()
        if row:
            return {
                'otp': row['otp'],
                'expires_at': datetime.fromisoformat(row['expires_at'])
            }
        return None
    finally:
        conn.close()

def _delete_reset_token(email: str):
    """Remove a used/expired reset token from the DB."""
    conn = _db()
    try:
        conn.execute("DELETE FROM password_reset_tokens WHERE email=?", (email,))
        conn.commit()
    finally:
        conn.close()

def _purge_reset_tokens():
    """Remove expired password-reset tokens from DB and memory cache."""
    now = datetime.now(timezone.utc)
    # Purge memory cache
    expired_mem = [k for k, v in _reset_tokens.items() if now > v['expires_at']]
    for k in expired_mem:
        del _reset_tokens[k]
    # Purge DB
    conn = _db()
    try:
        conn.execute("DELETE FROM password_reset_tokens WHERE expires_at < ?", (now.isoformat(),))
        conn.commit()
    finally:
        conn.close()

# ── DDoS Protection Settings ──────────────────────────────────────────────────
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

# ── Database Initialization ──────────────────────────────────────────────────
def init_db():
    """Create all necessary tables if they don't exist."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")

    cur = conn.cursor()

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

    cur.execute("""CREATE TABLE IF NOT EXISTS players (
        user_id INTEGER PRIMARY KEY,
        data TEXT,
        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    cur.execute("""CREATE TABLE IF NOT EXISTS guilds (
        name TEXT PRIMARY KEY,
        data TEXT,
        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    cur.execute("""CREATE TABLE IF NOT EXISTS world_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        data TEXT,
        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    cur.execute("""CREATE TABLE IF NOT EXISTS account_links (
        discord_id TEXT PRIMARY KEY,
        linked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_login TIMESTAMP
    )""")

    cur.execute("""CREATE TABLE IF NOT EXISTS activity_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        action TEXT,
        detail TEXT,
        ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

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

    # Migrate older auth_users tables
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

    cur.execute("""CREATE TABLE IF NOT EXISTS player_stats (
        account_key TEXT PRIMARY KEY,
        data TEXT NOT NULL,
        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    cur.execute("""CREATE TABLE IF NOT EXISTS announcements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message TEXT NOT NULL,
        author TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

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

    # ── FIX: Persist password reset tokens so they survive restarts ────────────
    cur.execute("""CREATE TABLE IF NOT EXISTS password_reset_tokens (
        email TEXT PRIMARY KEY,
        otp TEXT NOT NULL,
        expires_at TIMESTAMP NOT NULL
    )""")

    # ── FIX: Persist guild memberships explicitly ──────────────────────────────
    # Guild membership is the source of truth here; player_stats.guild is
    # kept in sync on every join/leave via save_stats so load_game_data()
    # always rebuilds correctly after a restart.
    cur.execute("""CREATE TABLE IF NOT EXISTS guild_members (
        account_key TEXT NOT NULL,
        guild_name TEXT NOT NULL,
        joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (account_key)
    )""")

    conn.commit()
    conn.close()

# ── Secret Key ─────────────────────────────────────────────────────────────
def _get_or_create_secret_key():
    """Use FLASK_SECRET if set, otherwise persist a generated key to disk."""
    env_secret = os.environ.get('FLASK_SECRET')
    if env_secret:
        return env_secret

    # ── FIX: Store secret key in DATA_DIR so it persists across restarts.
    # If the key changes every restart, all existing sessions are invalidated
    # and logged-out users have to sign in again unnecessarily.
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

UPDATE_INTERVAL = 30
cached_data = {}
last_updated = None
_lock = threading.Lock()

# ── Password Helpers ──────────────────────────────────────────────────────────
def _hash_password(password, salt=None):
    if salt is None:
        salt = secrets.token_hex(16)
    iterations = 260_000
    derived = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), iterations)
    return f"pbkdf2${iterations}${salt}${derived.hex()}"

def _verify_password(password, stored):
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
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL;")
    c.row_factory = sqlite3.Row
    return c

def _log_activity(user_id, action, detail=""):
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
        del _ip_banned[ip]
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
def load_game_data():
    """Load leaderboard/dashboard data from the dashboard's own tables."""
    global cached_data, last_updated
    try:
        init_db()
        conn = _db()
        players = {}
        for row in conn.execute(
            "SELECT ps.account_key, ps.data, a.nickname, a.avatar "
            "FROM player_stats ps "
            "LEFT JOIN auth_users a ON ps.account_key = 'email:' || a.auth_id"
        ).fetchall():
            try:
                d = json.loads(row['data'])
            except Exception:
                continue
            d.setdefault('name', row['nickname'] or row['account_key'])
            d['avatar'] = row['avatar']
            players[row['account_key']] = d

        # ── FIX: Rebuild guild membership from guild_members table, not just
        # from the player stats blob. This ensures guilds survive restarts even
        # if a guild route forgot to call save_stats with the updated guild field.
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

            # Keep player_stats in sync — if the guild field drifted, fix it
            if pd.get('guild') != guild_name:
                atype, uid = _split_account(gm_row['account_key'])
                save_stats(atype, uid, {'guild': guild_name})
                if gm_row['account_key'] in players:
                    players[gm_row['account_key']]['guild'] = guild_name

        # Also pick up guilds from player_stats for players not in guild_members
        # (backwards-compat: data created before the guild_members table existed)
        for pid, pd in players.items():
            g = pd.get('guild')
            if g and g != '—':
                guilds.setdefault(g, {'members': []})
                member_name = pd.get('name', pid)
                if member_name not in guilds[g]['members']:
                    guilds[g]['members'].append(member_name)
                    # Back-fill guild_members so future restarts are covered
                    try:
                        conn.execute(
                            "INSERT OR IGNORE INTO guild_members (account_key, guild_name) VALUES (?, ?)",
                            (pid, g)
                        )
                    except Exception:
                        pass

        conn.commit()
        conn.close()
        with _lock:
            cached_data = {'players': players, 'guilds': guilds, 'world_state': cached_data.get('world_state', {})}
            last_updated = datetime.now(timezone.utc)
    except Exception as e:
        log.error(f"[Dashboard] load_game_data error: {e}")

def _bg_updater():
    while True:
        load_game_data()
        time.sleep(UPDATE_INTERVAL)

def _split_account(raw):
    if ':' in raw:
        t, u = raw.split(':', 1)
        return (t if t in ('email', 'discord') else 'email'), u
    return 'email', raw

# ── Guild helpers — call these from guild_routes.py ──────────────────────────
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
    """Remove a player from their guild."""
    key = _stats_key(auth_type, uid)
    save_stats(auth_type, uid, {'guild': '—'})
    conn = _db()
    try:
        conn.execute("DELETE FROM guild_members WHERE account_key=?", (key,))
        conn.commit()
    finally:
        conn.close()

# ── FastAPI App Setup ─────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    ensure_admin_user()
    load_game_data()
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

from routes_profile import register_profile_routes
register_profile_routes(app)

from game_engine import register_game_routes
register_game_routes(app, _db)

from community import register_community_routes
register_community_routes(app, _db)

from guild_routes import register_guild_routes
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

# ── DDoS Protection Middleware ──────────────────────────────────────────────
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

    _ip_connections[ip] += 1
    if _ip_connections[ip] > DDOS_BURST_LIMIT:
        _ban_ip(ip, f"burst: {_ip_connections[ip]} concurrent")
        _ip_connections[ip] = 0
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
        _ip_connections[ip] = max(0, -_ip_connections.get(ip, 1) - 1)
        return _add_security_headers(PlainTextResponse("request too big", status_code=413))

    _global_req_count['n'] += 1
    _req_id_counter += 1
    request.state.req_id = f"{_req_id_counter:08x}"
    request.state.req_ip = ip
    request.state.req_start = time.monotonic()

    response = await call_next(request)

    _ip_connections[ip] = max(0, _ip_connections.get(ip, 1) - 1)
    _global_req_count['n'] = max(0, _global_req_count['n'] - 1)

    return _add_security_headers(response, req_id=getattr(request.state, 'req_id', None))

# ── Discord OAuth ─────────────────────────────────────────────────────────────
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
    oauth_url = f"{DISCORD_API}/oauth2/authorize?{urlencode(params)}"
    return RedirectResponse(oauth_url)

@app.get('/callback')
async def discord_callback(request: Request):
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

    game_data = get_stats('discord', str(user_id))

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
    ip = _get_real_ip(request)

    if _strict_rate_limit(ip, max_calls=8, window=600, scope='signup'):
        return _json({'ok': False, 'error': 'Too many signup attempts. Please wait a few minutes.'}, 429)

    data = await _get_json(request)
    email = str(data.get('email', '')).strip().lower()
    password = str(data.get('password', '')).strip()
    nickname = str(data.get('nickname', '')).strip() or email.split('@')[0]

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
            return _json({'ok': False, 'error': 'This email is already registered.'}, 400)

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

        get_stats('email', auth_id)
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
        _apply_live_stats(request.session['user'])

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
        return _json({'ok': False, 'error': 'Something went wrong. Please try again.'}, 500)
    finally:
        if conn:
            conn.close()

@app.post('/api/login')
async def api_login(request: Request):
    ip = _get_real_ip(request)

    if _strict_rate_limit(ip, max_calls=10, window=300, scope='login'):
        return _json({'ok': False, 'error': 'Too many login attempts. Please wait a moment.'}, 429)

    data = await _get_json(request)
    email = str(data.get('email', '')).strip().lower()
    password = str(data.get('password', '')).strip()
    keep_signed_in = bool(data.get('keep_signed_in', False))  # noqa: F841

    if not email or not password:
        return _json({'ok': False, 'error': 'Please fill in all fields.'}, 400)

    conn = None
    try:
        conn = _db()
        user = conn.execute(
            "SELECT auth_id, email, password_hash, nickname, avatar, bio, is_admin, created_at, last_login "
            "FROM auth_users WHERE lower(email) = ?",
            (email,)
        ).fetchone()

        if not user:
            return _json({'ok': False, 'error': 'Invalid email or password.'}, 401)

        if not _verify_password(password, user['password_hash']):
            return _json({'ok': False, 'error': 'Invalid email or password.'}, 401)

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
        _apply_live_stats(request.session['user'])

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
        return _json({'ok': False, 'error': 'Something went wrong. Please try again.'}, 500)
    finally:
        if conn:
            conn.close()

@app.post('/api/register')
async def api_register_alias(request: Request):
    return await api_signup(request)

@app.get('/api/me')
async def api_me(request: Request):
    user = request.session.get('user')
    if not user:
        return {'logged_in': False}

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
        session_user = dict(request.session['user'])
        session_user['is_admin'] = is_admin
        request.session['user'] = session_user

    live = _apply_live_stats(dict(request.session['user']))
    return {'logged_in': True, **live, 'is_admin': is_admin}

@app.get('/api/check-auth')
async def api_check_auth(request: Request):
    user = request.session.get('user')
    if not user:
        return {'logged_in': False}
    return {'logged_in': True, 'user': _apply_live_stats(dict(user))}

@app.get('/logout')
async def logout(request: Request):
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

    conn = None
    try:
        conn = _db()
        if user.get('auth_type') == 'email':
            conn.execute(
                "UPDATE auth_users SET nickname = ?, bio = ? WHERE auth_id = ?",
                (nickname or None, bio or None, int(user['discord_id']))
            )
            conn.commit()

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
    ip = _get_real_ip(request)
    user = request.session.get('user')
    if not user:
        return _json({'ok': False, 'error': 'Not logged in'}, 401)

    if user.get('is_admin', False):
        return _json({'ok': False, 'error': 'Admin accounts cannot self-delete.'}, 403)

    if _strict_rate_limit(ip, max_calls=5, window=300, scope='delete'):
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

        account_key = _stats_key(auth_type or 'email', user_id)
        conn.execute("DELETE FROM player_stats WHERE account_key = ?", (account_key,))
        conn.execute("DELETE FROM guild_members WHERE account_key = ?", (account_key,))
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
    ip = _get_real_ip(request)

    if _strict_rate_limit(ip, max_calls=5, window=600, scope='reset-request'):
        return _json({'ok': False, 'error': 'Too many requests. Please wait a few minutes.'}, 429)

    data = await _get_json(request)
    email = str(data.get('email', '')).strip().lower()

    if not email or '@' not in email:
        return _json({'ok': False, 'error': 'Please enter a valid email address.'}, 400)

    _purge_reset_tokens()

    conn = None
    try:
        conn = _db()
        account = conn.execute(
            "SELECT auth_id FROM auth_users WHERE lower(email) = ?", (email,)
        ).fetchone()
    except Exception:
        log.exception("[Password Reset] Database lookup failed")
        return _json({'ok': False, 'error': 'Unable to process the request right now.'}, 500)
    finally:
        if conn:
            conn.close()

    generic_response = {
        'ok': True,
        'message': 'If that email is registered, you will receive a reset code.'
    }

    if not account:
        return generic_response

    otp = str(secrets.randbelow(900000) + 100000)

    try:
        await asyncio.to_thread(send_reset_email, email, otp)
    except Exception:
        log.exception("[Password Reset] EmailJS delivery failed")
        return _json({'ok': False, 'error': 'Unable to send the reset email. Please try again later.'}, 500)

    # ── FIX: Persist OTP to DB (not just memory) so it survives a restart
    expires_at = datetime.now(UTC) + timedelta(minutes=5)
    _reset_tokens[email] = {'otp': otp, 'expires_at': expires_at}
    _store_reset_token(email, otp, expires_at)

    log.info("[Password Reset] Reset email accepted by EmailJS")
    return generic_response

@app.post('/api/password/verify-reset')
async def api_password_reset(request: Request):
    ip = _get_real_ip(request)

    if _strict_rate_limit(ip, max_calls=8, window=300, scope='reset-verify'):
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

    # ── FIX: Check memory cache first, fall back to DB ────────────────────────
    record = _reset_tokens.get(email) or _load_reset_token(email)
    if not record:
        return _json({'ok': False, 'error': 'No reset request found.'}, 400)

    if datetime.now(UTC) > record['expires_at']:
        _reset_tokens.pop(email, None)
        _delete_reset_token(email)
        return _json({'ok': False, 'error': 'The code has expired.'}, 400)

    if not hmac.compare_digest(record['otp'], otp):
        return _json({'ok': False, 'error': 'Incorrect code.'}, 400)

    conn = None
    try:
        conn = _db()
        existing = conn.execute("SELECT auth_id FROM auth_users WHERE lower(email) = ?", (email,)).fetchone()
        if not existing:
            return _json({'ok': False, 'error': 'No account found for that email.'}, 404)

        new_hash = _hash_password(new_password)
        conn.execute("UPDATE auth_users SET password_hash = ? WHERE auth_id = ?", (new_hash, existing['auth_id']))
        conn.commit()

        _reset_tokens.pop(email, None)
        _delete_reset_token(email)
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
               FROM auth_users ORDER BY auth_id DESC"""
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

        return {
            'ok': True,
            'users': user_list,
            'stats': {
                'total': len(user_list),
                'online': sum(1 for u in user_list if u.get('is_online')),
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
        # ── FIX: Also clean up guild membership on admin delete ────────────────
        conn.execute("DELETE FROM guild_members WHERE account_key LIKE ?", (f'email:{user_id}',))
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
    user = request.session.get('user')
    if not user or not user.get('is_admin'):
        return _json({'ok': False, 'error': 'forbidden'}, 403)
    body = await _get_json(request)
    message = str(body.get('message') or '').strip()[:1000]
    if not message:
        return _json({'ok': False, 'error': 'message required'}, 400)
    conn = _db()
    try:
        conn.execute("INSERT INTO announcements (message, author) VALUES (?, ?)",
                     (message, user.get('username')))
        conn.commit()
    finally:
        conn.close()
    return {'ok': True}

@app.get('/api/announcements')
async def api_announcements():
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
    user = request.session.get('user')
    if not user or not user.get('is_admin'):
        return _json({'ok': False, 'error': 'forbidden'}, 403)
    body = await _get_json(request)
    user_id = body.get('user_id')
    amount = body.get('amount')
    if not user_id or not isinstance(amount, (int, float)):
        return _json({'ok': False, 'error': 'user_id and amount required'}, 400)
    atype, uid = _split_account(str(user_id))
    stats = get_stats(atype, uid)
    new = save_stats(atype, uid, {'gold': stats['gold'] + int(amount)})
    load_game_data()
    return {'ok': True, 'gold': new['gold']}

@app.post('/api/admin/users/{user_id}/toggle-admin')
async def api_admin_toggle_admin(user_id: int, request: Request):
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
        conn.execute("UPDATE auth_users SET is_admin = ? WHERE auth_id = ?", (new_status, user_id))
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
    user = request.session.get('user')
    if not user or not user.get('is_admin', False):
        return _json({'ok': False, 'error': 'Admin access required'}, 403)
    load_game_data()
    with _lock:
        data = cached_data.copy()
    q = (request.query_params.get('q') or '').strip().lower()
    players = []
    for pid, pd in data.get('players', {}).items():
        name = pd.get('name', 'Unknown')
        if q and q not in name.lower() and q not in str(pid).lower():
            continue
        players.append({
            'id': pid, 'name': name, 'level': pd.get('level', 1),
            'class': pd.get('char_class', '—'), 'gold': pd.get('gold', 0),
            'gems': pd.get('gems', 0), 'special_coins': pd.get('special_coins', 0),
            'xp': pd.get('xp', 0), 'guild': pd.get('guild', '—'),
            'wins': pd.get('battles_won', 0),
        })
    players.sort(key=lambda p: (-p['level'], p['name']))
    return {'ok': True, 'players': players, 'total': len(players)}

@app.get('/api/admin/players/{user_id}')
async def api_admin_player_detail(user_id: str, request: Request):
    user = request.session.get('user')
    if not user or not user.get('is_admin', False):
        return _json({'ok': False, 'error': 'Admin access required'}, 403)
    atype, uid = _split_account(user_id)
    return {'ok': True, 'player': get_stats(atype, uid)}

@app.post('/api/admin/players/{user_id}')
async def api_admin_update_player(user_id: str, request: Request):
    user = request.session.get('user')
    if not user or not user.get('is_admin', False):
        return _json({'ok': False, 'error': 'Admin access required'}, 403)
    body = await _get_json(request)
    updates = body.get('updates')
    if not isinstance(updates, dict) or not updates:
        return _json({'ok': False, 'error': 'updates object required'}, 400)
    atype, uid = _split_account(user_id)

    # ── FIX: If admin changes guild via the player editor, sync guild_members too
    if 'guild' in updates:
        new_guild = str(updates['guild']).strip()
        if new_guild and new_guild != '—':
            join_guild(atype, uid, new_guild)
        else:
            leave_guild(atype, uid)

    merged = save_stats(atype, uid, updates)
    load_game_data()
    _log_activity(uid, "admin_edit_player", f"Admin updated fields: {', '.join(updates.keys())}")
    return {'ok': True, 'player': merged}

@app.get('/api/admin/stats')
async def api_admin_stats(request: Request):
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

@app.get('/hunting-grounds')
async def hunting_grounds(request: Request):
    return templates.TemplateResponse(request, 'hunting-grounds.html', {})

@app.get('/tower')
async def tower_page(request: Request):
    return templates.TemplateResponse(request, 'tower.html', {})

@app.get('/shop')
async def shop_page(request: Request):
    return templates.TemplateResponse(request, 'shop.html', {})

@app.get('/community')
async def community_page(request: Request):
    return templates.TemplateResponse(request, 'community.html', {})

# ── API Routes ──────────────────────────────────────────────────────────────
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

    pl.sort(key=lambda p: (-p['level'], -p['xp'], p['name']))
    return {'leaderboard': pl[:50], 'total': len(pl)}

@app.get('/api/activity/{user_id}')
async def api_activity(user_id: str):
    try:
        conn = _db()
        rows = conn.execute(
            "SELECT action, detail, ts FROM activity_log "
            "WHERE user_id=? ORDER BY ts DESC LIMIT 30",
            (str(user_id),),
        ).fetchall()
        conn.close()
        return {'items': [{'action': r['action'], 'detail': r['detail'], 'ts': r['ts']} for r in rows]}
    except Exception as e:
        log.error(f"[api/activity] error: {e}")
        return _json({'items': []}, 500)

@app.get('/api/inventory/{user_id}')
async def api_inventory(user_id: str):
    atype, uid = _split_account(user_id)
    game = get_stats(atype, uid)
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

    stats = get_stats(user.get('auth_type', 'email'), user['discord_id'])
    return {'logged_in': True, 'nickname': nickname, 'avatar': avatar, 'bio': bio,
            'is_admin': is_admin, 'stats': stats, **{k: stats.get(k) for k in
            ('level', 'gold', 'gems', 'special_coins', 'xp', 'xp_needed', 'char_class')}}

def run_dashboard():
    host = "127.0.0.1"
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
