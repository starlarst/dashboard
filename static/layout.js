/*
 * Shared sidebar + mobile navigation for Realm of Valor pages.
 * This file is the SINGLE owner of the mobile drawer open/close state.
 * Other scripts must NOT re-bind #navToggle / #navBackdrop.
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

    // If the page already rendered its own topbar (dashboard.html), don't inject a second one.
    const hasOwnTopbar = !!document.getElementById('mobileTopbar');
    const hasOwnBackdrop = !!document.getElementById('navBackdrop');
    const hasOwnSidebar = !!document.getElementById('sidebar');

    slot.outerHTML = `
        ${hasOwnTopbar ? '' : `
        <header class="topbar" id="mobileTopbar">
            <button class="menu-toggle" id="navToggle" aria-label="Toggle navigation" aria-expanded="false"><i class="fas fa-bars"></i></button>
            <span class="topbar-logo">Realm of Valor</span>
        </header>`}
        ${hasOwnBackdrop ? '' : `<div class="nav-backdrop" id="navBackdrop"></div>`}
        ${hasOwnSidebar ? '' : `
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
                <ul class="nav-links">${NAV.map(navItem).join('')}</ul>
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
        </aside>`}`;

    const sidebar   = document.getElementById('sidebar');
    const backdrop  = document.getElementById('navBackdrop');
    const toggleBtn = document.getElementById('navToggle');
    const mqlDesktop = window.matchMedia('(min-width: 1101px)');

    const setOpen = (open) => {
        if (!sidebar || !backdrop) return;
        sidebar.classList.toggle('is-open', open);
        backdrop.classList.toggle('is-open', open);
        document.body.classList.toggle('drawer-open', open);
        document.body.style.overflow = (open && !mqlDesktop.matches) ? 'hidden' : '';
        if (toggleBtn) toggleBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    };

    const closeDrawer = () => setOpen(false);
    const toggleDrawer = () => setOpen(!sidebar.classList.contains('is-open'));

    if (toggleBtn && !toggleBtn.dataset.realmNavBound) {
        toggleBtn.dataset.realmNavBound = '1';
        toggleBtn.addEventListener('click', (e) => { e.preventDefault(); toggleDrawer(); });
    }
    if (backdrop && !backdrop.dataset.realmNavBound) {
        backdrop.dataset.realmNavBound = '1';
        backdrop.addEventListener('click', closeDrawer);
    }
    if (sidebar) {
        sidebar.querySelectorAll('.nav-links a').forEach(a => {
            a.addEventListener('click', () => {
                // Only close on mobile; desktop keeps the sidebar visible.
                if (!mqlDesktop.matches) closeDrawer();
            });
        });
    }

    // Reset on rotation / resize to desktop.
    const onViewportChange = () => { if (mqlDesktop.matches) closeDrawer(); };
    mqlDesktop.addEventListener('change', onViewportChange);
    window.addEventListener('resize', onViewportChange);

    // Desktop collapse.
    const collapseBtn = document.getElementById('sidebarCollapseBtn');
    if (collapseBtn && !collapseBtn.dataset.realmNavBound) {
        collapseBtn.dataset.realmNavBound = '1';
        collapseBtn.addEventListener('click', () => {
            if (!mqlDesktop.matches) return;
            sidebar.classList.toggle('is-collapsed');
            document.body.classList.toggle('sidebar-is-collapsed');
        });
    }

    // Load user info + admin nav.
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
            const uEl = document.getElementById('sidebar-username');
            if (name && uEl) uEl.textContent = name;
            const avEl = document.getElementById('sidebar-avatar');
            if (u.avatar && avEl) avEl.src = u.avatar;
            if (u.level) {
                const rEl = document.getElementById('sidebar-userrole');
                if (rEl) rEl.textContent = `Level ${u.level}`;
            }
            if (u.is_admin) {
                const adminSlot = document.getElementById('adminNavSlot');
                if (adminSlot && !adminSlot.dataset.filled) {
                    adminSlot.dataset.filled = '1';
                    adminSlot.innerHTML = adminHTML;
                }
            }
        })
        .catch(() => {});

    // Public API for other scripts (dashboard.js reads state, never re-binds).
    window.RealmNav = {
        open:   () => setOpen(true),
        close:  () => setOpen(false),
        toggle: () => toggleDrawer(),
        isOpen: () => !!(sidebar && sidebar.classList.contains('is-open'))
    };
})();
