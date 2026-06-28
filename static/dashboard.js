
const RECAPTCHA_SITE_KEY = "6LfdHyEtAAAAAJ34WMBrqoIc_Um2YqTE1jX-Lmkz";
const EMAILJS_PUBLIC_KEY  = "eMQUadu5q-_pONmwc";
const EMAILJS_SERVICE_ID  = "service_miabeqg";
const EMAILJS_TEMPLATE_ID = "template_tntozpq";
const API_BASE = window.location.origin;

// ── STATE ────────────────────────────────────────────────────────────
let currentUser           = null;
let isDashboardInitialized = false;
let isShowingDashboard    = false;
let resetEmail            = "";
let resetOTP              = "";

// ── PAGE CONFIG (overview is the only page rendered client-side; ────
//    Battle Log / Inventory / Quest Board / Guild Hall / Settings are
//    real Flask routes now — see dashboard_app.py)
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

// ════════════════════════════════════════════════════════════════════
// DOMContentLoaded
// ════════════════════════════════════════════════════════════════════
document.addEventListener('DOMContentLoaded', () => {
    // Init EmailJS
    if (typeof emailjs !== 'undefined') emailjs.init({ publicKey: EMAILJS_PUBLIC_KEY });

    checkAuth();
    setupSidebar();
    setupOTPInputs();
    setupDiscordButtons();
});

// ════════════════════════════════════════════════════════════════════
// AUTH CHECK
// ════════════════════════════════════════════════════════════════════
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

    if (toggle && backdrop && sidebar) {
        toggle.addEventListener('click', () => {
            sidebar.classList.toggle('is-open');
            backdrop.classList.toggle('is-open');
        });
        backdrop.addEventListener('click', () => {
            sidebar.classList.remove('is-open');
            backdrop.classList.remove('is-open');
        });
    }

    // Desktop collapse
    if (collapseBtn && sidebar && container) {
        collapseBtn.addEventListener('click', () => {
            sidebar.classList.toggle('is-collapsed');
            container.classList.toggle('is-collapsed');
            positionNavPill();
        });
    }

    // Nav link clicks
    document.querySelectorAll('.nav-links a').forEach(link => {
        link.addEventListener('click', (e) => {
            const page = link.dataset.page || 'overview';

            // Overview stays an in-page SPA swap (no reload needed, we're already here).
            // Every other page is a real Flask route (/battle-log, /inventory, etc.)
            // with its own template, so let the browser navigate there normally.
            if (page === 'overview') {
                e.preventDefault();
                navigateTo(page);
            }

            sidebar?.classList.remove('is-open');
            backdrop?.classList.remove('is-open');
        });
    });
}

function positionNavPill() {
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

    // Hide all panels
    document.querySelectorAll('.panel').forEach(p => p.classList.add('hidden'));

    // Show target panels
    config.panels.forEach(cls => {
        const el = document.querySelector(`.${cls}`);
        if (el) el.classList.remove('hidden');
    });

    // Update panel titles
    Object.keys(config.titles).forEach(cls => {
        const panel = document.querySelector(`.${cls}`);
        if (!panel) return;
        const span = panel.querySelector('.panel-header span');
        if (span) span.innerHTML = config.titles[cls];
    });

    // Admin visibility
    const adminPanel = document.getElementById('adminPanel');
    if (adminPanel) {
        if (currentUser?.is_admin) adminPanel.classList.add('visible');
        else adminPanel.classList.remove('visible');
    }

    // Active link
    document.querySelectorAll('.nav-links a').forEach(a => {
        a.classList.toggle('active', a.dataset.page === page);
    });
    positionNavPill();
}

// ════════════════════════════════════════════════════════════════════
// SHOW DASHBOARD (post-login transition)
// ════════════════════════════════════════════════════════════════════
function showDashboard(user) {
    if (isShowingDashboard) return;
    isShowingDashboard = true;

    try {
        if (!user) { isShowingDashboard = false; return; }
        currentUser = user;

        document.getElementById('auth-screen').style.display   = 'none';
        document.getElementById('dashboard').style.display     = 'flex';
        document.getElementById('dashTopbar').style.display    = 'flex';

        const name = user.username || 'Adventurer';
        setEl('user-name-display', name);
        setEl('dash-name', name);
        setEl('dash-level', user.level || 1);
        setEl('dash-class', user.char_class || 'Adventurer');
        const chip = document.getElementById('dash-level-chip');
        if (chip) chip.textContent = user.level || 1;

        countUpTo('gold-display', user.gold || 0);
        countUpTo('gems-display', user.gems || 0);

        const balance = document.getElementById('auction-balance');
        if (balance) balance.innerHTML = (user.gold || 0).toLocaleString() + ' <span style="font-size:0.7rem;color:#333;">Gold</span>';

        const xpPct = user.xp_pct || 0;
        const xpBar = document.getElementById('xp-bar');
        if (xpBar) { xpBar.style.width = xpPct + '%'; }
        setEl('xp-text', `${user.xp || 0} / ${user.xp_needed || 100}`);

        if (user.avatar) {
            const av = document.getElementById('user-avatar');
            if (av) av.src = user.avatar;
        }

        updateLevelRing(xpPct);
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

        // Show overview
        setTimeout(() => navigateTo('overview'), 280);

        // Bot intro (once per session)
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
    }
    if (data.nickname) {
        document.querySelectorAll('#dash-name, #user-name-display').forEach(el => {
            if (el.textContent !== data.nickname) el.textContent = data.nickname;
        });
    }
}

// ════════════════════════════════════════════════════════════════════
// LOGOUT
// ════════════════════════════════════════════════════════════════════
async function logout() {
    try { await fetch('/logout'); } catch (e) { console.error(e); }

    document.getElementById('dashboard').style.display  = 'none';
    document.getElementById('dashTopbar').style.display = 'none';
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
// AUTH FUNCTIONS (untouched logic)
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
                body: JSON.stringify({ email, password: pass, recaptcha_token: token })
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
            resetEmail = email;
            resetOTP   = data.otp || '';
            showOTPForm(email);

            if (resetOTP && typeof emailjs !== 'undefined') {
                try {
                    await emailjs.send(EMAILJS_SERVICE_ID, EMAILJS_TEMPLATE_ID, {
                        to_email: email, otp_code: resetOTP, from_name: 'RPG Ghost'
                    });
                    showToast('Reset code sent to your email!');
                } catch {
                    showToast(`Code: ${resetOTP} (check console)`);
                }
            } else {
                showToast(`Your code: ${resetOTP}`);
            }
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

function resendOTP() {
    if (!resetEmail) return;
    showToast('Requesting new code...');
    document.getElementById('forgot-form').dispatchEvent(new Event('submit'));
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
        const res     = await fetch(`${API_BASE}/api/leaderboard`);
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
            tbody.innerHTML += `
                <tr class="${meCls}">
                    <td><span class="lb-rank ${rankCls}">#${i + 1}</span></td>
                    <td><img src="https://api.dicebear.com/7.x/avataaars/svg?seed=${p.name}&backgroundColor=b6e3f4" class="lb-avatar">
                        <span class="lb-name">${p.name}</span></td>
                    <td><span class="lb-class">${p.class}</span></td>
                    <td style="text-align:right"><span class="lb-score">${p.xp.toLocaleString()} XP</span></td>
                </tr>`;
        });
    } catch (e) { console.error('Leaderboard error:', e); }
}

function createPodiumHTML(player, rankClass, icon) {
    const colorClass = rankClass === 'first' ? 'gold' : rankClass === 'second' ? 'silver' : 'bronze';
    return `
        <div class="podium-item ${rankClass}">
            <img src="https://api.dicebear.com/7.x/avataaars/svg?seed=${player.name}&backgroundColor=b6e3f4" class="podium-avatar" alt="">
            <div class="podium-rank ${colorClass}">${icon}</div>
            <div class="podium-name">${player.name}</div>
            <div class="podium-score">${player.xp.toLocaleString()} XP</div>
        </div>`;
}

async function loadInventory(userId) {
    try {
        const res   = await fetch(`${API_BASE}/api/inventory/${userId}`);
        const data  = await res.json();
        const items = data.items || [];
        const cont  = document.getElementById('inventory-container');
        if (!cont) return;

        cont.innerHTML = '';
        if (items.length === 0) {
            cont.innerHTML = '<p style="grid-column:1/-1;text-align:center;color:#333;padding:20px;">Inventory is empty.</p>';
            return;
        }

        items.slice(0, 8).forEach(item => {
            const rarity = (item.rarity || 'common').toLowerCase();
            const icon   = getItemIcon(item.name);
            const slot   = document.createElement('div');
            slot.className = `inv-slot ${rarity}`;
            slot.innerHTML = `<i class="fa-solid ${icon}"></i>${item.quantity > 1 ? `<span class="item-count">${item.quantity}</span>` : ''}`;

            slot.addEventListener('mousemove', e => {
                const tt = document.getElementById('tooltip');
                document.getElementById('tt-name').textContent = item.name;
                document.getElementById('tt-desc').textContent = rarity.charAt(0).toUpperCase() + rarity.slice(1) + ' Item';
                document.getElementById('tt-stat').textContent = `Qty: ${item.quantity}`;
                tt.style.opacity = '1';
                tt.style.left    = Math.min(e.clientX + 15, window.innerWidth - 220) + 'px';
                tt.style.top     = Math.min(e.clientY + 15, window.innerHeight - 100) + 'px';
            });
            slot.addEventListener('mouseleave', () => {
                document.getElementById('tooltip').style.opacity = '0';
            });
            cont.appendChild(slot);
        });
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

        auctions.slice(0, 4).forEach(a => {
            const col = a.rarity === 'legendary' ? 'var(--rarity-legendary)'
                      : a.rarity === 'epic'       ? 'var(--rarity-epic)'
                      : a.rarity === 'rare'        ? 'var(--rarity-rare)'
                      : '#22c55e';
            const div = document.createElement('div');
            div.className = 'auction-item';
            div.onclick   = () => placeBid(a.name);
            div.innerHTML = `
                <div class="auction-item-icon" style="background:${col}15;color:${col}">
                    <i class="fa-solid fa-gavel"></i>
                </div>
                <div class="auction-item-info">
                    <div class="auction-item-name" style="color:${col}">${a.name}</div>
                    <div class="auction-item-desc">Seller: ${a.seller || 'Unknown'}</div>
                </div>
                <div class="auction-item-price">💰 ${(a.current_bid || 0).toLocaleString()} Gold</div>
                <button class="btn-bid" onclick="event.stopPropagation();placeBid('${a.name}')">Bid</button>`;
            cont.appendChild(div);
        });

        const listed = document.getElementById('items-listed');
        if (listed) listed.textContent = auctions.length;
    } catch (e) { console.error('Auctions error:', e); }
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
                    tr.innerHTML = `
                        <td><span style="font-family:'Orbitron',sans-serif;font-size:0.7rem;color:#444">#${user.id}</span></td>
                        <td><img src="${user.avatar}" class="user-avatar"><span class="lb-name">${user.nickname}</span></td>
                        <td style="font-size:0.8rem;color:#666">${user.email}</td>
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

    const defaultDesc = "Hey there, Adventurer. I'm RPG Ghost — a fully-featured Discord RPG bot where you can battle monsters, loot rare gear, complete quests, trade in the auction house, and climb the leaderboard with your guild. Your journey through the spectral realm starts here.";

    overlay.classList.add('visible');

    await humanTypewriter(descEl, defaultDesc, () => enterBtn?.classList.add('ready'));

    const dismiss = () => {
        overlay.classList.remove('visible');
        setTimeout(() => overlay.style.display = 'none', 400);
    };

    enterBtn?.addEventListener('click', dismiss);
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

// Fill the SVG XP ring
function updateLevelRing(pct) {
    const ring = document.getElementById('levelRing');
    if (!ring) return;
    const circumference = 339;   /* 2π × 54 — matches stroke-dasharray in CSS */
    const offset = circumference - (circumference * Math.max(0, Math.min(100, pct)) / 100);
    requestAnimationFrame(() => { ring.style.strokeDashoffset = offset; });
}

// Spawn ambient particles around avatar
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

// Animate a counter from 0 to target
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

// Ripple on data-ripple buttons
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
});

// ════════════════════════════════════════════════════════════════════
// GAME ACTIONS (UI feedback, no backend change needed)
// ════════════════════════════════════════════════════════════════════
function performAction(action) {
    if (action === 'heal')  showToast('💚 Casting Heal... HP Restored!');
    if (action === 'hunt')  showToast('⚔️ Entering the hunting grounds...');
    if (action === 'daily') showToast('🎁 Daily reward claimed: +100 XP, +50 Gold!');
}

function placeBid(itemName) {
    showToast(`💰 Bid placed on ${itemName}!`);
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

    // Recreate progress bar
    const old = toast.querySelector('.toast-progress');
    if (old) old.remove();
    const bar = document.createElement('div');
    bar.className = 'toast-progress';
    toast.appendChild(bar);

    toast.classList.add('show');
    setTimeout(() => toast.classList.remove('show'), 3500);
}

// ════════════════════════════════════════════════════════════════════
// UTILITIES
// ════════════════════════════════════════════════════════════════════
function setEl(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
}
