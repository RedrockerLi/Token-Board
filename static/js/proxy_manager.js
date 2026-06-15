/**
 * proxy_manager.js — Account, Key, and Pricing management pages.
 *
 * Exports: initAccountsPage(), initKeysPage(), initPricingPage()
 * Lazy-loaded by app.js when navigating to #/proxy/accounts etc.
 */

// ── Shared Helpers ───────────────────────────────────────────────────────

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
    // Simple toast notification
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
                <td>${esc(a.created_at || '')}</td>
                <td>
                    <button class="btn btn--sm" onclick="editAccount(${a.id})">编辑</button>
                    <button class="btn btn--sm" onclick="deleteAccount(${a.id}, '${esc(a.name)}')" style="color:#EF4444;">删除</button>
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
                body: JSON.stringify({ name: data.name, upstream_key: data.upstream_key, base_url: data.base_url }),
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
        form.querySelector('[type=submit]').textContent = '添加账户';
        closeModal('accountModal');
        loadAccountsTable();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

function editAccount(id) {
    // Load account data and populate form
    proxyFetch('/api/proxy/accounts').then((accounts) => {
        const acc = accounts.find((a) => a.id === id);
        if (!acc) return;
        const form = document.querySelector('#accountForm');
        form['name'].value = acc.name;
        form['upstream_key'].value = acc.upstream_key;
        form['base_url'].value = acc.base_url;
        form.dataset.editId = id;
        form.querySelector('[type=submit]').textContent = '更新账户';
        openModal('accountModal');
    });
}

async function deleteAccount(id, name) {
    if (!confirm(`确定删除账户 "${name}"？\\n\\n如果有本地密钥关联此账户，删除将被拒绝。`)) return;
    try {
        await proxyFetch(`/api/proxy/accounts/${id}`, { method: 'DELETE' });
        showToast('账户已删除');
        loadAccountsTable();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

function initAccountsPage() {
    const el = document.getElementById('page-proxy-accounts');
    if (!el || el.dataset.initialized) return;
    el.dataset.initialized = '1';

    el.innerHTML = `
        <div class="page-header">
            <h1 class="page-title">上游账户管理</h1>
            <p class="page-subtitle">仅支持 OpenAI-API-Compatible 的上游服务</p>
            <button class="btn btn--primary" onclick="openModal('accountModal')">+ 添加账户</button>
        </div>
        <table class="mgmt-table" id="accountsTable">
            <thead><tr><th>名称</th><th>上游密钥</th><th>Base URL</th><th>创建时间</th><th>操作</th></tr></thead>
            <tbody></tbody>
        </table>

        <!-- Account Modal -->
        <div class="modal-overlay" id="accountModal" style="display:none">
            <div class="modal">
                <div class="modal__header">
                    <h3>账户信息</h3>
                    <button class="modal__close" onclick="closeModal('accountModal')">&times;</button>
                </div>
                <form id="accountForm" onsubmit="saveAccount(event)" data-edit-id="">
                    <label>名称 <input name="name" required></label>
                    <label>上游 API Key <input name="upstream_key" required></label>
                    <label>Base URL（OpenAI 兼容） <input name="base_url" placeholder="https://api.example.com/v1"></label>
                    <button type="submit" class="btn btn--primary">添加账户</button>
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
    tbody.innerHTML = '<tr><td colspan="7" class="td-loading">加载中...</td></tr>';

    try {
        const keys = await proxyFetch('/api/proxy/keys');
        if (!keys.length) {
            tbody.innerHTML = '<tr><td colspan="7" class="td-empty">暂无密钥，请点击"生成密钥"</td></tr>';
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
                    <button class="btn btn--sm" onclick="editKey(${k.id})">编辑</button>
                    <button class="btn btn--sm" onclick="deleteKey(${k.id}, '${esc(k.label || k.key_masked)}')" style="color:#EF4444;">删除</button>
                </td>
            </tr>
        `).join('');
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="7" class="td-error">加载失败: ${esc(err.message)}</td></tr>`;
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
        // Show the generated key prominently
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
    if (!confirm(`确定删除密钥 "${label}"？\\n\\n此操作不可恢复，使用该密钥的 AI 工具将立即无法连接。`)) return;
    try {
        await proxyFetch(`/api/proxy/keys/${id}`, { method: 'DELETE' });
        showToast('密钥已删除');
        loadKeysTable();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

async function editKey(id) {
    const label = prompt('新标签（留空不变）：');
    if (label === null) return;
    const accId = prompt('新账户 ID（留空不变）：');
    try {
        const data = {};
        if (label) data.label = label;
        if (accId) data.account_id = parseInt(accId);
        await proxyFetch(`/api/proxy/keys/${id}`, {
            method: 'PUT',
            body: JSON.stringify(data),
        });
        showToast('密钥已更新');
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
        sel.innerHTML = accounts
            .map((a) => `<option value="${a.id}">${esc(a.name)}</option>`)
            .join('');
    } catch (err) {
        console.error('Failed to load accounts for key form:', err);
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
        <table class="mgmt-table" id="keysTable">
            <thead><tr><th>密钥</th><th>标签</th><th>关联账户</th><th>最后使用</th><th>创建时间</th><th>操作</th></tr></thead>
            <tbody></tbody>
        </table>

        <!-- Key Generation Modal -->
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
    `;

    loadAccountOptions();
    loadKeysTable();
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

    const payload = {
        model_pattern: data.model_pattern,
        input_price: parseFloat(data.input_price),
        output_price: parseFloat(data.output_price),
    };

    try {
        if (id) {
            await proxyFetch(`/api/proxy/pricing/${id}`, {
                method: 'PUT',
                body: JSON.stringify(payload),
            });
            showToast('定价已更新');
        } else {
            await proxyFetch('/api/proxy/pricing', {
                method: 'POST',
                body: JSON.stringify(payload),
            });
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

function initPricingPage() {
    const el = document.getElementById('page-proxy-pricing');
    if (!el || el.dataset.initialized) return;
    el.dataset.initialized = '1';

    el.innerHTML = `
        <div class="page-header">
            <h1 class="page-title">模型定价管理</h1>
            <p class="page-subtitle">配置模型价格（每千 token 价格，人民币）</p>
            <button class="btn btn--primary" onclick="openModal('pricingModal')">+ 添加定价</button>
        </div>
        <table class="mgmt-table" id="pricingTable">
            <thead><tr><th>模型匹配</th><th>输入价格</th><th>输出价格</th><th>货币</th><th>操作</th></tr></thead>
            <tbody></tbody>
        </table>

        <!-- Pricing Modal -->
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

// ── Modal Helpers ────────────────────────────────────────────────────────

function openModal(id) {
    const modal = document.getElementById(id);
    if (modal) modal.style.display = '';
}

function closeModal(id) {
    const modal = document.getElementById(id);
    if (modal) modal.style.display = 'none';
    // Reset generated key display when closing key modal
    if (id === 'keyModal') {
        const disp = document.getElementById('generatedKeyDisplay');
        if (disp) disp.style.display = 'none';
    }
}

// ── Utilities ────────────────────────────────────────────────────────────

function esc(s) {
    if (s == null) return '';
    return String(s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

function maskKey(key) {
    if (!key) return '***';
    return key.length > 12 ? key.slice(0, 6) + '...' + key.slice(-4) : key.slice(0, 4) + '...';
}
