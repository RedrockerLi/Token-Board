/**
 * charts.js — ECharts rendering layer.
 *
 * All chart creation and option configuration lives here.
 * Functions expect pre-fetched data — no HTTP calls.
 */

// ── Color palette (system-like accent colors with enough separation) ──
const chartColors = ['#B45F45', '#6E8B77', '#6F8A5D', '#C08B42', '#927CA6', '#A75558', '#8F735B', '#A57B93'];

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
            data: pieData,
            color: colors
        }]
    }, CHART_ANIM));
}
