/** Agent subscriptions and software configuration pages. */

let agentSubscriptions = [];
let agentSoftware = [];
let agentTypes = [];

function agentDate(value) {
    return value ? String(value).slice(0, 10) : '';
}

async function loadAgentSubscriptions() {
    const tbody = document.querySelector('#agentSubscriptionsTable tbody');
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="5" class="td-loading">加载中...</td></tr>';
    try {
        agentSubscriptions = await proxyApi('/api/proxy/agent-subscriptions');
        if (!agentSubscriptions.length) {
            tbody.innerHTML = '<tr><td colspan="5" class="td-empty">暂无订阅，请点击“添加订阅”</td></tr>';
            return;
        }
        tbody.innerHTML = agentSubscriptions.map((item) => `
            <tr>
                <td>${esc(item.name)}</td>
                <td>${(item.instances || []).map((instance) =>
                    `${esc(instance.label)}：${esc(instance.currency || item.currency || 'CNY')} ${(Number(instance.monthly_price || 0)).toFixed(2)} / 月`
                ).join('<br>') || '—'}</td>
                <td>${esc(agentDate(item.valid_from))}</td>
                <td>${esc(fmtLocal(item.updated_at || item.created_at))}</td>
                <td>
                    <button class="btn btn--sm" onclick="editAgentSubscription(${item.id})">编辑</button>
                    <button class="btn btn--sm" style="color:var(--color-danger);" onclick="deleteAgentSubscription(${item.id}, '${esc(item.name)}')">删除</button>
                </td>
            </tr>
        `).join('');
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="5" class="td-error">加载失败: ${esc(err.message)}</td></tr>`;
    }
}

function openAgentSubscriptionModal(item) {
    const form = document.getElementById('agentSubscriptionForm');
    if (!form) return;
    form.reset();
    form.dataset.editId = item ? item.id : '';
    form.name.value = item ? item.name : '';
    form.valid_from.value = item ? agentDate(item.valid_from) : '';
    form.currency.value = item ? (item.currency || 'CNY') : 'CNY';
    const rows = document.getElementById('agentInstanceRows');
    if (rows) {
        rows.innerHTML = '';
        const instances = item && item.instances && item.instances.length
            ? item.instances : [{}];
        instances.forEach(addAgentInstanceRow);
    }
    document.getElementById('agentSubscriptionModalTitle').textContent = item ? '编辑订阅' : '添加订阅';
    document.getElementById('agentSubscriptionSubmit').textContent = item ? '保存' : '添加订阅';
    openModal('agentSubscriptionModal');
}

function addAgentInstanceRow(instance) {
    const rows = document.getElementById('agentInstanceRows');
    if (!rows) return;
    const item = instance || {};
    const row = document.createElement('div');
    row.className = 'agent-instance-row';
    row.dataset.instanceId = item.id || '';
    const rowNumber = rows.querySelectorAll('.agent-instance-row').length + 1;
    const parentStart = document.querySelector('#agentSubscriptionForm [name="valid_from"]');
    row.innerHTML = `
        <input name="instance_label" required placeholder="实例名称" value="${esc(item.label || `实例 ${rowNumber}`)}">
        <input name="instance_valid_from" required type="date" value="${esc(agentDate(item.valid_from) || (parentStart && parentStart.value) || '')}">
        <input name="instance_monthly_price" required type="number" min="0" step="0.01" value="${item.monthly_price == null ? '' : esc(item.monthly_price)}">
        <button type="button" class="btn btn--sm" onclick="this.parentElement.remove()">删除</button>`;
    rows.appendChild(row);
}

function collectAgentInstances(form) {
    const rows = [...document.querySelectorAll('#agentInstanceRows .agent-instance-row')];
    if (!rows.length) throw new Error('至少添加一个订阅实例');
    return rows.map((row) => ({
            id: row.dataset.instanceId || undefined,
            label: row.querySelector('[name="instance_label"]').value.trim(),
            valid_from: row.querySelector('[name="instance_valid_from"]').value,
            monthly_price: row.querySelector('[name="instance_monthly_price"]').value || 0,
        }));
}

function editAgentSubscription(id) {
    const item = agentSubscriptions.find((row) => row.id === id);
    if (item) openAgentSubscriptionModal(item);
}

async function saveAgentSubscription(event) {
    event.preventDefault();
    const form = event.target;
    try {
        const payload = {
            name: form.name.value.trim(),
            valid_from: form.valid_from.value,
            currency: form.currency.value,
            instances: collectAgentInstances(form),
        };
        const id = form.dataset.editId;
        await proxyApi(id ? `/api/proxy/agent-subscriptions/${id}` : '/api/proxy/agent-subscriptions', {
            method: id ? 'PUT' : 'POST',
            body: JSON.stringify(payload),
        });
        closeModal('agentSubscriptionModal');
        ConfigSync.markDirty();
        showToast(id ? '订阅已更新' : '订阅已创建');
        loadAgentSubscriptions();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

async function deleteAgentSubscription(id, name) {
    if (!confirm(`确定删除订阅“${name}”？`)) return;
    try {
        await proxyApi(`/api/proxy/agent-subscriptions/${id}`, { method: 'DELETE' });
        ConfigSync.markDirty();
        showToast('订阅已删除');
        loadAgentSubscriptions();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

async function loadAgentSoftware() {
    const tbody = document.querySelector('#agentSoftwareTable tbody');
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="5" class="td-loading">加载中...</td></tr>';
    try {
        [agentSoftware, agentTypes, agentSubscriptions] = await Promise.all([
            proxyApi('/api/proxy/agent-software'),
            proxyApi('/api/proxy/agent-types'),
            proxyApi('/api/proxy/agent-subscriptions'),
        ]);
        if (!agentSoftware.length) {
            tbody.innerHTML = '<tr><td colspan="5" class="td-empty">暂无软件，请点击“添加软件”</td></tr>';
            return;
        }
        tbody.innerHTML = agentSoftware.map((item) => `
            <tr>
                <td>${esc(item.name)}</td>
                <td>${esc((agentTypes.find((type) => type.kind === item.agent_kind) || {}).label || item.agent_kind)}</td>
                <td><code>${esc((item.config || {}).data_root || (item.config || {}).path || '默认目录')}</code></td>
                <td>${(item.subscription_ids || []).map((id) => {
                    const sub = agentSubscriptions.find((row) => row.id === id);
                    return sub ? esc(sub.name) : `ID:${id}`;
                }).join('、') || '未绑定'}</td>
                <td>
                    <button class="btn btn--sm" onclick="editAgentSoftware(${item.id})">编辑</button>
                    <button class="btn btn--sm" style="color:var(--color-danger);" onclick="deleteAgentSoftware(${item.id}, '${esc(item.name)}')">删除</button>
                </td>
            </tr>
        `).join('');
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="5" class="td-error">加载失败: ${esc(err.message)}</td></tr>`;
    }
}

function renderAgentTypeOptions(selected) {
    const select = document.querySelector('#agentSoftwareForm [name="agent_kind"]');
    if (!select) return;
    select.innerHTML = agentTypes.map((item) =>
        `<option value="${esc(item.kind)}">${esc(item.label || item.kind)}</option>`).join('');
    if (selected) select.value = selected;
}

function openAgentSoftwareModal(item) {
    const form = document.getElementById('agentSoftwareForm');
    if (!form) return;
    form.reset();
    form.dataset.editId = item ? item.id : '';
    renderAgentTypeOptions(item && item.agent_kind);
    form.name.value = item ? item.name : '';
    form.agent_kind.value = item ? item.agent_kind : (agentTypes[0] || {}).kind || '';
    form.data_root.value = item ? ((item.config || {}).data_root || (item.config || {}).path || '') : '';
    renderAgentSubscriptionBindings(item ? (item.subscription_ids || []) : []);
    document.getElementById('agentSoftwareModalTitle').textContent = item ? '编辑软件' : '添加软件';
    document.getElementById('agentSoftwareSubmit').textContent = item ? '保存' : '添加软件';
    openModal('agentSoftwareModal');
}

function renderAgentSubscriptionBindings(selected) {
    const target = document.getElementById('agentSubscriptionBindings');
    if (!target) return;
    target.innerHTML = '';
    if (!agentSubscriptions.length) {
        target.innerHTML = '<span class="muted">暂无订阅，请先在订阅管理中添加</span>';
        return;
    }
    [...new Set((selected || []).map(Number))].forEach((id) => {
        addAgentSubscriptionBindingRow(id);
    });
}

function addAgentSubscriptionBindingRow(subscriptionId = '') {
    const target = document.getElementById('agentSubscriptionBindings');
    if (!target || !agentSubscriptions.length) return;
    const row = document.createElement('div');
    row.className = 'agent-binding-row';
    row.innerHTML = `
        <select name="subscription_ids" required>
            <option value="">选择订阅</option>
            ${agentSubscriptions.map((item) => `<option value="${item.id}">${esc(item.name)}</option>`).join('')}
        </select>
        <button type="button" class="btn btn--sm" style="color:var(--color-danger);">删除</button>`;
    row.querySelector('select').value = subscriptionId || '';
    row.querySelector('button').onclick = () => row.remove();
    target.appendChild(row);
}

function editAgentSoftware(id) {
    const item = agentSoftware.find((row) => row.id === id);
    if (item) openAgentSoftwareModal(item);
}

async function saveAgentSoftware(event) {
    event.preventDefault();
    const form = event.target;
    const root = form.data_root.value.trim();
    const payload = {
        name: form.name.value.trim(),
        agent_kind: form.agent_kind.value,
        config: root ? { data_root: root } : {},
        subscription_ids: [...form.querySelectorAll('select[name="subscription_ids"]')]
            .map((select) => Number(select.value))
            .filter((id, index, values) => id && values.indexOf(id) === index),
    };
    try {
        const id = form.dataset.editId;
        await proxyApi(id ? `/api/proxy/agent-software/${id}` : '/api/proxy/agent-software', {
            method: id ? 'PUT' : 'POST',
            body: JSON.stringify(payload),
        });
        closeModal('agentSoftwareModal');
        ConfigSync.markDirty();
        showToast(id ? '软件已更新' : '软件已创建');
        loadAgentSoftware();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

async function deleteAgentSoftware(id, name) {
    if (!confirm(`确定删除软件“${name}”？`)) return;
    try {
        await proxyApi(`/api/proxy/agent-software/${id}`, { method: 'DELETE' });
        ConfigSync.markDirty();
        showToast('软件已删除');
        loadAgentSoftware();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

function initAgentSubscriptionsPage() {
    const el = document.getElementById('page-agent-subscriptions');
    if (!el || el.dataset.initialized) return;
    el.dataset.initialized = '1';
    el.innerHTML = `
        <div class="page-header">
            <h1 class="page-title">订阅管理</h1>
            <p class="page-subtitle">记录智能体相关订阅。订阅与软件分别维护，可在软件编辑中进行多对多绑定。</p>
            <button class="btn btn--primary" onclick="openAgentSubscriptionModal()">+ 添加订阅</button>
        </div>
        <table class="mgmt-table" id="agentSubscriptionsTable">
            <thead><tr><th>名称</th><th>实例与月费</th><th>开始时间</th><th>更新时间</th><th>操作</th></tr></thead>
            <tbody></tbody>
        </table>
        <div class="modal-overlay" id="agentSubscriptionModal" style="display:none">
            <div class="modal">
                <div class="modal__header"><h3 id="agentSubscriptionModalTitle">添加订阅</h3><button class="modal__close" onclick="closeModal('agentSubscriptionModal')">&times;</button></div>
                <form id="agentSubscriptionForm" onsubmit="saveAgentSubscription(event)" data-edit-id="">
                    <label>名称 <input name="name" required></label>
                    <label>开始时间 <input name="valid_from" type="date" required></label>
                    <label>币种 <select name="currency"><option value="CNY">CNY</option><option value="USD">USD</option></select></label>
                    <div class="agent-instance-list"><div class="form-label">订阅实例</div><div id="agentInstanceRows"></div><button type="button" class="btn btn--sm" onclick="addAgentInstanceRow()">+ 添加实例</button></div>
                    <button id="agentSubscriptionSubmit" class="btn btn--primary" type="submit">添加订阅</button>
                </form>
            </div>
        </div>
    `;
    loadAgentSubscriptions();
}

function initAgentSoftwarePage() {
    const el = document.getElementById('page-agent-software');
    if (!el || el.dataset.initialized) return;
    el.dataset.initialized = '1';
    el.innerHTML = `
        <div class="page-header">
            <h1 class="page-title">软件管理</h1>
            <p class="page-subtitle">配置智能体软件来源。用量类型由后端 adapter registry 提供，支持多种本地 agent。</p>
            <button class="btn btn--primary" onclick="openAgentSoftwareModal()">+ 添加软件</button>
        </div>
        <table class="mgmt-table" id="agentSoftwareTable">
            <thead><tr><th>名称</th><th>类型</th><th>数据目录</th><th>绑定订阅</th><th>操作</th></tr></thead>
            <tbody></tbody>
        </table>
        <div class="modal-overlay" id="agentSoftwareModal" style="display:none">
            <div class="modal">
                <div class="modal__header"><h3 id="agentSoftwareModalTitle">添加软件</h3><button class="modal__close" onclick="closeModal('agentSoftwareModal')">&times;</button></div>
                <form id="agentSoftwareForm" onsubmit="saveAgentSoftware(event)" data-edit-id="">
                    <label>名称 <input name="name" required placeholder="例如：我的 Codex"></label>
                    <label>Agent 类型 <select name="agent_kind" required></select></label>
                    <label>数据目录（可选） <input name="data_root" placeholder="留空使用该类型默认目录"></label>
                    <div class="agent-binding-list-wrap"><div class="form-label">绑定订阅</div><div id="agentSubscriptionBindings"></div><button type="button" class="btn btn--sm" onclick="addAgentSubscriptionBindingRow()">+ 添加订阅</button></div>
                    <button id="agentSoftwareSubmit" class="btn btn--primary" type="submit">添加软件</button>
                </form>
            </div>
        </div>
    `;
    loadAgentSoftware();
}
