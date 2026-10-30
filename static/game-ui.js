/* Shared helpers for the game pages. Exposes window.GX. */
(function () {
    const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const fmt = n => Number(n || 0).toLocaleString();
    const pct = (a, b) => (b > 0 ? Math.max(0, Math.min(100, (a / b) * 100)) : 0);

    function host() {
        let h = document.getElementById('gx-toasts');
        if (!h) { h = document.createElement('div'); h.id = 'gx-toasts'; h.setAttribute('aria-live', 'polite'); document.body.appendChild(h); }
        return h;
    }

    function toast(msg, bad) {
        const el = document.createElement('div');
        el.className = 'gx-toast' + (bad ? ' bad' : '');
        el.textContent = msg;
        host().appendChild(el);
        setTimeout(() => el.remove(), 4800);
    }

    // Calls /api/game/<path>. Never throws; returns { ok, status, data }.
    async function api(method, path, body) {
        try {
            const res = await fetch('/api/game/' + path, {
                method, credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: body === undefined ? undefined : JSON.stringify(body)
            });
            const data = await res.json().catch(() => ({}));
            if (res.status === 401) { window.location.href = '/'; return { ok: false, status: 401, data }; }
            return { ok: res.ok && data.ok !== false, status: res.status, data };
        } catch (e) {
            return { ok: false, status: 0, data: { error: 'Could not reach the server. Try again.' } };
        }
    }

    const EVENT_ICON = { level_up: '⭐', achievement: '🏆', quest: '📜', title: '🎖️', rank: '🛡️', boss: '💀' };

    function events(list) {
        (list || []).forEach((e, i) => setTimeout(() => toast((EVENT_ICON[e.type] || '✨') + ' ' + e.text), 900 * (i + 1)));
    }

    // POST/GET that toasts the outcome and any bonus events.
    async function act(method, path, body) {
        const r = await api(method, path, body);
        if (!r.ok) toast(r.data.error || 'That did not work.', true);
        else { if (r.data.message) toast(r.data.message); events(r.data.events); }
        return r;
    }

    // Player strip: level, HP, XP, wallet and an optional Heal button.
    function hud(el, s, opts) {
        opts = opts || {};
        const role = s.adv_joined ? `Guild Rank ${esc(s.adv_rank)}` : 'No guild';
        const title = s.equipped_title ? ` · ${esc(s.equipped_title)}` : '';
        el.innerHTML = `
            <div class="gx-hud">
                <div class="gx-hud-id">
                    <div class="gx-hud-lv">Level ${fmt(s.level)}</div>
                    <div class="gx-faint">${role}${title}</div>
                </div>
                <div class="gx-meter">
                    <div class="gx-meter-label"><span>Health</span><span>${fmt(s.hp)} / ${fmt(s.max_hp)}</span></div>
                    <div class="gx-track"><div class="gx-fill hp" style="width:${pct(s.hp, s.max_hp)}%"></div></div>
                </div>
                <div class="gx-meter">
                    <div class="gx-meter-label"><span>Experience</span><span>${fmt(s.xp)} / ${fmt(s.xp_needed)}</span></div>
                    <div class="gx-track"><div class="gx-fill xp" style="width:${pct(s.xp, s.xp_needed)}%"></div></div>
                </div>
                <div class="gx-wallet">
                    <span title="Gold"><i class="fa-solid fa-coins gx-gold"></i>${fmt(s.gold)}</span>
                    <span title="Gems"><i class="fa-solid fa-gem" style="color:var(--dash-purple)"></i>${fmt(s.gems)}</span>
                </div>
                ${opts.onHeal ? `<button class="gx-btn sm" id="gx-heal" ${s.hp >= s.max_hp && s.mp >= s.max_mp ? 'disabled' : ''}>Heal · ${fmt(s.heal_cost)} gold</button>` : ''}
            </div>`;
        const b = el.querySelector('#gx-heal');
        if (b) b.addEventListener('click', opts.onHeal);
    }

    function countdown(sec) {
        sec = Math.max(0, Math.floor(sec));
        const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
        return h ? `${h}h ${m}m` : m ? `${m}m ${s}s` : `${s}s`;
    }

    window.GX = { esc, fmt, pct, toast, api, act, events, hud, countdown };
})();
