'use strict';

// 单上游多密钥的展示顺序：悬浮卡 / 账户表 / 编辑弹窗按密钥名称的字典序
// 排列，无名密钥以打码值为名。排序只发生在渲染层——编辑弹窗提交的
// keep_key_ids 按存储下标还原，不把存储顺序改写成名称顺序。

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const staticJs = (name) => fs.readFileSync(
    path.join(__dirname, '..', '..', 'static', 'js', name), 'utf8');

const context = { Date, isNaN, Math, String, Number };
vm.createContext(context);
// utils.js 先加载（页面里也是这个顺序），proxy_manager.js 依赖它的 esc/fmtLocal。
vm.runInContext(staticJs('utils.js'), context, { filename: 'utils.js' });
vm.runInContext(staticJs('proxy_manager.js'), context, { filename: 'proxy_manager.js' });

const { keyDisplayName, sortKeysByName, usageCardHtml, collectKeyRows } = context;

// ── 展示名与排序键 ──────────────────────────────────────────────────────

assert.strictEqual(keyDisplayName({ label: '主力', masked: 'sk-***1' }), '主力');
assert.strictEqual(keyDisplayName({ label: '', masked: 'sk-***1' }), 'sk-***1');
assert.strictEqual(keyDisplayName({ masked: 'sk-***1' }), 'sk-***1');
assert.strictEqual(keyDisplayName({}), '');
assert.strictEqual(keyDisplayName(null), '');

// 字典序（UTF-16 码元序）：数字 < 大写 < 小写，与浏览器区域设置无关。
const keys = [
    { label: 'b', masked: 'm3' },
    { label: 'B2', masked: 'm1' },
    { label: 'a10', masked: 'm4' },
    { label: 'a2', masked: 'm2' },
];
assert.deepStrictEqual([...sortKeysByName(keys).map(keyDisplayName)],
                       ['B2', 'a10', 'a2', 'b']);

// 无名密钥以打码值参与同一字典序，与具名密钥混排。
const mixed = [
    { label: 'z-last', masked: 'sk-z' },
    { masked: 'sk-a' },
    { label: '', masked: 'sk-b' },
];
assert.deepStrictEqual([...sortKeysByName(mixed).map(keyDisplayName)],
                       ['sk-a', 'sk-b', 'z-last']);

// 同名保持原有相对顺序；入参数组不被修改，返回新数组。
const stable = [
    { label: 'same', masked: 'm-first' },
    { label: 'same', masked: 'm-second' },
];
const sorted = sortKeysByName(stable);
assert.deepStrictEqual([...sorted.map((k) => k.masked)], ['m-first', 'm-second']);
assert.notStrictEqual(sorted, stable);
assert.deepStrictEqual([...stable.map((k) => k.masked)], ['m-first', 'm-second']);
assert.deepStrictEqual([...sortKeysByName(null)], []);
assert.deepStrictEqual([...sortKeysByName(undefined)], []);

// ── 悬浮卡：逐把密钥按名称排序，无名密钥展示打码值 ───────────────────────

const card = usageCardHtml({
    name: 'OpenCode Go',
    account_type: 'plan',
    keys: [
        { label: 'key-b', masked: 'sk-***2' },
        { masked: 'sk-***3' },
        { label: 'key-a', masked: 'sk-***1' },
    ],
});
assert.ok(card.indexOf('key-a') < card.indexOf('key-b'), card);
assert.ok(card.indexOf('key-b') < card.indexOf('sk-***3'), card);
assert.ok(card.includes('<code>sk-***3</code>'), card);

// ── 账户表：打码值、×N 提示与名称清单同按名称排序（云端密钥合并参与）────

const tbody = { innerHTML: '', querySelectorAll: () => [] };
context.document = {
    querySelector: (sel) => (sel === '#accountsTable tbody' ? tbody : null),
};
vm.runInContext(`_accountsCache = ${JSON.stringify([{
    id: 1,
    name: 'Test Upstream',
    account_type: 'api',
    base_url: 'https://example.com',
    api_format: 'openai',
    key_count: 3,
    max_concurrency: null,
    keys: [
        { id: 11, label: 'key-b', masked: 'sk-***2' },
        { id: 12, label: 'key-a', masked: 'sk-***1' },
    ],
    cloud_keys: [{ masked: 'am-***0' }],
}])}`, context);
context.renderAccountsTable();
const table = tbody.innerHTML;
assert.ok(table.includes('title="3 把密钥：key-a、key-b"'), table);
// 展示的打码值取名称序第一位：云端密钥无名，按打码值 'am-***0' 排在最前
// （'am-' < 'key-' < 'sk-'），因此合并排序后它才是行上展示的那把。
assert.ok(table.includes('<code>am-***0</code>'), table);

// ── 编辑弹窗：行按名称展示，提交的 keep_key_ids 回到存储下标顺序 ─────────

const rowStub = (keepId, storedIndex) => ({
    dataset: { keepId: String(keepId), storedIndex: String(storedIndex) },
    querySelector: () => ({ value: '' }),
});
// DOM 顺序 = 名称序（下标 1 的行在前），存储下标与之相反。
const keepRows = [rowStub(2, 1), rowStub(1, 0)];
const formStub = {
    account_type: { value: 'api' },
    querySelectorAll: (sel) => (sel.includes('existing-key') ? keepRows : []),
};
assert.deepStrictEqual([...collectKeyRows(formStub).keep_key_ids], ['1', '2']);

console.log('proxy manager key order ok');
