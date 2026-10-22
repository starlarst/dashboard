/*
 * Shared sidebar + mobile navigation for Realm of Valor pages.
 */
(function () {
    const NAV = [
        { key: 'overview',     href: '/dashboard',    icon: 'fa-compass',       label: 'Overview' },
        { key: 'profile',      href: '/profile',      icon: 'fa-id-card',       label: 'Profile' },
        { key: 'achievements', href: '/achievements', icon: 'fa-trophy',        label: 'Achievements' },
        { key: 'leaderboard',  href: '/leaderboard',  icon: 'fa-ranking-star',  label: 'Leaderboard' },
        { key: 'battle-log',   href: '/battle-log',   icon: 'fa-khanda',        label: 'Battle Log' },
        { key: 'inventory',    href: '/inventory',    icon: 'fa-boxes-stacked', label: 'Inventory' },
        { key: 'quest-board',  href: '/quest-board',  icon: 'fa-scroll',        label: 'Quest Board' },
        { key: 'guild-hall',   href: '/guild-hall',   icon: 'fa-users',         label: 'Guild Hall' },
        { key: 'settings',     href: '/settings',     icon: 'fa-gear',          label: 'Settings' }
    ];

    const slot = document.getElementById('layout-slot');
    if (!slot) return;
    const currentPage = document.body.dataset.page || '';

    const navItem = item => `
            <li><a href="${item.href}" data-page="${item.key}"${item.key === currentPage ? ' class="active"' : ''}>
                <span class="nav-icon"><i class="fa-solid ${item.icon}"></i></span>
                <span>${item.label}</span>
            </a></li>`;

    slot.outerHTML = `
    <header class="topbar" id="mobileTopbar">
        <button class="menu-toggle" id="navToggle" aria-label="Toggle navigation"><i class="fas fa-bars"></i></button>
        <span class="topbar-logo">Realm of Valor</span>
    </header>
    <div class="nav-backdrop" id="navBackdrop"></div>

    <aside id="sidebar" class="sidebar" role="navigation" aria-label="Main Navigation">
        <div class="sidebar-head">
            <a href="/dashboard" class="logo">
                <span class="logo-icon"><i class="fas fa-shield-halved"></i></span>
                <span>Realm of Valor</span>
            </a>
            <button class="sidebar-collapse-btn" id="sidebarCollapseBtn" aria-label="Toggle sidebar">
                <i class="fas fa-chevron-left"></i>
            </button>
        </div>

        <nav class="sidebar-nav">
            <div class="nav-section-label">Main</div>
            <ul class="nav-links">${NAV.map(navItem).join('')}
            </ul>
            <div id="adminNavSlot"></div>
        </nav>

        <div class="sidebar-footer">
            <div class="sidebar-user" role="link" tabindex="0" onclick="location.href='/profile'">
                <img src="https://api.dicebear.com/7.x/avataaars/svg?seed=default&backgroundColor=b6e3f4" alt="Avatar" class="sidebar-avatar" id="sidebar-avatar">
                <div>
                    <div class="sidebar-user-name" id="sidebar-username">Adventurer</div>
                    <div class="sidebar-user-role" id="sidebar-userrole">Level 1</div>
                </div>
            </div>
        </div>
    </aside>`;

    // ── Mobile menu ──
    const sidebar  = document.getElementById('sidebar');
    const backdrop = document.getElementById('navBackdrop');

    const closeDrawer = () => {
        sidebar.classList.remove('is-open');
        backdrop.classList.remove('is-open');
        document.body.classList.remove('drawer-open');
        document.body.style.overflow = '';
    };

    document.getElementById('navToggle').addEventListener('click', () => {
        const willOpen = !sidebar.classList.contains('is-open');
        sidebar.classList.toggle('is-open', willOpen);
        backdrop.classList.toggle('is-open', willOpen);
        document.body.classList.toggle('drawer-open', willOpen);
        document.body.style.overflow = willOpen ? 'hidden' : '';
    });
    backdrop.addEventListener('click', closeDrawer);

    // Close on navigation
    sidebar.querySelectorAll('.nav-links a').forEach(a => {
        a.addEventListener('click', closeDrawer);
    });

    // Reset when rotating back to desktop width
    window.addEventListener('resize', () => {
        if (window.matchMedia('(min-width: 1101px)').matches) closeDrawer();
    });

    // ── Desktop collapse ──
    document.getElementById('sidebarCollapseBtn').addEventListener('click', () => {
        if (window.matchMedia('(max-width: 1100px)').matches) return;
        sidebar.classList.toggle('is-collapsed');
        document.body.classList.toggle('sidebar-is-collapsed');
    });

    // ── Logged-in user details and admin section ──
    const adminHTML = `
            <div class="nav-section-label">Admin</div>
            <ul class="nav-links">
                <li><a href="/dashboard" data-page="admin">
                    <span class="nav-icon"><i class="fa-solid fa-crown"></i></span>
                    <span>Admin Panel</span>
                </a></li>
            </ul>`;

    fetch('/api/me', { credentials: 'same-origin' })
        .then(r => r.json())
        .then(data => {
            const u = data.user || data;
            if (!u) return;
            const name = u.nickname || u.username;
            if (name) document.getElementById('sidebar-username').textContent = name;
            if (u.avatar) document.getElementById('sidebar-avatar').src = u.avatar;
            if (u.level) document.getElementById('sidebar-userrole').textContent = `Level ${u.level}`;
            if (u.is_admin) document.getElementById('adminNavSlot').innerHTML = adminHTML;
        })
        .catch(() => { /* not logged in or offline: keep defaults */ });
})();
