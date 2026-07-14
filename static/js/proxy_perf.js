/**
 * proxy_perf.js — Performance monitoring dashboard.
 *
 * Exports: initPerfPage(), destroyPerfPage()
 * Lazy-loaded by app.js when navigating to #/proxy/perf.
 */

let perfRefreshTimer = null;

// Chart instances for cleanup
let chartLatency = null;
let chartRPM = null;
let chartUtilization = null;
let chartSuccessRate = null;
let chartModelLatency = null;

// ── Page HTML Builder ──────────────────────────────────────────────────

function buildPerfPageHTML() {
    return `
        <div class="page-header">
            <h1 class="page-title">代理性能监控</h1>
            <p class="page-subtitle">实时性能指标 · 每 15 秒自动刷新</p>
        </div>

        <!-- Stat Cards -->
        <div class="stats-grid" style="grid-template-columns:repeat(4, 1fr);">
            <div class="stat-card stat-card--highlight">
                <div class="stat-card__label"><span class="icon-dot" style="background:#0070F3;"></span> 当前并发</div>
                <div class="stat-card__value number-lg" id="perfConcurrent">--</div>
                <div class="stat-card__sub">处理中的请求数</div>
            </div>
            <div class="stat-card">
                <div class="stat-card__label"><span class="icon-dot" style="background:#8B5CF6;"></span> 请求速率</div>
                <div class="stat-card__value number-lg" id="perfRPM">--</div>
                <div class="stat-card__sub">最近 1 分钟 (RPM)</div>
            </div>
            <div class="stat-card">
                <div class="stat-card__label"><span class="icon-dot" style="background:#22C55E;"></span> 成功率</div>
                <div class="stat-card__value number-lg" id="perfSuccessRate">--</div>
                <div class="stat-card__sub">最近 15 分钟</div>
            </div>
            <div class="stat-card stat-card--cyan">
                <div class="stat-card__label"><span class="icon-dot" style="background:#F59E0B;"></span> 平均延迟</div>
                <div class="stat-card__value number-lg" id="perfAvgLatency">--</div>
                <div class="stat-card__sub">最近 15 分钟 (成功请求)</div>
            </div>
        </div>

        <!-- Latency Distribution Chart -->
        <div class="section">
            <div class="chart-card">
                <div class="chart-card__title">TTFT 首Token延迟分布</div>
                <div class="chart-container chart-container--lg" id="chartLatency"></div>
            </div>
        </div>

        <!-- Requests Per Minute + Proxy Utilization -->
        <div class="section">
            <div class="charts-grid charts-grid--2col">
                <div class="chart-card">
                    <div class="chart-card__title">每分钟请求数</div>
                    <div class="chart-container chart-container--lg" id="chartRPM"></div>
                </div>
                <div class="chart-card">
                    <div class="chart-card__title">代理占用率</div>
                    <div class="chart-container chart-container--lg" id="chartUtilization"></div>
                </div>
            </div>
        </div>

        <!-- Success Rate + Per-Model -->
        <div class="section">
            <div class="charts-grid charts-grid--2col">
                <div class="chart-card">
                    <div class="chart-card__title">请求成功率</div>
                    <div class="chart-container chart-container--sm" id="chartSuccessRate"></div>
                </div>
                <div class="chart-card">
                    <div class="chart-card__title">各模型平均 TTFT</div>
                    <div class="chart-container chart-container--lg" id="chartModelLatency"></div>
                </div>
            </div>
        </div>
    `;
}

// ── Chart Renderers ────────────────────────────────────────────────────

function renderLatencyChart(domId, data) {
    chartLatency = initChart(domId);
    if (!chartLatency) return;
    if (!data || !data.length) {
        chartLatency.setOption({
            title: { text: '暂无数据', left: 'center', top: 'center', textStyle: { color: '#999', fontSize: 14 } }
        });
        return;
    }
    var labels = data.map(function(d) { return d.bucket.substring(11); }); // HH:MM only

    chartLatency.setOption({
        tooltip: {
            trigger: 'axis',
            formatter: function(params) {
                var s = params[0].name + '<br/>';
                params.forEach(function(p) {
                    s += p.marker + ' ' + p.seriesName + ': ' + p.value + ' ms<br/>';
                });
                return s;
            }
        },
        legend: {
            data: ['P50-上游', 'P50-代理', 'P95-上游', 'P95-代理', 'P99-上游', 'P99-代理'],
            bottom: 0,
            textStyle: { fontSize: 10 }
        },
        grid: { left: 60, right: 20, top: 20, bottom: 50 },
        xAxis: { type: 'category', data: labels, axisLabel: { rotate: 45, fontSize: 10 } },
        yAxis: { type: 'value', name: '延迟 (ms)', axisLabel: { fontSize: 10 } },
        series: [
            {
                name: 'P50-上游', type: 'line',
                data: data.map(function(d) { return d.p50_upstream; }),
                smooth: true, symbol: 'none',
                lineStyle: { color: '#0070F3', width: 2, type: 'dashed' },
            },
            {
                name: 'P50-代理', type: 'line',
                data: data.map(function(d) { return d.p50_total; }),
                smooth: true, symbol: 'none',
                lineStyle: { color: '#0070F3', width: 2 },
            },
            {
                name: 'P95-上游', type: 'line',
                data: data.map(function(d) { return d.p95_upstream; }),
                smooth: true, symbol: 'none',
                lineStyle: { color: '#F59E0B', width: 2, type: 'dashed' },
            },
            {
                name: 'P95-代理', type: 'line',
                data: data.map(function(d) { return d.p95_total; }),
                smooth: true, symbol: 'none',
                lineStyle: { color: '#F59E0B', width: 2 },
            },
            {
                name: 'P99-上游', type: 'line',
                data: data.map(function(d) { return d.p99_upstream; }),
                smooth: true, symbol: 'none',
                lineStyle: { color: '#EF4444', width: 1.5, type: 'dashed' },
            },
            {
                name: 'P99-代理', type: 'line',
                data: data.map(function(d) { return d.p99_total; }),
                smooth: true, symbol: 'none',
                lineStyle: { color: '#EF4444', width: 1.5 },
            },
        ],
    });
}

function renderRPMChart(domId, data) {
    chartRPM = initChart(domId);
    if (!chartRPM) return;
    if (!data || !data.length) {
        chartRPM.setOption({
            title: { text: '暂无数据', left: 'center', top: 'center', textStyle: { color: '#999', fontSize: 14 } }
        });
        return;
    }
    var labels = data.map(function(d) { return d.bucket.substring(11); });
    var vals = data.map(function(d) { return d.requests; });

    chartRPM.setOption({
        tooltip: { trigger: 'axis' },
        grid: { left: 50, right: 20, top: 20, bottom: 40 },
        xAxis: { type: 'category', data: labels, axisLabel: { rotate: 45, fontSize: 10 } },
        yAxis: { type: 'value', name: '请求/分钟', axisLabel: { fontSize: 10 }, minInterval: 1 },
        series: [{
            name: '请求数', type: 'line', data: vals,
            smooth: true, symbol: 'circle', symbolSize: 4,
            lineStyle: { color: '#0070F3', width: 2 },
            itemStyle: { color: '#0070F3' },
            areaStyle: { color: 'rgba(0,112,243,0.08)' },
        }],
    });
}

function renderUtilizationChart(domId, data) {
    chartUtilization = initChart(domId);
    if (!chartUtilization) return;
    if (!data || !data.length) {
        chartUtilization.setOption({
            title: { text: '暂无数据', left: 'center', top: 'center', textStyle: { color: '#999', fontSize: 14 } }
        });
        return;
    }
    var labels = data.map(function(d) { return d.bucket.substring(11); });
    var concurrent = data.map(function(d) { return d.peak_concurrent; });

    chartUtilization.setOption({
        tooltip: {
            trigger: 'axis',
            formatter: function(params) {
                var p = params[0];
                return p.name + '<br/>峰值并发: ' + p.value + ' / 128';
            }
        },
        grid: { left: 50, right: 20, top: 20, bottom: 40 },
        xAxis: { type: 'category', data: labels, axisLabel: { rotate: 45, fontSize: 10 } },
        yAxis: { type: 'value', name: '并发数', axisLabel: { fontSize: 10 }, minInterval: 1 },
        series: [{
            name: '峰值并发', type: 'line', data: concurrent,
            smooth: true, symbol: 'circle', symbolSize: 4,
            lineStyle: { color: '#8B5CF6', width: 2 },
            itemStyle: { color: '#8B5CF6' },
            areaStyle: { color: 'rgba(139,92,246,0.1)' },
        }],
    });
}
function renderSuccessRateChart(domId, summary) {
    chartSuccessRate = initChart(domId);
    if (!chartSuccessRate) return;
    var total = summary.total_requests || 0;
    var errors = summary.error_count || 0;
    var success = total - errors;
    if (total === 0) {
        chartSuccessRate.setOption({
            title: { text: '暂无数据', left: 'center', top: 'center', textStyle: { color: '#999', fontSize: 14 } }
        });
        return;
    }
    chartSuccessRate.setOption({
        tooltip: {
            trigger: 'item',
            formatter: function(p) { return p.name + ': ' + fmtNum(p.value) + ' (' + p.percent + '%)'; }
        },
        series: [{
            type: 'pie',
            radius: ['50%', '75%'],
            center: ['50%', '50%'],
            data: [
                { name: '成功', value: success, itemStyle: { color: '#22C55E' } },
                { name: '失败', value: errors, itemStyle: { color: '#EF4444' } },
            ],
            label: { show: true, formatter: '{b}', fontSize: 12 },
        }],
    });
}

function renderModelLatencyChart(domId, models) {
    chartModelLatency = initChart(domId);
    if (!chartModelLatency) return;
    if (!models || !models.length) {
        chartModelLatency.setOption({
            title: { text: '暂无数据', left: 'center', top: 'center', textStyle: { color: '#999', fontSize: 14 } }
        });
        return;
    }
    // Sort by latency descending
    models.sort(function(a, b) { return b.avg_latency_ms - a.avg_latency_ms; });
    var names = models.map(function(m) { return m.model; });
    var latencies = models.map(function(m) { return m.avg_latency_ms; });
    var colors = models.map(function(_, i) { return (typeof chartColors !== 'undefined' ? chartColors : ['#0070F3','#00CEF3','#22C55E','#F59E0B','#8B5CF6','#EF4444','#EC4899','#6366F1'])[i % 8]; });

    chartModelLatency.setOption({
        tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
        grid: { left: 50, right: 20, top: 10, bottom: 60 },
        xAxis: { type: 'category', data: names, axisLabel: { rotate: 30, fontSize: 10 } },
        yAxis: { type: 'value', name: 'TTFT (ms)', axisLabel: { fontSize: 10 } },
        series: [{
            name: 'TTFT', type: 'bar',
            data: latencies.map(function(v, i) {
                return { value: v, itemStyle: { color: colors[i] } };
            }),
            barMaxWidth: 28,
        }],
    });
}

// ── Data Loading ───────────────────────────────────────────────────────

async function loadAllPerfData() {
    try {
        var results = await Promise.all([
            fetchPerfSummary(15),
            fetchPerfLatency(60),
            fetchPerfThroughput(60),
            fetchPerfModels(60),
            fetchPerfRealtime(),
        ]);
        var summary = results[0];
        var latency = results[1];
        var throughput = results[2];
        var models = results[3];
        var realtime = results[4];

        // Update stat cards
        var elConcurrent = document.getElementById('perfConcurrent');
        var elRPM = document.getElementById('perfRPM');
        var elSuccess = document.getElementById('perfSuccessRate');
        var elLatency = document.getElementById('perfAvgLatency');

        if (elConcurrent) elConcurrent.textContent = realtime.latest_concurrent;
        if (elRPM) elRPM.textContent = realtime.rpm;
        if (elSuccess) elSuccess.textContent = (summary.success_rate || 0) + '%';
        if (elLatency) elLatency.textContent = (summary.avg_latency_ms || 0) + ' ms';

        // Render charts
        renderLatencyChart('chartLatency', latency);
        renderRPMChart('chartRPM', throughput);
        renderUtilizationChart('chartUtilization', throughput);
        renderSuccessRateChart('chartSuccessRate', summary);
        renderModelLatencyChart('chartModelLatency', models);
    } catch (err) {
        console.error('Failed to load perf data:', err);
    }
}

// ── Init / Destroy (Exports) ───────────────────────────────────────────

function initPerfPage() {
    var el = document.getElementById('page-proxy-perf');
    if (!el || el.dataset.initialized) return;
    el.dataset.initialized = '1';

    el.innerHTML = buildPerfPageHTML();

    loadAllPerfData();

    // Auto-refresh every 15 seconds
    if (perfRefreshTimer) clearInterval(perfRefreshTimer);
    perfRefreshTimer = setInterval(loadAllPerfData, 15000);
}

function destroyPerfPage() {
    if (perfRefreshTimer) {
        clearInterval(perfRefreshTimer);
        perfRefreshTimer = null;
    }
    // Dispose chart instances to avoid memory leaks
    [chartLatency, chartRPM, chartUtilization, chartSuccessRate, chartModelLatency].forEach(function(c) {
        if (c) { c.dispose(); }
    });
    chartLatency = chartRPM = chartUtilization = chartSuccessRate = chartModelLatency = null;

    // Clean up DOM
    var el = document.getElementById('page-proxy-perf');
    if (el) {
        el.innerHTML = '';
        delete el.dataset.initialized;
    }
}
