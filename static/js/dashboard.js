/**
 * dashboard.js — Application orchestration layer.
 *
 * Global state, data-loading functions, DOM event handlers, and initialisation.
 * Depends on: api.js (fmtNum, fmtCost, requestJSON, buildParams, fetch* wrappers)
 *             charts.js (initChart, renderTimeSeriesChart, renderPieChart, chartColors)
 *
 * Exports: initDashboard() — called by the SPA router when #/dashboard is active.
 */

// ── Global state ──
var currentMonth = null;       // { year: number, month: number }
var currentKeyName = '';       // '' = overview (all users)
var summaryData = null;        // cached /api/summary response
var summaryRequest = null;     // in-flight/cached summary request for current user
var summaryRequestKey = null;  // currentKeyName used by summaryRequest
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

// Full sorted user list (backend order: most-recent call month → month volume),
// used by the "更多用户" picker. The dropdown itself shows the top 5.
let allKeyNames = [];
const KEY_SELECTOR_TOP = 5;
const DASHBOARD_DELETE_QUEUE_KEY = 'tokenBoard.dashboardDeleteQueue';

function loadPendingDashboardUserDeletes() {
    try {
        var raw = window.localStorage.getItem(DASHBOARD_DELETE_QUEUE_KEY);
        var names = JSON.parse(raw || '[]');
        if (!Array.isArray(names)) return [];
        return names.filter(function (name) {
            return typeof name === 'string' && name.trim();
        }).map(function (name) { return name.trim(); });
    } catch (err) {
        console.warn('Unable to load pending dashboard deletions:', err);
        return [];
    }
}

var pendingDashboardUserDeletes = new Set(loadPendingDashboardUserDeletes());
var moreUsersClosePromise = null;
var moreUsersModalSessionOpen = false;

function persistPendingDashboardUserDeletes() {
    try {
        window.localStorage.setItem(
            DASHBOARD_DELETE_QUEUE_KEY,
            JSON.stringify(Array.from(pendingDashboardUserDeletes).sort())
        );
    } catch (err) {
        console.warn('Unable to persist pending dashboard deletions:', err);
    }
}

function populateKeyNameSelector(keyNames) {
    var keySel = document.getElementById('keyNameSelector');
    if (!keySel) return '';
    var prevKeyVal = keySel.value;
    keySel.innerHTML = '<option value="">总览 (所有用户)</option>';
    allKeyNames = (keyNames || []).filter(function (name) {
        return !pendingDashboardUserDeletes.has(name);
    });
    allKeyNames.slice(0, KEY_SELECTOR_TOP).forEach(function (name) {
        var opt = document.createElement('option');
        opt.value = name;
        opt.textContent = name;
        keySel.appendChild(opt);
    });
    // Preserve a non-top selection across reloads so the filter isn't silently
    // reset to "all users" (e.g. after picking a month/model).
    if (prevKeyVal && allKeyNames.indexOf(prevKeyVal) >= 0 &&
        allKeyNames.slice(0, KEY_SELECTOR_TOP).indexOf(prevKeyVal) < 0) {
        var opt = document.createElement('option');
        opt.value = prevKeyVal;
        opt.textContent = prevKeyVal;
        keySel.appendChild(opt);
    }
    return prevKeyVal;
}

// ── "更多用户" picker ────────────────────────────────────────────────

function renderMoreUsersList() {
    var list = document.getElementById('moreUsersList');
    if (!list) return;
    var visibleNames = allKeyNames.filter(function (name) {
        return !pendingDashboardUserDeletes.has(name);
    });
    if (!visibleNames.length) {
        list.innerHTML = '<div class="td-empty">暂无其他用户</div>';
    } else {
        list.innerHTML = visibleNames.map(function (name) {
            var active = (name === currentKeyName) ? ' more-user-row--active' : '';
            return '<div class="more-user-row' + active + '">' +
                '<button type="button" class="btn btn--sm more-user-item" data-name="' +
                esc(name) + '">' + esc(name) + '</button>' +
                '<button type="button" class="btn btn--sm more-user-delete" data-name="' +
                esc(name) + '" title="删除历史数据">删除</button>' +
                '</div>';
        }).join('');
    }
}

function openMoreUsersModal() {
    if (!moreUsersModalSessionOpen) {
        moreUsersModalSessionOpen = true;
    }
    renderMoreUsersList();
    openModal('moreUsersModal');
}

function removeMoreUserFromList(name) {
    allKeyNames = allKeyNames.filter(function (item) { return item !== name; });
    renderMoreUsersList();
}

async function closeMoreUsersModalImpl() {
    var modal = document.getElementById('moreUsersModal');
    var controls = modal ? modal.querySelectorAll('button') : [];
    Array.from(controls).forEach(function (control) { control.disabled = true; });

    try {
        var names = Array.from(pendingDashboardUserDeletes).sort();
        if (!names.length) {
            closeModal('moreUsersModal');
            moreUsersModalSessionOpen = false;
            return true;
        }

        var result = await deleteDashboardUsers(names);
        if (!result || result.status !== 'ok') {
            throw new Error((result && result.message) || '删除失败');
        }
        pendingDashboardUserDeletes.clear();
        persistPendingDashboardUserDeletes();
        closeModal('moreUsersModal');
        moreUsersModalSessionOpen = false;
        await refreshData();
        if (typeof showToast === 'function') {
            var suffix = result.uploaded ? '并已上传到云端' : '并已保存到本机';
            showToast('已删除 ' + result.deleted_names.length + ' 个用户的历史数据' + suffix);
        }
        return true;
    } catch (err) {
        if (err && err.name === 'HttpError' && err.status === 404) {
            // Another machine may already have removed every queued name.
            // The desired state is satisfied, so do not trap the user in a
            // retry loop for a request that has no remaining work.
            pendingDashboardUserDeletes.clear();
            persistPendingDashboardUserDeletes();
            closeModal('moreUsersModal');
            moreUsersModalSessionOpen = false;
            await refreshData();
            if (typeof showToast === 'function') {
                showToast('待删除用户已不存在');
            }
            return true;
        }
        if (typeof showToast === 'function') {
            showToast('删除提交失败：' + (err.message || '操作失败') +
                '；删除队列已保留，关闭窗口可重试', 'error');
        } else {
            alert('删除提交失败：' + (err.message || '操作失败'));
        }
        return false;
    } finally {
        Array.from(controls).forEach(function (control) { control.disabled = false; });
    }
}

async function closeMoreUsersModal() {
    if (moreUsersClosePromise) return moreUsersClosePromise;
    var operation = closeMoreUsersModalImpl();
    moreUsersClosePromise = operation;
    try {
        return await operation;
    } finally {
        if (moreUsersClosePromise === operation) moreUsersClosePromise = null;
    }
}

async function selectMoreUser(name) {
    if (!await closeMoreUsersModal()) return;
    currentKeyName = name;
    var keySel = document.getElementById('keyNameSelector');
    if (keySel) {
        var existing = Array.from(keySel.options).some(function (o) { return o.value === name; });
        if (!existing) {
            var opt = document.createElement('option');
            opt.value = name;
            opt.textContent = name;
            keySel.appendChild(opt);
        }
        keySel.value = name;
    }
    loadSummary();
    loadModelPie();
    loadTypePie();
}

async function deleteMoreUser(name, button) {
    if (!confirm('确定删除用户「' + name + '」的历史数据吗？')) {
        return;
    }

    var row = button && button.closest ? button.closest('.more-user-row') : null;
    var controls = row ? row.querySelectorAll('button') : [];
    Array.from(controls).forEach(function (control) { control.disabled = true; });

    try {
        pendingDashboardUserDeletes.add(name);
        persistPendingDashboardUserDeletes();
        removeMoreUserFromList(name);
        if (currentKeyName === name) {
            currentKeyName = '';
            var keySel = document.getElementById('keyNameSelector');
            if (keySel) keySel.value = '';
        }
        if (typeof showToast === 'function') {
            showToast('已将用户「' + name + '」加入删除队列，关闭窗口后提交');
        }
    } catch (err) {
        Array.from(controls).forEach(function (control) { control.disabled = false; });
        if (typeof showToast === 'function') {
            showToast('加入删除队列失败：' + (err.message || '操作失败'), 'error');
        } else {
            alert('删除失败：' + (err.message || '同步失败'));
        }
    }
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

// All-history token share by display unit for the current user scope. Both the
// model cards and the model pie use this same calculation.
let globalTokenShares = null;
let globalTokenSharesKey = null;

function calculateGlobalTokenShares(data) {
    var breakdown = data.model_breakdown || {};
    var aliasMaps = buildAliasMaps(Object.keys(breakdown));
    var modelToAlias = aliasMaps.modelToAlias;
    var units = {};

    Object.keys(breakdown).forEach(function (m) {
        var lower = m.toLowerCase();
        var unit = (lower in modelToAlias) ? modelToAlias[lower] : m;
        units[unit] = (units[unit] || 0) + (breakdown[m].total_tokens || 0);
    });

    var total = Object.keys(units).reduce(function (sum, unit) {
        return sum + units[unit];
    }, 0);
    var shares = {};
    if (total > 0) {
        Object.keys(units).forEach(function (unit) {
            shares[unit] = units[unit] / total;
        });
    }
    return shares;
}

function updateGlobalTokenShares(summaryDataForCurrentUser) {
    var scopeKey = currentKeyName || '';
    if (globalTokenSharesKey === scopeKey && globalTokenShares !== null) return;
    globalTokenShares = calculateGlobalTokenShares(summaryDataForCurrentUser);
    globalTokenSharesKey = scopeKey;
}

function fetchDashboardSummary() {
    var requestKey = currentKeyName || '';
    if (summaryRequest && summaryRequestKey === requestKey) return summaryRequest;

    summaryRequestKey = requestKey;
    summaryRequest = fetchSummary().then(function (data) {
        summaryData = data;
        return data;
    }).catch(function (err) {
        if (summaryRequestKey === requestKey) {
            summaryRequest = null;
            summaryRequestKey = null;
        }
        throw err;
    });
    return summaryRequest;
}

// Models the CURRENT user actually used in the currently-selected month
// (set of model names from that month's daily breakdown).  undefined = not yet
// computed; null = no month / fetch failed. Used as the second branch of the
// enabled-model condition.
let currentMonthUsedModels = undefined;

async function fetchCurrentMonthUsedModels() {
    if (!currentMonth) return null;
    try {
        // buildParams applies the current user filter to fetchDaily.
        var daily = await fetchDaily(currentMonth.year, currentMonth.month);
        var set = {};
        (daily.days || []).forEach(function (day) {
            Object.keys(day.by_model || {}).forEach(function (m) { set[m] = true; });
        });
        return set;
    } catch (e) {
        return null;
    }
}

function buildDynamicCharts(models, usedModels) {
    clearDynamicCharts();
    // A model/display unit is enabled when its global share is >1% OR it was
    // used in the current month. This is also how alias groups are handled.
    var aliasMaps = buildAliasMaps(models);
    var modelToAlias = aliasMaps.modelToAlias;
    var aliasToModels = aliasMaps.aliasToModels;

    var isUsed = function (m) {
        if (usedModels == null) return true;
        var lower = String(m).toLowerCase();
        return !!(lower in usedModels || m in usedModels);
    };
    var isEnabledUnit = function (unit, members) {
        if (globalTokenShares && globalTokenShares[unit] > 0.01) return true;
        var candidates = (members && members.length) ? members : [unit];
        return candidates.some(isUsed);
    };

    var filteredModels = models.filter(function (m) {
        var lower = m.toLowerCase();
        if (lower === 'unknown') return false;
        var unit = (lower in modelToAlias) ? modelToAlias[lower] : m;
        return isEnabledUnit(unit, aliasToModels[unit]);
    });

    // Remove individual models that are covered by an alias group
    filteredModels = filteredModels.filter(function (m) {
        return !(m.toLowerCase() in modelToAlias);
    });

    // Add enabled alias groups as chart entries.
    var chartEntries = filteredModels.slice();  // standalone models
    Object.keys(aliasToModels).forEach(function (aliasName) {
        var members = aliasToModels[aliasName] || [];
        if (isEnabledUnit(aliasName, members)) chartEntries.push(aliasName);
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
    var data = await fetchDashboardSummary();

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

    // Actual consumption is the metered bill plus the subscription allocation
    // attached to each agent. The theoretical card is usage-derived only.
    if (elStatCost) elStatCost.textContent = fmtCost(data.actual_cost || 0);
    var statCostSub = document.getElementById('statCostSub');
    if (statCostSub) {
        statCostSub.textContent = '理论消费 ' + fmtCost(data.theoretical_total_cost || 0);
    }

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
    }

    // Models the current user actually used in the current month — one side of
    // the enabled-model condition.
    currentMonthUsedModels = await fetchCurrentMonthUsedModels();
    updateGlobalTokenShares(data);

    buildDynamicCharts(models, currentMonthUsedModels);

    if (currentMonth) {
        loadDailyCharts();
    }

    loadMonthlyCharts();

    var lastUpdatedEl = document.getElementById('lastUpdated');
    if (lastUpdatedEl) {
        var nowIso = new Date().toISOString();
        lastUpdatedEl.textContent =
            '数据更新时间: ' + fmtLocal(nowIso) + ' · 共 ' + months.length + ' 个月数据';
    }
}

// ── Daily charts ──

/**
 * Generate all dates in YYYY-MM-DD format for a given year/month.
 * @returns {string[]} e.g. ["2026-06-01", "2026-06-02", ...]
 */
function generateMonthDates(year, month) {
    var daysInMonth = new Date(year, month, 0).getDate();  // month is 1-based, Date uses 0-based
    var dates = [];
    for (var d = 1; d <= daysInMonth; d++) {
        var dd = String(d).padStart(2, '0');
        var mm = String(month).padStart(2, '0');
        dates.push(year + '-' + mm + '-' + dd);
    }
    return dates;
}

/** Fill missing days with zero values so the chart X-axis shows the entire month. */
function fillMonthDays(daysMap, year, month) {
    var allDates = generateMonthDates(year, month);
    return allDates.map(function (date) {
        var d = daysMap[date];
        if (d) return d;
        return {
            date: date,
            output_tokens: 0,
            input_tokens: 0,
            input_cache_hit_tokens: 0,
            input_cache_miss_tokens: 0,
            total_tokens: 0,
            requests: 0,
            cost: 0
        };
    });
}

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
        var daysMap = {};
        if (aliasModels && aliasModels.length > 0) {
            // Merge multiple models (aliases) into one chart
            var allResults = await Promise.all(aliasModels.map(function (m) {
                return fetchDaily(currentMonth.year, currentMonth.month, m);
            }));
            allResults.forEach(function (result) {
                (result.days || []).forEach(function (d) {
                    if (!daysMap[d.date]) {
                        daysMap[d.date] = { date: d.date, output_tokens: 0, input_tokens: 0, input_cache_hit_tokens: 0, input_cache_miss_tokens: 0, total_tokens: 0, requests: 0, cost: 0 };
                    }
                    daysMap[d.date].output_tokens += d.output_tokens || 0;
                    daysMap[d.date].input_tokens += d.input_tokens || 0;
                    daysMap[d.date].input_cache_hit_tokens += d.input_cache_hit_tokens || 0;
                    daysMap[d.date].input_cache_miss_tokens += d.input_cache_miss_tokens || 0;
                    daysMap[d.date].total_tokens += d.total_tokens || 0;
                    daysMap[d.date].requests += d.requests || 0;
                    daysMap[d.date].cost += (d.cost || 0);
                });
            });
        } else {
            var data = await fetchDaily(currentMonth.year, currentMonth.month, modelName);
            (data.days || []).forEach(function (d) {
                daysMap[d.date] = d;
            });
        }

        // Fill in all days of the month — missing days get 0 values
        var days = fillMonthDays(daysMap, currentMonth.year, currentMonth.month);

        var labels = days.map(function (d) { return d.date; });
        var outputVals = days.map(function (d) { return d.output_tokens; });
        var inputVals = days.map(function (d) { return d.input_tokens; });
        var requestVals = days.map(function (d) { return d.requests; });
        var costVals = days.map(function (d) { return d.cost; });

        var rotate = labels.length > 14 ? 45 : 0;
        renderTimeSeriesChart(chartId, loaderId, labels, outputVals, inputVals,
                              requestVals, costVals, days, rotate);
    } catch (err) {
        console.error('Failed to load daily chart for ' + modelName + ':', err);
        loader.classList.add('loading--text'); loader.textContent = '加载失败';
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
        var data = await fetchDashboardSummary();
        updateGlobalTokenShares(data);
        var breakdown = data.model_breakdown || {};
        var aliasMaps = buildAliasMaps(Object.keys(breakdown));
        var modelToAlias = aliasMaps.modelToAlias;

        // Merge individual model entries into alias groups
        var merged = {};
        Object.entries(breakdown).forEach(function (entry) {
            var modelName = entry[0];
            var modelData = entry[1];
            var tokens = modelData.total_tokens || 0;
            // This is the token-equivalent amount only. A plan's real
            // subscription fee is not a model cost and is never used here.
            var theoreticalCost = modelData.theoretical_cost || 0;
            var lower = modelName.toLowerCase();
            var displayName = (lower in modelToAlias)
                ? modelToAlias[lower]
                : modelName;
            if (!merged[displayName]) {
                merged[displayName] = { tokens: 0, theoretical_cost: 0 };
            }
            merged[displayName].tokens += tokens;
            merged[displayName].theoretical_cost += theoreticalCost || 0;
        });

        var pieData = Object.entries(merged)
            .map(function (entry) {
                return {
                    name: entry[0],
                    value: entry[1].tokens,
                    theoretical_cost: entry[1].theoretical_cost,
                };
            })
            .filter(function (d) { return d.value > 0; })
            .sort(function (a, b) { return b.value - a.value; });

        // The model pie only includes display units whose global token share is
        // greater than 1%.
        pieData = pieData.filter(function (d) {
            return !globalTokenShares || globalTokenShares[d.name] > 0.01;
        });

        renderPieChart('chartModelPie', pieData, chartColors);
        loader.style.display = 'none';
    } catch (err) {
        console.error('Failed to load model pie:', err);
        loader.classList.add('loading--text'); loader.textContent = '加载失败';
    }
}

async function loadTypePie() {
    var loader = document.getElementById('loadingTypePie');
    if (!loader) return;
    try {
        var data = await fetchTokenTypes();
        var filtered = data.filter(function (d) { return d.value > 0; });
        renderPieChart('chartTypePie', filtered, ['#B45F45', '#6E8B77', '#C08B42']);
        loader.style.display = 'none';
    } catch (err) {
        console.error('Failed to load type pie:', err);
        loader.classList.add('loading--text'); loader.textContent = '加载失败';
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
        loader.classList.add('loading--text'); loader.textContent = '加载失败';
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
                    if (loader) { loader.style.display = 'flex'; loader.classList.add('loading--text'); loader.textContent = '请选择月份'; }
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

    var moreBtn = document.getElementById('moreUsersBtn');
    if (moreBtn && !moreBtn._bound) {
        moreBtn._bound = true;
        moreBtn.addEventListener('click', openMoreUsersModal);
    }
    var moreList = document.getElementById('moreUsersList');
    if (moreList && !moreList._bound) {
        moreList._bound = true;
        moreList.addEventListener('click', function (e) {
            var deleteBtn = e.target.closest ? e.target.closest('.more-user-delete') : null;
            if (deleteBtn) {
                deleteMoreUser(deleteBtn.dataset.name, deleteBtn);
                return;
            }
            var btn = e.target.closest ? e.target.closest('.more-user-item') : null;
            if (btn) selectMoreUser(btn.dataset.name);
        });
    }

}

// ── Refresh ──

async function refreshData() {
    try {
        await fetchRefresh();
        // Invalidate cached summaries and global shares after importing fresh
        // data.
        summaryData = null;
        summaryRequest = null;
        summaryRequestKey = null;
        globalTokenShares = null;
        globalTokenSharesKey = null;
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
