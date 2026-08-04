/**
 * proxy_manager.js — Account, Aggregate, Key, and Pricing management pages.
 *
 * Exports: initAccountsPage(), initAggregatesPage(), initKeysPage(),
 *          initPricingPage()
 */

// ── Shared Helpers (esc defined in utils.js) ──

async function proxyFetch(url, options = {}) {
    const resp = await fetch(url, {
        headers: { 'Content-Type': 'application/json', ...options.headers },
        ...options,
    });
    if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.error || `HTTP ${resp.status}`);
    }
    return resp.json();
}

function showToast(msg, type = 'success') {
    const toast = document.createElement('div');
    toast.className = `toast toast--${type}`;
    toast.textContent = msg;
    toast.style.cssText = `
        position:fixed; bottom:24px; right:24px; z-index:9999;
        padding:12px 20px; border-radius:8px; color:#fff; font-size:14px;
        background:${type === 'error' ? '#EF4444' : '#22C55E'};
        box-shadow:0 4px 12px rgba(0,0,0,0.15); animation:slideUp 0.3s ease;
    `;
    document.body.appendChild(toast);
    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transition = 'opacity 0.3s';
        setTimeout(() => toast.remove(), 300);
    }, 3000);
}

function maskKey(key) {
    if (!key) return '***';
    return key.length > 12 ? key.slice(0, 6) + '...' + key.slice(-4) : key.slice(0, 4) + '...';
}

function openModal(id) {
    const modal = document.getElementById(id);
    if (modal) modal.style.display = '';
}

function closeModal(id) {
    const modal = document.getElementById(id);
    if (modal) modal.style.display = 'none';
    if (id === 'keyModal') {
        const disp = document.getElementById('generatedKeyDisplay');
        if (disp) disp.style.display = 'none';
    }
}

// ── Accounts Page ────────────────────────────────────────────────────────

/// Show/hide the plan monthly-price field based on the account type.
function togglePlanFields(sel) {
    const field = document.getElementById('planPriceField');
    if (!field) return;
    field.style.display = (sel && sel.value === 'plan') ? '' : 'none';
}

// ── Multi-key form helpers ─────────────────────────────────────────────
// Editing shows existing keys as masked keep-rows (id + masked value, never
// the secret) plus editable rows for NEW keys. `_keyRowsEdited` tracks whether
// the user touched the key section so a rename-only save never wipes keys.
let _keyRowsEdited = false;

function addKeyRow(value) {
    const list = document.getElementById('keyList');
    if (!list) return;
    const div = document.createElement('div');
    div.style.cssText = 'display:flex; gap:6px; margin:6px 0; align-items:center;';
    const input = document.createElement('input');
    input.type = 'text';
    input.name = 'upstream_keys[]';
    input.placeholder = 'sk-...（新密钥）';
    input.value = value || '';
    input.addEventListener('input', () => { _keyRowsEdited = true; });
    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'btn btn--sm';
    del.textContent = '删除';
    del.onclick = () => { _keyRowsEdited = true; div.remove(); };
    div.appendChild(input);
    div.appendChild(del);
    list.appendChild(div);
}

function addExistingKeyRow(keepId, masked) {
    const list = document.getElementById('keyList');
    if (!list) return;
    const div = document.createElement('div');
    div.style.cssText = 'display:flex; gap:6px; margin:6px 0; align-items:center;';
    div.className = 'existing-key';
    div.dataset.keepId = keepId;
    const span = document.createElement('span');
    span.style.cssText = 'flex:1; font-family:monospace; background:#f3f4f6; padding:4px 8px; border-radius:4px; color:var(--color-text-secondary, #6b7280);';
    span.textContent = masked + '（已配置，如需删除点“移除”）';
    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'btn btn--sm';
    del.textContent = '移除';
    del.onclick = () => { _keyRowsEdited = true; div.remove(); };
    div.appendChild(span);
    div.appendChild(del);
    list.appendChild(div);
}

function resetKeyList() {
    const list = document.getElementById('keyList');
    if (list) list.innerHTML = '';
    _keyRowsEdited = false;
}

function collectKeyRows(form) {
    const keyInputs = [...form.querySelectorAll('#keyList input[name="upstream_keys[]"]')];
    const upstream_keys = keyInputs.map(i => i.value.trim()).filter(v => v);
    const keepRows = [...form.querySelectorAll('#keyList .existing-key')];
    const keep_key_ids = keepRows.map(r => r.dataset.keepId);
    return { upstream_keys, keep_key_ids };
}

async function loadAccountsTable() {
    const tbody = document.querySelector('#accountsTable tbody');
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="8" class="td-loading">加载中...</td></tr>';
    try {
        const accounts = await proxyFetch('/api/proxy/accounts');
        const real = accounts.filter(a => !a.is_aggregate);
        if (!real.length) {
            tbody.innerHTML = '<tr><td colspan="8" class="td-empty">暂无账户，请点击"添加账户"（聚合账户请到"上游账户聚合"管理）</td></tr>';
            return;
        }
        tbody.innerHTML = real.map((a) => `
            <tr>
                <td>${esc(a.name)}</td>
                <td><code>${esc(maskKey(a.upstream_key))}</code>${a.key_count > 1 ? ` <span class="badge" title="${a.key_count} 把密钥（同一配置的多个并发槽位）">×${a.key_count}</span>` : ''}${!a.upstream_key && !a.key_count ? ' <span class="badge" style="color:#B45309;background:#FFFBEB;border-color:#FCD34D;" title="本机未配置上游 Key。云端同步来的账户需在本机填入 Key 才能转发请求">未配置 Key</span>' : ''}</td>
                <td>${esc(a.base_url)}</td>
                <td>${esc(({openai: 'OpenAI', openai_responses: 'OpenAI Responses', anthropic: 'Anthropic'})[a.api_format] || a.api_format)}</td>
                <td>${a.account_type === 'plan' ? '<span class="badge badge--active">plan</span>' : '<span class="badge">api</span>'}</td>
                <td>${a.account_type === 'plan' ? '¥' + (+(a.monthly_price || 0)).toFixed(2) + (a.key_count > 1 ? '×' + a.key_count : '') + '/月' : '-'}</td>
                <td>${a.max_concurrency ? a.max_concurrency + ' 并发' : '无限制'}</td>
                <td>
                    <button class="btn btn--sm" onclick="editAccount(${a.id})">编辑</button>
                    <button class="btn btn--sm" onclick="updateAccountModels(${a.id}, '${esc(a.name)}')">更新模型</button>
                </td>
            </tr>
        `).join('');
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="8" class="td-error">加载失败: ${esc(err.message)}</td></tr>`;
    }
}

async function saveAccount(e) {
    e.preventDefault();
    const form = e.target;
    const id = form.dataset.editId;
    const { upstream_keys, keep_key_ids } = collectKeyRows(form);
    try {
        if (id) {
            const payload = {
                name: form['name'].value,
                base_url: form['base_url'].value,
                api_format: form['api_format'].value,
                endpoint_path: form['endpoint_path'].value || '',
                auth_header: form['auth_header'].value || 'auto',
                account_type: form['account_type'].value || 'api',
                monthly_price: form['monthly_price'].value || 0,
                max_concurrency: form['max_concurrency'].value || null,
            };
            // Only send the key set when the user actually edited it — a
            // rename-only save must never wipe the existing keys.
            if (_keyRowsEdited) {
                payload.upstream_keys = upstream_keys;
                payload.keep_key_ids = keep_key_ids;
                payload.keys_edited = true;
            }
            await proxyFetch(`/api/proxy/accounts/${id}`, {
                method: 'PUT',
                body: JSON.stringify(payload),
            });
            showToast('账户已更新');
            ConfigSync.markDirty();
        } else {
            await proxyFetch('/api/proxy/accounts', {
                method: 'POST',
                body: JSON.stringify({
                    name: form['name'].value,
                    upstream_keys,
                    base_url: form['base_url'].value,
                    api_format: form['api_format'].value,
                    endpoint_path: form['endpoint_path'].value || '',
                    auth_header: form['auth_header'].value || 'auto',
                    account_type: form['account_type'].value || 'api',
                    monthly_price: form['monthly_price'].value || 0,
                    max_concurrency: form['max_concurrency'].value || null,
                }),
            });
            showToast('账户已创建');
            ConfigSync.markDirty();
        }
        form.reset();
        resetKeyList();
        form.dataset.editId = '';
        document.getElementById('accountDeleteBtn').style.display = 'none';
        document.getElementById('accountModelBtn').style.display = 'none';
        form.querySelector('[type=submit]').textContent = '添加账户';
        closeModal('accountModal');
        loadAccountsTable();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

function editAccount(id) {
    proxyFetch('/api/proxy/accounts').then((accounts) => {
        const acc = accounts.find((a) => a.id === id);
        if (!acc) return;
        const form = document.querySelector('#accountForm');
        form['name'].value = acc.name;
        form['base_url'].value = acc.base_url;
        form['api_format'].value = acc.api_format || 'openai';
        form['endpoint_path'].value = acc.endpoint_path || '';
        form['auth_header'].value = acc.auth_header || 'auto';
        form['account_type'].value = acc.account_type || 'api';
        form['monthly_price'].value = acc.account_type === 'plan' ? (acc.monthly_price || 0) : '';
        form['max_concurrency'].value = acc.max_concurrency || '';
        togglePlanFields(form['account_type']);
        // Existing keys → masked keep-rows; user adds new rows as needed.
        resetKeyList();
        (acc.keys || []).forEach((k) => addExistingKeyRow(k.id, k.masked));
        form.dataset.editId = id;
        form.querySelector('[type=submit]').textContent = '保存';
        document.getElementById('accountDeleteBtn').style.display = '';
        document.getElementById('accountModelBtn').style.display = '';
        document.getElementById('accountDeleteBtn').onclick = () => { closeModal('accountModal'); deleteAccount(id, acc.name); };
        document.getElementById('accountModelBtn').onclick = () => updateAccountModels(id, acc.name);
        openModal('accountModal');
    });
}

let _deleteAccountPendingId = null;

async function deleteAccount(id, name) {
    // Count local keys bound to this account.
    let keys = [];
    try { keys = await proxyFetch('/api/proxy/keys'); } catch (_) {}
    const bound = keys.filter((k) => k.account_id === id).length;

    if (bound === 0) {
        if (!confirm(`确定删除账户 "${name}"？`)) return;
        await _doDeleteAccount(id, 'detach');
        return;
    }
    // Has bound keys → let the user choose cascade vs detach.
    _deleteAccountPendingId = id;
    document.getElementById('deleteAccountMsg').textContent =
        `账户 "${name}" 有 ${bound} 个关联本地密钥，选择删除方式：`;
    openModal('deleteAccountModal');
}

async function _doDeleteAccount(id, mode) {
    try {
        await proxyFetch(`/api/proxy/accounts/${id}?mode=${mode}`, { method: 'DELETE' });
        showToast('账户已删除');
        ConfigSync.markDirty();
        closeModal('deleteAccountModal');
        loadAccountsTable();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

function deleteAccountCascade() {
    if (_deleteAccountPendingId != null) _doDeleteAccount(_deleteAccountPendingId, 'cascade');
}

function deleteAccountDetach() {
    if (_deleteAccountPendingId != null) _doDeleteAccount(_deleteAccountPendingId, 'detach');
}

async function updateAccountModels(id, name) {
    const btns = document.querySelectorAll('#accountModelBtn, [onclick*="updateAccountModels"]');
    btns.forEach(b => { b.disabled = true; b.textContent = '获取中...'; });
    try {
        const result = await proxyFetch(`/api/proxy/accounts/${id}/models`, { method: 'POST', body: '{}' });
        showToast(`${name}: 获取到 ${result.count} 个模型`);
        ConfigSync.markDirty();
    } catch (err) {
        showToast(`获取失败: ${err.message}`, 'error');
    }
    btns.forEach(b => { b.disabled = false; b.textContent = '更新模型'; });
}

function openAddAccountModal() {
    const form = document.querySelector('#accountForm');
    if (form) {
        form.reset();
        form.dataset.editId = '';
        togglePlanFields(form['account_type']);
    }
    resetKeyList();
    const delBtn = document.getElementById('accountDeleteBtn');
    const modelBtn = document.getElementById('accountModelBtn');
    if (delBtn) delBtn.style.display = 'none';
    if (modelBtn) modelBtn.style.display = 'none';
    const submit = form && form.querySelector('[type=submit]');
    if (submit) submit.textContent = '添加账户';
    openModal('accountModal');
}

function initAccountsPage() {
    const el = document.getElementById('page-proxy-accounts');
    if (!el || el.dataset.initialized) return;
    el.dataset.initialized = '1';
    el.innerHTML = `
        <div class="page-header">
            <h1 class="page-title">上游账户管理</h1>
            <p class="page-subtitle">支持 OpenAI 兼容 / OpenAI Responses / Anthropic 兼容 的上游服务</p>
            <button class="btn btn--primary" onclick="openAddAccountModal()">+ 添加账户</button>
        </div>
        <table class="mgmt-table" id="accountsTable">
            <thead><tr><th>名称</th><th>上游密钥</th><th>Base URL</th><th>API 格式</th><th>类型</th><th>plan 月费</th><th>并发限额</th><th>操作</th></tr></thead>
            <tbody></tbody>
        </table>
        <div class="modal-overlay" id="accountModal" style="display:none">
            <div class="modal">
                <div class="modal__header">
                    <h3>账户信息</h3>
                    <button class="modal__close" onclick="closeModal('accountModal')">&times;</button>
                </div>
                <form id="accountForm" onsubmit="saveAccount(event)" data-edit-id="">
                    <label>名称 <input name="name" required></label>
                    <div style="margin-bottom:10px;">
                        <label>上游 API Key（多把密钥 = 同一配置的多个槽位；仅存本机，不上传云端）</label>
                        <div id="keyList" style="margin:4px 0;"></div>
                        <button type="button" class="btn btn--sm" onclick="addKeyRow()">+ 添加密钥</button>
                    </div>
                    <label>Base URL <input name="base_url" placeholder="https://api.example.com/v1"></label>
                    <label>API 格式
                        <select name="api_format">
                            <option value="openai">OpenAI 兼容</option>
                            <option value="openai_responses">OpenAI Responses</option>
                            <option value="anthropic">Anthropic 兼容</option>
                        </select>
                    </label>
                    <label>上游路径（可选）
                        <input name="endpoint_path" placeholder="留空自动推导，如 /v1/messages 或 /responses">
                    </label>
                    <label>认证方式
                        <select name="auth_header">
                            <option value="auto">自动（按 API 格式推导）</option>
                            <option value="bearer">Authorization: Bearer</option>
                            <option value="x-api-key">x-api-key + anthropic-version</option>
                        </select>
                    </label>
                    <label>账户类型
                        <select name="account_type" onchange="togglePlanFields(this)">
                            <option value="api">api — 按调用量计费</option>
                            <option value="plan">plan — 订阅套餐，调用免费</option>
                        </select>
                    </label>
                    <label id="planPriceField" style="display:none;">plan 每月价格 (¥)
                        <input name="monthly_price" type="number" step="0.01" min="0" placeholder="如 99">
                    </label>
                    <label>并发限额（可选，留空 = 无限制）
                        <input name="max_concurrency" type="number" step="1" min="1" placeholder="如 3">
                    </label>
                    <div style="display:flex; gap:8px;">
                        <button type="submit" class="btn btn--primary">添加账户</button>
                        <button type="button" class="btn btn--sm" id="accountModelBtn" style="display:none">更新模型</button>
                        <button type="button" class="btn btn--sm" id="accountDeleteBtn" style="display:none; color:#EF4444;">删除账户</button>
                    </div>
                </form>
            </div>
        </div>
        <div class="modal-overlay" id="deleteAccountModal" style="display:none">
            <div class="modal">
                <div class="modal__header">
                    <h3>删除账户</h3>
                    <button class="modal__close" onclick="closeModal('deleteAccountModal')">&times;</button>
                </div>
                <div id="deleteAccountMsg" style="padding:10px 0;"></div>
                <div style="display:flex; flex-direction:column; gap:8px;">
                    <button class="btn" onclick="deleteAccountCascade()">级联删除密钥并删除账户</button>
                    <button class="btn" style="color:#EF4444;" onclick="deleteAccountDetach()">仅解绑密钥（密钥需重新分配）</button>
                </div>
            </div>
        </div>
    `;
    loadAccountsTable();
}

// ── Keys Page ────────────────────────────────────────────────────────────

async function loadKeysTable() {
    const tbody = document.querySelector('#keysTable tbody');
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="6" class="td-loading">加载中...</td></tr>';
    try {
        const keys = await proxyFetch('/api/proxy/keys');
        if (!keys.length) {
            tbody.innerHTML = '<tr><td colspan="6" class="td-empty">暂无密钥，请点击"生成密钥"</td></tr>';
            return;
        }
        tbody.innerHTML = keys.map((k) => `
            <tr>
                <td><code class="key-display" title="${esc(k.key_value)}">${esc(k.key_masked)}</code> <button class="btn btn--sm" onclick="copyKey('${esc(k.key_value)}')">复制</button></td>
                <td>${esc(k.label || '-')}</td>
                <td>${k.account_id == null ? '<span style="color:#9CA3AF;">未分配</span>' : esc(k.account_name || `ID:${k.account_id}`)}</td>
                <td>${esc(k.last_used_at || '从未使用')}</td>
                <td>${esc(k.created_at || '')}</td>
                <td>
                    <button class="btn btn--sm" onclick="openEditKeyModal(${k.id})">编辑</button>
                    <button class="btn btn--sm" onclick="deleteKey(${k.id}, '${esc(k.label || k.key_masked)}')" style="color:#EF4444;">删除</button>
                </td>
            </tr>
        `).join('');
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="6" class="td-error">加载失败: ${esc(err.message)}</td></tr>`;
    }
}

async function generateKey(e) {
    e.preventDefault();
    const form = e.target;
    const data = Object.fromEntries(new FormData(form));
    try {
        const result = await proxyFetch('/api/proxy/keys', {
            method: 'POST',
            body: JSON.stringify(data),
        });
        document.getElementById('generatedKeyDisplay').style.display = '';
        document.getElementById('generatedKeyValue').textContent = result.key_value;
        showToast('密钥已生成！请立即复制保存');
        ConfigSync.markDirty();
        loadKeysTable();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

function copyKey(text) {
    navigator.clipboard.writeText(text).then(() => showToast('已复制到剪贴板'));
}

async function deleteKey(id, label) {
    if (!confirm(`确定删除密钥 "${label}"？\n\n此操作不可恢复，使用该密钥的 AI 工具将立即无法连接。`)) return;
    try {
        await proxyFetch(`/api/proxy/keys/${id}`, { method: 'DELETE' });
        showToast('密钥已删除');
        ConfigSync.markDirty();
        loadKeysTable();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

async function openEditKeyModal(id) {
    try {
        const [keys, accounts] = await Promise.all([
            proxyFetch('/api/proxy/keys'),
            proxyFetch('/api/proxy/accounts'),
        ]);
        const key = keys.find((k) => k.id === id);
        if (!key) return;

        // Populate form
        document.getElementById('editKeyLabel').value = key.label || '';
        const accountSel = document.getElementById('editKeyAccount');
        accountSel.innerHTML =
            `<option value="" ${key.account_id == null ? 'selected' : ''}>未分配</option>` +
            accounts.map((a) =>
                `<option value="${a.id}" ${a.id === key.account_id ? 'selected' : ''}>${esc(a.name)}</option>`
            ).join('');

        document.getElementById('editKeyId').value = id;
        openModal('editKeyModal');
    } catch (err) {
        showToast(err.message, 'error');
    }
}

async function saveKeyEdit(e) {
    e.preventDefault();
    const form = e.target;
    const id = form['id'].value;
    const label = form['label'].value;
    const accountIdVal = form['account_id'].value;
    const accountId = accountIdVal === '' ? null : parseInt(accountIdVal);

    try {
        const data = {};
        if (label) data.label = label;
        data.account_id = accountId;
        await proxyFetch(`/api/proxy/keys/${id}`, { method: 'PUT', body: JSON.stringify(data) });
        showToast('密钥已更新');
        ConfigSync.markDirty();
        closeModal('editKeyModal');
        loadKeysTable();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

async function loadAccountOptions() {
    try {
        const accounts = await proxyFetch('/api/proxy/accounts');
        const sel = document.getElementById('keyAccountSelect');
        if (!sel) return;
        sel.innerHTML = accounts.map((a) => `<option value="${a.id}">${esc(a.name)}</option>`).join('');
    } catch (err) {
        console.error('Failed to load accounts:', err);
    }
}

function initKeysPage() {
    const el = document.getElementById('page-proxy-keys');
    if (!el || el.dataset.initialized) return;
    el.dataset.initialized = '1';

    el.innerHTML = `
        <div class="page-header">
            <h1 class="page-title">本地密钥管理</h1>
            <p class="page-subtitle">生成密钥供 AI 工具连接代理使用</p>
            <button class="btn btn--primary" onclick="openModal('keyModal')">+ 生成密钥</button>
        </div>
        <div style="margin-bottom:16px; padding:12px 16px; background:var(--color-surface, #F8FAFC); border:1px solid var(--color-border); border-radius:8px; font-size:13px; color:var(--color-text-tertiary);">
            <strong style="color:var(--color-text-secondary);">配置说明</strong>
            <div style="margin-top:6px;">一把密钥同时支持三种客户端格式，代理根据请求 URL 自动识别并转换为上游格式</div>
            <div style="margin-top:6px;"><code style="background:var(--color-bg, #F1F5F9); padding:2px 6px; border-radius:4px;">BASE_URL = http://localhost:8800/v1</code></div>
        </div>
        <table class="mgmt-table" id="keysTable">
            <thead><tr><th>密钥</th><th>标签</th><th>关联账户</th><th>最后使用</th><th>创建时间</th><th>操作</th></tr></thead>
            <tbody></tbody>
        </table>

        <!-- Generate Key Modal -->
        <div class="modal-overlay" id="keyModal" style="display:none">
            <div class="modal">
                <div class="modal__header">
                    <h3>生成新密钥</h3>
                    <button class="modal__close" onclick="closeModal('keyModal')">&times;</button>
                </div>
                <form id="keyForm" onsubmit="generateKey(event)">
                    <label>标签（可选）<input name="label" placeholder="例如: Claude Code"></label>
                    <label>关联账户 <select name="account_id" id="keyAccountSelect" required></select></label>
                    <button type="submit" class="btn btn--primary">生成密钥</button>
                </form>
                <div id="generatedKeyDisplay" style="display:none; margin-top:16px; padding:12px; background:#F0FDF4; border-radius:8px;">
                    <strong style="color:#166534">新密钥（仅显示一次）：</strong>
                    <code id="generatedKeyValue" style="word-break:break-all; font-size:14px;"></code>
                    <button class="btn btn--sm" onclick="copyKey(document.getElementById('generatedKeyValue').textContent)" style="margin-top:8px">复制密钥</button>
                </div>
            </div>
        </div>

        <!-- Edit Key Modal -->
        <div class="modal-overlay" id="editKeyModal" style="display:none">
            <div class="modal">
                <div class="modal__header">
                    <h3>编辑密钥</h3>
                    <button class="modal__close" onclick="closeModal('editKeyModal')">&times;</button>
                </div>
                <form id="editKeyForm" onsubmit="saveKeyEdit(event)">
                    <input type="hidden" name="id" id="editKeyId">
                    <label>标签 <input name="label" id="editKeyLabel" placeholder="例如: Claude Code"></label>
                    <label>关联账户 <select name="account_id" id="editKeyAccount"></select></label>
                    <button type="submit" class="btn btn--primary" style="margin-top:12px;">保存</button>
                </form>
            </div>
        </div>
    `;

    loadAccountOptions();
    loadKeysTable();
}

// ── Aggregates Page ─────────────────────────────────────────────────────

let aggAccountsCache = null;  // real upstream accounts for entry dropdowns

async function loadAggregatesTable() {
    const tbody = document.querySelector('#aggregatesTable tbody');
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="4" class="td-loading">加载中...</td></tr>';
    try {
        const aggregates = await proxyFetch('/api/proxy/aggregates');
        if (!aggregates.length) {
            tbody.innerHTML = '<tr><td colspan="4" class="td-empty">暂无聚合账户，请点击"新建聚合账户"</td></tr>';
            return;
        }
        tbody.innerHTML = aggregates.map((a) => `
            <tr>
                <td>${esc(a.name)}</td>
                <td>${a.entries ? a.entries.length : 0} 条映射</td>
                <td>${a.entries ? a.entries.map(e =>
                    `<code>${esc(e.pattern)} → ${esc(e.upstream_account_name || `账户${e.upstream_account_id}`)} / ${esc(e.upstream_model)}</code>`
                ).join('<br>') : ''}</td>
                <td>
                    <button class="btn btn--sm" onclick="openAggregateModal(${a.id})">编辑</button>
                    <button class="btn btn--sm" onclick="deleteAggregate(${a.id}, '${esc(a.name)}')" style="color:#EF4444;">删除</button>
                </td>
            </tr>
        `).join('');
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="4" class="td-error">加载失败: ${esc(err.message)}</td></tr>`;
    }
}

function aggRow(pattern, accountId, accountName, upstreamModel) {
    const accountOpts = aggAccountsCache && aggAccountsCache.length
        ? aggAccountsCache.map(a =>
            `<option value="${a.id}" ${a.id === accountId ? 'selected' : ''}>${esc(a.name)}</option>`).join('')
        : `<option value="${accountId || ''}">${esc(accountName || '加载中...')}</option>`;
    return `<div class="map-row" style="display:flex;gap:8px;align-items:center;margin-bottom:6px;">
        <input value="${esc(pattern||'')}" placeholder="模型名称" title="精确模型名；同一模型可配多行（多上游账户），按顺序依次使用" style="flex:1;font-size:12px;padding:4px 8px;border:1px solid var(--color-border);border-radius:4px;">
        <span style="color:var(--color-text-tertiary);">→</span>
        <select class="agg-acct" style="flex:1;font-size:12px;padding:4px 8px;border:1px solid var(--color-border);border-radius:4px;" onchange="resetAggModel(this)">${accountOpts}</select>
        <select class="agg-model" style="flex:1;font-size:12px;padding:4px 8px;border:1px solid var(--color-border);border-radius:4px;" onfocus="loadAggModels(this)"><option value="${esc(upstreamModel||'')}">${esc(upstreamModel||'点击获取模型')}</option></select>
        <button type="button" class="btn btn--sm" onclick="moveAggRow(this, 'up')" title="上移">▲</button>
        <button type="button" class="btn btn--sm" onclick="moveAggRow(this, 'down')" title="下移">▼</button>
        <button type="button" class="btn btn--sm" onclick="this.parentElement.remove()" style="color:#EF4444;">✕</button>
    </div>`;
}

function addAggRow() {
    const div = document.createElement('div');
    div.innerHTML = aggRow('', '', '', '');
    document.getElementById('aggMappings').appendChild(div.firstElementChild);
}

function moveAggRow(btn, dir) {
    const row = btn.parentElement;
    if (dir === 'up' && row.previousElementSibling) {
        row.parentElement.insertBefore(row, row.previousElementSibling);
    } else if (dir === 'down' && row.nextElementSibling) {
        row.parentElement.insertBefore(row.nextElementSibling, row);
    }
}

function resetAggModel(sel) {
    const row = sel.closest('.map-row');
    const modelSel = row.querySelector('.agg-model');
    modelSel.innerHTML = '<option value="">点击获取模型</option>';
}

async function loadAggModels(sel) {
    if (sel.options.length > 1) return;
    const row = sel.closest('.map-row');
    const acctId = row.querySelector('.agg-acct').value;
    if (!acctId) { sel.innerHTML = '<option value="">请先选择账户</option>'; return; }
    try {
        const models = await proxyFetch(`/api/proxy/accounts/${acctId}/models`);
        if (!models.length) { sel.innerHTML = '<option value="">该账户暂无模型</option>'; return; }
        const cur = sel.value;
        sel.innerHTML = models.map(m => `<option value="${esc(m)}" ${m===cur?'selected':''}>${esc(m)}</option>`).join('');
    } catch (e) {
        sel.innerHTML = '<option value="">加载失败</option>';
    }
}

async function loadAggAccountCache() {
    if (aggAccountsCache) return;
    const accounts = await proxyFetch('/api/proxy/accounts');
    aggAccountsCache = accounts.filter(a => !a.is_aggregate);
}

async function openAggregateModal(id) {
    document.getElementById('aggregateForm').dataset.editId = id || '';
    document.getElementById('aggregateForm').querySelector('[type=submit]').textContent = id ? '保存' : '创建';
    document.getElementById('aggMappings').innerHTML = '';
    document.getElementById('aggregateName').value = '';

    try {
        await loadAggAccountCache();
    } catch (e) {
        aggAccountsCache = [];
    }

    if (id) {
        const aggregates = await proxyFetch('/api/proxy/aggregates');
        const agg = aggregates.find(a => a.id === id);
        if (!agg) return;
        document.getElementById('aggregateName').value = agg.name;
        if (agg.entries) {
            document.getElementById('aggMappings').innerHTML = agg.entries.map((e) =>
                aggRow(e.pattern, e.upstream_account_id, e.upstream_account_name, e.upstream_model)
            ).join('');
        }
    }
    openModal('aggregateModal');
    if (!document.querySelector('#aggMappings .map-row')) addAggRow();
}

async function saveAggregate(e) {
    e.preventDefault();
    const form = e.target;
    const id = form.dataset.editId;
    const name = document.getElementById('aggregateName').value;
    const rows = document.querySelectorAll('#aggMappings .map-row');
    const entries = [];
    for (const row of rows) {
        const pattern = row.querySelector('input').value.trim();
        const accountId = row.querySelector('.agg-acct').value;
        const upstream = row.querySelector('.agg-model').value.trim();
        if (pattern && accountId && upstream) {
            if (/[*?]/.test(pattern)) {
                showToast(`模型名称 "${pattern}" 不能包含通配符（* ?），请填写精确模型名`, 'error');
                return;
            }
            entries.push({ pattern, account_id: parseInt(accountId), upstream_model: upstream });
        }
    }
    if (!entries.length) {
        showToast('请至少添加一条模型映射', 'error');
        return;
    }
    try {
        if (id) {
            await proxyFetch(`/api/proxy/aggregates/${id}`, { method: 'PUT', body: JSON.stringify({ name, entries }) });
            showToast('聚合账户已更新');
            ConfigSync.markDirty();
        } else {
            await proxyFetch('/api/proxy/aggregates', { method: 'POST', body: JSON.stringify({ name, entries }) });
            showToast('聚合账户已创建');
            ConfigSync.markDirty();
        }
        closeModal('aggregateModal');
        loadAggregatesTable();
    } catch (err) { showToast(err.message, 'error'); }
}

async function deleteAggregate(id, name) {
    if (!confirm(`删除聚合账户 "${name}"？\n\n关联此聚合账户的本地密钥将失效。`)) return;
    try {
        await proxyFetch(`/api/proxy/aggregates/${id}`, { method: 'DELETE' });
        showToast('聚合账户已删除');
        ConfigSync.markDirty();
        loadAggregatesTable();
    } catch (err) { showToast(err.message, 'error'); }
}

function initAggregatesPage() {
    const el = document.getElementById('page-proxy-aggregates');
    if (!el || el.dataset.initialized) return;
    el.dataset.initialized = '1';
    el.innerHTML = `
        <div class="page-header">
            <h1 class="page-title">上游账户聚合</h1>
            <p class="page-subtitle">聚合多个上游账户为一个账户：模型列表即此聚合账户暴露给客户端的全部模型。同一模型可配置多个上游账户，请求从上到下依次使用——当前账户达到并发限额或处于冷却期时自动使用下一个</p>
            <button class="btn btn--primary" onclick="openAggregateModal()">+ 新建聚合账户</button>
        </div>
        <table class="mgmt-table" id="aggregatesTable">
            <thead><tr><th>名称</th><th>条目数</th><th>映射预览</th><th>操作</th></tr></thead>
            <tbody></tbody>
        </table>
        <div class="modal-overlay" id="aggregateModal" style="display:none">
            <div class="modal" style="max-width:720px;">
                <div class="modal__header">
                    <h3>聚合账户</h3>
                    <button class="modal__close" onclick="closeModal('aggregateModal')">&times;</button>
                </div>
                <form id="aggregateForm" onsubmit="saveAggregate(event)" data-edit-id="">
                    <label>聚合账户名称 <input id="aggregateName" name="name" required placeholder="例如: 全部模型聚合"></label>
                    <label>模型映射 <button type="button" class="btn btn--sm" onclick="addAggRow()">+ 添加</button></label>
                    <div id="aggMappings" style="max-height:350px;overflow-y:auto;"></div>
                    <button type="submit" class="btn btn--primary">创建</button>
                </form>
            </div>
        </div>
    `;
    loadAggregatesTable();
}

// ── Pricing Page ─────────────────────────────────────────────────────────

// Cached pricing rows (with slots) so the edit modal can look up by id.
let _pricingCache = [];

// ── Time-slot helpers (UTC+0 storage ↔ UTC+8 UI) ─────────────────────────
// Slot boundaries are stored as minute-of-day in UTC+0; the UI enters and
// shows them in UTC+8 (Beijing time).
function minutesToHHMM(m) {
    m = ((Math.round(m) % 1440) + 1440) % 1440;
    return String(Math.floor(m / 60)).padStart(2, '0') + ':' + String(m % 60).padStart(2, '0');
}
function hhmmToMinutes(hhmm) {
    var p = String(hhmm || '').split(':').map(Number);
    if (p.length < 2 || isNaN(p[0]) || isNaN(p[1])) return NaN;
    return p[0] * 60 + p[1];
}
function minutes8to0(min8) { return ((min8 - 480) % 1440 + 1440) % 1440; }  // UTC+8 → UTC+0
function minutes0to8(min0) { return ((min0 + 480) % 1440 + 1440) % 1440; }  // UTC+0 → UTC+8

function addSlotRow(slot) {
    var rows = document.getElementById('slotRows');
    if (!rows) return;
    var div = document.createElement('div');
    div.className = 'slot-row';
    div.style.cssText = 'display:flex;gap:6px;align-items:center;margin-bottom:6px;';
    var startVal = slot ? minutesToHHMM(minutes0to8(slot.start_minute)) : '08:00';
    var endVal = slot ? minutesToHHMM(minutes0to8(slot.end_minute)) : '23:00';
    var multVal = slot ? slot.multiplier : 1.0;
    div.innerHTML =
        '<input type="time" class="slot-start" step="60" value="' + startVal + '">' +
        '<span style="color:var(--color-text-secondary);">至</span>' +
        '<input type="time" class="slot-end" step="60" value="' + endVal + '">' +
        '<input type="number" class="slot-multiplier" step="0.05" min="0" value="' + multVal + '" style="width:70px;" placeholder="倍率">' +
        '<button type="button" class="btn btn--sm" onclick="removeSlotRow(this)">×</button>';
    rows.appendChild(div);
}

function removeSlotRow(btn) {
    var row = btn.parentElement;
    if (row) row.remove();
}

function collectSlots() {
    var rows = document.querySelectorAll('#slotRows .slot-row');
    var slots = [];
    rows.forEach(function (row) {
        var start = hhmmToMinutes(row.querySelector('.slot-start').value);
        var end = hhmmToMinutes(row.querySelector('.slot-end').value);
        var mult = parseFloat(row.querySelector('.slot-multiplier').value);
        if (isNaN(start) || isNaN(end) || isNaN(mult)) return;  // skip incomplete rows
        if (start === end) return;  // zero-length window is meaningless
        slots.push({
            start_minute: minutes8to0(start),
            end_minute: minutes8to0(end),
            multiplier: mult,
        });
    });
    return slots;
}

function slotsSummary(slots) {
    if (!slots || !slots.length) return '<span style="color:var(--color-text-tertiary);">无</span>';
    return slots.map(function (s) {
        return minutesToHHMM(minutes0to8(s.start_minute)) + '-' + minutesToHHMM(minutes0to8(s.end_minute)) + ' ×' + s.multiplier;
    }).join('、');
}

async function loadPricingTable() {
    const tbody = document.querySelector('#pricingTable tbody');
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="7" class="td-loading">加载中...</td></tr>';
    try {
        const pricing = await proxyFetch('/api/proxy/pricing');
        _pricingCache = pricing;
        if (!pricing.length) {
            tbody.innerHTML = '<tr><td colspan="7" class="td-empty">暂无定价</td></tr>';
            return;
        }
        tbody.innerHTML = pricing.map((p) => `
            <tr>
                <td><code>${esc(p.model_pattern)}</code></td>
                <td>¥${p.input_price.toFixed(4)} / 1M tokens</td>
                <td>¥${p.output_price.toFixed(4)} / 1M tokens</td>
                <td>${p.cache_read_price != null ? '¥' + p.cache_read_price.toFixed(4) + ' / 1M tokens' : '<span style="color:var(--color-text-tertiary);">同输入价</span>'}</td>
                <td>${slotsSummary(p.slots)}</td>
                <td>${esc(p.currency)}</td>
                <td>
                    <button class="btn btn--sm" onclick="reorderPricing(${p.id},'up')">▲</button>
                    <button class="btn btn--sm" onclick="reorderPricing(${p.id},'down')">▼</button>
                    <button class="btn btn--sm" onclick="editPricing(${p.id})">编辑</button>
                    <button class="btn btn--sm" onclick="deletePricing(${p.id})">删除</button>
                </td>
            </tr>
        `).join('');
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="7" class="td-error">加载失败: ${esc(err.message)}</td></tr>`;
    }
}

async function savePricing(e) {
    e.preventDefault();
    const form = e.target;
    const data = Object.fromEntries(new FormData(form));
    const id = form.dataset.editId;
    const cacheRead = data.cache_read_price !== '' ? parseFloat(data.cache_read_price) : null;
    const payload = {
        model_pattern: data.model_pattern,
        input_price: parseFloat(data.input_price),
        output_price: parseFloat(data.output_price),
        cache_read_price: cacheRead,
        slots: collectSlots(),
    };
    try {
        if (id) {
            await proxyFetch(`/api/proxy/pricing/${id}`, { method: 'PUT', body: JSON.stringify(payload) });
            showToast('定价已更新');
            ConfigSync.markDirty();
        } else {
            await proxyFetch('/api/proxy/pricing', { method: 'POST', body: JSON.stringify(payload) });
            showToast('定价已添加');
            ConfigSync.markDirty();
        }
        form.reset();
        form.dataset.editId = '';
        form.querySelector('[type=submit]').textContent = '添加';
        document.getElementById('slotRows').innerHTML = '';
        closeModal('pricingModal');
        loadPricingTable();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

function editPricing(id) {
    const p = _pricingCache.find(x => x.id === id);
    if (!p) return;
    const form = document.querySelector('#pricingForm');
    form['model_pattern'].value = p.model_pattern;
    form['input_price'].value = p.input_price;
    form['output_price'].value = p.output_price;
    form['cache_read_price'].value = p.cache_read_price == null ? '' : p.cache_read_price;
    // Populate time-slot editor (empty for new rows)
    const slotRows = document.getElementById('slotRows');
    if (slotRows) {
        slotRows.innerHTML = '';
        (p.slots || []).forEach(function (s) { addSlotRow(s); });
    }
    form.dataset.editId = id;
    form.querySelector('[type=submit]').textContent = '更新';
    openModal('pricingModal');
}

async function deletePricing(id) {
    if (!confirm('确定删除此定价条目？')) return;
    try {
        await proxyFetch(`/api/proxy/pricing/${id}`, { method: 'DELETE' });
        showToast('定价已删除');
        ConfigSync.markDirty();
        loadPricingTable();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

async function reorderPricing(id, dir) {
    try {
        await proxyFetch('/api/proxy/pricing/reorder', { method: 'POST', body: JSON.stringify({ id, direction: dir }) });
        ConfigSync.markDirty();
        loadPricingTable();
    } catch (err) { showToast(err.message, 'error'); }
}

function initPricingPage() {
    const el = document.getElementById('page-proxy-pricing');
    if (!el || el.dataset.initialized) return;
    el.dataset.initialized = '1';
    el.innerHTML = `
        <div class="page-header">
            <h1 class="page-title">模型定价管理</h1>
            <p class="page-subtitle">配置模型价格（百万元 token 价格，人民币）· 时段倍率按 UTC+8 时间设置</p>
            <button class="btn btn--primary" onclick="openModal('pricingModal')">+ 添加定价</button>
        </div>
        <table class="mgmt-table" id="pricingTable">
            <thead><tr><th>模型匹配</th><th>输入价格</th><th>输出价格</th><th>缓存命中价格</th><th>时段倍率</th><th>货币</th><th>操作</th></tr></thead>
            <tbody></tbody>
        </table>
        <div class="modal-overlay" id="pricingModal" style="display:none">
            <div class="modal">
                <div class="modal__header">
                    <h3>模型定价</h3>
                    <button class="modal__close" onclick="closeModal('pricingModal')">&times;</button>
                </div>
                <form id="pricingForm" onsubmit="savePricing(event)" data-edit-id="">
                    <label>模型匹配模式 <input name="model_pattern" required placeholder="例如: deepseek-v4*"></label>
                    <label>输入价格 (¥/1M tokens) <input name="input_price" type="number" step="0.0001" required></label>
                    <label>输出价格 (¥/1M tokens) <input name="output_price" type="number" step="0.0001" required></label>
                    <label>缓存命中价格 (¥/1M tokens，可选) <input name="cache_read_price" type="number" step="0.0001" placeholder="留空 = 与输入价格相同"></label>
                    <div style="margin:10px 0;">
                        <div style="font-size:13px;color:var(--color-text-secondary);margin-bottom:6px;">
                            时段倍率（每日生效，按 UTC+8 时间；倍率作用于输入/输出/缓存三档价格）
                        </div>
                        <div id="slotRows"></div>
                        <button type="button" class="btn btn--sm" onclick="addSlotRow(null)">+ 添加时段</button>
                    </div>
                    <button type="submit" class="btn btn--primary">添加</button>
                </form>
            </div>
        </div>
    `;
    document.getElementById('slotRows').innerHTML = '';
    loadPricingTable();
}
