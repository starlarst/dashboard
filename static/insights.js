/* ══════════════════════════════════════════════════════════════════
   REALM INSIGHTS — contextual intelligence panel for RPG Ghost
   One <script src="/static/insights.js" defer></script> per page.
   • Loads its own stylesheet (insights.css, same folder).
   • Reads the page from the existing sidebar (.nav-links a.active),
     falling back to <body data-page>.
   • Uses only real data from the existing endpoints. Where a page has
     no data source the panel says so instead of inventing numbers.
   • Never touches auth, forms, or existing listeners.
   Public API: window.RealmInsights { open, close, minimize, refresh,
   registerProvider(pageKey, fn) }
   ══════════════════════════════════════════════════════════════════ */
(function () {
    'use strict';
    if (window.RealmInsights) return;               // single instance guard

    // ── bootstrap: stylesheet ───────────────────────────────────────
    const script = document.currentScript;
    const BASE = script && script.src ? script.src.replace(/[^/]*$/, '') : '/static/';
    if (!document.getElementById('rgi-css')) {
        const l = document.createElement('link');
        l.id = 'rgi-css'; l.rel = 'stylesheet'; l.href = BASE + 'insights.css';
        document.head.appendChild(l);
    }

    // ── tiny helpers ────────────────────────────────────────────────
    const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c =>
        ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const num = n => (Number.isFinite(Number(n)) && n !== null && n !== '') ? Math.round(Number(n)).toLocaleString() : '—';
    const pct = (a, b) => (Number(b) > 0 ? Math.max(0, Math.min(100, (Number(a) / Number(b)) * 100)) : 0);
    const cap = s => s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
    const store = {
        get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
        set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* private mode */ } }
    };
    const mqMobile = window.matchMedia('(max-width: 768px)');
    const mqWide = window.matchMedia('(min-width: 1360px)');

    // ── persisted UI state ──────────────────────────────────────────
    const ui = Object.assign({ state: 'closed', secs: {} }, store.get('rgi.ui', {}));
    let dismissed = new Set(store.get('rgi.dismissed', []));
    const saveUI = () => store.set('rgi.ui', ui);
    const saveDismissed = () => store.set('rgi.dismissed', Array.from(dismissed));

    // ── data layer (existing endpoints only, short cache) ───────────
    const cache = new Map();
    async function getJSON(url, ttl, force) {
        const c = cache.get(url);
        if (!force && c && Date.now() - c.t < (ttl || 20000)) return c.v;
        let v = null;
        try {
            const r = await fetch(url, { credentials: 'same-origin' });
            if (r.ok) v = await r.json();
        } catch (e) { /* offline → null */ }
        cache.set(url, { t: Date.now(), v });
        return v;
    }
    const SOURCES = {
        me:       f => getJSON('/api/me', 60000, f),
        state:    async f => { const d = await getJSON('/api/game/state', 15000, f); return d && d.ok ? d.state : null; },
        inv:      async f => { const d = await getJSON('/api/game/inventory', 15000, f); return d && Array.isArray(d.items) ? d.items : null; },
        auctions: async f => { const d = await getJSON('/api/auctions', 15000, f); return d && Array.isArray(d.auctions) ? d.auctions : null; },
        admin:    async f => { const d = await getJSON('/api/admin/users', 20000, f); return d && d.ok ? d.stats : null; },
        guild:    async f => { const d = await getJSON('/api/my-guild', 20000, f); return d ? { guild: d.guild || null } : null; },
        lb:       async f => { const d = await getJSON('/api/game/leaderboard', 30000, f); return d && Array.isArray(d.leaderboard) ? d.leaderboard : null; }
    };

    // ── page detection (reuses the existing navigation) ─────────────
    function navLinks() {
        return Array.from(document.querySelectorAll('.nav-links a')).map(a => {
            const labelEl = a.querySelector('span:not(.nav-icon):not(.nav-badge)');
            const label = ((labelEl ? labelEl.textContent : a.textContent) || '').replace(/\s+/g, ' ').trim();
            const icon = a.querySelector('i');
            return { el: a, page: a.dataset.page || '', label, icon: icon ? icon.className : 'fa-solid fa-circle',
                     active: a.classList.contains('active') };
        }).filter(n => n.label);
    }
    function currentKey() {
        const active = document.querySelector('.nav-links a.active');
        const raw = (active && active.dataset.page) || document.body.dataset.page || 'overview';
        return String(raw).toLowerCase();
    }
    const PAGE_TABLE = [   // first match wins
        ['admin', /admin/], ['auction', /auction|market/], ['quests', /quest/], ['achievements', /achiev/],
        ['battle', /battle|combat|fight/], ['inventory', /invent|equip|gear|item/], ['leaderboard', /leader|rank/],
        ['guild', /guild/], ['community', /communit|lounge|chat/], ['overview', /overview|dashboard|home/]
    ];
    const canonical = key => { const hit = PAGE_TABLE.find(p => p[1].test(key)); return hit ? hit[0] : 'generic'; };

    // ── page models ─────────────────────────────────────────────────
    // A builder returns plain text only (escaped at render time):
    //  { icon, label, overview, chips[[k,v]], details[[k,v]], bar:{label,value}|null,
    //    tips[{id,text}], warnings[text], actions[{label,sub,icon,primary,run}], notes[text] }
    const hasEl = id => !!document.getElementById(id);
    const unavailable = 'Live data could not be loaded right now, so figures are hidden instead of guessed.';

    function vitals(s) {
        const hp = pct(s.hp, s.max_hp);
        return { hpPct: hp, xpPct: pct(s.xp, s.xp_needed), total: (+s.battles_won || 0) + (+s.battles_lost || 0) };
    }
    function charDetails(s) {
        const rows = [['Level', num(s.level)], ['XP', `${num(s.xp)} / ${num(s.xp_needed)}`],
            ['Combat power', num(s.combat_power)], ['HP', `${num(s.hp)} / ${num(s.max_hp)}`],
            ['MP', `${num(s.mp)} / ${num(s.max_mp)}`], ['Gold', num(s.gold)], ['Gems', num(s.gems)]];
        if (s.rank != null) rows.push(['Rank', '#' + num(s.rank)]);
        return rows;
    }
    function lowHp(s, out) {
        if (!s || !(Number(s.max_hp) > 0)) return;
        const p = Math.round(pct(s.hp, s.max_hp));
        if (p < 30) out.push(`HP is at ${p}% (${num(s.hp)} / ${num(s.max_hp)}). Heal before taking another fight.`);
    }
    const act = (label, sub, icon, run, primary) => ({ label, sub, icon, run, primary: !!primary });

    const builders = {
        overview(c) {
            const s = c.state, m = { icon: 'fa-compass', label: 'Overview', needs: ['state'], tips: [], warnings: [], actions: [], notes: [] };
            if (s) {
                const v = vitals(s), cls = s.char_class || (c.me && c.me.char_class) || 'adventurer';
                m.overview = `${c.name ? c.name + ' is' : 'You are'} a level ${num(s.level)} ${cls}, ${Math.round(v.xpPct)}% of the way to level ${num(+s.level + 1)}.`;
                m.chips = [['Level', num(s.level)], ['Power', num(s.combat_power)], ['Gold', num(s.gold)], ['Gems', num(s.gems)]];
                m.details = charDetails(s);
                m.bar = { label: `XP to level ${num(+s.level + 1)}`, value: v.xpPct, text: `${num(Math.max(0, s.xp_needed - s.xp))} XP left` };
                lowHp(s, m.warnings);
                if (v.hpPct < 70) m.actions.push(actRun('heal', 'Heal', 'Restore HP before your next hunt', 'fa-heart-pulse', v.hpPct < 40));
                m.actions.push(actRun('hunt', 'Hunt', 'Fight for XP, gold and loot', 'fa-crosshairs', v.hpPct >= 70));
                m.actions.push(actRun('daily', 'Daily reward', 'Try your daily claim', 'fa-gift'));
            } else {
                m.overview = 'Your character summary, vitals and quick actions live on this page.';
                m.notes.push(unavailable);
            }
            m.tips.push({ id: 'ov-heal', text: 'Use Heal before Hunt whenever the HP bar is running low.' });
            if (hasEl('miniGame')) m.tips.push({ id: 'ov-spirit', text: 'The ghost button (bottom right) opens Spirit Catcher, a short mini-game that rewards gold and XP.' });
            if (hasEl('inventory-grid')) m.tips.push({ id: 'ov-inv', text: 'Hover an inventory slot to see the item name and rarity.' });
            if (hasEl('notifBell')) m.tips.push({ id: 'ov-bell', text: 'The bell in the header collects level-ups and other game events.' });
            return m;
        },

        inventory(c) {
            const items = c.inv, m = { icon: 'fa-box-open', label: 'Inventory', needs: ['inv'], tips: [], warnings: [], actions: [], notes: [] };
            if (items) {
                const qty = items.reduce((n, i) => n + (Number(i.quantity) || 0), 0);
                const by = {};
                items.forEach(i => { const r = String(i.rarity || 'common').toLowerCase(); by[r] = (by[r] || 0) + (Number(i.quantity) || 0); });
                const order = ['legendary', 'epic', 'rare', 'common'];
                const keys = order.filter(k => by[k]).concat(Object.keys(by).filter(k => !order.includes(k)));
                m.overview = items.length ? `${num(items.length)} item types, ${num(qty)} items in total.` : 'Your inventory is empty.';
                m.chips = [['Types', num(items.length)], ['Items', num(qty)]];
                m.details = keys.map(k => [cap(k), num(by[k])]);
                if (!items.length) m.warnings.push('Nothing to equip or sell yet. Hunting is the quickest way to find loot.');
                m.actions.push(actRun('hunt', 'Hunt for loot', 'Go to Overview and fight', 'fa-crosshairs', !items.length));
            } else { m.overview = 'Everything you carry, grouped by rarity.'; m.notes.push(unavailable); }
            m.tips.push({ id: 'in-rarity', text: 'Rarity runs Common, Rare, Epic, then Legendary. The slot colour shows the tier.' });
            m.tips.push({ id: 'in-hover', text: 'Hover (or long-press) an item to see its name and rarity.' });
            return m;
        },

        quests(c) {
            const s = c.state, m = { icon: 'fa-scroll', label: 'Quest Board', needs: ['state'], tips: [], warnings: [], actions: [], notes: [] };
            m.overview = 'Quests reward progression. Review objectives and rewards here, then head out and complete them.';
            if (s) { m.details = [['Level', num(s.level)], ['Combat power', num(s.combat_power)], ['HP', `${num(s.hp)} / ${num(s.max_hp)}`]]; lowHp(s, m.warnings); }
            else m.notes.push(unavailable);
            m.notes.push('Live quest objectives and rewards are not exposed to Insights yet, so none are shown.');
            m.tips.push({ id: 'qs-notify', text: 'Quest completions are reported through the dashboard notification feed.' });
            m.tips.push({ id: 'qs-ready', text: 'Check your HP before starting a quest that involves fighting.' });
            m.actions.push(actNav('overview', 'Review your character', 'Level, power and vitals', 'fa-id-card'));
            return m;
        },

        achievements(c) {
            const s = c.state, m = { icon: 'fa-trophy', label: 'Achievements', needs: ['state'], tips: [], warnings: [], actions: [], notes: [] };
            m.overview = 'Achievements track long-term milestones across your adventure.';
            if (s) {
                m.chips = [['Level', num(s.level)]];
                m.details = [['Current level', num(s.level)], ['XP to next level', num(Math.max(0, s.xp_needed - s.xp))], ['Battles won', num(s.battles_won)]];
                m.bar = { label: `Next milestone: level ${num(+s.level + 1)}`, value: pct(s.xp, s.xp_needed), text: `${Math.round(pct(s.xp, s.xp_needed))}%` };
            } else m.notes.push(unavailable);
            m.notes.push('Achievement completion data is not exposed to Insights yet, so no percentage is shown.');
            m.tips.push({ id: 'ac-notify', text: 'Newly earned achievements appear in the notification feed as they happen.' });
            m.actions.push(actNav('overview', 'Keep progressing', 'Hunt and level up from Overview', 'fa-crosshairs'));
            return m;
        },

        battle(c) {
            const s = c.state, m = { icon: 'fa-khanda', label: 'Battle Log', needs: ['state'], tips: [], warnings: [], actions: [], notes: [] };
            if (s) {
                const w = +s.battles_won || 0, l = +s.battles_lost || 0, t = w + l, rate = t ? Math.round((w / t) * 100) : null;
                m.overview = t ? `${num(t)} battles recorded: ${num(w)} won and ${num(l)} lost${rate != null ? ` (${rate}% win rate)` : ''}.`
                               : 'No battles recorded yet. Hunt from Overview to start your log.';
                m.chips = [['Won', num(w)], ['Lost', num(l)]];
                if (rate != null) m.chips.push(['Win rate', rate + '%']);
                m.details = [['Battles won', num(w)], ['Battles lost', num(l)], ['Total', num(t)], ['Win rate', rate != null ? rate + '%' : '—'],
                             ['Combat power', num(s.combat_power)], ['HP', `${num(s.hp)} / ${num(s.max_hp)}`]];
                if (rate != null) m.bar = { label: 'Win rate', value: rate, text: rate + '%' };
                lowHp(s, m.warnings);
                if (t >= 10 && rate < 40) m.warnings.push(`Your win rate is ${rate}% over ${num(t)} battles. Recover HP and compare your Combat Power before the next fight.`);
                if (pct(s.hp, s.max_hp) < 70) m.actions.push(actRun('heal', 'Heal', 'Restore HP first', 'fa-heart-pulse', true));
                m.actions.push(actRun('hunt', 'Hunt', 'Add another fight to the log', 'fa-crosshairs'));
            } else { m.overview = 'A record of your fights and how they went.'; m.notes.push(unavailable); }
            m.tips.push({ id: 'bt-power', text: 'Combat Power is your overall strength rating. Compare it before and after changing gear.' });
            m.tips.push({ id: 'bt-heal', text: 'Healing between fights keeps defeats from stacking up.' });
            return m;
        },

        auction(c) {
            const a = c.auctions, s = c.state, m = { icon: 'fa-gavel', label: 'Auction House', needs: ['auctions', 'state'], tips: [], warnings: [], actions: [], notes: [] };
            if (a) {
                const bids = a.map(x => Number(x.current_bid) || 0).filter(n => n >= 0);
                const min = bids.length ? Math.min(...bids) : null, max = bids.length ? Math.max(...bids) : null;
                m.overview = a.length ? `${num(a.length)} active listing${a.length === 1 ? '' : 's'} right now.` : 'There are no active listings right now.';
                m.chips = [['Listings', num(a.length)]];
                m.details = [['Active listings', num(a.length)], ['Lowest current bid', min != null ? num(min) + ' gold' : '—'], ['Highest current bid', max != null ? num(max) + ' gold' : '—']];
                if (s) {
                    m.details.push(['Your gold', num(s.gold) + ' gold']);
                    if (a.length) m.details.push(['Listings you can afford', num(bids.filter(b => b <= s.gold).length)]);
                    if (min != null && Number(s.gold) < min) m.warnings.push(`Your gold (${num(s.gold)}) is below the lowest current bid (${num(min)}).`);
                }
            } else { m.overview = 'Buy and sell items with other adventurers.'; m.notes.push(unavailable); }
            m.tips.push({ id: 'au-list', text: 'Only items in your inventory can be listed. Set a starting bid you would be happy to accept.' });
            m.tips.push({ id: 'au-dur', text: 'Longer listings give more players time to bid; shorter ones return your gold sooner.' });
            m.tips.push({ id: 'au-gold', text: 'Keep some gold in reserve so you can still bid when a good listing appears.' });
            m.actions.push(actNav('inventory', 'Check your inventory', 'Find something worth listing', 'fa-box-open'));
            return m;
        },

        admin(c) {
            const st = c.admin, m = { icon: 'fa-crown', label: 'Admin Panel', needs: ['admin'], tips: [], warnings: [], actions: [], notes: [] };
            m.overview = 'Player, account and economy tools for administrators.';
            if (st) {
                m.chips = [['Users', num(st.total)], ['Online', num(st.online)]];
                m.details = [['Registered users', num(st.total)], ['Online now', num(st.online)], ['Admins', num(st.admins)]];
            } else if (c.me && c.me.is_admin === false) {
                m.warnings.push('Your account is not flagged as an admin, so admin tools may be unavailable.');
            } else m.notes.push('System figures could not be loaded right now.');
            m.tips.push({ id: 'ad-id', text: 'Double-check the player ID before giving gold or editing a player.' });
            m.tips.push({ id: 'ad-ann', text: 'Announcements are sent to players, so keep them short and clear.' });
            [['adminTabUsers', 'Users', 'Accounts, roles and status', 'fa-users'], ['adminTabPlayers', 'Players', 'Search and edit characters', 'fa-user-pen'],
             ['adminTabActions', 'Actions', 'Announcements and gold grants', 'fa-bolt']].forEach(([id, label, sub, icon]) => {
                if (hasEl(id)) m.actions.push(act(label, sub, icon, () => document.getElementById(id).click()));
            });
            return m;
        },

        leaderboard(c) {
            const lb = c.lb, m = { icon: 'fa-ranking-star', label: 'Leaderboard', needs: ['lb'], tips: [], warnings: [], actions: [], notes: [] };
            if (lb && lb.length) {
                m.overview = `Top ${num(lb.length)} adventurers ranked by XP.`;
                const myId = c.me && c.me.discord_id, idx = myId != null ? lb.findIndex(p => String(p.id) === String(myId)) : -1;
                m.details = [['Players listed', num(lb.length)], ['Leader', `${lb[0].name} · ${num(lb[0].xp)} XP`]];
                if (idx >= 0) {
                    m.details.push(['Your position', '#' + (idx + 1)]);
                    if (idx > 0) m.details.push(['XP to pass the next player', num(Math.max(0, lb[idx - 1].xp - lb[idx].xp) + 1)]);
                } else m.notes.push('You are not in the visible part of the ranking.');
            } else { m.overview = 'See how you rank against other adventurers.'; m.notes.push(unavailable); }
            m.tips.push({ id: 'lb-xp', text: 'Rankings follow XP, so every successful hunt moves you up.' });
            m.actions.push(actRun('hunt', 'Hunt for XP', 'Go to Overview and fight', 'fa-crosshairs'));
            return m;
        },

        guild(c) {
            const g = c.guild && c.guild.guild, m = { icon: 'fa-shield-halved', label: 'Guild Hall', needs: ['guild'], tips: [], warnings: [], actions: [], notes: [] };
            if (c.guild && !g) {
                m.overview = 'You are not in a guild yet. Create one or browse existing guilds to join.';
                m.tips.push({ id: 'gu-join', text: 'Use Create Guild or Browse & Join on this page to get started.' });
            } else if (g) {
                m.overview = `${g.name} · level ${num(g.level)} · ${num(g.member_count)} of ${num(g.max_members)} members.`;
                m.chips = [['Level', num(g.level)], ['Members', `${num(g.member_count)}/${num(g.max_members)}`]];
                m.details = [['Your role', g.my_role || (g.is_leader ? 'Leader' : g.is_officer ? 'Officer' : 'Member')], ['Leader', g.leader_name || '—'], ['Treasury', num(g.treasury) + ' gold']];
                if (g.motd) m.notes.push('Message of the day: ' + g.motd);
                if (Number(g.member_count) >= Number(g.max_members)) m.warnings.push('The guild is full, so new members cannot join until someone leaves.');
                m.tips.push({ id: 'gu-treasury', text: 'Anyone can deposit into the treasury; only the leader can withdraw.' });
                m.tips.push({ id: 'gu-chat', text: 'Guild chat refreshes automatically, so you can leave it open while you play.' });
            } else { m.overview = 'Coordinate with your guild and review its roster.'; m.notes.push(unavailable); }
            return m;
        },

        community(c) {
            const m = { icon: 'fa-comments', label: 'Community', needs: [], tips: [], warnings: [], actions: [], notes: [] };
            m.overview = 'Chat in the Lounge or message other adventurers directly.';
            m.tips.push({ id: 'cm-status', text: 'Set your status so friends know whether you are free to chat.' });
            m.tips.push({ id: 'cm-mention', text: 'Type @ followed by a name to mention someone.' });
            m.tips.push({ id: 'cm-safety', text: 'Use Report on a message or Block on a profile if someone is bothering you.' });
            return m;
        },

        generic(c) {
            const s = c.state, m = { icon: 'fa-circle-info', label: (document.title || 'This page').replace(/^RPG Ghost\s*[|\-–]\s*/i, ''), needs: ['state'], tips: [], warnings: [], actions: [], notes: [] };
            m.overview = 'No page-specific insights are available here yet. Here is your current character snapshot.';
            if (s) { m.chips = [['Level', num(s.level)], ['Gold', num(s.gold)]]; m.details = charDetails(s); lowHp(s, m.warnings); }
            else m.notes.push(unavailable);
            return m;
        }
    };

    // action factories ------------------------------------------------
    function actRun(name, label, sub, icon, primary) {
        return { label, sub, icon, primary: !!primary, run() {
            const go = () => { if (typeof window.performAction === 'function') window.performAction(name); };
            if (typeof window.performAction === 'function') {
                if (canonical(currentKey()) !== 'overview' && gotoPage('overview')) setTimeout(go, 80); else go();
            } else if (!gotoPage('overview')) toast('Open the Overview page to do this.');
        } };
    }
    function actNav(page, label, sub, icon) { return { label, sub, icon, run() { if (!gotoPage(page)) toast('That page is not available from here.'); } }; }
    function gotoPage(canon) {
        const link = navLinks().find(n => canonical((n.page || n.label).toLowerCase()) === canon || canonical(n.label.toLowerCase()) === canon);
        if (!link) return false;
        link.el.click();               // runs the site's own navigation (in-page or full load)
        return true;
    }
    function toast(msg) { if (typeof window.showToast === 'function') window.showToast(msg); }

    // optional extension point ---------------------------------------
    const providers = {};

    // ── build the model for the current page ────────────────────────
    let seq = 0, model = null, modelSig = '', modelKey = '';
    async function buildModel(force) {
        const mySeq = ++seq, key = currentKey(), canon = canonical(key);
        const draft = builders[canon]({ me: null });   // read `needs` only
        const needs = draft.needs || [];
        const ctx = {};
        const jobs = [SOURCES.me(force).then(v => { ctx.me = v && v.logged_in ? v : null; })];
        needs.forEach(n => jobs.push(SOURCES[n](force).then(v => { ctx[n === 'inv' ? 'inv' : n] = v; })));
        await Promise.all(jobs);
        if (mySeq !== seq) return null;                // user navigated away mid-load
        ctx.name = ctx.me && (ctx.me.username || ctx.me.name) || '';
        ctx.page = key;
        const m = builders[canon](ctx);
        m.key = key; m.canon = canon; m.nav = navLinks();
        if (providers[canon]) {
            try { const extra = await providers[canon](ctx, m); if (extra) mergeExtra(m, extra); } catch (e) { console.warn('[Insights] provider failed', e); }
        }
        return m;
    }
    function mergeExtra(m, x) {
        ['details', 'tips', 'warnings', 'actions', 'notes'].forEach(k => { if (Array.isArray(x[k])) m[k] = (m[k] || []).concat(x[k]); });
        if (x.overview) m.overview = x.overview;
    }

    // ── DOM ─────────────────────────────────────────────────────────
    let panel, fab, scrim, bodyEl, subEl, minBtn, expBtn, badgeEl, liveEl, mounted = false;

    function mount() {
        if (mounted) return; mounted = true;
        fab = document.createElement('button');
        fab.className = 'rgi-fab'; fab.type = 'button';
        fab.setAttribute('aria-label', 'Open Realm Insights'); fab.setAttribute('aria-haspopup', 'dialog');
        fab.innerHTML = '<i class="fa-solid fa-lightbulb" aria-hidden="true"></i><span class="rgi-badge" hidden></span>';
        badgeEl = fab.querySelector('.rgi-badge');

        scrim = document.createElement('div'); scrim.className = 'rgi-scrim';

        panel = document.createElement('aside');
        panel.className = 'rgi-panel'; panel.tabIndex = -1;
        panel.setAttribute('role', 'complementary'); panel.setAttribute('aria-label', 'Realm Insights');
        panel.innerHTML = `
            <div class="rgi-handle" aria-hidden="true"></div>
            <header class="rgi-head">
                <div class="rgi-logo" aria-hidden="true"><i class="fa-solid fa-lightbulb"></i></div>
                <div class="rgi-titles"><div class="rgi-title">Realm Insights</div><div class="rgi-sub" id="rgiSub">Loading…</div></div>
                <button type="button" class="rgi-btn" data-rgi="min" aria-label="Minimize insights"><i class="fa-solid fa-minus" aria-hidden="true"></i></button>
                <button type="button" class="rgi-btn" data-rgi="exp" aria-label="Expand insights" hidden><i class="fa-solid fa-expand" aria-hidden="true"></i></button>
                <button type="button" class="rgi-btn" data-rgi="close" aria-label="Close insights"><i class="fa-solid fa-xmark" aria-hidden="true"></i></button>
            </header>
            <div class="rgi-body" id="rgiBody"></div>
            <div class="rgi-sr" aria-live="polite" id="rgiLive"></div>`;
        document.body.append(scrim, fab, panel);
        bodyEl = panel.querySelector('#rgiBody'); subEl = panel.querySelector('#rgiSub'); liveEl = panel.querySelector('#rgiLive');
        minBtn = panel.querySelector('[data-rgi="min"]'); expBtn = panel.querySelector('[data-rgi="exp"]');

        fab.addEventListener('click', () => api.open(true));
        scrim.addEventListener('click', () => api.close());
        panel.addEventListener('click', onPanelClick);
        panel.addEventListener('keydown', e => { if (e.key === 'Escape') { e.stopPropagation(); api.close(true); } });
        setupSwipe();
        applyState();
    }

    // ── state machine ───────────────────────────────────────────────
    function applyState() {
        if (!mounted) return;
        const s = ui.state, open = s === 'open', min = s === 'min';
        panel.classList.toggle('is-open', open); panel.classList.toggle('is-min', min);
        panel.setAttribute('aria-hidden', String(!(open || min)));
        fab.classList.toggle('is-shown', s === 'closed');
        scrim.classList.toggle('is-shown', open && mqMobile.matches);
        minBtn.hidden = min; expBtn.hidden = !min;
        document.body.classList.toggle('rgi-docked', open && mqWide.matches && !!document.querySelector('.main-content'));
        // keep hidden controls out of the tab order
        panel.querySelectorAll('button').forEach(b => { if (!b.hidden) b.tabIndex = (open || min) ? 0 : -1; });
    }
    function setState(s, persist) {
        ui.state = s;
        if (persist !== false) saveUI();
        applyState();
    }

    const api = window.RealmInsights = {
        open(focus) { mount(); setState('open'); render(true); refresh(true); if (focus) panel.focus({ preventScroll: true }); },
        close(returnFocus) { if (!mounted) return; setState('closed'); if (returnFocus) fab.focus({ preventScroll: true }); },
        minimize() { if (!mounted) return; setState('min'); },
        refresh() { return refresh(true); },
        registerProvider(pageKey, fn) { if (typeof fn === 'function') providers[pageKey] = fn; }
    };

    // ── render ──────────────────────────────────────────────────────
    function sec(id, cat, icon, title, count, inner, i) {
        const collapsed = ui.secs[id] === false || (ui.secs[id] === undefined && id === 'nav');
        const sid = 'rgi-s-' + id;
        return `<section class="rgi-sec${collapsed ? ' is-collapsed' : ''}" data-cat="${cat}" data-sec="${id}" style="--i:${i}">
            <button type="button" class="rgi-sec-h" aria-expanded="${!collapsed}" aria-controls="${sid}">
                <span class="rgi-sec-ic" aria-hidden="true"><i class="fa-solid ${icon}"></i></span>
                <span class="rgi-sec-t">${esc(title)}</span>
                ${count ? `<span class="rgi-count">${count}</span>` : ''}
                <i class="fa-solid fa-chevron-down rgi-chev" aria-hidden="true"></i>
            </button>
            <div class="rgi-sec-c" id="${sid}">${inner}</div></section>`;
    }

    function visibleTips(m) { return (m.tips || []).filter(t => !dismissed.has(m.canon + ':' + t.id)); }

    function render(animate) {
        if (!mounted) return;
        if (!model) {
            bodyEl.innerHTML = '<div class="rgi-loading" aria-hidden="true"><div class="rgi-skel"></div><div class="rgi-skel"></div><div class="rgi-skel"></div></div>';
            return;
        }
        const m = model;
        subEl.textContent = m.label;
        let i = 0, html = '';

        // 1. Page overview
        let ov = `<p class="rgi-p">${esc(m.overview)}</p>`;
        if (m.chips && m.chips.length) ov += `<div class="rgi-chips">${m.chips.map(c => `<span class="rgi-chip">${esc(c[0])}<b>${esc(c[1])}</b></span>`).join('')}</div>`;
        if (m.bar) ov += `<div><div class="rgi-row" style="border:0;padding:0 0 5px"><span>${esc(m.bar.label)}</span><b>${esc(m.bar.text || '')}</b></div>
            <div class="rgi-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(m.bar.value)}" aria-label="${esc(m.bar.label)}"><i data-w="${m.bar.value.toFixed(1)}"></i></div></div>`;
        html += sec('overview', 'overview', m.icon, 'Page overview', 0, ov, i++);

        // 2. Key details
        const rows = (m.details || []).map(r => `<div class="rgi-row"><span>${esc(r[0])}</span><b>${esc(r[1])}</b></div>`).join('');
        const notes = (m.notes || []).map(n => `<div class="rgi-note"><i class="fa-solid fa-circle-info" aria-hidden="true"></i><span>${esc(n)}</span></div>`).join('');
        if (rows || notes) html += sec('details', 'details', 'fa-list-ul', 'Key details', 0, (rows ? `<div class="rgi-rows">${rows}</div>` : '') + notes, i++);

        // 3. Warnings (only when something real needs attention)
        if (m.warnings && m.warnings.length) {
            html += sec('warnings', 'warnings', 'fa-triangle-exclamation', 'Important warnings', m.warnings.length,
                m.warnings.map((w, k) => `<div class="rgi-warn" role="alert" style="--i:${k}"><i class="fa-solid fa-circle-exclamation" aria-hidden="true"></i><p>${esc(w)}</p></div>`).join(''), i++);
        }

        // 4. Helpful tips (dismissed ones stay dismissed)
        const tips = visibleTips(m), shown = tips.slice(0, 3), hiddenN = (m.tips || []).length - tips.length;
        if (shown.length || hiddenN) {
            let inner = shown.map((t, k) => `<div class="rgi-tip" style="--i:${k}"><i class="fa-solid fa-lightbulb" aria-hidden="true"></i><p>${esc(t.text)}</p>
                <button type="button" class="rgi-x" data-tip="${esc(t.id)}" aria-label="Dismiss this tip"><i class="fa-solid fa-xmark" aria-hidden="true"></i></button></div>`).join('');
            if (!shown.length) inner = '<div class="rgi-note"><i class="fa-solid fa-check" aria-hidden="true"></i><span>You have dismissed every tip for this page.</span></div>';
            if (hiddenN) inner += `<button type="button" class="rgi-link" data-reset="1">Restore ${hiddenN} dismissed tip${hiddenN === 1 ? '' : 's'}</button>`;
            html += sec('tips', 'tips', 'fa-lightbulb', 'Helpful tips', shown.length, inner, i++);
        }

        // 5. Recommended actions
        if (m.actions && m.actions.length) {
            html += sec('actions', 'actions', 'fa-bolt', 'Recommended actions', m.actions.length,
                `<div class="rgi-acts">${m.actions.map((a, k) => `<button type="button" class="rgi-act${a.primary ? ' is-primary' : ''}" data-act="${k}">
                    <i class="fa-solid ${esc(a.icon)}" aria-hidden="true"></i><span class="rgi-act-t">${esc(a.label)}${a.sub ? `<small>${esc(a.sub)}</small>` : ''}</span></button>`).join('')}</div>`, i++);
        }

        // 6. Quick navigation (read from the real sidebar)
        const navs = (m.nav || []).filter(n => !n.active);
        if (navs.length) {
            html += sec('nav', 'nav', 'fa-location-arrow', 'Quick navigation', 0,
                `<div class="rgi-navs">${navs.map((n, k) => `<button type="button" class="rgi-navbtn" data-nav="${k}"><i class="${esc(n.icon)}" aria-hidden="true"></i><span>${esc(n.label)}</span></button>`).join('')}</div>`, i++);
        }

        html += '<div class="rgi-foot">Based on your live game data</div>';
        const scroll = bodyEl.scrollTop;
        bodyEl.classList.toggle('rgi-noanim', !animate);
        bodyEl.innerHTML = html;
        bodyEl.scrollTop = animate ? 0 : scroll;
        requestAnimationFrame(() => bodyEl.querySelectorAll('.rgi-bar i[data-w]').forEach(b => { b.style.width = b.dataset.w + '%'; }));
        liveEl.textContent = animate ? `Insights updated for ${m.label}` : '';
    }

    function onPanelClick(e) {
        const t = e.target.closest('button'); if (!t || !panel.contains(t)) return;
        if (t.dataset.rgi === 'close') return api.close(true);
        if (t.dataset.rgi === 'min') return api.minimize();
        if (t.dataset.rgi === 'exp') { setState('open'); refresh(true); return; }
        const head = t.closest('.rgi-sec-h');
        if (head) {
            const s = head.parentElement, collapsed = !s.classList.contains('is-collapsed');
            s.classList.toggle('is-collapsed', collapsed); head.setAttribute('aria-expanded', String(!collapsed));
            ui.secs[s.dataset.sec] = !collapsed; saveUI(); return;
        }
        if (t.dataset.tip) { dismissed.add(model.canon + ':' + t.dataset.tip); saveDismissed(); render(false); return; }
        if (t.dataset.reset) { (model.tips || []).forEach(x => dismissed.delete(model.canon + ':' + x.id)); saveDismissed(); render(false); return; }
        if (t.dataset.act != null && model.actions[+t.dataset.act]) {
            if (mqMobile.matches) setState('min');         // get out of the way so the result is visible
            model.actions[+t.dataset.act].run(); scheduleRefresh(1500); return;
        }
        if (t.dataset.nav != null) {
            const link = model.nav.filter(n => !n.active)[+t.dataset.nav];
            if (link) { if (mqMobile.matches) setState('min'); link.el.click(); }
        }
    }

    // ── refresh / navigation awareness ──────────────────────────────
    let refreshTimer = null;
    function scheduleRefresh(ms) { clearTimeout(refreshTimer); refreshTimer = setTimeout(() => refresh(true), ms); }

    async function refresh(force) {
        if (!mounted) return;
        const m = await buildModel(force);
        if (!m) return;
        const sig = JSON.stringify([m.key, m.overview, m.chips, m.details, m.bar, m.warnings, m.notes, m.actions.map(a => a.label), m.nav.map(n => n.label + n.active)]);
        const samePage = m.key === modelKey;
        model = m;
        updateBadge();
        if (sig !== modelSig || !samePage) {
            const animate = !samePage || modelSig === '';
            modelSig = sig; modelKey = m.key;
            if (ui.state !== 'closed') render(animate);
        }
    }
    function updateBadge() {
        const n = model && model.warnings ? model.warnings.length : 0;
        badgeEl.hidden = !n; badgeEl.textContent = n ? String(n) : '';
        fab.setAttribute('aria-label', n ? `Open Realm Insights, ${n} warning${n === 1 ? '' : 's'}` : 'Open Realm Insights');
    }

    let lastKey = '';
    function onNavigate() {
        const k = currentKey();
        if (k === lastKey) return;
        lastKey = k; model = null; modelSig = '';
        if (ui.state !== 'closed') render(true);
        refresh(false);
    }
    let navRaf = 0;
    const queueNav = () => { cancelAnimationFrame(navRaf); navRaf = requestAnimationFrame(onNavigate); };

    // ── swipe-to-dismiss (bottom sheet) ─────────────────────────────
    function setupSwipe() {
        let y0 = null, dy = 0;
        const start = e => { if (!mqMobile.matches) return; y0 = e.touches[0].clientY; dy = 0; panel.classList.add('rgi-drag'); };
        const move = e => {
            if (y0 == null) return;
            dy = Math.max(0, e.touches[0].clientY - y0);
            panel.style.transform = `translateY(${dy}px)`;
        };
        const end = () => {
            if (y0 == null) return;
            panel.classList.remove('rgi-drag'); panel.style.transform = '';
            const dist = dy; y0 = null; dy = 0;
            if (dist > 90) (ui.state === 'min' ? api.close() : api.minimize());
        };
        [panel.querySelector('.rgi-handle'), panel.querySelector('.rgi-head')].forEach(el => {
            el.addEventListener('touchstart', start, { passive: true });
            el.addEventListener('touchmove', move, { passive: true });
            el.addEventListener('touchend', end);
            el.addEventListener('touchcancel', end);
        });
    }

    // ── start-up: only for signed-in users ──────────────────────────
    let started = false;
    async function start() {
        if (started) return;
        // dashboard.html shows a login screen first; never mount over it
        const appEl = document.getElementById('dashboard-app');
        if (appEl && getComputedStyle(appEl).display === 'none') return false;
        const me = await SOURCES.me(true);
        if (!me || !me.logged_in) return false;
        started = true;
        mount();
        if (mqMobile.matches && ui.state === 'open') ui.state = 'closed';   // never auto-open a sheet
        applyState();
        lastKey = currentKey();
        await refresh(false);
        if (ui.state !== 'closed') render(true);

        // single, delegated listeners
        document.addEventListener('click', ev => {
            if (ev.target.closest && ev.target.closest('.nav-links a')) queueNav();
            if (ev.target.closest && ev.target.closest('.quick-actions .action-btn')) scheduleRefresh(1800);
        });
        window.addEventListener('popstate', queueNav);
        window.addEventListener('hashchange', queueNav);
        new MutationObserver(muts => {
            for (const m of muts) { const t = m.target; if (t && t.closest && t.closest('.nav-links')) { queueNav(); return; } }
        }).observe(document.body, { attributes: true, attributeFilter: ['class'], subtree: true });
        const onMq = () => applyState();
        (mqMobile.addEventListener ? mqMobile.addEventListener('change', onMq) : mqMobile.addListener(onMq));
        (mqWide.addEventListener ? mqWide.addEventListener('change', onMq) : mqWide.addListener(onMq));
        setInterval(() => { if (ui.state === 'open' && !document.hidden) refresh(true); }, 30000);
        document.addEventListener('visibilitychange', () => { if (!document.hidden && ui.state !== 'closed') refresh(false); });
        return true;
    }

    function boot() {
        start().then(ok => {
            if (ok !== false) return;
            // Not signed in yet (e.g. dashboard login screen). Wait for the app to appear.
            const app = document.getElementById('dashboard-app');
            if (!app) return;
            // showDashboard() reveals the app with an inline display style, so watch style as well as class
            const obs = new MutationObserver(() => {
                if (getComputedStyle(app).display !== 'none') { obs.disconnect(); start(); }
            });
            obs.observe(app, { attributes: true, attributeFilter: ['class', 'style'] });
        }).catch(e => console.warn('[Insights] start failed', e));
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();
