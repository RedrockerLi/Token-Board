/**
 * charts.js — ECharts rendering layer.
 *
 * All chart creation and option configuration lives here.
 * Functions expect pre-fetched data — no HTTP calls.
 */

// ── Color palette (system-like accent colors with enough separation) ──
// Keep this small palette for the unrelated proxy charts.  Model charts use
// generateChartColor(), whose output is based on rank and does not repeat.
const chartColors = ['#B45F45', '#6E8B77', '#6F8A5D', '#C08B42', '#927CA6', '#A75558', '#8F735B', '#A57B93'];

function _chartHslToHex(h, s, l) {
    s /= 100;
    l /= 100;
    var c = (1 - Math.abs(2 * l - 1)) * s;
    var x = c * (1 - Math.abs((h / 60) % 2 - 1));
    var m = l - c / 2;
    var r = 0;
    var g = 0;
    var b = 0;
    if (h < 60) { r = c; g = x; }
    else if (h < 120) { r = x; g = c; }
    else if (h < 180) { g = c; b = x; }
    else if (h < 240) { g = x; b = c; }
    else if (h < 300) { r = x; b = c; }
    else { r = c; b = x; }
    var channel = function (value) {
        return Math.round((value + m) * 255).toString(16).padStart(2, '0');
    };
    return '#' + channel(r) + channel(g) + channel(b);
}

/**
 * Return the deterministic model color for a zero-based rank.
 *
 * A warm terracotta hue anchors the palette.  Following ranks advance by the
 * golden angle, keeping neighbouring models visually distinct without using
 * a repeating color array.  Small, deterministic saturation/lightness shifts
 * retain the dashboard's muted character while improving stacked-bar
 * separation.  The result depends only on rank, never on the model count.
 */
function generateChartColor(rank) {
    var value = Number(rank);
    if (!isFinite(value)) value = 0;
    value = Math.max(0, Math.floor(value));
    var goldenAngle = 137.50776405003785;
    var hue = (18 + value * goldenAngle) % 360;
    var saturationSteps = [46, 40, 44, 38, 48];
    var lightnessSteps = [49, 45, 53, 47, 51, 43];
    var saturation = saturationSteps[value % saturationSteps.length];
    var lightness = lightnessSteps[Math.floor(value / saturationSteps.length) % lightnessSteps.length];
    return _chartHslToHex(hue, saturation, lightness);
}

// ── Shared visual style (purely cosmetic — no data semantics) ──

function _vGrad(top, bottom) {
    return new echarts.graphic.LinearGradient(0, 0, 0, 1, [
        { offset: 0, color: top },
        { offset: 1, color: bottom }
    ]);
}

var CHART_ANIM = {
    animationDuration: 520,
    animationEasing: 'cubicOut',
    animationDurationUpdate: 360,
    // Match the UI font instead of ECharts' default sans
    textStyle: {
        fontFamily: '-apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", Roboto, "Helvetica Neue", Arial, "Microsoft YaHei", sans-serif'
    }
};
var TOOLTIP_STYLE = {
    backgroundColor: 'rgba(255,253,249,0.97)',
    borderColor: 'rgba(59,50,44,0.14)',
    borderRadius: 12,
    padding: [10, 13],
    extraCssText: 'box-shadow: 0 7px 18px rgba(61,48,39,0.12), 0 24px 50px rgba(61,48,39,0.12);'
};

// ── Chart lifecycle ──

/** Initialise (or re-initialise) an ECharts instance on a DOM element. */
function initChart(domId) {
    const dom = document.getElementById(domId);
    if (!dom) return null;
    const existing = echarts.getInstanceByDom(dom);
    if (existing) existing.dispose();
    const chart = echarts.init(dom, null, { renderer: 'svg' });
    const ro = new ResizeObserver(function () { chart.resize(); });
    ro.observe(dom);
    // ECharts does not own external observers. Tie the observer to the chart's
    // lifecycle so 15-second dashboard refreshes do not accumulate callbacks
    // retaining disposed chart/DOM instances.
    const dispose = chart.dispose.bind(chart);
    chart.dispose = function () {
        ro.disconnect();
        dispose();
    };
    return chart;
}

// ── Time-series chart (bars + line) ──

/**
 * Render a dual-axis time-series chart with token bars and a request-count line.
 *
 * @param {string} chartId         - DOM id of the chart container
 * @param {string} loaderId        - DOM id of the loading placeholder
 * @param {string[]} labels        - X-axis labels (dates or month labels)
 * @param {number[]} outputTokens  - output token counts
 * @param {number[]} inputTokens   - input token counts
 * @param {number[]} requests      - request counts
 * @param {number[]} cost          - cost values (unused in rendering, kept for signature compat)
 * @param {object[]} rawData       - original record objects for tooltip detail
 * @param {number} xAxisLabelRotate - rotation angle for x-axis labels
 */
function renderTimeSeriesChart(chartId, loaderId, labels, outputTokens, inputTokens,
                                requests, cost, rawData, xAxisLabelRotate) {
    var dom = document.getElementById(chartId);
    var loader = document.getElementById(loaderId);
    var chart = initChart(chartId);

    chart.setOption(Object.assign({
        tooltip: Object.assign({
            trigger: 'axis',
            textStyle: { color: '#24211F', fontSize: 13 },
            formatter: function (params) {
                var idx = params[0] && params[0].dataIndex;
                if (idx == null) return '';
                var d = rawData[idx];
                var total = d.total_tokens || 1;
                var pct = function (v) { return (v / total * 100).toFixed(1); };
                var html = '<b>' + (d.date || d.label) + '</b><br/>';
                html += '输出Token: <b>' + fmtNum(d.output_tokens) + '</b> (' + pct(d.output_tokens) + '%)<br/>';
                html += '输入缓存命中: <b>' + fmtNum(d.input_cache_hit_tokens) + '</b> (' + pct(d.input_cache_hit_tokens) + '%)<br/>';
                html += '输入缓存未命中: <b>' + fmtNum(d.input_cache_miss_tokens) + '</b> (' + pct(d.input_cache_miss_tokens) + '%)<br/>';
                html += '消费: <b>' + fmtCost(d.cost) + '</b><br/>';
                return html;
            }
        }, TOOLTIP_STYLE),
        legend: {
            data: ['输出Token', '输入Token', '消费'],
            bottom: 0,
            textStyle: { fontSize: 12, color: '#716B65' }
        },
        grid: { left: 70, right: 70, top: 16, bottom: 40 },
        xAxis: {
            type: 'category',
            data: labels,
            axisLine: { lineStyle: { color: '#D5CEC5' } },
            axisTick: { show: false },
            axisLabel: { show: false }
        },
        yAxis: [
            {
                type: 'value',
                axisLabel: {
                    color: '#746B64',
                    fontSize: 11,
                    formatter: function (v) { return fmtNum(v); }
                },
                splitLine: { lineStyle: { color: '#E7E1D9', type: 'dashed' } }
            },
            {
                type: 'value',
                axisLabel: { color: '#746B64', fontSize: 11, formatter: function (v) { return fmtCost(v); } },
                splitLine: { show: false }
            }
        ],
        series: [
            {
                name: '输出Token',
                type: 'bar',
                stack: 'tokens',
                yAxisIndex: 0,
                data: outputTokens,
                itemStyle: { color: _vGrad('#CF876D', '#B45F45') },
                barMaxWidth: 26
            },
            {
                name: '输入Token',
                type: 'bar',
                stack: 'tokens',
                yAxisIndex: 0,
                data: inputTokens,
                itemStyle: { color: _vGrad('#A8BF92', '#6E8B77'), borderRadius: [4, 4, 0, 0] },
                barMaxWidth: 26
            },
            {
                name: '消费',
                type: 'line',
                yAxisIndex: 1,
                data: cost,
                lineStyle: { color: '#927CA6', width: 2.25 },
                itemStyle: { color: '#927CA6', borderColor: '#fffdf9', borderWidth: 1.5 },
                areaStyle: { color: _vGrad('rgba(146,124,166,0.16)', 'rgba(146,124,166,0)') },
                symbol: 'circle',
                symbolSize: 5
            }
        ]
    }, CHART_ANIM));

    if (loader) loader.style.display = 'none';
    dom.style.display = 'block';
}

// ── Pie / donut chart ──

/**
 * Render a donut pie chart.
 *
 * @param {string} domId     - DOM id of the chart container
 * @param {object[]} pieData - [{ name, value, theoretical_cost? }, ...]
 * @param {string[]} colors  - color array for slices
 */
function renderPieChart(domId, pieData, colors) {
    var chart = initChart(domId);
    if (!chart) return null;
    var palette = colors || [];
    var coloredData = (pieData || []).map(function (item, index) {
        var color = item.color || palette[index] ||
            generateChartColor(item.rank == null ? index : item.rank);
        return Object.assign({}, item, {
            itemStyle: Object.assign({}, item.itemStyle || {}, { color: color }),
        });
    });
    chart.setOption(Object.assign({
        tooltip: Object.assign({
            trigger: 'item',
            textStyle: { color: '#24211F' },
            formatter: function (p) {
                var html = p.name + '<br/>Tokens: ' + fmtNum(p.value) +
                    ' (' + p.percent + '%)';
                if (p.data && p.data.theoretical_cost != null) {
                    html += '<br/>理论花费: ' +
                        fmtCost(p.data.theoretical_cost);
                }
                return html;
            }
        }, TOOLTIP_STYLE),
        legend: {
            orient: 'vertical',
            right: 10,
            top: 'center',
            icon: 'circle',
            itemWidth: 9,
            itemHeight: 9,
            textStyle: { fontSize: 12, color: '#716B65' }
        },
        series: [{
            type: 'pie',
            radius: ['48%', '76%'],
            center: ['35%', '50%'],
            avoidLabelOverlap: false,
            padAngle: 2,
            itemStyle: { borderRadius: 7, borderColor: '#F4F1EC', borderWidth: 2 },
            label: { show: false },
            emphasis: {
                scale: true,
                scaleSize: 5,
                label: { show: true, fontSize: 14, fontWeight: 'bold' }
            },
            data: coloredData,
        }]
    }, CHART_ANIM));
    return chart;
}

// ── WebGL 3D calendar ──────────────────────────────────────────────────────

function _calendarEscape(value) {
    if (typeof esc === 'function') return esc(String(value == null ? '' : value));
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (ch) {
        return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch];
    });
}

function _calendarNumber(value) {
    var number = Number(value);
    return isFinite(number) ? number : 0;
}

function _calendarFormatNumber(value) {
    if (typeof fmtNum === 'function') return fmtNum(_calendarNumber(value));
    return _calendarNumber(value).toLocaleString('en-US');
}

function _calendarDateParts(value) {
    var match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value || ''));
    if (!match) return null;
    return { year: Number(match[1]), month: Number(match[2]), day: Number(match[3]) };
}

function _calendarDaysInMonth(year, month) {
    return new Date(year, month, 0).getDate();
}

function _calendarDayOfYear(parts) {
    return Math.floor(
        (Date.UTC(parts.year, parts.month - 1, parts.day) -
            Date.UTC(parts.year, 0, 1)) / 86400000
    );
}

/**
 * Map each local calendar date to its 3D category coordinates.
 *
 * Month view uses seven weekday columns (Sunday first) and one depth category
 * per week. Year view uses one column per continuous seven-day slice and seven
 * depth categories for weekdays (also Sunday first), producing the familiar
 * 7 × 52/53 heat-calendar shape while retaining the 3D bar interaction.
 */
function buildCalendar3DLayout(days, mode, calendarOptions) {
    var options = calendarOptions || {};
    var source = Array.isArray(days) ? days : [];
    var first = source.length ? _calendarDateParts(source[0].date) : null;
    var year = Number(options.year || (first && first.year));
    var month = Number(options.month || (first && first.month));
    var viewMode = mode === 'year' ? 'year' : 'month';
    var weekdays = ['日', '一', '二', '三', '四', '五', '六'];
    var dateCoordinates = {};
    var xLabels;
    var zLabels;
    var weekCount;

    if (viewMode === 'month') {
        if (!year || !month) {
            return { mode: viewMode, year: year, month: month, dates: [],
                xLabels: weekdays, zLabels: [], coordinates: dateCoordinates };
        }
        var firstWeekday = new Date(year, month - 1, 1).getDay();
        var daysInMonth = _calendarDaysInMonth(year, month);
        weekCount = Math.ceil((firstWeekday + daysInMonth) / 7);
        xLabels = weekdays.slice();
        zLabels = Array.from({ length: weekCount }, function (_, index) {
            return '第' + (index + 1) + '周';
        });
        source.forEach(function (day) {
            var parts = _calendarDateParts(day.date);
            if (!parts || parts.year !== year || parts.month !== month) return;
            var weekday = new Date(parts.year, parts.month - 1, parts.day).getDay();
            var week = Math.floor((firstWeekday + parts.day - 1) / 7);
            dateCoordinates[day.date] = { x: weekday, y: week };
        });
    } else {
        if (!year) {
            return { mode: viewMode, year: year, month: month, dates: [],
                xLabels: [], zLabels: weekdays, coordinates: dateCoordinates };
        }
        var lastDayOfYear = new Date(year, 11, 31);
        var lastDayOfYearParts = {
            year: year, month: 12, day: lastDayOfYear.getDate()
        };
        // A complete year has 53 seven-day columns at most.  Starting the
        // continuous grid at Jan 1 avoids the rare 54-column result that a
        // Sunday-aligned calculation produces for a leap year beginning on
        // Saturday, while every date still gets its own cell.
        weekCount = Math.ceil((_calendarDayOfYear(lastDayOfYearParts) + 1) / 7);
        xLabels = Array.from({ length: weekCount }, function (_, index) {
            return String(index + 1);
        });
        zLabels = weekdays.slice();
        source.forEach(function (day) {
            var parts = _calendarDateParts(day.date);
            if (!parts || parts.year !== year) return;
            var weekday = new Date(parts.year, parts.month - 1, parts.day).getDay();
            var week = Math.floor(_calendarDayOfYear(parts) / 7);
            dateCoordinates[day.date] = { x: week, y: weekday };
        });
    }

    return {
        mode: viewMode,
        year: year,
        month: month,
        dates: source.map(function (day) { return day.date; }),
        xLabels: xLabels,
        zLabels: zLabels,
        coordinates: dateCoordinates,
        weekCount: weekCount,
    };
}

function _calendarHasWebGL() {
    if (typeof window !== 'undefined' && window.__tokenBoardEchartsGLFailed) return false;
    if (typeof document === 'undefined' || !document.createElement) return false;
    try {
        var canvas = document.createElement('canvas');
        return !!(canvas.getContext && (
            canvas.getContext('webgl') || canvas.getContext('experimental-webgl') ||
            canvas.getContext('webgl2')
        ));
    } catch (_) {
        return false;
    }
}

function _setCalendarCompatibility(domId, loaderId, compatibilityId, message) {
    var dom = document.getElementById(domId);
    var loader = loaderId && document.getElementById(loaderId);
    var compatibility = compatibilityId && document.getElementById(compatibilityId);
    if (dom) dom.style.display = 'none';
    if (loader) loader.style.display = 'none';
    if (compatibility) {
        compatibility.textContent = message;
        compatibility.style.display = 'flex';
    }
}

function _calendarResizeBinding(chart, dom) {
    if (typeof ResizeObserver !== 'undefined') {
        var observer = new ResizeObserver(function () { chart.resize(); });
        observer.observe(dom);
        return function () { observer.disconnect(); };
    }
    var resize = function () { chart.resize(); };
    window.addEventListener('resize', resize);
    return function () { window.removeEventListener('resize', resize); };
}

function _initCalendarChart(domId) {
    var dom = document.getElementById(domId);
    if (!dom || typeof echarts === 'undefined' || typeof echarts.init !== 'function') return null;
    var existing = typeof echarts.getInstanceByDom === 'function'
        ? echarts.getInstanceByDom(dom) : null;
    if (existing) existing.dispose();
    var chart = echarts.init(dom, null, { renderer: 'canvas' });
    var unbindResize = _calendarResizeBinding(chart, dom);
    var dispose = chart.dispose.bind(chart);
    chart.dispose = function () {
        unbindResize();
        dispose();
    };
    return chart;
}

function _calendarModelTokens(day, entry) {
    var model = day && day.by_model && day.by_model[entry.name];
    return _calendarNumber(model && model.total_tokens);
}

function _calendarTooltip(day, modelEntries) {
    var html = '<b>' + _calendarEscape(day.date) + '</b><br/>' +
        '总 Token: <b>' + _calendarFormatNumber(day.total_tokens) + '</b><br/>' +
        '请求数: <b>' + _calendarFormatNumber(day.requests) + '</b>';
    var rows = [];
    (modelEntries || []).forEach(function (entry) {
        var tokens = _calendarModelTokens(day, entry);
        if (tokens > 0) {
            rows.push('<span style="color:' + entry.color + '">●</span> ' +
                _calendarEscape(entry.name) + ': <b>' + _calendarFormatNumber(tokens) + '</b>');
        }
    });
    if (rows.length) html += '<br/>' + rows.join('<br/>');
    return html;
}

/**
 * Render the interactive stacked WebGL calendar.
 *
 * `days` must contain display-unit `by_model` values.  Dashboard orchestration
 * performs alias merging and filtering before calling this renderer, which
 * keeps the renderer independent from the API and makes the same color/rank
 * entries usable by the pie chart and its legend.
 */
function renderCalendar3D(domId, days, modelEntries, calendarOptions) {
    var options = calendarOptions || {};
    var loaderId = options.loaderId || 'loadingCalendar3D';
    var compatibilityId = options.compatibilityId || 'calendarCompatibility';
    var dom = document.getElementById(domId);
    if (!dom) return null;

    if (typeof echarts === 'undefined' || typeof echarts.init !== 'function' ||
        !_calendarHasWebGL()) {
        _setCalendarCompatibility(domId, loaderId, compatibilityId,
            '当前浏览器无法使用 WebGL 3D 日历图，请启用硬件加速或更换浏览器。');
        return null;
    }

    var normalizedDays = (Array.isArray(days) ? days : []).slice().sort(function (a, b) {
        return String(a.date).localeCompare(String(b.date));
    });
    var entries = (Array.isArray(modelEntries) ? modelEntries : []).map(function (entry, index) {
        return Object.assign({}, entry, {
            color: entry.color || generateChartColor(entry.rank == null ? index : entry.rank),
        });
    });
    var layout = buildCalendar3DLayout(normalizedDays, options.mode, options);
    var dayByDate = {};
    normalizedDays.forEach(function (day) { dayByDate[day.date] = day; });
    var activeEntries = entries.filter(function (entry) {
        return normalizedDays.some(function (day) { return _calendarModelTokens(day, entry) > 0; });
    });
    var gridWidth = options.mode === 'year' ? 270 : 150;
    var gridDepth = options.mode === 'year' ? 80 : 112;
    // bar3D's barSize is expressed in grid coordinates, not as a fraction of
    // a category band. Slightly overlap the calculated cell to eliminate
    // sub-pixel seams between adjacent calendar bars.
    var barSize = [
        gridWidth / Math.max(layout.xLabels.length, 1) * 1.02,
        gridDepth / Math.max(layout.zLabels.length, 1) * 1.02,
    ];
    var chart;

    function buildSeries() {
        return activeEntries.map(function (entry) {
            var data = [];
            normalizedDays.forEach(function (day) {
                var coordinate = layout.coordinates[day.date];
                var tokens = _calendarModelTokens(day, entry);
                if (!coordinate) return;
                data.push({
                    // bar3D stacks along its third (z) dimension.  The two
                    // calendar categories therefore occupy x/y and Token is
                    // the vertical z value. Keep every model's dates aligned;
                    // sparse data would make stack offsets depend on array
                    // position instead of the calendar cell.
                    value: [coordinate.x, coordinate.y, tokens],
                    date: day.date,
                    model: entry.name,
                    silent: tokens <= 0,
                    itemStyle: {
                        color: entry.color,
                        opacity: tokens > 0 ? 1 : 0,
                        borderColor: 'rgba(255,253,249,0.35)',
                        borderWidth: tokens > 0 ? 0.4 : 0,
                    },
                });
            });
            return {
                name: entry.name,
                type: 'bar3D',
                stack: 'daily-token-total',
                // Keep model colors flat; lighting would add highlights and
                // shadows that obscure the token-color mapping.
                shading: 'color',
                data: data,
                // Fill the complete x/y category cell so adjacent calendar
                // bars touch instead of leaving a visible grid gap.
                barSize: barSize,
                label: { show: false },
                emphasis: { itemStyle: { opacity: 1 } },
            };
        });
    }

    try {
        // The calendar container is hidden while loading. Make it measurable
        // before WebGL initialisation; otherwise ECharts-GL can keep a tiny
        // fallback viewport in the top-left after the container is revealed.
        dom.style.display = 'block';
        chart = _initCalendarChart(domId);
        if (!chart) throw new Error('ECharts 初始化失败');
        chart.setOption({
            animation: true,
            animationDuration: 420,
            animationDurationUpdate: 260,
            animationEasing: 'cubicOut',
            textStyle: CHART_ANIM.textStyle,
            tooltip: Object.assign({
                trigger: 'item',
                textStyle: { color: '#24211F', fontSize: 13 },
                formatter: function (params) {
                    var date = params && params.data && params.data.date;
                    return date && dayByDate[date]
                        ? _calendarTooltip(dayByDate[date], entries) : '';
                },
            }, TOOLTIP_STYLE),
            legend: {
                type: 'scroll',
                bottom: 0,
                left: 8,
                right: 8,
                data: entries.map(function (entry) {
                    return { name: entry.name, itemStyle: { color: entry.color } };
                }),
                icon: 'roundRect',
                itemWidth: 12,
                itemHeight: 8,
                textStyle: { fontSize: 12, color: '#716B65' },
                formatter: function (name) { return String(name); },
            },
            grid3D: {
                boxWidth: gridWidth,
                boxDepth: gridDepth,
                boxHeight: 78,
                environment: '#F8F5F0',
                axisLine: { lineStyle: { color: 'rgba(113,107,101,0.42)' } },
                axisPointer: { show: false },
                // Keep the calendar floor clean; the side/back grid planes
                // should not read like a visible usage scale.
                splitLine: { show: false },
                viewControl: {
                    projection: 'perspective',
                    autoRotate: false,
                    distance: options.mode === 'year' ? 260 : 220,
                    alpha: 25,
                    beta: 35,
                    rotateSensitivity: 1,
                    zoomSensitivity: 1,
                    panSensitivity: 0.4,
                },
            },
            xAxis3D: {
                type: 'category',
                name: options.mode === 'year' ? '周' : '星期',
                data: layout.xLabels,
                axisLabel: { color: '#716B65', fontSize: options.mode === 'year' ? 9 : 11 },
                nameTextStyle: { color: '#716B65', fontSize: 11 },
            },
            yAxis3D: {
                type: 'category',
                name: options.mode === 'year' ? '星期' : '周',
                data: layout.zLabels,
                axisLabel: { color: '#716B65', fontSize: 10 },
                nameTextStyle: { color: '#716B65', fontSize: 11 },
            },
            zAxis3D: {
                // Keep the value axis in the coordinate system so bar heights
                // still map to Token values; hide only its visual elements.
                type: 'value',
                name: '',
                min: 0,
                // bar3D needs the z-axis line geometry to establish its
                // height scale. Keep that geometry alive but make it fully
                // transparent so no usage axis is visible.
                axisLine: {
                    show: true,
                    lineStyle: { color: 'rgba(0,0,0,0)', opacity: 0 },
                },
                axisTick: { show: false },
                axisLabel: { show: false },
                splitLine: { show: false },
                axisPointer: { show: false },
            },
            series: buildSeries(),
        });
        chart.resize();

        chart.__tokenBoardCalendar = {
            layout: layout,
            days: normalizedDays,
            entries: entries,
        };

        var loader = document.getElementById(loaderId);
        var compatibility = document.getElementById(compatibilityId);
        if (loader) loader.style.display = 'none';
        if (compatibility) compatibility.style.display = 'none';
        return chart;
    } catch (error) {
        console.error('Failed to render 3D calendar:', error);
        if (chart) chart.dispose();
        _setCalendarCompatibility(domId, loaderId, compatibilityId,
            '3D 日历图加载失败，请检查浏览器 WebGL 支持或网络连接。');
        return null;
    }
}
