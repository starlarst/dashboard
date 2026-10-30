import json
import logging
import random
import threading
import time
from datetime import datetime, timezone, timedelta

from fastapi import Request
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)

_DB = None
_state_lock = threading.Lock()
_last_call = {}                 

MAX_LEVEL = 1000
DAILY_COOLDOWN = timedelta(hours=24)
STREAK_WINDOW = timedelta(hours=48)
SPIRIT_ROUND_MIN_SECONDS = 18
SPIRIT_MAX_SCORE = 40
MAX_ACTIVE_QUESTS = 3
TOWER_FLOORS = 100

ITEMS = {
    'Rusty Sword':        dict(slot='weapon', rarity='common',    atk=4,  price=40,    desc='A chipped blade. Still sharp enough.'),
    'Wooden Club':        dict(slot='weapon', rarity='common',    atk=5,  price=45,    desc='Crude, heavy, and surprisingly effective.'),
    'Iron Sword':         dict(slot='weapon', rarity='rare',      atk=14, price=350,   desc='A reliable soldier\'s blade.'),
    'Steel Dagger':       dict(slot='weapon', rarity='rare',      atk=12, price=300,   desc='Balanced and quick.'),
    "Hunter's Bow":       dict(slot='weapon', rarity='rare',      atk=15, price=380,   desc='Strung with sinew from the old forest.'),
    "Knight's Longsword": dict(slot='weapon', rarity='rare',      atk=24, price=900,   desc='Forged for those who hold the line.'),
    'Wraith Blade':       dict(slot='weapon', rarity='epic',      atk=30, price=1800,  desc='Cuts through armor like mist.'),
    'Bone Reaver':        dict(slot='weapon', rarity='epic',      atk=36, price=2200,  desc='Carved from a Bone Knight\'s spine.'),
    'Berserker Axe':      dict(slot='weapon', rarity='epic',      atk=40, price=2500,  desc='It wants to be swung.'),
    'Ghostfire Staff':    dict(slot='weapon', rarity='legendary', atk=60, price=6000,  desc='Burns with a cold, spectral flame.'),
    'Magma Greatsword':   dict(slot='weapon', rarity='legendary', atk=75, price=9000,  desc='Still glowing from the volcano\'s heart.'),
    # armor
    'Wooden Shield':      dict(slot='armor',  rarity='common',    df=4,   price=40,    desc='Splinters, but it blocks.'),
    'Leather Vest':       dict(slot='armor',  rarity='common',    df=5,   price=50,    desc='Light, quiet, better than nothing.'),
    'Chainmail':          dict(slot='armor',  rarity='rare',      df=12,  price=420,   desc='Rings of iron, worn smooth.'),
    'Knight Armor':       dict(slot='armor',  rarity='rare',      df=14,  price=480,   desc='Dented in all the right places.'),
    'Dragonscale Plate':  dict(slot='armor',  rarity='epic',      df=30,  price=1900,  desc='Forged from a Bone Drake hide.'),
    'Paladin Plate':      dict(slot='armor',  rarity='epic',      df=34,  price=2200,  desc='Blessed steel that hums when you fight.'),
    'Obsidian Armor':     dict(slot='armor',  rarity='epic',      df=38,  price=2600,  desc='Glass-black and impossibly light.'),
    'Crown of the Fallen King': dict(slot='armor', rarity='legendary', df=55, price=6000, desc='Heavy is the head.'),
    'Phoenix Mantle':     dict(slot='armor',  rarity='legendary', df=70,  price=9000,  desc='Warm to the touch. Always.'),
    # consumables
    'Healing Potion':         dict(slot='consumable', rarity='common', hp=300, price=25,  desc='Smells awful. Restores 300 HP.'),
    'Greater Healing Potion': dict(slot='consumable', rarity='rare',   hp=800, price=80,  desc='Restores 800 HP.'),
    'Mana Elixir':            dict(slot='consumable', rarity='rare',   mp=250, price=60,  desc='Restores 250 MP.'),
    'Scroll of Insight':      dict(slot='consumable', rarity='rare',   xp=300, price=0,   desc='Read it and learn: +300 XP.'),
    # misc
    'Old Scroll':             dict(slot='misc', rarity='common', price=10,  desc='Faded ink and forgotten spells.'),
    'Spectral Crystal':       dict(slot='misc', rarity='rare',   price=80,  desc='Hums softly in the dark.'),
    'Adventurer Guild Card':  dict(slot='misc', rarity='common', price=0,   desc='Proof that you belong. Not for sale.'),
}
NO_SELL = {'Adventurer Guild Card'}
RARITY_SELL = {'common': 8, 'rare': 30, 'epic': 120, 'legendary': 500}

ZONES = {
    'forest': dict(
        name='Whispering Forest', req=1, power=(20, 60), xp=1.0, gold=1.0, loot=0.20, odds=(0.0, 0.03, 0.25),
        enemies=['Slime', 'Goblin', 'Dire Wolf'],
        blurb='Gentle on the way in, but the wolves are learning.',
        table={'common': ['Rusty Sword', 'Wooden Club', 'Leather Vest', 'Healing Potion', 'Old Scroll'],
               'rare': ['Steel Dagger', "Hunter's Bow", 'Mana Elixir'], 'epic': ['Wraith Blade'], 'legendary': []}),
    'crypt': dict(
        name='Sunken Crypt', req=8, power=(90, 170), xp=2.2, gold=2.0, loot=0.28, odds=(0.02, 0.10, 0.35),
        enemies=['Skeleton', 'Wraith', 'Bone Knight'],
        blurb='Water drips on old bones. Something answers.',
        table={'common': ['Healing Potion', 'Wooden Shield', 'Old Scroll'],
               'rare': ['Chainmail', 'Greater Healing Potion', 'Spectral Crystal'],
               'epic': ['Wraith Blade', 'Bone Reaver', 'Dragonscale Plate'],
               'legendary': ['Ghostfire Staff', 'Crown of the Fallen King']}),
    'volcano': dict(
        name='Ashen Volcano', req=20, power=(210, 380), xp=4.5, gold=4.0, loot=0.35, odds=(0.05, 0.18, 0.40),
        enemies=['Fire Imp', 'Magma Golem', 'Bone Drake'],
        blurb='The ground is warm. Do not stand still.',
        table={'common': ['Healing Potion'],
               'rare': ['Greater Healing Potion', 'Chainmail', 'Mana Elixir'],
               'epic': ['Obsidian Armor', 'Dragonscale Plate', 'Bone Reaver'],
               'legendary': ['Magma Greatsword', 'Phoenix Mantle', 'Ghostfire Staff']}),
}

RANKS = [('F', 0), ('E', 100), ('D', 300), ('C', 700), ('B', 1500), ('A', 3000), ('S', 6000)]
RANK_DISCOUNT = [0, 3, 5, 8, 10, 12, 15]       # shop discount % by rank index

GUILD_QUESTS = [
    ('m_first_steps', 'First Steps',     'Win 5 hunts.',                         0, 'wins',          5,   80,   60,  0,  20),
    ('m_potion_run',  'Potion Run',      'Heal yourself 3 times.',               0, 'heals',         3,   60,   40,  0,  15),
    ('m_forest',      'Forest Patrol',   'Win 15 hunts in the Whispering Forest.', 0, 'zone_forest', 15,  120,  90,  0,  30),
    ('m_tower_1',     'Into the Tower',  'Clear 3 Tower floors.',                0, 'tower',         3,   120,  100, 0,  30),
    ('m_gear_up',     'Gear Up',         'Find 5 pieces of loot.',               1, 'loot',          5,   200,  150, 0,  40),
    ('m_crypt',       'Crypt Raid',      'Win 15 hunts in the Sunken Crypt.',    1, 'zone_crypt',    15,  350,  250, 0,  60),
    ('m_boss_hits',   'Boss Hunter',     'Strike the world boss 3 times.',       1, 'boss_hits',     3,   300,  200, 2,  50),
    ('m_tower_10',    'Tower Climber',   'Clear 10 Tower floors.',               2, 'tower',         10,  600,  400, 5,  80),
    ('m_veteran',     'Veteran Hunter',  'Win 100 hunts.',                       2, 'wins',          100, 700,  500, 0,  100),
    ('m_volcano',     'Ashen Trial',     'Win 20 hunts in the Ashen Volcano.',   3, 'zone_volcano',  20,  1500, 900, 5,  150),
    ('m_tower_25',    'Warden\'s Road',  'Clear 25 Tower floors.',               3, 'tower',         25,  2000, 1200, 10, 200),
    ('m_slayer',      'Giant Slayer',    'Help defeat 3 world bosses.',          4, 'boss',          3,   3000, 2000, 15, 300),
    ('m_hunter_500',  'Relentless',      'Win 500 hunts.',                       4, 'wins',          500, 3000, 2000, 10, 300),
    ('m_tower_50',    'Tower Conqueror', 'Clear 50 Tower floors.',               5, 'tower',         50,  6000, 4000, 30, 600),
    ('m_tower_100',   'Sovereign',       'Clear all 100 Tower floors.',          6, 'tower',         100, 20000, 10000, 100, 1000),
]

DAILY_QUESTS = [
    ('d_hunt3', 'Monster Sweep',      'Win 3 hunts today.',       'wins',    3, 60,  40, 5),
    ('d_hunt6', 'Bounty Hunter',      'Win 6 hunts today.',       'wins',    6, 120, 90, 8),
    ('d_heal1', 'Tend Your Wounds',   'Heal once today.',         'heals',   1, 20,  20, 3),
    ('d_daily', 'Claim the Blessing', 'Claim your daily reward.', 'dailies', 1, 30,  30, 3),
]

TOWER_TITLES = {10: 'Tower Challenger', 25: 'Tower Warden', 50: 'Tower Conqueror', 100: 'Tower Sovereign'}

SHOP = [
    ('s_heal',   'item',  'Healing Potion',         'gold', 25,   1,  'Restores 300 HP.'),
    ('s_heal2',  'item',  'Greater Healing Potion', 'gold', 80,   10, 'Restores 800 HP.'),
    ('s_mana',   'item',  'Mana Elixir',            'gold', 60,   1,  'Restores 250 MP.'),
    ('s_iron',   'item',  'Iron Sword',             'gold', 350,  1,  '+14 attack.'),
    ('s_knight', 'item',  "Knight's Longsword",     'gold', 900,  10, '+24 attack.'),
    ('s_axe',    'item',  'Berserker Axe',          'gold', 2500, 18, '+40 attack.'),
    ('s_armor',  'item',  'Knight Armor',           'gold', 480,  1,  '+14 defense.'),
    ('s_pal',    'item',  'Paladin Plate',          'gold', 2200, 18, '+34 defense.'),
    ('s_insight','item',  'Scroll of Insight',      'gems', 8,    1,  '+300 XP when read.'),
    ('t_slime',  'title', 'Slime Slayer',           'gold', 500,  1,  'A title for the first of many.'),
    ('t_hoard',  'title', 'Gold Hoarder',           'gold', 5000, 5,  'Shows everyone you can afford it.'),
    ('t_night',  'title', 'Night Owl',              'gems', 10,   1,  'For those who hunt after dark.'),
    ('t_baron',  'title', 'Gem Baron',              'gems', 25,   1,  'Rare, shiny, and a little smug.'),
]

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
    ('guild_member', 'Sworn Adventurer',   'Join the Adventurer Guild.',     'fa-id-badge',      'common',    'Guild',       'adv_joined',   1,    '50 Gold',   50),
    ('boss_1',       'Giant Slayer',       'Help defeat a world boss.',      'fa-skull',         'epic',      'Combat',      'boss_kills',   1,    '500 Gold',  500),
    ('tower_10',     'Tower Climber',      'Clear floor 10 of the Tower.',   'fa-chess-rook',    'rare',      'Tower',       'tower_floor',  10,   '300 Gold',  300),
]

BOSS_NAMES = ['Bone Colossus', 'Crimson Wyrm', 'Morvath the Lich King', 'The Hollow Titan',
              'Zephra the Stormcaller', 'Abyssal Leviathan', 'Ashen Behemoth']


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


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _throttle(action, key, seconds):
    """True if the caller must wait. Records the call when allowed."""
    now = time.monotonic()
    k = (action, key)
    last = _last_call.get(k)
    if last is not None and now - last < seconds:    
        return True
    _last_call[k] = now
    return False


def _lifetime_xp(st):
    level = max(1, min(MAX_LEVEL, _int(st.get('level'), 1)))
    total, need = _int(st.get('xp')), 100
    for _ in range(level - 1):
        total += need
        need = int(need * 1.25)
    return total


def _default_state(name='Adventurer'):
    return {
        'level': 1, 'gold': 100, 'gems': 10, 'special_coins': 0,
        'xp': 0, 'xp_needed': 100, 'char_class': 'Adventurer',
        'battles_won': 0, 'prestige_level': 0, 'guild': '—',
        'name': name,
        'hp': 1000, 'max_hp': 1000, 'mp': 500, 'max_mp': 500,
        'battles_lost': 0, 'boss_kills': 0, 'pvp_wins': 0, 'pvp_losses': 0,
        'heals': 0, 'dailies': 0, 'best_spirit': 0, 'legendaries': 0,
        'daily_at': None, 'streak': 0, 'spirit_started': None,
        'zone': 'forest', 'zone_wins': {}, 'loot_found': 0, 'boss_attacks': 0,
        'titles': [], 'equipped_title': None,
        'adv_joined': False, 'adv_joined_at': None, 'guild_points': 0,
        'quests_active': {}, 'quests_done': [],
        'tower_floor': 0,
        'inventory': [], 'pinned': [], 'unlocked': {},
        'quest_day': _today(), 'quest_claimed': [], 'quest_progress': {}, 'quest_history': [],
    }

def _load(conn, ident):
    row = conn.execute("SELECT data FROM player_stats WHERE account_key=?", (ident['key'],)).fetchone()
    st = {}
    if row:
        try:
            st = json.loads(row[0]) or {}
        except Exception:
            st = {}
    level = max(1, min(MAX_LEVEL, _int(st.get('level'), 1)))
    if 'max_hp' not in st:
        st['max_hp'] = 1000 + 50 * (level - 1)
        st['max_mp'] = 500 + 20 * (level - 1)
        st.setdefault('hp', st['max_hp'])
        st.setdefault('mp', st['max_mp'])
    for k, v in _default_state(ident['name']).items():
        st.setdefault(k, v)
    st['name'] = ident['name']
    st['level'] = level
    st['xp_needed'] = max(1, _int(st['xp_needed'], 100))
    for k in ('xp', 'gold', 'gems'):
        st[k] = max(0, _int(st[k]))
    st['max_hp'] = max(1, _int(st['max_hp'], 1000))
    st['max_mp'] = max(1, _int(st['max_mp'], 500))
    st['hp'] = _clamp(_int(st['hp'], st['max_hp']), 0, st['max_hp'])
    st['mp'] = _clamp(_int(st['mp'], st['max_mp']), 0, st['max_mp'])
    inv = st['inventory'] if isinstance(st['inventory'], list) else []
    st['inventory'] = [i for i in inv if isinstance(i, dict) and i.get('name')]
    if st['zone'] not in ZONES:
        st['zone'] = 'forest'
    if not isinstance(st['zone_wins'], dict):
        st['zone_wins'] = {}
    if not isinstance(st['quests_active'], dict):
        st['quests_active'] = {}
    if st.get('quest_day') != _today():
        st['quest_day'] = _today()
        st['quest_claimed'] = []
        st['quest_progress'] = {}
    return st


def _save(conn, ident, st):
    conn.execute(
        "INSERT INTO player_stats (account_key, data, last_updated) VALUES (?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(account_key) DO UPDATE SET data=excluded.data, last_updated=CURRENT_TIMESTAMP",
        (ident['key'], json.dumps(st)))


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


def _ensure_world_tables():
    conn = _DB()
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS world_boss (
            id INTEGER PRIMARY KEY AUTOINCREMENT, period TEXT UNIQUE, name TEXT,
            max_hp INTEGER, hp INTEGER, power INTEGER, killed_by TEXT, killed_at TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS world_boss_damage (
            boss_id INTEGER, account_key TEXT, name TEXT,
            damage INTEGER DEFAULT 0, hits INTEGER DEFAULT 0, claimed INTEGER DEFAULT 0,
            PRIMARY KEY (boss_id, account_key))""")
        conn.commit()
    finally:
        conn.close()

def _meta(name):
    return ITEMS.get(name, dict(slot='misc', rarity='common', price=0, desc=''))


def _bump(st, counter, n=1):
    st['quest_progress'][counter] = st['quest_progress'].get(counter, 0) + n


def _rank_idx(points):
    idx = 0
    for i, (_l, need) in enumerate(RANKS):
        if points >= need:
            idx = i
    return idx


def _counter(st, key):
    if key == 'wins':
        return _int(st['battles_won'])
    if key == 'heals':
        return _int(st['heals'])
    if key == 'boss':
        return _int(st['boss_kills'])
    if key == 'boss_hits':
        return _int(st['boss_attacks'])
    if key == 'tower':
        return _int(st['tower_floor'])
    if key == 'loot':
        return _int(st['loot_found'])
    if key.startswith('zone_'):
        return _int(st['zone_wins'].get(key[5:], 0))
    return 0


def _gear(st):
    atk = df = 0
    seen = set()
    for it in st['inventory']:
        m = _meta(it['name'])
        if it.get('equipped') and m['slot'] in ('weapon', 'armor') and m['slot'] not in seen:
            seen.add(m['slot'])
            atk += m.get('atk', 0)
            df += m.get('df', 0)
    return atk, df


def _derived(st):
    lvl = st['level']
    gatk, gdef = _gear(st)
    attack, defense = 10 + lvl * 4 + gatk, 8 + lvl * 3 + gdef
    magic, speed, luck = 6 + lvl * 3, 5 + lvl * 2, 3 + lvl
    return {
        'attack': attack, 'defense': defense, 'magic': magic, 'speed': speed, 'luck': luck,
        'gear_attack': gatk, 'gear_defense': gdef,
        'critical_chance': min(50, 5 + lvl // 2),
        'combat_power': attack * 3 + defense * 2 + magic * 2 + speed + luck + st['max_hp'] // 10,
    }


def _heal_cost(st):
    return 30 + 3 * st['level']


def _discount(st):
    return RANK_DISCOUNT[_rank_idx(st['guild_points'])] if st['adv_joined'] else 0


def _add_item(st, name, qty=1):
    m = _meta(name)
    for it in st['inventory']:
        if it.get('name') == name:
            it['quantity'] = _int(it.get('quantity'), 1) + qty
            break
    else:
        st['inventory'].append({'name': name, 'rarity': m['rarity'], 'quantity': qty,
                                'equipped': False, 'description': m.get('desc', '')})
    if m['rarity'] == 'legendary':
        st['legendaries'] += qty


def _roll_loot(zone):
    leg, epic, rare = zone['odds']
    r = random.random()
    rarity = 'legendary' if r < leg else 'epic' if r < leg + epic else 'rare' if r < leg + epic + rare else 'common'
    pool = zone['table'].get(rarity) or zone['table']['common']
    return random.choice(pool)


def _gain(st, events, xp=0, gold=0, gems=0):
    st['gold'] += gold
    st['gems'] += gems
    st['xp'] += xp
    while st['xp'] >= st['xp_needed'] and st['level'] < MAX_LEVEL:
        st['xp'] -= st['xp_needed']
        st['level'] += 1
        st['xp_needed'] = int(st['xp_needed'] * 1.25)
        st['max_hp'] += 50
        st['max_mp'] += 20
        st['hp'], st['mp'] = st['max_hp'], st['max_mp']
        events.append(('level_up', f"Level up! You reached level {st['level']}."))


def _gp(st, events, n):
    """Guild points; announces rank-ups."""
    if n <= 0 or not st['adv_joined']:
        return
    before = _rank_idx(st['guild_points'])
    st['guild_points'] += n
    after = _rank_idx(st['guild_points'])
    if after > before:
        events.append(('rank', f"Adventurer Guild rank up! You are now Rank {RANKS[after][0]}."))


def _grant_title(st, events, title):
    if title not in st['titles']:
        st['titles'].append(title)
        events.append(('title', f'Title unlocked: {title}'))


def _check_progress(st, events):
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
        if st['adv_joined']:
            for qid, qname, _o, counter, target, gold, xp, gp in DAILY_QUESTS:
                if qid in st['quest_claimed']:
                    continue
                if st['quest_progress'].get(counter, 0) >= target:
                    st['quest_claimed'].append(qid)
                    st['quest_history'].insert(0, {
                        'title': qname, 'completed_at': _iso(),
                        'description': f'Daily contract completed. +{gold} Gold, +{xp} XP.'})
                    st['quest_history'] = st['quest_history'][:30]
                    events.append(('quest', f"Contract complete: {qname} (+{gold} gold, +{xp} XP, +{gp} guild pts)"))
                    _gain(st, events, xp=xp, gold=gold)
                    _gp(st, events, gp)
                    changed = True
    st.pop('unique_items', None)


def _all_rows(conn):
    out = []
    for r in conn.execute("SELECT account_key, data FROM player_stats").fetchall():
        try:
            out.append((r[0], json.loads(r[1])))
        except Exception:
            continue
    return out


def _rank_of(conn, ident, st):
    mine = _lifetime_xp(st)
    return 1 + sum(1 for k, s in _all_rows(conn) if k != ident['key'] and _lifetime_xp(s) > mine)


def _inv_view(st):
    out = []
    for it in st['inventory']:
        m = _meta(it['name'])
        v = dict(it)
        v.update({'slot': m['slot'], 'atk': m.get('atk', 0), 'def': m.get('df', 0),
                  'hp': m.get('hp', 0), 'mp': m.get('mp', 0), 'xp': m.get('xp', 0),
                  'rarity': it.get('rarity') or m['rarity'],
                  'description': it.get('description') or m.get('desc', '')})
        out.append(v)
    order = {'legendary': 0, 'epic': 1, 'rare': 2, 'common': 3}
    out.sort(key=lambda i: (order.get(i['rarity'], 4), i['name']))
    return out


def _public(conn, ident, st):
    ri = _rank_idx(st['guild_points'])
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
        'daily_at': st['daily_at'], 'streak': st['streak'], 'heal_cost': _heal_cost(st),
        'zone': st['zone'], 'tower_floor': st['tower_floor'],
        'equipped_title': st['equipped_title'],
        'adv_joined': st['adv_joined'], 'adv_rank': RANKS[ri][0] if st['adv_joined'] else None,
        'adv_points': st['guild_points'],
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
            'progress_pct': round(cur / target * 100), 'xp': 0, 'hidden': False})
    return out


def _err(msg, code=400):
    return JSONResponse({'ok': False, 'error': msg, 'message': msg}, status_code=code)


def _unauth():
    return JSONResponse({'logged_in': False, 'ok': False, 'error': 'unauthorized'}, status_code=401)


def _need_guild(st):
    if not st['adv_joined']:
        return _err('Join the Adventurer Guild first.', 403)
    return None


# ─────────────────────────── tower
def _tower_power(floor):
    return int(20 + 9 * floor + 0.2 * floor * floor)


def _tower_rank_req(floor):
    return min(len(RANKS) - 1, (floor - 1) // 10)


def _tower_preview(st):
    floor = st['tower_floor'] + 1
    if floor > TOWER_FLOORS:
        return None
    boss = floor % 5 == 0
    power = int(_tower_power(floor) * (1.4 if boss else 1))
    d = _derived(st)
    ratio = (d['attack'] + d['defense']) / power
    mult = 3 if boss else 1
    return {
        'floor': floor, 'boss': boss, 'power': power,
        'name': f'Guardian of Floor {floor}' if boss else ['Tower Imp', 'Stone Sentinel', 'Shadow Knight', 'Void Hound', 'Arcane Golem'][floor % 5],
        'win_chance': round(_clamp(0.30 + 0.45 * ratio, 0.10, 0.92) * 100),
        'rank_req': RANKS[_tower_rank_req(floor)][0],
        'reward': {'xp': (30 + 8 * floor) * mult, 'gold': (25 + 6 * floor) * mult,
                   'gems': (2 + floor // 5) if boss else 0, 'gp': 1 + floor // 10 + (5 if boss else 0)},
    }


# ─────────────────────────── world boss
def _ensure_boss(conn):
    period = _today()
    row = conn.execute("SELECT * FROM world_boss WHERE period=?", (period,)).fetchone()
    if row:
        return row
    players = conn.execute("SELECT COUNT(*) FROM player_stats").fetchone()[0]
    ordinal = _now().toordinal()
    max_hp = 8000 + 2500 * min(players, 40)
    conn.execute("INSERT INTO world_boss (period, name, max_hp, hp, power) VALUES (?,?,?,?,?)",
                 (period, BOSS_NAMES[ordinal % len(BOSS_NAMES)], max_hp, max_hp, 300 + (ordinal % 7) * 60))
    return conn.execute("SELECT * FROM world_boss WHERE period=?", (period,)).fetchone()


def _boss_view(conn, ident, boss):
    mine = conn.execute("SELECT damage, hits FROM world_boss_damage WHERE boss_id=? AND account_key=?",
                        (boss['id'], ident['key'])).fetchone()
    top = conn.execute("SELECT name, damage FROM world_boss_damage WHERE boss_id=? "
                       "ORDER BY damage DESC LIMIT 10", (boss['id'],)).fetchall()
    tomorrow = (_now() + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    claimable = conn.execute(
        "SELECT COUNT(*) FROM world_boss_damage d JOIN world_boss b ON b.id=d.boss_id "
        "WHERE d.account_key=? AND d.claimed=0 AND d.damage>0 AND (b.hp<=0 OR b.period<?)",
        (ident['key'], _today())).fetchone()[0]
    cd = max(0, 20 - (time.monotonic() - _last_call.get(('boss', ident['key']), -999)))
    return {
        'name': boss['name'], 'hp': boss['hp'], 'max_hp': boss['max_hp'], 'power': boss['power'],
        'defeated': boss['hp'] <= 0, 'killed_by': boss['killed_by'],
        'ends_in': int((tomorrow - _now()).total_seconds()),
        'your_damage': mine['damage'] if mine else 0, 'your_hits': mine['hits'] if mine else 0,
        'top': [{'name': t['name'], 'damage': t['damage']} for t in top],
        'claimable': claimable, 'cooldown': int(cd + 0.999) if cd > 0 else 0,
    }


def profile_overlay(auth_type, uid):
    """For routes_profile.py: engine-derived fields the extended profile expects."""
    ident = {'uid': str(uid), 'key': f'{auth_type}:{uid}', 'name': 'Adventurer'}
    conn = _DB()
    try:
        st = _load(conn, ident)
        weapon = next((i['name'] for i in st['inventory']
                       if i.get('equipped') and _meta(i['name'])['slot'] == 'weapon'), None)
        return {
            'xp': _lifetime_xp(st), 'favorite_weapon': weapon,
            'boss_kills': st['boss_kills'], 'prestige': st['prestige_level'],
            'equipped_title': st['equipped_title'],
            'achievements': [{'id': a['id'], 'name': a['name'], 'icon': a['icon'],
                              'rarity': a['rarity'], 'unlocked': a['unlocked']} for a in _ach_list(st)],
            '_global_rank': _rank_of(conn, ident, st),
        }
    finally:
        conn.close()


# ═════════════════════════════════════════════════════════════ routes
def register_game_routes(app, db_factory):
    global _DB
    _DB = db_factory
    try:
        _ensure_world_tables()
    except Exception:
        log.exception('[game_engine] could not create world boss tables')

    def _run(request, fn, write=True, save=True):
        ident = _identity(request)
        if not ident:
            return _unauth()
        with _state_lock:
            conn = _DB()
            conn.isolation_level = None
            try:
                if write:
                    conn.execute('BEGIN IMMEDIATE')
                try:
                    st = _load(conn, ident)
                    result = fn(conn, ident, st)
                    if isinstance(result, JSONResponse):
                        if write:
                            conn.execute('ROLLBACK')
                        return result
                    if write and save:
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

    _EVENT_ACTION = {'level_up': 'level_up', 'achievement': 'achievement_unlocked', 'quest': 'quest_reward_claim',
                     'title': 'title_unlocked', 'rank': 'guild_rank_up', 'boss': 'boss_defeated'}

    def _finish(conn, ident, st, events, action, detail):
        _check_progress(st, events)
        if action:
            _log(conn, ident, action, detail)
        for kind, text in events:
            _log(conn, ident, _EVENT_ACTION.get(kind, kind), text)
        return [{'type': k, 'text': t} for k, t in events]

    async def _body(request):
        try:
            b = await request.json()
            return b if isinstance(b, dict) else {}
        except Exception:
            return {}

    # ───────────────────────── state / zones
    @app.get('/api/game/state')
    async def game_state(request: Request):
        return _run(request, lambda c, i, s: {}, write=False)

    @app.get('/api/game/zones')
    async def game_zones(request: Request):
        def go(conn, ident, st):
            d = _derived(st)
            zones = []
            for zid, z in ZONES.items():
                mid = (z['power'][0] + z['power'][1]) / 2
                zones.append({
                    'id': zid, 'name': z['name'], 'req_level': z['req'], 'blurb': z['blurb'],
                    'locked': st['level'] < z['req'], 'current': st['zone'] == zid,
                    'enemies': z['enemies'], 'power_range': list(z['power']),
                    'win_chance': round(_clamp(0.45 + 0.30 * ((d['attack'] + d['defense']) / mid), 0.2, 0.95) * 100),
                    'xp_mult': z['xp'], 'gold_mult': z['gold'],
                    'loot_chance': round(z['loot'] * 100), 'wins': _int(st['zone_wins'].get(zid, 0)),
                })
            return {'zones': zones}
        return _run(request, go, write=False)

    # ───────────────────────── hunt / heal / daily / spirit
    @app.post('/api/game/hunt')
    async def game_hunt(request: Request):
        body = await _body(request)

        def go(conn, ident, st):
            if _throttle('hunt', ident['key'], 1.0):
                return _err('Catch your breath before the next hunt.', 429)
            zid = body.get('zone') or st['zone']
            zone = ZONES.get(zid)
            if not zone:
                return _err('Unknown hunting ground.')
            if st['level'] < zone['req']:
                return _err(f"{zone['name']} requires level {zone['req']}.")
            st['zone'] = zid
            if st['hp'] < -(-st['max_hp'] // 10):
                return _err('Too wounded to hunt. Heal first!')
            d = _derived(st)
            power = d['attack'] + d['defense']
            idx = random.randrange(len(zone['enemies']))
            lo, hi = zone['power']
            enemy_power = int(lo + (hi - lo) * (idx + random.random()) / len(zone['enemies']))
            enemy = zone['enemies'][idx]
            chance = _clamp(0.45 + 0.30 * (power / enemy_power), 0.2, 0.95)
            dmg_scale = (1 + enemy_power / 40) * _clamp(enemy_power / power, 0.5, 2.0)
            penalty = max(0.25, 1 - 0.04 * max(0, st['level'] - zone['req'] - 10))
            events = []
            if random.random() < chance:
                xp = int((random.randint(15, 35) + st['level'] * 2) * zone['xp'] * penalty)
                gold = int(random.randint(10, 40) * zone['gold'])
                st['hp'] = max(1, st['hp'] - int(random.randint(5, 15) * dmg_scale))
                st['battles_won'] += 1
                st['zone_wins'][zid] = _int(st['zone_wins'].get(zid, 0)) + 1
                _bump(st, 'wins')
                msg = f'Defeated a {enemy} in the {zone["name"]}! +{xp} XP, +{gold} gold.'
                loot = None
                if random.random() < zone['loot']:
                    loot = _roll_loot(zone)
                    _add_item(st, loot)
                    st['loot_found'] += 1
                    msg += f' Looted: {loot}.'
                _gain(st, events, xp=xp, gold=gold)
                evs = _finish(conn, ident, st, events, 'hunt_victory', msg)
                return {'result': 'win', 'enemy': enemy, 'zone': zid, 'message': msg, 'loot': loot,
                        'win_chance': round(chance * 100), 'events': evs}
            dmg = int(random.randint(15, 30) * dmg_scale)
            st['hp'] = max(1, st['hp'] - dmg)
            st['battles_lost'] += 1
            msg = f'A {enemy} beat you. -{dmg} HP, +5 XP.'
            _gain(st, events, xp=5)
            evs = _finish(conn, ident, st, events, 'hunt_defeat', msg)
            return {'result': 'loss', 'enemy': enemy, 'zone': zid, 'message': msg,
                    'win_chance': round(chance * 100), 'events': evs}
        return _run(request, go)

    @app.post('/api/game/heal')
    async def game_heal(request: Request):
        def go(conn, ident, st):
            if st['hp'] >= st['max_hp'] and st['mp'] >= st['max_mp']:
                return _err('You are already at full health.')
            cost = _heal_cost(st)
            if st['gold'] < cost:
                return _err(f'Healing costs {cost} gold.')
            st['gold'] -= cost
            st['hp'], st['mp'] = st['max_hp'], st['max_mp']
            st['heals'] += 1
            _bump(st, 'heals')
            msg = f'Healed to full for {cost} gold.'
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
            st['streak'] = st['streak'] + 1 if (last and _now() - last < STREAK_WINDOW) else 1
            day = (st['streak'] - 1) % 7 + 1                  # 7-day cycle; day 7 is the big one
            xp, gold = 100 + 20 * (day - 1), 50 + 15 * (day - 1)
            gems = 5 if day == 7 else 0
            st['daily_at'] = _iso()
            st['dailies'] += 1
            _bump(st, 'dailies')
            events = []
            _gain(st, events, xp=xp, gold=gold, gems=gems)
            msg = (f'Daily reward claimed (streak {st["streak"]}, day {day}/7): +{xp} XP, +{gold} Gold'
                   + (f', +{gems} Gems!' if gems else '!'))
            return {'message': msg, 'streak': st['streak'], 'events': _finish(conn, ident, st, events, 'daily_claim', msg)}
        return _run(request, go)

    @app.post('/api/game/spirit/start')
    async def game_spirit_start(request: Request):
        def go(conn, ident, st):
            st['spirit_started'] = _iso()
            return {}
        return _run(request, go)

    @app.post('/api/game/spirit')
    async def game_spirit(request: Request):
        body = await _body(request)

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

    # ───────────────────────── stats / activity / leaderboard
    @app.get('/api/game/battle-stats')
    async def game_battle_stats(request: Request):
        return _run(request, lambda c, i, s: {
            'battles_won': s['battles_won'], 'battles_lost': s['battles_lost'],
            'world_boss_kills': s['boss_kills'], 'pvp_wins': s['pvp_wins'], 'pvp_losses': s['pvp_losses'],
            'xp': _lifetime_xp(s)}, write=False)

    @app.get('/api/game/activity')
    async def game_activity(request: Request):
        ident = _identity(request)
        if not ident:
            return _unauth()
        conn = _DB()
        try:
            rows = conn.execute("SELECT action, detail, ts FROM activity_log WHERE user_id=? "
                                "ORDER BY id DESC LIMIT 100", (ident['uid'],)).fetchall()
            items = []
            for r in rows:
                ts = r[2] or ''
                if ts and 'T' not in ts:
                    ts = ts.replace(' ', 'T') + 'Z'
                items.append({'action': r[0], 'detail': r[1] or '', 'ts': ts})
            return {'items': items}
        finally:
            conn.close()

    @app.get('/api/game/leaderboard')
    async def game_leaderboard(request: Request):
        conn = _DB()
        try:
            players = []
            for key, s in _all_rows(conn):
                xp = _lifetime_xp(s)
                if xp <= 0:
                    continue
                players.append({'id': key.split(':', 1)[-1], 'name': s.get('name') or key,
                                'class': s.get('char_class', 'Adventurer'), 'xp': xp,
                                'level': _int(s.get('level'), 1), 'title': s.get('equipped_title'),
                                'tower_floor': _int(s.get('tower_floor'))})
            players.sort(key=lambda p: (-p['xp'], p['name']))
            return {'leaderboard': players[:50], 'total': len(players)}
        finally:
            conn.close()

    # ───────────────────────── inventory / gear
    @app.get('/api/game/inventory')
    async def game_inventory(request: Request):
        return _run(request, lambda c, i, s: {'items': _inv_view(s)}, write=False)

    @app.post('/api/game/inventory/equip')
    async def game_equip(request: Request):
        body = await _body(request)

        def go(conn, ident, st):
            name = str(body.get('name', ''))
            item = next((i for i in st['inventory'] if i['name'] == name), None)
            if not item:
                return _err('You do not own that item.')
            slot = _meta(name)['slot']
            if slot not in ('weapon', 'armor'):
                return _err('That item cannot be equipped.')
            if item.get('equipped'):
                item['equipped'] = False
                msg = f'Unequipped {name}.'
            else:
                for other in st['inventory']:
                    if _meta(other['name'])['slot'] == slot:
                        other['equipped'] = False
                item['equipped'] = True
                msg = f'Equipped {name}.'
            return {'message': msg}
        return _run(request, go)

    @app.post('/api/game/inventory/use')
    async def game_use(request: Request):
        body = await _body(request)

        def go(conn, ident, st):
            name = str(body.get('name', ''))
            item = next((i for i in st['inventory'] if i['name'] == name), None)
            m = _meta(name)
            if not item or m['slot'] != 'consumable':
                return _err('You cannot use that.')
            healed_hp = min(m.get('hp', 0), st['max_hp'] - st['hp'])
            healed_mp = min(m.get('mp', 0), st['max_mp'] - st['mp'])
            if not healed_hp and not healed_mp and not m.get('xp'):
                return _err('You are already full. Save it for later.')
            st['hp'] += healed_hp
            st['mp'] += healed_mp
            item['quantity'] = _int(item.get('quantity'), 1) - 1
            if item['quantity'] <= 0:
                st['inventory'].remove(item)
            events = []
            if m.get('xp'):
                _gain(st, events, xp=m['xp'])
            parts = [t for t in (f'+{healed_hp} HP' if healed_hp else '', f'+{healed_mp} MP' if healed_mp else '',
                                 f"+{m['xp']} XP" if m.get('xp') else '') if t]
            msg = f'Used {name}: ' + ', '.join(parts) + '.'
            return {'message': msg, 'events': _finish(conn, ident, st, events, 'item_used', msg)}
        return _run(request, go)

    @app.post('/api/game/inventory/sell')
    async def game_sell(request: Request):
        body = await _body(request)
        names = body.get('names') or []

        def go(conn, ident, st):
            if not isinstance(names, list) or not names:
                return _err('Nothing selected.')
            wanted = {str(n) for n in names}
            kept, total, sold = [], 0, 0
            for it in st['inventory']:
                if it['name'] in wanted and not it.get('equipped') and it['name'] not in NO_SELL:
                    m = _meta(it['name'])
                    unit = int(m['price'] * 0.4) if m.get('price') else RARITY_SELL.get(it.get('rarity'), 8)
                    qty = _int(it.get('quantity'), 1)
                    total += max(1, unit) * qty
                    sold += qty
                else:
                    kept.append(it)
            if not sold:
                return _err('Those items cannot be sold (equipped, not for sale, or missing).')
            st['inventory'] = kept
            st['gold'] += total
            msg = f'Sold {sold} item(s) for {total} gold.'
            _log(conn, ident, 'inventory_sell', msg)
            return {'message': msg, 'gold_gained': total}
        return _run(request, go)

    # ───────────────────────── shop / titles
    @app.get('/api/game/shop')
    async def game_shop(request: Request):
        def go(conn, ident, st):
            disc = _discount(st)
            items = []
            for sid, kind, name, cur, price, req, desc in SHOP:
                final = int(price * (100 - disc) / 100) if cur == 'gold' else price
                m = _meta(name) if kind == 'item' else {}
                items.append({
                    'id': sid, 'kind': kind, 'name': name, 'currency': cur, 'price': final, 'base_price': price,
                    'req_level': req, 'locked': st['level'] < req, 'description': desc,
                    'rarity': m.get('rarity', 'rare' if kind == 'title' else 'common'),
                    'slot': m.get('slot', 'title'), 'atk': m.get('atk', 0), 'def': m.get('df', 0),
                    'owned': (name in st['titles']) if kind == 'title' else False,
                    'owned_qty': sum(_int(i.get('quantity'), 1) for i in st['inventory'] if i['name'] == name),
                })
            return {'items': items, 'discount': disc, 'titles': st['titles'], 'equipped_title': st['equipped_title']}
        return _run(request, go, write=False)

    @app.post('/api/game/shop/buy')
    async def game_shop_buy(request: Request):
        body = await _body(request)

        def go(conn, ident, st):
            entry = next((s for s in SHOP if s[0] == body.get('id')), None)
            if not entry:
                return _err('That item is not for sale.')
            sid, kind, name, cur, price, req, _d = entry
            qty = 1 if kind == 'title' else _clamp(_int(body.get('qty'), 1), 1, 99)
            if st['level'] < req:
                return _err(f'Requires level {req}.')
            if kind == 'title' and name in st['titles']:
                return _err('You already own that title.')
            unit = int(price * (100 - _discount(st)) / 100) if cur == 'gold' else price
            total = unit * qty
            if st[cur] < total:
                return _err(f'Not enough {cur}. You need {total:,}.')
            st[cur] -= total
            events = []
            if kind == 'title':
                st['titles'].append(name)
                msg = f'Bought the title "{name}" for {total:,} {cur}.'
            else:
                _add_item(st, name, qty)
                msg = f'Bought {qty}× {name} for {total:,} {cur}.'
            _log(conn, ident, 'shop_purchase', msg)
            return {'message': msg, 'events': _finish(conn, ident, st, events, None, '')}
        return _run(request, go)

    @app.post('/api/game/title')
    async def game_title(request: Request):
        body = await _body(request)

        def go(conn, ident, st):
            t = body.get('title')
            if t in (None, ''):
                st['equipped_title'] = None
                return {'message': 'Title removed.'}
            if t not in st['titles']:
                return _err('You do not own that title.')
            st['equipped_title'] = t
            return {'message': f'Title set: {t}.'}
        return _run(request, go)

    # ───────────────────────── achievements
    @app.get('/api/game/achievements')
    async def game_achievements(request: Request):
        return _run(request, lambda c, i, s: {'achievements': _ach_list(s)}, write=False)

    @app.post('/api/game/achievements/pin')
    async def game_pin(request: Request):
        body = await _body(request)

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

    # ───────────────────────── world boss
    @app.get('/api/game/boss')
    async def game_boss(request: Request):
        def go(conn, ident, st):
            return {'boss': _boss_view(conn, ident, _ensure_boss(conn))}
        return _run(request, go, save=False)

    @app.post('/api/game/boss/attack')
    async def game_boss_attack(request: Request):
        def go(conn, ident, st):
            boss = _ensure_boss(conn)
            if boss['hp'] <= 0:
                return _err('The boss is already down. Claim your reward!')
            if st['hp'] < st['max_hp'] * 0.2:
                return _err('Too wounded to fight a world boss. Heal first!')
            if _throttle('boss', ident['key'], 20):
                return _err('Your arms are still shaking. Wait a few seconds.', 429)
            d = _derived(st)
            dmg = int((d['attack'] * 3 + d['magic']) * random.uniform(0.85, 1.15))
            crit = random.random() * 100 < d['critical_chance']
            if crit:
                dmg = int(dmg * 1.75)
            dmg = max(1, min(dmg, boss['hp']))
            new_hp = boss['hp'] - dmg
            conn.execute("UPDATE world_boss SET hp=? WHERE id=?", (new_hp, boss['id']))
            conn.execute(
                "INSERT INTO world_boss_damage (boss_id, account_key, name, damage, hits) VALUES (?,?,?,?,1) "
                "ON CONFLICT(boss_id, account_key) DO UPDATE SET damage=damage+excluded.damage, hits=hits+1, name=excluded.name",
                (boss['id'], ident['key'], ident['name'], dmg))
            retaliation = int(st['max_hp'] * random.uniform(0.04, 0.08))
            st['hp'] = max(1, st['hp'] - retaliation)
            st['boss_attacks'] += 1
            xp, gold = dmg // 30 + 10, dmg // 15
            events = []
            _gain(st, events, xp=xp, gold=gold)
            msg = (f'{"Critical hit! " if crit else ""}You hit {boss["name"]} for {dmg:,}. '
                   f'It struck back for {retaliation:,}. +{xp} XP, +{gold} gold.')
            if new_hp <= 0:
                conn.execute("UPDATE world_boss SET killed_by=?, killed_at=? WHERE id=?",
                             (ident['name'], _iso(), boss['id']))
                _gain(st, events, gems=5)
                events.append(('boss', f'{boss["name"]} has fallen! Last hit by {ident["name"]} (+5 gems).'))
            evs = _finish(conn, ident, st, events, 'boss_hit', msg)
            boss = conn.execute("SELECT * FROM world_boss WHERE id=?", (boss['id'],)).fetchone()
            return {'message': msg, 'damage': dmg, 'crit': crit, 'events': evs, 'boss': _boss_view(conn, ident, boss)}
        return _run(request, go)

    @app.post('/api/game/boss/claim')
    async def game_boss_claim(request: Request):
        def go(conn, ident, st):
            rows = conn.execute(
                "SELECT d.boss_id, d.damage, b.name, b.max_hp, b.hp FROM world_boss_damage d "
                "JOIN world_boss b ON b.id=d.boss_id "
                "WHERE d.account_key=? AND d.claimed=0 AND d.damage>0 AND (b.hp<=0 OR b.period<?)",
                (ident['key'], _today())).fetchall()
            if not rows:
                return _err('No boss rewards to claim yet. A boss pays out once it falls or the day ends.')
            events, parts = [], []
            for r in rows:
                share = r['damage'] / r['max_hp']
                killed = r['hp'] <= 0
                mult = 1.0 if killed else 0.3
                gold = int((300 + share * 20000) * mult)
                xp = int((200 + share * 10000) * mult)
                gems = int((3 + share * 40) * mult)
                _gain(st, events, xp=xp, gold=gold, gems=gems)
                line = f'{r["name"]}: +{gold:,} gold, +{xp:,} XP, +{gems} gems'
                if killed and share >= 0.01:
                    st['boss_kills'] += 1
                    _gp(st, events, 20)
                    if random.random() < 0.5:
                        drop = _roll_loot(ZONES['volcano'] if random.random() < 0.5 else ZONES['crypt'])
                        _add_item(st, drop)
                        line += f', and a drop: {drop}'
                parts.append(line + ('' if killed else ' (boss escaped, partial reward)'))
                conn.execute("UPDATE world_boss_damage SET claimed=1 WHERE boss_id=? AND account_key=?",
                             (r['boss_id'], ident['key']))
            msg = 'Boss rewards claimed. ' + '; '.join(parts) + '.'
            return {'message': msg, 'events': _finish(conn, ident, st, events, 'boss_reward_claim', msg)}
        return _run(request, go)

    # ───────────────────────── Adventurer Guild
    def _quest_views(st):
        ri = _rank_idx(st['guild_points'])
        out = []
        for qid, name, desc, need_rank, counter, target, gold, xp, gems, gp in GUILD_QUESTS:
            active = st['quests_active'].get(qid)
            if qid in st['quests_done']:
                status, prog = 'done', target
            elif active:
                prog = _clamp(_counter(st, counter) - _int(active.get('base')), 0, target)
                status = 'ready' if prog >= target else 'active'
            elif ri < need_rank:
                status, prog = 'locked', 0
            else:
                status, prog = 'available', 0
            out.append({'id': qid, 'name': name, 'description': desc, 'rank': RANKS[need_rank][0],
                        'status': status, 'progress': prog, 'target': target,
                        'reward': {'gold': gold, 'xp': xp, 'gems': gems, 'gp': gp}})
        return out

    @app.get('/api/game/guild')
    async def game_guild(request: Request):
        def go(conn, ident, st):
            ri = _rank_idx(st['guild_points'])
            nxt = RANKS[ri + 1] if ri + 1 < len(RANKS) else None
            view = {'member': st['adv_joined'], 'rank': RANKS[ri][0], 'rank_idx': ri,
                    'points': st['guild_points'], 'rank_floor': RANKS[ri][1],
                    'next_rank': nxt[0] if nxt else None, 'next_at': nxt[1] if nxt else None,
                    'discount': _discount(st), 'joined_at': st['adv_joined_at'],
                    'ranks': [{'rank': l, 'points': p, 'discount': RANK_DISCOUNT[i]} for i, (l, p) in enumerate(RANKS)],
                    'max_active': MAX_ACTIVE_QUESTS,
                    'active_count': len(st['quests_active']), 'done_count': len(st['quests_done'])}
            if st['adv_joined']:
                view['quests'] = _quest_views(st)
                view['daily'] = [{
                    'id': qid, 'name': name, 'description': obj, 'done': qid in st['quest_claimed'],
                    'progress': min(target, st['quest_progress'].get(counter, 0)), 'target': target,
                    'reward': {'gold': gold, 'xp': xp, 'gp': gp}}
                    for qid, name, obj, counter, target, gold, xp, gp in DAILY_QUESTS]
            else:
                view['quests'], view['daily'] = [], []
            return {'guild': view}
        return _run(request, go, write=False)

    @app.post('/api/game/guild/join')
    async def game_guild_join(request: Request):
        def go(conn, ident, st):
            if st['adv_joined']:
                return _err('You are already a member of the Adventurer Guild.')
            st['adv_joined'] = True
            st['adv_joined_at'] = _iso()
            _add_item(st, 'Adventurer Guild Card')
            events = []
            _gain(st, events, gold=50)
            msg = 'Welcome to the Adventurer Guild! You received your Guild Card and 50 gold.'
            return {'message': msg, 'events': _finish(conn, ident, st, events, 'guild_join', msg)}
        return _run(request, go)

    @app.post('/api/game/guild/accept')
    async def game_guild_accept(request: Request):
        body = await _body(request)

        def go(conn, ident, st):
            bad = _need_guild(st)
            if bad:
                return bad
            q = next((q for q in GUILD_QUESTS if q[0] == body.get('id')), None)
            if not q:
                return _err('Unknown mission.')
            qid, name, _d, need_rank, counter = q[0], q[1], q[2], q[3], q[4]
            if qid in st['quests_done']:
                return _err('You already completed that mission.')
            if qid in st['quests_active']:
                return _err('You already accepted that mission.')
            if _rank_idx(st['guild_points']) < need_rank:
                return _err(f'Requires Guild Rank {RANKS[need_rank][0]}.')
            if len(st['quests_active']) >= MAX_ACTIVE_QUESTS:
                return _err(f'You can only carry {MAX_ACTIVE_QUESTS} missions at once.')
            st['quests_active'][qid] = {'base': _counter(st, counter), 'at': _iso()}
            msg = f'Mission accepted: {name}.'
            _log(conn, ident, 'quest_accepted', msg)
            return {'message': msg}
        return _run(request, go)

    @app.post('/api/game/guild/abandon')
    async def game_guild_abandon(request: Request):
        body = await _body(request)

        def go(conn, ident, st):
            if body.get('id') not in st['quests_active']:
                return _err('That mission is not active.')
            del st['quests_active'][body['id']]
            return {'message': 'Mission abandoned. Progress was reset.'}
        return _run(request, go)

    @app.post('/api/game/guild/claim')
    async def game_guild_claim(request: Request):
        body = await _body(request)

        def go(conn, ident, st):
            bad = _need_guild(st)
            if bad:
                return bad
            q = next((q for q in GUILD_QUESTS if q[0] == body.get('id')), None)
            active = st['quests_active'].get(body.get('id'))
            if not q or not active:
                return _err('That mission is not active.')
            qid, name, _d, _nr, counter, target, gold, xp, gems, gp = q
            if _counter(st, counter) - _int(active.get('base')) < target:
                return _err('Mission is not finished yet.')
            del st['quests_active'][qid]
            st['quests_done'].append(qid)
            st['quest_history'].insert(0, {'title': name, 'completed_at': _iso(),
                                           'description': f'Guild mission completed. +{gold:,} Gold, +{xp:,} XP.'})
            st['quest_history'] = st['quest_history'][:30]
            events = []
            _gain(st, events, xp=xp, gold=gold, gems=gems)
            _gp(st, events, gp)
            msg = f'Mission complete: {name}! +{gold:,} gold, +{xp:,} XP' + (f', +{gems} gems' if gems else '') + f', +{gp} guild points.'
            return {'message': msg, 'events': _finish(conn, ident, st, events, 'quest_reward_claim', msg)}
        return _run(request, go)

    # ───────────────────────── Tower
    @app.get('/api/game/tower')
    async def game_tower(request: Request):
        def go(conn, ident, st):
            if not st['adv_joined']:
                return {'tower': {'member': False, 'floors_total': TOWER_FLOORS}}
            ri = _rank_idx(st['guild_points'])
            cleared = st['tower_floor']
            nxt = _tower_preview(st)
            ladder = []
            for f in range(min(TOWER_FLOORS, cleared + 8), max(0, cleared - 5), -1):
                ladder.append({'floor': f, 'cleared': f <= cleared, 'boss': f % 5 == 0,
                               'rank_req': RANKS[_tower_rank_req(f)][0],
                               'locked': _tower_rank_req(f) > ri and f > cleared})
            return {'tower': {'member': True, 'cleared': cleared, 'floors_total': TOWER_FLOORS,
                              'next': nxt, 'ladder': ladder, 'rank': RANKS[ri][0],
                              'blocked_by_rank': bool(nxt and _tower_rank_req(nxt['floor']) > ri),
                              'titles': TOWER_TITLES}}
        return _run(request, go, write=False)

    @app.post('/api/game/tower/climb')
    async def game_tower_climb(request: Request):
        def go(conn, ident, st):
            bad = _need_guild(st)
            if bad:
                return _err('Join the Adventurer Guild to enter the Tower.', 403)
            pv = _tower_preview(st)
            if not pv:
                return _err('You have conquered every floor of the Tower!')
            floor = pv['floor']
            if _rank_idx(st['guild_points']) < _tower_rank_req(floor):
                return _err(f'Floor {floor} requires Guild Rank {pv["rank_req"]}. Complete missions to rank up.')
            if st['hp'] < st['max_hp'] * 0.25:
                return _err('Too wounded to climb. Heal first!')
            if _throttle('tower', ident['key'], 2.0):
                return _err('The stairs are crumbling. Wait a moment.', 429)
            chance = pv['win_chance'] / 100
            events = []
            if random.random() < chance:
                st['tower_floor'] = floor
                r = pv['reward']
                st['hp'] = max(1, st['hp'] - int(st['max_hp'] * random.uniform(0.03, 0.08) * (2 if pv['boss'] else 1)))
                _gain(st, events, xp=r['xp'], gold=r['gold'], gems=r['gems'])
                _gp(st, events, r['gp'])
                if floor in TOWER_TITLES:
                    _grant_title(st, events, TOWER_TITLES[floor])
                msg = (f'Floor {floor} cleared! {pv["name"]} falls. +{r["xp"]} XP, +{r["gold"]} gold'
                       + (f', +{r["gems"]} gems' if r['gems'] else '') + f', +{r["gp"]} guild points.')
                evs = _finish(conn, ident, st, events, 'tower_victory', msg)
                return {'result': 'win', 'floor': floor, 'message': msg, 'events': evs}
            dmg = int(st['max_hp'] * random.uniform(0.10, 0.20))
            st['hp'] = max(1, st['hp'] - dmg)
            msg = f'{pv["name"]} drives you back from floor {floor}. -{dmg} HP.'
            evs = _finish(conn, ident, st, events, 'tower_defeat', msg)
            return {'result': 'loss', 'floor': floor, 'message': msg, 'events': evs}
        return _run(request, go)
