/**
 * charts.js — ECharts rendering layer.
 *
 * All chart creation and option configuration lives here.
 * Functions expect pre-fetched data — no HTTP calls.
 */

// ── Color palette (Tailwind 500 family — uniform lightness/chroma) ──
const chartColors = ['#3B82F6', '#06B6D4', '#10B981', '#F59E0B', '#8B5CF6', '#F43F5E', '#6366F1', '#14B8A6'];

// ── Shared visual style (purely cosmetic — no data semantics) ──

function _vGrad(top, bottom) {
    return new echarts.graphic.LinearGradient(0, 0, 0, 1, [
        { offset: 0, color: top },
        { offset: 1, color: bottom }
    ]);
}

var CHART_ANIM = {
    animationDuration: 600,
    animationEasing: 'cubicOut',
    animationDurationUpdate: 400,
    // Match the UI font instead of ECharts' default sans
    textStyle: {
        fontFamily: 'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, "Microsoft YaHei", sans-serif'
    }
};
var TOOLTIP_STYLE = {
    backgroundColor: '#fff',
    borderColor: '#E8EBF0',
    borderRadius: 10,
    padding: [10, 14],
    extraCssText: 'box-shadow: 0 4px 12px rgba(2,14,54,0.08), 0 12px 28px rgba(2,14,54,0.08);'
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
            textStyle: { color: '#020E36', fontSize: 13 },
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
            textStyle: { fontSize: 12, color: '#6B7194' }
        },
        grid: { left: 70, right: 70, top: 16, bottom: 40 },
        xAxis: {
            type: 'category',
            data: labels,
            axisLine: { lineStyle: { color: '#E8EBF0' } },
            axisTick: { show: false },
            axisLabel: { show: false }
        },
        yAxis: [
            {
                type: 'value',
                axisLabel: {
                    color: '#9094A2',
                    fontSize: 11,
                    formatter: function (v) { return fmtNum(v); }
                },
                splitLine: { lineStyle: { color: '#F0F1F5', type: 'dashed' } }
            },
            {
                type: 'value',
                axisLabel: { color: '#9094A2', fontSize: 11, formatter: function (v) { return fmtCost(v); } },
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
                itemStyle: { color: _vGrad('#60A5FA', '#2563EB') },
                barMaxWidth: 28
            },
            {
                name: '输入Token',
                type: 'bar',
                stack: 'tokens',
                yAxisIndex: 0,
                data: inputTokens,
                itemStyle: { color: _vGrad('#4FD6F0', '#06B6D4'), borderRadius: [4, 4, 0, 0] },
                barMaxWidth: 28
            },
            {
                name: '消费',
                type: 'line',
                yAxisIndex: 1,
                data: cost,
                lineStyle: { color: '#EF4444', width: 2.5 },
                itemStyle: { color: '#EF4444', borderColor: '#fff', borderWidth: 1.5 },
                areaStyle: { color: _vGrad('rgba(239,68,68,0.14)', 'rgba(239,68,68,0)') },
                symbol: 'circle',
                symbolSize: 6
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
    chart.setOption(Object.assign({
        tooltip: Object.assign({
            trigger: 'item',
            textStyle: { color: '#020E36' },
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
            textStyle: { fontSize: 12, color: '#6B7194' }
        },
        series: [{
            type: 'pie',
            radius: ['48%', '76%'],
            center: ['35%', '50%'],
            avoidLabelOverlap: false,
            padAngle: 2,
            itemStyle: { borderRadius: 6, borderColor: '#fff', borderWidth: 2 },
            label: { show: false },
            emphasis: {
                scale: true,
                scaleSize: 5,
                label: { show: true, fontSize: 14, fontWeight: 'bold' }
            },
            data: pieData,
            color: colors
        }]
    }, CHART_ANIM));
}
