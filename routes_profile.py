from fastapi import Request

def _coerce_int(value, default=0):
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _iter_inventory(game):
    """Yield (name, item_dict) whether inventory is a dict or a list.
    The current DEFAULT_STATS doesn't include 'inventory', so this normally
    yields nothing — that's fine, the showcase just stays empty until
    something writes inventory into the player's stats blob."""
    inv = game.get('inventory') or {}
    if isinstance(inv, dict):
        for name, item in inv.items():
            if isinstance(item, dict):
                yield name, item
            else:
                yield name, {'rarity': 'common'}
    elif isinstance(inv, list):
        for item in inv:
            if isinstance(item, dict):
                yield item.get('name'), item


def _iter_pets(game):
    pets = game.get('pets') or []
    if isinstance(pets, dict):
        for name, pet in pets.items():
            if isinstance(pet, dict):
                pet = dict(pet)
                pet.setdefault('name', name)
                yield pet
    elif isinstance(pets, list):
        for pet in pets:
            if isinstance(pet, dict):
                yield pet


def register_profile_routes(app):
    # Import here (not at module top) to avoid circular imports — this module
    # is imported by dashboard_app.py after the app is created.
    from dashboard_app import _db, cached_data, _lock, get_stats, _stats_key

    @app.get('/api/profile/{discord_id}/extended')
    async def api_profile_extended(discord_id: str, request: Request):
        """
        Extended profile for the logged-in user. Response shape matches
        profile.html:

        {
            "prestige": int,
            "equipped_title": str | null,
            "guild": { "id": str, "name": str } | null,
            "global_rank": int | null,
            "joined_at": "ISO-8601" | null,
            "stats": {
                "play_time_hours": int | null,
                "commands_used": int,
                "win_rate": int | null,
                "damage_dealt": int,
                "healing_done": int,
                "boss_kills": int,
                "dungeons_completed": int,
                "battles_won": int
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
        session_user = request.session.get('user')
        if not session_user:
            return {"error": "unauthorized"}, 403

        # session['user']['discord_id'] holds auth_id for email accounts and
        # the Discord snowflake for OAuth. The path param is the same value;
        # compare as strings to cover both.
        if str(session_user.get('discord_id')) != str(discord_id):
            return {"error": "unauthorized"}, 403

        auth_type = session_user.get('auth_type', 'email')
        uid = discord_id  # keep as string; get_stats accepts either

        try:
            game = get_stats(auth_type, uid) or {}
        except Exception:
            game = {}

        # ── Prestige / title ───────────────────────────────────────
        prestige = _coerce_int(game.get('prestige_level', game.get('prestige', 0)))
        equipped_title = game.get('equipped_title') or game.get('title')

        # ── Guild — cached_data['guilds'] is keyed by guild NAME ──
        guild = None
        guild_name = game.get('guild') or game.get('guild_name')
        if guild_name and guild_name != '—':
            with _lock:
                cached_guilds = dict(cached_data.get('guilds', {}))
            doc = cached_guilds.get(guild_name)
            if isinstance(doc, dict):
                guild = {"id": guild_name, "name": guild_name}
            else:
                guild = {"id": str(guild_name), "name": str(guild_name)}

        # ── Combat numbers — only fields the backend actually writes ──
        battles_won = _coerce_int(game.get('battles_won', 0))
        battles_lost_raw = game.get('battles_lost', game.get('losses'))
        battles_lost = _coerce_int(battles_lost_raw) if battles_lost_raw is not None else None

        # win_rate: only computable if the backend has a "losses" field.
        # It doesn't in DEFAULT_STATS, so return None and let the UI hide it
        # rather than showing a misleading 0%.
        if battles_lost is not None and (battles_won + battles_lost) > 0:
            win_rate = int(round((battles_won / (battles_won + battles_lost)) * 100))
        else:
            win_rate = None

        # play_time_hours: DEFAULT_STATS has no playtime field. Only report
        # it if the blob actually contains one; otherwise None (UI hides it).
        if 'play_time_hours' in game:
            play_time_hours = _coerce_int(game.get('play_time_hours'))
        elif 'play_time_seconds' in game:
            play_time_hours = _coerce_int(game.get('play_time_seconds')) // 3600
        else:
            play_time_hours = None

        # ── Global rank by XP ─────────────────────────────────────
        # cached_data['players'] is keyed by account_key ("email:3"), so skip
        # our own account_key — not the bare uid — when counting who's above.
        my_xp = _coerce_int(game.get('xp', 0))
        my_key = _stats_key(auth_type, uid)
        with _lock:
            players = dict(cached_data.get('players', {}))

        higher = 0
        for pid, pd in players.items():
            if str(pid) == my_key:
                continue
            if not isinstance(pd, dict):
                continue
            if _coerce_int(pd.get('xp', 0)) > my_xp:
                higher += 1
        global_rank = (higher + 1) if players else None

        # ── Stats block ───────────────────────────────────────────
        stats = {
            "play_time_hours":    play_time_hours,
            "commands_used":      _coerce_int(game.get('commands_used', 0)),
            "win_rate":           win_rate,
            "damage_dealt":       _coerce_int(game.get('damage_dealt', 0)),
            "healing_done":       _coerce_int(game.get('healing_done', 0)),
            "boss_kills":         _coerce_int(game.get('boss_kills', game.get('world_boss_kills', 0))),
            "dungeons_completed": _coerce_int(game.get('dungeons_completed', 0)),
            "battles_won":        battles_won,
        }

        # ── Showcase ──────────────────────────────────────────────
        showcase = {}

        fav_weapon = game.get('favorite_weapon')
        if fav_weapon:
            for name, item in _iter_inventory(game):
                if name == fav_weapon:
                    rarity = item.get('rarity', 'common') if isinstance(item, dict) else 'common'
                    showcase["favorite_weapon"] = {"name": name, "rarity": rarity}
                    break

        fav_pet = game.get('favorite_pet')
        if fav_pet:
            for pet in _iter_pets(game):
                if pet.get('name') == fav_pet:
                    showcase["favorite_pet"] = {
                        "name": pet.get('name'),
                        "level": _coerce_int(pet.get('level', 1), 1),
                    }
                    break

        # ── Pinned achievements ──────────────────────────────────
        # Nothing in DEFAULT_STATS populates these, so this is normally empty;
        # the check tolerates either the dict-list or id-list shape if some
        # future writer adds them.
        pinned_raw = game.get('pinned_achievements') or []
        pinned_ids = {str(p) for p in pinned_raw} if isinstance(pinned_raw, list) else set()

        pinned_achievements = []
        all_ach = game.get('achievements') or []
        if isinstance(all_ach, list):
            for a in all_ach:
                if not isinstance(a, dict):
                    continue
                if str(a.get('id')) not in pinned_ids:
                    continue
                if a.get('unlocked') is False:
                    continue
                pinned_achievements.append({
                    "name": a.get('name'),
                    "icon": a.get('icon', 'fa-medal'),
                    "rarity": a.get('rarity', 'common'),
                })

        # ── Activity timeline — same table /api/activity/{user_id} reads ─
        activity_timeline = []
        conn = None
        try:
            conn = _db()
            rows = conn.execute(
                "SELECT action, detail, ts FROM activity_log "
                "WHERE user_id=? ORDER BY ts DESC LIMIT 8",
                (str(uid),),
            ).fetchall()
            activity_timeline = [
                {"title": r['action'], "description": r['detail'] or '', "timestamp": r['ts']}
                for r in rows
            ]
        except Exception:
            activity_timeline = []
        finally:
            if conn is not None:
                conn.close()

        # ── Recent rewards — derived from activity_log; there is no reward
        # table in this backend yet. Filter matches what actually shows up
        # for hunt / daily / claim actions logged via _log_activity.
        recent_rewards = []
        conn = None
        try:
            conn = _db()
            rows = conn.execute(
                "SELECT action, detail, ts FROM activity_log "
                "WHERE user_id=? AND ("
                "  action LIKE '%reward%' OR action LIKE '%claim%' "
                "  OR action LIKE '%daily%' OR action LIKE '%hunt%'"
                ") ORDER BY ts DESC LIMIT 6",
                (str(uid),),
            ).fetchall()
            recent_rewards = [
                {"name": r['action'], "icon": "fa-gift", "amount": 0, "timestamp": r['ts']}
                for r in rows
            ]
        except Exception:
            recent_rewards = []
        finally:
            if conn is not None:
                conn.close()

        # ── Joined date ─────────────────────────────────────────
        # Discord OAuth writes account_links.linked_at; email accounts do not.
        # Fall back to auth_users.created_at for the email case.
        joined_at = None
        if auth_type == 'email':
            conn = None
            try:
                conn = _db()
                row = conn.execute(
                    "SELECT created_at FROM auth_users WHERE auth_id = ?",
                    (_coerce_int(uid),),
                ).fetchone()
                if row and row['created_at']:
                    joined_at = row['created_at']
            except Exception:
                joined_at = None
            finally:
                if conn is not None:
                    conn.close()
        else:
            conn = None
            try:
                conn = _db()
                row = conn.execute(
                    "SELECT linked_at FROM account_links WHERE discord_id=?",
                    (str(uid),),
                ).fetchone()
                if row and row['linked_at']:
                    joined_at = row['linked_at']
            except Exception:
                joined_at = None
            finally:
                if conn is not None:
                    conn.close()

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
