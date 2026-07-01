from flask import Flask, render_template, jsonify, request, redirect, session, Response, g
from dotenv import load_dotenv
import json, os, threading, time, secrets, hashlib, hmac
from urllib.parse import urlencode
from datetime import datetime, timedelta, UTC
from collections import defaultdict
import sqlite3
import requests as http_requests
import logging

load_dotenv()

# Setup logging
log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(name)s — %(message)s")

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
DISCORD_BID_CHANNEL = '1492784006658396301'

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
DDOS_BLOCKED_UA_FRAGS = [
    'sqlmap', 'nikto', 'nmap', 'masscan', 'zgrab',
    'python-requests/2.18', 'curl/7.29', 'dirbuster',
    'go-http-client/1', 'libwww-perl',
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

# ── Flask App Setup ──────────────────────────────────────────────────────────
app = Flask(__name__)

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

app.secret_key = _get_or_create_secret_key()
app.permanent_session_lifetime = timedelta(days=30)

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

def _get_real_ip():
    """Get the real client IP."""
    xff = request.headers.get('X-Forwarded-For', '')
    if xff:
        return xff.split(',')[0].strip()
    return request.remote_addr or '0.0.0.0'

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

# ── Admin Configuration ──────────────────────────────────────────────────────
ADMIN_EMAIL = 'Admin@gmail.com'
ADMIN_PASSWORD = 'Rerir@123DND'
ADMIN_PASSWORD_HASH = _hash_password(ADMIN_PASSWORD)

def ensure_admin_user():
    """Create admin user if it doesn't exist."""
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
                 "System Administrator", 1, datetime.now(UTC).isoformat())
            )
            conn.commit()
            log.info(f"[Admin] Admin account created with email: {ADMIN_EMAIL}")
    except Exception as e:
        log.error(f"[Admin] Failed to create admin account: {e}")
    finally:
        if conn:
            conn.close()

# ── DDoS Protection Middleware ──────────────────────────────────────────────
@app.before_request
def ddos_guard():
    """Protect the app from DDoS and abuse."""
    global _req_id_counter
    ip = _get_real_ip()

    if _is_banned(ip):
        log.info(f"[DDoS] Blocked banned IP: {ip}")
        return Response("Forbidden", status=403)

    ua = (request.user_agent.string or '').lower()
    if any(frag in ua for frag in DDOS_BLOCKED_UA_FRAGS):
        _ban_ip(ip, f"bad UA: {ua[:60]}")
        return Response("Forbidden", status=403)

    honey_paths = [
        '/wp-admin', '/wp-login', '/.env', '/admin.php',
        '/phpinfo', '/config.php', '/xmlrpc', '/shell',
        '/.git', '/etc/passwd', '/proc/self',
    ]
    if any(request.path.startswith(p) for p in honey_paths):
        _ban_ip(ip, f"honeypot: {request.path}")
        return Response("Not Found", status=404)

    if _global_req_count['n'] >= DDOS_GLOBAL_CAP:
        log.warning(f"[DDoS] Global cap hit ({_global_req_count['n']} reqs)")
        return Response("Service Unavailable", status=503, headers={'Retry-After': '5'})

    _ip_connections[ip] += 1
    if _ip_connections[ip] > DDOS_BURST_LIMIT:
        _ban_ip(ip, f"burst: {_ip_connections[ip]} concurrent")
        _ip_connections[ip] = 0
        return Response("Too Many Requests", status=429, headers={'Retry-After': '60'})

    cl = request.content_length
    if cl and cl > DDOS_MAX_BODY_BYTES:
        log.warning(f"[DDoS] Body too large from {ip}: {cl} bytes")
        return Response("Request Too Large", status=413)

    _global_req_count['n'] += 1
    _req_id_counter += 1
    g.req_id = f"{_req_id_counter:08x}"
    g.req_ip = ip
    g.req_start = time.monotonic()

@app.after_request
def ddos_after(response):
    """Clean up after each request."""
    ip = getattr(g, 'req_ip', _get_real_ip())
    _ip_connections[ip] = max(0, _ip_connections.get(ip, 1) - 1)
    _global_req_count['n'] = max(0, _global_req_count['n'] - 1)

    if hasattr(g, 'req_id'):
        response.headers['X-Request-ID'] = g.req_id

    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'

    return response

# ── Discord OAuth ─────────────────────────────────────────────────────────────
@app.route('/login/discord')
def discord_login():
    """Redirect user to Discord for OAuth."""
    params = {
        'client_id': DISCORD_CLIENT_ID,
        'redirect_uri': DISCORD_REDIRECT_URI,
        'response_type': 'code',
        'scope': 'identify email',
    }
    oauth_url = f"{DISCORD_API}/oauth2/authorize?{urlencode(params)}"
    return redirect(oauth_url)

@app.route('/callback')
def discord_callback():
    """Handle Discord OAuth callback."""
    ip = _get_real_ip()
    code = request.args.get('code')
    error = request.args.get('error')

    if error or not code:
        log.warning(f"[OAuth] callback error param={error} code_present={bool(code)}")
        return redirect('/?auth=failed')

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
        return redirect('/?auth=failed')

    access_token = token_res.json().get('access_token')

    user_res = http_requests.get(
        f'{DISCORD_API}/users/@me',
        headers={'Authorization': f'Bearer {access_token}'},
    )

    if user_res.status_code != 200:
        log.warning(f'[OAuth] /users/@me failed ({user_res.status_code}): {user_res.text}')
        return redirect('/?auth=failed')

    discord_user = user_res.json()
    user_id = int(discord_user['id'])
    avatar_hash = discord_user.get('avatar')
    avatar_url = (
        f"https://cdn.discordapp.com/avatars/{user_id}/{avatar_hash}.png"
        if avatar_hash
        else f"https://cdn.discordapp.com/embed/avatars/{user_id % 5}.png"
    )

    game_data = get_player_game_data(user_id)

    session['user'] = {
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
        'ua': request.user_agent.string[:120],
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
    log.info(f"[OAuth] Logged in: {session['user']['username']} (ID: {user_id})")

    return redirect('/?auth=success')

# ── Auth Routes ──────────────────────────────────────────────────────────────
@app.route('/api/signup', methods=['POST'])
def api_signup():
    """Register a new email/password account."""
    ip = _get_real_ip()

    if _strict_rate_limit(ip, max_calls=8, window=600):
        return jsonify({
            'ok': False,
            'error': 'Too many signup attempts. Please wait a few minutes.'
        }), 429

    data = request.get_json(silent=True) or {}
    email = str(data.get('email', '')).strip().lower()
    password = str(data.get('password', '')).strip()
    nickname = str(data.get('nickname', '')).strip() or email.split('@')[0]
    recaptcha_token = data.get('recaptcha_token')

    if not email or '@' not in email:
        return jsonify({'ok': False, 'error': 'Please enter a valid email address.'}), 400

    if not password or len(password) < 6:
        return jsonify({'ok': False, 'error': 'Password must be at least 6 characters.'}), 400

    if len(password) > 128:
        return jsonify({'ok': False, 'error': 'Password is too long.'}), 400

    common_passwords = ['password', 'password1', '12345678', '123456789', 'qwerty123',
                        'letmein', 'iloveyou', 'admin123', 'welcome1', 'monkey123']
    if password.lower() in common_passwords:
        return jsonify({'ok': False, 'error': 'That password is too common.'}), 400

    conn = None
    try:
        conn = _db()
        existing = conn.execute(
            "SELECT auth_id FROM auth_users WHERE email = ?", (email,)
        ).fetchone()

        if existing:
            return jsonify({
                'ok': False,
                'error': 'This email is already registered.'
            }), 400

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

        session['user'] = {
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
            'ua': request.user_agent.string[:120],
            'at': datetime.now(UTC).isoformat(),
        })
        _session_log[sid] = _session_log[sid][-10:]

        return jsonify({
            'ok': True,
            'logged_in': True,
            'message': 'Welcome to RPG Ghost! 🎮',
            'user': session['user'],
            'is_admin': False
        }), 201

    except Exception as e:
        log.error(f"[Signup] Database error: {e}")
        return jsonify({
            'ok': False,
            'error': 'Something went wrong. Please try again.'
        }), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/login', methods=['POST'])
def api_login():
    """Log in with email and password."""
    ip = _get_real_ip()

    if _strict_rate_limit(ip, max_calls=10, window=300):
        return jsonify({
            'ok': False,
            'error': 'Too many login attempts. Please wait a moment.'
        }), 429

    data = request.get_json(silent=True) or {}
    email = str(data.get('email', '')).strip()
    password = str(data.get('password', '')).strip()
    keep_signed_in = bool(data.get('keep_signed_in', False))

    if not email or not password:
        return jsonify({'ok': False, 'error': 'Please fill in all fields.'}), 400

    conn = None
    try:
        conn = _db()
        user = conn.execute(
            "SELECT auth_id, email, password_hash, nickname, avatar, bio, is_admin, created_at, last_login "
            "FROM auth_users WHERE email = ?",
            (email,)
        ).fetchone()

        if not user:
            return jsonify({
                'ok': False,
                'error': 'Invalid email or password.'
            }), 401

        if not _verify_password(password, user['password_hash']):
            return jsonify({
                'ok': False,
                'error': 'Invalid email or password.'
            }), 401

        # Update last login
        conn.execute(
            "UPDATE auth_users SET last_login = ? WHERE auth_id = ?",
            (datetime.now(UTC).isoformat(), user['auth_id'])
        )
        conn.commit()

        nickname = user['nickname'] or email.split('@')[0]
        avatar_url = user['avatar'] or f"https://ui-avatars.com/api/?name={nickname}&background=5865F2&color=fff&size=64"

        session.permanent = keep_signed_in

        session['user'] = {
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
            'ua': request.user_agent.string[:120],
            'at': datetime.now(UTC).isoformat(),
        })
        _session_log[sid] = _session_log[sid][-10:]

        _log_activity(user['auth_id'], 'login', f'from {ip}')
        log.info(f"[Login] {email} logged in successfully")

        return jsonify({
            'ok': True,
            'logged_in': True,
            'message': f'Welcome back, {nickname}! 🎉',
            'user': session['user'],
            'is_admin': bool(user['is_admin'])
        })

    except Exception as e:
        log.error(f"[Login] Error: {e}")
        return jsonify({
            'ok': False,
            'error': 'Something went wrong. Please try again.'
        }), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/register', methods=['POST'])
def api_register_alias():
    """Alias for /api/signup."""
    return api_signup()

@app.route('/api/me')
def api_me():
    """Get current user info."""
    user = session.get('user')
    if not user:
        return jsonify({'logged_in': False})
    return jsonify({'logged_in': True, **user})

@app.route('/api/check-auth')
def api_check_auth():
    """Check if user is logged in."""
    user = session.get('user')
    if not user:
        return jsonify({'logged_in': False})
    return jsonify({'logged_in': True, 'user': user})

@app.route('/logout')
def logout():
    """Log out the current user."""
    u = session.pop('user', None)
    if u:
        _log_activity(u.get('discord_id', ''), "logout")
    session.clear()
    return redirect('/')

# ── Password Reset ──────────────────────────────────────────────────────────
_reset_tokens = {}

def _purge_reset_tokens():
    """Remove expired password-reset tokens."""
    now = datetime.now(UTC)
    expired = [k for k, v in _reset_tokens.items() if now > v['expires_at']]
    for k in expired:
        del _reset_tokens[k]

@app.route('/api/profile', methods=['POST'])
def api_update_profile():
    """Update the logged-in user's nickname/bio."""
    user = session.get('user')
    if not user:
        return jsonify({'ok': False, 'error': 'Not logged in'}), 401

    data = request.get_json(silent=True) or {}
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
        session['user']['username'] = nickname or user.get('username')
        session['user']['bio'] = bio
        session.modified = True

        _log_activity(user.get('discord_id', ''), 'profile_update')
        return jsonify({'ok': True, 'message': 'Profile updated.'})
    except Exception as e:
        log.error(f"[profile update] Error: {e}")
        return jsonify({'ok': False, 'error': 'Something went wrong.'}), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/delete-account', methods=['DELETE'])
def api_delete_account():
    """Allow a logged-in user to permanently delete their own account."""
    ip = _get_real_ip()
    user = session.get('user')
    if not user:
        return jsonify({'ok': False, 'error': 'Not logged in'}), 401

    if user.get('is_admin', False):
        return jsonify({
            'ok': False,
            'error': 'Admin accounts cannot self-delete. Ask another admin to revoke your admin status first.'
        }), 403

    if _strict_rate_limit(ip, max_calls=5, window=300):
        return jsonify({'ok': False, 'error': 'Too many attempts. Please wait a moment.'}), 429

    data = request.get_json(silent=True) or {}
    password = str(data.get('password', ''))
    user_id = user.get('discord_id')
    auth_type = user.get('auth_type')

    if not user_id:
        return jsonify({'ok': False, 'error': 'Invalid session.'}), 400

    conn = None
    try:
        conn = _db()

        if auth_type == 'email':
            row = conn.execute(
                "SELECT password_hash FROM auth_users WHERE auth_id = ?", (int(user_id),)
            ).fetchone()
            if not row:
                return jsonify({'ok': False, 'error': 'Account not found.'}), 404
            if not password or not _verify_password(password, row['password_hash']):
                return jsonify({'ok': False, 'error': 'Incorrect password.'}), 401
            conn.execute("DELETE FROM auth_users WHERE auth_id = ?", (int(user_id),))

        # Remove any game data tied to this account, regardless of auth type
        conn.execute("DELETE FROM players WHERE user_id = ?", (int(user_id),))
        conn.execute("DELETE FROM users WHERE user_id = ?", (int(user_id),))
        conn.execute("DELETE FROM account_links WHERE discord_id = ?", (str(user_id),))
        conn.commit()

        _log_activity(user_id, 'account_deleted', f'self-delete from {ip}')
        log.info(f"[Account] User {user_id} deleted their own account")

        session.pop('user', None)
        session.clear()

        return jsonify({'ok': True, 'message': 'Account deleted.'})
    except Exception as e:
        log.error(f"[delete-account] Error: {e}")
        return jsonify({'ok': False, 'error': 'Something went wrong. Please try again.'}), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/password/request-reset', methods=['POST'])
def api_password_request_reset():
    """Request a password reset code."""
    ip = _get_real_ip()

    if _strict_rate_limit(ip, max_calls=5, window=600):
        return jsonify({'ok': False, 'error': 'Too many requests. Please wait a few minutes.'}), 429

    data = request.get_json(silent=True) or {}
    email = str(data.get('email', '')).strip().lower()

    if not email or '@' not in email:
        return jsonify({'ok': False, 'error': 'Please enter a valid email address.'}), 400

    _purge_reset_tokens()

    otp = str(secrets.randbelow(900000) + 100000)
    _reset_tokens[email] = {
        'otp': otp,
        'expires_at': datetime.now(UTC) + timedelta(minutes=5)
    }

    log.info(f'[Password Reset] OTP generated for {email[:20]}')

    return jsonify({
        'ok': True,
        'message': 'If that email is registered, you will receive a reset code.',
        'otp': otp
    })

@app.route('/api/password/verify-reset', methods=['POST'])
def api_password_reset():
    """Reset password using OTP."""
    ip = _get_real_ip()

    if _strict_rate_limit(ip, max_calls=8, window=300):
        return jsonify({'ok': False, 'error': 'Too many attempts. Please wait a moment.'}), 429

    data = request.get_json(silent=True) or {}
    email = str(data.get('email', '')).strip().lower()
    otp = str(data.get('otp', '')).strip()
    new_password = str(data.get('new_password', '')).strip()

    if not email:
        return jsonify({'ok': False, 'error': 'Email address is required.'}), 400

    if not otp or len(otp) != 6:
        return jsonify({'ok': False, 'error': 'Please enter the 6-digit code.'}), 400

    if len(new_password) < 6:
        return jsonify({'ok': False, 'error': 'Password must be at least 6 characters.'}), 400

    _purge_reset_tokens()
    record = _reset_tokens.get(email)
    if not record:
        return jsonify({'ok': False, 'error': 'No reset request found.'}), 400

    if datetime.now(UTC) > record['expires_at']:
        _reset_tokens.pop(email, None)
        return jsonify({'ok': False, 'error': 'The code has expired.'}), 400

    if not hmac.compare_digest(record['otp'], otp):
        return jsonify({'ok': False, 'error': 'Incorrect code.'}), 400

    conn = None
    try:
        conn = _db()
        existing = conn.execute("SELECT auth_id FROM auth_users WHERE email = ?", (email,)).fetchone()
        if not existing:
            return jsonify({'ok': False, 'error': 'No account found for that email.'}), 404

        new_hash = _hash_password(new_password)
        conn.execute("UPDATE auth_users SET password_hash = ? WHERE email = ?", (new_hash, email))
        conn.commit()

        _reset_tokens.pop(email, None)
        _log_activity(existing['auth_id'], 'password_reset')

        return jsonify({'ok': True, 'message': 'Password updated successfully!'})

    except Exception as e:
        log.error(f"[Password Reset] Error: {e}")
        return jsonify({'ok': False, 'error': 'Something went wrong.'}), 500
    finally:
        if conn:
            conn.close()

# ── Admin Routes ─────────────────────────────────────────────────────────────
@app.route('/api/admin/users')
def api_admin_users():
    """Get all users for admin panel."""
    user = session.get('user')
    if not user:
        return jsonify({'ok': False, 'error': 'Not logged in'}), 401
    
    if not user.get('is_admin', False):
        return jsonify({'ok': False, 'error': 'Admin access required'}), 403
    
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
        return jsonify({
            'ok': True,
            'users': user_list,
            'stats': {
                'total': total_users,
                'online': online_users,
                'admins': sum(1 for u in user_list if u['is_admin'])
            }
        })
        
    except Exception as e:
        log.error(f"[Admin/users] Error: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/admin/users/<int:user_id>', methods=['DELETE'])
def api_admin_delete_user(user_id):
    """Delete a user (admin only)."""
    user = session.get('user')
    if not user or not user.get('is_admin', False):
        return jsonify({'ok': False, 'error': 'Admin access required'}), 403
    
    if str(user_id) == user.get('discord_id'):
        return jsonify({'ok': False, 'error': 'Cannot delete your own account'}), 400
    
    conn = None
    try:
        conn = _db()
        existing = conn.execute(
            "SELECT auth_id, is_admin FROM auth_users WHERE auth_id = ?", (user_id,)
        ).fetchone()
        
        if not existing:
            return jsonify({'ok': False, 'error': 'User not found'}), 404
        
        if existing['is_admin']:
            return jsonify({'ok': False, 'error': 'Cannot delete admin accounts'}), 403
        
        conn.execute("DELETE FROM auth_users WHERE auth_id = ?", (user_id,))
        conn.commit()
        
        log.info(f"[Admin] User {user_id} deleted by admin")
        return jsonify({'ok': True, 'message': 'User deleted successfully'})
        
    except Exception as e:
        log.error(f"[Admin/delete] Error: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/admin/users/<int:user_id>/toggle-admin', methods=['POST'])
def api_admin_toggle_admin(user_id):
    """Toggle admin status for a user."""
    user = session.get('user')
    if not user or not user.get('is_admin', False):
        return jsonify({'ok': False, 'error': 'Admin access required'}), 403
    
    if str(user_id) == user.get('discord_id'):
        return jsonify({'ok': False, 'error': 'Cannot modify your own admin status'}), 400
    
    conn = None
    try:
        conn = _db()
        existing = conn.execute(
            "SELECT auth_id, is_admin FROM auth_users WHERE auth_id = ?", (user_id,)
        ).fetchone()
        
        if not existing:
            return jsonify({'ok': False, 'error': 'User not found'}), 404
        
        new_status = 0 if existing['is_admin'] else 1
        conn.execute(
            "UPDATE auth_users SET is_admin = ? WHERE auth_id = ?",
            (new_status, user_id)
        )
        conn.commit()
        
        return jsonify({
            'ok': True, 
            'is_admin': bool(new_status),
            'message': f"Admin status {'granted' if new_status else 'revoked'}"
        })
        
    except Exception as e:
        log.error(f"[Admin/toggle-admin] Error: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/admin/stats')
def api_admin_stats():
    """Get admin statistics."""
    user = session.get('user')
    if not user or not user.get('is_admin', False):
        return jsonify({'ok': False, 'error': 'Admin access required'}), 403
    
    try:
        conn = _db()
        total_users = conn.execute("SELECT COUNT(*) FROM auth_users").fetchone()[0]
        active_sessions = len(_session_log)
        
        recent_activity = conn.execute(
            "SELECT action, detail, ts FROM activity_log ORDER BY ts DESC LIMIT 20"
        ).fetchall()
        
        conn.close()
        
        return jsonify({
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
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

# ── Game Data Routes ─────────────────────────────────────────────────────────
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
            last_updated = datetime.now(UTC)

        log.info(f"[Dashboard] Refreshed — {len(players)} players, {len(guilds)} guilds")
    except Exception as e:
        log.error(f"[Dashboard] load_game_data error: {e}")

def _bg_updater():
    """Background thread to refresh game data."""
    while True:
        load_game_data()
        time.sleep(UPDATE_INTERVAL)

@app.route('/')
@app.route('/dashboard')
def index():
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

    return render_template('dashboard.html',
        num_players=len(players_list),
        num_guilds=len(guilds_list),
        players=players_list,
        guilds=guilds_list,
        invasion_active=world_state.get('invasion_active', False),
        invasion_details=world_state.get('current_invasion', 'None'),
        king=world_state.get('king', '—'),
        season=world_state.get('season', 'Normal'),
        weather=world_state.get('weather', 'Clear'),
        active_events=world_state.get('active_events', []),
        last_updated=updated.strftime('%Y-%m-%d %H:%M:%S') if updated else 'Never',
    )

@app.route('/battle-log')
def battle_log():
    return render_template('battle-log.html')

@app.route('/inventory')
def inventory():
    return render_template('inventory.html')

@app.route('/quest-board')
def quest_board():
    return render_template('quest-board.html')

@app.route('/guild-hall')
def guild_hall():
    return render_template('guild-hall.html')

@app.route('/settings')
def settings():
    return render_template('settings.html')

# ── API Routes ──────────────────────────────────────────────────────────────
@app.route('/api/stats')
def api_stats():
    """Get server stats."""
    with _lock:
        data = cached_data.copy()
        updated = last_updated

    ws = data.get('world_state', {})
    return jsonify({
        'num_players': len(data.get('players', {})),
        'num_guilds': len(data.get('guilds', {})),
        'last_updated': updated.strftime('%Y-%m-%d %H:%M:%S') if updated else None,
        'total_cmds': ws.get('total_commands', 0),
        'battles': ws.get('total_battles', 0),
        'uptime': ws.get('uptime', '—'),
        'cmd_count': ws.get('command_count', 0),
    })

@app.route('/api/leaderboard')
def api_leaderboard():
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
    return jsonify({'leaderboard': pl[:50], 'total': len(pl)})

@app.route('/api/activity/<user_id>')
def api_activity(user_id):
    """Get user activity log."""
    try:
        conn = _db()
        rows = conn.execute(
            "SELECT action, detail, ts FROM activity_log "
            "WHERE user_id=? ORDER BY ts DESC LIMIT 30",
            (str(user_id),),
        ).fetchall()
        conn.close()
        return jsonify({
            'items': [{'action': r['action'], 'detail': r['detail'], 'ts': r['ts']} for r in rows]
        })
    except Exception as e:
        log.error(f"[api/activity] error: {e}")
        return jsonify({'items': []}), 500

@app.route('/api/inventory/<user_id>')
def api_inventory(user_id):
    """Get a player's inventory items."""
    try:
        uid = int(user_id)
    except (ValueError, TypeError):
        return jsonify({'items': []}), 400

    game = get_player_game_data(uid)
    if not game:
        return jsonify({'items': []}), 404

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

    return jsonify({'items': items})

@app.route('/api/auctions')
def api_auctions():
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
        return jsonify({'auctions': auctions})
    except Exception as e:
        log.warning(f'[api/auctions] {e}')
        return jsonify({'auctions': []}), 200

@app.route('/api/profile')
def api_profile():
    """Get logged-in user's profile."""
    user = session.get('user')
    if not user:
        return jsonify({'logged_in': False})

    nickname = user.get('username')
    avatar = user.get('avatar')
    bio = user.get('bio', '')

    if user.get('auth_type') == 'email':
        try:
            conn = _db()
            row = conn.execute(
                "SELECT nickname, avatar, bio FROM auth_users WHERE auth_id = ?",
                (int(user['discord_id']),)
            ).fetchone()
            conn.close()
            if row:
                nickname = row['nickname'] or nickname
                avatar = row['avatar'] or avatar
                bio = row['bio'] or bio
        except Exception as e:
            log.warning(f"[profile] lookup failed: {e}")

    return jsonify({'logged_in': True, 'nickname': nickname, 'avatar': avatar, 'bio': bio})

# ── Main Entry Point ────────────────────────────────────────────────────────
def run_dashboard():
    """Start the dashboard server."""
    init_db()
    ensure_admin_user()
    load_game_data()
    threading.Thread(target=_bg_updater, daemon=True).start()
    app.run(
        host="0.0.0.0",
        port=25507,
        debug=False,
        use_reloader=False
    )

if __name__ == '__main__':
    run_dashboard()
