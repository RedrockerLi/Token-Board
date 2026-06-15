/**
 * charts.js — ECharts rendering layer.
 *
 * All chart creation and option configuration lives here.
 * Functions expect pre-fetched data — no HTTP calls.
 */

// ── Color palette ──
const chartColors = ['#0070F3', '#00CEF3', '#22C55E', '#F59E0B', '#8B5CF6', '#EF4444', '#EC4899', '#6366F1'];

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

    chart.setOption({
        tooltip: {
            trigger: 'axis',
            backgroundColor: '#fff',
            borderColor: '#E8EBF0',
            textStyle: { color: '#020E36', fontSize: 13 },
            boxShadow: '0 4px 16px rgba(0,0,0,0.08)',
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
                html += '费用: <b>' + fmtCost(d.cost) + '</b><br/>';
                return html;
            }
        },
        legend: {
            data: ['输出Token', '输入Token', '费用'],
            bottom: 0,
            textStyle: { fontSize: 12, color: '#6B7194' }
        },
        grid: { left: 70, right: 70, top: 16, bottom: 40 },
        xAxis: {
            type: 'category',
            data: labels,
            axisLine: { lineStyle: { color: '#E8EBF0' } },
            axisTick: { show: false },
            axisLabel: { color: '#9094A2', fontSize: 11, rotate: xAxisLabelRotate || 0 }
        },
        yAxis: [
            {
                type: 'value',
                name: 'Tokens',
                nameTextStyle: { color: '#9094A2', fontSize: 11 },
                axisLabel: {
                    color: '#9094A2',
                    fontSize: 11,
                    formatter: function (v) { return fmtNum(v); }
                },
                splitLine: { lineStyle: { color: '#F0F1F5', type: 'dashed' } }
            },
            {
                type: 'value',
                name: '费用 (CNY)',
                nameTextStyle: { color: '#9094A2', fontSize: 11 },
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
                itemStyle: { color: '#0070F3' },
                barMaxWidth: 28
            },
            {
                name: '输入Token',
                type: 'bar',
                stack: 'tokens',
                yAxisIndex: 0,
                data: inputTokens,
                itemStyle: { color: '#00CEF3', borderRadius: [4, 4, 0, 0] },
                barMaxWidth: 28
            },
            {
                name: '费用',
                type: 'line',
                yAxisIndex: 1,
                data: cost,
                lineStyle: { color: '#EF4444', width: 2.5 },
                itemStyle: { color: '#EF4444' },
                symbol: 'circle',
                symbolSize: 6
            }
        ]
    });

    if (loader) loader.style.display = 'none';
    dom.style.display = 'block';
}

// ── Pie / donut chart ──

/**
 * Render a donut pie chart.
 *
 * @param {string} domId     - DOM id of the chart container
 * @param {object[]} pieData - [{ name, value }, ...]
 * @param {string[]} colors  - color array for slices
 */
function renderPieChart(domId, pieData, colors) {
    var chart = initChart(domId);
    chart.setOption({
        tooltip: {
            trigger: 'item',
            backgroundColor: '#fff',
            borderColor: '#E8EBF0',
            textStyle: { color: '#020E36' },
            formatter: function (p) {
                return p.name + '<br/>Tokens: ' + fmtNum(p.value) + ' (' + p.percent + '%)';
            }
        },
        legend: {
            orient: 'vertical',
            right: 10,
            top: 'center',
            textStyle: { fontSize: 12, color: '#6B7194' }
        },
        series: [{
            type: 'pie',
            radius: ['45%', '75%'],
            center: ['35%', '50%'],
            avoidLabelOverlap: false,
            itemStyle: { borderRadius: 4, borderColor: '#fff', borderWidth: 2 },
            label: { show: false },
            emphasis: {
                label: { show: true, fontSize: 14, fontWeight: 'bold' }
            },
            data: pieData,
            color: colors
        }]
    });
}
