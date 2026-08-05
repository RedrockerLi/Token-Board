/**
 * proxy_perf.js — Performance monitoring dashboard.
 *
 * Exports: initPerfPage(), destroyPerfPage()
 * Lazy-loaded by app.js when navigating to #/proxy/perf.
 */

let perfRefreshTimer = null;

// Chart instances for cleanup
let chartLatency = null;
let chartSpeed = null;
let chartRPM = null;
let chartUpstreamSuccess = null;
let chartModelLatency = null;
let chartModelSpeed = null;

function perfEsc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function(ch) {
        return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch];
    });
}

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
                <div class="stat-card__label"><span class="icon-dot" style="background:#F59E0B;"></span> 平均 TTFT</div>
                <div class="stat-card__value number-lg" id="perfAvgLatency">--</div>
                <div class="stat-card__sub">最近 15 分钟 (可观测流式成功请求)</div>
            </div>
        </div>

        <!-- Latency Distribution Chart -->
        <div class="section">
            <div class="chart-card">
                <div class="chart-card__title">请求 TTFT 分布 (P50/P95/P99)</div>
                <div class="chart-container chart-container--lg" id="chartLatency"></div>
            </div>
        </div>

        <!-- Speed Distribution Chart -->
        <div class="section">
            <div class="chart-card">
                <div class="chart-card__title">请求输出速度分布 (P50/P95/P99)</div>
                <div class="chart-container chart-container--lg" id="chartSpeed"></div>
            </div>
        </div>

        <!-- Requests Per Minute -->
        <div class="section">
            <div class="charts-grid charts-grid--2col">
                <div class="chart-card">
                    <div class="chart-card__title">每分钟请求数</div>
                    <div class="chart-container chart-container--lg" id="chartRPM"></div>
                </div>
                <div class="chart-card">
                    <div class="chart-card__title">各上游账户成功率 (最近 1h)</div>
                    <div class="chart-container chart-container--sm" id="chartUpstreamSuccess"></div>
                </div>
            </div>
        </div>

        <!-- Per-Model -->
        <div class="section">
            <div class="charts-grid charts-grid--2col">
                <div class="chart-card">
                    <div class="chart-card__title">各模型平均 TTFT</div>
                    <div class="chart-container chart-container--lg" id="chartModelLatency"></div>
                </div>
                <div class="chart-card">
                    <div class="chart-card__title">各模型平均输出速度</div>
                    <div class="chart-container chart-container--lg" id="chartModelSpeed"></div>
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
    var labels = data.map(function(d) { return fmtUtc8HHMM(d.bucket); }); // HH:MM in UTC+8

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
            data: ['P50', 'P95', 'P99'],
            bottom: 0,
            textStyle: { fontSize: 10 }
        },
        grid: { left: 60, right: 20, top: 20, bottom: 50 },
        xAxis: { type: 'category', data: labels, axisLabel: { rotate: 45, fontSize: 10 } },
        yAxis: { type: 'value', axisLabel: { fontSize: 10 } },
        series: [
            {
                name: 'P50', type: 'line',
                data: data.map(function(d) { return d.p50; }),
                smooth: true, symbol: 'none',
                lineStyle: { color: '#0070F3', width: 2 },
            },
            {
                name: 'P95', type: 'line',
                data: data.map(function(d) { return d.p95; }),
                smooth: true, symbol: 'none',
                lineStyle: { color: '#F59E0B', width: 2 },
            },
            {
                name: 'P99', type: 'line',
                data: data.map(function(d) { return d.p99; }),
                smooth: true, symbol: 'none',
                lineStyle: { color: '#EF4444', width: 1.5 },
            },
        ],
    });
}

function renderSpeedChart(domId, data) {
    chartSpeed = initChart(domId);
    if (!chartSpeed) return;
    if (!data || !data.length) {
        chartSpeed.setOption({
            title: { text: '暂无数据', left: 'center', top: 'center', textStyle: { color: '#999', fontSize: 14 } }
        });
        return;
    }
    var labels = data.map(function(d) { return fmtUtc8HHMM(d.bucket); }); // HH:MM in UTC+8

    chartSpeed.setOption({
        tooltip: {
            trigger: 'axis',
            formatter: function(params) {
                var s = params[0].name + '<br/>';
                params.forEach(function(p) {
                    s += p.marker + ' ' + p.seriesName + ': ' + p.value + ' tokens/s<br/>';
                });
                return s;
            }
        },
        legend: {
            data: ['P50', 'P95', 'P99'],
            bottom: 0,
            textStyle: { fontSize: 10 }
        },
        grid: { left: 60, right: 20, top: 20, bottom: 50 },
        xAxis: { type: 'category', data: labels, axisLabel: { rotate: 45, fontSize: 10 } },
        yAxis: { type: 'value', name: 'tokens/s', axisLabel: { fontSize: 10 } },
        series: [
            {
                name: 'P50', type: 'line',
                data: data.map(function(d) { return d.p50; }),
                smooth: true, symbol: 'none',
                lineStyle: { color: '#0070F3', width: 2 },
            },
            {
                name: 'P95', type: 'line',
                data: data.map(function(d) { return d.p95; }),
                smooth: true, symbol: 'none',
                lineStyle: { color: '#F59E0B', width: 2 },
            },
            {
                name: 'P99', type: 'line',
                data: data.map(function(d) { return d.p99; }),
                smooth: true, symbol: 'none',
                lineStyle: { color: '#EF4444', width: 1.5 },
            },
        ],
    });
}

/** Date(UTC) → "YYYY-MM-DD HH:MM"（与后端 strftime bucket 格式一致） */
function fmtUtcMinuteStr(d) {
    return d.getUTCFullYear() + '-' + String(d.getUTCMonth() + 1).padStart(2, '0')
         + '-' + String(d.getUTCDate()).padStart(2, '0')
         + ' ' + String(d.getUTCHours()).padStart(2, '0')
         + ':' + String(d.getUTCMinutes()).padStart(2, '0');
}

/** "YYYY-MM-DD HH:MM"（UTC）→ 毫秒时间戳 */
function parseUtcMinuteMs(s) {
    return new Date(String(s).replace(' ', 'T') + ':00Z').getTime();
}

/**
 * 把后端稀疏的按分钟数据补全为连续 *minutes* 个 1 分钟桶，
 * 结束于当前 UTC 分钟（被 fmtUtc8HHMM 显示为 UTC+8 的「现在」），
 * 缺的分钟用 0 请求数填充。
 */
function buildThroughputSeries(data, minutes) {
    var now = new Date();
    now.setUTCSeconds(0, 0);
    var end = now.getTime();
    data.forEach(function(d) {
        var t = parseUtcMinuteMs(d.bucket);
        if (!isNaN(t) && t > end) end = t;   // 防时钟偏差丢数据
    });
    var map = {};
    data.forEach(function(d) { map[d.bucket] = d.requests; });
    var out = [];
    for (var i = minutes - 1; i >= 0; i--) {
        var key = fmtUtcMinuteStr(new Date(end - i * 60000));
        out.push({ bucket: key, requests: map[key] || 0 });
    }
    return out;
}

function renderRPMChart(domId, data, minutes) {
    chartRPM = initChart(domId);
    if (!chartRPM) return;
    // Fill the full minute window — minutes without requests show 0.
    data = buildThroughputSeries(data || [], minutes || 60);
    var labels = data.map(function(d) { return fmtUtc8HHMM(d.bucket); });
    var vals = data.map(function(d) { return d.requests; });

    chartRPM.setOption({
        tooltip: { trigger: 'axis' },
        grid: { left: 50, right: 20, top: 20, bottom: 40 },
        xAxis: { type: 'category', data: labels, axisLabel: { rotate: 45, fontSize: 10 } },
        yAxis: { type: 'value', axisLabel: { fontSize: 10 }, minInterval: 1 },
        series: [{
            name: '请求数', type: 'line', data: vals,
            smooth: true, symbol: 'circle', symbolSize: 4,
            lineStyle: { color: '#0070F3', width: 2 },
            itemStyle: { color: '#0070F3' },
            areaStyle: { color: 'rgba(0,112,243,0.08)' },
        }],
    });
}

function renderUpstreamSuccessRateChart(domId, data) {
    chartUpstreamSuccess = initChart(domId);
    if (!chartUpstreamSuccess) return;
    if (!data || !data.length) {
        chartUpstreamSuccess.setOption({
            title: { text: '暂无数据', left: 'center', top: 'center', textStyle: { color: '#999', fontSize: 14 } }
        });
        return;
    }
    // Success rate per real upstream over the last hour — highest volume first.
    var names = data.map(function(d) { return d.account_name; });
    var rates = data.map(function(d) { return d.success_rate; });
    var colors = data.map(function(d) {
        var r = d.success_rate;
        if (r == null) return '#6B7280';
        return r >= 95 ? '#22C55E' : (r >= 80 ? '#F59E0B' : '#EF4444');
    });

    chartUpstreamSuccess.setOption({
        tooltip: {
            trigger: 'item',
            formatter: function(params) {
                var d = data[params.dataIndex];
                return perfEsc(d.account_name) + '<br/>成功率: <b>' + d.success_rate + '%</b><br/>'
                    + '总请求: ' + d.total + ' | 失败: ' + d.errors;
            }
        },
        grid: { left: 50, right: 30, top: 30, bottom: 40 },
        xAxis: { type: 'category', data: names, axisLabel: { rotate: 30, fontSize: 10 } },
        yAxis: { type: 'value', axisLabel: { fontSize: 10 }, min: 0, max: 100 },
        series: [{
            name: '成功率', type: 'bar',
            data: rates.map(function(v, i) {
                return { value: v, itemStyle: { color: colors[i] } };
            }),
            barMaxWidth: 28,
            label: {
                show: true, position: 'top', fontSize: 10,
                formatter: function(p) { return p.value + '%'; },
            },
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
    // Sort by observed TTFT descending.
    models = models.filter(function(m) { return m.avg_ttft_ms != null; });
    if (!models.length) {
        chartModelLatency.setOption({
            title: { text: '暂无可观测流式 TTFT 数据', left: 'center', top: 'center', textStyle: { color: '#999', fontSize: 14 } }
        });
        return;
    }
    models.sort(function(a, b) { return b.avg_ttft_ms - a.avg_ttft_ms; });
    var names = models.map(function(m) { return m.model; });
    var latencies = models.map(function(m) { return m.avg_ttft_ms; });
    var colors = models.map(function(_, i) { return (typeof chartColors !== 'undefined' ? chartColors : ['#0070F3','#00CEF3','#22C55E','#F59E0B','#8B5CF6','#EF4444','#EC4899','#6366F1'])[i % 8]; });

    chartModelLatency.setOption({
        tooltip: {
            trigger: 'axis',
            axisPointer: { type: 'shadow' },
            formatter: function(params) {
                var m = models[params[0].dataIndex];
                return perfEsc(m.model) + '<br/>' + params[0].marker + ' TTFT: <b>'
                    + params[0].value + ' ms</b><br/>样本数: ' + m.ttft_samples;
            }
        },
        grid: { left: 50, right: 20, top: 10, bottom: 60 },
        xAxis: { type: 'category', data: names, axisLabel: { rotate: 30, fontSize: 10 } },
        yAxis: { type: 'value', axisLabel: { fontSize: 10 } },
        series: [{
            name: 'TTFT', type: 'bar',
            data: latencies.map(function(v, i) {
                return { value: v, itemStyle: { color: colors[i] } };
            }),
            barMaxWidth: 28,
        }],
    });
}

function renderModelSpeedChart(domId, models) {
    chartModelSpeed = initChart(domId);
    if (!chartModelSpeed) return;
    models = (models || []).filter(function(m) { return m.avg_output_tps != null; });
    if (!models.length) {
        chartModelSpeed.setOption({
            title: { text: '暂无可观测流式速度数据', left: 'center', top: 'center', textStyle: { color: '#999', fontSize: 14 } }
        });
        return;
    }
    models.sort(function(a, b) { return b.avg_output_tps - a.avg_output_tps; });
    var colors = models.map(function(_, i) { return (typeof chartColors !== 'undefined' ? chartColors : ['#0070F3','#00CEF3','#22C55E','#F59E0B','#8B5CF6','#EF4444','#EC4899','#6366F1'])[i % 8]; });
    chartModelSpeed.setOption({
        tooltip: {
            trigger: 'axis', axisPointer: { type: 'shadow' },
            formatter: function(params) {
                var m = models[params[0].dataIndex];
                return perfEsc(m.model) + '<br/>' + params[0].marker + ' 输出速度: <b>'
                    + params[0].value.toFixed(2) + ' tokens/s</b><br/>样本数: ' + m.speed_samples;
            }
        },
        grid: { left: 55, right: 20, top: 10, bottom: 60 },
        xAxis: { type: 'category', data: models.map(function(m) { return m.model; }), axisLabel: { rotate: 30, fontSize: 10 } },
        yAxis: { type: 'value', name: 'tokens/s', axisLabel: { fontSize: 10 } },
        series: [{
            name: '输出速度', type: 'bar', barMaxWidth: 28,
            data: models.map(function(m, i) { return { value: m.avg_output_tps, itemStyle: { color: colors[i] } }; }),
        }],
    });
}

// ── Data Loading ───────────────────────────────────────────────────────

async function loadAllPerfData() {
    try {
        var results = await Promise.all([
            fetchPerfSummary(15),
            fetchPerfLatency(60),
            fetchPerfSpeed(60),
            fetchPerfThroughput(60),
            fetchPerfModels(60),
            fetchPerfRealtime(),
            fetchPerfUpstreamSuccessRate(60),
        ]);
        var summary = results[0];
        var latency = results[1];
        var speed = results[2];
        var throughput = results[3];
        var models = results[4];
        var realtime = results[5];
        var upstreamSuccess = results[6];

        // Update stat cards
        var elConcurrent = document.getElementById('perfConcurrent');
        var elRPM = document.getElementById('perfRPM');
        var elSuccess = document.getElementById('perfSuccessRate');
        var elLatency = document.getElementById('perfAvgLatency');

        if (elConcurrent) {
            elConcurrent.textContent = realtime.latest_concurrent == null
                ? '--' : realtime.latest_concurrent;
        }
        if (elRPM) elRPM.textContent = realtime.rpm;
        if (elSuccess) elSuccess.textContent = (summary.success_rate || 0) + '%';
        if (elLatency) {
            elLatency.textContent = summary.avg_ttft_ms != null
                ? summary.avg_ttft_ms + ' ms' : '--';
        }

        // Render charts
        renderLatencyChart('chartLatency', latency);
        renderSpeedChart('chartSpeed', speed);
        renderRPMChart('chartRPM', throughput, 60);
        renderUpstreamSuccessRateChart('chartUpstreamSuccess', upstreamSuccess);
        renderModelLatencyChart('chartModelLatency', models);
        renderModelSpeedChart('chartModelSpeed', models);
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
    [chartLatency, chartSpeed, chartRPM, chartUpstreamSuccess, chartModelLatency, chartModelSpeed].forEach(function(c) {
        if (c) { c.dispose(); }
    });
    chartLatency = chartSpeed = chartRPM = chartUpstreamSuccess = chartModelLatency = chartModelSpeed = null;

    // Clean up DOM
    var el = document.getElementById('page-proxy-perf');
    if (el) {
        el.innerHTML = '';
        delete el.dataset.initialized;
    }
}
