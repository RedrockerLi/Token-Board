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
var summaryRequest = null;     // in-flight/cached summary request for current scope
var summaryRequestKey = null;  // user + current natural month
var dashboardDataGeneration = 0;

var calendarState = {
    mode: 'month',
    year: null,
    month: null,
    days: [],
};
var calendarLoadSequence = 0;
var dailyDataCache = Object.create(null);
var dailyDataRequests = Object.create(null);
var dailyDataCacheGeneration = 0;

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
    var units = Object.create(null);
    Object.keys(breakdown).forEach(function (modelName) {
        var modelData = breakdown[modelName] || {};
        if (!units[modelName]) units[modelName] = {
            tokens: 0,
            weighted_tokens: 0,
            theoretical_cost: 0,
        };
        units[modelName].tokens += modelTokenCount(modelData);
        var weighted = Number(modelData.weighted_total_tokens);
        units[modelName].weighted_tokens += isFinite(weighted)
            ? Math.max(0, weighted) : modelTokenCount(modelData);
        units[modelName].theoretical_cost += Number(
            modelData.theoretical_cost || 0);
    });
    return units;
}

/**
 * Build the model set shared by the model pie and calendar.
 *
 * The full weighted total is the visibility denominator.  Once the strict
 * >1% filter is applied, visibleShare is recalculated against the visible
 * weighted total so the pie itself always sums to 100%.
 */
function buildModelEntries(data) {
    var units = aggregateModelBreakdown(data);
    var weightedTotal = Object.keys(units).reduce(function (sum, name) {
        return sum + units[name].weighted_tokens;
    }, 0);

    var ranked = Object.keys(units)
        .filter(function (name) { return units[name].tokens > 0; })
        .map(function (name) {
            return {
                name: name,
                tokens: units[name].tokens,
                weighted_tokens: units[name].weighted_tokens,
                theoretical_cost: units[name].theoretical_cost,
                fullShare: weightedTotal > 0
                    ? units[name].weighted_tokens / weightedTotal : 0,
            };
        })
        .sort(function (a, b) {
            return b.weighted_tokens - a.weighted_tokens ||
                String(a.name).localeCompare(String(b.name));
        });

    var visible = ranked.filter(function (entry) {
        return entry.fullShare > 0.01;
    });
    var visibleTotal = visible.reduce(function (sum, entry) {
        return sum + entry.weighted_tokens;
    }, 0);
    var visibleNames = new Set(visible.map(function (entry) { return entry.name; }));
    ranked.forEach(function (entry) {
        entry.visible = visibleNames.has(entry.name);
        if (entry.visible) {
            entry.rank = visible.indexOf(entry);
            entry.color = generateChartColor(entry.rank);
            entry.visibleShare = visibleTotal > 0
                ? entry.weighted_tokens / visibleTotal : 0;
        } else {
            entry.rank = null;
            entry.color = '#716B65';
            entry.visibleShare = 0;
        }
    });
    return { all: ranked, visible: visible, weightedTotal: weightedTotal };
}

/**
 * Collapse models hidden from the pie into one calendar entry.  The entry
 * keeps its source names so the calendar can retain per-model tooltip detail
 * while exposing only one neutral-gray `Other` item in the legend.
 */
function buildCalendarModelEntries(data) {
    var modelSet = buildModelEntries(data);
    var entries = modelSet.visible.slice();
    var hidden = modelSet.all.filter(function (entry) { return !entry.visible; });
    if (hidden.length) {
        entries.push({
            name: 'Other',
            models: hidden.map(function (entry) { return entry.name; }),
            tokens: hidden.reduce(function (sum, entry) {
                return sum + entry.tokens;
            }, 0),
            weighted_tokens: hidden.reduce(function (sum, entry) {
                return sum + entry.weighted_tokens;
            }, 0),
            theoretical_cost: hidden.reduce(function (sum, entry) {
                return sum + entry.theoretical_cost;
            }, 0),
            visible: false,
            rank: null,
            color: '#716B65',
        });
    }
    return entries;
}

function fetchDashboardSummary() {
    var current = getBrowserCurrentMonth();
    var requestKey = (currentUserId || '') + '|' + current.year + '-' + current.month;
    if (summaryRequest && summaryRequestKey === requestKey) return summaryRequest;

    var requestGeneration = dashboardDataGeneration;
    summaryRequestKey = requestKey;
    summaryRequest = fetchSummary(current.year, current.month).then(function (data) {
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
        var summary = await fetchDashboardSummary();
        var modelEntries = buildCalendarModelEntries(summary);
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
        calendarState.days = days;
        renderCalendar3D('chartCalendar3D', days, modelEntries, {
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

function showModelPieEmptyState(message) {
    var dom = document.getElementById('chartModelPie');
    var loader = document.getElementById('loadingModelPie');
    if (dom) {
        var chart = typeof echarts !== 'undefined' && echarts.getInstanceByDom
            ? echarts.getInstanceByDom(dom) : null;
        if (chart) chart.dispose();
        dom.style.display = 'none';
    }
    if (loader) {
        loader.classList.add('loading--text');
        loader.textContent = message || '暂无模型占比超过 1%';
        loader.style.display = 'flex';
    }
}

async function loadModelPie() {
    var requestKey = currentUserId || '';
    var requestGeneration = dashboardDataGeneration;
    var loader = document.getElementById('loadingModelPie');
    if (!loader) return;
    try {
        var data = await fetchDashboardSummary();
        if (requestKey !== (currentUserId || '') ||
            requestGeneration !== dashboardDataGeneration) return;
        var modelEntries = buildModelEntries(data).visible;
        var pieData = modelEntries.map(function (entry) {
            return {
                name: entry.name,
                value: entry.weighted_tokens,
                real_tokens: entry.tokens,
                theoretical_cost: entry.theoretical_cost,
                rank: entry.rank,
                color: entry.color,
            };
        });

        if (!pieData.length) {
            showModelPieEmptyState('暂无模型占比超过 1%');
            return;
        }

        var dom = document.getElementById('chartModelPie');
        if (dom) dom.style.display = 'block';
        loader.classList.remove('loading--text');
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
    resetDailyDataCache();
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
