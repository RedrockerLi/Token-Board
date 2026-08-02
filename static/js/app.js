/**
 * app.js — SPA Router + Sidebar Controller.
 *
 * Provides hash-based page routing, collapsible sidebar with localStorage
 * state persistence, and lazy-loading of page modules.
 *
 * Dependencies: none (loaded first).
 */

// ── Sidebar ──────────────────────────────────────────────────────────────

const Sidebar = {
    /** Initialize sidebar state and bind events. */
    init() {
        const sidebar = document.getElementById('sidebar');
        const toggle = document.getElementById('sidebarToggle');

        // Restore collapsed state
        const collapsed = localStorage.getItem('sidebarCollapsed') === 'true';
        if (collapsed) {
            sidebar.classList.add('sidebar--collapsed');
        }

        // Toggle collapse
        toggle.addEventListener('click', () => {
            sidebar.classList.toggle('sidebar--collapsed');
            localStorage.setItem(
                'sidebarCollapsed',
                sidebar.classList.contains('sidebar--collapsed')
            );
        });

        // Nav section collapse (click on section title toggles children)
        document.querySelectorAll('.sidebar__nav-section-title').forEach((title) => {
            title.addEventListener('click', () => {
                const section = title.parentElement;
                section.classList.toggle('sidebar__nav-section--collapsed');
            });
        });
    },

    /** Highlight the active nav item for the given hash. */
    setActive(hash) {
        document.querySelectorAll('.sidebar__nav-item').forEach((item) => {
            item.classList.remove('sidebar__nav-item--active');
        });
        const active = document.querySelector(`[href="${hash}"]`);
        if (active) active.classList.add('sidebar__nav-item--active');
    },
};

// ── Page Registry ────────────────────────────────────────────────────────

/**
 * Each page definition:
 *   hash      — URL hash (e.g. '#/dashboard')
 *   container — DOM element ID to show/hide
 *   title     — window.document.title when active
 *   module    — (optional) JS file to lazy-load
 *   initFn    — (required) function to call on navigate (or name of global)
 *   destroyFn — (optional) function to call when leaving the page
 */
const PAGES = [
    {
        hash: '#/dashboard',
        container: 'page-dashboard',
        title: '用量仪表板 · Token Board',
        initFn: 'initDashboard',
        destroyFn: null,
    },
    {
        hash: '#/proxy/billing',
        container: 'page-billing',
        title: '消费报告 · Token Board',
        module: '/static/js/proxy_billing.js',
        initFn: 'initBillingPage',
        destroyFn: null,
    },
    {
        hash: '#/proxy/accounts',
        container: 'page-proxy-accounts',
        title: '上游账户 · Token Board',
        initFn: 'initAccountsPage',
        destroyFn: null,
    },
    {
        hash: '#/proxy/aggregates',
        container: 'page-proxy-aggregates',
        title: '上游账户聚合 · Token Board',
        initFn: 'initAggregatesPage',
        destroyFn: null,
    },
    {
        hash: '#/proxy/keys',
        container: 'page-proxy-keys',
        title: '本地密钥 · Token Board',
        initFn: 'initKeysPage',
        destroyFn: null,
    },
    {
        hash: '#/proxy/pricing',
        container: 'page-proxy-pricing',
        title: '模型定价 · Token Board',
        initFn: 'initPricingPage',
        destroyFn: null,
    },
    {
        hash: '#/proxy/logs',
        container: 'page-proxy-logs',
        title: '请求日志 · Token Board',
        module: '/static/js/proxy_billing.js',
        initFn: 'initLogsPage',
        destroyFn: null,
    },
    {
        hash: '#/proxy/perf',
        container: 'page-proxy-perf',
        title: '性能监控 · Token Board',
        module: '/static/js/proxy_perf.js',
        initFn: 'initPerfPage',
        destroyFn: 'destroyPerfPage',
    },
];

const DEFAULT_HASH = '#/dashboard';

const Router = {
    currentPage: null,
    loadedModules: {},

    /** Navigate to a hash. Called on page load and hashchange. */
    navigate(hash) {
        // Default
        if (!hash || hash === '#') {
            hash = DEFAULT_HASH;
        }

        const page = PAGES.find((p) => p.hash === hash);
        if (!page) {
            return this.navigate(DEFAULT_HASH);
        }

        // Destroy previous page if needed
        if (this.currentPage && this.currentPage.destroyFn) {
            const fn = resolveFn(this.currentPage.destroyFn);
            if (fn) fn();
        }

        // Hide all containers
        document.querySelectorAll('[id^="page-"]').forEach((el) => {
            el.style.display = 'none';
        });

        // Show target
        const container = document.getElementById(page.container);
        if (container) container.style.display = '';

        // Update sidebar
        Sidebar.setActive(hash);

        // Update title
        document.title = page.title;

        // Load module if needed
        const loadAndInit = () => {
            const initFn = resolveFn(page.initFn);
            if (initFn) initFn();
        };

        if (page.module && !this.loadedModules[page.module]) {
            this.loadedModules[page.module] = true;
            this._loadScript(page.module, loadAndInit);
        } else {
            loadAndInit();
        }

        this.currentPage = page;

        // Update hash without triggering hashchange
        if (window.location.hash !== hash) {
            history.pushState(null, '', hash);
        }
    },

    _loadScript(src, callback) {
        const script = document.createElement('script');
        script.src = src;
        script.onload = callback;
        script.onerror = () => {
            console.error('Failed to load module:', src);
            callback(); // try anyway
        };
        document.body.appendChild(script);
    },
};

// ── Helpers ──────────────────────────────────────────────────────────────

function resolveFn(nameOrFn) {
    if (typeof nameOrFn === 'function') return nameOrFn;
    if (typeof nameOrFn === 'string') {
        const fn = window[nameOrFn];
        return typeof fn === 'function' ? fn : null;
    }
    return null;
}

// ── Bootstrap ────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
    Sidebar.init();

    // Click handler for sidebar nav items
    document.addEventListener('click', (e) => {
        const link = e.target.closest('.sidebar__nav-item');
        if (link && link.getAttribute('href')) {
            e.preventDefault();
            const hash = link.getAttribute('href');
            Router.navigate(hash);
        }
    });

    // Hash change
    window.addEventListener('hashchange', () => {
        Router.navigate(window.location.hash);
    });

    // Initial navigation
    const initialHash = window.location.hash || DEFAULT_HASH;
    Router.navigate(initialHash);
});
