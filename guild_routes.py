"""
Guild Hall routes: my-guild, guild browse/create/join/edit/leave, guild chat,
and bot-info. Mirrors the register_*_routes(app, _db) pattern used by
community.py and game_engine.py, and reuses the existing `guilds` and
`player_stats` tables (no changes to those schemas required).

Data model
----------
guilds.data (JSON) shape:
    {
      "tag": "IV",
      "description": "...",
      "motd": "...",
      "leader_key": "email:3",
      "leader_name": "Some Nickname",
      "created_at": "<iso8601>",
      "max_members": 50,
      "treasury": 0,
      "level": 1,
      "members": {
        "email:3": {"name": "Some Nickname", "role": "Leader", "joined_at": "<iso8601>"},
        "discord:123456789012345678": {"name": "Other", "role": "Member", "joined_at": "<iso8601>"}
      }
    }

A member's account_key is "<auth_type>:<discord_id>" — the same key used by
get_stats/save_stats in main.py (_stats_key). Each member's `guild` field in
player_stats is kept in sync with the guild name so the leaderboard / home
dashboard (which already derive guild membership from player_stats) continue
to work unmodified.

New table: guild_messages — guild chat history (not covered by any existing
table).
"""

import json
import logging
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)

MAX_MEMBERS_DEFAULT = 50
CHAT_HISTORY_LIMIT = 50


def _json(content: dict, status_code: int = 200):
    return JSONResponse(content=content, status_code=status_code)


async def _get_json(request: Request) -> dict:
    try:
        return await request.json()
    except Exception:
        return {}


def _account_key(user: dict) -> str:
    return f"{user.get('auth_type', 'email')}:{user['discord_id']}"


def _init_guild_tables(db):
    conn = db()
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS guild_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_name TEXT NOT NULL,
            account_key TEXT NOT NULL,
            username TEXT,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_guild_messages_guild ON guild_messages(guild_name, id)"
        )
        conn.commit()
    finally:
        conn.close()


# ── guild row helpers ───────────────────────────────────────────────────────
def _normalize_guild(name, data):
    data = dict(data or {})
    data['name'] = name
    data.setdefault('members', {})
    data.setdefault('max_members', MAX_MEMBERS_DEFAULT)
    data.setdefault('treasury', 0)
    data.setdefault('level', 1)
    data.setdefault('tag', '')
    data.setdefault('description', '')
    data.setdefault('motd', '')
    data.setdefault('leader_name', 'Unknown')
    return data


def _load_guild(conn, name):
    row = conn.execute("SELECT name, data FROM guilds WHERE name = ?", (name,)).fetchone()
    if not row:
        return None
    try:
        data = json.loads(row['data'])
    except Exception:
        data = {}
    return _normalize_guild(row['name'], data)


def _save_guild(conn, name, data):
    payload = {k: v for k, v in data.items() if k != 'name'}
    conn.execute(
        "INSERT INTO guilds (name, data, last_updated) VALUES (?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(name) DO UPDATE SET data=excluded.data, last_updated=CURRENT_TIMESTAMP",
        (name, json.dumps(payload)),
    )


def _find_member_guild(conn, account_key):
    """Scan guilds for the one this account_key belongs to. Returns (name, data) or (None, None)."""
    for row in conn.execute("SELECT name, data FROM guilds").fetchall():
        try:
            data = json.loads(row['data'])
        except Exception:
            continue
        if account_key in (data.get('members') or {}):
            return row['name'], _normalize_guild(row['name'], data)
    return None, None


def _player_level(conn, account_key):
    row = conn.execute("SELECT data FROM player_stats WHERE account_key=?", (account_key,)).fetchone()
    if row:
        try:
            return json.loads(row['data']).get('level', 1)
        except Exception:
            pass
    return 1


def _set_player_guild(conn, account_key, guild_name):
    """Keep player_stats.guild in sync so the leaderboard/dashboard (which
    already read this field) reflect guild membership without changes there."""
    row = conn.execute("SELECT data FROM player_stats WHERE account_key=?", (account_key,)).fetchone()
    try:
        data = json.loads(row['data']) if row else {}
    except Exception:
        data = {}
    data['guild'] = guild_name or '—'
    conn.execute(
        "INSERT INTO player_stats (account_key, data, last_updated) VALUES (?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(account_key) DO UPDATE SET data=excluded.data, last_updated=CURRENT_TIMESTAMP",
        (account_key, json.dumps(data)),
    )


def _guild_summary(name, data):
    members = data.get('members', {})
    return {
        'id': name,
        'name': name,
        'tag': data.get('tag', ''),
        'description': data.get('description', ''),
        'level': data.get('level', 1),
        'member_count': len(members),
        'max_members': data.get('max_members', MAX_MEMBERS_DEFAULT),
        'leader_name': data.get('leader_name', 'Unknown'),
    }


def register_guild_routes(app: FastAPI, db):
    """db: the same `_db` connection-factory callable used elsewhere in main.py."""
    _init_guild_tables(db)

    # ── my-guild ─────────────────────────────────────────────────────────
    @app.get('/api/my-guild')
    async def api_my_guild(request: Request):
        user = request.session.get('user')
        if not user:
            return {'guild': None}

        account_key = _account_key(user)
        conn = db()
        try:
            name, data = _find_member_guild(conn, account_key)
            if not name:
                return {'guild': None}

            members_raw = data.get('members', {})
            role_rank = {'Leader': 0, 'Officer': 1, 'Member': 2}
            members = sorted(
                (
                    {
                        'name': info.get('name', 'Unknown'),
                        'level': _player_level(conn, key),
                        'role': info.get('role', 'Member'),
                    }
                    for key, info in members_raw.items()
                ),
                key=lambda m: (role_rank.get(m['role'], 3), m['name'].lower()),
            )

            return {
                'guild': {
                    'id': name,
                    'name': name,
                    'tag': data.get('tag', ''),
                    'description': data.get('description', ''),
                    'motd': data.get('motd', ''),
                    'leader_name': data.get('leader_name', 'Unknown'),
                    'level': data.get('level', 1),
                    'member_count': len(members_raw),
                    'max_members': data.get('max_members', MAX_MEMBERS_DEFAULT),
                    'treasury': data.get('treasury', 0),
                    'is_leader': members_raw.get(account_key, {}).get('role') == 'Leader',
                    'members': members,
                }
            }
        finally:
            conn.close()

    # ── browse ───────────────────────────────────────────────────────────
    @app.get('/api/guilds')
    async def api_guilds_list():
        conn = db()
        try:
            rows = conn.execute("SELECT name, data FROM guilds ORDER BY name").fetchall()
            guilds = []
            for row in rows:
                try:
                    data = json.loads(row['data'])
                except Exception:
                    data = {}
                guilds.append(_guild_summary(row['name'], _normalize_guild(row['name'], data)))
            guilds.sort(key=lambda g: (-g['member_count'], g['name'].lower()))
            return {'guilds': guilds}
        finally:
            conn.close()

    # ── create ───────────────────────────────────────────────────────────
    @app.post('/api/guild/create')
    async def api_guild_create(request: Request):
        user = request.session.get('user')
        if not user:
            return _json({'success': False, 'error': 'Not logged in'}, 401)

        body = await _get_json(request)
        name = str(body.get('name', '')).strip()[:32]
        description = str(body.get('description', '')).strip()[:200]
        tag = str(body.get('tag', '')).strip()[:5]

        if not name:
            return _json({'success': False, 'error': 'Guild name is required.'}, 400)

        account_key = _account_key(user)
        conn = db()
        try:
            existing_name, _ = _find_member_guild(conn, account_key)
            if existing_name:
                return _json(
                    {'success': False, 'error': 'You are already in a guild. Leave it first.'}, 400
                )

            if conn.execute("SELECT 1 FROM guilds WHERE name = ?", (name,)).fetchone():
                return _json({'success': False, 'error': 'A guild with that name already exists.'}, 400)

            leader_name = user.get('username', 'Unknown')
            now = datetime.now(timezone.utc).isoformat()
            data = {
                'tag': tag,
                'description': description,
                'motd': '',
                'leader_key': account_key,
                'leader_name': leader_name,
                'created_at': now,
                'max_members': MAX_MEMBERS_DEFAULT,
                'treasury': 0,
                'level': 1,
                'members': {
                    account_key: {'name': leader_name, 'role': 'Leader', 'joined_at': now},
                },
            }
            _save_guild(conn, name, data)
            _set_player_guild(conn, account_key, name)
            conn.commit()
            log.info(f"[Guild] '{name}' created by {account_key}")
            return {'success': True, 'guild': {'name': name}}
        finally:
            conn.close()

    # ── join ─────────────────────────────────────────────────────────────
    @app.post('/api/guild/join')
    async def api_guild_join(request: Request):
        user = request.session.get('user')
        if not user:
            return _json({'success': False, 'error': 'Not logged in'}, 401)

        body = await _get_json(request)
        guild_id = str(body.get('guild_id', '')).strip()
        if not guild_id:
            return _json({'success': False, 'error': 'Missing guild.'}, 400)

        account_key = _account_key(user)
        conn = db()
        try:
            existing_name, _ = _find_member_guild(conn, account_key)
            if existing_name:
                return _json(
                    {'success': False, 'error': 'You are already in a guild. Leave it first.'}, 400
                )

            data = _load_guild(conn, guild_id)
            if not data:
                return _json({'success': False, 'error': 'Guild not found.'}, 404)

            members = data.get('members', {})
            if len(members) >= data.get('max_members', MAX_MEMBERS_DEFAULT):
                return _json({'success': False, 'error': 'This guild is full.'}, 400)

            members[account_key] = {
                'name': user.get('username', 'Unknown'),
                'role': 'Member',
                'joined_at': datetime.now(timezone.utc).isoformat(),
            }
            data['members'] = members
            _save_guild(conn, guild_id, data)
            _set_player_guild(conn, account_key, guild_id)
            conn.commit()
            return {'success': True}
        finally:
            conn.close()

    # ── edit (leader only) ──────────────────────────────────────────────
    @app.post('/api/guild/edit')
    async def api_guild_edit(request: Request):
        user = request.session.get('user')
        if not user:
            return _json({'success': False, 'error': 'Not logged in'}, 401)

        account_key = _account_key(user)
        conn = db()
        try:
            name, data = _find_member_guild(conn, account_key)
            if not name:
                return _json({'success': False, 'error': 'You are not in a guild.'}, 400)
            if data.get('members', {}).get(account_key, {}).get('role') != 'Leader':
                return _json({'success': False, 'error': 'Only the guild leader can edit the guild.'}, 403)

            body = await _get_json(request)

            new_name = str(body.get('name', '')).strip()[:32]
            if new_name and new_name != name:
                if conn.execute("SELECT 1 FROM guilds WHERE name = ?", (new_name,)).fetchone():
                    return _json(
                        {'success': False, 'error': 'A guild with that name already exists.'}, 400
                    )
                conn.execute("UPDATE guilds SET name=? WHERE name=?", (new_name, name))
                conn.execute(
                    "UPDATE guild_messages SET guild_name=? WHERE guild_name=?", (new_name, name)
                )
                for key in data.get('members', {}):
                    _set_player_guild(conn, key, new_name)
                name = new_name

            if 'description' in body:
                data['description'] = str(body.get('description') or '').strip()[:200]
            if 'motd' in body:
                data['motd'] = str(body.get('motd') or '').strip()[:120]

            _save_guild(conn, name, data)
            conn.commit()
            return {
                'success': True,
                'guild': {
                    'name': name,
                    'description': data.get('description', ''),
                    'motd': data.get('motd', ''),
                },
            }
        finally:
            conn.close()

    # ── leave ────────────────────────────────────────────────────────────
    @app.post('/api/guild/leave')
    async def api_guild_leave(request: Request):
        user = request.session.get('user')
        if not user:
            return _json({'success': False, 'error': 'Not logged in'}, 401)

        account_key = _account_key(user)
        conn = db()
        try:
            name, data = _find_member_guild(conn, account_key)
            if not name:
                return _json({'success': False, 'error': 'You are not in a guild.'}, 400)

            members = data.get('members', {})
            my_role = members.get(account_key, {}).get('role', 'Member')
            members.pop(account_key, None)

            if not members:
                # last member leaving disbands the guild
                conn.execute("DELETE FROM guilds WHERE name=?", (name,))
                conn.execute("DELETE FROM guild_messages WHERE guild_name=?", (name,))
            else:
                if my_role == 'Leader':
                    # promote the longest-tenured officer (or member, if no officers)
                    def _succession_key(item):
                        key, info = item
                        rank = 0 if info.get('role') == 'Officer' else 1
                        return (rank, info.get('joined_at', ''))

                    next_key, next_info = sorted(members.items(), key=_succession_key)[0]
                    next_info['role'] = 'Leader'
                    data['leader_key'] = next_key
                    data['leader_name'] = next_info.get('name', 'Unknown')
                data['members'] = members
                _save_guild(conn, name, data)

            _set_player_guild(conn, account_key, '—')
            conn.commit()
            log.info(f"[Guild] {account_key} left '{name}'")
            return {'success': True}
        finally:
            conn.close()

    # ── chat ─────────────────────────────────────────────────────────────
    @app.get('/api/chat/messages')
    async def api_chat_messages(request: Request):
        user = request.session.get('user')
        if not user:
            return {'messages': []}

        account_key = _account_key(user)
        conn = db()
        try:
            name, _ = _find_member_guild(conn, account_key)
            if not name:
                return {'messages': []}

            rows = conn.execute(
                "SELECT username, text, created_at FROM guild_messages "
                "WHERE guild_name=? ORDER BY id DESC LIMIT ?",
                (name, CHAT_HISTORY_LIMIT),
            ).fetchall()
            messages = [
                {'username': r['username'] or 'Unknown', 'text': r['text'], 'ts': r['created_at']}
                for r in reversed(rows)
            ]
            return {'messages': messages}
        finally:
            conn.close()

    @app.post('/api/chat/send')
    async def api_chat_send(request: Request):
        user = request.session.get('user')
        if not user:
            return _json({'ok': False, 'error': 'Not logged in'}, 401)

        account_key = _account_key(user)
        conn = db()
        try:
            name, _ = _find_member_guild(conn, account_key)
            if not name:
                return _json({'ok': False, 'error': 'You are not in a guild.'}, 400)

            body = await _get_json(request)
            text = str(body.get('text', '')).strip()[:200]
            if not text:
                return _json({'ok': False, 'error': 'Message is empty.'}, 400)

            conn.execute(
                "INSERT INTO guild_messages (guild_name, account_key, username, text, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (name, account_key, user.get('username', 'Unknown'), text,
                 datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
            return {'ok': True}
        finally:
            conn.close()

    # ── bot intro overlay ────────────────────────────────────────────────
    @app.get('/api/bot-info')
    async def api_bot_info():
        conn = db()
        try:
            row = conn.execute(
                "SELECT nickname FROM auth_users WHERE is_admin=1 ORDER BY auth_id ASC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        return {
            'name': 'RPG Ghost',
            'owner_username': (row['nickname'] if row and row['nickname'] else 'the bot owner'),
            'description': (
                "I'm RPG Ghost, a text-based adventure bot for Discord — battle monsters, "
                "loot gear, trade in the auction house, and rise up the leaderboard with your guild."
            ),
        }
