'use strict';

// 账户列表的操作列与复制流程：
//   1. 行内只保留 编辑 / 更新 / 复制——测试并发与取消删除都收进编辑弹窗；
//   2. 编辑弹窗保留 更新模型 / 测试并发 / 取消删除（各一处）；
//   3. 更新模型按按钮还原各自文案（行=更新，弹窗=更新模型）；
//   4. 复制：确认后 POST duplicate，刷新列表并打开新账户的编辑弹窗；
//      没有本机明文密钥时 toast 提示手填。

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

// ── DOM 桩：tbody、页面容器与按钮列表 ───────────────────────────────────

const tbody = { innerHTML: '', querySelectorAll: () => [] };
const pageEl = { dataset: {}, innerHTML: '' };
let buttons = [];
context.document = {
    querySelector: (sel) => (sel === '#accountsTable tbody' ? tbody : null),
    querySelectorAll: () => buttons,
    getElementById: (id) => (id === 'page-proxy-accounts' ? pageEl : null),
};
context.window = { addEventListener: () => {} };
context.showToast = () => {};
context.ConfigSync = { markDirty: () => { context.__dirty = true; } };
context.proxyApi = async () => [];

vm.runInContext(`TYPE_SPECS = ${JSON.stringify({
    api: { short_label: 'API', billing: 'usage', subtypes: {} },
})}`, context);
vm.runInContext("_accountsTabType = 'api'", context);

const apiAccount = {
    id: 1,
    name: 'OpenAI',
    base_url: 'http://upstream.test/v1',
    api_format: 'openai',
    account_type: 'api',
    subtype: null,
    currency: 'CNY',
    monthly_price: 0,
    max_concurrency: 3,
    key_count: 1,
    ends_at: null,
    keys: [{ id: 11, label: 'key-a', masked: 'sk-***1' }],
    cloud_keys: [],
};

// ── 行内操作：只有编辑 / 更新（复制、测试并发与取消删除都在编辑弹窗） ────

vm.runInContext(`_accountsCache = ${JSON.stringify([apiAccount])}`, context);
context.renderAccountsTable();
assert.ok(tbody.innerHTML.includes('<td class="td-actions">'), tbody.innerHTML);
assert.ok(tbody.innerHTML.includes('>编辑</button>'), tbody.innerHTML);
assert.ok(tbody.innerHTML.includes('>更新</button>'), tbody.innerHTML);
assert.strictEqual((tbody.innerHTML.match(/<button/g) || []).length, 2,
                   tbody.innerHTML);
assert.ok(!tbody.innerHTML.includes('复制'), tbody.innerHTML);
assert.ok(!tbody.innerHTML.includes('duplicateAccount'), tbody.innerHTML);
assert.ok(!tbody.innerHTML.includes('测试并发'), tbody.innerHTML);
assert.ok(!tbody.innerHTML.includes('取消删除'), tbody.innerHTML);

// 已安排到期删除的账户：行内只留「到期」徽章（提示去编辑弹窗取消）。
vm.runInContext(`_accountsCache = ${JSON.stringify([
    { ...apiAccount, ends_at: '2099-01-01T00:00:00Z' },
])}`, context);
context.renderAccountsTable();
assert.ok(tbody.innerHTML.includes('到期'), tbody.innerHTML);
assert.ok(tbody.innerHTML.includes('在编辑弹窗中可取消删除'), tbody.innerHTML);
assert.ok(!tbody.innerHTML.includes('>取消删除</button>'), tbody.innerHTML);

// ── 编辑弹窗：保留 复制 / 更新模型 / 测试并发 / 取消删除 ────────────────

context.initAccountsPage();
const pageHtml = pageEl.innerHTML;
const occurrences = (needle) => pageHtml.split(needle).length - 1;
assert.strictEqual(occurrences('测试并发'), 1, pageHtml);
assert.strictEqual(occurrences('取消删除'), 1, pageHtml);
assert.strictEqual(occurrences('更新模型'), 1, pageHtml);
assert.strictEqual(occurrences('>复制</button>'), 1, pageHtml);
assert.ok(pageHtml.includes('id="accountTestConcBtn"'), pageHtml);
assert.ok(pageHtml.includes('id="accountCancelDeleteBtn"'), pageHtml);
assert.ok(pageHtml.includes('id="accountModelBtn"'), pageHtml);
assert.ok(pageHtml.includes('id="accountDuplicateBtn"'), pageHtml);
assert.ok(pageHtml.includes('恢复账户及尚未到期的上游密钥'), pageHtml);

async function main() {
    // initAccountsPage 触发的列表加载还在飞：先让它落地，避免它复用下面的
    // proxyApi 桩（会把 /models 调用的记录覆盖成 /accounts）。
    await new Promise((resolve) => setImmediate(resolve));

    // ── 更新模型：行与弹窗各自还原文案 ─────────────────────────────────

    const rowBtn = { textContent: '更新', disabled: false };
    const modalBtn = { textContent: '更新模型', disabled: false };
    buttons = [rowBtn, modalBtn];
    let modelsCall = null;
    let duringCall = null;
    context.proxyApi = async (url, options) => {
        if (!url.endsWith('/models')) return [];
        modelsCall = { url, options };
        duringCall = {
            disabled: [rowBtn.disabled, modalBtn.disabled],
            labels: [rowBtn.textContent, modalBtn.textContent],
        };
        return { count: 7 };
    };
    await context.updateAccountModels(1, 'OpenAI');
    assert.strictEqual(modelsCall.url, '/api/proxy/accounts/1/models');
    assert.strictEqual(modelsCall.options.method, 'POST');
    assert.deepStrictEqual(duringCall.disabled, [true, true]);
    assert.deepStrictEqual(duringCall.labels, ['获取中...', '获取中...']);
    assert.deepStrictEqual([rowBtn.textContent, modalBtn.textContent],
                           ['更新', '更新模型']);
    assert.deepStrictEqual([rowBtn.disabled, modalBtn.disabled], [false, false]);

    // ── 复制：取消确认不发请求 ──────────────────────────────────────────

    let duplicateCall = null;
    let reloaded = 0;
    let editedId = null;
    let toastText = null;
    let confirmText = '';
    context.loadAccountsTable = async () => { reloaded += 1; };
    context.editAccount = async (id) => { editedId = id; };
    context.showToast = (msg) => { toastText = msg; };
    context.proxyApi = async (url, options) => {
        duplicateCall = { url, options };
        return { id: 7, name: 'OpenAI 副本', keys_copied: 2 };
    };
    context.confirm = (text) => { confirmText = text; return false; };
    await context.duplicateAccount(1, 'OpenAI', { disabled: false, textContent: '复制' });
    assert.strictEqual(duplicateCall, null, 'cancelling sends no request');

    // ── 复制：创建、刷新列表、打开新账户的编辑弹窗 ──────────────────────

    context.confirm = (text) => { confirmText = text; return true; };
    const copyBtn = { disabled: false, textContent: '复制' };
    await context.duplicateAccount(1, 'OpenAI', copyBtn);
    assert.strictEqual(duplicateCall.url, '/api/proxy/accounts/1/duplicate');
    assert.strictEqual(duplicateCall.options.method, 'POST');
    assert.strictEqual(reloaded, 1);
    assert.strictEqual(editedId, 7);
    assert.ok(confirmText.includes('OpenAI'), confirmText);
    assert.ok(String(toastText).includes('OpenAI 副本'), String(toastText));
    assert.ok(context.__dirty, 'config sync marked dirty');
    // 弹窗是常驻节点：复制完成后按钮必须恢复，不能停在「复制中...」。
    assert.deepStrictEqual([copyBtn.disabled, copyBtn.textContent],
                           [false, '复制']);

    // ── 复制：没有本机明文密钥时提示手填 ────────────────────────────────

    context.proxyApi = async () => ({ id: 8, name: '无密钥 副本', keys_copied: 0 });
    await context.duplicateAccount(2, '无密钥', { disabled: false, textContent: '复制' });
    assert.ok(String(toastText).includes('没有本机明文密钥'), String(toastText));

    // ── 测试并发：读弹窗里的输入值，无需先保存 ──────────────────────────

    const concInput = { value: '4' };
    const concBtn = { disabled: false, textContent: '测试并发' };
    context.document.querySelector = (sel) => (
        sel === '#accountsTable tbody' ? tbody
            : (sel === '#accountForm [name="max_concurrency"]' ? concInput : null));
    context.document.getElementById = (id) => (
        id === 'page-proxy-accounts' ? pageEl
            : (id === 'accountTestConcBtn' ? concBtn : null));
    let concCall = null;
    context.proxyApi = async (url, options) => {
        concCall = { url, options };
        return { concurrency: 4, succeeded: 4, message: '并发 4 安全' };
    };
    await context.testConcurrency(1);
    assert.strictEqual(concCall.url, '/api/proxy/accounts/1/test-concurrency');
    assert.strictEqual(JSON.parse(concCall.options.body).concurrency, 4);
    assert.deepStrictEqual([concBtn.disabled, concBtn.textContent],
                           [false, '测试并发']);

    // 没有有效输入时不发请求。
    concInput.value = '';
    concCall = null;
    await context.testConcurrency(1);
    assert.strictEqual(concCall, null);

    console.log('proxy manager accounts actions ok');
}

main().catch((err) => {
    console.error(err);
    process.exit(1);
});
