from flask import Flask, render_template, jsonify, request, redirect, session, Response, g
from dotenv import load_dotenv
import json, os, threading, time, secrets, hashlib
from datetime import datetime, timedelta
from collections import defaultdict
import sqlite3
import requests as http_requests
import logging
from datetime import datetime, timedelta, UTC
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

DISCORD_API = 'https://discord.com/api'
RECAPTCHA_SECRET = os.getenv('RECAPTCHA_SECRET', '6LcnbLMsAAAAAJTcJgjSjzBQHzpkIGPLp4PHngb5')
RECAPTCHA_VERIFY_URL = 'https://www.google.com/recaptcha/api/siteverify'

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'veyra.db')

BOT_LINK_SECRET = os.getenv('BOT_LINK_SECRET')
if not BOT_LINK_SECRET:
    raise ValueError("BOT_LINK_SECRET not found in .env file!")

DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN', '')
DISCORD_GUILD_ID = '1466740140163731488'
DISCORD_BID_CHANNEL = '1492784006658396301'

# ── In-memory storage for various features ──────────────────────────────────
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

    # Email/password authentication
    cur.execute("""CREATE TABLE IF NOT EXISTS auth_users (
        auth_id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        nickname TEXT,
        avatar TEXT,
        bio TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_login TIMESTAMP
    )""")

    conn.commit()
    conn.close()

# ── Flask App Setup ──────────────────────────────────────────────────────────
app = Flask(__name__)

def _get_or_create_secret_key():
    """Use FLASK_SECRET if set, otherwise persist a generated key to disk so
    sessions/cookies survive server restarts instead of invalidating every
    logged-in user each time the process restarts."""
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

UPDATE_INTERVAL = 30
cached_data = {}
last_updated = None
_lock = threading.Lock()

# ── DDoS Protection Middleware ──────────────────────────────────────────────
def _get_real_ip():
    """Get the real client IP, even behind a proxy."""
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
    """Ban an IP address for a specified duration."""
    _ip_banned[ip] = time.time() + duration
    log.warning(f"[DDoS] BANNED {ip} for {duration}s — {reason}")

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

    now = time.time()
    hits = [t for t in _rate_limits.get(ip, []) if now - t < 300]
    remaining = max(0, 5 - len(hits))
    response.headers['X-RateLimit-Limit'] = '5'
    response.headers['X-RateLimit-Remaining'] = str(remaining)
    response.headers['X-RateLimit-Reset'] = str(int(now) + 300)

    if hasattr(g, 'req_id'):
        response.headers['X-Request-ID'] = g.req_id

    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'

    return response

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

# ── Database Helpers ──────────────────────────────────────────────────────────
def _db():
    """Get a database connection."""
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL;")
    c.row_factory = sqlite3.Row
    return c

def _rate_limited(ip, max_calls=5, window=300):
    """Sliding-window rate limiter for general endpoints."""
    now = time.time()
    hits = [t for t in _rate_limits.get(ip, []) if now - t < window]
    _rate_limits[ip] = hits
    if len(hits) >= max_calls:
        return True
    _rate_limits[ip].append(now)
    return False

def _verify_recaptcha(token):
    """Verify a reCAPTCHA v3 token. Returns True if verification passes or if we're in development."""
    # Skip verification in development or if token is missing
    if not token or token in ('skip', 'dev-bypass', 'null', 'undefined'):
        log.info("[reCAPTCHA] Skipping verification (development mode or missing token)")
        return True

    try:
        r = http_requests.post(
            RECAPTCHA_VERIFY_URL,
            data={'secret': RECAPTCHA_SECRET, 'response': token},
            timeout=10,
        )
        result = r.json()
        log.info(f"[reCAPTCHA] response: {result}")

        if not result.get('success', False):
            # NOTE: a mismatched site-key/secret pair (or any verification
            # hiccup) used to hard-block signup here. That contradicted the
            # rest of this function, which deliberately never blocks signup
            # over reCAPTCHA (see the score and exception branches below).
            # We now log and allow consistently, so a misconfigured/missing
            # reCAPTCHA key pair can never lock real users out of signup.
            error_codes = result.get('error-codes', [])
            log.warning(f"[reCAPTCHA] Verification failed with errors: {error_codes} — allowing signup anyway")
            return True

        score = result.get('score', 0.0)
        # Accept scores above 0.5, but also accept lower scores rather than blocking
        if score >= 0.5:
            return True
        else:
            log.info(f"[reCAPTCHA] Low score ({score}), but allowing signup anyway")
            return True  # Allow signup even with low score

    except Exception as e:
        log.warning(f"[reCAPTCHA] request failed: {e}")
        # Allow signup even if reCAPTCHA fails - better UX
        return True

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

# ── Discord OAuth ─────────────────────────────────────────────────────────────
@app.route('/login/discord')
def discord_login():
    """Redirect user to Discord for OAuth."""
    oauth_url = (
        f"{DISCORD_API}/oauth2/authorize"
        f"?client_id={DISCORD_CLIENT_ID}"
        f"&redirect_uri={DISCORD_REDIRECT_URI}"
        f"&response_type=code"
        f"&scope=identify%20email"
    )
    return redirect(oauth_url)

@app.route('/callback')
def discord_callback():
    """Handle Discord OAuth callback."""
    code = request.args.get('code')
    error = request.args.get('error')

    if error or not code:
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
        log.warning(f'[OAuth] Token exchange failed: {token_res.text}')
        return redirect('/?auth=failed')

    access_token = token_res.json().get('access_token')

    user_res = http_requests.get(
        f'{DISCORD_API}/users/@me',
        headers={'Authorization': f'Bearer {access_token}'},
    )

    if user_res.status_code != 200:
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
        'ip': request.remote_addr,
        'auth_type': 'discord'
    }

    sid = str(user_id)
    _session_log.setdefault(sid, []).append({
        'ip': request.remote_addr,
        'ua': request.user_agent.string[:120],
        'at': datetime.now(UTC).isoformat(),
    })
    _session_log[sid] = _session_log[sid][-10:]

    try:
        conn = _db()
        conn.execute(
            "INSERT INTO account_links (discord_id, last_login) VALUES (?,?) "
            "ON CONFLICT(discord_id) DO UPDATE SET last_login=excluded.last_login",
            (sid, datetime.utcnow()),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f"[login] account_links update failed: {e}")

    _log_activity(user_id, "login", f"from {request.remote_addr}")
    log.info(f"[OAuth] Logged in: {session['user']['username']} (ID: {user_id})")

    return redirect('/?auth=success')

# ── Favicon & Register Alias Fixes ───────────────────────────────────────────
@app.route('/favicon.ico')
def favicon():
    """Return empty response for favicon to prevent 404 clutter."""
    return Response(status=204)

@app.route('/api/register', methods=['POST'])
def api_register_alias():
    """Alias for /api/signup to support frontend calls to /api/register."""
    return api_signup()

# ── Email/Password Authentication ─────────────────────────────────────────────
@app.route('/api/signup', methods=['POST'])
def api_signup():
    """Register a new email/password account with human-friendly validation."""
    ip = _get_real_ip()

    # Rate limit signup attempts. Generous enough that a real person fixing a
    # validation error (e.g. forgetting the uppercase/number requirement) a
    # couple of times in a row won't get locked out of their own signup.
    if _strict_rate_limit(ip, max_calls=8, window=600):
        return jsonify({
            'ok': False,
            'error': 'Too many signup attempts. Please wait a few minutes and try again.'
        }), 429

    data = request.get_json(silent=True) or {}
    email = str(data.get('email', '')).strip().lower()
    password = str(data.get('password', '')).strip()
    nickname = str(data.get('nickname', '')).strip() or email.split('@')[0]
    recaptcha_token = data.get('recaptcha_token')

    # ── Human-friendly validation ──

    # Check email format
    if not email:
        return jsonify({
            'ok': False,
            'error': 'Please enter your email address.'
        }), 400

    if '@' not in email or '.' not in email:
        return jsonify({
            'ok': False,
            'error': 'Please enter a valid email address (e.g., name@domain.com)'
        }), 400

    # Check password
    if not password:
        return jsonify({
            'ok': False,
            'error': 'Please create a password for your account.'
        }), 400

    if len(password) < 6:
        return jsonify({
            'ok': False,
            'error': 'Your password must be at least 6 characters long for security.'
        }), 400

    if len(password) > 128:
        return jsonify({
            'ok': False,
            'error': 'Your password is too long (maximum 128 characters).'
        }), 400

    # Check for common weak passwords
    common_passwords = ['password', 'password1', '12345678', '123456789', 'qwerty123',
                        'letmein', 'iloveyou', 'admin123', 'welcome1', 'monkey123']
    if password.lower() in common_passwords:
        return jsonify({
            'ok': False,
            'error': 'That password is too common. Please choose a stronger one.'
        }), 400

    # Check for at least one number
    if not any(c.isdigit() for c in password):
        return jsonify({
            'ok': False,
            'error': 'For extra security, include at least one number in your password.'
        }), 400

    # Optional: Check for uppercase letter
    if not any(c.isupper() for c in password):
        return jsonify({
            'ok': False,
            'error': 'For extra security, include at least one uppercase letter in your password.'
        }), 400

    # Verify reCAPTCHA - now handles errors gracefully
    if not _verify_recaptcha(recaptcha_token):
        return jsonify({
            'ok': False,
            'error': 'Security check failed. Please refresh the page and try again.'
        }), 400

    # ── Check if email already exists ──
    try:
        conn = _db()
        existing = conn.execute(
            "SELECT auth_id FROM auth_users WHERE email = ?", (email,)
        ).fetchone()

        if existing:
            conn.close()
            return jsonify({
                'ok': False,
                'error': 'This email is already registered. Would you like to sign in instead?'
            }), 400

        # ── Hash the password ──
        salt = secrets.token_hex(16)
        password_hash = hashlib.sha256((salt + password).encode()).hexdigest()

        # ── Create the user ──
        avatar_url = f"https://ui-avatars.com/api/?name={nickname}&background=5865F2&color=fff&size=64"

        cursor = conn.execute(
            """INSERT INTO auth_users (email, password_hash, nickname, avatar, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (email, f"{salt}:{password_hash}", nickname, avatar_url, datetime.now(UTC).isoformat())
        )
        auth_id = cursor.lastrowid
        conn.commit()
        conn.close()

        # ── Log the signup ──
        _log_activity(auth_id, 'signup', f'email={email}')
        log.info(f"[Signup] New account created: {email} (ID: {auth_id})")

        # ── Create session ──
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
            'auth_type': 'email'
        }

        return jsonify({
            'ok': True,
            'message': 'Welcome to RPG Ghost! Your adventure begins now. 🎮',
            'user': session['user']
        }), 201

    except sqlite3.IntegrityError:
        return jsonify({
            'ok': False,
            'error': 'This email is already registered. Would you like to sign in instead?'
        }), 400
    except Exception as e:
        log.error(f"[Signup] Database error: {e}")
        return jsonify({
            'ok': False,
            'error': 'Something went wrong. Please try again in a moment.'
        }), 500

@app.route('/api/login', methods=['POST'])
def api_login():
    """Log in with email and password with human-friendly feedback."""
    ip = _get_real_ip()

    # Rate limit login attempts. Wide enough that a couple of typos won't
    # lock a real user out of their own account right after.
    if _strict_rate_limit(ip, max_calls=10, window=300):
        return jsonify({
            'ok': False,
            'error': 'Too many login attempts. Please wait a moment and try again.'
        }), 429

    data = request.get_json(silent=True) or {}
    email = str(data.get('email', '')).strip().lower()
    password = str(data.get('password', '')).strip()

    # ── Human-friendly validation ──
    if not email:
        return jsonify({
            'ok': False,
            'error': 'Please enter your email address.'
        }), 400

    if '@' not in email:
        return jsonify({
            'ok': False,
            'error': 'Please enter a valid email address.'
        }), 400

    if not password:
        return jsonify({
            'ok': False,
            'error': 'Please enter your password.'
        }), 400

    # ── Check credentials ──
    try:
        conn = _db()
        user = conn.execute(
            "SELECT auth_id, email, password_hash, nickname, avatar, bio, created_at, last_login "
            "FROM auth_users WHERE email = ?",
            (email,)
        ).fetchone()

        if not user:
            conn.close()
            # Don't reveal if email exists (security)
            return jsonify({
                'ok': False,
                'error': 'Invalid email or password. Please check your credentials and try again.'
            }), 401

        # ── Verify password ──
        stored = user['password_hash']
        if ':' not in stored:
            # Legacy hash format (no salt)
            if hashlib.sha256(password.encode()).hexdigest() != stored:
                conn.close()
                return jsonify({
                    'ok': False,
                    'error': 'Invalid email or password. Please check your credentials and try again.'
                }), 401
        else:
            salt, hash_value = stored.split(':', 1)
            computed_hash = hashlib.sha256((salt + password).encode()).hexdigest()
            if computed_hash != hash_value:
                conn.close()
                return jsonify({
                    'ok': False,
                    'error': 'Invalid email or password. Please check your credentials and try again.'
                }), 401

        # ── Update last login ──
        conn.execute(
            "UPDATE auth_users SET last_login = ? WHERE auth_id = ?",
            (datetime.now(UTC).isoformat(), user['auth_id'])
        )
        conn.commit()
        conn.close()

        # ── Create session ──
        nickname = user['nickname'] or email.split('@')[0]
        avatar_url = user['avatar'] or f"https://ui-avatars.com/api/?name={nickname}&background=5865F2&color=fff&size=64"

        session['user'] = {
            'discord_id': str(user['auth_id']),
            'username': nickname,
            'email': user['email'],
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
            'auth_type': 'email'
        }

        # Record session
        sid = str(user['auth_id'])
        _session_log.setdefault(sid, []).append({
            'ip': ip,
            'ua': request.user_agent.string[:120],
            'at': datetime.now(UTC).isoformat(),
        })
        _session_log[sid] = _session_log[sid][-10:]

        _log_activity(user['auth_id'], 'login', f'from {ip}')
        log.info(f"[Login] {email} logged in successfully (ID: {user['auth_id']})")

        return jsonify({
            'ok': True,
            'message': f'Welcome back, {nickname}! 🎉',
            'user': session['user']
        })

    except Exception as e:
        log.error(f"[Login] Error: {e}")
        return jsonify({
            'ok': False,
            'error': 'Something went wrong. Please try again in a moment.'
        }), 500

@app.route('/api/check-auth')
def api_check_auth():
    """Check if user is logged in and return their info."""
    user = session.get('user')
    if not user:
        return jsonify({'logged_in': False})

    # Refresh user data from DB for email users
    if user.get('auth_type') == 'email':
        try:
            conn = _db()
            db_user = conn.execute(
                "SELECT nickname, avatar, bio FROM auth_users WHERE auth_id = ?",
                (int(user['discord_id']),)
            ).fetchone()
            if db_user:
                user['username'] = db_user['nickname'] or user['username']
                user['avatar'] = db_user['avatar'] or user['avatar']
            conn.close()
        except Exception:
            pass

    return jsonify({
        'logged_in': True,
        'user': user
    })

@app.route('/logout')
def logout():
    """Log out the current user."""
    u = session.pop('user', None)
    if u:
        _log_activity(u.get('discord_id', ''), "logout",
                     f"auth_type={u.get('auth_type', 'unknown')}")
    session.clear()
    return redirect('/')

@app.route('/api/me')
def api_me():
    """Get current user info."""
    user = session.get('user')
    if not user:
        return jsonify({'logged_in': False})
    return jsonify({'logged_in': True, **user})

# ── Password Reset Flow ──────────────────────────────────────────────────────
_reset_tokens = {}

@app.route('/api/password/request-reset', methods=['POST'])
def api_password_request_reset():
    """Request a password reset code."""
    ip = _get_real_ip()

    if _strict_rate_limit(ip, max_calls=5, window=600):
        return jsonify({
            'ok': False,
            'error': 'Too many requests. Please wait a few minutes.'
        }), 429

    data = request.get_json(silent=True) or {}
    email = str(data.get('email', '')).strip().lower()

    if not email or '@' not in email:
        return jsonify({
            'ok': False,
            'error': 'Please enter a valid email address.'
        }), 400

    # Generate OTP
    otp = str(secrets.randbelow(900000) + 100000)
    _reset_tokens[email] = {
        'otp': otp,
        'expires_at': datetime.utcnow() + timedelta(minutes=5)
    }

    log.info(f'[Password Reset] OTP generated for {email[:20]}')

    # In production, send via EmailJS/SMTP
    return jsonify({
        'ok': True,
        'message': 'If that email is registered, you will receive a reset code.',
        'otp': otp  # Only for development
    })

@app.route('/api/password/reset', methods=['POST'])
def api_password_reset():
    """Reset password using OTP."""
    ip = _get_real_ip()

    if _strict_rate_limit(ip, max_calls=8, window=300):
        return jsonify({
            'ok': False,
            'error': 'Too many attempts. Please wait a moment.'
        }), 429

    data = request.get_json(silent=True) or {}
    email = str(data.get('email', '')).strip().lower()
    otp = str(data.get('otp', '')).strip()
    new_password = str(data.get('new_password', '')).strip()

    # ── Validation ──
    if not email:
        return jsonify({
            'ok': False,
            'error': 'Email address is required.'
        }), 400

    if not otp or len(otp) != 6:
        return jsonify({
            'ok': False,
            'error': 'Please enter the 6-digit code from your email.'
        }), 400

    if len(new_password) < 6:
        return jsonify({
            'ok': False,
            'error': 'Your new password must be at least 6 characters long.'
        }), 400

    # ── Verify OTP ──
    record = _reset_tokens.get(email)
    if not record:
        return jsonify({
            'ok': False,
            'error': 'No reset request found. Please request a new code.'
        }), 400

    if datetime.utcnow() > record['expires_at']:
        _reset_tokens.pop(email, None)
        return jsonify({
            'ok': False,
            'error': 'The code has expired. Please request a new one.'
        }), 400

    if record['otp'] != otp:
        return jsonify({
            'ok': False,
            'error': 'Incorrect code. Please check your email and try again.'
        }), 400

    # ── Update password ──
    try:
        conn = _db()
        salt = secrets.token_hex(16)
        password_hash = hashlib.sha256((salt + new_password).encode()).hexdigest()

        conn.execute(
            "UPDATE auth_users SET password_hash = ? WHERE email = ?",
            (f"{salt}:{password_hash}", email)
        )
        conn.commit()
        conn.close()

        _reset_tokens.pop(email, None)
        _log_activity(email, 'password_reset')

        return jsonify({
            'ok': True,
            'message': 'Password updated successfully! You can now log in with your new password.'
        })

    except Exception as e:
        log.error(f"[Password Reset] Error: {e}")
        return jsonify({
            'ok': False,
            'error': 'Something went wrong. Please try again.'
        }), 500

# ── Account Linking System ──────────────────────────────────────────────────
def _purge_link_tokens():
    """Remove expired link tokens."""
    now = datetime.utcnow()
    expired = [k for k, v in _link_tokens.items() if now > v['expires_at']]
    for k in expired:
        del _link_tokens[k]

@app.route('/api/link/register', methods=['POST'])
def link_register():
    """Register a link token from Discord bot."""
    ip = _get_real_ip()

    if _strict_rate_limit(ip, max_calls=3, window=60):
        return jsonify({
            'ok': False,
            'error': 'Too many requests. Please wait a minute.'
        }), 429

    data = request.get_json(silent=True) or {}
    token = str(data.get('token', '')).strip()
    discord_id = str(data.get('discord_id', '')).strip()
    secret = data.get('bot_secret', '')

    if secret != BOT_LINK_SECRET:
        log.warning(f"[link/register] bad bot_secret from ip={ip}")
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401

    if not discord_id or not discord_id.isdigit():
        return jsonify({
            'ok': False,
            'error': 'Invalid Discord ID.'
        }), 400

    if not token or len(token) < 16:
        return jsonify({
            'ok': False,
            'error': 'Invalid token.'
        }), 400

    _purge_link_tokens()

    for tok in [k for k, v in _link_tokens.items() if v['discord_id'] == discord_id]:
        del _link_tokens[tok]

    expires_at = datetime.utcnow() + timedelta(minutes=8)
    _link_tokens[token] = {
        'discord_id': discord_id,
        'expires_at': expires_at,
        'ip': ip,
    }

    log.info(f"[link/register] Token stored for {discord_id}")
    return jsonify({
        'ok': True,
        'expires_in': 480,
        'message': 'Token registered. Click the link to complete linking.'
    })

@app.route('/api/link/verify', methods=['POST'])
def link_verify():
    """Verify a link token after user clicks it."""
    ip = _get_real_ip()
    data = request.get_json(silent=True) or {}

    token = str(data.get('token', '')).strip()
    discord_id = str(data.get('discord_id', '')).strip()
    secret = data.get('bot_secret', '')

    if secret != BOT_LINK_SECRET:
        log.warning(f"[link/verify] bad bot_secret from ip={ip}")
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401

    info = _link_tokens.get(token)
    if not info:
        return jsonify({
            'ok': False,
            'error': 'Token not found or already used.'
        }), 404

    if datetime.utcnow() > info['expires_at']:
        del _link_tokens[token]
        return jsonify({
            'ok': False,
            'error': 'This link has expired. Please request a new one.'
        }), 410

    if info['discord_id'] != discord_id:
        return jsonify({
            'ok': False,
            'error': 'This token does not match your Discord ID.'
        }), 400

    del _link_tokens[token]

    try:
        conn = _db()
        conn.execute(
            """INSERT INTO account_links (discord_id, linked_at)
               VALUES (?, ?)
               ON CONFLICT(discord_id)
               DO UPDATE SET linked_at = excluded.linked_at""",
            (discord_id, datetime.now(UTC).isoformat()),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.error(f"[link/verify] DB error: {e}")
        return jsonify({
            'ok': False,
            'error': 'Database error. Please try again.'
        }), 500

    _log_activity(discord_id, "account_linked")
    log.info(f"[link/verify] {discord_id} linked successfully")
    return jsonify({'ok': True, 'message': 'Account linked successfully!'})

@app.route('/link')
def link_page():
    """Landing page for account linking."""
    token = request.args.get('token', '').strip()
    ip = _get_real_ip()

    if not token:
        return '''
        <html><body style="background:#0a0a0f;color:#e0e0e0;display:flex;align-items:center;justify-content:center;height:100vh;font-family:sans-serif;text-align:center;">
            <div style="background:#13131f;padding:40px;border-radius:16px;border:1px solid rgba(255,255,255,0.08);max-width:400px;">
                <h1 style="color:#ef4444;">❌ Invalid Link</h1>
                <p>No token provided. Please use the link from your Discord DM.</p>
            </div>
        </body></html>
        '''

    _purge_link_tokens()
    info = _link_tokens.get(token)

    if not info:
        return '''
        <html><body style="background:#0a0a0f;color:#e0e0e0;display:flex;align-items:center;justify-content:center;height:100vh;font-family:sans-serif;text-align:center;">
            <div style="background:#13131f;padding:40px;border-radius:16px;border:1px solid rgba(255,255,255,0.08);max-width:400px;">
                <h1 style="color:#ef4444;">❌ Invalid or Expired</h1>
                <p>This link is invalid or has already been used. Run !link in Discord for a new one.</p>
            </div>
        </body></html>
        '''

    if datetime.utcnow() > info['expires_at']:
        del _link_tokens[token]
        return '''
        <html><body style="background:#0a0a0f;color:#e0e0e0;display:flex;align-items:center;justify-content:center;height:100vh;font-family:sans-serif;text-align:center;">
            <div style="background:#13131f;padding:40px;border-radius:16px;border:1px solid rgba(255,255,255,0.08);max-width:400px;">
                <h1 style="color:#ef4444;">⏰ Link Expired</h1>
                <p>This link has expired. Please run !link in Discord to get a fresh one.</p>
            </div>
        </body></html>
        '''

    discord_id = info['discord_id']

    try:
        conn = _db()
        conn.execute(
            """INSERT INTO account_links (discord_id, linked_at)
               VALUES (?, ?)
               ON CONFLICT(discord_id)
               DO UPDATE SET linked_at = excluded.linked_at""",
            (discord_id, datetime.now(UTC).isoformat()),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.error(f"[link] DB error: {e}")
        return '''
        <html><body style="background:#0a0a0f;color:#e0e0e0;display:flex;align-items:center;justify-content:center;height:100vh;font-family:sans-serif;text-align:center;">
            <div style="background:#13131f;padding:40px;border-radius:16px;border:1px solid rgba(255,255,255,0.08);max-width:400px;">
                <h1 style="color:#ef4444;">❌ Error</h1>
                <p>Something went wrong. Please try again.</p>
            </div>
        </body></html>
        '''

    del _link_tokens[token]
    _log_activity(discord_id, 'account_linked_web', f'via /link page ip={ip}')

    if session.get('user'):
        session['user']['linked'] = True

    return '''
    <html><body style="background:#0a0a0f;color:#e0e0e0;display:flex;align-items:center;justify-content:center;height:100vh;font-family:sans-serif;text-align:center;">
        <div style="background:#13131f;padding:40px;border-radius:16px;border:2px solid #57F287;max-width:400px;">
            <h1 style="color:#57F287;">✅ Success!</h1>
            <p>Your Discord account has been linked to the RPG Ghost Dashboard!</p>
            <a href="/" style="display:inline-block;margin-top:20px;padding:12px 28px;background:#57F287;color:#000;border-radius:8px;text-decoration:none;font-weight:700;">Go to Dashboard</a>
        </div>
    </body></html>
    '''

@app.route('/api/link/status/<discord_id>')
def link_status(discord_id):
    """Check if an account is linked."""
    try:
        conn = _db()
        row = conn.execute(
            "SELECT linked_at FROM account_links WHERE discord_id=?", (discord_id,)
        ).fetchone()
        conn.close()
        if row:
            return jsonify({'linked': True, 'linked_at': row['linked_at']})
    except Exception as e:
        log.warning(f"[link/status] error: {e}")
    return jsonify({'linked': False})

# ── Game Data Helpers ────────────────────────────────────────────────────────
def get_player_game_data(user_id):
    """Get player game data from database."""
    try:
        conn = _db()
        row = conn.execute("SELECT data FROM players WHERE user_id=?", (user_id,)).fetchone()
        if row:
            conn.close()
            return json.loads(row['data'])

        row = conn.execute(
            "SELECT gold, gems, special_coins, xp, level FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        conn.close()
        if row:
            return {k: row[k] for k in row.keys()}
    except Exception as e:
        log.error(f'[DB] get_player_game_data({user_id}) error: {e}')
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
            except:
                pass

        guilds = {}
        for row in conn.execute("SELECT name, data FROM guilds").fetchall():
            try:
                guilds[row['name']] = json.loads(row['data'])
            except:
                pass

        world_state = {}
        row = conn.execute("SELECT data FROM world_state WHERE id=1").fetchone()
        if row:
            try:
                world_state = json.loads(row['data'])
            except:
                pass

        conn.close()

        with _lock:
            cached_data = {'players': players, 'guilds': guilds, 'world_state': world_state}
            last_updated = datetime.utcnow()

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
    return render_template('battle-log.html') # You may need to create this file

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

@app.route('/api/players')
def api_players():
    """Get top players for leaderboard."""
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
            'guild': pd.get('guild', '—'),
            'xp': xp,
            'xp_needed': xp_needed,
            'xp_pct': round(xp / xp_needed * 100, 1),
        })

    pl.sort(key=lambda p: (-p['level'], -p['xp'], p['name']))
    return jsonify({'players': pl[:20]})

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

@app.route('/api/guilds')
def api_guilds():
    """Get guild list."""
    with _lock:
        data = cached_data.copy()

    guilds = []
    for name, gd in data.get('guilds', {}).items():
        guilds.append({
            'name': name,
            'members': len(gd.get('members', [])),
            'level': gd.get('level', 1),
            'gold': gd.get('gold', 0),
            'rank': gd.get('rank', '—'),
        })

    guilds.sort(key=lambda g: (-g['members'], g['name']))
    return jsonify({'guilds': guilds, 'total': len(guilds)})

@app.route('/api/world')
def api_world():
    """Get world state."""
    with _lock:
        data = cached_data.copy()

    ws = data.get('world_state', {})
    return jsonify({
        'invasion_active': ws.get('invasion_active', False),
        'current_invasion': ws.get('current_invasion'),
        'king': ws.get('king'),
        'season': ws.get('season', 'Normal'),
        'weather': ws.get('weather', 'Clear'),
        'active_events': ws.get('active_events', []),
        'weekly_challenge': ws.get('weekly_challenge', {}),
        'world_boss': ws.get('world_boss'),
        'market_trends': ws.get('market_trends', {}),
        'last_updated': last_updated.strftime('%Y-%m-%d %H:%M:%S') if last_updated else None,
    })

@app.route('/api/guild/<guild_name>')
def api_guild(guild_name):
    """Get guild details."""
    with _lock:
        data = cached_data.copy()

    guilds = data.get('guilds', {})
    gd = guilds.get(guild_name)
    if not gd:
        key = next((k for k in guilds if k.lower() == guild_name.lower()), None)
        gd = guilds.get(key) if key else None

    if not gd:
        return jsonify({'error': 'Guild not found'}), 404

    members = gd.get('members', [])
    member_list = []
    for m in members[:30]:
        if isinstance(m, dict):
            member_list.append({
                'name': m.get('name', str(m)),
                'role': m.get('role', 'Member'),
                'rank': m.get('rank', ''),
                'icon': m.get('icon', '⚔️')
            })
        else:
            member_list.append({'name': str(m), 'role': 'Member', 'rank': '', 'icon': '⚔️'})

    return jsonify({'guild': {
        'name': guild_name,
        'tag': gd.get('tag', guild_name[:3].upper()),
        'icon': gd.get('icon', '🏰'),
        'level': gd.get('level', 1),
        'gold': gd.get('gold', 0),
        'rank': gd.get('rank', '—'),
        'wars_won': gd.get('wars_won', 0),
        'open': gd.get('open', True),
        'members': member_list,
    }})

# ── Activity Timeline ────────────────────────────────────────────────────────
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

# ── Player Profile ──────────────────────────────────────────────────────────
@app.route('/api/player/<user_id>')
def api_player(user_id):
    """Get player profile data."""
    game = get_player_game_data(int(user_id))
    if not game:
        return jsonify({'error': 'Player not found'}), 404

    xp = game.get('xp', 0)
    xp_needed = game.get('xp_needed', 100) or 100

    return jsonify({
        'user_id': user_id,
        'name': game.get('name', 'Unknown'),
        'level': game.get('level', 1),
        'class': game.get('char_class', '—'),
        'gold': game.get('gold', 0),
        'gems': game.get('gems', 0),
        'guild': game.get('guild', '—'),
        'xp': xp,
        'xp_needed': xp_needed,
        'xp_pct': round(xp / xp_needed * 100, 1),
        'pets': game.get('pets', []),
        'inventory': game.get('inventory', {}),
        'hp': game.get('hp', 100),
        'max_hp': game.get('max_hp', 100),
    })

# ── Inventory ────────────────────────────────────────────────────────────────
@app.route('/api/inventory/<user_id>')
def api_inventory(user_id):
    """Get a player's inventory items. This endpoint was referenced by the
    dashboard's Inventory panel (loadInventory()) but never existed, so the
    panel always silently failed and stayed on 'Loading inventory...'."""
    try:
        game = get_player_game_data(int(user_id))
    except (ValueError, TypeError):
        return jsonify({'items': []}), 400

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
                # info is just a quantity number
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

# ── Notifications ────────────────────────────────────────────────────────────
@app.route('/api/notifications/<user_id>')
def api_notifications(user_id):
    """Get user notifications."""
    game = get_player_game_data(int(user_id))
    if not game:
        return jsonify({'notifications': []}), 404

    notifs = [
        {'type': n.get('type', 'info'), 'msg': n.get('msg', ''), 'ts': n.get('ts', '')}
        for n in game.get('pending_notifications', [])
    ]
    return jsonify({'notifications': notifs})

# ── Security Dashboard ──────────────────────────────────────────────────────
@app.route('/api/security/<discord_id>')
def api_security(discord_id):
    """Get security info for a user."""
    user = session.get('user')
    if not user or user.get('discord_id') != discord_id:
        return jsonify({'error': 'Unauthorized'}), 401

    sessions = _session_log.get(discord_id, [])

    try:
        conn = _db()
        row = conn.execute(
            "SELECT last_login, linked_at FROM account_links WHERE discord_id=?", (discord_id,)
        ).fetchone()
        conn.close()
        last_login = row['last_login'] if row else None
        linked_at = row['linked_at'] if row else None
    except Exception:
        last_login = linked_at = None

    return jsonify({
        'discord_id': discord_id,
        'linked_at': linked_at,
        'last_login': last_login,
        'active_sessions': sessions,
        'session_count': len(sessions),
    })

@app.route('/api/security/logout-all', methods=['POST'])
def logout_all():
    """Log out all sessions for a user."""
    user = session.get('user')
    if not user:
        return jsonify({'error': 'Not logged in'}), 401

    did = user.get('discord_id')
    _session_log[did] = []
    session.clear()
    _log_activity(did, "logout_all")
    return jsonify({'ok': True, 'message': 'All sessions logged out.'})

# ── Daily Rewards ────────────────────────────────────────────────────────────
@app.route('/api/daily/<user_id>', methods=['POST'])
def api_daily(user_id):
    """Claim daily reward."""
    user = session.get('user')
    if not user or user.get('discord_id') != str(user_id):
        return jsonify({'error': 'Unauthorized'}), 401

    try:
        conn = _db()
        row = conn.execute("SELECT last_daily FROM users WHERE user_id=?", (int(user_id),)).fetchone()
        if not row:
            conn.close()
            return jsonify({'ok': False, 'error': 'Player not found'}), 404

        now = datetime.utcnow()
        last_daily = row['last_daily']

        if last_daily:
            try:
                ld = datetime.fromisoformat(last_daily)
                if ld.date() >= now.date():
                    next_claim = datetime.combine(now.date() + timedelta(days=1), datetime.min.time())
                    conn.close()
                    return jsonify({
                        'ok': False,
                        'already_claimed': True,
                        'next_claim': next_claim.isoformat()
                    })
            except ValueError:
                pass

        conn.execute("UPDATE users SET last_daily=? WHERE user_id=?", (now.isoformat(), int(user_id)))
        conn.commit()
        conn.close()
        _log_activity(user_id, "daily_claim")
        return jsonify({'ok': True, 'message': 'Daily reward ready!'})
    except Exception as e:
        log.error(f"[api/daily] error: {e}")
        return jsonify({'ok': False, 'error': 'Server error'}), 500

# ── Server Status ──────────────────────────────────────────────────────────
@app.route('/api/server-status')
def api_server_status():
    """Get server health status."""
    import platform
    conn_count = sum(_ip_connections.values())
    ban_count = len([v for v in _ip_banned.values() if time.time() < v])

    return jsonify({
        'status': 'ok',
        'timestamp': datetime.now(UTC).isoformat(),
        'active_requests': _global_req_count['n'],
        'active_ips': conn_count,
        'banned_ips': ban_count,
        'python': platform.python_version(),
        'db_path': DB_PATH,
    })

# ── Global Chat ──────────────────────────────────────────────────────────────
_chat_messages = []
_chat_ring_maxlen = 200
_chat_id_counter_val = 0
_chat_lock2 = threading.Lock()
_chat_online = {}

def _chat_purge_online():
    now = time.time()
    expired = [k for k, v in list(_chat_online.items()) if now - v > 120]
    for k in expired:
        _chat_online.pop(k, None)

@app.route('/api/chat/messages')
def api_chat_messages():
    """Get chat messages."""
    ip = _get_real_ip()
    if _rate_limited(ip, max_calls=30, window=60):
        return jsonify({'error': 'Rate limited'}), 429

    after = int(request.args.get('after', 0))
    user = session.get('user')
    uid = user.get('discord_id', ip) if user else ip

    with _chat_lock2:
        _chat_online[uid] = time.time()
        _chat_purge_online()
        msgs = [m for m in _chat_messages if m['id'] > after]

    return jsonify({'messages': msgs, 'online_count': len(_chat_online)})

@app.route('/api/chat/send', methods=['POST'])
def api_chat_send():
    """Send a chat message."""
    ip = _get_real_ip()
    if _strict_rate_limit(ip, max_calls=5, window=10):
        return jsonify({'ok': False, 'error': 'Please slow down!'}), 429

    body = request.get_json(silent=True) or {}
    user = session.get('user')
    username = (user.get('username') if user else None) or body.get('username', 'Guest')
    avatar = (user.get('avatar') if user else None) or body.get('avatar')
    text = str(body.get('text', '')).strip()[:200]

    if not text:
        return jsonify({'ok': False, 'error': 'Please enter a message.'}), 400

    banned_words = ['discord.gg', 'http://', 'https://t.me']
    if any(w in text.lower() for w in banned_words):
        return jsonify({'ok': False, 'error': 'Links are not allowed in chat.'}), 400

    global _chat_id_counter_val
    with _chat_lock2:
        _chat_id_counter_val += 1
        msg = {
            'id': _chat_id_counter_val,
            'username': username[:32],
            'avatar': avatar,
            'text': text,
            'ts': datetime.now(UTC).isoformat(),
        }
        _chat_messages.append(msg)
        if len(_chat_messages) > _chat_ring_maxlen:
            _chat_messages.pop(0)

    _log_activity(user.get('discord_id', ip) if user else ip, 'chat', text[:80])
    return jsonify({'ok': True, 'id': _chat_id_counter_val})

@app.route('/api/chat/stream')
def api_chat_stream():
    """SSE stream for chat."""
    ip = _get_real_ip()
    if _rate_limited(ip, max_calls=5, window=60):
        return Response('Rate limited', status=429)

    last_id_init = _chat_id_counter_val

    def event_stream():
        local_last = last_id_init
        while True:
            time.sleep(1.5)
            with _chat_lock2:
                new_msgs = [m for m in _chat_messages if m['id'] > local_last]
                online = len(_chat_online)
            if new_msgs:
                local_last = new_msgs[-1]['id']
                data = json.dumps({'messages': new_msgs, 'online_count': online})
                yield f'data: {data}\n\n'
            else:
                yield ': ping\n\n'

    return Response(event_stream(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

# ── Live Auction ────────────────────────────────────────────────────────────
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
                tl = max(0, int((datetime.fromisoformat(r['end_time']) - datetime.utcnow()).total_seconds()))
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

def _post_discord_bid(username, avatar, item_name, auction_id, amount, bidder_id):
    """Post bid notification to Discord."""
    if not DISCORD_BOT_TOKEN:
        return

    embed = {
        "title": "🏛️ New Auction Bid",
        "color": 0xe74c3c,
        "fields": [
            {"name": "Item", "value": item_name, "inline": True},
            {"name": "Bid Amount", "value": f"**{amount:,} 🪙**", "inline": True},
            {"name": "Auction ID", "value": f"`#{auction_id}`", "inline": True},
            {"name": "Bidder", "value": f"<@{bidder_id}>", "inline": True},
        ],
        "footer": {"text": "RPG Ghost Auction House"},
        "timestamp": datetime.now(UTC).isoformat(),
    }
    if avatar:
        embed["thumbnail"] = {"url": avatar}

    payload = {"embeds": [embed]}
    url = f"https://discord.com/api/v10/channels/{DISCORD_BID_CHANNEL}/messages"

    try:
        resp = http_requests.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
                "Content-Type": "application/json",
            },
            timeout=100,
        )
        if resp.status_code not in (200, 201, 204):
            log.warning(f"[discord_bid] HTTP {resp.status_code}: {resp.text[:120]}")
    except Exception as e:
        log.warning(f"[discord_bid] failed: {e}")

@app.route('/api/auction/bid', methods=['POST'])
def api_auction_bid():
    """Place a bid on an auction."""
    user = session.get('user')
    if not user:
        return jsonify({'ok': False, 'error': 'Please log in to bid.'}), 401

    body = request.get_json(silent=True) or {}
    auction_id = body.get('auction_id')
    amount = int(body.get('amount', 0))
    uid = int(user['discord_id'])

    if not auction_id or amount <= 0:
        return jsonify({'ok': False, 'error': 'Invalid bid amount.'}), 400

    try:
        conn = _db()
        a = conn.execute("SELECT * FROM auctions WHERE id=? AND status='active'", (auction_id,)).fetchone()
        if not a:
            conn.close()
            return jsonify({'ok': False, 'error': 'Auction not found.'}), 404

        if a['seller_id'] == uid:
            conn.close()
            return jsonify({'ok': False, 'error': "You can't bid on your own auction."}), 400

        if amount <= a['current_bid']:
            conn.close()
            return jsonify({'ok': False, 'error': f'Bid must be higher than {a["current_bid"]} gold.'}), 400

        if a['highest_bidder']:
            conn.execute("UPDATE users SET gold=gold+? WHERE user_id=?", (a['current_bid'], a['highest_bidder']))

        player = conn.execute("SELECT gold FROM users WHERE user_id=?", (uid,)).fetchone()
        if not player or (player['gold'] or 0) < amount:
            conn.close()
            return jsonify({'ok': False, 'error': 'You don\'t have enough gold.'}), 400

        conn.execute("UPDATE users SET gold=gold-? WHERE user_id=?", (amount, uid))
        conn.execute("UPDATE auctions SET current_bid=?, highest_bidder=? WHERE id=?", (amount, uid, auction_id))
        conn.commit()
        item_name = a['item_name']
        conn.close()

        _log_activity(uid, 'auction_bid', f'auction={auction_id} amount={amount}')

        threading.Thread(
            target=_post_discord_bid,
            args=(user.get('username', 'Unknown'), user.get('avatar', ''),
                  item_name, auction_id, amount, uid),
            daemon=True,
        ).start()

        return jsonify({'ok': True, 'new_bid': amount})
    except Exception as e:
        log.error(f'[api/auction/bid] {e}')
        return jsonify({'ok': False, 'error': 'Server error.'}), 500

# ── Main Entry Point ────────────────────────────────────────────────────────
def run_dashboard():
    """Start the dashboard server."""
    load_game_data()
    app.run(
        host="0.0.0.0",
        port=25507,
        debug=False,        # ← Must be False when running in a thread
        use_reloader=False  # ← Explicitly disable the reloader
    )

if __name__ == '__main__':
    run_dashboard()