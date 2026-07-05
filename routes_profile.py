"""
Player Profile — extended data endpoint.

Matches the real backend in dashboard_app.py: sqlite-backed `players` table
(JSON blob per user, read via get_player_game_data / cached_data), the
`activity_log` table (same one /api/activity/{user_id} already reads), and
session-based auth via request.session['user'] (same as /api/me, /api/profile).

Drop this file next to dashboard_app.py and add near the other routes:

    from routes_profile import register_profile_routes
    register_profile_routes(app)

It reuses dashboard_app's existing globals/helpers directly (_db, cached_data,
_lock, get_player_game_data) rather than redefining them, so it needs those
importable from dashboard_app.
"""

from fastapi import Request


def register_profile_routes(app):
    # Import here (not at module top) to avoid circular imports, since
    # dashboard_app.py will import this module.
    from dashboard_app import _db, cached_data, _lock, get_player_game_data

    @app.get('/api/profile/{discord_id}/extended')
    async def api_profile_extended(discord_id: str, request: Request):
        """
        Returns everything the Player Profile page needs beyond the base
        /api/me payload: prestige, guild, rank, stats, showcase, pinned
        achievements, activity timeline, and recent rewards.

        Response shape consumed by profile.html:

        {
            "prestige": int,
            "equipped_title": str | null,
            "guild": { "id": str, "name": str } | null,
            "global_rank": int | null,
            "joined_at": "ISO-8601 timestamp" | null,
            "stats": {
                "play_time_hours": int, "commands_used": int, "win_rate": int,
                "damage_dealt": int, "healing_done": int, "boss_kills": int,
                "dungeons_completed": int, "battles_won": int
            },
            "showcase": {
                "favorite_weapon": { "name": str, "rarity": str } | null,
                "favorite_pet": { "name": str, "level": int } | null
            },
            "pinned_achievements": [ { "name": str, "icon": str, "rarity": str } ],
            "activity_timeline": [ { "title": str, "description": str, "timestamp": str } ],
            "recent_rewards": [ { "name": str, "icon": str, "amount": int, "timestamp": str } ]
        }
        """
        try:
            uid = int(discord_id)
        except (ValueError, TypeError):
            return {"error": "invalid discord_id"}, 400

        # Only the logged-in user can view their own extended profile for now.
        # (Loosen this if you want public profile pages later.)
        session_user = request.session.get('user')
        if not session_user or str(session_user.get('discord_id')) != str(uid):
            return {"error": "unauthorized"}, 403

        game = get_player_game_data(uid) or {}

        # ── Prestige / title / guild ────────────────────────────────
        prestige = game.get('prestige_level', 0)
        equipped_title = game.get('equipped_title') or game.get('title')

        guild = None
        guild_name = game.get('guild')
        if guild_name and guild_name != '—':
            with _lock:
                guild_doc = cached_data.get('guilds', {}).get(guild_name)
            if guild_doc:
                guild = {"id": guild_name, "name": guild_doc.get('name', guild_name)}
            else:
                guild = {"id": guild_name, "name": guild_name}

        # ── Global rank (by XP), reusing the same cached_data /api/leaderboard uses ──
        with _lock:
            players = cached_data.get('players', {})
        my_xp = game.get('xp', 0)
        higher_xp_count = sum(1 for pd in players.values() if pd.get('xp', 0) > my_xp)
        global_rank = higher_xp_count + 1 if players else None

        # ── Stats block ────────────────────────────────────────────
        stats = {
            "play_time_hours":    game.get('play_time_hours', 0),
            "commands_used":      game.get('commands_used', 0),
            "win_rate":           game.get('win_rate', 0),
            "damage_dealt":       game.get('damage_dealt', 0),
            "healing_done":       game.get('healing_done', 0),
            "boss_kills":         game.get('boss_kills', 0),
            "dungeons_completed": game.get('dungeons_completed', 0),
            "battles_won":        game.get('battles_won', 0),
        }

        # ── Showcase (favorite weapon / pet) ──────────────────────
        showcase = {}
        raw_inventory = game.get('inventory', {})
        fav_weapon_name = game.get('favorite_weapon')
        if fav_weapon_name and isinstance(raw_inventory, dict) and fav_weapon_name in raw_inventory:
            w = raw_inventory[fav_weapon_name]
            showcase["favorite_weapon"] = {
                "name": fav_weapon_name,
                "rarity": (w.get('rarity', 'common') if isinstance(w, dict) else 'common'),
            }

        raw_pets = game.get('pets', [])
        fav_pet_name = game.get('favorite_pet')
        if fav_pet_name and isinstance(raw_pets, list):
            pet = next((p for p in raw_pets if isinstance(p, dict) and p.get('name') == fav_pet_name), None)
            if pet:
                showcase["favorite_pet"] = {"name": pet.get('name'), "level": pet.get('level', 1)}

        # ── Pinned achievements ────────────────────────────────────
        pinned_ids = set(game.get('pinned_achievements', []))
        all_achievements = game.get('achievements', [])
        pinned_achievements = [
            {
                "name": a.get('name'),
                "icon": a.get('icon', 'fa-medal'),
                "rarity": a.get('rarity', 'common'),
            }
            for a in all_achievements
            if isinstance(a, dict) and a.get('id') in pinned_ids and a.get('unlocked')
        ]

        # ── Activity timeline — same source as /api/activity/{user_id} ─
        activity_timeline = []
        try:
            conn = _db()
            rows = conn.execute(
                "SELECT action, detail, ts FROM activity_log "
                "WHERE user_id=? ORDER BY ts DESC LIMIT 8",
                (str(uid),),
            ).fetchall()
            conn.close()
            activity_timeline = [
                {"title": r['action'], "description": r['detail'] or '', "timestamp": r['ts']}
                for r in rows
            ]
        except Exception:
            activity_timeline = []

        # ── Recent rewards — no dedicated reward log table exists yet.
        # Until one is added, derive a lightweight view from activity_log
        # entries whose action indicates a reward (adjust the filter list
        # to match whatever action strings the bot actually logs).
        recent_rewards = []
        try:
            conn = _db()
            rows = conn.execute(
                "SELECT action, detail, ts FROM activity_log "
                "WHERE user_id=? AND (action LIKE '%reward%' OR action LIKE '%claim%' OR action LIKE '%daily%') "
                "ORDER BY ts DESC LIMIT 6",
                (str(uid),),
            ).fetchall()
            conn.close()
            recent_rewards = [
                {"name": r['action'], "icon": "fa-gift", "amount": 0, "timestamp": r['ts']}
                for r in rows
            ]
        except Exception:
            recent_rewards = []

        # ── Joined date — from account_links, matching the schema in init_db() ──
        joined_at = None
        try:
            conn = _db()
            row = conn.execute(
                "SELECT linked_at FROM account_links WHERE discord_id=?", (str(uid),)
            ).fetchone()
            conn.close()
            if row and row['linked_at']:
                joined_at = row['linked_at']
        except Exception:
            joined_at = None

        return {
            "prestige": prestige,
            "equipped_title": equipped_title,
            "guild": guild,
            "global_rank": global_rank,
            "joined_at": joined_at,
            "stats": stats,
            "showcase": showcase,
            "pinned_achievements": pinned_achievements,
            "activity_timeline": activity_timeline,
            "recent_rewards": recent_rewards,
        }
