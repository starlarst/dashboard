"""
game_engine.py - standalone, server-authoritative game engine for the web RPG.

Plugs into the dashboard's EXISTING stores, so there is one source of truth:
  * player_stats (account_key -> JSON blob)  - same row get_stats()/save_stats(),
    /api/me, the admin panel and the old leaderboard already read and write.
    The engine keeps level/xp/xp_needed/gold/gems/char_class/battles_won in the
    same keys, and adds hp/mp/inventory/quests/etc. beside them in the blob.
  * activity_log - hunts, heals, level-ups, ... are appended, so battle-log works.

No Discord bot involved. Hook-up in dashboard_app.py, right after
`register_profile_routes(app)`:

    from game_engine import register_game_routes
    register_game_routes(app, _db)
"""
import json
import logging
import random
import threading
import time
from datetime import datetime, timezone, timedelta

from fastapi import Request
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)

_DB = None                     # set by register_game_routes(app, db_factory)
_state_lock = threading.Lock()
_last_hunt = {}                # account_key -> monotonic time of last hunt

# ───────────────────────── balance (same numbers the old client-side sim used)
HEAL_COST = 30
DAILY_COOLDOWN = timedelta(hours=24)
HUNT_MIN_INTERVAL = 1.0        # seconds between hunts (anti-spam)
SPIRIT_ROUND_MIN_SECONDS = 18  # a round is 20s; reject anything faster
SPIRIT_MAX_SCORE = 40
HUNT_WIN_CHANCE = 0.70
HUNT_ENEMIES = ['Slime', 'Goblin', 'Skeleton', 'Wraith', 'Dire Wolf', 'Bone Drake']
LOOT_CHANCE = 0.25
MAX_LEVEL = 1000

LOOT_TABLE = {
    'common': [
        ('Rusty Sword', 'A chipped blade. Still sharp enough.'),
        ('Healing Potion', 'Smells awful. Works fine.'),
        ('Wooden Shield', 'Splinters, but it blocks.'),
        ('Old Scroll', 'Faded ink and forgotten spells.'),
    ],
    'rare': [
        ('Steel Dagger', 'Balanced and quick.'),
        ('Mana Elixir', 'Restores a surge of magic.'),
        ('Spectral Crystal', 'Hums softly in the dark.'),
    ],
    'epic': [
        ('Wraith Blade', 'Cuts through armor like mist.'),
        ('Dragonscale Plate', 'Forged from a Bone Drake hide.'),
    ],
    'legendary': [
        ('Ghostfire Staff', 'Burns with a cold, spectral flame.'),
        ('Crown of the Fallen King', 'Heavy is the head.'),
    ],
}


def _roll_rarity():
    r = random.random()
    if r < 0.02:
        return 'legendary'
    if r < 0.10:
        return 'epic'
    if r < 0.35:
        return 'rare'
    return 'common'


# (id, name, description, icon, rarity, category, stat, target, reward_text, reward_gold)
ACHIEVEMENTS = [
    ('first_blood',  'First Blood',        'Win your first hunt.',           'fa-khanda',        'common',    'Combat',      'battles_won',  1,    '25 Gold',   25),
    ('hunter_10',    'Seasoned Hunter',    'Win 10 hunts.',                  'fa-crosshairs',    'common',    'Combat',      'battles_won',  10,   '100 Gold',  100),
    ('hunter_50',    'Relentless',         'Win 50 hunts.',                  'fa-dragon',        'rare',      'Combat',      'battles_won',  50,   '500 Gold',  500),
    ('hunter_200',   'Legend of the Wild', 'Win 200 hunts.',                 'fa-crown',         'epic',      'Combat',      'battles_won',  200,  '2000 Gold', 2000),
    ('level_5',      'Rising Star',        'Reach level 5.',                 'fa-star',          'common',    'Progression', 'level',        5,    '50 Gold',   50),
    ('level_10',     'Veteran',            'Reach level 10.',                'fa-medal',         'rare',      'Progression', 'level',        10,   '250 Gold',  250),
    ('level_25',     'Champion',           'Reach level 25.',                'fa-trophy',        'epic',      'Progression', 'level',        25,   '1500 Gold', 1500),
    ('rich_1000',    'Coin Collector',     'Hold 1,000 gold at once.',       'fa-coins',         'common',    'Economy',     'gold',         1000, '100 Gold',  100),
    ('healer_5',     'Patched Up',         'Heal 5 times.',                  'fa-heart-pulse',   'common',    'Survival',    'heals',        5,    '50 Gold',   50),
    ('daily_7',      'Dedicated',          'Claim 7 daily rewards.',         'fa-gift',          'rare',      'Progression', 'dailies',      7,    '300 Gold',  300),
    ('collector_10', 'Hoarder',            'Own 10 different items.',        'fa-boxes-stacked', 'rare',      'Collection',  'unique_items', 10,   '200 Gold',  200),
    ('legend_find',  'Legendary Find',     'Find a legendary item.',         'fa-gem',           'legendary', 'Collection',  'legendaries',  1,    '1000 Gold', 1000),
    ('ghost_20',     'Ghost Whisperer',    'Catch 20 spirits in one round.', 'fa-ghost',         'epic',      'Minigames',   'best_spirit',  20,   '500 Gold',  500),
]

# (id, name, objective, counter, target, gold, xp)
DAILY_QUESTS = [
    ('d_hunt3', 'Monster Sweep',      'Win 3 hunts today.',        'wins',    3, 60,  40),
    ('d_hunt6', 'Bounty Hunter',      'Win 6 hunts today.',        'wins',    6, 120, 90),
    ('d_heal1', 'Tend Your Wounds',   'Heal once today.',          'heals',   1, 20,  20),
    ('d_daily', 'Claim the Blessing', 'Claim your daily reward.',  'dailies', 1, 30,  30),
]


# ───────────────────────── helpers
def _now():
    return datetime.now(timezone.utc)


def _iso(dt=None):
    return (dt or _now()).strftime('%Y-%m-%dT%H:%M:%SZ')


def _today():
    return _now().strftime('%Y-%m-%d')


def _parse(ts):
    try:
        return datetime.strptime(ts, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _lifetime_xp(st):
    """Total XP ever earned, derived from level + current xp (works for admin-edited accounts too)."""
    level = max(1, min(MAX_LEVEL, _int(st.get('level'), 1)))
    total, need = _int(st.get('xp')), 100
    for _ in range(level - 1):
        total += need
        need = int(need * 1.25)
    return total


def _default_state(name='Adventurer'):
    return {
        # shared with the dashboard's DEFAULT_STATS (same keys on purpose)
        'level': 1, 'gold': 100, 'gems': 10, 'special_coins': 0,
        'xp': 0, 'xp_needed': 100, 'char_class': 'Adventurer',
        'battles_won': 0, 'prestige_level': 0, 'guild': '—',
        'name': name,
        # game-engine additions
        'hp': 1000, 'max_hp': 1000, 'mp': 500, 'max_mp': 500,
        'battles_lost': 0, 'boss_kills': 0, 'pvp_wins': 0, 'pvp_losses': 0,
        'heals': 0, 'dailies': 0, 'best_spirit': 0, 'legendaries': 0,
        'daily_at': None, 'spirit_started': None,
        'inventory': [], 'pinned': [], 'unlocked': {},
        'quest_day': _today(), 'quest_claimed': [], 'quest_progress': {}, 'quest_history': [],
    }


# ───────────────────────── storage (player_stats / activity_log)
def _load(conn, ident):
    row = conn.execute("SELECT data FROM player_stats WHERE account_key=?", (ident['key'],)).fetchone()
    st = {}
    if row:
        try:
            st = json.loads(row[0]) or {}
        except Exception:
            st = {}
    level = max(1, min(MAX_LEVEL, _int(st.get('level'), 1)))
    if 'max_hp' not in st:                         # account created before the engine existed
        st['max_hp'] = 1000 + 50 * (level - 1)
        st['max_mp'] = 500 + 20 * (level - 1)
        st.setdefault('hp', st['max_hp'])
        st.setdefault('mp', st['max_mp'])
    for k, v in _default_state(ident['name']).items():
        st.setdefault(k, v)
    st['name'] = ident['name']
    # sanitize (admin edits or legacy data can leave odd values behind)
    st['level'] = level
    st['xp_needed'] = max(1, _int(st['xp_needed'], 100))
    st['xp'] = max(0, _int(st['xp']))
    st['gold'] = max(0, _int(st['gold']))
    st['gems'] = max(0, _int(st['gems']))
    st['max_hp'] = max(1, _int(st['max_hp'], 1000))
    st['max_mp'] = max(1, _int(st['max_mp'], 500))
    st['hp'] = max(0, min(st['max_hp'], _int(st['hp'], st['max_hp'])))
    st['mp'] = max(0, min(st['max_mp'], _int(st['mp'], st['max_mp'])))
    if not isinstance(st['inventory'], list):
        st['inventory'] = []
    if st.get('quest_day') != _today():
        st['quest_day'] = _today()
        st['quest_claimed'] = []
        st['quest_progress'] = {}
    return st


def _save(conn, ident, st):
    conn.execute(
        "INSERT INTO player_stats (account_key, data, last_updated) VALUES (?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(account_key) DO UPDATE SET data=excluded.data, last_updated=CURRENT_TIMESTAMP",
        (ident['key'], json.dumps(st)),
    )


def _log(conn, ident, action, detail):
    conn.execute("INSERT INTO activity_log (user_id, action, detail) VALUES (?,?,?)",
                 (ident['uid'], action, detail))


def _identity(request: Request):
    su = request.session.get('user') if hasattr(request, 'session') else None
    if not su or su.get('discord_id') is None:
        return None
    uid = str(su['discord_id'])
    auth_type = su.get('auth_type', 'email')
    name = su.get('nickname') or su.get('username') or su.get('name') or 'Adventurer'
    return {'uid': uid, 'key': f'{auth_type}:{uid}', 'name': str(name)[:60]}


# ───────────────────────── rules
def _bump(st, counter, n=1):
    st['quest_progress'][counter] = st['quest_progress'].get(counter, 0) + n


def _derived(st):
    lvl = st['level']
    attack, defense, magic = 10 + lvl * 4, 8 + lvl * 3, 6 + lvl * 3
    speed, luck = 5 + lvl * 2, 3 + lvl
    return {
        'attack': attack, 'defense': defense, 'magic': magic, 'speed': speed, 'luck': luck,
        'critical_chance': min(50, 5 + lvl // 2),
        'combat_power': attack * 3 + defense * 2 + magic * 2 + speed + luck + st['max_hp'] // 10,
    }


def _add_item(st, rarity):
    name, desc = random.choice(LOOT_TABLE[rarity])
    for it in st['inventory']:
        if it.get('name') == name:
            it['quantity'] = _int(it.get('quantity'), 1) + 1
            break
    else:
        st['inventory'].append({'name': name, 'rarity': rarity, 'quantity': 1,
                                'equipped': False, 'description': desc})
    if rarity == 'legendary':
        st['legendaries'] += 1
    return name


def _gain(st, events, xp=0, gold=0):
    st['gold'] += gold
    st['xp'] += xp
    while st['xp'] >= st['xp_needed'] and st['level'] < MAX_LEVEL:
        st['xp'] -= st['xp_needed']
        st['level'] += 1
        st['xp_needed'] = int(st['xp_needed'] * 1.25)
        st['max_hp'] += 50
        st['max_mp'] += 20
        st['hp'], st['mp'] = st['max_hp'], st['max_mp']
        events.append(('level_up', f"Level up! You reached level {st['level']}."))


def _check_progress(st, events):
    """Unlock achievements and complete daily quests; pay rewards (which may chain)."""
    changed = True
    while changed:
        changed = False
        st['unique_items'] = len(st['inventory'])
        for aid, name, _d, _i, _r, _c, stat, target, rtext, rgold in ACHIEVEMENTS:
            if aid in st['unlocked']:
                continue
            if _int(st.get(stat)) >= target:
                st['unlocked'][aid] = _iso()
                events.append(('achievement', f"Achievement unlocked: {name} (+{rtext})"))
                _gain(st, events, gold=rgold)
                changed = True
        for qid, qname, _o, counter, target, gold, xp in DAILY_QUESTS:
            if qid in st['quest_claimed']:
                continue
            if st['quest_progress'].get(counter, 0) >= target:
                st['quest_claimed'].append(qid)
                st['quest_history'].insert(0, {
                    'title': qname, 'completed_at': _iso(),
                    'description': f'Daily contract completed. +{gold} Gold, +{xp} XP.'})
                st['quest_history'] = st['quest_history'][:30]
                events.append(('quest', f"Quest complete: {qname} (+{gold} gold, +{xp} XP)"))
                _gain(st, events, xp=xp, gold=gold)
                changed = True
    st.pop('unique_items', None)


def _all_lifetime(conn):
    out = []
    for r in conn.execute("SELECT account_key, data FROM player_stats").fetchall():
        try:
            out.append((r[0], json.loads(r[1])))
        except Exception:
            continue
    return out


def _rank_of(conn, ident, st):
    mine = _lifetime_xp(st)
    higher = sum(1 for key, s in _all_lifetime(conn) if key != ident['key'] and _lifetime_xp(s) > mine)
    return higher + 1


def _public(conn, ident, st):
    """Flat view shaped like /api/me so pages can merge it directly."""
    out = {
        'username': st['name'], 'class': st['char_class'], 'char_class': st['char_class'],
        'level': st['level'], 'xp': st['xp'], 'xp_needed': st['xp_needed'],
        'xp_pct': round(st['xp'] / st['xp_needed'] * 100, 1),
        'total_xp': _lifetime_xp(st),
        'hp': st['hp'], 'max_hp': st['max_hp'], 'mp': st['mp'], 'max_mp': st['max_mp'],
        'gold': st['gold'], 'gems': st['gems'],
        'battles_won': st['battles_won'], 'battles_lost': st['battles_lost'],
        'world_boss_kills': st['boss_kills'], 'pvp_wins': st['pvp_wins'], 'pvp_losses': st['pvp_losses'],
        'rank': _rank_of(conn, ident, st),
        'daily_at': st['daily_at'],
        'items_owned': sum(_int(i.get('quantity'), 1) for i in st['inventory']),
    }
    out.update(_derived(st))
    return out


def _ach_list(st):
    st = dict(st)
    st['unique_items'] = len(st['inventory'])
    out = []
    for aid, name, desc, icon, rarity, cat, stat, target, rtext, _g in ACHIEVEMENTS:
        cur = min(target, _int(st.get(stat)))
        out.append({
            'id': aid, 'name': name, 'description': desc, 'icon': icon,
            'rarity': rarity, 'category': cat, 'reward': rtext,
            'unlocked': aid in st['unlocked'], 'unlocked_at': st['unlocked'].get(aid),
            'pinned': aid in st['pinned'],
            'progress_current': cur, 'progress_target': target,
            'progress_pct': round(cur / target * 100), 'xp': 0, 'hidden': False,
        })
    return out


def _err(msg, code=400):
    return JSONResponse({'ok': False, 'error': msg, 'message': msg}, status_code=code)


def _unauth():
    return JSONResponse({'logged_in': False, 'ok': False, 'error': 'unauthorized'}, status_code=401)


def profile_overlay(auth_type, uid):
    """For routes_profile.py: engine-derived fields the extended profile expects."""
    ident = {'uid': str(uid), 'key': f'{auth_type}:{uid}', 'name': 'Adventurer'}
    conn = _DB()
    try:
        st = _load(conn, ident)
        order = {'legendary': 0, 'epic': 1, 'rare': 2, 'common': 3}
        best = min(st['inventory'], key=lambda i: order.get(i.get('rarity'), 3), default=None)
        return {
            'xp': _lifetime_xp(st),
            'favorite_weapon': best['name'] if best else None,
            'achievements': [{'id': a['id'], 'name': a['name'], 'icon': a['icon'],
                              'rarity': a['rarity'], 'unlocked': a['unlocked']} for a in _ach_list(st)],
            '_global_rank': _rank_of(conn, ident, st),
        }
    finally:
        conn.close()


# ───────────────────────── routes
def register_game_routes(app, db_factory):
    global _DB
    _DB = db_factory

    def _run(request, fn, write=True):
        """Load -> mutate -> save -> respond, atomically. One code path for every action."""
        ident = _identity(request)
        if not ident:
            return _unauth()
        with _state_lock:
            conn = _DB()
            conn.isolation_level = None              # we manage the transaction ourselves
            try:
                if write:
                    conn.execute('BEGIN IMMEDIATE')  # one writer at a time, even across workers
                try:
                    st = _load(conn, ident)
                    result = fn(conn, ident, st)
                    if isinstance(result, JSONResponse):
                        if write:
                            conn.execute('ROLLBACK')
                        return result
                    if write:
                        _save(conn, ident, st)
                    result['state'] = _public(conn, ident, st)
                    result['ok'] = True
                    result['logged_in'] = True
                    if write:
                        conn.execute('COMMIT')
                    return result
                except Exception:
                    if conn.in_transaction:
                        conn.execute('ROLLBACK')
                    raise
            except Exception:
                log.exception('[game_engine] action failed')
                return _err('Server error. Please try again.', 500)
            finally:
                conn.close()

    def _finish(conn, ident, st, events, action, detail):
        _check_progress(st, events)
        _log(conn, ident, action, detail)
        for kind, text in events:
            _log(conn, ident, {'level_up': 'level_up', 'achievement': 'achievement_unlocked',
                               'quest': 'quest_reward_claim'}[kind], text)
        return [{'type': k, 'text': t} for k, t in events]

    @app.get('/api/game/state')
    async def game_state(request: Request):
        return _run(request, lambda c, i, s: {}, write=False)

    @app.post('/api/game/hunt')
    async def game_hunt(request: Request):
        def go(conn, ident, st):
            now = time.monotonic()
            if now - _last_hunt.get(ident['key'], 0) < HUNT_MIN_INTERVAL:
                return _err('Catch your breath before the next hunt.', 429)
            if st['hp'] < -(-st['max_hp'] // 10):
                return _err('Too wounded to hunt. Heal first!')
            _last_hunt[ident['key']] = now
            enemy = HUNT_ENEMIES[random.randint(0, min(len(HUNT_ENEMIES) - 1, 1 + st['level'] // 2))]
            events = []
            if random.random() < HUNT_WIN_CHANCE:
                xp = random.randint(15, 35) + st['level'] * 2
                gold = random.randint(10, 40)
                st['hp'] = max(1, st['hp'] - random.randint(5, 15))
                st['battles_won'] += 1
                _bump(st, 'wins')
                msg = f'Defeated a {enemy}! +{xp} XP, +{gold} gold.'
                loot = None
                if random.random() < LOOT_CHANCE:
                    loot = _add_item(st, _roll_rarity())
                    msg += f' Looted: {loot}.'
                _gain(st, events, xp=xp, gold=gold)
                evs = _finish(conn, ident, st, events, 'hunt_victory', msg)
                return {'result': 'win', 'enemy': enemy, 'message': msg, 'loot': loot, 'events': evs}
            dmg = random.randint(15, 30)
            st['hp'] = max(1, st['hp'] - dmg)
            st['battles_lost'] += 1
            msg = f'A {enemy} beat you. -{dmg} HP, +5 XP.'
            _gain(st, events, xp=5)
            evs = _finish(conn, ident, st, events, 'hunt_defeat', msg)
            return {'result': 'loss', 'enemy': enemy, 'message': msg, 'events': evs}
        return _run(request, go)

    @app.post('/api/game/heal')
    async def game_heal(request: Request):
        def go(conn, ident, st):
            if st['hp'] >= st['max_hp'] and st['mp'] >= st['max_mp']:
                return _err('You are already at full health.')
            if st['gold'] < HEAL_COST:
                return _err(f'Healing costs {HEAL_COST} gold.')
            st['gold'] -= HEAL_COST
            st['hp'], st['mp'] = st['max_hp'], st['max_mp']
            st['heals'] += 1
            _bump(st, 'heals')
            msg = f'Healed to full for {HEAL_COST} gold.'
            return {'message': msg, 'events': _finish(conn, ident, st, [], 'heal', msg)}
        return _run(request, go)

    @app.post('/api/game/daily')
    async def game_daily(request: Request):
        def go(conn, ident, st):
            last = _parse(st['daily_at']) if st['daily_at'] else None
            if last and _now() < last + DAILY_COOLDOWN:
                left = int((last + DAILY_COOLDOWN - _now()).total_seconds())
                mins = -(-left // 60)
                return _err(f'Daily already claimed. Come back in {mins // 60}h {mins % 60}m.')
            st['daily_at'] = _iso()
            st['dailies'] += 1
            _bump(st, 'dailies')
            events = []
            _gain(st, events, xp=100, gold=50)
            msg = 'Daily reward claimed: +100 XP, +50 Gold!'
            return {'message': msg, 'events': _finish(conn, ident, st, events, 'daily_claim', msg)}
        return _run(request, go)

    # Spirit Catcher: the server timestamps the round start, so a score can't be
    # claimed without actually spending the time, and it's capped by elapsed time.
    @app.post('/api/game/spirit/start')
    async def game_spirit_start(request: Request):
        def go(conn, ident, st):
            st['spirit_started'] = _iso()
            return {}
        return _run(request, go)

    @app.post('/api/game/spirit')
    async def game_spirit(request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}

        def go(conn, ident, st):
            started = _parse(st['spirit_started']) if st['spirit_started'] else None
            if not started:
                return _err('Start a round first.')
            elapsed = (_now() - started).total_seconds()
            if elapsed < SPIRIT_ROUND_MIN_SECONDS:
                return _err('That round ended too quickly. Try again.')
            st['spirit_started'] = None
            score = max(0, min(_int(body.get('score')), SPIRIT_MAX_SCORE, int(elapsed * 2)))
            if score == 0:
                return {'message': 'No ghosts caught.', 'events': []}
            st['best_spirit'] = max(st['best_spirit'], score)
            gold, xp = score * 3, score * 2
            events = []
            _gain(st, events, xp=xp, gold=gold)
            msg = f'Spirit Catcher: {score} caught. +{gold} gold, +{xp} XP.'
            return {'message': msg, 'events': _finish(conn, ident, st, events, 'minigame_reward', msg)}
        return _run(request, go)

    @app.get('/api/game/battle-stats')
    async def game_battle_stats(request: Request):
        return _run(request, lambda c, i, s: {
            'battles_won': s['battles_won'], 'battles_lost': s['battles_lost'],
            'world_boss_kills': s['boss_kills'],
            'pvp_wins': s['pvp_wins'], 'pvp_losses': s['pvp_losses'],
            'xp': _lifetime_xp(s),
        }, write=False)

    @app.get('/api/game/activity')
    async def game_activity(request: Request):
        ident = _identity(request)
        if not ident:
            return _unauth()
        conn = _DB()
        try:
            rows = conn.execute(
                "SELECT action, detail, ts FROM activity_log WHERE user_id=? ORDER BY id DESC LIMIT 100",
                (ident['uid'],)).fetchall()
            items = []
            for r in rows:
                ts = r[2] or ''
                if ts and 'T' not in ts:                 # SQLite default is UTC without a marker
                    ts = ts.replace(' ', 'T') + 'Z'
                items.append({'action': r[0], 'detail': r[1] or '', 'ts': ts})
            return {'items': items}
        finally:
            conn.close()

    @app.get('/api/game/inventory')
    async def game_inventory(request: Request):
        return _run(request, lambda c, i, s: {'items': s['inventory']}, write=False)

    @app.post('/api/game/inventory/sell')
    async def game_sell(request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        names = body.get('names') or []
        price = {'common': 8, 'rare': 30, 'epic': 120, 'legendary': 500}

        def go(conn, ident, st):
            if not isinstance(names, list) or not names:
                return _err('Nothing selected.')
            wanted = {str(n) for n in names}
            kept, total, sold = [], 0, 0
            for it in st['inventory']:
                if it.get('name') in wanted and not it.get('equipped'):
                    qty = _int(it.get('quantity'), 1)
                    total += price.get(it.get('rarity'), 8) * qty
                    sold += qty
                else:
                    kept.append(it)
            if not sold:
                return _err('Those items cannot be sold (equipped or missing).')
            st['inventory'] = kept
            st['gold'] += total
            msg = f'Sold {sold} item(s) for {total} gold.'
            _log(conn, ident, 'inventory_sell', msg)
            return {'message': msg, 'gold_gained': total}
        return _run(request, go)

    @app.get('/api/game/quests')
    async def game_quests(request: Request):
        def go(conn, ident, st):
            daily = [{
                'id': qid, 'name': name, 'objective': obj,
                'progress': min(target, st['quest_progress'].get(counter, 0)), 'target': target,
                'reward': {'gold': gold, 'xp': xp},
            } for qid, name, obj, counter, target, gold, xp in DAILY_QUESTS if qid not in st['quest_claimed']]
            return {'daily_quests': daily, 'active_quests': [],
                    'completed_quests': st['quest_history'][:6]}
        return _run(request, go, write=False)

    @app.get('/api/game/achievements')
    async def game_achievements(request: Request):
        return _run(request, lambda c, i, s: {'achievements': _ach_list(s)}, write=False)

    @app.post('/api/game/achievements/pin')
    async def game_pin(request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}

        def go(conn, ident, st):
            aid = str(body.get('achievement_id', ''))
            if aid not in st['unlocked']:
                return _err('Achievement is not unlocked.')
            if body.get('pinned'):
                if aid not in st['pinned']:
                    if len(st['pinned']) >= 3:
                        return _err('You can pin up to 3 achievements.')
                    st['pinned'].append(aid)
            elif aid in st['pinned']:
                st['pinned'].remove(aid)
            return {'pinned': st['pinned']}
        return _run(request, go)

    @app.get('/api/game/leaderboard')
    async def game_leaderboard(request: Request):
        conn = _DB()
        try:
            players = []
            for key, s in _all_lifetime(conn):
                xp = _lifetime_xp(s)
                if xp <= 0:
                    continue
                players.append({'id': key.split(':', 1)[-1], 'name': s.get('name') or key,
                                'class': s.get('char_class', 'Adventurer'), 'xp': xp,
                                'level': _int(s.get('level'), 1)})
            players.sort(key=lambda p: (-p['xp'], p['name']))
            return {'leaderboard': players[:50], 'total': len(players)}
        finally:
            conn.close()
