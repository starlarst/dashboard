const RECAPTCHA_SITE_KEY = "6LdvvT8tAAAAAKDmzCl0RaBvPr8ndJbdmFNQaqk7";
const API_BASE = window.location.origin;
let currentUser           = null;
let isDashboardInitialized = false;
let isShowingDashboard    = false;
let resetEmail            = "";
let resetOTP              = "";
let notifications         = [];
let cachedInventoryItems  = [];
function escapeHTML(str) {
    if (str === null || str === undefined) return '';
    return String(str).replace(/[&<>"']/g, ch => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[ch]));
}

const pageConfig = {
    'overview': {
        panels: ['character-card', 'inventory-grid', 'stats-row', 'leaderboard-panel', 'auction-panel'],
        titles: {
            'inventory-grid':  '<i class="fa-solid fa-backpack" style="color:var(--soul-purple)"></i> Inventory',
            'leaderboard-panel':'<i class="fa-solid fa-trophy" style="color:var(--gold)"></i> Leaderboard',
            'auction-panel':   '<i class="fa-solid fa-gavel" style="color:var(--gold)"></i> Auction House'
        }
    }
};

document.addEventListener('DOMContentLoaded', () => {
    // Auth UI wiring runs first and is isolated so a failure elsewhere can't break it.
    try { setupAuthUI(); } catch (e) { console.error('setupAuthUI failed:', e); }

    checkAuth();
    setupSidebar();
    setupOTPInputs();
    setupDiscordButtons();
    setupNotifications();
    setupListItemModal();
    setupEditPlayerModal();
    setupMusicToggle();
    setupSpiritGame();

    document.getElementById('adminTabUsers')?.addEventListener('click', () => switchAdminTab('users'));
    document.getElementById('adminTabPlayers')?.addEventListener('click', () => switchAdminTab('players'));
    document.getElementById('adminTabActions')?.addEventListener('click', () => switchAdminTab('actions'));
});

// ════════════════════════════════════════════════════════════════════
// AUTH UI WIRING (tabs, eye toggles, forgot / OTP / resend links)
// The HTML has no inline onclick on these controls, so wire them here.
// ════════════════════════════════════════════════════════════════════
let resendCooldownTimer = null;

function showAuthForm(id) {
    document.querySelectorAll('.auth-tab').forEach(t => {
        t.classList.remove('active');
        t.setAttribute('aria-selected', 'false');
    });
    document.querySelectorAll('.auth-form').forEach(f => f.classList.remove('active'));
    document.getElementById(id)?.classList.add('active');
}

function setupAuthUI() {
    // Sign In / Create Account tabs
    document.querySelectorAll('.auth-tab').forEach(tab => {
        tab.addEventListener('click', () => switchTab(tab.dataset.tab));
    });

    // Show / hide password
    document.querySelectorAll('.password-toggle[data-target]').forEach(btn => {
        btn.addEventListener('click', () => togglePassword(btn.dataset.target));
    });

    // Forgot password link
    document.getElementById('show-forgot')?.addEventListener('click', e => {
        e.preventDefault();
        showForgotPasswordForm();
    });

    // Back to sign in links
    ['show-login-from-forgot', 'show-login-from-otp'].forEach(id => {
        document.getElementById(id)?.addEventListener('click', e => {
            e.preventDefault();
            switchTab('login');
        });
    });

    // Resend code
    document.getElementById('resend-otp')?.addEventListener('click', e => {
        e.preventDefault();
        resendOTP();
    });

    // Allow pasting a full 6-digit code into the OTP boxes
    const otpInputs = document.querySelectorAll('.otp-input');
    otpInputs.forEach(input => {
        input.addEventListener('paste', e => {
            const digits = (e.clipboardData?.getData('text') || '').replace(/\D/g, '').slice(0, 6);
            if (!digits) return;
            e.preventDefault();
            otpInputs.forEach((box, i) => { box.value = digits[i] || ''; });
            otpInputs[Math.min(digits.length, 6) - 1]?.focus();
        });
    });
}

async function sendAnnouncement() {
    const message = document.getElementById('announceMsg').value.trim();
    if (!message) return showToast('Enter a message first');
    try {
        const res = await fetch('/api/admin/announce', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message })
        });
        const data = await res.json();
        showToast(data.ok ? 'Announcement sent!' : (data.error || 'Failed'));
    } catch (e) { showToast('Request failed'); }
}

async function giveGold() {
    const user_id = document.getElementById('goldUserId').value.trim();
    const amount = parseInt(document.getElementById('goldAmount').value, 10);
    if (!user_id || !amount) return showToast('Enter user ID and amount');
    try {
        const res = await fetch('/api/admin/give-gold', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user_id, amount })
        });
        const data = await res.json();
        showToast(data.ok ? `Gave ${amount} gold to ${user_id}` : (data.error || 'Failed'));
    } catch (e) { showToast('Request failed'); }
}

async function checkAuth() {
    const params = new URLSearchParams(window.location.search);
    const authStatus = params.get('auth');

    if (authStatus === 'success') {
        window.history.replaceState({}, document.title, window.location.pathname);
        showToast('Successfully logged in with Discord!');
    } else if (authStatus === 'failed') {
        window.history.replaceState({}, document.title, window.location.pathname);
        showToast('Discord login failed. Please try again.', true);
        return;
    }

    try {
        const res  = await fetch(`${API_BASE}/api/me`);
        const data = await res.json();
        if (data.logged_in) showDashboard(data);
    } catch (err) {
        console.error('Auth check error:', err);
    }
}

// ════════════════════════════════════════════════════════════════════
// SIDEBAR & NAVIGATION
// ════════════════════════════════════════════════════════════════════
function setupSidebar() {
    const sidebar     = document.getElementById('sidebar');
    const backdrop    = document.getElementById('navBackdrop');
    const toggle      = document.getElementById('navToggle');
    const collapseBtn = document.getElementById('sidebarCollapseBtn');
    const container   = document.getElementById('dashboard');

    const closeDrawer = () => {
        if (window.RealmNav) { window.RealmNav.close(); return; }
        sidebar?.classList.remove('is-open');
        backdrop?.classList.remove('is-open');
        document.body.classList.remove('drawer-open');
        document.body.style.overflow = '';
        toggle?.setAttribute('aria-expanded', 'false');
    };

    // layout.js already wires the hamburger, backdrop and collapse button.
    // Binding them a second time made every click run twice (open, then
    // immediately close), which is why the sidebar looked dead. Only bind a
    // control if nothing else has claimed it yet.
    if (toggle && backdrop && sidebar && !toggle.dataset.realmNavBound) {
        toggle.dataset.realmNavBound = '1';
        toggle.addEventListener('click', (e) => {
            e.preventDefault();
            const willOpen = !sidebar.classList.contains('is-open');
            sidebar.classList.toggle('is-open', willOpen);
            backdrop.classList.toggle('is-open', willOpen);
            document.body.classList.toggle('drawer-open', willOpen);
            document.body.style.overflow = willOpen ? 'hidden' : '';
            toggle.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
        });
    }
    if (backdrop && !backdrop.dataset.realmNavBound) {
        backdrop.dataset.realmNavBound = '1';
        backdrop.addEventListener('click', closeDrawer);
    }

    if (collapseBtn && sidebar && !collapseBtn.dataset.realmNavBound) {
        collapseBtn.dataset.realmNavBound = '1';
        collapseBtn.addEventListener('click', () => {
            // Desktop-only control
            if (window.matchMedia('(max-width: 1100px)').matches) return;
            sidebar.classList.toggle('is-collapsed');
            container?.classList.toggle('is-collapsed');
            document.body.classList.toggle('sidebar-is-collapsed');
            positionNavPill();
        });
    }

    // On the dashboard, these menu items are in-page views, not separate pages.
    const dashboardPages = ['overview', 'leaderboard', 'auction', 'admin'];
    const scrollTargets  = { leaderboard: '.leaderboard-panel', auction: '.auction-panel', admin: '#adminPanel' };
    const onDashboard = document.body.dataset.page === 'overview' || !!document.getElementById('dashboard-app');

    document.querySelectorAll('.nav-links a').forEach(link => {
        if (link.dataset.dashNavBound) return;
        link.dataset.dashNavBound = '1';
        link.addEventListener('click', (e) => {
            const page = link.dataset.page || 'overview';
            if (onDashboard && dashboardPages.includes(page)) {
                e.preventDefault();
                navigateTo(page);
                closeDrawer();
                const sel = scrollTargets[page];
                // wait a frame so the drawer's scroll lock is released first
                requestAnimationFrame(() => requestAnimationFrame(() => {
                    const el = sel ? document.querySelector(sel) : null;
                    if (el && el.offsetParent !== null) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
                    else window.scrollTo({ top: 0, behavior: 'smooth' });
                }));
                return;
            }
            closeDrawer();
        });
    });

    // Rotating to desktop must not leave the drawer "open"
    window.addEventListener('resize', () => {
        if (window.matchMedia('(min-width: 1101px)').matches) closeDrawer();
    });
}

function positionNavPill() {
    // Skip on mobile/tablet — the sidebar is an off-canvas overlay there
    // and its rect is zero-width while translated off-screen.
    if (window.matchMedia('(max-width: 1100px)').matches) return;
    // Also skip while the sidebar is collapsed (icons only)
    if (document.getElementById('sidebar')?.classList.contains('is-collapsed')) return;

    const pill       = document.getElementById('navActivePill');
    const activeLink = document.querySelector('.nav-links a.active');
    const list       = document.getElementById('navLinksList');
    if (!pill || !activeLink || !list) return;

    const listRect = list.getBoundingClientRect();
    const linkRect = activeLink.getBoundingClientRect();
    pill.style.height = linkRect.height + 'px';
    pill.style.top    = (linkRect.top - listRect.top) + 'px';
    pill.classList.add('is-active');
}

window.addEventListener('resize', positionNavPill);

function navigateTo(page) {
    const config = pageConfig[page] || pageConfig['overview'];

    document.querySelectorAll('.panel').forEach(p => p.classList.add('hidden'));

    config.panels.forEach(cls => {
        const el = document.querySelector(`.${cls}`);
        if (el) el.classList.remove('hidden');
    });

    Object.keys(config.titles).forEach(cls => {
        const panel = document.querySelector(`.${cls}`);
        if (!panel) return;
        const span = panel.querySelector('.panel-header span');
        if (span) span.innerHTML = config.titles[cls];
    });

    const adminPanel = document.getElementById('adminPanel');
    if (adminPanel) {
        if (currentUser?.is_admin) {
            adminPanel.classList.add('visible');
            adminPanel.classList.remove('hidden');
        } else {
            adminPanel.classList.remove('visible');
        }
    }

    document.querySelectorAll('.nav-links a').forEach(a => {
        a.classList.toggle('active', a.dataset.page === page);
    });
    positionNavPill();
}

// ════════════════════════════════════════════════════════════════════
// ADMIN TAB SWITCHING
// ════════════════════════════════════════════════════════════════════
function switchAdminTab(tab) {
    document.querySelectorAll('.admin-btn').forEach(b => b.classList.remove('active-tab'));

    document.getElementById('adminUsersPanel').style.display = 'none';
    document.getElementById('adminPlayersPanel').style.display = 'none';
    document.getElementById('adminActionsPanel').style.display = 'none';

    if (tab === 'users') {
        document.getElementById('adminTabUsers').classList.add('active-tab');
        document.getElementById('adminUsersPanel').style.display = 'block';
        refreshAdminPanel();
    } else if (tab === 'players') {
        document.getElementById('adminTabPlayers').classList.add('active-tab');
        document.getElementById('adminPlayersPanel').style.display = 'block';
        refreshAdminPlayers();
    } else if (tab === 'actions') {
        document.getElementById('adminTabActions').classList.add('active-tab');
        document.getElementById('adminActionsPanel').style.display = 'block';
    }
}

// ════════════════════════════════════════════════════════════════════
// NOTIFICATIONS
// ════════════════════════════════════════════════════════════════════
function setupNotifications() {
    const bell = document.getElementById('notifBell');
    if (!bell) return;

    bell.addEventListener('click', (e) => {
        e.stopPropagation();
        document.getElementById('notifPanel')?.classList.toggle('is-open');
        markAllNotificationsRead();
    });
    document.addEventListener('click', () => {
        document.getElementById('notifPanel')?.classList.remove('is-open');
    });
    document.getElementById('notifPanel')?.addEventListener('click', e => e.stopPropagation());
    document.getElementById('notifClearBtn')?.addEventListener('click', () => {
        notifications = [];
        renderNotifications();
    });

    addNotification('gift', 'Daily reward available', 'Claim your daily login bonus.');
}

function addNotification(type, title, desc) {
    notifications.unshift({
        id: Date.now() + Math.random(),
        type, title, desc,
        time: new Date(),
        read: false
    });
    notifications = notifications.slice(0, 30);
    renderNotifications();
}

function markAllNotificationsRead() {
    notifications.forEach(n => n.read = true);
    renderNotifications();
}

function renderNotifications() {
    const bell = document.getElementById('notifBell');
    const list = document.getElementById('notifList');
    if (!bell || !list) return;

    const hasUnread = notifications.some(n => !n.read);
    bell.classList.toggle('has-unread', hasUnread);

    if (notifications.length === 0) {
        list.innerHTML = '<div class="notif-empty">You\'re all caught up.</div>';
        return;
    }

    const icons = {
        gift: 'fa-gift', level: 'fa-star', quest: 'fa-scroll',
        auction: 'fa-gavel', guild: 'fa-users', battle: 'fa-khanda'
    };
    const colors = {
        gift: 'var(--gold)', level: 'var(--dash-teal)', quest: 'var(--dash-purple)',
        auction: 'var(--gold)', guild: 'var(--dash-emerald)', battle: 'var(--dash-red)'
    };

    list.innerHTML = notifications.map(n => `
        <div class="notif-item ${n.read ? '' : 'unread'}">
            <i class="fa-solid ${icons[n.type] || 'fa-bell'}" style="color:${colors[n.type] || 'var(--dash-teal)'}"></i>
            <div>
                <div class="notif-item-title">${escapeHTML(n.title)}</div>
                <div class="notif-item-desc">${escapeHTML(n.desc)}</div>
                <div class="notif-item-time">${timeAgo(n.time)}</div>
            </div>
        </div>`).join('');
}

function timeAgo(date) {
    const secs = Math.floor((Date.now() - new Date(date).getTime()) / 1000);
    if (secs < 60) return 'Just now';
    if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
    if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
    return `${Math.floor(secs / 86400)}d ago`;
}

// ════════════════════════════════════════════════════════════════════
// SHOW DASHBOARD
// ════════════════════════════════════════════════════════════════════
function showDashboard(user) {
    if (isShowingDashboard) return;
    isShowingDashboard = true;

    try {
        if (!user) { isShowingDashboard = false; return; }
        currentUser = user;

        document.getElementById('auth-screen').style.display   = 'none';
        document.getElementById('dashboard-app').style.display = 'block';

        const name = user.username || 'Adventurer';
        setEl('user-name-display', name);
        setEl('dash-name', name);
        setEl('dash-level', user.level || 1);
        setEl('dash-class-display', user.char_class || 'Explorer');
        setEl('dash-class', `Level ${user.level || 1} · ${user.char_class || 'Explorer'}`);
        const chip = document.getElementById('dash-level-chip');
        if (chip) chip.textContent = user.level || 1;

        setEl('char-guild', user.guild || 'Unguilded');
        setEl('char-reputation', user.reputation || 'Neutral');
        setEl('char-location', user.location || 'Eldoria');
        setEl('char-power', (user.combat_power || 0).toLocaleString());

        setEl('char-atk', user.attack || 0);
        setEl('char-def', user.defense || 0);
        setEl('char-mag', user.magic || 0);
        setEl('char-spd', user.speed || 0);
        setEl('char-lck', user.luck || 0);
        setEl('char-crit', (user.critical_chance || 0) + '%');

        initSim(user);   // placeholder only; the real numbers come from /api/game/state

        setEl('stat-gold', (user.gold || 0).toLocaleString());
        setEl('stat-gems', (user.gems || 0).toLocaleString());
        setEl('stat-rank', '#' + (user.rank || 0));
        setEl('stat-wins', user.battles_won || 0);
        setEl('stat-losses', user.battles_lost || 0);
        setEl('stat-playtime', user.playtime || '0h');

        const balance = document.getElementById('auction-balance');
        if (balance) balance.innerHTML = (user.gold || 0).toLocaleString() + ' <span style="font-size:0.7rem;color:#333;">Gold</span>';

        const xpPct = user.xp_pct || 0;
        const xpBar = document.getElementById('xp-bar');
        if (xpBar) { xpBar.style.width = xpPct + '%'; }
        setEl('xp-text', `${user.xp || 0} / ${user.xp_needed || 100}`);

        if (user.avatar) {
            const av = document.getElementById('user-avatar');
            if (av) av.src = user.avatar;
            const sidebarAv = document.getElementById('sidebar-avatar');
            if (sidebarAv) sidebarAv.src = user.avatar;
        }
        setEl('sidebar-username', name);
        setEl('sidebar-userrole', `Level ${user.level || 1}`);

        const hp = Number(user.hp), maxHp = Number(user.max_hp);
        if (Number.isFinite(hp) && maxHp > 0) {
            const hpBar = document.getElementById('hp-bar');
            if (hpBar) hpBar.style.width = Math.min(100, (hp / maxHp) * 100) + '%';
            setEl('hp-text', `${hp} / ${maxHp}`);
        }
        const mp = Number(user.mp), maxMp = Number(user.max_mp);
        if (Number.isFinite(mp) && maxMp > 0) {
            const mpBar = document.getElementById('mp-bar');
            if (mpBar) mpBar.style.width = Math.min(100, (mp / maxMp) * 100) + '%';
            setEl('mp-text', `${mp} / ${maxMp}`);
        }

        applySim(true);
        syncGameState(true);
        spawnCharParticles();
        loadCustomProfile().catch(console.error);

        if (!isDashboardInitialized) {
            isDashboardInitialized = true;
            if (user.discord_id) {
                loadLeaderboard();
                loadInventory(user.discord_id);
                loadAuctions();
            }
        }

        if (user.is_admin) {
            document.getElementById('adminPanel')?.classList.add('visible');
            setTimeout(refreshAdminPanel, 600);
        } else {
            document.getElementById('adminPanel')?.classList.remove('visible');
        }

        setTimeout(() => navigateTo('overview'), 280);
        setTimeout(maybeShowBotIntro, 700);

    } catch (err) {
        console.error('showDashboard error:', err);
    } finally {
        setTimeout(() => { isShowingDashboard = false; }, 100);
    }
}

async function loadCustomProfile() {
    const res  = await fetch(`${API_BASE}/api/profile`);
    const data = await res.json();
    if (!data.logged_in) return;

    if (data.avatar) {
        const el = document.getElementById('user-avatar');
        if (el && el.src !== data.avatar) el.src = data.avatar;
        const sidebarAv = document.getElementById('sidebar-avatar');
        if (sidebarAv && sidebarAv.src !== data.avatar) sidebarAv.src = data.avatar;
    }
    if (data.nickname) {
        document.querySelectorAll('#dash-name, #user-name-display, #sidebar-username').forEach(el => {
            if (el.textContent !== data.nickname) el.textContent = data.nickname;
        });
    }
}

// ════════════════════════════════════════════════════════════════════
// LOGOUT
// ════════════════════════════════════════════════════════════════════
async function logout() {
    try { await fetch('/logout'); } catch (e) { console.error(e); }

    document.getElementById('dashboard-app').style.display  = 'none';
    document.getElementById('auth-screen').style.display = 'flex';
    document.getElementById('login-form').reset();
    document.getElementById('register-form').reset();
    document.getElementById('forgot-form').reset();
    document.getElementById('otp-form').reset();
    document.querySelectorAll('.otp-input').forEach(i => i.value = '');
    updatePasswordStrength('');
    updatePasswordStrength('', 'otp');
    switchTab('login');
    isDashboardInitialized = false;
    isShowingDashboard     = false;
    currentUser            = null;
    showToast('Logged out successfully');
}

// ════════════════════════════════════════════════════════════════════
// AUTH FUNCTIONS
// ════════════════════════════════════════════════════════════════════
function switchTab(tab) {
    document.querySelectorAll('.auth-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.auth-form').forEach(f => f.classList.remove('active'));
    if (tab === 'login') {
        document.querySelector('.auth-tab:first-child').classList.add('active');
        document.getElementById('login-form').classList.add('active');
    } else {
        document.querySelector('.auth-tab:last-child').classList.add('active');
        document.getElementById('register-form').classList.add('active');
    }
}

function togglePassword(inputId) {
    const input = document.getElementById(inputId);
    const icon  = input.parentElement.querySelector('.password-toggle i');
    if (input.type === 'password') {
        input.type = 'text';
        icon.classList.replace('fa-eye', 'fa-eye-slash');
    } else {
        input.type = 'password';
        icon.classList.replace('fa-eye-slash', 'fa-eye');
    }
}

function getPasswordStrength(password) {
    if (!password) return { score: 0, label: '', color: '#666', hint: '' };
    const hasLower  = /[a-z]/.test(password);
    const hasUpper  = /[A-Z]/.test(password);
    const hasNumber = /[0-9]/.test(password);
    const hasSymbol = /[^a-zA-Z0-9]/.test(password);
    const variety   = [hasLower, hasUpper, hasNumber, hasSymbol].filter(Boolean).length;
    let score = 0;
    if (password.length >= 8)  score++;
    if (password.length >= 12) score++;
    if (variety >= 3) score++;
    if (variety === 4 && password.length >= 12) score++;
    score = Math.min(4, score);
    const levels = [
        { label: 'Very Weak',    color: '#ef4444' },
        { label: 'Weak',         color: '#f97316' },
        { label: 'Fair',         color: '#f59e0b' },
        { label: 'Strong',       color: '#22c55e' },
        { label: 'Very Strong',  color: '#00f3ff' }
    ];
    const hint = password.length < 8 ? 'Use at least 8 characters'
               : variety < 3         ? 'Add numbers, symbols, or uppercase'
               : password.length < 12? 'Longer passwords are stronger'
               : 'Great password!';
    return { score, label: levels[score].label, color: levels[score].color, hint };
}

function updatePasswordStrength(password, suffix = '') {
    const tag  = suffix ? `-${suffix}` : '';
    const wrap  = document.getElementById(`pw-strength-wrap${tag}`);
    const fill  = document.getElementById(`pw-strength-fill${tag}`);
    const label = document.getElementById(`pw-strength-label${tag}`);
    const hint  = document.getElementById(`pw-strength-hint${tag}`);
    if (!wrap || !fill || !label || !hint) return;
    if (!password) { wrap.style.display = 'none'; return; }
    wrap.style.display = 'block';
    const result = getPasswordStrength(password);
    fill.style.width           = (result.score / 4 * 100) + '%';
    fill.style.backgroundColor = result.color;
    label.textContent          = result.label;
    label.style.color          = result.color;
    hint.textContent           = result.hint;
}

async function handleLogin(event) {
    event.preventDefault();
    const email    = document.getElementById('login-email').value.trim();
    const password = document.getElementById('login-pass').value;
    if (!email || !password) { showToast('Please fill in all fields', true); return; }

    const btn = document.getElementById('login-btn');
    btn.disabled = true; btn.textContent = 'VERIFYING...';

    try {
        const res  = await fetch(`${API_BASE}/api/login`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email, password })
        });
        const data = await res.json();
        if (data.ok && data.logged_in) {
            showToast('Welcome back, ' + (data.user?.username || 'Adventurer') + '!');
            showDashboard(data.user);
        } else {
            showToast(data.error || 'Login failed. Check your credentials.', true);
        }
    } catch (err) {
        console.error(err);
        showToast('Cannot reach server. Check your connection.', true);
    } finally {
        btn.disabled = false; btn.textContent = 'SIGN IN';
    }
}

async function handleRegister(event) {
    event.preventDefault();
    const username    = document.getElementById('reg-username')?.value.trim() || '';
    const email       = document.getElementById('reg-email').value.trim();
    const pass        = document.getElementById('reg-pass').value;
    const confirmPass = document.getElementById('reg-confirm-pass').value;
    const btn         = document.getElementById('register-btn');

    if (pass !== confirmPass) { showToast('Passwords do not match!', true); return; }
    if (getPasswordStrength(pass).score < 2) { showToast('Please choose a stronger password.', true); return; }

    btn.disabled = true; btn.textContent = 'VERIFYING SECURITY...';

    const submit = async (token) => {
        try {
            const res  = await fetch(`${API_BASE}/api/signup`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ email, nickname: username, password: pass, recaptcha_token: token })
            });
            const data = await res.json();
            if (data.ok && data.logged_in) {
                showToast('Account created! Welcome, ' + (data.user?.username || 'Adventurer') + '!');
                showDashboard(data.user);
            } else {
                showToast(data.error || 'Registration failed.', true);
            }
        } catch (err) {
            showToast('Cannot reach server.', true);
        } finally {
            btn.disabled = false; btn.textContent = 'CREATE ACCOUNT';
        }
    };

    if (typeof grecaptcha !== 'undefined' && RECAPTCHA_SITE_KEY) {
        grecaptcha.ready(() => {
            grecaptcha.execute(RECAPTCHA_SITE_KEY, { action: 'register' })
                .then(submit).catch(() => submit(null));
        });
    } else {
        submit(null);
    }
}

function showForgotPasswordForm() {
    document.querySelectorAll('.auth-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.auth-form').forEach(f => f.classList.remove('active'));
    document.getElementById('forgot-form').classList.add('active');
    document.getElementById('forgot-email').focus();
}

function showOTPForm(email) {
    document.querySelectorAll('.auth-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.auth-form').forEach(f => f.classList.remove('active'));
    document.getElementById('otp-form').classList.add('active');
    document.getElementById('otp-email-display').textContent = email;
    document.querySelectorAll('.otp-input')[0].focus();
}

async function handleForgotPassword(event) {
    event.preventDefault();
    const email = document.getElementById('forgot-email').value.trim();
    const btn   = document.getElementById('forgot-btn');
    if (!email) { showToast('Please enter your email address.', true); return; }
    btn.disabled = true; btn.textContent = 'SENDING...';

    try {
        const res  = await fetch(`${API_BASE}/api/password/request-reset`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email })
        });
        const data = await res.json();

        if (data.ok) {
            // The server generates the OTP and sends it through EmailJS.
            resetEmail = email;
            showOTPForm(email);
            startResendCooldown(60);
            showToast('If that email is registered, a reset code is on its way.');
        } else {
            showToast(data.error || 'Could not request a reset code.', true);
        }
    } catch (err) {
        showToast(err.message || 'Cannot reach server.', true);
    } finally {
        btn.disabled = false; btn.textContent = 'SEND RESET CODE';
    }
}

async function handleOTPSubmit(event) {
    event.preventDefault();
    let otp = '';
    document.querySelectorAll('.otp-input').forEach(i => otp += i.value);
    const newPassword = document.getElementById('otp-new-pass').value;
    const btn         = document.getElementById('otp-btn');

    if (otp.length !== 6)        { showToast('Please enter the complete 6-digit code.', true); return; }
    if (!newPassword || newPassword.length < 6) { showToast('Password must be at least 6 characters.', true); return; }

    btn.disabled = true; btn.textContent = 'VERIFYING...';

    try {
        const res  = await fetch(`${API_BASE}/api/password/verify-reset`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email: resetEmail, otp, new_password: newPassword })
        });
        const data = await res.json();
        if (data.ok) {
            showToast('Password updated successfully!');
            document.getElementById('otp-form').reset();
            document.querySelectorAll('.otp-input').forEach(i => i.value = '');
            updatePasswordStrength('', 'otp');
            switchTab('login');
        } else {
            showToast(data.error || 'Invalid or expired code.', true);
        }
    } catch (err) {
        showToast(err.message || 'Cannot reach server.', true);
    } finally {
        btn.disabled = false; btn.textContent = 'RESET PASSWORD';
    }
}

function startResendCooldown(seconds) {
    const link = document.getElementById('resend-otp');
    if (!link) return;
    clearInterval(resendCooldownTimer);
    let left = seconds;
    link.style.pointerEvents = 'none';
    link.style.opacity = '0.5';
    link.textContent = `Resend (${left}s)`;
    resendCooldownTimer = setInterval(() => {
        left--;
        if (left <= 0) {
            clearInterval(resendCooldownTimer);
            link.style.pointerEvents = '';
            link.style.opacity = '';
            link.textContent = 'Resend';
        } else {
            link.textContent = `Resend (${left}s)`;
        }
    }, 1000);
}

async function resendOTP() {
    if (!resetEmail) {
        showToast('Enter your email first.', true);
        showForgotPasswordForm();
        return;
    }
    showToast('Requesting new code...');
    try {
        const res  = await fetch(`${API_BASE}/api/password/request-reset`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email: resetEmail })
        });
        const data = await res.json();
        if (data.ok) {
            showToast('A new code has been sent.');
            startResendCooldown(60);
        } else {
            showToast(data.error || 'Could not resend the code.', true);
        }
    } catch (err) {
        showToast('Cannot reach server.', true);
    }
}

function setupOTPInputs() {
    const inputs = document.querySelectorAll('.otp-input');
    inputs.forEach((input, idx) => {
        input.addEventListener('input', e => {
            e.target.value = e.target.value.replace(/[^0-9]/g, '');
            if (e.target.value.length === 1 && idx < inputs.length - 1) inputs[idx + 1].focus();
        });
        input.addEventListener('keydown', e => {
            if (e.key === 'Backspace' && !e.target.value && idx > 0) inputs[idx - 1].focus();
            if (e.key === 'Enter') document.getElementById('otp-btn')?.click();
        });
    });
}

function setupDiscordButtons() {
    document.querySelectorAll('.social-btn.discord').forEach(btn => {
        btn.addEventListener('click', e => {
            e.preventDefault();
            showToast('Redirecting to Discord...');
            setTimeout(() => { window.location.href = '/login/discord'; }, 500);
        });
    });
}

// ════════════════════════════════════════════════════════════════════
// DATA LOADERS
// ════════════════════════════════════════════════════════════════════
async function loadLeaderboard() {
    try {
        const res     = await fetch(`${API_BASE}/api/game/leaderboard`);
        const data    = await res.json();
        const players = data.leaderboard || [];

        const podium = document.getElementById('lb-podium');
        const tbody  = document.getElementById('lb-tbody');
        if (!podium || !tbody) return;

        podium.innerHTML = '';
        if (players[1]) podium.innerHTML += createPodiumHTML(players[1], 'second', '🥈');
        if (players[0]) podium.innerHTML += createPodiumHTML(players[0], 'first', '👑');
        if (players[2]) podium.innerHTML += createPodiumHTML(players[2], 'third', '🥉');

        tbody.innerHTML = '';
        players.slice(0, 10).forEach((p, i) => {
            const rankCls = i === 0 ? 'top1' : i === 1 ? 'top2' : i === 2 ? 'top3' : '';
            const meCls   = currentUser && p.id == currentUser.discord_id ? 'lb-you' : '';
            const safeName  = escapeHTML(p.name);
            const safeClass = escapeHTML(p.class);
            const safeSeed  = encodeURIComponent(p.name || '');
            tbody.innerHTML += `
                <tr class="${meCls}">
                    <td><span class="lb-rank ${rankCls}">#${i + 1}</span></td>
                    <td><img src="https://api.dicebear.com/7.x/avataaars/svg?seed=${safeSeed}&backgroundColor=b6e3f4" class="lb-avatar">
                        <span class="lb-name">${safeName}</span></td>
                    <td><span class="lb-class">${safeClass}</span></td>
                    <td style="text-align:right"><span class="lb-score">${(p.xp || 0).toLocaleString()} XP</span></td>
                </tr>`;
        });
    } catch (e) { console.error('Leaderboard error:', e); }
}

function createPodiumHTML(player, rankClass, icon) {
    const colorClass = rankClass === 'first' ? 'gold' : rankClass === 'second' ? 'silver' : 'bronze';
    const safeName = escapeHTML(player.name);
    const safeSeed = encodeURIComponent(player.name || '');
    return `
        <div class="podium-item ${rankClass}">
            <img src="https://api.dicebear.com/7.x/avataaars/svg?seed=${safeSeed}&backgroundColor=b6e3f4" class="podium-avatar" alt="">
            <div class="podium-rank ${colorClass}">${icon}</div>
            <div class="podium-name">${safeName}</div>
            <div class="podium-score">${(player.xp || 0).toLocaleString()} XP</div>
        </div>`;
}

const tooltipEls = {
    root: null, name: null, desc: null, stat: null,
    init() {
        this.root = document.getElementById('tooltip');
        this.name = document.getElementById('tt-name');
        this.desc = document.getElementById('tt-desc');
        this.stat = document.getElementById('tt-stat');
        return !!(this.root && this.name && this.desc && this.stat);
    }
};

async function loadInventory(userId) {
    try {
        const res   = await fetch(`${API_BASE}/api/game/inventory`, { credentials: 'same-origin' });
        const data  = await res.json();
        const items = data.items || [];
        const cont  = document.getElementById('inventory-container');
        if (!cont) return;

        cachedInventoryItems = items;
        const countEl = document.getElementById('inventory-count');
        if (countEl) countEl.textContent = items.length + ' items';

        cont.innerHTML = '';
        if (items.length === 0) {
            cont.innerHTML = '<p style="grid-column:1/-1;text-align:center;color:#333;padding:20px;">Inventory is empty.</p>';
            return;
        }

        if (!tooltipEls.root) tooltipEls.init();

        items.slice(0, 8).forEach(item => {
            const rarity   = (item.rarity || 'common').toLowerCase();
            const icon     = getItemIcon(item.name);
            const safeName = escapeHTML(item.name);
            const qty      = Number(item.quantity) || 0;
            const slot     = document.createElement('div');
            slot.className = `inv-slot ${rarity}`;
            slot.dataset.label = safeName;
            slot.innerHTML = `<i class="fa-solid ${icon}"></i>${qty > 1 ? `<span class="item-count">${qty}</span>` : ''}`;

            // Desktop hover tooltip
            slot.addEventListener('mousemove', e => {
                if (window.matchMedia('(hover: none)').matches) return;
                if (!tooltipEls.root && !tooltipEls.init()) return;
                tooltipEls.name.textContent = safeName;
                tooltipEls.desc.textContent = rarity.charAt(0).toUpperCase() + rarity.slice(1) + ' Item';
                tooltipEls.stat.textContent = `Qty: ${qty}`;
                tooltipEls.root.style.opacity = '1';
                tooltipEls.root.style.left    = Math.min(e.clientX + 15, window.innerWidth - 220) + 'px';
                tooltipEls.root.style.top     = Math.min(e.clientY + 15, window.innerHeight - 100) + 'px';
            });
            slot.addEventListener('mouseleave', () => {
                if (tooltipEls.root) tooltipEls.root.style.opacity = '0';
            });

            // Touch: tap to show tooltip
            slot.addEventListener('click', e => {
                if (!window.matchMedia('(hover: none)').matches) return;
                e.stopPropagation();
                if (!tooltipEls.root && !tooltipEls.init()) return;
                const r = slot.getBoundingClientRect();
                tooltipEls.name.textContent = safeName;
                tooltipEls.desc.textContent = rarity.charAt(0).toUpperCase() + rarity.slice(1) + ' Item';
                tooltipEls.stat.textContent = `Qty: ${qty}`;
                tooltipEls.root.style.left = Math.min(Math.max(8, r.left), window.innerWidth - 220) + 'px';
                tooltipEls.root.style.top  = Math.max(8, r.top - 110) + 'px';
                tooltipEls.root.style.opacity = '1';
            });

            cont.appendChild(slot);
        });

        // Dismiss on outside tap (mobile) - bound once, loadInventory can now run many times
        if (!window.__invDismissBound) document.addEventListener('click', e => {
            if (tooltipEls.root && !tooltipEls.root.contains(e.target)) {
                tooltipEls.root.style.opacity = '0';
            }
        }, { passive: true });
        window.__invDismissBound = true;

    } catch (e) { console.error('Inventory error:', e); }
}

function getItemIcon(name) {
    const n = (name || '').toLowerCase();
    if (n.includes('sword') || n.includes('blade') || n.includes('weapon')) return 'fa-khanda';
    if (n.includes('shield') || n.includes('armor'))  return 'fa-shield-halved';
    if (n.includes('potion') || n.includes('elixir')) return 'fa-flask';
    if (n.includes('gem')    || n.includes('crystal'))return 'fa-gem';
    if (n.includes('scroll') || n.includes('map'))    return 'fa-scroll';
    if (n.includes('coin')   || n.includes('gold'))   return 'fa-coins';
    return 'fa-box';
}

async function loadAuctions() {
    try {
        const res      = await fetch(`${API_BASE}/api/auctions`);
        const data     = await res.json();
        const auctions = data.auctions || [];
        const cont     = document.getElementById('auction-items');
        if (!cont) return;

        cont.innerHTML = '';
        if (auctions.length === 0) {
            cont.innerHTML = '<p style="text-align:center;color:#333;padding:20px;">No active auctions.</p>';
            return;
        }

        const listed = document.getElementById('items-listed');
        if (listed) listed.textContent = auctions.length;

        auctions.slice(0, 4).forEach(a => {
            const col = a.rarity === 'legendary' ? 'var(--rarity-legendary)'
                      : a.rarity === 'epic'       ? 'var(--rarity-epic)'
                      : a.rarity === 'rare'        ? 'var(--rarity-rare)'
                      : '#22c55e';
            const safeName   = escapeHTML(a.name);
            const safeSeller = escapeHTML(a.seller || 'Unknown');
            const div = document.createElement('div');
            div.className = 'auction-item';
            div.innerHTML = `
                <div class="auction-item-icon" style="background:${col}15;color:${col}">
                    <i class="fa-solid fa-gavel"></i>
                </div>
                <div class="auction-item-info">
                    <div class="auction-item-name" style="color:${col}">${safeName}</div>
                    <div class="auction-item-desc">Seller: ${safeSeller}</div>
                </div>
                <div class="auction-item-price">💰 ${(a.current_bid || 0).toLocaleString()} Gold</div>
                <button class="btn-bid" type="button">Bid</button>`;
            div.addEventListener('click', () => placeBid(a.name));
            div.querySelector('.btn-bid').addEventListener('click', (e) => {
                e.stopPropagation();
                placeBid(a.name);
            });
            cont.appendChild(div);
        });
    } catch (e) { console.error('Auctions error:', e); }
}

// ════════════════════════════════════════════════════════════════════
// LIST ITEM
// ════════════════════════════════════════════════════════════════════
function setupListItemModal() {
    const modal = document.getElementById('listItemModal');
    if (!modal) return;

    modal.addEventListener('click', (e) => { if (e.target === modal) closeListItemModal(); });
    document.getElementById('listItemClose')?.addEventListener('click', closeListItemModal);
    document.getElementById('listItemCancel')?.addEventListener('click', closeListItemModal);
    document.getElementById('listItemForm')?.addEventListener('submit', handleListItemSubmit);
}

function openListItemModal() {
    const select = document.getElementById('listItemSelect');
    if (select) {
        if (cachedInventoryItems.length === 0) {
            select.innerHTML = '<option value="">No items available</option>';
        } else {
            select.innerHTML = cachedInventoryItems.map((item, i) =>
                `<option value="${i}">${escapeHTML(item.name)} (x${Number(item.quantity) || 1})</option>`
            ).join('');
        }
    }
    document.getElementById('listItemModal')?.classList.add('is-open');
}

function closeListItemModal() {
    document.getElementById('listItemModal')?.classList.remove('is-open');
    document.getElementById('listItemForm')?.reset();
}

async function handleListItemSubmit(event) {
    event.preventDefault();
    const select   = document.getElementById('listItemSelect');
    const bidInput = document.getElementById('listItemStartBid');
    const duration = document.getElementById('listItemDuration');
    const submitBtn = document.getElementById('listItemSubmit');

    const idx = select?.value;
    const item = cachedInventoryItems[idx];
    const startBid = parseInt(bidInput?.value, 10);

    if (!item) { showToast('Please select an item to list.', true); return; }
    if (!startBid || startBid < 1) { showToast('Enter a valid starting bid.', true); return; }

    submitBtn.disabled = true;
    submitBtn.innerHTML = 'Listing...';

    try {
        const res  = await fetch(`${API_BASE}/api/auctions`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                item_name: item.name,
                starting_bid: startBid,
                duration_hours: parseInt(duration?.value, 10) || 24
            })
        });
        const data = await res.json();
        if (data.ok) {
            showToast(`${item.name} listed on the Auction House!`);
            addNotification('auction', 'Item listed', `${item.name} is now up for auction.`);
            closeListItemModal();
            loadAuctions();
        } else {
            showToast(data.error || 'Failed to list item.', true);
        }
    } catch (err) {
        console.error('List item error:', err);
        showToast('Cannot reach server to list item.', true);
    } finally {
        submitBtn.disabled = false;
        submitBtn.innerHTML = '<i class="fa-solid fa-gavel"></i> List Item';
    }
}

// ════════════════════════════════════════════════════════════════════
// ADMIN PANEL
// ════════════════════════════════════════════════════════════════════
async function refreshAdminPanel() {
    try {
        const [usersRes, statsRes] = await Promise.all([
            fetch(`${API_BASE}/api/admin/users`),
            fetch(`${API_BASE}/api/admin/stats`)
        ]);
        const usersData = await usersRes.json();
        const statsData = await statsRes.json();

        if (usersData.ok) {
            setEl('adminTotalUsers',  usersData.stats.total);
            setEl('adminOnlineUsers', usersData.stats.online);
            setEl('adminTotalAdmins', usersData.stats.admins);

            const tbody = document.getElementById('adminUsersTbody');
            if (tbody) {
                tbody.innerHTML = '';
                usersData.users.forEach(user => {
                    const isYou = user.id == currentUser?.discord_id;
                    const tr    = document.createElement('tr');
                    if (isYou) tr.className = 'lb-you';
                    const safeNickname = escapeHTML(user.nickname);
                    const safeEmail    = escapeHTML(user.email);
                    const safeAvatar   = escapeHTML(user.avatar || '');
                    tr.innerHTML = `
                        <td><span style="font-family:'Orbitron',sans-serif;font-size:0.7rem;color:#444">#${escapeHTML(user.id)}</span></td>
                        <td><img src="${safeAvatar}" class="user-avatar"><span class="lb-name">${safeNickname}</span></td>
                        <td style="font-size:0.8rem;color:#666">${safeEmail}</td>
                        <td><span class="admin-role-badge ${user.is_admin ? 'admin' : 'user'}">${user.is_admin ? '👑 Admin' : 'User'}</span></td>
                        <td><span class="${user.is_online ? 'admin-status-online' : 'admin-status-offline'}">${user.is_online ? '🟢 Online' : '⚫ Offline'}</span></td>
                        <td style="font-size:0.7rem;color:#444">${new Date(user.created_at).toLocaleDateString()}</td>
                        <td style="text-align:center">${!isYou ? `
                            <button onclick="toggleAdmin(${user.id})" class="admin-action-btn ${user.is_admin ? 'warning' : ''}">${user.is_admin ? '👑' : '⭐'}</button>
                            <button onclick="deleteUser(${user.id})" class="admin-action-btn danger"><i class="fa-solid fa-trash"></i></button>
                        ` : `<span style="font-size:0.65rem;color:#444;padding:3px 10px;background:rgba(255,255,255,.03);border-radius:6px">You</span>`}</td>`;
                    tbody.appendChild(tr);
                });
            }
        }

        if (statsData.ok) setEl('adminActiveSessions', statsData.stats.active_sessions || 0);
    } catch (e) {
        console.error('Admin refresh error:', e);
        showToast('Failed to load admin data', true);
    }

    refreshAdminPlayers();
}

// ── Player Management ──────────────────────────────────────────────
let playerSearchDebounce = null;
let cachedAdminPlayers   = [];

function handlePlayerSearch() {
    clearTimeout(playerSearchDebounce);
    playerSearchDebounce = setTimeout(() => refreshAdminPlayers(), 300);
}

async function refreshAdminPlayers() {
    const tbody = document.getElementById('adminPlayersTbody');
    if (!tbody) return;

    const q = document.getElementById('playerSearchInput')?.value.trim() || '';

    try {
        const res  = await fetch(`${API_BASE}/api/admin/players?q=${encodeURIComponent(q)}`);
        const data = await res.json();

        if (!data.ok) {
            tbody.innerHTML = `<tr><td colspan="9" style="text-align:center; padding:20px; color:#666;">${escapeHTML(data.error || 'Failed to load players')}</td></tr>`;
            return;
        }

        cachedAdminPlayers = data.players;

        if (data.players.length === 0) {
            tbody.innerHTML = '<tr><td colspan="9" style="text-align:center; padding:20px; color:#666;">No players found.</td></tr>';
            return;
        }

        tbody.innerHTML = '';
        data.players.forEach(p => {
            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td><span style="font-family:'Orbitron',sans-serif;font-size:0.7rem;color:#444">#${escapeHTML(p.id)}</span></td>
                <td><span class="lb-name">${escapeHTML(p.name)}</span></td>
                <td>${escapeHTML(p.level)}</td>
                <td style="font-size:0.8rem;color:#666">${escapeHTML(p.class)}</td>
                <td style="color:var(--gold)">${Number(p.gold || 0).toLocaleString()}</td>
                <td>${Number(p.gems || 0).toLocaleString()}</td>
                <td>${Number(p.xp || 0).toLocaleString()}</td>
                <td style="font-size:0.8rem;color:#666">${escapeHTML(p.guild)}</td>
                <td style="text-align:center">
                    <button onclick="openEditPlayerModal('${escapeHTML(p.id)}')" class="admin-action-btn">
                        <i class="fa-solid fa-pen"></i>
                    </button>
                </td>`;
            tbody.appendChild(tr);
        });
    } catch (e) {
        console.error('Admin players refresh error:', e);
        tbody.innerHTML = '<tr><td colspan="9" style="text-align:center; padding:20px; color:#666;">Failed to load players.</td></tr>';
    }
}

let currentEditPlayerId = null;

async function openEditPlayerModal(userId) {
    currentEditPlayerId = userId;
    setEl('editPlayerIdLabel', `#${userId}`);

    try {
        const res  = await fetch(`${API_BASE}/api/admin/players/${userId}`);
        const data = await res.json();
        if (!data.ok) { showToast(data.error || 'Failed to load player', true); return; }

        const p = data.player || {};
        document.getElementById('editPlayerGold').value          = p.gold ?? 0;
        document.getElementById('editPlayerGems').value          = p.gems ?? p.Gems ?? 0;
        document.getElementById('editPlayerSpecialCoins').value  = p.special_coins ?? 0;
        document.getElementById('editPlayerLevel').value         = p.level ?? 1;
        document.getElementById('editPlayerXp').value            = p.xp ?? 0;
        document.getElementById('editPlayerWins').value          = p.battles_won ?? 0;

        const jsonBox = document.getElementById('editPlayerRawJson');
        jsonBox.value = JSON.stringify(p, null, 2);
        jsonBox.style.display = 'none';
        document.getElementById('advancedJsonChevron')?.classList.remove('fa-chevron-up');
        document.getElementById('advancedJsonChevron')?.classList.add('fa-chevron-down');

        document.getElementById('editPlayerModal')?.classList.add('is-open');
    } catch (e) {
        console.error('Open edit player error:', e);
        showToast('Failed to load player data', true);
    }
}

function closeEditPlayerModal() {
    document.getElementById('editPlayerModal')?.classList.remove('is-open');
    document.getElementById('editPlayerForm')?.reset();
    currentEditPlayerId = null;
}

function toggleAdvancedPlayerJSON(event) {
    event.preventDefault();
    const box = document.getElementById('editPlayerRawJson');
    const chevron = document.getElementById('advancedJsonChevron');
    const isHidden = box.style.display === 'none';
    box.style.display = isHidden ? 'block' : 'none';
    chevron?.classList.toggle('fa-chevron-down', !isHidden);
    chevron?.classList.toggle('fa-chevron-up', isHidden);
}

async function handleEditPlayerSubmit(event) {
    event.preventDefault();
    if (!currentEditPlayerId) return;

    const submitBtn = document.getElementById('editPlayerSubmit');
    submitBtn.disabled = true;
    submitBtn.innerHTML = 'Saving...';

    const updates = {
        gold: Number(document.getElementById('editPlayerGold').value) || 0,
        gems: Number(document.getElementById('editPlayerGems').value) || 0,
        special_coins: Number(document.getElementById('editPlayerSpecialCoins').value) || 0,
        level: Number(document.getElementById('editPlayerLevel').value) || 1,
        xp: Number(document.getElementById('editPlayerXp').value) || 0,
        battles_won: Number(document.getElementById('editPlayerWins').value) || 0,
    };

    const jsonBox = document.getElementById('editPlayerRawJson');
    if (jsonBox.style.display !== 'none' && jsonBox.value.trim()) {
        try {
            const parsed = JSON.parse(jsonBox.value);
            Object.assign(updates, parsed);
        } catch (e) {
            showToast('Advanced JSON is not valid — fix it or collapse that section.', true);
            submitBtn.disabled = false;
            submitBtn.innerHTML = '<i class="fa-solid fa-floppy-disk"></i> Save Changes';
            return;
        }
    }

    try {
        const res  = await fetch(`${API_BASE}/api/admin/players/${currentEditPlayerId}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ updates })
        });
        const data = await res.json();

        if (data.ok) {
            showToast(data.bot_synced
                ? 'Player updated and synced live to the bot!'
                : 'Player saved here, but the bot could not be reached — it will pick up the change on its next sync.');
            closeEditPlayerModal();
            refreshAdminPlayers();
        } else {
            showToast(data.error || 'Failed to update player', true);
        }
    } catch (e) {
        console.error('Save player error:', e);
        showToast('Request failed', true);
    } finally {
        submitBtn.disabled = false;
        submitBtn.innerHTML = '<i class="fa-solid fa-floppy-disk"></i> Save Changes';
    }
}

function setupEditPlayerModal() {
    const modal = document.getElementById('editPlayerModal');
    if (!modal) return;

    modal.addEventListener('click', (e) => { if (e.target === modal) closeEditPlayerModal(); });
    document.getElementById('editPlayerClose')?.addEventListener('click', closeEditPlayerModal);
    document.getElementById('editPlayerCancel')?.addEventListener('click', closeEditPlayerModal);
    document.getElementById('editPlayerForm')?.addEventListener('submit', handleEditPlayerSubmit);
}

async function toggleAdmin(userId) {
    if (!confirm('Change this user\'s admin status?')) return;
    try {
        const res  = await fetch(`${API_BASE}/api/admin/users/${userId}/toggle-admin`, { method: 'POST', headers: { 'Content-Type': 'application/json' } });
        const data = await res.json();
        if (data.ok) { showToast(data.message); await refreshAdminPanel(); }
        else showToast(data.error || 'Failed to toggle admin status', true);
    } catch { showToast('Server error', true); }
}

async function deleteUser(userId) {
    if (!confirm('⚠️ Delete this user permanently?')) return;
    if (!confirm('Really? All data will be destroyed.')) return;
    try {
        const res  = await fetch(`${API_BASE}/api/admin/users/${userId}`, { method: 'DELETE', headers: { 'Content-Type': 'application/json' } });
        const data = await res.json();
        if (data.ok) { showToast(data.message); await refreshAdminPanel(); }
        else showToast(data.error || 'Failed to delete user', true);
    } catch { showToast('Server error', true); }
}

// ════════════════════════════════════════════════════════════════════
// BACKGROUND MUSIC
// ════════════════════════════════════════════════════════════════════
const YT_MUSIC_VIDEO_ID = 'dQw4w9WgXcQ';
let musicEnabled  = localStorage.getItem('rpgGhostMusic') === 'on';
let ytPlayer      = null;
let ytPlayerReady = false;

function onYouTubeIframeAPIReady() {
    ytPlayer = new YT.Player('ytMusicPlayer', {
        height: '1',
        width: '1',
        videoId: YT_MUSIC_VIDEO_ID,
        playerVars: {
            autoplay: 0,
            controls: 0,
            disablekb: 1,
            fs: 0,
            modestbranding: 1,
            playsinline: 1
        },
        events: {
            onReady: () => {
                ytPlayerReady = true;
                ytPlayer.setVolume(100);
                const btn = document.getElementById('musicToggle');
                if (musicEnabled) tryPlayMusic(btn);
                else updateMusicButton(btn);
            },
            onStateChange: (e) => {
                if (e.data === YT.PlayerState.ENDED) {
                    ytPlayer.seekTo(0);
                    ytPlayer.playVideo();
                }
            },
            onError: (event) => { console.error("YT Error:", event.data); }
        }
    });
}

function setupMusicToggle() {
    const btn = document.getElementById('musicToggle');
    if (!btn) return;

    updateMusicButton(btn);

    btn.addEventListener('click', () => {
        musicEnabled = !musicEnabled;
        localStorage.setItem('rpgGhostMusic', musicEnabled ? 'on' : 'off');
        if (musicEnabled) tryPlayMusic(btn);
        else { ytPlayer?.pauseVideo(); updateMusicButton(btn); }
    });

    document.addEventListener('click', () => {
        if (musicEnabled && ytPlayerReady && ytPlayer?.getPlayerState() !== YT.PlayerState.PLAYING) {
            tryPlayMusic(btn);
        }
    }, { once: true });
}

function tryPlayMusic(btn) {
    if (!ytPlayerReady || !ytPlayer) return;
    ytPlayer.playVideo();
    updateMusicButton(btn);
}

function updateMusicButton(btn) {
    if (!btn) return;
    const icon = btn.querySelector('i');
    btn.classList.toggle('is-playing', musicEnabled);
    if (icon) icon.className = musicEnabled ? 'fa-solid fa-volume-high' : 'fa-solid fa-volume-xmark';
}

// ════════════════════════════════════════════════════════════════════
// BOT INTRO OVERLAY
// ════════════════════════════════════════════════════════════════════
function maybeShowBotIntro() {
    if (sessionStorage.getItem('botIntroSeen') === '1') return;
    sessionStorage.setItem('botIntroSeen', '1');
    launchBotIntro();
}

async function launchBotIntro() {
    const overlay  = document.getElementById('botIntroOverlay');
    const enterBtn = document.getElementById('botIntroEnter');
    const skipBtn  = document.getElementById('botIntroSkip');
    const descEl   = document.getElementById('botIntroDesc');
    if (!overlay || !descEl) return;

    const defaultDesc = "Hey there, Adventurer. I'm RPG Ghost — your guide through the spectral realm. Hunt monsters, loot rare gear, complete quests, trade in the auction house, and climb the leaderboard. Your journey through the spectral realm starts here.";

    overlay.classList.add('visible');

    await humanTypewriter(descEl, defaultDesc, () => enterBtn?.classList.add('ready'));

    const dismiss = () => {
        overlay.classList.remove('visible');
        setTimeout(() => overlay.style.display = 'none', 400);
    };

    enterBtn?.addEventListener('click', () => {
        const musicBtn = document.getElementById('musicToggle');
        if (musicEnabled && ytPlayerReady && ytPlayer?.getPlayerState() !== YT.PlayerState.PLAYING) {
            tryPlayMusic(musicBtn);
        }
        dismiss();
    });
    skipBtn?.addEventListener('click', dismiss);
}

function humanTypewriter(el, text, onDone) {
    return new Promise(resolve => {
        el.innerHTML = '<span class="tw-cursor"></span>';
        let i = 0;
        function next() {
            if (i >= text.length) {
                el.innerHTML = text + '<span class="tw-cursor"></span>';
                if (onDone) onDone();
                resolve();
                return;
            }
            const ch = text[i];
            el.innerHTML = text.slice(0, i + 1) + '<span class="tw-cursor"></span>';
            i++;
            let delay = 18 + Math.random() * 14;
            if (ch === '.' || ch === '!' || ch === '?') delay = 260 + Math.random() * 100;
            else if (ch === ',') delay = 110 + Math.random() * 50;
            else if (ch === ' ') delay = 14 + Math.random() * 16;
            setTimeout(next, delay);
        }
        next();
    });
}

// ════════════════════════════════════════════════════════════════════
// VISUAL HELPERS
// ════════════════════════════════════════════════════════════════════
function updateLevelRing(pct) {
    const ring = document.getElementById('levelRing');
    if (!ring) return;
    const circumference = 339;
    const offset = circumference - (circumference * Math.max(0, Math.min(100, pct)) / 100);
    requestAnimationFrame(() => { ring.style.strokeDashoffset = offset; });
}

function spawnCharParticles() {
    const cont = document.getElementById('charParticles');
    if (!cont || cont.dataset.seeded) return;
    cont.dataset.seeded = '1';
    for (let i = 0; i < 10; i++) {
        const p = document.createElement('span');
        p.className = 'char-particle';
        p.style.left            = (10 + Math.random() * 80) + '%';
        p.style.bottom          = '20px';
        p.style.setProperty('--drift', (Math.random() * 30 - 15) + 'px');
        p.style.animationDelay  = (Math.random() * 6) + 's';
        p.style.animationDuration = (5 + Math.random() * 3) + 's';
        cont.appendChild(p);
    }
}

function countUpTo(elId, target, duration = 900) {
    const el = document.getElementById(elId);
    if (!el || !target) { if (el) el.textContent = '0'; return; }
    const start = performance.now();
    (function tick(now) {
        const p     = Math.min((now - start) / duration, 1);
        const eased = 1 - Math.pow(1 - p, 3);
        el.textContent = Math.round(target * eased).toLocaleString();
        if (p < 1) requestAnimationFrame(tick);
    })(start);
}

document.addEventListener('click', e => {
    const btn = e.target.closest('[data-ripple]');
    if (!btn) return;
    const rect   = btn.getBoundingClientRect();
    const ripple = document.createElement('span');
    const size   = Math.max(rect.width, rect.height);
    ripple.className = 'ripple';
    ripple.style.width  = ripple.style.height = size + 'px';
    ripple.style.left   = (e.clientX - rect.left - size / 2) + 'px';
    ripple.style.top    = (e.clientY - rect.top  - size / 2) + 'px';
    btn.appendChild(ripple);
    ripple.addEventListener('animationend', () => ripple.remove());
    setTimeout(() => ripple.remove(), 800);
});

// ════════════════════════════════════════════════════════════════════
// GAME ACTIONS
// ════════════════════════════════════════════════════════════════════
let simState = null;     // mirror of the SERVER state (never saved locally)
let serverView = null;  // full flat view returned by /api/game/*
let actionBusy = false;

const rand = (min, max) => Math.floor(min + Math.random() * (max - min + 1));

function initSim(user) {
    // Placeholder shown for a split second until the server answers.
    simState = {
        level: Number(user.level) || 1, xp: 0, xpNeeded: 100,
        hp: 0, maxHp: 1, mp: 0, maxMp: 1,
        gold: 0, gems: 0, wins: 0, losses: 0, dailyAt: 0
    };
}

function applyServerState(st, first = false) {
    if (!st) return;
    serverView = st;
    simState = {
        level: st.level, xp: st.xp, xpNeeded: st.xp_needed,
        hp: st.hp, maxHp: st.max_hp, mp: st.mp, maxMp: st.max_mp,
        gold: st.gold, gems: st.gems, wins: st.battles_won, losses: st.battles_lost,
        dailyAt: st.daily_at
    };
    applySim(first);
    if (first) {
        countUpTo('gold-display', st.gold);
        countUpTo('gems-display', st.gems);
    }
}

async function syncGameState(first = false) {
    try {
        const res = await fetch('/api/game/state', { credentials: 'same-origin' });
        if (!res.ok) return;
        const data = await res.json();
        if (data.ok) applyServerState(data.state, first);
    } catch (e) { console.error('Game state error:', e); }
}

function applySim(initial = false) {
    const s = simState;
    if (!s) return;
    const num = n => Math.round(n).toLocaleString();

    if (!initial) {
        setEl('gold-display', num(s.gold));
        setEl('gems-display', num(s.gems));
    }
    setEl('stat-gold', num(s.gold));
    setEl('stat-gems', num(s.gems));
    const balance = document.getElementById('auction-balance');
    if (balance) balance.innerHTML = num(s.gold) + ' <span style="font-size:0.7rem;color:#333;">Gold</span>';

    const cls = (serverView && serverView.char_class) || (currentUser && currentUser.char_class) || 'Explorer';
    setEl('dash-level', s.level);
    setEl('dash-level-chip', s.level);
    setEl('dash-class', `Level ${s.level} · ${cls}`);
    setEl('dash-class-display', cls);
    setEl('sidebar-userrole', `Level ${s.level}`);
    const xpPct = Math.max(0, Math.min(100, (s.xp / s.xpNeeded) * 100));
    const xpBar = document.getElementById('xp-bar');
    if (xpBar) xpBar.style.width = xpPct + '%';
    setEl('xp-text', `${num(s.xp)} / ${num(s.xpNeeded)}`);
    updateLevelRing(xpPct);

    const hpBar = document.getElementById('hp-bar');
    if (hpBar) hpBar.style.width = Math.max(0, Math.min(100, (s.hp / s.maxHp) * 100)) + '%';
    setEl('hp-text', `${num(s.hp)} / ${num(s.maxHp)}`);
    const mpBar = document.getElementById('mp-bar');
    if (mpBar) mpBar.style.width = Math.max(0, Math.min(100, (s.mp / s.maxMp) * 100)) + '%';
    setEl('mp-text', `${num(s.mp)} / ${num(s.maxMp)}`);

    setEl('stat-wins', num(s.wins));
    setEl('stat-losses', num(s.losses));

    const v = serverView;
    if (v) {
        setEl('stat-rank', '#' + v.rank);
        setEl('char-power', (v.combat_power || 0).toLocaleString());
        setEl('char-atk', v.attack);
        setEl('char-def', v.defense);
        setEl('char-mag', v.magic);
        setEl('char-spd', v.speed);
        setEl('char-lck', v.luck);
        setEl('char-crit', v.critical_chance + '%');
    }
}

const EVENT_NOTIF = { level_up: 'level', achievement: 'gift', quest: 'quest', title: 'gift', rank: 'quest', boss: 'battle' };
const EVENT_ICON  = { level_up: '⭐', achievement: '🏆', quest: '📜', title: '🎖️', rank: '🛡️', boss: '💀' };

function announceEvents(events) {
    (events || []).forEach((ev, i) => {
        addNotification(EVENT_NOTIF[ev.type] || 'level',
            ev.type === 'level_up' ? 'Level up!' : ev.type === 'quest' ? 'Quest complete' : 'Achievement unlocked',
            ev.text);
        // The first toast belongs to the action itself; stagger the extras.
        setTimeout(() => showToast(`${EVENT_ICON[ev.type] || '✨'} ${ev.text}`), 1400 * (i + 1));
    });
}

async function performAction(action) {
    if (actionBusy) return;
    actionBusy = true;
    try {
        const res = await fetch(`/api/game/${action}`, {
            method: 'POST', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' }
        });
        const d = await res.json().catch(() => ({}));
        if (res.status === 401) { showToast('Session expired. Please log in again.', true); return; }
        if (!d.ok) { showToast(d.error || 'That did not work.', true); return; }

        applyServerState(d.state);

        if (action === 'hunt') {
            if (d.result === 'win') {
                showToast(`⚔️ ${d.message}`);
                addNotification('battle', 'Hunt won', d.message);
            } else {
                showToast(`💀 ${d.message}`, true);
                addNotification('battle', 'Hunt lost', d.message);
            }
            if (d.loot && currentUser && currentUser.discord_id) loadInventory(currentUser.discord_id);
        } else if (action === 'heal') {
            showToast(`💚 ${d.message}`);
            addNotification('level', 'Healed up', d.message);
        } else if (action === 'daily') {
            showToast(`🎁 ${d.message}`);
            addNotification('gift', 'Daily reward claimed', d.message);
        }
        announceEvents(d.events);
        if (currentUser && currentUser.discord_id) loadLeaderboard();
    } catch (e) {
        console.error(e);
        showToast('Could not reach the server. Try again.', true);
    } finally {
        actionBusy = false;
    }
}

// ════════════════════════════════════════════════════════════════════
// MINI GAME — Spirit Catcher
// ════════════════════════════════════════════════════════════════════
const spirit = {
    running: false, score: 0, timeLeft: 0, last: -1,
    tick: null, spawn: null, hide: null, best: 0
};
const SPIRIT_SECONDS = 20;

function setupSpiritGame() {
    const root = document.getElementById('miniGame');
    const grid = document.getElementById('mgGrid');
    if (!root || !grid) return;

    try { spirit.best = Number(localStorage.getItem('rpgGhostSpiritBest')) || 0; } catch { /* ignore */ }
    setEl('mgBest', spirit.best);

    for (let i = 0; i < 9; i++) {
        const cell = document.createElement('button');
        cell.type = 'button';
        cell.className = 'mg-cell';
        cell.setAttribute('aria-label', 'Spirit spot ' + (i + 1));
        cell.addEventListener('click', () => {
            if (!spirit.running || !cell.classList.contains('ghost')) return;
            cell.classList.remove('ghost');
            cell.classList.add('caught');
            setTimeout(() => cell.classList.remove('caught'), 180);
            spirit.score++;
            setEl('mgScore', spirit.score);
            spawnSpirit(true);
        });
        grid.appendChild(cell);
    }

    document.getElementById('miniGameFab')?.addEventListener('click', () => root.classList.toggle('is-open'));
    document.getElementById('miniGameClose')?.addEventListener('click', () => root.classList.remove('is-open'));
    document.getElementById('mgStart')?.addEventListener('click', startSpiritGame);
}

function clearSpiritBoard() {
    document.querySelectorAll('#mgGrid .mg-cell').forEach(c => c.classList.remove('ghost', 'caught'));
}

function spawnSpirit(immediate = false) {
    if (!spirit.running) return;
    clearTimeout(spirit.spawn); clearTimeout(spirit.hide);
    clearSpiritBoard();

    const show = () => {
        if (!spirit.running) return;
        const cells = document.querySelectorAll('#mgGrid .mg-cell');
        let i;
        do { i = rand(0, cells.length - 1); } while (i === spirit.last);
        spirit.last = i;
        cells[i].classList.add('ghost');
        const life = Math.max(550, 1000 - (SPIRIT_SECONDS - spirit.timeLeft) * 25);
        spirit.hide = setTimeout(() => { clearSpiritBoard(); spawnSpirit(); }, life);
    };
    spirit.spawn = setTimeout(show, immediate ? 120 : rand(150, 350));
}

function startSpiritGame() {
    if (spirit.running) return;
    // The server timestamps the round so the reward can't be claimed without playing it.
    fetch('/api/game/spirit/start', { method: 'POST', credentials: 'same-origin' }).catch(() => {});
    spirit.running  = true;
    spirit.score    = 0;
    spirit.timeLeft = SPIRIT_SECONDS;
    spirit.last     = -1;
    setEl('mgScore', 0);
    setEl('mgTime', spirit.timeLeft);
    setEl('mgMsg', 'Tap the ghosts before they vanish!');
    const startBtn = document.getElementById('mgStart');
    if (startBtn) { startBtn.disabled = true; startBtn.textContent = 'Playing…'; }

    spirit.tick = setInterval(() => {
        spirit.timeLeft--;
        setEl('mgTime', spirit.timeLeft);
        if (spirit.timeLeft <= 0) endSpiritGame();
    }, 1000);
    spawnSpirit(true);
}

function endSpiritGame() {
    spirit.running = false;
    clearInterval(spirit.tick); clearTimeout(spirit.spawn); clearTimeout(spirit.hide);
    clearSpiritBoard();

    const score = spirit.score;
    const gold  = score * 3;
    const xp    = score * 2;
    if (score > spirit.best) {
        spirit.best = score;
        setEl('mgBest', score);
        try { localStorage.setItem('rpgGhostSpiritBest', String(score)); } catch { /* ignore */ }
    }

    const startBtn = document.getElementById('mgStart');
    if (startBtn) { startBtn.disabled = false; startBtn.textContent = 'Play again'; }

    if (score > 0 && simState) {
        setEl('mgMsg', `Caught ${score}! Claiming your reward...`);
        submitSpiritScore(score);
    } else {
        setEl('mgMsg', 'No ghosts caught. Try again!');
    }
}

async function submitSpiritScore(score) {
    try {
        const res = await fetch('/api/game/spirit', {
            method: 'POST', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ score })
        });
        const d = await res.json().catch(() => ({}));
        if (!d.ok) { setEl('mgMsg', d.error || 'Reward could not be claimed.'); return; }
        setEl('mgMsg', d.message);
        showToast(`👻 ${d.message}`);
        applyServerState(d.state);
        announceEvents(d.events);
        if (currentUser && currentUser.discord_id) loadLeaderboard();
    } catch (e) {
        console.error(e);
        setEl('mgMsg', 'Could not reach the server.');
    }
}

function placeBid(itemName) {
    showToast(`💰 Bid placed on ${itemName}!`);
    addNotification('auction', 'Bid placed', `You placed a bid on ${itemName}.`);
    const el = document.getElementById('active-bids');
    if (el) el.textContent = (parseInt(el.textContent) || 0) + 1;
}

// ════════════════════════════════════════════════════════════════════
// TOAST
// ════════════════════════════════════════════════════════════════════
function showToast(message, isError = false) {
    const toast   = document.getElementById('toast');
    const msgEl   = document.getElementById('toast-message');
    const iconEl  = toast?.querySelector('i');
    if (!toast || !msgEl) return;

    msgEl.textContent = message;
    toast.style.borderLeftColor = isError ? '#ef4444' : 'var(--dash-teal)';
    if (iconEl) iconEl.style.color = isError ? '#ef4444' : 'var(--spectral-teal)';

    const old = toast.querySelector('.toast-progress');
    if (old) old.remove();
    const bar = document.createElement('div');
    bar.className = 'toast-progress';
    toast.appendChild(bar);

    toast.classList.add('show');
    setTimeout(() => toast.classList.remove('show'), 3500);
}

function setEl(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
}
// FIX: Forgot Password click handler
document.addEventListener("click", function (event) {
    const element = event.target.closest(
        'a, button, [role="button"]'
    );

    if (!element) return;

    const text = (
        element.textContent ||
        element.getAttribute("aria-label") ||
        ""
    ).trim();

    if (!/forgot\s*password/i.test(text)) return;

    event.preventDefault();

    const forgotForm = document.getElementById("forgot-form");
    const emailInput = document.getElementById("forgot-email");

    if (!forgotForm || !emailInput) {
        console.error(
            "Forgot Password: #forgot-form or #forgot-email is missing."
        );
        return;
    }

    document.querySelectorAll(".auth-tab, .auth-form")
        .forEach(function (item) {
            item.classList.remove("active");
        });

    forgotForm.classList.add("active");
    emailInput.focus();
}, true);
document.getElementById('logoutBtn')?.addEventListener('click', logout);
