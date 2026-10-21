'use strict';

// 导出按钮的反馈:POST /api/proxy/export 永远返回 200,导出结果只写在响应体
// 的 status 里,所以 exportData() 必须读响应体,失败时提示原因且不刷新页面。

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const staticJs = (name) => fs.readFileSync(
    path.join(__dirname, '..', '..', 'static', 'js', name), 'utf8');

const button = { disabled: false, textContent: '导出数据' };
const toasts = [];
let response = null;
let refreshCalls = 0;

const context = {
    document: { getElementById: (id) => (id === 'btnExport' ? button : null) },
    proxyApi: async () => response,
    showToast: (message, type) => toasts.push({ message, type }),
    fetchRefresh: async () => { refreshCalls += 1; },
    console,
};
vm.createContext(context);
vm.runInContext(staticJs('proxy_billing.js'), context,
                { filename: 'proxy_billing.js' });

(async () => {
    // 成功:提示成功、刷新数据、按钮复位。
    response = { status: 'ok' };
    await context.exportData();
    assert.deepStrictEqual(toasts, [{ message: '导出成功', type: undefined }]);
    assert.strictEqual(refreshCalls, 1);
    assert.strictEqual(button.textContent, '导出数据');
    assert.strictEqual(button.disabled, false);

    // 失败:HTTP 200 + status=error,必须报错、带上后端原因、且不刷新。
    toasts.length = 0;
    response = {
        status: 'error',
        message: '同步失败: IntegrityError: CHECK constraint failed',
    };
    await context.exportData();
    assert.strictEqual(toasts.length, 1);
    assert.strictEqual(
        toasts[0].message,
        '导出失败: 同步失败: IntegrityError: CHECK constraint failed');
    assert.strictEqual(toasts[0].type, 'error');
    assert.strictEqual(refreshCalls, 1);
    assert.strictEqual(button.disabled, false);

    // 空响应体同样按失败处理。
    toasts.length = 0;
    response = null;
    await context.exportData();
    assert.strictEqual(toasts[0].message, '导出失败: 导出未完成');
    assert.strictEqual(toasts[0].type, 'error');
    assert.strictEqual(refreshCalls, 1);

    console.log('proxy billing export feedback ok');
})().catch((err) => {
    console.error(err);
    process.exit(1);
});
