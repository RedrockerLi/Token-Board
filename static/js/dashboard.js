/**
 * dashboard.js — Application orchestration layer.
 *
 * Global state, data-loading functions, DOM event handlers, and initialisation.
 * Depends on: api.js (fmtNum, fmtCost, fetchJSON, buildParams, fetch* wrappers)
 *             charts.js (initChart, renderTimeSeriesChart, renderPieChart, chartColors)
 *
 * Exports: initDashboard() — called by the SPA router when #/dashboard is active.
 */

// ── Global state ──
var currentMonth = null;       // { year: number, month: number }
var currentKeyName = '';       // '' = overview (all users)
var summaryData = null;        // cached /api/summary response
var modelsList = [];           // list of model names from /api/summary
var modelPlatformMap = {};     // modelName -> platform name (from /api/models)

// Dynamic chart containers
var dailyChartIds = [];        // [{ chartId, loaderId, model, platform }]
var monthlyChartIds = [];      // [{ chartId, loaderId, model, platform }]
var dailyModelMap = {};        // modelName -> { chartId, loaderId }
var monthlyModelMap = {};

// ── Model alias helpers ──

/** Build alias lookup maps from displayConfig.model_aliases.
 *  Each alias entry: { name: "Display Name", models: ["model-a", "model-b"] }
 *  apiModels: list of actual model names from the API (used for case-sensitive resolution).
 *  Returns { aliasToModels: { displayName: [actual_model_names] }, modelToAlias: { lowercase_model: displayName } } */
function buildAliasMaps(apiModels) {
    var aliases = displayConfig.model_aliases || [];
    var aliasToModels = {};
    var modelToAlias = {};

    // Build case-insensitive lookup: lowercase → actual API model name
    var apiModelLookup = {};
    (apiModels || []).forEach(function (m) {
        apiModelLookup[m.toLowerCase()] = m;
    });

    aliases.forEach(function (a) {
        if (a.name && a.models && a.models.length > 0) {
            var resolvedModels = [];
            a.models.forEach(function (m) {
                var lower = m.toLowerCase();
                // Resolve to actual case from API data (backend matching is case-sensitive)
                var actual = apiModelLookup[lower] || m;
                resolvedModels.push(actual);
                modelToAlias[lower] = a.name;
            });
            aliasToModels[a.name] = resolvedModels;
        }
    });
    return { aliasToModels: aliasToModels, modelToAlias: modelToAlias };
}

// ── Subtitle ──

function updateSubtitle() {
    var el = document.getElementById('pageSubtitle');
    if (!el) return;
    var parts = [];
    if (currentKeyName) {
        parts.push('筛选: ' + currentKeyName);
        parts.push('费用按Token比例分摊');
    } else {
        parts.push('总览 (所有用户)');
    }
    parts.push('所有日期均按 UTC+0 时间显示');
    el.textContent = parts.join(' · ');
}

// ── Selector population ──

function populateMonthSelector(months) {
    var sel = document.getElementById('monthSelector');
    if (!sel) return '';
    var prevMonthVal = sel.value;
    sel.innerHTML = '<option value="">-- 选择月份 --</option>';
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
    if (!keySel) return '';
    var prevKeyVal = keySel.value;
    keySel.innerHTML = '<option value="">总览 (所有用户)</option>';
    var hiddenUsers = displayConfig.hidden_users || [];
    keyNames.forEach(function (name) {
        if (hiddenUsers.indexOf(name) !== -1) return;
        var opt = document.createElement('option');
        opt.value = name;
        opt.textContent = name;
        keySel.appendChild(opt);
    });
    return prevKeyVal;
}

// ── Dynamic chart container management ──

function clearDynamicCharts() {
    var dailyEl = document.getElementById('dailyCharts');
    var monthlyEl = document.getElementById('monthlyCharts');
    if (dailyEl) dailyEl.innerHTML = '';
    if (monthlyEl) monthlyEl.innerHTML = '';
    dailyChartIds = [];
    monthlyChartIds = [];
    dailyModelMap = {};
    monthlyModelMap = {};
}

function buildDynamicCharts(models) {
    clearDynamicCharts();
    // Layer 1: Filter out hidden models and "unknown" model — never show in dashboard charts
    var hiddenModels = (displayConfig.hidden_models || []).map(function (m) { return m.toLowerCase(); });
    var filteredModels = models.filter(function (m) {
        var lower = m.toLowerCase();
        return lower !== 'unknown' && hiddenModels.indexOf(lower) === -1;
    });
    modelsList = filteredModels.slice();

    // Layer 2: Build alias maps and exclude individual models that belong to an alias group
    var aliasMaps = buildAliasMaps(models);
    var modelToAlias = aliasMaps.modelToAlias;
    var aliasToModels = aliasMaps.aliasToModels;

    // Remove individual models that are covered by an alias group
    filteredModels = filteredModels.filter(function (m) {
        return !(m.toLowerCase() in modelToAlias);
    });

    // Add alias group display names as chart entries (respect hidden_models)
    var chartEntries = filteredModels.slice();  // standalone models
    Object.keys(aliasToModels).forEach(function (aliasName) {
        if (hiddenModels.indexOf(aliasName.toLowerCase()) === -1) {
            chartEntries.push(aliasName);
        }
    });

    modelsList = chartEntries.slice();
    var idx = 0;

    chartEntries.forEach(function (modelName) {
        var aliasModels = aliasToModels[modelName] || null;  // non-null if this is an alias group

        // Daily chart
        var dailyChartId = 'chartDaily_' + idx;
        var dailyLoaderId = 'loadingDaily_' + idx;
        var dailyCard = document.createElement('div');
        dailyCard.className = 'chart-card';
        dailyCard.innerHTML =
            '<div class="chart-card__title">每日用量 - ' + modelName + '</div>' +
            '<div class="chart-container chart-container--lg" id="' + dailyChartId + '"></div>' +
            '<div class="loading" id="' + dailyLoaderId + '">加载中</div>';
        var dailyContainer = document.getElementById('dailyCharts');
        if (dailyContainer) dailyContainer.appendChild(dailyCard);
        var dailyEntry = { chartId: dailyChartId, loaderId: dailyLoaderId, model: modelName };
        if (aliasModels) dailyEntry.aliasModels = aliasModels;
        dailyChartIds.push(dailyEntry);
        dailyModelMap[modelName] = { chartId: dailyChartId, loaderId: dailyLoaderId };

        // Monthly chart
        var monthlyChartId = 'chartMonthly_' + idx;
        var monthlyLoaderId = 'loadingMonthly_' + idx;
        var monthlyCard = document.createElement('div');
        monthlyCard.className = 'chart-card';
        monthlyCard.innerHTML =
            '<div class="chart-card__title">月度趋势 - ' + modelName + '</div>' +
            '<div class="chart-container chart-container--lg" id="' + monthlyChartId + '"></div>' +
            '<div class="loading" id="' + monthlyLoaderId + '">加载中</div>';
        var monthlyContainer = document.getElementById('monthlyCharts');
        if (monthlyContainer) monthlyContainer.appendChild(monthlyCard);
        var monthlyEntry = { chartId: monthlyChartId, loaderId: monthlyLoaderId, model: modelName };
        if (aliasModels) monthlyEntry.aliasModels = aliasModels;
        monthlyChartIds.push(monthlyEntry);
        monthlyModelMap[modelName] = { chartId: monthlyChartId, loaderId: monthlyLoaderId };

        idx++;
    });

}

// ── Summary loader ──

async function loadSummary() {
    await loadDisplayConfig();  // Load frontend display filter config first
    var data = await fetchSummary();
    summaryData = data;

    var elStatTotalTokens = document.getElementById('statTotalTokens');
    var elStatOutputTokens = document.getElementById('statOutputTokens');
    var elStatInputTokens = document.getElementById('statInputTokens');
    var elStatCacheHitTokens = document.getElementById('statCacheHitTokens');
    var elStatRequests = document.getElementById('statRequests');
    var elStatCost = document.getElementById('statCost');

    if (elStatTotalTokens) elStatTotalTokens.textContent = fmtNum(data.total_tokens);
    if (elStatOutputTokens) elStatOutputTokens.textContent = fmtNum(data.total_output_tokens);
    if (elStatInputTokens) elStatInputTokens.textContent = fmtNum(data.total_input_tokens);
    if (elStatCacheHitTokens) elStatCacheHitTokens.textContent = fmtNum(data.total_input_cache_hit_tokens);
    if (elStatRequests) elStatRequests.textContent = fmtNum(data.total_requests);
    if (elStatCost) elStatCost.textContent = fmtCost(data.total_cost);

    var months = data.available_months || [];
    var keyNames = data.api_key_names || [];
    var platforms = data.platforms || [];
    var models = data.models || [];

    if (Object.keys(modelPlatformMap).length === 0) {
        try {
            modelPlatformMap = await fetchModels();
        } catch (e) {
            console.error('Failed to fetch models:', e);
        }
    }

    var prevMonthVal = populateMonthSelector(months);
    var prevKeyVal = populateKeyNameSelector(keyNames);

    var keySel = document.getElementById('keyNameSelector');
    if (keySel) {
        if (prevKeyVal && Array.from(keySel.options).some(function (o) { return o.value === prevKeyVal; })) {
            keySel.value = prevKeyVal;
        } else {
            keySel.value = '';
            currentKeyName = '';
        }
    }

    buildDynamicCharts(models);

    var sel = document.getElementById('monthSelector');
    if (months.length > 0) {
        var latest = months[months.length - 1];
        if (sel && (!prevMonthVal || !Array.from(sel.options).some(function (o) { return o.value === prevMonthVal; }))) {
            sel.value = latest.year + '-' + latest.month;
            currentMonth = { year: latest.year, month: latest.month };
        } else if (sel) {
            sel.value = prevMonthVal;
            var parts = prevMonthVal.split('-').map(Number);
            currentMonth = { year: parts[0], month: parts[1] };
        }
        if (currentMonth) {
            var label = currentMonth.year + '-' + String(currentMonth.month).padStart(2, '0');
            var labelEl = document.getElementById('currentMonthLabel');
            if (labelEl) labelEl.textContent = '当前显示: ' + label;
        }
        loadDailyCharts();
    }

    updateSubtitle();
    loadMonthlyCharts();

    var lastUpdatedEl = document.getElementById('lastUpdated');
    if (lastUpdatedEl) {
        lastUpdatedEl.textContent =
            '数据更新时间: ' + new Date().toLocaleString('zh-CN') + ' · 共 ' + months.length + ' 个月数据';
    }
}

// ── Daily charts ──

async function loadDailyChartForModel(modelName, chartId, loaderId, aliasModels) {
    var dom = document.getElementById(chartId);
    var loader = document.getElementById(loaderId);
    if (!dom || !loader) return;
    loader.style.display = 'flex';
    dom.style.display = 'none';

    if (!currentMonth) {
        loader.style.display = 'none';
        dom.style.display = 'block';
        return;
    }

    try {
        var days;
        if (aliasModels && aliasModels.length > 0) {
            // Merge multiple models (aliases) into one chart
            var allResults = await Promise.all(aliasModels.map(function (m) {
                return fetchDaily(currentMonth.year, currentMonth.month, m);
            }));
            var merged = {};
            allResults.forEach(function (result) {
                (result.days || []).forEach(function (d) {
                    if (!merged[d.date]) {
                        merged[d.date] = { date: d.date, output_tokens: 0, input_tokens: 0, input_cache_hit_tokens: 0, input_cache_miss_tokens: 0, total_tokens: 0, requests: 0, cost: 0 };
                    }
                    merged[d.date].output_tokens += d.output_tokens || 0;
                    merged[d.date].input_tokens += d.input_tokens || 0;
                    merged[d.date].input_cache_hit_tokens += d.input_cache_hit_tokens || 0;
                    merged[d.date].input_cache_miss_tokens += d.input_cache_miss_tokens || 0;
                    merged[d.date].total_tokens += d.total_tokens || 0;
                    merged[d.date].requests += d.requests || 0;
                    merged[d.date].cost += (d.cost || 0);
                });
            });
            days = Object.keys(merged).sort().map(function (date) { return merged[date]; });
        } else {
            var data = await fetchDaily(currentMonth.year, currentMonth.month, modelName);
            days = data.days || [];
        }

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
    var tasks = dailyChartIds.map(function (info) {
        return loadDailyChartForModel(info.model, info.chartId, info.loaderId, info.aliasModels);
    });
    await Promise.all(tasks);
}

// ── Pie charts ──

async function loadModelPie() {
    await loadDisplayConfig();  // Ensure config is loaded (may race with loadSummary)
    var loader = document.getElementById('loadingModelPie');
    if (!loader) return;
    try {
        var data = await fetchSummary();
        var breakdown = data.model_breakdown || {};
        var hiddenModels = (displayConfig.hidden_models || []).map(function (m) { return m.toLowerCase(); });
        var aliasMaps = buildAliasMaps(Object.keys(breakdown));
        var modelToAlias = aliasMaps.modelToAlias;
        var aliasToModels = aliasMaps.aliasToModels;

        // Merge individual model entries into alias groups
        var merged = {};
        Object.entries(breakdown).forEach(function (entry) {
            var modelName = entry[0];
            var tokens = entry[1].total_tokens || 0;
            var lower = modelName.toLowerCase();
            if (hiddenModels.indexOf(lower) !== -1) return;
            if (lower in modelToAlias) {
                // This model belongs to an alias group → merge
                var aliasName = modelToAlias[lower];
                // Also skip if the alias name itself is hidden
                if (hiddenModels.indexOf(aliasName.toLowerCase()) !== -1) return;
                merged[aliasName] = (merged[aliasName] || 0) + tokens;
            } else {
                merged[modelName] = (merged[modelName] || 0) + tokens;
            }
        });

        var pieData = Object.entries(merged)
            .map(function (entry) { return { name: entry[0], value: entry[1] }; })
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
    if (!loader) return;
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

async function loadMonthlyTrendForModel(modelName, chartId, loaderId, aliasModels) {
    var dom = document.getElementById(chartId);
    var loader = document.getElementById(loaderId);
    if (!dom || !loader) return;
    loader.style.display = 'flex';
    dom.style.display = 'none';

    try {
        var monthlyData;
        if (aliasModels && aliasModels.length > 0) {
            // Merge multiple models (aliases) into one chart
            var allResults = await Promise.all(aliasModels.map(function (m) {
                return fetchMonthly(m);
            }));
            var merged = {};
            allResults.forEach(function (result) {
                result.forEach(function (d) {
                    if (!merged[d.label]) {
                        merged[d.label] = { label: d.label, output_tokens: 0, input_tokens: 0, input_cache_hit_tokens: 0, input_cache_miss_tokens: 0, total_tokens: 0, requests: 0, cost: 0 };
                    }
                    merged[d.label].output_tokens += d.output_tokens || 0;
                    merged[d.label].input_tokens += d.input_tokens || 0;
                    merged[d.label].input_cache_hit_tokens += d.input_cache_hit_tokens || 0;
                    merged[d.label].input_cache_miss_tokens += d.input_cache_miss_tokens || 0;
                    merged[d.label].total_tokens += d.total_tokens || 0;
                    merged[d.label].requests += d.requests || 0;
                    merged[d.label].cost += (d.cost || 0);
                });
            });
            monthlyData = Object.keys(merged).sort().map(function (label) { return merged[label]; });
        } else {
            monthlyData = await fetchMonthly(modelName);
        }

        var labels = monthlyData.map(function (d) { return d.label; });
        var outputVals = monthlyData.map(function (d) { return d.output_tokens; });
        var inputVals = monthlyData.map(function (d) { return d.input_tokens; });
        var requestVals = monthlyData.map(function (d) { return d.requests; });
        var costVals = monthlyData.map(function (d) { return d.cost; });

        renderTimeSeriesChart(chartId, loaderId, labels, outputVals, inputVals,
                              requestVals, costVals, monthlyData, 0);
    } catch (err) {
        console.error('Failed to load monthly trend for ' + modelName + ':', err);
        loader.textContent = '加载失败';
    }
}

async function loadMonthlyCharts() {
    var tasks = monthlyChartIds.map(function (info) {
        return loadMonthlyTrendForModel(info.model, info.chartId, info.loaderId, info.aliasModels);
    });
    await Promise.all(tasks);
}

// ── Event handlers ──

function bindDashboardEvents() {
    var monthSel = document.getElementById('monthSelector');
    if (monthSel && !monthSel._bound) {
        monthSel._bound = true;
        monthSel.addEventListener('change', function () {
            var val = this.value;
            if (!val) {
                currentMonth = null;
                var labelEl = document.getElementById('currentMonthLabel');
                if (labelEl) labelEl.textContent = '';
                dailyChartIds.forEach(function (info) {
                    var dom = document.getElementById(info.chartId);
                    var loader = document.getElementById(info.loaderId);
                    if (dom) dom.style.display = 'none';
                    if (loader) { loader.style.display = 'flex'; loader.textContent = '请选择月份'; }
                });
                return;
            }
            var parts = val.split('-').map(Number);
            currentMonth = { year: parts[0], month: parts[1] };
            var label = parts[0] + '-' + String(parts[1]).padStart(2, '0');
            var labelEl = document.getElementById('currentMonthLabel');
            if (labelEl) labelEl.textContent = '当前显示: ' + label;
            loadDailyCharts();
        });
    }

    var keySel = document.getElementById('keyNameSelector');
    if (keySel && !keySel._bound) {
        keySel._bound = true;
        keySel.addEventListener('change', async function () {
            currentKeyName = this.value;
            await loadSummary();
            loadModelPie();
            loadTypePie();
        });
    }

}

// ── Refresh ──

async function refreshData() {
    try {
        await fetchRefresh();
        await loadSummary();
        await loadModelPie();
        await loadTypePie();
    } catch (err) {
        console.error('Refresh failed:', err);
        alert('刷新失败，请检查服务器日志');
    }
}

// ── Initialise (exported) ──

function initDashboard() {
    var el = document.getElementById('page-dashboard');
    if (!el || el.dataset.initialized) return;
    el.dataset.initialized = '1';

    bindDashboardEvents();
    loadSummary();
    loadModelPie();
    loadTypePie();
}
