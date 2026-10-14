'use strict';

// 悬浮卡的窗口展示与调度对齐：重置倒计时只在窗口仍在计时、重置时刻是真实
// 截止时显示。判据镜像 router.cpp 读取 usage_json 的规则与
// candidate_selection.h 的窗口压力——5h 认 state=started（idle 是上游占位
// 值：观测时刻+5h，不是截止，显示「未开始」）；周/月窗口非 unknown 即计时
// 中；缺 state 字段的旧快照按解析规则补足（周/月有可解析重置时刻视为
// started，滚动窗口不猜）；无法判定时不显示倒计时，只给百分比。

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const NOW = Date.UTC(2026, 9, 14, 6, 50, 8);   // 2026-10-14T06:50:08Z

// 冻结「现在」，倒计时断言才可复现（fmtResetIn 用 Date.parse 与 Date.now）。
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

const { usageWindowText, usageCardHtml } = context;

const HOUR = 3600000;
const FIVE_HOURS = 5 * HOUR;

// ── 计时中的窗口：显示重置倒计时 ────────────────────────────────────────

assert.strictEqual(
    usageWindowText({ percent: 40, resets_at: iso(NOW + HOUR), state: 'started' }, '5h'),
    '40%(1h)');
assert.strictEqual(
    usageWindowText({ percent: 40, resets_at: iso(NOW + 95 * 60000), state: 'started' }, '5h'),
    '40%(1h35m)');
assert.strictEqual(
    usageWindowText({
        percent: 20, resets_at: iso(NOW + 3 * 24 * HOUR + 2 * HOUR), state: 'started',
    }, 'week'),
    '20%(3d2h)');
// 重置时刻已过、快照尚未刷新：0m（该窗口已重置，下一周期由下一次观测给出）。
assert.strictEqual(
    usageWindowText({ percent: 100, resets_at: iso(NOW - 60000), state: 'started' }, '5h'),
    '100%(0m)');

// ── 未开始计时的滚动窗口：占位重置时刻不冒充截止 ────────────────────────

assert.strictEqual(
    usageWindowText({ percent: 0, resets_at: iso(NOW + FIVE_HOURS), state: 'idle' }, '5h'),
    '0%(未开始)');
// 占位值距观测越远显示越小（4h56m 之类）也不显示——idle 一律「未开始」。
assert.strictEqual(
    usageWindowText({ percent: 0, resets_at: iso(NOW + 16 * 60000), state: 'idle' }, '5h'),
    '0%(未开始)');

// ── 无法判定：不猜，只给百分比 ──────────────────────────────────────────

assert.strictEqual(
    usageWindowText({ percent: 0, resets_at: iso(NOW + 6 * HOUR), state: 'unknown' }, '5h'),
    '0%');
assert.strictEqual(
    usageWindowText({ percent: 30, resets_at: null, state: 'unknown' }, 'week'),
    '30%');
assert.strictEqual(
    usageWindowText({ percent: null, resets_at: null, state: 'idle' }, '5h'),
    '—');

// ── 旧快照（无 state 字段）：按 router.cpp 的规则补足 ───────────────────

// 周/月窗口有可解析的重置时刻即视为计时中；
assert.strictEqual(
    usageWindowText({ percent: 20, resets_at: iso(NOW + 2 * HOUR) }, 'week'),
    '20%(2h)');
// 滚动窗口保持 unknown，不显示占位倒计时。
assert.strictEqual(
    usageWindowText({ percent: 0, resets_at: iso(NOW + FIVE_HOURS) }, '5h'),
    '0%');
// 非字符串 state / 非字符串 resets_at 与缺字段同样处理。
assert.strictEqual(
    usageWindowText({ percent: 20, resets_at: 12345, state: 7 }, 'week'),
    '20%');

// ── 卡级：同一把密钥的 idle 5h 与计时中的周/月窗口并存 ──────────────────

const card = usageCardHtml({
    name: 'OpenCode Go',
    account_type: 'plan',
    keys: [{
        label: 'key-a',
        masked: 'sk-***1',
        usage: {
            checked_at: iso(NOW),
            windows: {
                '5h': { percent: 0, resets_at: iso(NOW + FIVE_HOURS), state: 'idle' },
                week: { percent: 20, resets_at: iso(NOW + 2 * HOUR), state: 'started' },
                month: { percent: 79, resets_at: iso(NOW + 5 * 24 * HOUR), state: 'started' },
            },
        },
    }],
});
assert.ok(card.includes('0%(未开始)'), card);
assert.ok(card.includes('20%(2h)'), card);
assert.ok(card.includes('79%(5d)'), card);

console.log('proxy manager usage card ok');
