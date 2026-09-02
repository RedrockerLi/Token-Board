/**
 * proxy_settings.js — Settings page: per-format proxy timeouts + WebDAV sync.
 *
 * Exports: initSettingsPage()
 * Lazy-loaded by app.js when navigating to #/settings/general.
 */

// ── Timeout config ────────────────────────────────────────────────────────

/** Client wire formats (proxy_timeout_config rows) and their display labels. */
const TIMEOUT_GROUPS = [
    { key: 'anthropic',        label: 'Anthropic 客户端', hint: 'Claude Code · /v1/messages' },
    { key: 'openai_responses', label: 'OpenAI Responses', hint: 'codex · /v1/responses' },
    { key: 'openai',           label: 'OpenAI 客户端',   hint: 'OpenAI SDK · /v1/chat/completions' },
];

/** Fallback values for "恢复默认" (mirror cc-switch's per-app defaults). */
const TIMEOUT_DEFAULTS = {
    anthropic:        { streaming_first_byte_timeout: 90, streaming_idle_timeout: 180, non_streaming_timeout: 600 },
    openai_responses: { streaming_first_byte_timeout: 60, streaming_idle_timeout: 120, non_streaming_timeout: 600 },
    openai:           { streaming_first_byte_timeout: 60, streaming_idle_timeout: 120, non_streaming_timeout: 600 },
};

const TIMEOUT_FIELD_LABELS = {
    streaming_first_byte_timeout: '首字节',
    streaming_idle_timeout: '静默',
    non_streaming_timeout: '非流式',
};

function timeoutInputId(group, field) {
    return `tc-${group}-${field}`;
}

async function loadTimeoutConfig() {
    try {
        const cfg = await proxyApi('/api/proxy/timeout-config');
        for (const g of TIMEOUT_GROUPS) {
            const row = cfg[g.key] || {};
            for (const field of ['streaming_first_byte_timeout',
                                 'streaming_idle_timeout',
                                 'non_streaming_timeout']) {
                const input = document.getElementById(timeoutInputId(g.key, field));
                if (input) input.value = row[field] != null ? row[field] : '';
            }
        }
    } catch (err) {
        showToast('加载超时配置失败: ' + err.message, 'error');
    }
}

function collectTimeoutConfig() {
    const out = {};
    for (const g of TIMEOUT_GROUPS) {
        out[g.key] = {};
        for (const field of ['streaming_first_byte_timeout',
                             'streaming_idle_timeout',
                             'non_streaming_timeout']) {
            out[g.key][field] = document.getElementById(timeoutInputId(g.key, field)).value;
        }
    }
    return out;
}

async function saveTimeoutConfig() {
    const btn = document.getElementById('btnTimeoutSave');
    btn.disabled = true;
    try {
        await proxyApi('/api/proxy/timeout-config', {
            method: 'PUT',
            body: JSON.stringify(collectTimeoutConfig()),
        });
        showToast('超时配置已保存（即时生效）');
        ConfigSync.markDirty();
    } catch (err) {
        showToast(err.message, 'error');
    }
    btn.disabled = false;
}

/** Fill the inputs with the seed defaults (does not auto-save). */
function resetTimeoutConfig() {
    for (const g of TIMEOUT_GROUPS) {
        const d = TIMEOUT_DEFAULTS[g.key];
        for (const field of ['streaming_first_byte_timeout',
                             'streaming_idle_timeout',
                             'non_streaming_timeout']) {
            document.getElementById(timeoutInputId(g.key, field)).value = d[field];
        }
    }
    showToast('已填入默认值，点击「保存超时配置」生效');
}

// ── Plan billing config ─────────────────────────────────────────────────

async function loadBillingConfig() {
    try {
        const cfg = await proxyApi('/api/proxy/billing-config');
        document.getElementById('billingCancellationMode').value = cfg.cancellation_mode || 'immediate';
    } catch (err) {
        showToast('加载 Plan 计费设置失败: ' + err.message, 'error');
    }
}

async function saveBillingConfig() {
    const btn = document.getElementById('btnBillingConfigSave');
    btn.disabled = true;
    try {
        await proxyApi('/api/proxy/billing-config', {
            method: 'PUT',
            body: JSON.stringify({
                cancellation_mode: document.getElementById('billingCancellationMode').value,
            }),
        });
        showToast('Plan 计费设置已保存');
        ConfigSync.markDirty();
    } catch (err) {
        showToast(err.message, 'error');
    }
    btn.disabled = false;
}

// ── WebDAV sync ───────────────────────────────────────────────────────────

async function loadSyncConfig() {
    try {
        const cfg = await proxyApi('/api/proxy/sync/config');
        document.getElementById('syncBaseUrl').value = cfg.base_url || '';
        document.getElementById('syncFolder').value = cfg.folder || 'token-board-sync';
        document.getElementById('syncUsername').value = cfg.username || '';
        document.getElementById('syncPassword').value = cfg.has_password ? '••••••' : '';
    } catch (e) {
        /* use empty form */
    }
}

async function saveSyncSettings(e) {
    e.preventDefault();
    const form = e.target;
    const data = Object.fromEntries(new FormData(form));
    try {
        await proxyApi('/api/proxy/sync/config', {
            method: 'PUT',
            body: JSON.stringify(data),
        });
        showToast('同步配置已保存');
        loadSyncConfig();  // re-mask the password
    } catch (err) {
        showToast(err.message, 'error');
    }
}

async function testSyncConnection() {
    const btn = document.getElementById('btnTestSync');
    btn.disabled = true;
    btn.textContent = '测试中...';
    try {
        const data = Object.fromEntries(new FormData(document.getElementById('syncConfigForm')));
        const result = await proxyApi('/api/proxy/sync/test', {
            method: 'POST',
            body: JSON.stringify(data),
        });
        showToast(result.message, result.status === 'ok' ? 'success' : 'error');
    } catch (err) {
        showToast('测试失败: ' + err.message, 'error');
    }
    btn.disabled = false;
    btn.textContent = '测试连接';
}

// ── Page ──────────────────────────────────────────────────────────────────

function initSettingsPage() {
    const el = document.getElementById('page-settings-general');
    if (!el || el.dataset.initialized) return;
    el.dataset.initialized = '1';

    const timeoutRows = TIMEOUT_GROUPS.map((g) => {
        const inputs = ['streaming_first_byte_timeout', 'streaming_idle_timeout',
                        'non_streaming_timeout'].map((f) => `
            <td>
                <input type="number" id="${timeoutInputId(g.key, f)}"
                       class="input-number" style="width:110px;"
                       ${f === 'streaming_idle_timeout' ? 'min="0"' : 'min="1"'}>
            </td>`).join('');
        return `
            <tr>
                <td>
                    <div>${esc(g.label)}</div>
                    <div class="td-subtle" style="font-size:12px;color:var(--color-text-secondary);">${esc(g.hint)}</div>
                </td>
                ${inputs}
            </tr>`;
    }).join('');

    el.innerHTML = `
        <div class="page-header">
            <h1 class="page-title">通用设置</h1>
            <p class="page-subtitle">代理超时、Plan/智能体订阅计费与 WebDAV 同步设置</p>
        </div>

        <!-- ═══ Timeout config ═══ -->
        <div class="section">
            <div class="chart-card">
                <div class="chart-card__title" style="margin-bottom:12px;">代理超时配置</div>
                <div class="table-scroll">
                    <table class="mgmt-table">
                        <thead>
                            <tr>
                                <th>客户端格式</th>
                                <th>流式首字节超时（秒）</th>
                                <th>流式静默超时（秒，0=禁用）</th>
                                <th>非流式超时（秒）</th>
                            </tr>
                        </thead>
                        <tbody>${timeoutRows}</tbody>
                    </table>
                </div>
                <div style="margin-top:12px; font-size:13px; color:var(--color-text-secondary);">
                    首字节：等待首个流式数据块的最大时间（1-120）· 静默：两个数据块之间的最大间隔（0-600，填 0 禁用，防止中途卡住）·
                    非流式：非流式请求的整体读取超时（60-1200）
                </div>
                <div style="display:flex; gap:8px; margin-top:12px; justify-content:flex-end;">
                    <button class="btn btn--sm" onclick="resetTimeoutConfig()">恢复默认</button>
                    <button class="btn btn--primary" id="btnTimeoutSave" onclick="saveTimeoutConfig()">保存超时配置</button>
                </div>
            </div>
        </div>

        <!-- ═══ Plan billing config ═══ -->
        <div class="section">
            <div class="chart-card">
                <div class="chart-card__title" style="margin-bottom:12px;">Plan/智能体订阅计费</div>
                <div style="display:grid; grid-template-columns:1fr 1fr; gap:12px;">
                    <label style="align-self:center;">月费修改固定从下一计费周期生效</label>
                    <label>删除订阅类账户的默认操作
                        <select id="billingCancellationMode">
                            <option value="immediate">本期立即删除（本期计费）</option>
                            <option value="end_of_period">到期立即删除（本期计费，下期不计费）</option>
                        </select>
                    </label>
                </div>
                <div style="display:flex; justify-content:flex-end; margin-top:12px;">
                    <button class="btn btn--primary" id="btnBillingConfigSave" onclick="saveBillingConfig()">保存 Plan 计费设置</button>
                </div>
            </div>
        </div>

        <!-- ═══ WebDAV sync ═══ -->
        <div class="section">
            <div class="chart-card">
                <div class="chart-card__title" style="margin-bottom:12px;">WebDAV 同步</div>
                <p style="margin:0 0 12px; font-size:13px; color:var(--color-text-secondary);">
                    多台电脑共用代理时，用 WebDAV 同步配置与聚合用量。
                </p>
                <form id="syncConfigForm" onsubmit="saveSyncSettings(event)">
                    <div style="display:grid; grid-template-columns:1fr 1fr; gap:12px;">
                        <label>WebDAV 服务器地址
                            <input name="base_url" id="syncBaseUrl" required
                                   placeholder="https://dav.example.com/remote.php/dav/files/user">
                        </label>
                        <label>同步文件夹
                            <input name="folder" id="syncFolder" value="token-board-sync" placeholder="token-board-sync">
                        </label>
                        <label>用户名
                            <input name="username" id="syncUsername" required>
                        </label>
                        <label>密码
                            <input name="password" id="syncPassword" type="password" placeholder="留空不变">
                        </label>
                    </div>
                    <div style="display:flex; gap:8px; margin-top:12px;">
                        <button type="submit" class="btn btn--primary">保存配置</button>
                        <button type="button" class="btn btn--sm" id="btnTestSync" onclick="testSyncConnection()">测试连接</button>
                    </div>
                </form>
            </div>
        </div>
    `;

    loadTimeoutConfig();
    loadBillingConfig();
    loadSyncConfig();
}
