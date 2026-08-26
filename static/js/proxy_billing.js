/**
 * proxy_billing.js — Billing overview and request log viewer pages.
 *
 * Exports: initBillingPage(), initLogsPage()
 * Lazy-loaded by app.js when navigating to #/proxy/billing or #/proxy/logs.
 */

// ── Billing Page ─────────────────────────────────────────────────────────

async function loadBillingStats() {
    try {
        const stats = await proxyApi('/api/proxy/stats');
        document.getElementById('billTotalRequests').textContent = fmtNum(stats.total_requests);
        document.getElementById('billTodayRequests').textContent = fmtNum(stats.today_requests);
        document.getElementById('billTotalCost').textContent = '¥' + (stats.total_cost || 0).toFixed(2);
        document.getElementById('billTodayCost').textContent = '¥' + (stats.today_cost || 0).toFixed(2);
        document.getElementById('billTotalTokens').textContent = fmtNum(stats.total_tokens);
        document.getElementById('billActiveUpstreams').textContent = stats.active_upstreams;
    } catch (err) {
        console.error('Failed to load billing stats:', err);
    }
}

async function loadTodayUpstreamTable() {
    const tbody = document.querySelector('#todayUpstreamTable tbody');
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="5" class="td-loading">加载中...</td></tr>';
    try {
        const rows = await proxyApi('/api/proxy/billing/today-upstreams');
        if (!rows.length) {
            tbody.innerHTML = '<tr><td colspan="5" class="td-empty">今日暂无活跃上游</td></tr>';
            return;
        }
        tbody.innerHTML = rows.map((r) => `
            <tr>
                <td><code>${esc(r.account_name)}</code></td>
                <td>${fmtCost(r.real_cost)}</td>
                <td>${fmtCost(r.theoretical_cost)}</td>
                <td>${fmtNum(r.tokens)}</td>
                <td>${fmtNum(r.requests)}</td>
            </tr>
        `).join('');
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="5" class="td-error">加载失败: ${esc(err.message)}</td></tr>`;
    }
}

let billingChart = null;

/** Date(UTC) → "YYYY-MM-DD"（与后端 date(requested_at) 的 UTC 分桶一致） */
function fmtUtcDateStr(d) {
    return d.getUTCFullYear() + '-' + String(d.getUTCMonth() + 1).padStart(2, '0')
         + '-' + String(d.getUTCDate()).padStart(2, '0');
}

/**
 * 把后端稀疏的按日数据补全为连续 *days* 天的滚动窗口：
 * 窗口结束 = max(今天, 数据最大日期)（防浏览器/服务器时钟偏差），
 * 起点向前扩到数据最早日期，保证不丢任何有消费的天；
 * 缺的天用全 0 记录填充。
 */
function buildDailySeries(data, days) {
    var now = new Date();
    var endStr = fmtUtcDateStr(now);
    var minData = null;
    data.forEach(function(d) {
        if (d.date > endStr) endStr = d.date;
        if (minData === null || d.date < minData) minData = d.date;
    });
    var end = new Date(endStr + 'T00:00:00Z');
    var start = new Date(end.getTime() - (days - 1) * 86400000);
    if (minData !== null && minData < fmtUtcDateStr(start)) {
        start = new Date(minData + 'T00:00:00Z');
    }
    var map = {};
    data.forEach(function(d) { map[d.date] = d; });
    var out = [];
    for (var t = new Date(start.getTime()); t <= end; t = new Date(t.getTime() + 86400000)) {
        var key = fmtUtcDateStr(t);
        var d = map[key];
        out.push(d || {
            date: key, input_tokens: 0, output_tokens: 0,
            cache_hit_tokens: 0, cache_miss_tokens: 0, requests: 0, cost: 0
        });
    }
    return out;
}

async function loadDailyBillingChart() {
    const dom = document.getElementById('chartBillingDaily');
    if (!dom) return;

    try {
        // Rolling 30-day window (no month selection).
        const raw = await proxyApi('/api/proxy/billing/daily-by-model?days=30');

        // Fill the full 30-day window — days without usage show 0.
        const data = buildDailySeries(raw, 30);

        const dates = data.map(d => d.date);
        const inputTokens = data.map(d => d.input_tokens);
        const outputTokens = data.map(d => d.output_tokens);
        const costs = data.map(d => d.cost);

        if (typeof echarts !== 'undefined') {
            if (billingChart) billingChart.dispose();
            billingChart = echarts.init(dom);
            billingChart.setOption(Object.assign({
                tooltip: Object.assign({
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
                }, (typeof TOOLTIP_STYLE !== 'undefined' ? TOOLTIP_STYLE : {})),
                legend: { data: ['输入Token', '输出Token', '消费'], bottom: 0, textStyle: { fontSize: 11, color: '#6E6E73' } },
                grid: { left: 70, right: 70, top: 20, bottom: 50 },
                xAxis: { type: 'category', data: dates, axisLabel: { rotate: 30, fontSize: 10 } },
                yAxis: [
                    { type: 'value', axisLabel: { formatter: v => fmtNum(v), fontSize: 10 } },
                    { type: 'value', axisLabel: { fontSize: 10 } },
                ],
                series: [
                    {
                        name: '输出Token', type: 'bar', stack: 'tokens',
                        data: outputTokens, barMaxWidth: 28,
                        itemStyle: { color: typeof _vGrad !== 'undefined' ? _vGrad('#CF876D', '#B45F45') : '#B45F45' },
                    },
                    {
                        name: '输入Token', type: 'bar', stack: 'tokens',
                        data: inputTokens, barMaxWidth: 28,
                        itemStyle: { color: typeof _vGrad !== 'undefined' ? _vGrad('#A8BF92', '#6E8B77') : '#6E8B77', borderRadius: [4, 4, 0, 0] },
                    },
                    {
                        name: '消费', type: 'line', yAxisIndex: 1,
                        data: costs,
                        lineStyle: { color: '#927CA6', width: 2.25 },
                        itemStyle: { color: '#927CA6', borderColor: '#fffdf9', borderWidth: 1.5 },
                        areaStyle: { color: typeof _vGrad !== 'undefined' ? _vGrad('rgba(146,124,166,0.16)', 'rgba(146,124,166,0)') : 'rgba(146,124,166,0.08)' },
                        symbol: 'circle', symbolSize: 6,
                    },
                ],
            }, (typeof CHART_ANIM !== 'undefined' ? CHART_ANIM : {})));
            new ResizeObserver(() => billingChart.resize()).observe(dom);
        }
    } catch (err) {
        console.error('Failed to load daily billing chart:', err);
    }
}

async function exportData() {
    const btn = document.getElementById('btnExport');
    btn.disabled = true;
    btn.textContent = '导出中...';
    try {
        const result = await proxyApi('/api/proxy/export', { method: 'POST' });
        showToast('导出成功');
        try { await fetchRefresh(); } catch (e) { /* ignore */ }
    } catch (err) {
        showToast('导出失败: ' + err.message, 'error');
    }
    btn.disabled = false;
    btn.textContent = '导出数据';
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
            </div>
        </div>

        <!-- Stats Cards -->
        <div class="stats-grid" style="grid-template-columns: repeat(3, 1fr);">
            <div class="stat-card stat-card--highlight">
                <div class="stat-card__label"><span class="icon-dot" style="background:#B45F45;"></span> 近30天 Token</div>
                <div class="stat-card__value number-lg" id="billTotalTokens">--</div>
                <div class="stat-card__sub">滚动窗口</div>
            </div>
            <div class="stat-card">
                <div class="stat-card__label"><span class="icon-dot" style="background:#927CA6;"></span> 近30天请求数</div>
                <div class="stat-card__value number-lg" id="billTotalRequests">--</div>
                <div class="stat-card__sub">今日: <span id="billTodayRequests">--</span></div>
            </div>
            <div class="stat-card stat-card--cost">
                <div class="stat-card__label"><span class="icon-dot" style="background:#A75558;"></span> 近30天消费（实际）</div>
                <div class="stat-card__value number-lg" id="billTotalCost">--</div>
                <div class="stat-card__sub">今日消费（理论）: <span id="billTodayCost">--</span> · <span id="billActiveUpstreams">0</span> 个活跃上游</div>
            </div>
        </div>

        <!-- Daily Billing Chart + Export -->
        <div class="section">
            <div class="chart-card">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
                    <div class="chart-card__title" style="margin-bottom:0;">每日用量（近30天滚动）</div>
                    <div class="controls-group">
                        <button class="btn btn--sm" id="btnExport" onclick="exportData()">导出数据</button>
                    </div>
                </div>
                <div class="chart-container chart-container--lg" id="chartBillingDaily"></div>
            </div>
        </div>

        <!-- Today's active-upstream usage -->
        <div class="section">
            <div class="chart-card">
                <div class="chart-card__title">今日活跃上游用量</div>
                <div class="table-scroll">
                    <table class="mgmt-table" id="todayUpstreamTable">
                        <thead><tr><th>上游</th><th>实际消费</th><th>理论消费</th><th>Token 数</th><th>调用次数</th></tr></thead>
                        <tbody></tbody>
                    </table>
                </div>
            </div>
        </div>
    `;

    loadBillingStats();
    loadTodayUpstreamTable();
    loadDailyBillingChart();
}

// ── Logs Page ────────────────────────────────────────────────────────────

let logsPage = 1;

async function loadLogsTable() {
    const tbody = document.querySelector('#logsTable tbody');
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="9" class="td-loading">加载中...</td></tr>';

    const params = new URLSearchParams({
        page: logsPage,
        per_page: 50,
    });

    try {
        const data = await proxyApi(`/api/proxy/logs?${params}`);
        document.getElementById('logsPagination').textContent =
            `第 ${data.page} 页 / 共 ${data.total_pages} 页（${data.total} 条记录）`;

        if (!data.items.length) {
            tbody.innerHTML = '<tr><td colspan="9" class="td-empty">暂无日志记录</td></tr>';
            return;
        }

        tbody.innerHTML = data.items.map((r) => `
            <tr>
                <td>${esc(fmtLocal(r.requested_at))}</td>
                <td>${esc(r.account_name || `ID:${r.account_id ?? r.agent_software_id}`)}${r.source_kind === 'agent' ? ' <span class="badge">智能体</span>' : ''}</td>
                <td><code>${esc(r.model)}</code></td>
                <td>${fmtNum(r.prompt_tokens)} / ${fmtNum(r.cache_read_tokens || 0)} / ${fmtNum(r.completion_tokens)} / ${fmtNum(r.total_tokens)}</td>
                <td>¥${(r.cost || 0).toFixed(4)}</td>
                <td>${r.ttft_ms == null ? '—' : `${r.ttft_ms}ms`}</td>
                <td>${r.output_tps == null ? '—' : `${Number(r.output_tps).toFixed(2)} tokens/s`}</td>
                <td>${r.source_kind === 'agent' ? '导入' : (r.is_streaming ? 'SSE' : 'REST')}</td>
                <td><span class="badge ${r.status_code >= 200 && r.status_code < 300 ? 'badge--active' : 'badge--inactive'}">${r.status_code}</span></td>
            </tr>
        `).join('');

        // Update pagination buttons
        document.getElementById('btnLogsPrev').disabled = data.page <= 1;
        document.getElementById('btnLogsNext').disabled = data.page >= data.total_pages;
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="9" class="td-error">加载失败: ${esc(err.message)}</td></tr>`;
    }
}

function logPageNext() { logsPage++; loadLogsTable(); }
function logPagePrev() { if (logsPage > 1) { logsPage--; loadLogsTable(); } }

function initLogsPage() {
    const el = document.getElementById('page-proxy-logs');
    if (!el || el.dataset.initialized) return;
    el.dataset.initialized = '1';

    el.innerHTML = `
        <div class="page-header">
            <h1 class="page-title">请求日志</h1>
            <p class="page-subtitle">代理请求和智能体本地用量记录</p>
        </div>

        <!-- Log Table -->
        <div class="table-scroll">
            <table class="mgmt-table" id="logsTable">
                <thead><tr><th>时间</th><th>账户</th><th>模型</th><th>Tokens (输入/命中/输出/总计)</th><th>消费</th><th>TTFT</th><th>输出速度</th><th>模式</th><th>状态</th></tr></thead>
                <tbody></tbody>
            </table>
        </div>

        <!-- Pagination -->
        <div class="pagination" style="display:flex; justify-content:center; align-items:center; gap:16px; margin-top:16px;">
            <button class="btn btn--sm" id="btnLogsPrev" onclick="logPagePrev()">上一页</button>
            <span id="logsPagination" style="font-size:13px; color:var(--color-text-secondary);">加载中...</span>
            <button class="btn btn--sm" id="btnLogsNext" onclick="logPageNext()">下一页</button>
        </div>
    `;

    loadLogsTable();
}
