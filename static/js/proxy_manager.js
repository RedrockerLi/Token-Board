/**
 * proxy_manager.js — Account, Key, and Pricing management pages.
 *
 * Exports: initAccountsPage(), initKeysPage(), initPricingPage()
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

async function loadAccountsTable() {
    const tbody = document.querySelector('#accountsTable tbody');
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="6" class="td-loading">加载中...</td></tr>';
    try {
        const accounts = await proxyFetch('/api/proxy/accounts');
        if (!accounts.length) {
            tbody.innerHTML = '<tr><td colspan="6" class="td-empty">暂无账户，请点击"添加账户"</td></tr>';
            return;
        }
        tbody.innerHTML = accounts.map((a) => `
            <tr>
                <td>${esc(a.name)}</td>
                <td><code>${esc(maskKey(a.upstream_key))}</code></td>
                <td>${esc(a.base_url)}</td>
                <td>${esc(({openai: 'OpenAI', openai_responses: 'OpenAI Responses', anthropic: 'Anthropic'})[a.api_format] || a.api_format)}</td>
                <td>${esc(a.created_at || '')}</td>
                <td>
                    <button class="btn btn--sm" onclick="editAccount(${a.id})">编辑</button>
                    <button class="btn btn--sm" onclick="updateAccountModels(${a.id}, '${esc(a.name)}')">更新模型</button>
                </td>
            </tr>
        `).join('');
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="6" class="td-error">加载失败: ${esc(err.message)}</td></tr>`;
    }
}

async function saveAccount(e) {
    e.preventDefault();
    const form = e.target;
    const data = Object.fromEntries(new FormData(form));
    const id = form.dataset.editId;
    try {
        if (id) {
            await proxyFetch(`/api/proxy/accounts/${id}`, {
                method: 'PUT',
                body: JSON.stringify({
                    name: data.name,
                    upstream_key: data.upstream_key,
                    base_url: data.base_url,
                    api_format: data.api_format,
                    endpoint_path: data.endpoint_path || '',
                    auth_header: data.auth_header || 'auto',
                }),
            });
            showToast('账户已更新');
        } else {
            await proxyFetch('/api/proxy/accounts', {
                method: 'POST',
                body: JSON.stringify(data),
            });
            showToast('账户已创建');
        }
        form.reset();
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
        form['upstream_key'].value = acc.upstream_key;
        form['base_url'].value = acc.base_url;
        form['api_format'].value = acc.api_format || 'openai';
        form['endpoint_path'].value = acc.endpoint_path || '';
        form['auth_header'].value = acc.auth_header || 'auto';
        form.dataset.editId = id;
        form.querySelector('[type=submit]').textContent = '保存';
        document.getElementById('accountDeleteBtn').style.display = '';
        document.getElementById('accountModelBtn').style.display = '';
        document.getElementById('accountDeleteBtn').onclick = () => { closeModal('accountModal'); deleteAccount(id, acc.name); };
        document.getElementById('accountModelBtn').onclick = () => updateAccountModels(id, acc.name);
        openModal('accountModal');
    });
}

async function deleteAccount(id, name) {
    if (!confirm(`确定删除账户 "${name}"？\n\n如果有本地密钥关联此账户，删除将被拒绝。`)) return;
    try {
        await proxyFetch(`/api/proxy/accounts/${id}`, { method: 'DELETE' });
        showToast('账户已删除');
        loadAccountsTable();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

async function updateAccountModels(id, name) {
    const btns = document.querySelectorAll('#accountModelBtn, [onclick*="updateAccountModels"]');
    btns.forEach(b => { b.disabled = true; b.textContent = '获取中...'; });
    try {
        const result = await proxyFetch(`/api/proxy/accounts/${id}/models`, { method: 'POST', body: '{}' });
        showToast(`${name}: 获取到 ${result.count} 个模型`);
    } catch (err) {
        showToast(`获取失败: ${err.message}`, 'error');
    }
    btns.forEach(b => { b.disabled = false; b.textContent = '更新模型'; });
}

function initAccountsPage() {
    const el = document.getElementById('page-proxy-accounts');
    if (!el || el.dataset.initialized) return;
    el.dataset.initialized = '1';
    el.innerHTML = `
        <div class="page-header">
            <h1 class="page-title">上游账户管理</h1>
            <p class="page-subtitle">支持 OpenAI 兼容 / OpenAI Responses / Anthropic 兼容 的上游服务</p>
            <button class="btn btn--primary" onclick="openModal('accountModal')">+ 添加账户</button>
        </div>
        <table class="mgmt-table" id="accountsTable">
            <thead><tr><th>名称</th><th>上游密钥</th><th>Base URL</th><th>API 格式</th><th>创建时间</th><th>操作</th></tr></thead>
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
                    <label>上游 API Key <input name="upstream_key" required></label>
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
                    <div style="display:flex; gap:8px;">
                        <button type="submit" class="btn btn--primary">添加账户</button>
                        <button type="button" class="btn btn--sm" id="accountModelBtn" style="display:none">更新模型</button>
                        <button type="button" class="btn btn--sm" id="accountDeleteBtn" style="display:none; color:#EF4444;">删除账户</button>
                    </div>
                </form>
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
                <td>${esc(k.account_name || `ID:${k.account_id}`)}</td>
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
        accountSel.innerHTML = accounts.map((a) =>
            `<option value="${a.id}" ${a.id === key.account_id ? 'selected' : ''}>${esc(a.name)}</option>`
        ).join('');

        // Load template selector
        const templates = await proxyFetch('/api/proxy/templates');
        const tsel = document.getElementById('keyTemplateSelect');
        tsel.innerHTML = '<option value="">不使用模板</option>' + templates.map(t =>
            `<option value="${t.id}" ${key.template_id === t.id ? 'selected' : ''}>${esc(t.name)} (${t.entries ? t.entries.length : 0} 条)</option>`
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
    const accountId = parseInt(form['account_id'].value);
    const templateId = form['template_id'].value ? parseInt(form['template_id'].value) : null;

    try {
        const data = {};
        if (label) data.label = label;
        data.account_id = accountId;
        data.template_id = templateId;
        await proxyFetch(`/api/proxy/keys/${id}`, { method: 'PUT', body: JSON.stringify(data) });
        showToast('密钥已更新');
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
                    <label>模型映射模板 <select name="template_id" id="keyTemplateSelect"><option value="">不使用模板</option></select></label>
                    <button type="submit" class="btn btn--primary" style="margin-top:12px;">保存</button>
                </form>
            </div>
        </div>
    `;

    loadAccountOptions();
    loadKeysTable();
}

// ── Templates Page ──────────────────────────────────────────────────

async function loadTemplatesTable() {
    const tbody = document.querySelector('#templatesTable tbody');
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="4" class="td-loading">加载中...</td></tr>';
    try {
        const templates = await proxyFetch('/api/proxy/templates');
        if (!templates.length) {
            tbody.innerHTML = '<tr><td colspan="4" class="td-empty">暂无模板，请点击"新建模板"</td></tr>';
            return;
        }
        tbody.innerHTML = templates.map((t) => `
            <tr>
                <td>${esc(t.name)}</td>
                <td>${t.entries ? t.entries.length : 0} 条映射</td>
                <td>${t.entries ? t.entries.map(e => `<code>${esc(e.pattern)} → ${esc(e.upstream_model)}</code>`).join('<br>') : ''}</td>
                <td>
                    <button class="btn btn--sm" onclick="editTemplate(${t.id})">编辑</button>
                    <button class="btn btn--sm" onclick="deleteTemplate(${t.id}, '${esc(t.name)}')" style="color:#EF4444;">删除</button>
                </td>
            </tr>
        `).join('');
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="4" class="td-error">加载失败: ${esc(err.message)}</td></tr>`;
    }
}

async function openTemplateModal(id) {
    document.getElementById('templateForm').dataset.editId = id || '';
    document.getElementById('templateForm').querySelector('[type=submit]').textContent = id ? '保存' : '创建';
    document.getElementById('templateMappings').innerHTML = '';

    if (id) {
        const templates = await proxyFetch('/api/proxy/templates');
        const t = templates.find(t => t.id === id);
        if (!t) return;
        document.getElementById('templateName').value = t.name;
        if (t.entries) {
            document.getElementById('templateMappings').innerHTML = t.entries.map((e, i) => mappingRow(e.pattern, e.upstream_model)).join('');
        }
    }
    openModal('templateModal');
    populateTemplateAcctSelect();
    addMappingRow();
}

function mappingRow(pattern, upstream) {
    return `<div class="map-row" style="display:flex;gap:8px;align-items:center;margin-bottom:6px;">
        <input value="${esc(pattern||'')}" placeholder="正则" style="flex:1;font-size:12px;padding:4px 8px;border:1px solid var(--color-border);border-radius:4px;">
        <span style="color:var(--color-text-tertiary);">→</span>
        <select style="flex:1;font-size:12px;padding:4px 8px;border:1px solid var(--color-border);border-radius:4px;" onfocus="loadUpstreamModels(this)"><option value="${esc(upstream||'')}">${esc(upstream||'点击获取模型')}</option></select>
        <button class="btn btn--sm" onclick="moveMapping(this, 'up')" title="上移">▲</button>
        <button class="btn btn--sm" onclick="moveMapping(this, 'down')" title="下移">▼</button>
        <button class="btn btn--sm" onclick="this.parentElement.remove()" style="color:#EF4444;">✕</button>
    </div>`;
}

function refreshAllModelDropdowns() {
    document.querySelectorAll('#templateMappings select').forEach(s => { s.innerHTML = '<option value="">点击获取模型</option>'; });
}

async function populateTemplateAcctSelect() {
    try {
        const accounts = await proxyFetch('/api/proxy/accounts');
        const sel = document.getElementById('templateAcctSelect');
        if (!sel) return;
        sel.innerHTML = '<option value="">-- 选择账户 --</option>' + accounts.map(a => `<option value="${a.id}">${esc(a.name)}</option>`).join('');
    } catch (e) {}
}

function addMappingRow() {
    const div = document.createElement('div');
    div.innerHTML = mappingRow('', '');
    document.getElementById('templateMappings').appendChild(div.firstElementChild);
}

function moveMapping(btn, dir) {
    const row = btn.parentElement;
    if (dir === 'up' && row.previousElementSibling) {
        row.parentElement.insertBefore(row, row.previousElementSibling);
    } else if (dir === 'down' && row.nextElementSibling) {
        row.parentElement.insertBefore(row.nextElementSibling, row);
    }
}

async function loadUpstreamModels(sel) {
    if (sel.options.length > 1) return;
    const acctId = document.getElementById('templateAcctSelect')?.value;
    if (!acctId) { sel.innerHTML = '<option value="">请先选择上游账户</option>'; return; }
    try {
        const models = await proxyFetch(`/api/proxy/accounts/${acctId}/models`);
        if (!models.length) { sel.innerHTML = '<option value="">该账户暂无模型</option>'; return; }
        const cur = sel.value;
        sel.innerHTML = models.map(m => `<option value="${esc(m)}" ${m===cur?'selected':''}>${esc(m)}</option>`).join('');
    } catch (e) {
        sel.innerHTML = '<option value="">加载失败</option>';
    }
}

async function saveTemplate(e) {
    e.preventDefault();
    const form = e.target;
    const id = form.dataset.editId;
    const name = document.getElementById('templateName').value;
    const rows = document.querySelectorAll('#templateMappings .map-row');
    const entries = [];
    rows.forEach(row => {
        const inputs = row.querySelectorAll('input, select');
        const pattern = inputs[0].value.trim();
        const upstream = inputs[1].value.trim();
        if (pattern && upstream) entries.push({ pattern, upstream_model: upstream });
    });
    try {
        if (id) {
            await proxyFetch(`/api/proxy/templates/${id}`, { method: 'PUT', body: JSON.stringify({ name, entries }) });
            showToast('模板已更新');
        } else {
            await proxyFetch('/api/proxy/templates', { method: 'POST', body: JSON.stringify({ name, entries }) });
            showToast('模板已创建');
        }
        closeModal('templateModal');
        loadTemplatesTable();
    } catch (err) { showToast(err.message, 'error'); }
}

async function deleteTemplate(id, name) {
    if (!confirm(`删除模板 "${name}"？`)) return;
    try {
        await proxyFetch(`/api/proxy/templates/${id}`, { method: 'DELETE' });
        showToast('模板已删除');
        loadTemplatesTable();
    } catch (err) { showToast(err.message, 'error'); }
}

function editTemplate(id) { openTemplateModal(id); }

function initTemplatesPage() {
    const el = document.getElementById('page-proxy-templates');
    if (!el || el.dataset.initialized) return;
    el.dataset.initialized = '1';
    el.innerHTML = `
        <div class="page-header">
            <h1 class="page-title">模型映射模板</h1>
            <p class="page-subtitle">创建可复用的模型映射模板，在密钥管理中引用</p>
            <button class="btn btn--primary" onclick="openTemplateModal()">+ 新建模板</button>
        </div>
        <table class="mgmt-table" id="templatesTable">
            <thead><tr><th>名称</th><th>条目数</th><th>映射预览</th><th>操作</th></tr></thead>
            <tbody></tbody>
        </table>
        <div class="modal-overlay" id="templateModal" style="display:none">
            <div class="modal" style="max-width:640px;">
                <div class="modal__header"><h3>编辑模板</h3><button class="modal__close" onclick="closeModal('templateModal')">&times;</button></div>
                <form id="templateForm" onsubmit="saveTemplate(event)" data-edit-id="">
                    <label>模板名称 <input id="templateName" name="name" required></label>
                    <label>获取模型列表的上游账户 <select id="templateAcctSelect" onchange="refreshAllModelDropdowns()"></select></label>
                    <label>映射条目 <button type="button" class="btn btn--sm" onclick="addMappingRow()">+ 添加</button></label>
                    <div id="templateMappings" style="max-height:350px;overflow-y:auto;"></div>
                    <button type="submit" class="btn btn--primary">创建</button>
                </form>
            </div>
        </div>
    `;
    loadTemplatesTable();
}

// ── Pricing Page ─────────────────────────────────────────────────────────

async function loadPricingTable() {
    const tbody = document.querySelector('#pricingTable tbody');
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="5" class="td-loading">加载中...</td></tr>';
    try {
        const pricing = await proxyFetch('/api/proxy/pricing');
        if (!pricing.length) {
            tbody.innerHTML = '<tr><td colspan="5" class="td-empty">暂无定价</td></tr>';
            return;
        }
        tbody.innerHTML = pricing.map((p) => `
            <tr>
                <td><code>${esc(p.model_pattern)}</code></td>
                <td>¥${p.input_price.toFixed(2)} / 1M tokens</td>
                <td>¥${p.output_price.toFixed(2)} / 1M tokens</td>
                <td>${esc(p.currency)}</td>
                <td>
                    <button class="btn btn--sm" onclick="reorderPricing(${p.id},'up')">▲</button>
                    <button class="btn btn--sm" onclick="reorderPricing(${p.id},'down')">▼</button>
                    <button class="btn btn--sm" onclick="editPricing(${p.id}, '${esc(p.model_pattern)}', ${p.input_price}, ${p.output_price})">编辑</button>
                    <button class="btn btn--sm" onclick="deletePricing(${p.id})">删除</button>
                </td>
            </tr>
        `).join('');
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="5" class="td-error">加载失败: ${esc(err.message)}</td></tr>`;
    }
}

async function savePricing(e) {
    e.preventDefault();
    const form = e.target;
    const data = Object.fromEntries(new FormData(form));
    const id = form.dataset.editId;
    const payload = { model_pattern: data.model_pattern, input_price: parseFloat(data.input_price), output_price: parseFloat(data.output_price) };
    try {
        if (id) {
            await proxyFetch(`/api/proxy/pricing/${id}`, { method: 'PUT', body: JSON.stringify(payload) });
            showToast('定价已更新');
        } else {
            await proxyFetch('/api/proxy/pricing', { method: 'POST', body: JSON.stringify(payload) });
            showToast('定价已添加');
        }
        form.reset();
        form.dataset.editId = '';
        form.querySelector('[type=submit]').textContent = '添加';
        closeModal('pricingModal');
        loadPricingTable();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

function editPricing(id, pattern, inputPrice, outputPrice) {
    const form = document.querySelector('#pricingForm');
    form['model_pattern'].value = pattern;
    form['input_price'].value = inputPrice;
    form['output_price'].value = outputPrice;
    form.dataset.editId = id;
    form.querySelector('[type=submit]').textContent = '更新';
    openModal('pricingModal');
}

async function deletePricing(id) {
    if (!confirm('确定删除此定价条目？')) return;
    try {
        await proxyFetch(`/api/proxy/pricing/${id}`, { method: 'DELETE' });
        showToast('定价已删除');
        loadPricingTable();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

async function reorderPricing(id, dir) {
    try {
        await proxyFetch('/api/proxy/pricing/reorder', { method: 'POST', body: JSON.stringify({ id, direction: dir }) });
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
            <p class="page-subtitle">配置模型价格（百万元 token 价格，人民币）</p>
            <button class="btn btn--primary" onclick="openModal('pricingModal')">+ 添加定价</button>
        </div>
        <table class="mgmt-table" id="pricingTable">
            <thead><tr><th>模型匹配</th><th>输入价格</th><th>输出价格</th><th>货币</th><th>操作</th></tr></thead>
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
                    <label>输入价格 (¥/1M tokens) <input name="input_price" type="number" step="0.01" required></label>
                    <label>输出价格 (¥/1M tokens) <input name="output_price" type="number" step="0.01" required></label>
                    <button type="submit" class="btn btn--primary">添加</button>
                </form>
            </div>
        </div>
    `;
    loadPricingTable();
}
