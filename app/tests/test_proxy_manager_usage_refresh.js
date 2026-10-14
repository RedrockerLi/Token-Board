'use strict';

// 点击账户名立即刷新用量：proxyApi 桩替换 HTTP 往返，验证
//   1. 卡片状态行：pending / 超时提示 / 失败提示（文本转义），旧数据保留；
//   2. 点击 → POST /api/proxy/accounts/<id>/usage-refresh，卡片就地进入刷新态；
//   3. 完成后整行合并进 _accountsCache 并就地重渲染；鼠标移开再悬浮看到新
//      数据（监听器在事件时从缓存解析账户，不捕获绑定时对象）；
//   4. 同一账户刷新中重复点击去重；新一次尝试清掉上一次的提示。

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const NOW = Date.UTC(2026, 9, 14, 6, 50, 8);   // 2026-10-14T06:50:08Z
const HOUR = 3600000;

// 冻结「现在」：倒计时与观测时刻断言才可复现。
class FixedDate extends Date {
    constructor(...args) {
        if (args.length === 0) super(NOW);
        else super(...args);
    }
    static now() { return NOW; }
}

const iso = (ms) => new Date(ms).toISOString();

const staticJs = (name) => fs.readFileSync(
    path.join(__dirname, '..', '..', 'static', 'js', name), 'utf8');

const context = { Date: FixedDate, isNaN, Math, String, Number };
vm.createContext(context);
// utils.js 先加载（页面里也是这个顺序），proxy_manager.js 依赖它的 esc/fmtLocal。
vm.runInContext(staticJs('utils.js'), context, { filename: 'utils.js' });
vm.runInContext(staticJs('proxy_manager.js'), context, { filename: 'proxy_manager.js' });

const { usageCardHtml } = context;

const usageSnapshot = (percent, resetsAt) => ({
    checked_at: iso(NOW),
    error: null,
    subscription: 'active',
    windows: {
        '5h': { percent, resets_at: resetsAt, state: 'started' },
        week: { percent: 20, resets_at: iso(NOW + 48 * HOUR), state: 'started' },
        month: { percent: 30, resets_at: iso(NOW + 240 * HOUR), state: 'started' },
    },
});

const accountRow = (usage) => ({
    id: 1,
    name: 'OpenCode Go',
    account_type: 'plan',
    subtype: 'opencode-go',
    base_url: 'https://opencode.ai/zen/go',
    api_format: 'anthropic',
    max_concurrency: null,
    key_count: 1,
    ends_at: null,
    keys: [{ id: 11, label: 'key-a', masked: 'sk-***1', usage }],
    cloud_keys: [],
});

const OLD_USAGE = usageSnapshot(40, iso(NOW + 1 * HOUR));
const FRESH_USAGE = usageSnapshot(60, iso(NOW + 2 * HOUR));
const ACCOUNT = accountRow(OLD_USAGE);

// ── 纯渲染：状态行、旧数据保留与转义 ────────────────────────────────────

const pendingCard = usageCardHtml(ACCOUNT, { pending: true });
assert.ok(pendingCard.includes('正在获取最新用量…'), pendingCard);
assert.ok(pendingCard.includes('40%(1h)'), 'pending keeps the old data');
assert.ok(pendingCard.includes('观测于'), pendingCard);
assert.ok(!usageCardHtml(ACCOUNT).includes('正在获取最新用量'),
          'the plain one-arg rendering carries no status line');

const slowCard = usageCardHtml(ACCOUNT,
                               { notice: { text: '刷新较慢，稍后重新悬浮查看' } });
assert.ok(slowCard.includes('class="usage-card__status">刷新较慢，稍后重新悬浮查看'),
          slowCard);
const failedCard = usageCardHtml(ACCOUNT,
                                 { notice: { level: 'error', text: '获取失败：<b>x</b>' } });
assert.ok(failedCard.includes('class="usage-card__error">获取失败：&lt;b&gt;x&lt;/b&gt;'),
          failedCard);

// ── DOM 桩：tbody、锚点与卡片节点 ───────────────────────────────────────

const handlers = {};
const anchor = {
    dataset: { accountId: '1' },
    addEventListener(type, fn) { handlers[type] = fn; },
    getBoundingClientRect: () => ({ left: 10, top: 10, bottom: 30 }),
};
const tbody = { innerHTML: '', querySelectorAll: () => [anchor] };
const cardNode = {
    className: '', innerHTML: '', style: {}, attrs: {}, classes: new Set(),
    classList: {
        add: (name) => cardNode.classes.add(name),
        remove: (name) => cardNode.classes.delete(name),
        contains: (name) => cardNode.classes.has(name),
    },
    setAttribute: (key, value) => { cardNode.attrs[key] = value; },
    getBoundingClientRect: () => ({ width: 300, height: 200 }),
};
context.document = {
    querySelector: (sel) => (sel === '#accountsTable tbody' ? tbody : null),
    createElement: () => cardNode,
    body: { appendChild: () => {} },
};
context.window = { innerWidth: 1200, innerHeight: 800 };

// ── proxyApi 桩：每次调用返回一个可手动结算的延迟 promise ───────────────

const apiCalls = [];
let apiDeferred = null;
context.proxyApi = (url, options) => {
    apiCalls.push({ url, options });
    let resolve, reject;
    const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
    apiDeferred = { resolve, reject };
    return promise;
};

vm.runInContext(`TYPE_SPECS = ${JSON.stringify({
    api: { short_label: 'API', subtypes: {} },
    plan: {
        short_label: 'Plan',
        subtypes: { 'opencode-go': { usage_path: '/v1/usage', label: 'OpenCode Go' } },
    },
})}`, context);
vm.runInContext("_accountsTabType = 'plan'", context);
vm.runInContext(`_accountsCache = ${JSON.stringify([ACCOUNT])}`, context);

context.renderAccountsTable();
assert.ok(handlers.mouseenter && handlers.mouseleave && handlers.click,
          'the anchor binds hover and click listeners');

async function main() {
    // ── 悬浮 → 点击 → 完成 → 移开 → 再悬浮 ─────────────────────────────

    handlers.mouseenter();
    assert.ok(cardNode.classList.contains('is-open'), 'hover opens the card');
    assert.ok(cardNode.innerHTML.includes('40%(1h)'), cardNode.innerHTML);

    const first = handlers.click();
    assert.strictEqual(apiCalls.length, 1);
    assert.strictEqual(apiCalls[0].url, '/api/proxy/accounts/1/usage-refresh');
    // 选项对象产生于 vm realm，跨 realm 比较只看字段。
    assert.strictEqual(apiCalls[0].options.method, 'POST');
    assert.ok(cardNode.classList.contains('is-open'), 'click keeps the card open');
    assert.ok(cardNode.innerHTML.includes('正在获取最新用量…'), cardNode.innerHTML);
    assert.ok(cardNode.innerHTML.includes('40%(1h)'), 'old data stays while pending');

    // 刷新中重复点击同一账户：不再发请求（延迟 promise 不被覆盖）。
    handlers.click();
    assert.strictEqual(apiCalls.length, 1);

    apiDeferred.resolve({ status: 'ok', account: accountRow(FRESH_USAGE) });
    await first;
    assert.ok(cardNode.innerHTML.includes('60%(2h)'), cardNode.innerHTML);
    assert.ok(!cardNode.innerHTML.includes('正在获取最新用量'),
              'the pending line clears on completion');
    assert.strictEqual(
        vm.runInContext('_accountsCache[0].keys[0].usage.windows["5h"].percent',
                        context),
        60, 'the fresh row is merged into the cache');

    handlers.mouseleave();
    assert.ok(!cardNode.classList.contains('is-open'), 'mouseleave closes the card');
    handlers.mouseenter();
    assert.ok(cardNode.innerHTML.includes('60%(2h)'),
              're-hover shows the refreshed data without a table re-render');

    // ── 服务端超时（202 pending）：提示稍后查看，旧数据保留 ─────────────

    const slow = handlers.click();
    assert.strictEqual(apiCalls.length, 2);
    assert.ok(cardNode.innerHTML.includes('正在获取最新用量…'), cardNode.innerHTML);
    apiDeferred.resolve({ status: 'pending' });
    await slow;
    assert.ok(cardNode.innerHTML.includes('刷新较慢，稍后重新悬浮查看'),
              cardNode.innerHTML);
    assert.ok(cardNode.innerHTML.includes('60%(2h)'), 'old data stays on pending');

    // ── 失败：红字提示，旧数据保留 ──────────────────────────────────────

    const failed = handlers.click();
    assert.strictEqual(apiCalls.length, 3);
    apiDeferred.reject(new Error('boom'));
    await failed;
    assert.ok(cardNode.innerHTML.includes('获取失败：boom'), cardNode.innerHTML);
    assert.ok(cardNode.innerHTML.includes('60%(2h)'), 'old data stays on failure');

    // ── 再次点击：清掉上一次的提示，成功后无残留 ────────────────────────

    const retry = handlers.click();
    assert.ok(cardNode.innerHTML.includes('正在获取最新用量…'),
              'a new attempt replaces the stale notice');
    assert.ok(!cardNode.innerHTML.includes('获取失败：boom'));
    apiDeferred.resolve({ status: 'ok', account: accountRow(FRESH_USAGE) });
    await retry;
    assert.ok(!cardNode.innerHTML.includes('获取失败：boom'),
              'the notice does not outlive the successful retry');

    console.log('proxy manager usage refresh ok');
}

main().catch((err) => {
    console.error(err);
    process.exit(1);
});
