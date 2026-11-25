'use strict';

// 消费报告页统计卡片的「API 等值」副标题：口径是近 30 天滚动窗口，取
// /api/proxy/stats 的 api_equivalent_cost（与卡片主值 total_cost 的 30 天窗口一致），
// 不再是只有今天的 today_cost。副标题不再夹带当日的活跃上游计数。

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const staticJs = (name) => fs.readFileSync(
    path.join(__dirname, '..', '..', 'static', 'js', name), 'utf8');

const nodes = {};
const node = (id) => (nodes[id] ||
    (nodes[id] = { textContent: '', innerHTML: '', dataset: {} }));
// 真实页面里这些值节点来自 initBillingPage() 写入的 innerHTML；
// 假 DOM 不解析 HTML，按 id 预先注册。
['page-billing', 'billTotalRequests', 'billTodayRequests', 'billTotalCost',
 'billApiEquivalentCost', 'billTotalTokens'].forEach(node);

const stats = {
    total_requests: 40991,
    today_requests: 809,
    total_cost: 442.93,
    api_equivalent_cost: 1006.93,
    today_cost: 21.87,
    today_actual_cost: 0,
    total_tokens: 5007565984,
    active_upstreams: 1,
};

const context = {
    document: {
        getElementById: (id) => nodes[id] || null,
        // 当日活跃上游表格按需加载，这里不需要它的行。
        querySelector: () => null,
    },
    proxyApi: async () => stats,
    fmtNum: (value) => String(value),
    console,
};
vm.createContext(context);
vm.runInContext(staticJs('proxy_billing.js'), context,
                { filename: 'proxy_billing.js' });

(async () => {
    context.initBillingPage();
    // loadBillingStats() 是 fire-and-forget 的异步加载，等一轮宏任务再断言。
    await new Promise((resolve) => setImmediate(resolve));

    const markup = nodes['page-billing'].innerHTML;
    assert.ok(markup.includes('近30天消费（API 等值）'), '副标题应为近30天 API 等值口径');
    assert.ok(!markup.includes('理论'), '旧口径名称已全部替换');
    assert.ok(!markup.includes('个活跃上游'), '副标题不再展示活跃上游计数');
    assert.ok(!markup.includes('billTodayCost'), '旧卡片字段已移除');

    // 主值仍是近 30 天实际消费，副标题是同一窗口的 API 等值消费。
    assert.strictEqual(nodes.billTotalCost.textContent, '¥442.93');
    assert.strictEqual(nodes.billApiEquivalentCost.textContent, '¥1006.93');

    console.log('proxy billing stats card ok');
})().catch((err) => {
    console.error(err);
    process.exit(1);
});
