/**
 * dashboard.js — Application orchestration layer.
 *
 * Global state, data-loading functions, DOM event handlers, and initialisation.
 * Depends on: api.js (fmtNum, fmtCost, fetchJSON, buildParams, fetch* wrappers)
 *             charts.js (initChart, renderTimeSeriesChart, renderPieChart, chartColors)
 */

// ── Global state ──
var currentMonth = null;       // { year: number, month: number }
var currentKeyName = '';       // '' = overview (all users)
var summaryData = null;        // cached /api/summary response

var dailyProRawData = [];
var dailyFlashRawData = [];
var monthlyProRawData = [];
var monthlyFlashRawData = [];

// ── Subtitle ──

function updateSubtitle() {
    var el = document.getElementById('pageSubtitle');
    if (currentKeyName) {
        el.textContent = '筛选: ' + currentKeyName + ' · 费用按Token比例分摊 · 所有日期均按 UTC+0 时间显示';
    } else {
        el.textContent = '总览 (所有用户) · 所有日期均按 UTC+0 时间显示 · Token 统计跨所有可用月份';
    }
}

// ── Selector population ──

function populateMonthSelector(months) {
    var sel = document.getElementById('monthSelector');
    var prevMonthVal = sel.value;
    sel.innerHTML = '<option value="">-- 选择月份 --</option>';
    // Most recent first
    months.slice().reverse().forEach(function (m) {
        var opt = document.createElement('option');
        opt.value = m.year + '-' + m.month;
        opt.textContent = m.year + ' - ' + m.month + '月';
        sel.appendChild(opt);
    });
    return prevMonthVal;
}

function populateKeyNameSelector(keyNames) {
    var keySel = document.getElementById('keyNameSelector');
    var prevKeyVal = keySel.value;
    keySel.innerHTML = '<option value="">总览 (所有用户)</option>';
    keyNames.forEach(function (name) {
        var opt = document.createElement('option');
        opt.value = name;
        opt.textContent = name;
        keySel.appendChild(opt);
    });
    return prevKeyVal;
}

// ── Summary loader ──

async function loadSummary() {
    var data = await fetchSummary();
    summaryData = data;

    document.getElementById('statTotalTokens').textContent = fmtNum(data.total_tokens);
    document.getElementById('statOutputTokens').textContent = fmtNum(data.total_output_tokens);
    document.getElementById('statInputTokens').textContent = fmtNum(data.total_input_tokens);
    document.getElementById('statCacheHitTokens').textContent = fmtNum(data.total_input_cache_hit_tokens);
    document.getElementById('statRequests').textContent = fmtNum(data.total_requests);
    document.getElementById('statCost').textContent = fmtCost(data.total_cost);

    var months = data.available_months || [];
    var keyNames = data.api_key_names || [];

    // Populate selectors (preserve previous selection when possible)
    var prevMonthVal = populateMonthSelector(months);
    var prevKeyVal = populateKeyNameSelector(keyNames);

    // Restore key name selection
    var keySel = document.getElementById('keyNameSelector');
    if (prevKeyVal && Array.from(keySel.options).some(function (o) { return o.value === prevKeyVal; })) {
        keySel.value = prevKeyVal;
    } else {
        keySel.value = '';
        currentKeyName = '';
    }

    // Restore / default month selection
    var sel = document.getElementById('monthSelector');
    if (months.length > 0) {
        var latest = months[months.length - 1];
        if (!prevMonthVal || !Array.from(sel.options).some(function (o) { return o.value === prevMonthVal; })) {
            sel.value = latest.year + '-' + latest.month;
            currentMonth = { year: latest.year, month: latest.month };
        } else {
            sel.value = prevMonthVal;
            var parts = prevMonthVal.split('-').map(Number);
            currentMonth = { year: parts[0], month: parts[1] };
        }
        var label = currentMonth.year + '-' + String(currentMonth.month).padStart(2, '0');
        document.getElementById('currentMonthLabel').textContent = '当前显示: ' + label;
        loadDailyCharts();
    }

    updateSubtitle();

    document.getElementById('lastUpdated').textContent =
        '数据更新时间: ' + new Date().toLocaleString('zh-CN') + ' · 共 ' + months.length + ' 个月数据';
}

// ── Daily charts ──

async function loadDailyChartForModel(modelName, chartId, loaderId) {
    var dom = document.getElementById(chartId);
    var loader = document.getElementById(loaderId);
    loader.style.display = 'flex';
    dom.style.display = 'none';

    if (!currentMonth) {
        loader.style.display = 'none';
        dom.style.display = 'block';
        return;
    }

    try {
        var data = await fetchDaily(currentMonth.year, currentMonth.month, modelName);
        var days = data.days || [];

        if (modelName === 'deepseek-v4-pro') dailyProRawData = days;
        else dailyFlashRawData = days;

        var labels = days.map(function (d) { return d.date; });
        var outputVals = days.map(function (d) { return d.output_tokens; });
        var inputVals = days.map(function (d) { return d.input_tokens; });
        var requestVals = days.map(function (d) { return d.requests; });
        var costVals = days.map(function (d) { return d.cost; });

        renderTimeSeriesChart(chartId, loaderId, labels, outputVals, inputVals,
                              requestVals, costVals, days, 0);
    } catch (err) {
        console.error('Failed to load daily chart for ' + modelName + ':', err);
        loader.textContent = '加载失败';
    }
}

async function loadDailyCharts() {
    await Promise.all([
        loadDailyChartForModel('deepseek-v4-pro', 'chartDailyPro', 'loadingDailyPro'),
        loadDailyChartForModel('deepseek-v4-flash', 'chartDailyFlash', 'loadingDailyFlash')
    ]);
}

// ── Pie charts ──

async function loadModelPie() {
    var loader = document.getElementById('loadingModelPie');
    try {
        var data = await fetchSummary();
        var breakdown = data.model_breakdown || {};
        var pieData = Object.entries(breakdown)
            .map(function (entry) { return { name: entry[0], value: entry[1].total_tokens }; })
            .filter(function (d) { return d.value > 0; })
            .sort(function (a, b) { return b.value - a.value; });

        renderPieChart('chartModelPie', pieData, chartColors);
        loader.style.display = 'none';
    } catch (err) {
        console.error('Failed to load model pie:', err);
        loader.textContent = '加载失败';
    }
}

async function loadTypePie() {
    var loader = document.getElementById('loadingTypePie');
    try {
        var data = await fetchTokenTypes();
        var filtered = data.filter(function (d) { return d.value > 0; });
        renderPieChart('chartTypePie', filtered, ['#0070F3', '#00CEF3', '#F59E0B']);
        loader.style.display = 'none';
    } catch (err) {
        console.error('Failed to load type pie:', err);
        loader.textContent = '加载失败';
    }
}

// ── Monthly trend charts ──

async function loadMonthlyTrendForModel(modelName, chartId, loaderId) {
    var dom = document.getElementById(chartId);
    var loader = document.getElementById(loaderId);
    loader.style.display = 'flex';
    dom.style.display = 'none';

    try {
        var data = await fetchMonthly(modelName);

        if (modelName === 'deepseek-v4-pro') monthlyProRawData = data;
        else monthlyFlashRawData = data;

        var labels = data.map(function (d) { return d.label; });
        var outputVals = data.map(function (d) { return d.output_tokens; });
        var inputVals = data.map(function (d) { return d.input_tokens; });
        var requestVals = data.map(function (d) { return d.requests; });
        var costVals = data.map(function (d) { return d.cost; });

        renderTimeSeriesChart(chartId, loaderId, labels, outputVals, inputVals,
                              requestVals, costVals, data, 0);
    } catch (err) {
        console.error('Failed to load monthly trend for ' + modelName + ':', err);
        loader.textContent = '加载失败';
    }
}

async function loadMonthlyCharts() {
    await Promise.all([
        loadMonthlyTrendForModel('deepseek-v4-pro', 'chartMonthlyPro', 'loadingMonthlyPro'),
        loadMonthlyTrendForModel('deepseek-v4-flash', 'chartMonthlyFlash', 'loadingMonthlyFlash')
    ]);
}

// ── Event handlers ──

document.getElementById('monthSelector').addEventListener('change', function () {
    var val = this.value;
    if (!val) {
        currentMonth = null;
        document.getElementById('currentMonthLabel').textContent = '';
        ['chartDailyPro', 'chartDailyFlash'].forEach(function (id) {
            document.getElementById(id).style.display = 'none';
        });
        ['loadingDailyPro', 'loadingDailyFlash'].forEach(function (id) {
            document.getElementById(id).style.display = 'flex';
            document.getElementById(id).textContent = '请选择月份';
        });
        return;
    }
    var parts = val.split('-').map(Number);
    currentMonth = { year: parts[0], month: parts[1] };
    var label = parts[0] + '-' + String(parts[1]).padStart(2, '0');
    document.getElementById('currentMonthLabel').textContent = '当前显示: ' + label;
    loadDailyCharts();
});

document.getElementById('keyNameSelector').addEventListener('change', function () {
    currentKeyName = this.value;
    loadSummary();
    loadModelPie();
    loadTypePie();
    loadMonthlyCharts();
    if (currentMonth) loadDailyCharts();
});

// ── Refresh ──

async function refreshData() {
    try {
        await fetchRefresh();
        await loadSummary();
        await loadModelPie();
        await loadTypePie();
        await loadMonthlyCharts();
        if (currentMonth) await loadDailyCharts();
    } catch (err) {
        console.error('Refresh failed:', err);
        alert('刷新失败，请检查服务器日志');
    }
}

// ── Initialise ──

async function init() {
    await loadSummary();
    await loadModelPie();
    await loadTypePie();
    await loadMonthlyCharts();
}

init();
