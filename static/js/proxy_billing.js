/**
 * proxy_billing.js — Billing overview and request log viewer pages.
 *
 * Exports: initBillingPage(), initLogsPage()
 * Lazy-loaded by app.js when navigating to #/proxy/billing or #/proxy/logs.
 */

/**
 * Format a UTC timestamp string from SQLite (YYYY-MM-DD HH:MM:SS)
 * into the browser's local timezone display.
 */
function fmtTime(ts) {
    if (!ts) return '';
    // SQLite datetime('now') returns "YYYY-MM-DD HH:MM:SS" in UTC.
    // Convert to ISO 8601 so JS Date parses it as UTC reliably.
    const iso = ts.replace(' ', 'T') + 'Z';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return ts; // fallback for malformed values
    return d.toLocaleString();
}

// ── Billing Page ─────────────────────────────────────────────────────────

async function loadBillingStats() {
    try {
        const stats = await proxyFetch('/api/proxy/stats');
        document.getElementById('billTotalRequests').textContent = fmtNum(stats.total_requests);
        document.getElementById('billTodayRequests').textContent = fmtNum(stats.today_requests);
        document.getElementById('billTotalCost').textContent = '¥' + (stats.total_cost || 0).toFixed(2);
        document.getElementById('billTodayCost').textContent = '¥' + (stats.today_cost || 0).toFixed(2);
        document.getElementById('billTotalTokens').textContent = fmtNum(stats.total_tokens);
        document.getElementById('billActiveKeys').textContent = stats.active_keys;
    } catch (err) {
        console.error('Failed to load billing stats:', err);
    }
}

let billingChart = null;

async function loadDailyBillingChart(year, month) {
    const dom = document.getElementById('chartBillingDaily');
    if (!dom) return;

    if (!year || !month) {
        dom.innerHTML = '<div class="loading" style="display:flex">请选择月份</div>';
        return;
    }

    try {
        const data = await proxyFetch(`/api/proxy/billing/daily-by-model?year=${year}&month=${month}`);

        if (!data.length) {
            dom.innerHTML = '<div class="loading" style="display:flex">该月暂无数据</div>';
            return;
        }

        const dates = data.map(d => d.date);
        const inputTokens = data.map(d => d.input_tokens);
        const outputTokens = data.map(d => d.output_tokens);
        const costs = data.map(d => d.cost);

        if (typeof echarts !== 'undefined') {
            if (billingChart) billingChart.dispose();
            billingChart = echarts.init(dom);
            billingChart.setOption({
                tooltip: {
                    trigger: 'axis',
                    axisPointer: { type: 'shadow' },
                    formatter: function(params) {
                        const idx = params[0] && params[0].dataIndex;
                        if (idx == null) return '';
                        const d = data[idx];
                        const total = ((d.input_tokens || 0) + (d.output_tokens || 0)) || 1;
                        const pct = (v) => (v / total * 100).toFixed(1);
                        const hit = d.cache_hit_tokens || 0;
                        const miss = d.cache_miss_tokens || 0;
                        let html = '<b>' + d.date + '</b><br/>';
                        html += '输出Token: <b>' + fmtNum(d.output_tokens || 0) + '</b> (' + pct(d.output_tokens || 0) + '%)<br/>';
                        html += '输入缓存命中: <b>' + fmtNum(hit) + '</b> (' + pct(hit) + '%)<br/>';
                        html += '输入缓存未命中: <b>' + fmtNum(miss) + '</b> (' + pct(miss) + '%)<br/>';
                        html += '消费: <b>¥' + (d.cost || 0).toFixed(2) + '</b>';
                        return html;
                    }
                },
                legend: { data: ['输入Token', '输出Token', '消费'], bottom: 0, textStyle: { fontSize: 11 } },
                grid: { left: 70, right: 70, top: 20, bottom: 50 },
                xAxis: { type: 'category', data: dates, axisLabel: { rotate: 30, fontSize: 10 } },
                yAxis: [
                    { type: 'value', name: 'Tokens', axisLabel: { formatter: v => fmtNum(v), fontSize: 10 } },
                    { type: 'value', name: '消费 (¥)', axisLabel: { fontSize: 10 } },
                ],
                series: [
                    {
                        name: '输出Token', type: 'bar', stack: 'tokens',
                        data: outputTokens, barMaxWidth: 28,
                        itemStyle: { color: '#0070F3' },
                    },
                    {
                        name: '输入Token', type: 'bar', stack: 'tokens',
                        data: inputTokens, barMaxWidth: 28,
                        itemStyle: { color: '#00CEF3', borderRadius: [4, 4, 0, 0] },
                    },
                    {
                        name: '消费', type: 'line', yAxisIndex: 1,
                        data: costs,
                        lineStyle: { color: '#EF4444', width: 2.5 },
                        itemStyle: { color: '#EF4444' },
                        symbol: 'circle', symbolSize: 6,
                    },
                ],
            });
            new ResizeObserver(() => billingChart.resize()).observe(dom);
        }
    } catch (err) {
        console.error('Failed to load daily billing chart:', err);
    }
}

function onBillingMonthChange() {
    const sel = document.getElementById('billingMonthSelector');
    if (!sel || !sel.value) return;
    const [year, month] = sel.value.split('-').map(Number);
    loadDailyBillingChart(year, month);
}

async function populateBillingMonthSelector() {
    const sel = document.getElementById('billingMonthSelector');
    if (!sel) return;
    try {
        const months = await proxyFetch('/api/proxy/billing/months');
        sel.innerHTML = '<option value="">-- 选择月份 --</option>';
        months.forEach((m) => {
            const opt = document.createElement('option');
            opt.value = m.year + '-' + m.month;
            opt.textContent = m.year + ' - ' + m.month + '月';
            sel.appendChild(opt);
        });
        // Auto-select latest month
        if (months.length > 0) {
            const latest = months[months.length - 1];
            sel.value = latest.year + '-' + latest.month;
            const [y, m] = [latest.year, latest.month];
            loadDailyBillingChart(y, m);
        }
    } catch (err) {
        console.error('Failed to load billing months:', err);
    }
}

async function exportData() {
    const btn = document.getElementById('btnExport');
    btn.disabled = true;
    btn.textContent = '导出中...';
    try {
        const result = await proxyFetch('/api/proxy/export', { method: 'POST' });
        showToast('导出成功');
        try { await fetchRefresh(); } catch (e) { /* ignore */ }
    } catch (err) {
        showToast('导出失败: ' + err.message, 'error');
    }
    btn.disabled = false;
    btn.textContent = '导出数据';
}

// ── Sync ─────────────────────────────────────────────────────────────

async function openSyncConfig() {
    try {
        const cfg = await proxyFetch('/api/proxy/sync/config');
        document.getElementById('syncBaseUrl').value = cfg.base_url || '';
        document.getElementById('syncFolder').value = cfg.folder || 'token-board-sync';
        document.getElementById('syncUsername').value = cfg.username || '';
        document.getElementById('syncPassword').value = cfg.has_password ? '••••••' : '';
    } catch (e) {
        /* use empty form */
    }
    openModal('syncModal');
}

async function saveSyncConfig(e) {
    e.preventDefault();
    const form = e.target;
    const data = Object.fromEntries(new FormData(form));
    try {
        await proxyFetch('/api/proxy/sync/config', {
            method: 'PUT',
            body: JSON.stringify(data),
        });
        showToast('同步配置已保存');
        closeModal('syncModal');
    } catch (err) {
        showToast(err.message, 'error');
    }
}

async function testSyncConnection() {
    const btn = document.getElementById('btnTestSync');
    btn.disabled = true;
    btn.textContent = '测试中...';
    try {
        const data = Object.fromEntries(new FormData(document.getElementById('syncConfigForm')));
        const result = await proxyFetch('/api/proxy/sync/test', {
            method: 'POST',
            body: JSON.stringify(data),
        });
        showToast(result.message, result.status === 'ok' ? 'success' : 'error');
    } catch (err) {
        showToast('测试失败: ' + err.message, 'error');
    }
    btn.disabled = false;
    btn.textContent = '测试连接';
}

function initBillingPage() {
    const el = document.getElementById('page-billing');
    if (!el || el.dataset.initialized) return;
    el.dataset.initialized = '1';

    el.innerHTML = `
        <div class="page-header">
            <div style="display:flex; justify-content:space-between; align-items:flex-start;">
                <div>
                    <h1 class="page-title">消费报告</h1>
                    <p class="page-subtitle">代理转发请求的消费概况</p>
                </div>
                <div class="controls-group">
                    <button class="btn btn--sm" onclick="openSyncConfig()" title="同步设置">⚙</button>
                </div>
            </div>
        </div>

        <!-- Stats Cards -->
        <div class="stats-grid" style="grid-template-columns: repeat(3, 1fr);">
            <div class="stat-card stat-card--highlight">
                <div class="stat-card__label"><span class="icon-dot" style="background:#0070F3;"></span> 总Token</div>
                <div class="stat-card__value number-lg" id="billTotalTokens">--</div>
                <div class="stat-card__sub">跨所有账户</div>
            </div>
            <div class="stat-card">
                <div class="stat-card__label"><span class="icon-dot" style="background:#8B5CF6;"></span> 总请求数</div>
                <div class="stat-card__value number-lg" id="billTotalRequests">--</div>
                <div class="stat-card__sub">今日: <span id="billTodayRequests">--</span></div>
            </div>
            <div class="stat-card stat-card--cyan">
                <div class="stat-card__label"><span class="icon-dot" style="background:#EF4444;"></span> 总消费（实际）</div>
                <div class="stat-card__value number-lg" id="billTotalCost">--</div>
                <div class="stat-card__sub">今日消费（理论）: <span id="billTodayCost">--</span> · <span id="billActiveKeys">0</span> 个活跃密钥</div>
            </div>
        </div>

        <!-- Daily Billing Chart + Export -->
        <div class="section">
            <div class="chart-card">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
                    <div class="chart-card__title" style="margin-bottom:0;">每日用量</div>
                    <div class="controls-group">
                        <select class="select-styled" id="billingMonthSelector" onchange="onBillingMonthChange()">
                            <option value="">-- 选择月份 --</option>
                        </select>
                        <button class="btn btn--sm" id="btnExport" onclick="exportData()">导出数据</button>
                    </div>
                </div>
                <div class="chart-container chart-container--lg" id="chartBillingDaily"></div>
            </div>
        </div>
    `;

    loadBillingStats();
    populateBillingMonthSelector();

    // Append sync modal to body (shared, outside page container)
    if (!document.getElementById('syncModal')) {
        const modal = document.createElement('div');
        modal.id = 'syncModal';
        modal.className = 'modal-overlay';
        modal.style.display = 'none';
        modal.innerHTML = `
            <div class="modal">
                <div class="modal__header">
                    <h3>WebDAV 同步设置</h3>
                    <button class="modal__close" onclick="closeModal('syncModal')">&times;</button>
                </div>
                <form id="syncConfigForm" onsubmit="saveSyncConfig(event)">
                    <label>WebDAV 服务器地址 <input name="base_url" id="syncBaseUrl" required placeholder="https://dav.example.com/remote.php/dav/files/user"></label>
                    <label>同步文件夹 <input name="folder" id="syncFolder" value="token-board-sync" placeholder="token-board-sync"></label>
                    <label>用户名 <input name="username" id="syncUsername" required></label>
                    <label>密码 <input name="password" id="syncPassword" type="password" placeholder="留空不变"></label>
                    <div style="display:flex; gap:8px;">
                        <button type="submit" class="btn btn--primary">保存配置</button>
                        <button type="button" class="btn btn--sm" id="btnTestSync" onclick="testSyncConnection()">测试连接</button>
                    </div>
                </form>
            </div>
        `;
        document.body.appendChild(modal);
    }
}

// ── Logs Page ────────────────────────────────────────────────────────────

let logsPage = 1;
let logsFilters = {};

async function loadLogsTable() {
    const tbody = document.querySelector('#logsTable tbody');
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="8" class="td-loading">加载中...</td></tr>';

    const params = new URLSearchParams({
        page: logsPage,
        per_page: 50,
        ...logsFilters,
    });

    try {
        const data = await proxyFetch(`/api/proxy/logs?${params}`);
        document.getElementById('logsPagination').textContent =
            `第 ${data.page} 页 / 共 ${data.total_pages} 页（${data.total} 条记录）`;

        if (!data.items.length) {
            tbody.innerHTML = '<tr><td colspan="8" class="td-empty">暂无日志记录</td></tr>';
            return;
        }

        tbody.innerHTML = data.items.map((r) => `
            <tr>
                <td>${esc(fmtTime(r.requested_at))}</td>
                <td>${esc(r.account_name || `ID:${r.account_id}`)}</td>
                <td><code>${esc(r.model)}</code></td>
                <td>${fmtNum(r.prompt_tokens)} / ${fmtNum(r.cache_read_tokens || 0)} / ${fmtNum(r.completion_tokens)} / ${fmtNum(r.total_tokens)}</td>
                <td>¥${(r.cost || 0).toFixed(4)}</td>
                <td>${r.duration_ms}ms</td>
                <td>${r.is_streaming ? 'SSE' : 'REST'}</td>
                <td><span class="badge ${r.status_code < 300 ? 'badge--active' : 'badge--inactive'}">${r.status_code}</span></td>
            </tr>
        `).join('');

        // Update pagination buttons
        document.getElementById('btnLogsPrev').disabled = data.page <= 1;
        document.getElementById('btnLogsNext').disabled = data.page >= data.total_pages;
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="8" class="td-error">加载失败: ${esc(err.message)}</td></tr>`;
    }
}

function logPageNext() { logsPage++; loadLogsTable(); }
function logPagePrev() { if (logsPage > 1) { logsPage--; loadLogsTable(); } }

function applyLogFilters() {
    logsPage = 1;
    const form = document.getElementById('logsFilterForm');
    const fd = new FormData(form);
    logsFilters = {};
    for (const [k, v] of fd.entries()) {
        if (v) logsFilters[k] = v;
    }
    loadLogsTable();
}

function initLogsPage() {
    const el = document.getElementById('page-proxy-logs');
    if (!el || el.dataset.initialized) return;
    el.dataset.initialized = '1';

    el.innerHTML = `
        <div class="page-header">
            <h1 class="page-title">请求日志</h1>
            <p class="page-subtitle">所有通过代理转发的 API 请求记录</p>
        </div>

        <!-- Filters -->
        <div class="section">
            <form id="logsFilterForm" onsubmit="event.preventDefault(); applyLogFilters();" class="filter-bar">
                <label>日期从 <input type="date" name="from"></label>
                <label>到 <input type="date" name="to"></label>
                <label>模型 <input type="text" name="model" placeholder="模型名称"></label>
                <label>账户ID <input type="number" name="account_id" placeholder="账户ID"></label>
                <button type="submit" class="btn btn--sm">筛选</button>
                <button type="button" class="btn btn--sm" onclick="logsFilters={}; logsPage=1; loadLogsTable();">重置</button>
            </form>
        </div>

        <!-- Log Table -->
        <table class="mgmt-table" id="logsTable">
            <thead><tr><th>时间</th><th>账户</th><th>模型</th><th>Tokens (输入/命中/输出/总计)</th><th>消费</th><th>延迟</th><th>模式</th><th>状态</th></tr></thead>
            <tbody></tbody>
        </table>

        <!-- Pagination -->
        <div class="pagination" style="display:flex; justify-content:center; align-items:center; gap:16px; margin-top:16px;">
            <button class="btn btn--sm" id="btnLogsPrev" onclick="logPagePrev()">上一页</button>
            <span id="logsPagination" style="font-size:13px; color:var(--color-text-secondary);">加载中...</span>
            <button class="btn btn--sm" id="btnLogsNext" onclick="logPageNext()">下一页</button>
        </div>
    `;

    loadLogsTable();
}
