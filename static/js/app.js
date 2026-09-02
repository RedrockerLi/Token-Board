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
    sidebar: null,
    scrim: null,
    mobileToggle: null,

    isMobile() {
        return window.matchMedia('(max-width: 720px)').matches;
    },

    setMobileOpen(open) {
        if (!this.sidebar) return;
        this.sidebar.classList.toggle('sidebar--mobile-open', open);
        if (this.scrim) {
            this.scrim.classList.toggle('sidebar-scrim--visible', open);
            this.scrim.setAttribute('aria-hidden', String(!open));
        }
        if (this.mobileToggle) {
            this.mobileToggle.setAttribute('aria-expanded', String(open));
            this.mobileToggle.setAttribute('aria-label', open ? '关闭导航' : '打开导航');
        }
        document.body.classList.toggle('nav-open', open);
    },

    closeMobile() {
        this.setMobileOpen(false);
    },

    /** Initialize sidebar state and bind events. */
    init() {
        const sidebar = document.getElementById('sidebar');
        const toggle = document.getElementById('sidebarToggle');
        const scrim = document.getElementById('sidebarScrim');
        const mobileToggle = document.getElementById('mobileNavToggle');
        this.sidebar = sidebar;
        this.scrim = scrim;
        this.mobileToggle = mobileToggle;

        // Restore collapsed state
        const collapsed = localStorage.getItem('sidebarCollapsed') === 'true';
        // A desktop collapse preference should not turn the mobile drawer into
        // an icon rail. Mobile gets a full-width, touch-friendly navigation.
        if (collapsed && !this.isMobile()) {
            sidebar.classList.add('sidebar--collapsed');
        }
        toggle.setAttribute(
            'aria-expanded',
            String(!sidebar.classList.contains('sidebar--collapsed'))
        );
        toggle.setAttribute(
            'aria-label',
            sidebar.classList.contains('sidebar--collapsed') ? '展开侧边栏' : '收缩侧边栏'
        );

        // Toggle collapse
        toggle.addEventListener('click', () => {
            if (this.isMobile()) {
                this.closeMobile();
                return;
            }
            sidebar.classList.toggle('sidebar--collapsed');
            localStorage.setItem(
                'sidebarCollapsed',
                sidebar.classList.contains('sidebar--collapsed')
            );
            toggle.setAttribute(
                'aria-expanded',
                String(!sidebar.classList.contains('sidebar--collapsed'))
            );
            toggle.setAttribute(
                'aria-label',
                sidebar.classList.contains('sidebar--collapsed') ? '展开侧边栏' : '收缩侧边栏'
            );
        });

        if (mobileToggle) {
            mobileToggle.addEventListener('click', () => {
                this.setMobileOpen(!sidebar.classList.contains('sidebar--mobile-open'));
            });
        }
        if (scrim) scrim.addEventListener('click', () => this.closeMobile());
        document.addEventListener('keydown', (event) => {
            if (event.key === 'Escape') this.closeMobile();
        });
        window.addEventListener('resize', () => {
            if (this.isMobile()) {
                sidebar.classList.remove('sidebar--collapsed');
            } else {
                this.closeMobile();
            }
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
        this.closeMobile();
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
        hash: '#/settings/pricing',
        container: 'page-settings-pricing',
        title: '模型定价 · 通用设置 · Token Board',
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
    {
        hash: '#/settings/general',
        container: 'page-settings-general',
        title: '通用设置 · Token Board',
        module: '/static/js/proxy_settings.js',
        initFn: 'initSettingsPage',
        destroyFn: null,
    },
    {
        hash: '#/agent/subscriptions',
        container: 'page-agent-subscriptions',
        title: '订阅管理 · Token Board',
        initFn: 'initAgentSubscriptionsPage',
        destroyFn: null,
    },
    {
        hash: '#/agent/software',
        container: 'page-agent-software',
        title: '软件管理 · Token Board',
        initFn: 'initAgentSoftwarePage',
        destroyFn: null,
    },
];

const DEFAULT_HASH = '#/dashboard';

// ── Config cloud sync (upload on leaving a settings page) ────────────────
// Config edits apply to the local proxy DB immediately. The whole config is
// pushed once when leaving a settings page; failed PUTs are rolled back by the
// server to the most recent cloud baseline.
const ConfigSync = {
    dirty: false,
    uploading: false,
    state: 'syncing',
    message: null,
    statusTimer: null,
    configPages: new Set([
        '#/proxy/accounts',
        '#/proxy/aggregates',
        '#/proxy/keys',
        '#/settings/pricing',
        '#/settings/general',
        '#/agent/subscriptions',
        '#/agent/software',
    ]),
    markDirty() { this.dirty = true; },
    isConfigPage(hash) { return this.configPages.has(hash); },
    /** Upload on leaving a settings page (no-op if nothing was edited). */
    flush() {
        if (!this.dirty) return Promise.resolve(true);
        this.dirty = false;
        return this.upload();
    },
    async refreshStatus() {
        try {
            const data = await proxyApi('/api/proxy/sync/config/status');
            const oldState = this.state;
            this.state = data.state || 'read_only';
            this.message = data.message || null;
            applyConfigSyncState();
            if (oldState !== this.state &&
                (this.state === 'writable' || this.state === 'local_only')) {
                if (this.state === 'writable') window.dispatchEvent(new Event('config-sync-ready'));
                if (this.statusTimer) {
                    clearInterval(this.statusTimer);
                    this.statusTimer = null;
                }
            }
            return data;
        } catch (e) {
            this.state = 'read_only';
            this.message = e.message || '无法读取云端配置状态';
            applyConfigSyncState();
            return null;
        }
    },
    startStatusPolling() {
        this.refreshStatus();
        if (!this.statusTimer) this.statusTimer = setInterval(() => this.refreshStatus(), 1000);
    },
    async upload() {
        if (this.state === 'syncing' || this.state === 'read_only') {
            showToast(this.message || '云端配置尚未就绪', 'error');
            return false;
        }
        this.uploading = true;
        try {
            let data;
            try {
                data = await proxyApi('/api/proxy/sync/config/upload', { method: 'POST' });
            } catch (e) {
                data = { status: 'error', message: e.message || '网络错误' };
            }
            if (data.status === 'ok') {
                showToast('配置已同步到云端');
                return true;
            }
            if (data.status === 'unconfigured') {
                return true;
            }
            if (data.status === 'rolled_back') {
                showToast(data.message || '上传失败，已恢复云端设置', 'error');
                window.dispatchEvent(new Event('config-sync-rolled-back'));
                return false;
            }
            if (data.status === 'read_only' || data.status === 'error') {
                showToast(data.message || '配置上传失败', 'error');
                this.refreshStatus();
                return false;
            }
            showToast(data.message || '配置上传失败', 'error');
            return false;
        } finally {
            this.uploading = false;
        }
    },
};

function applyConfigSyncState() {
    const banner = document.getElementById('configSyncStatusBar');
    if (banner) {
        const labels = {
            syncing: '正在拉取云端配置，设置暂时只读…',
            read_only: ConfigSync.message || '云端配置拉取失败，设置暂时只读。',
            local_only: '未配置云端同步，当前使用本机设置。',
            writable: '',
        };
        banner.textContent = labels[ConfigSync.state] || labels.read_only;
        banner.querySelectorAll('button').forEach((button) => button.remove());
        banner.style.display = ConfigSync.state === 'writable' ? 'none' : '';
        banner.className = `config-sync-status config-sync-status--${ConfigSync.state}`;
        if (ConfigSync.state === 'read_only') {
            const retry = document.createElement('button');
            retry.className = 'btn btn--sm';
            retry.textContent = '重新拉取';
            retry.onclick = () => {
                proxyApi('/api/proxy/sync/config/pull', { method: 'POST' })
                    .then(() => ConfigSync.startStatusPolling())
                    .catch((e) => showToast(e.message, 'error'));
            };
            banner.appendChild(retry);
        }
    }
    const editable = ConfigSync.state === 'writable' || ConfigSync.state === 'local_only';
    const containers = [
        'page-proxy-accounts', 'page-proxy-aggregates', 'page-proxy-keys',
        'page-settings-pricing', 'page-settings-general',
        'page-agent-subscriptions', 'page-agent-software',
    ];
    containers.forEach((id) => {
        const container = document.getElementById(id);
        if (!container) return;
        container.querySelectorAll('input,select,textarea,button').forEach((el) => {
            const connectionControl = id === 'page-settings-general' &&
                (el.id === 'btnTestSync' || el.id === 'btnSaveSync' ||
                 el.closest('#syncConfigForm'));
            el.disabled = connectionControl ? ConfigSync.state === 'syncing' : !editable;
        });
    });
}

function reloadCurrentConfigPage() {
    if (!Router.currentPage || !ConfigSync.isConfigPage(Router.currentPage.hash)) return;
    const container = document.getElementById(Router.currentPage.container);
    if (container) delete container.dataset.initialized;
    const hash = Router.currentPage.hash;
    Router.currentPage = null;
    Router.navigate(hash);
}

const Router = {
    currentPage: null,
    loadedModules: {},
    navigation: Promise.resolve(),

    /** Navigate to a hash. Called on page load and hashchange. */
    navigate(hash) {
        this.navigation = this.navigation.then(() => this._navigate(hash));
        return this.navigation;
    },

    async _navigate(hash) {
        // Default
        if (!hash || hash === '#') {
            hash = DEFAULT_HASH;
        }

        const page = PAGES.find((p) => p.hash === hash);
        if (!page) {
            return this._navigate(DEFAULT_HASH);
        }

        // Leaving a settings page waits for one whole-config upload.
        if (this.currentPage && hash !== this.currentPage.hash
                && ConfigSync.isConfigPage(this.currentPage.hash)) {
            if (!await ConfigSync.flush()) return;
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
            applyConfigSyncState();
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

    ConfigSync.startStatusPolling();

    // Opening the dashboard asks the standalone maintenance service for one
    // extra pass. The request returns immediately and repeated wakes coalesce.
    proxyApi('/api/proxy/agent-usage/import', { method: 'POST' }).catch((error) => {
        console.warn('Failed to schedule agent usage import:', error);
    });

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

    window.addEventListener('beforeunload', (event) => {
        if (ConfigSync.dirty || ConfigSync.uploading) {
            event.preventDefault();
            event.returnValue = '设置尚未同步，请先离开设置页完成上传。';
        }
    });

    window.addEventListener('config-sync-ready', () => {
        reloadCurrentConfigPage();
    });
    window.addEventListener('config-sync-rolled-back', () => {
        ConfigSync.dirty = false;
        reloadCurrentConfigPage();
    });

    // Initial navigation
    const initialHash = window.location.hash || DEFAULT_HASH;
    Router.navigate(initialHash);
});
