'use strict';

// 用量仪表盘 3D 日历柱状图的悬浮窗:每个 Token 用量后面必须紧跟
// API 等值消费的金额,金额直接写在括号里,不额外加标签。
// 数据里的 cost 是另一条口径,误用它会让括号里的数字变成实际消费。

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const staticJs = (name) => fs.readFileSync(
    path.join(__dirname, '..', '..', 'static', 'js', name), 'utf8');

const context = { console };
vm.createContext(context);
for (const name of ['utils.js', 'api.js', 'charts.js']) {
    vm.runInContext(staticJs(name), context, { filename: name });
}

const entries = [
    { name: 'model-a', color: '#B45F45' },
    { name: 'Other', models: ['model-b'], color: '#716B65' },
];

// cost 特意取与 api_equivalent_cost 不同的值,任何误用 cost 的写法都会被抓到。
const day = {
    date: '2026-08-09',
    total_tokens: 1500,
    requests: 2,
    cost: 99.0,
    api_equivalent_cost: 7.0,
    by_model: {
        'model-a': { total_tokens: 1000, requests: 1, cost: 50.0, api_equivalent_cost: 5.25 },
        'model-b': { total_tokens: 500, requests: 1, cost: 49.0, api_equivalent_cost: 1.75 },
    },
};

const tooltip = context._calendarTooltip(day, entries);

assert.ok(
    tooltip.includes('总 Token: <b>1,500</b> (¥7.00)'),
    '总用量后面应紧跟 API 等值消费:' + tooltip);
assert.ok(
    tooltip.includes('model-a: <b>1,000</b> (¥5.25)'),
    '按模型的用量后面应紧跟 API 等值消费:' + tooltip);
assert.ok(
    tooltip.includes('model-b: <b>500</b> (¥1.75)'),
    '被折叠进 Other 的模型同样带金额:' + tooltip);
assert.ok(
    tooltip.includes('请求数: <b>2</b><br/>'),
    '请求数不是 Token 用量,不追加金额:' + tooltip);
assert.ok(!tooltip.includes('¥99.00'), '金额取 api_equivalent_cost,不取 cost');
assert.ok(!tooltip.includes('¥50.00') && !tooltip.includes('¥49.00'),
          '按模型金额同样取 api_equivalent_cost');
assert.ok(!tooltip.includes('API 等值'), '括号里只放金额,不加标签');

// 没有任何消费的天按 0 显示,不能出现 undefined/NaN。
const emptyDay = {
    date: '2026-08-10',
    total_tokens: 0,
    requests: 0,
    by_model: {},
};
const emptyTooltip = context._calendarTooltip(emptyDay, entries);
assert.ok(
    emptyTooltip.includes('总 Token: <b>0</b> (¥0.00)'),
    '缺消费数据时显示 ¥0.00:' + emptyTooltip);

console.log('dashboard calendar tooltip ok');
