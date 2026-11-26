'use strict';

// 消费报告页每日用量图的折线口径:折线展示的是 API 等值消费
// (request_log.equivalent_cost),不是实际消费(按量实付 + 周期月费)。
// 图例、tooltip 与数据列都必须跟着换口径,漏掉任何一处都会让读图的人
// 把实际消费当成 API 等值消费。

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const staticJs = (name) => fs.readFileSync(
    path.join(__dirname, '..', '..', 'static', 'js', name), 'utf8');

const utcDate = (offsetDays) => {
    const d = new Date(Date.now() + offsetDays * 86400000);
    return d.getUTCFullYear() + '-' + String(d.getUTCMonth() + 1).padStart(2, '0')
        + '-' + String(d.getUTCDate()).padStart(2, '0');
};

const yesterday = utcDate(-1);
const today = utcDate(0);

// 实际消费特意取与 API 等值不同的值,任何误用 cost 的写法都会被抓到。
const rows = [
    {
        date: yesterday, input_tokens: 100, output_tokens: 50,
        cache_hit_tokens: 20, cache_miss_tokens: 80, requests: 3,
        metered_cost: 9.99, equivalent_cost: 12.34,
    },
    {
        date: today, input_tokens: 200, output_tokens: 60,
        cache_hit_tokens: 0, cache_miss_tokens: 200, requests: 4,
        metered_cost: 5.0, equivalent_cost: 6.78,
    },
].map((row) => Object.assign({}, row, { cost: row.metered_cost }));

let option = null;
const chart = {
    setOption: (next) => { option = next; },
    dispose: () => {},
    resize: () => {},
};

const nodes = { chartBillingDaily: { dataset: {}, style: {} } };

const context = {
    document: { getElementById: (id) => nodes[id] || null },
    proxyApi: async () => rows,
    echarts: { init: () => chart },
    ResizeObserver: class { observe() {} },
    fmtNum: (value) => String(value),
    console,
};
vm.createContext(context);
vm.runInContext(staticJs('proxy_billing.js'), context,
                { filename: 'proxy_billing.js' });

(async () => {
    await context.loadDailyBillingChart();
    assert.ok(option, '图表必须被渲染');

    const dates = option.xAxis.data;
    assert.strictEqual(dates.length, 30, '折线图窗口是 30 天滚动窗口');

    const legend = option.legend.data;
    assert.ok(legend.includes('API 等值消费'), '图例应为 API 等值消费');
    assert.ok(!legend.includes('消费'), '实际消费的图例已移除');

    const line = option.series.filter((s) => s.type === 'line');
    assert.strictEqual(line.length, 1, '只有一条消费折线');
    assert.strictEqual(line[0].name, 'API 等值消费');
    assert.strictEqual(line[0].yAxisIndex, 1, '折线仍挂在右轴');

    const pick = (date) => line[0].data[dates.indexOf(date)];
    assert.strictEqual(pick(yesterday), 12.34, '折线取 equivalent_cost');
    assert.strictEqual(pick(today), 6.78, '折线取 equivalent_cost');
    assert.ok(!line[0].data.includes(9.99), '折线不混入实际消费');
    assert.ok(!line[0].data.includes(5.0), '折线不混入实际消费');
    assert.strictEqual(
        line[0].data.filter((v) => v === 0).length, 28, '空缺的天按 0 补齐');

    const tooltip = option.tooltip.formatter([
        { dataIndex: dates.indexOf(today) }, { dataIndex: dates.indexOf(today) },
    ]);
    assert.ok(tooltip.includes('API 等值消费'), 'tooltip 应标 API 等值消费');
    assert.ok(tooltip.includes('¥6.78'), 'tooltip 展示 API 等值金额');
    assert.ok(!tooltip.includes('¥5.00'), 'tooltip 不再展示实际消费');

    console.log('proxy billing daily chart ok');
})().catch((err) => {
    console.error(err);
    process.exit(1);
});
