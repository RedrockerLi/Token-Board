/**
 * dashboard.js — Application orchestration layer.
 *
 * Global state, data-loading functions, DOM event handlers, and initialisation.
 * Depends on: api.js (fmtNum, fmtCost, requestJSON, buildParams, fetch* wrappers)
 *             charts.js (renderPieChart, renderCalendar3D, chartColors,
 *                         generateChartColor)
 *
 * Exports: initDashboard() — called by the SPA router when #/dashboard is active.
 */

// ── Global state ──
var currentUserId = '';        // '' = overview (all users)
var summaryData = null;        // cached /api/summary response
var summaryRequest = null;     // in-flight/cached summary request for current user
var summaryRequestKey = null;  // currentUserId used by summaryRequest
var dashboardDataGeneration = 0;

var calendarState = {
    mode: 'month',
    year: null,
    month: null,
    days: [],
};
var calendarLoadSequence = 0;
var currentMonthUsedModels = null;
var currentMonthUsedModelsKey = null;
var currentMonthUsedModelsRequest = null;
var currentMonthUsedModelsRequestKey = null;
var dailyDataCache = Object.create(null);
var dailyDataRequests = Object.create(null);
var dailyDataCacheGeneration = 0;
// A sub-1% model is kept only when its current browser-local natural month
// usage exceeds this strict token threshold.
var CURRENT_MONTH_MODEL_TOKEN_THRESHOLD = 50 * 1000 * 1000;

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

// Full sorted user list (backend order: most-recent call month → month volume),
// used by the "更多用户" picker. The dropdown itself shows the top 5.
let allUsers = [];
const KEY_SELECTOR_TOP = 5;
const DASHBOARD_DELETE_QUEUE_KEY = 'tokenBoard.dashboardArchiveQueue';

function loadPendingDashboardUserDeletes() {
    try {
        var raw = window.localStorage.getItem(DASHBOARD_DELETE_QUEUE_KEY);
        var ids = JSON.parse(raw || '[]');
        if (!Array.isArray(ids)) return [];
        return ids.map(function (id) { return Number(id); }).filter(function (id) {
            return Number.isInteger(id) && id > 0;
        });
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

function populateKeyNameSelector(users) {
    var keySel = document.getElementById('keyNameSelector');
    if (!keySel) return '';
    var prevKeyVal = keySel.value;
    keySel.innerHTML = '<option value="">总览 (所有用户)</option>';
    allUsers = (users || []).filter(function (user) {
        return user && !pendingDashboardUserDeletes.has(Number(user.id));
    });
    allUsers.slice(0, KEY_SELECTOR_TOP).forEach(function (user) {
        var opt = document.createElement('option');
        opt.value = String(user.id);
        opt.textContent = user.name;
        keySel.appendChild(opt);
    });
    // Preserve a non-top selection across reloads so the filter isn't silently
    // reset to "all users" (e.g. after picking a month/model).
    if (prevKeyVal && allUsers.some(function (user) { return String(user.id) === prevKeyVal; }) &&
        !allUsers.slice(0, KEY_SELECTOR_TOP).some(function (user) { return String(user.id) === prevKeyVal; })) {
        var opt = document.createElement('option');
        opt.value = prevKeyVal;
        var selected = allUsers.find(function (user) { return String(user.id) === prevKeyVal; });
        opt.textContent = selected ? selected.name : prevKeyVal;
        keySel.appendChild(opt);
    }
    return prevKeyVal;
}

// ── "更多用户" picker ────────────────────────────────────────────────

function renderMoreUsersList() {
    var list = document.getElementById('moreUsersList');
    if (!list) return;
    var visibleUsers = allUsers.filter(function (user) {
        return !pendingDashboardUserDeletes.has(Number(user.id));
    });
    if (!visibleUsers.length) {
        list.innerHTML = '<div class="td-empty">暂无其他用户</div>';
    } else {
        list.innerHTML = visibleUsers.map(function (user) {
            var active = (String(user.id) === String(currentUserId)) ? ' more-user-row--active' : '';
            var archiveButton = Number(user.id) === 0 ? '' :
                '<button type="button" class="btn btn--sm more-user-delete" data-user-id="' +
                esc(String(user.id)) + '" title="归档历史数据">归档</button>';
            return '<div class="more-user-row' + active + '">' +
                '<button type="button" class="btn btn--sm more-user-item" data-user-id="' +
                esc(String(user.id)) + '">' + esc(user.name) + '</button>' +
                archiveButton +
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

function removeMoreUserFromList(userId) {
    allUsers = allUsers.filter(function (item) { return Number(item.id) !== Number(userId); });
    renderMoreUsersList();
}

async function closeMoreUsersModalImpl() {
    var modal = document.getElementById('moreUsersModal');
    var controls = modal ? modal.querySelectorAll('button') : [];
    Array.from(controls).forEach(function (control) { control.disabled = true; });

    try {
        var userIds = Array.from(pendingDashboardUserDeletes).sort(function (a, b) { return a - b; });
        if (!userIds.length) {
            closeModal('moreUsersModal');
            moreUsersModalSessionOpen = false;
            return true;
        }

        var result = await deleteDashboardUsers(userIds);
        if (!result || result.status !== 'ok') {
            throw new Error((result && result.message) || '归档失败');
        }
        pendingDashboardUserDeletes.clear();
        persistPendingDashboardUserDeletes();
        closeModal('moreUsersModal');
        moreUsersModalSessionOpen = false;
        await refreshData();
        if (typeof showToast === 'function') {
            var suffix = result.uploaded ? '并已上传到云端' : '并已保存到本机';
            showToast('已归档 ' + (result.archived_user_ids || []).length + ' 个用户的历史数据' + suffix);
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
                showToast('待归档用户已不存在');
            }
            return true;
        }
        if (typeof showToast === 'function') {
            showToast('归档提交失败：' + (err.message || '操作失败') +
                '；归档队列已保留，关闭窗口可重试', 'error');
        } else {
            alert('归档提交失败：' + (err.message || '操作失败'));
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

async function selectMoreUser(userId) {
    if (!await closeMoreUsersModal()) return;
    currentUserId = String(userId);
    var keySel = document.getElementById('keyNameSelector');
    if (keySel) {
        var existing = Array.from(keySel.options).some(function (o) { return o.value === String(userId); });
        if (!existing) {
            var opt = document.createElement('option');
            opt.value = String(userId);
            var selected = allUsers.find(function (user) { return Number(user.id) === Number(userId); });
            opt.textContent = selected ? selected.name : String(userId);
            keySel.appendChild(opt);
        }
        keySel.value = String(userId);
    }
    invalidateDashboardData();
    invalidateCalendarLoad();
    await refreshDashboardVisuals();
}

async function deleteMoreUser(userId, button) {
    var user = allUsers.find(function (item) { return Number(item.id) === Number(userId); });
    var displayName = user ? user.name : String(userId);
    if (!confirm('确定归档用户「' + displayName + '」的历史数据吗？')) {
        return;
    }

    var row = button && button.closest ? button.closest('.more-user-row') : null;
    var controls = row ? row.querySelectorAll('button') : [];
    Array.from(controls).forEach(function (control) { control.disabled = true; });

    try {
        pendingDashboardUserDeletes.add(Number(userId));
        persistPendingDashboardUserDeletes();
        removeMoreUserFromList(userId);
        if (String(currentUserId) === String(userId)) {
            currentUserId = '';
            var keySel = document.getElementById('keyNameSelector');
            if (keySel) keySel.value = '';
        }
        if (typeof showToast === 'function') {
            showToast('已将用户「' + displayName + '」加入归档队列，关闭窗口后提交');
        }
    } catch (err) {
        Array.from(controls).forEach(function (control) { control.disabled = false; });
        if (typeof showToast === 'function') {
            showToast('加入归档队列失败：' + (err.message || '操作失败'), 'error');
        } else {
            alert('归档失败：' + (err.message || '同步失败'));
        }
    }
}

// All-history token share by display unit for the current user scope. Both the
// model cards and the model pie use this same calculation.
let globalTokenShares = null;
let globalTokenSharesKey = null;

function getBrowserCurrentMonth() {
    var now = new Date();
    return { year: now.getFullYear(), month: now.getMonth() + 1 };
}

function modelTokenCount(modelData) {
    if (!modelData) return 0;
    if (modelData.total_tokens != null) {
        var total = Number(modelData.total_tokens);
        return isFinite(total) ? Math.max(0, total) : 0;
    }
    return Math.max(0,
        Number(modelData.output_tokens || modelData.output || 0) +
        Number(modelData.input_cache_hit_tokens || modelData.input_hit || 0) +
        Number(modelData.input_cache_miss_tokens || modelData.input_miss || 0));
}

function mergeModelStat(target, source) {
    var fields = [
        'output_tokens', 'input_cache_hit_tokens', 'input_cache_miss_tokens',
        'total_tokens', 'requests', 'cost', 'theoretical_cost', 'actual_cost',
        'metered_cost', 'recurring_cost'
    ];
    fields.forEach(function (field) {
        target[field] = Number(target[field] || 0) + Number(source && source[field] || 0);
    });
}

function aggregateModelBreakdown(data) {
    var breakdown = data && data.model_breakdown || {};
    var aliasMaps = buildAliasMaps(Object.keys(breakdown));
    var units = Object.create(null);
    Object.keys(breakdown).forEach(function (modelName) {
        var lower = String(modelName).toLowerCase();
        var displayName = aliasMaps.modelToAlias[lower] || modelName;
        if (!units[displayName]) units[displayName] = {
            tokens: 0,
            theoretical_cost: 0,
        };
        units[displayName].tokens += modelTokenCount(breakdown[modelName]);
        units[displayName].theoretical_cost += Number(
            breakdown[modelName] && breakdown[modelName].theoretical_cost || 0);
    });
    return units;
}

function calculateGlobalTokenShares(data) {
    var units = aggregateModelBreakdown(data);

    var total = Object.keys(units).reduce(function (sum, unit) {
        return sum + units[unit].tokens;
    }, 0);
    var shares = {};
    if (total > 0) {
        Object.keys(units).forEach(function (unit) {
            shares[unit] = units[unit].tokens / total;
        });
    }
    return shares;
}

function isVisibleModel(modelName, shares, currentMonthModels) {
    var share = Number(shares && shares[modelName] || 0);
    var usedThisMonth = currentMonthModels && currentMonthModels.has
        ? currentMonthModels.has(modelName) : false;
    return share >= 0.01 || usedThisMonth;
}

/**
 * Build the one model set shared by the model pie, calendar bars and legend.
 * Rank is assigned before the visibility filter, so colors remain tied to a
 * model's global token position even when small models are hidden.
 */
function buildVisibleModelEntries(data, currentMonthModels) {
    var units = aggregateModelBreakdown(data);
    var used = currentMonthModels && currentMonthModels.has
        ? currentMonthModels : new Set();
    used.forEach(function (name) {
        if (!units[name]) units[name] = { tokens: 0, theoretical_cost: 0 };
    });

    var total = Object.keys(units).reduce(function (sum, name) {
        return sum + units[name].tokens;
    }, 0);
    var shares = {};
    Object.keys(units).forEach(function (name) {
        shares[name] = total > 0 ? units[name].tokens / total : 0;
    });
    return Object.keys(units)
        .map(function (name) {
            return {
                name: name,
                tokens: units[name].tokens,
                theoretical_cost: units[name].theoretical_cost,
                share: shares[name],
            };
        })
        .sort(function (a, b) {
            return b.tokens - a.tokens || String(a.name).localeCompare(String(b.name));
        })
        .map(function (entry, rank) {
            entry.rank = rank;
            entry.color = generateChartColor(rank);
            entry.visible = isVisibleModel(entry.name, shares, used);
            return entry;
        })
        .filter(function (entry) { return entry.visible; });
}

function collectCurrentMonthModelUnits(data) {
    var days = data && data.days || [];
    var modelNames = [];
    days.forEach(function (day) {
        Object.keys(day.by_model || {}).forEach(function (name) {
            if (modelNames.indexOf(name) < 0) modelNames.push(name);
        });
    });
    var aliasMaps = buildAliasMaps(modelNames);
    var tokenTotals = Object.create(null);
    days.forEach(function (day) {
        Object.keys(day.by_model || {}).forEach(function (modelName) {
            var tokens = modelTokenCount(day.by_model[modelName]);
            if (tokens <= 0) return;
            var displayName = aliasMaps.modelToAlias[String(modelName).toLowerCase()]
                || modelName;
            tokenTotals[displayName] = (tokenTotals[displayName] || 0) + tokens;
        });
    });
    var used = new Set();
    Object.keys(tokenTotals).forEach(function (displayName) {
        // Request-only rows and models at exactly 50M do not qualify.
        if (tokenTotals[displayName] > CURRENT_MONTH_MODEL_TOKEN_THRESHOLD) {
            used.add(displayName);
        }
    });
    return used;
}

function resetCurrentMonthModelCache() {
    currentMonthUsedModels = null;
    currentMonthUsedModelsKey = null;
    currentMonthUsedModelsRequest = null;
    currentMonthUsedModelsRequestKey = null;
}

function fetchCurrentMonthUsedModels() {
    var current = getBrowserCurrentMonth();
    var requestKey = (currentUserId || '') + '|' + current.year + '-' + current.month;
    if (currentMonthUsedModels instanceof Set &&
        currentMonthUsedModelsKey === requestKey) {
        return Promise.resolve(currentMonthUsedModels);
    }
    if (currentMonthUsedModelsRequest && currentMonthUsedModelsRequestKey === requestKey) {
        return currentMonthUsedModelsRequest;
    }

    currentMonthUsedModelsRequestKey = requestKey;
    currentMonthUsedModelsRequest = fetchDashboardDaily(current.year, current.month)
        .then(function (data) {
            var used = collectCurrentMonthModelUnits(data);
            if (currentMonthUsedModelsRequestKey === requestKey) {
                currentMonthUsedModels = used;
                currentMonthUsedModelsKey = requestKey;
            }
            return used;
        })
        .catch(function (error) {
            console.error('Failed to load current-month model usage:', error);
            if (currentMonthUsedModelsRequestKey === requestKey) {
                currentMonthUsedModels = new Set();
                currentMonthUsedModelsKey = requestKey;
            }
            return new Set();
        });
    return currentMonthUsedModelsRequest;
}

function updateGlobalTokenShares(summaryDataForCurrentUser) {
    var scopeKey = currentUserId || '';
    if (globalTokenSharesKey === scopeKey && globalTokenShares !== null) return;
    globalTokenShares = calculateGlobalTokenShares(summaryDataForCurrentUser);
    globalTokenSharesKey = scopeKey;
}

function fetchDashboardSummary() {
    var requestKey = currentUserId || '';
    if (summaryRequest && summaryRequestKey === requestKey) return summaryRequest;

    var requestGeneration = dashboardDataGeneration;
    summaryRequestKey = requestKey;
    summaryRequest = fetchSummary().then(function (data) {
        if (summaryRequestKey === requestKey &&
            dashboardDataGeneration === requestGeneration) summaryData = data;
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

// ── Summary loader ──

async function loadSummary() {
    await loadDisplayConfig();  // Load alias config before calculating pie shares
    var requestKey = currentUserId || '';
    var requestGeneration = dashboardDataGeneration;
    var data = await fetchDashboardSummary();
    if (requestKey !== (currentUserId || '') ||
        requestGeneration !== dashboardDataGeneration) return;

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

    // Actual consumption is the cumulative value stored on the selected
    // stable user identity; theoretical consumption comes from daily model use.
    if (elStatCost) elStatCost.textContent = fmtCost(data.actual_cost || 0);
    var statCostSub = document.getElementById('statCostSub');
    if (statCostSub) {
        statCostSub.textContent = '理论消费 ' + fmtCost(data.theoretical_total_cost || 0);
    }

    var months = data.available_months || [];
    var prevKeyVal = populateKeyNameSelector(data.users || []);

    var keySel = document.getElementById('keyNameSelector');
    if (keySel) {
        if (prevKeyVal && Array.from(keySel.options).some(function (o) { return o.value === prevKeyVal; })) {
            keySel.value = prevKeyVal;
        } else {
            keySel.value = '';
            currentUserId = '';
        }
    }

    updateGlobalTokenShares(data);
    populateCalendarSelectors(months);

    var lastUpdatedEl = document.getElementById('lastUpdated');
    if (lastUpdatedEl) {
        var nowIso = new Date().toISOString();
        lastUpdatedEl.textContent =
            '数据更新时间: ' + fmtLocal(nowIso) + ' · 共 ' + months.length + ' 个月数据';
    }
}

// ── Daily-use calendar ─────────────────────────────────────────────────────

function calendarScopeKey(year, month) {
    return (currentUserId || '') + '|' + year + '-' + month;
}

function fetchDashboardDaily(year, month) {
    var key = calendarScopeKey(year, month);
    var cache = dailyDataCache;
    var requests = dailyDataRequests;
    var generation = dailyDataCacheGeneration;
    if (Object.prototype.hasOwnProperty.call(cache, key)) {
        return Promise.resolve(cache[key]);
    }
    if (requests[key]) return requests[key];
    requests[key] = fetchDaily(year, month).then(function (data) {
        if (dailyDataCacheGeneration === generation) cache[key] = data;
        delete requests[key];
        return data;
    }).catch(function (error) {
        delete requests[key];
        throw error;
    });
    return requests[key];
}

function resetDailyDataCache() {
    dailyDataCacheGeneration++;
    dailyDataCache = Object.create(null);
    dailyDataRequests = Object.create(null);
}

function emptyCalendarDay(date) {
    return {
        date: date,
        output_tokens: 0,
        input_cache_hit_tokens: 0,
        input_cache_miss_tokens: 0,
        input_tokens: 0,
        total_tokens: 0,
        requests: 0,
        cost: 0,
        theoretical_cost: 0,
        actual_cost: 0,
        metered_cost: 0,
        recurring_cost: 0,
        by_model: {},
    };
}

function mergeCalendarDay(target, source) {
    var numericFields = [
        'output_tokens', 'input_cache_hit_tokens', 'input_cache_miss_tokens',
        'input_tokens', 'total_tokens', 'requests', 'cost', 'theoretical_cost',
        'actual_cost', 'metered_cost', 'recurring_cost'
    ];
    numericFields.forEach(function (field) {
        target[field] = Number(target[field] || 0) + Number(source && source[field] || 0);
    });
    Object.keys(source && source.by_model || {}).forEach(function (modelName) {
        if (!target.by_model[modelName]) target.by_model[modelName] = {
            output_tokens: 0,
            input_cache_hit_tokens: 0,
            input_cache_miss_tokens: 0,
            input_tokens: 0,
            total_tokens: 0,
            requests: 0,
            cost: 0,
            theoretical_cost: 0,
        };
        mergeModelStat(target.by_model[modelName], source.by_model[modelName]);
    });
    return target;
}

function mergeDailyResponses(responses) {
    var byDate = Object.create(null);
    (responses || []).forEach(function (response) {
        (response && response.days || []).forEach(function (day) {
            if (!day || !day.date) return;
            if (!byDate[day.date]) byDate[day.date] = emptyCalendarDay(day.date);
            mergeCalendarDay(byDate[day.date], day);
        });
    });
    return Object.keys(byDate).sort().map(function (date) { return byDate[date]; });
}

function calendarDateString(year, month, day) {
    return year + '-' + String(month).padStart(2, '0') + '-' + String(day).padStart(2, '0');
}

function calendarDateRange(year, mode, month) {
    var dates = [];
    if (mode === 'year') {
        for (var m = 1; m <= 12; m++) {
            var yearDays = new Date(year, m, 0).getDate();
            for (var yd = 1; yd <= yearDays; yd++) {
                dates.push(calendarDateString(year, m, yd));
            }
        }
        return dates;
    }
    var daysInMonth = new Date(year, month, 0).getDate();
    for (var d = 1; d <= daysInMonth; d++) {
        dates.push(calendarDateString(year, month, d));
    }
    return dates;
}

function fillCalendarDateRange(days, year, mode, month) {
    var existing = Object.create(null);
    (days || []).forEach(function (day) {
        if (day && day.date) existing[day.date] = day;
    });
    return calendarDateRange(year, mode, month).map(function (date) {
        return existing[date] || emptyCalendarDay(date);
    });
}

/** Merge raw API model keys into the same display units used by the summary. */
function mergeCalendarAliases(days) {
    var modelNames = [];
    (days || []).forEach(function (day) {
        Object.keys(day.by_model || {}).forEach(function (name) {
            if (modelNames.indexOf(name) < 0) modelNames.push(name);
        });
    });
    var aliasMaps = buildAliasMaps(modelNames);
    return (days || []).map(function (day) {
        var copy = Object.assign({}, day, { by_model: {} });
        Object.keys(day.by_model || {}).forEach(function (modelName) {
            var displayName = aliasMaps.modelToAlias[String(modelName).toLowerCase()]
                || modelName;
            if (!copy.by_model[displayName]) copy.by_model[displayName] = {
                output_tokens: 0,
                input_cache_hit_tokens: 0,
                input_cache_miss_tokens: 0,
                input_tokens: 0,
                total_tokens: 0,
                requests: 0,
                cost: 0,
                theoretical_cost: 0,
            };
            mergeModelStat(copy.by_model[displayName], day.by_model[modelName]);
        });
        return copy;
    });
}

function setCalendarLoading(message) {
    var dom = document.getElementById('chartCalendar3D');
    var loader = document.getElementById('loadingCalendar3D');
    var compatibility = document.getElementById('calendarCompatibility');
    if (dom) dom.style.display = 'none';
    if (compatibility) compatibility.style.display = 'none';
    if (loader) {
        loader.classList.remove('loading--text');
        loader.textContent = message || '加载中';
        loader.style.display = 'flex';
    }
}

function populateSelectOptions(select, values, selectedValue, labelForValue) {
    if (!select) return;
    select.innerHTML = '';
    values.forEach(function (value) {
        var option = document.createElement('option');
        option.value = String(value);
        option.textContent = labelForValue ? labelForValue(value) : String(value);
        select.appendChild(option);
    });
    if (values.some(function (value) { return String(value) === String(selectedValue); })) {
        select.value = String(selectedValue);
    }
}

function updateCalendarViewControls() {
    var monthButton = document.getElementById('calendarMonthView');
    var yearButton = document.getElementById('calendarYearView');
    var monthControls = document.getElementById('calendarMonthControls');
    var yearControls = document.getElementById('calendarYearControls');
    var isMonth = calendarState.mode === 'month';
    if (monthButton) {
        monthButton.classList.toggle('calendar-view-toggle__button--active', isMonth);
        monthButton.setAttribute('aria-pressed', String(isMonth));
    }
    if (yearButton) {
        yearButton.classList.toggle('calendar-view-toggle__button--active', !isMonth);
        yearButton.setAttribute('aria-pressed', String(!isMonth));
    }
    if (monthControls) monthControls.hidden = !isMonth;
    if (yearControls) yearControls.hidden = isMonth;
}

function populateCalendarSelectors(months) {
    var current = getBrowserCurrentMonth();
    var years = [];
    (months || []).forEach(function (item) {
        var year = Number(item && item.year);
        if (year && years.indexOf(year) < 0) years.push(year);
    });
    if (years.indexOf(current.year) < 0) years.push(current.year);
    years.sort(function (a, b) { return a - b; });

    if (!calendarState.year || years.indexOf(calendarState.year) < 0) {
        var hasCurrentMonth = (months || []).some(function (item) {
            return Number(item.year) === current.year && Number(item.month) === current.month;
        });
        if (hasCurrentMonth || !months || !months.length) {
            calendarState.year = current.year;
            calendarState.month = current.month;
        } else {
            var latest = months.slice().sort(function (a, b) {
                return Number(a.year) - Number(b.year) || Number(a.month) - Number(b.month);
            }).pop();
            calendarState.year = Number(latest.year);
            calendarState.month = Number(latest.month);
        }
    }
    if (!calendarState.month || calendarState.month < 1 || calendarState.month > 12) {
        calendarState.month = current.month;
    }

    populateSelectOptions(
        document.getElementById('calendarMonthYear'), years, calendarState.year,
        function (value) { return value + ' 年'; }
    );
    populateSelectOptions(
        document.getElementById('calendarYearSelect'), years, calendarState.year,
        function (value) { return value + ' 年'; }
    );
    populateSelectOptions(
        document.getElementById('calendarMonthMonth'),
        Array.from({ length: 12 }, function (_, index) { return index + 1; }),
        calendarState.month,
        function (value) { return value + ' 月'; }
    );
    updateCalendarViewControls();
}

function isCurrentCalendarRequest(sequence, scopeKey, mode, year, month, dataGeneration) {
    return sequence === calendarLoadSequence && scopeKey === (currentUserId || '') &&
        dataGeneration === dashboardDataGeneration &&
        calendarState.mode === mode && calendarState.year === year &&
        (mode === 'year' || calendarState.month === month);
}

async function loadCalendarData() {
    var sequence = ++calendarLoadSequence;
    var scopeKey = currentUserId || '';
    var dataGeneration = dashboardDataGeneration;
    var mode = calendarState.mode === 'year' ? 'year' : 'month';
    var year = Number(calendarState.year || getBrowserCurrentMonth().year);
    var month = Number(calendarState.month || getBrowserCurrentMonth().month);
    calendarState.year = year;
    calendarState.month = month;
    setCalendarLoading();

    try {
        await loadDisplayConfig();
        var summary = await fetchDashboardSummary();
        var currentModels = await fetchCurrentMonthUsedModels();
        if (!isCurrentCalendarRequest(sequence, scopeKey, mode, year, month, dataGeneration)) return;

        var responses;
        if (mode === 'year') {
            responses = await Promise.all(Array.from({ length: 12 }, function (_, index) {
                return fetchDashboardDaily(year, index + 1);
            }));
        } else {
            responses = [await fetchDashboardDaily(year, month)];
        }
        if (!isCurrentCalendarRequest(sequence, scopeKey, mode, year, month, dataGeneration)) return;

        var rawDays = mergeDailyResponses(responses);
        var days = fillCalendarDateRange(rawDays, year, mode, month);
        days = mergeCalendarAliases(days);
        var entries = buildVisibleModelEntries(summary, currentModels);
        calendarState.days = days;
        renderCalendar3D('chartCalendar3D', days, entries, {
            mode: mode,
            year: year,
            month: month,
            loaderId: 'loadingCalendar3D',
            compatibilityId: 'calendarCompatibility',
        });
        var label = mode === 'year'
            ? year + ' 年'
            : year + ' 年 ' + month + ' 月';
        var labelEl = document.getElementById('calendarScopeLabel');
        if (labelEl) labelEl.textContent = label + ' · 按 Token 堆叠';
    } catch (error) {
        if (!isCurrentCalendarRequest(sequence, scopeKey, mode, year, month, dataGeneration)) return;
        console.error('Failed to load calendar data:', error);
        var loader = document.getElementById('loadingCalendar3D');
        if (loader) {
            loader.classList.add('loading--text');
            loader.textContent = '加载失败';
            loader.style.display = 'flex';
        }
    }
}

// ── Pie charts ──

async function loadModelPie() {
    await loadDisplayConfig();  // Ensure config is loaded (may race with loadSummary)
    var requestKey = currentUserId || '';
    var requestGeneration = dashboardDataGeneration;
    var loader = document.getElementById('loadingModelPie');
    if (!loader) return;
    try {
        var data = await fetchDashboardSummary();
        var currentModels = await fetchCurrentMonthUsedModels();
        if (requestKey !== (currentUserId || '') ||
            requestGeneration !== dashboardDataGeneration) return;
        updateGlobalTokenShares(data);
        var modelEntries = buildVisibleModelEntries(data, currentModels);
        var pieData = modelEntries.filter(function (entry) {
            return entry.tokens > 0;
        }).map(function (entry) {
            return {
                name: entry.name,
                value: entry.tokens,
                theoretical_cost: entry.theoretical_cost,
                rank: entry.rank,
                color: entry.color,
            };
        });

        // The color is carried by the ranked entry so the pie and 3D calendar
        // remain identical even when hidden models leave rank gaps.
        renderPieChart('chartModelPie', pieData);
        loader.style.display = 'none';
    } catch (err) {
        if (requestKey !== (currentUserId || '') ||
            requestGeneration !== dashboardDataGeneration) return;
        console.error('Failed to load model pie:', err);
        loader.classList.add('loading--text'); loader.textContent = '加载失败';
    }
}

async function loadTypePie() {
    var requestKey = currentUserId || '';
    var requestGeneration = dashboardDataGeneration;
    var loader = document.getElementById('loadingTypePie');
    if (!loader) return;
    try {
        var data = await fetchTokenTypes();
        if (requestKey !== (currentUserId || '') ||
            requestGeneration !== dashboardDataGeneration) return;
        var filtered = data.filter(function (d) { return d.value > 0; });
        renderPieChart('chartTypePie', filtered, ['#B45F45', '#6E8B77', '#C08B42']);
        loader.style.display = 'none';
    } catch (err) {
        if (requestKey !== (currentUserId || '') ||
            requestGeneration !== dashboardDataGeneration) return;
        console.error('Failed to load type pie:', err);
        loader.classList.add('loading--text'); loader.textContent = '加载失败';
    }
}

async function refreshDashboardVisuals() {
    // Load the summary first so calendar selectors and global model ranks are
    // settled before the pie and calendar perform their own rendering.
    await loadSummary();
    await Promise.all([loadModelPie(), loadTypePie(), loadCalendarData()]);
}

function invalidateCalendarLoad() {
    calendarLoadSequence++;
}

function invalidateDashboardData() {
    dashboardDataGeneration++;
    summaryData = null;
    summaryRequest = null;
    summaryRequestKey = null;
    globalTokenShares = null;
    globalTokenSharesKey = null;
    resetDailyDataCache();
    resetCurrentMonthModelCache();
}

// ── Event handlers ──

function bindDashboardEvents() {
    var keySel = document.getElementById('keyNameSelector');
    if (keySel && !keySel._bound) {
        keySel._bound = true;
        keySel.addEventListener('change', async function () {
            currentUserId = this.value;
            invalidateDashboardData();
            invalidateCalendarLoad();
            try {
                await refreshDashboardVisuals();
            } catch (error) {
                console.error('Failed to switch dashboard user:', error);
            }
        });
    }

    var monthView = document.getElementById('calendarMonthView');
    if (monthView && !monthView._bound) {
        monthView._bound = true;
        monthView.addEventListener('click', function () {
            if (calendarState.mode === 'month') return;
            calendarState.mode = 'month';
            invalidateCalendarLoad();
            updateCalendarViewControls();
            loadCalendarData();
        });
    }
    var yearView = document.getElementById('calendarYearView');
    if (yearView && !yearView._bound) {
        yearView._bound = true;
        yearView.addEventListener('click', function () {
            if (calendarState.mode === 'year') return;
            calendarState.mode = 'year';
            invalidateCalendarLoad();
            updateCalendarViewControls();
            loadCalendarData();
        });
    }

    var monthYear = document.getElementById('calendarMonthYear');
    if (monthYear && !monthYear._bound) {
        monthYear._bound = true;
        monthYear.addEventListener('change', function () {
            calendarState.year = Number(this.value);
            var yearSelect = document.getElementById('calendarYearSelect');
            if (yearSelect) yearSelect.value = this.value;
            invalidateCalendarLoad();
            loadCalendarData();
        });
    }
    var monthMonth = document.getElementById('calendarMonthMonth');
    if (monthMonth && !monthMonth._bound) {
        monthMonth._bound = true;
        monthMonth.addEventListener('change', function () {
            calendarState.month = Number(this.value);
            invalidateCalendarLoad();
            loadCalendarData();
        });
    }
    var yearSelect = document.getElementById('calendarYearSelect');
    if (yearSelect && !yearSelect._bound) {
        yearSelect._bound = true;
        yearSelect.addEventListener('change', function () {
            calendarState.year = Number(this.value);
            var monthYearSelect = document.getElementById('calendarMonthYear');
            if (monthYearSelect) monthYearSelect.value = this.value;
            invalidateCalendarLoad();
            loadCalendarData();
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
                deleteMoreUser(deleteBtn.dataset.userId, deleteBtn);
                return;
            }
            var btn = e.target.closest ? e.target.closest('.more-user-item') : null;
            if (btn) selectMoreUser(btn.dataset.userId);
        });
    }
}

// ── Refresh ──

async function refreshData() {
    try {
        await fetchRefresh();
        // Invalidate cached summaries and calendar data after importing fresh
        // data. The generation also prevents an earlier same-scope response
        // from painting over the refreshed dashboard.
        invalidateDashboardData();
        invalidateCalendarLoad();
        await refreshDashboardVisuals();
    } catch (err) {
        console.error('Refresh failed:', err);
        alert('刷新失败，请检查服务器日志');
    }
}

// ── Initialise (exported) ──

function initDashboard() {
    var el = document.getElementById('page-dashboard');
    if (!el) return;
    if (!el.dataset.initialized) {
        el.dataset.initialized = '1';
        bindDashboardEvents();
    } else {
        // The maintenance/import worker may have recorded new usage while
        // another page was open. Re-entering the dashboard must therefore
        // recalculate the current-month exception instead of reusing it.
        invalidateDashboardData();
        invalidateCalendarLoad();
    }
    refreshDashboardVisuals().catch(function (error) {
        console.error('Failed to initialise dashboard:', error);
    });
}
