from fastapi import Request
from fastapi.responses import JSONResponse


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
    """Yield (name, item_dict) whether inventory is a dict or a list."""
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
    # cached_data is re-assigned by load_game_data(), so read it through the module at call time
    # (importing the name here would freeze a reference to the original empty dict).
    import dashboard_app as da
    from dashboard_app import _db, _lock, get_stats, _stats_key

    @app.get('/api/profile/{discord_id}/extended')
    def api_profile_extended(discord_id: str, request: Request):
        """
        Extended profile for the logged-in user. Response shape matches
        profile.html:

        {
            "prestige": int,
            "equipped_title": str | null,
            "guild": { "id": str, "name": str } | null,
            "global_rank": int | null,
            "joined_at": "ISO-8601" | null,
            "stats": { ... },
            "showcase": { "favorite_weapon": ..., "favorite_pet": ... },
            "pinned_achievements": [ ... ],
            "activity_timeline": [ ... ],
            "recent_rewards": [ ... ]
        }
        """
        session_user = request.session.get('user')
        if not session_user:
            return JSONResponse({"error": "unauthorized"}, status_code=403)

        # session['user']['discord_id'] holds auth_id for email accounts and
        # the Discord snowflake for OAuth. The path param is the same value;
        # compare as strings to cover both.
        if str(session_user.get('discord_id')) != str(discord_id):
            return JSONResponse({"error": "unauthorized"}, status_code=403)

        auth_type = session_user.get('auth_type', 'discord')
        uid = discord_id  # keep as string; get_stats accepts either

        try:
            game = get_stats(auth_type, uid) or {}
        except Exception:
            game = {}

        # ── Game engine overlay ────────────────────────────────────
        # game_engine.py keeps its data in the same player_stats row, so
        # inventory, battles_lost, boss_kills, pinned_achievements and
        # equipped_title are already in `game`. The overlay adds the values
        # the engine derives (lifetime XP, achievement list, equipped weapon,
        # global rank).
        engine_rank = None
        try:
            from game_engine import profile_overlay
            ov = profile_overlay(auth_type, uid)
            engine_rank = ov.get('_global_rank')
            game = {**game, **{k: v for k, v in ov.items() if not k.startswith('_') and v is not None}}
        except Exception:
            pass

        # ── Prestige / title ───────────────────────────────────────
        prestige = _coerce_int(game.get('prestige_level', game.get('prestige', 0)))
        equipped_title = game.get('equipped_title') or game.get('title')

        # ── Guild — cached_data['guilds'] is keyed by guild NAME ──
        guild = None
        guild_name = game.get('guild') or game.get('guild_name')
        if guild_name and guild_name != '—':
            with _lock:
                cached_guilds = dict(da.cached_data.get('guilds', {}))
            doc = cached_guilds.get(guild_name)
            if isinstance(doc, dict):
                guild = {"id": guild_name, "name": guild_name}
            else:
                guild = {"id": str(guild_name), "name": str(guild_name)}

        # ── Combat numbers ─────────────────────────────────────────
        battles_won = _coerce_int(game.get('battles_won', 0))
        battles_lost_raw = game.get('battles_lost', game.get('losses'))
        battles_lost = _coerce_int(battles_lost_raw) if battles_lost_raw is not None else None

        # win_rate: only computable if we know the losses; otherwise None and
        # the UI hides it rather than showing a misleading 0%.
        if battles_lost is not None and (battles_won + battles_lost) > 0:
            win_rate = int(round((battles_won / (battles_won + battles_lost)) * 100))
        else:
            win_rate = None

        if 'play_time_hours' in game:
            play_time_hours = _coerce_int(game.get('play_time_hours'))
        elif 'play_time_seconds' in game:
            play_time_hours = _coerce_int(game.get('play_time_seconds')) // 3600
        else:
            play_time_hours = None

        # ── Global rank by lifetime XP ────────────────────────────
        if engine_rank is not None:
            global_rank = engine_rank
        else:
            my_xp = _coerce_int(game.get('xp', 0))
            my_key = _stats_key(auth_type, uid)
            with _lock:
                players = dict(da.cached_data.get('players', {}))
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
        pinned_raw = game.get('pinned_achievements') or game.get('pinned') or []
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

        # ── Activity timeline — same table /api/game/activity reads ─
        activity_timeline = []
        conn = None
        try:
            conn = _db()
            rows = conn.execute(
                "SELECT action, detail, ts FROM activity_log "
                "WHERE user_id=? ORDER BY id DESC LIMIT 8",
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

        # ── Recent rewards — derived from activity_log ────────────
        recent_rewards = []
        conn = None
        try:
            conn = _db()
            rows = conn.execute(
                "SELECT action, detail, ts FROM activity_log "
                "WHERE user_id=? AND ("
                "  action LIKE '%reward%' OR action LIKE '%claim%' "
                "  OR action LIKE '%daily%' OR action LIKE '%hunt%'"
                ") ORDER BY id DESC LIMIT 6",
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
        joined_at = None
        if True:  # email accounts (auth_users) no longer exist; every account has an account_links row
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
