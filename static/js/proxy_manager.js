/**
 * proxy_manager.js — Account, Aggregate, Key, and Pricing management pages.
 *
 * Exports: initAccountsPage(), initAggregatesPage(), initKeysPage(),
 *          initPricingPage()
 */

// ── Shared Helpers (esc defined in utils.js) ──

// ── Upstream account-type spec (mirror of app/domain/account_types.py) ──
// The backend owns the type semantics; the frontend fetches the spec table
// once and asks it (billing / routable / holds_keys / …) instead of comparing
// type strings.  Adding an upstream type is a backend change only.
let TYPE_SPECS = null;

/** Fetch the account-type spec table once; cached for the session. */
async function ensureTypeSpecs() {
    if (TYPE_SPECS) return TYPE_SPECS;
    TYPE_SPECS = await proxyApi('/api/proxy/account-types');
    return TYPE_SPECS;
}

/** Spec for a type string; unknown/empty falls back to api. */
function typeSpec(type) {
    return (TYPE_SPECS && TYPE_SPECS[type]) || (TYPE_SPECS && TYPE_SPECS.api)
        || { billing: 'usage', routable: true, holds_keys: true,
             usage_source: 'proxy', deletion: 'immediate', cooldown: 'transient',
             subscription_unit: null, label: '', short_label: '' };
}

/** Render the account-type <select> options from the spec (labels). */
function _populateTypeOptions(select) {
    if (!select || !TYPE_SPECS) return;
    const current = select.value;
    select.innerHTML = Object.entries(TYPE_SPECS)
        .map(([t, s]) => `<option value="${esc(t)}">${esc(s.label)}</option>`)
        .join('');
    // Preserve the previously-selected type if it exists in the spec.
    if (TYPE_SPECS[current]) select.value = current;
}

function showToast(msg, type = 'success') {
    const toast = document.createElement('div');
    toast.className = `toast toast--${type}`;
    toast.textContent = msg;
    // Visuals live in dashboard.css (.toast / .toast--success / .toast--error).
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
    if (!modal) return;
    modal.style.display = '';
    modal.setAttribute('aria-hidden', 'false');
    document.body.classList.add('modal-open');
}

function closeModal(id) {
    const modal = document.getElementById(id);
    if (modal) {
        modal.style.display = 'none';
        modal.setAttribute('aria-hidden', 'true');
    }
    if (!document.querySelector('.modal-overlay[style=""]:not([style*="display: none"])')) {
        document.body.classList.remove('modal-open');
    }
    if (id === 'keyModal') {
        const disp = document.getElementById('generatedKeyDisplay');
        if (disp) disp.style.display = 'none';
    }
}

// ── Accounts Page ────────────────────────────────────────────────────────

/** Soft pastel pill for the account type column (label from the spec). */
function accountTypeBadge(type) {
    const s = typeSpec(type);
    return `<span class="badge badge--type-${esc(type) || 'api'}">${esc(s.short_label || type || 'API')}</span>`;
}

/** Native currency symbol for proxy plan subscription prices. */
function currencySymbol(currency) {
    return currency === 'USD' ? '$' : '¥';
}

/** Accounts that can actually be *used as an upstream* (local-key binding,
 * aggregate targets).  Non-routable types never appear in these
 * "use as upstream" pickers. */
function routableAccounts(accounts) {
    return (accounts || []).filter(a => !a.is_aggregate && typeSpec(a.account_type).routable);
}

/** Local-key binding accepts aggregates too: a key bound to an aggregate
 * resolves its upstream by model at request time.  The aggregate editor's
 * target picker still uses routableAccounts() (aggregates cannot nest). */
function keyBindingAccounts(accounts) {
    return (accounts || []).filter(a => typeSpec(a.account_type).routable);
}

/// Show/hide account-form fields based on the selected account type.
/// Mirrors the backend type spec (app/domain/account_types.py):
///   billing 'subscription'  → 订阅月费 + 币种
///   routable                → Base URL / API 格式 / 上游路径 / 认证方式 / 并发限额
///   holds_keys              → 上游密钥区
///   subscription_unit 'per_key'     → 每把密钥行的订阅起始日（plan）
function toggleTypeFields(sel) {
    const s = sel && typeSpec(sel.value);
    const showSub = s && s.billing === 'subscription';
    const price = document.getElementById('planPriceField');
    const cur = document.getElementById('currencyField');
    if (price) price.style.display = showSub ? '' : 'none';
    if (cur) cur.style.display = showSub ? '' : 'none';
    const routing = document.getElementById('routingFields');
    if (routing) routing.style.display = (s && s.routable) ? '' : 'none';
    const keySection = document.getElementById('accountKeySection');
    if (keySection) keySection.style.display = (s && s.holds_keys) ? '' : 'none';
    updatePlanPriceSymbol();
    applyKeyDateVisibility();
}

/// Current display value for a per-key 订阅起始日 date picker.
function keyDateDisplay() {
    const sel = document.querySelector('#accountForm [name="account_type"]');
    return sel && typeSpec(sel.value).subscription_unit === 'per_key' ? '' : 'none';
}

/// Refresh every key-row date picker's visibility after a type switch or an
/// addKeyRow / addExistingKeyRow insert.
function applyKeyDateVisibility() {
    document.querySelectorAll('#keyList .key-valid-from')
        .forEach(el => { el.style.display = keyDateDisplay(); });
}

/// Refresh the price-label currency symbol from the chosen currency select.
function updatePlanPriceSymbol() {
    const sym = document.getElementById('planPriceSymbol');
    const curSel = document.querySelector('#accountForm [name="currency"]');
    if (sym && curSel) sym.textContent = currencySymbol(curSel.value);
}

// ── Multi-key form helpers ─────────────────────────────────────────────
// Editing shows existing keys as masked keep-rows (id + masked value, never
// the secret) plus editable rows for NEW keys. `_keyRowsEdited` tracks whether
// the user touched the key section so a rename-only save never wipes keys.
let _keyRowsEdited = false;

function addKeyRow(value, validFrom) {
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
    const date = document.createElement('input');
    date.type = 'date';
    date.className = 'key-valid-from';
    date.style.display = keyDateDisplay();
    date.name = 'upstream_valid_froms[]';
    date.title = '订阅起始日（留空=创建日期）';
    date.value = validFrom ? utcDateToLocalDate(validFrom) : '';
    date.addEventListener('change', () => { _keyRowsEdited = true; });
    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'btn btn--sm';
    del.textContent = '删除';
    del.onclick = () => { _keyRowsEdited = true; div.remove(); };
    div.appendChild(input);
    div.appendChild(date);
    div.appendChild(del);
    list.appendChild(div);
}

function addExistingKeyRow(keepId, masked, validFrom, pendingDeletion) {
    const list = document.getElementById('keyList');
    if (!list) return;
    const div = document.createElement('div');
    div.style.cssText = 'display:flex; gap:6px; margin:6px 0; align-items:center;';
    div.className = 'existing-key';
    div.dataset.keepId = keepId;
    const span = document.createElement('span');
    span.style.cssText = 'flex:1; font-family:monospace; background:var(--color-surface-sunken, #eeebe5); padding:4px 8px; border-radius:4px; color:var(--color-text-secondary, #716b65);';
    span.textContent = masked + (pendingDeletion
        ? '（已安排本期期末自动删除）'
        : '（已配置，如需删除点“移除”）');
    const date = document.createElement('input');
    date.type = 'date';
    date.className = 'existing-key-valid-from key-valid-from';
    date.style.display = keyDateDisplay();
    date.title = '订阅起始日（留空=创建日期）';
    date.value = validFrom ? utcDateToLocalDate(validFrom) : '';
    date.addEventListener('change', () => { _keyRowsEdited = true; });
    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'btn btn--sm';
    if (pendingDeletion) {
        del.textContent = '下周期自动删除';
        del.disabled = true;
        del.style.opacity = '0.5';
        del.title = '该密钥已安排本期期末自动删除，无需手动移除';
    } else {
        del.textContent = '移除';
        del.onclick = () => { _keyRowsEdited = true; div.remove(); };
    }
    div.appendChild(span);
    div.appendChild(date);
    div.appendChild(del);
    list.appendChild(div);
}

function resetKeyList() {
    const list = document.getElementById('keyList');
    if (list) list.innerHTML = '';
    const cloud = document.getElementById('cloudKeyList');
    if (cloud) cloud.innerHTML = '';
    _keyRowsEdited = false;
}

/// 渲染一条 cloud-only 密钥的补填行：输入框（占位显示云端 mask 版本）+ 确定。
/// 用户填入明文点「确定」→ POST 到 /cloud-keys 变成本地 key。
function addCloudKeyRow(accountId, ck) {
    const list = document.getElementById('cloudKeyList');
    if (!list) return;
    const div = document.createElement('div');
    div.style.cssText = 'display:flex; gap:6px; margin:6px 0; align-items:center;';
    const input = document.createElement('input');
    input.type = 'text';
    input.placeholder = ck.masked + '（云端密钥，请输入明文）';
    input.style.flex = '1';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn btn--sm';
    btn.textContent = '确定';
    btn.onclick = () => confirmCloudKey(accountId, ck.masked, input, btn);
    div.appendChild(input);
    div.appendChild(btn);
    list.appendChild(div);
}

async function confirmCloudKey(accountId, masked, input, btn) {
    const keyValue = input.value.trim();
    if (!keyValue) {
        showToast('请输入该云端密钥的明文', 'error');
        return;
    }
    btn.disabled = true;
    btn.textContent = '保存中...';
    try {
        await proxyApi(`/api/proxy/accounts/${accountId}/cloud-keys`, {
            method: 'POST',
            body: JSON.stringify({ masked, key_value: keyValue }),
        });
        showToast('密钥已配置，可正常计费/路由');
        ConfigSync.markDirty();
        // 重载编辑弹窗：新 key 进入「已配置」列表，云端补填行消失。
        editAccount(accountId);
    } catch (err) {
        showToast(err.message, 'error');
        btn.disabled = false;
        btn.textContent = '确定';
    }
}

function collectKeyRows(form) {
    const type = form['account_type'] ? form['account_type'].value : 'api';
    // 仅 per_key 订阅类型（plan）携带每把密钥的订阅起始日，避免隐藏的旧日期
    // 输入值被误提交到非订阅类型。
    const useKeyDates = typeSpec(type).subscription_unit === 'per_key';
    const keyInputs = [...form.querySelectorAll('#keyList input[name="upstream_keys[]"]')];
    const upstream_keys = [];
    const new_valid_froms = [];
    keyInputs.forEach((input) => {
        if (!input.value.trim()) return;
        upstream_keys.push(input.value.trim());
        new_valid_froms.push(useKeyDates
            ? (localDateToUtcDate(input.parentElement.querySelector('[name="upstream_valid_froms[]"]').value) || null)
            : null);
    });
    const keepRows = [...form.querySelectorAll('#keyList .existing-key')];
    const keep_key_ids = keepRows.map(r => r.dataset.keepId);
    const keep_valid_froms = Object.fromEntries(keepRows.map(r => [
        r.dataset.keepId,
        useKeyDates
            ? (localDateToUtcDate(r.querySelector('.existing-key-valid-from').value) || null)
            : null,
    ]));
    return { upstream_keys, new_valid_froms, keep_key_ids, keep_valid_froms };
}

async function loadAccountsTable() {
    const tbody = document.querySelector('#accountsTable tbody');
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="8" class="td-loading">加载中...</td></tr>';
    try {
        await ensureTypeSpecs();
        const accounts = await proxyApi('/api/proxy/accounts');
        const real = accounts.filter(a => !a.is_aggregate);
        if (!real.length) {
            tbody.innerHTML = '<tr><td colspan="8" class="td-empty">暂无账户，请点击"添加账户"（聚合账户请到"上游账户聚合"管理）</td></tr>';
            return;
        }
        tbody.innerHTML = real.map((a) => {
            const keyEntries = [...(a.keys || []), ...(a.cloud_keys || [])];
            const uniqueMasks = [...new Set(keyEntries.map((key) => key.masked).filter(Boolean))];
            const keyCount = Number(a.key_count || uniqueMasks.length || 0);
            const firstMask = uniqueMasks[0] || '';
            return `
            <tr>
                <td>${esc(a.name)}${a.deleted_at ? ` <span class="badge" style="color:#8F5C2D;background:#FBF1DF;border-color:#DFBF86;" title="到期删除：${esc(fmtLocal(a.deleted_at))}">到期 ${esc(fmtLocal(a.deleted_at).slice(0, 10))}</span>` : ''}</td>
                <td><code>${esc(firstMask)}</code>${keyCount > 1 ? ` <span class="badge" title="${keyCount} 把密钥（同一配置的多个并发槽位）">×${keyCount}</span>` : ''}${!firstMask && !keyCount ? ' <span class="badge" style="color:#8F5C2D;background:#FBF1DF;border-color:#DFBF86;" title="本机未配置上游 Key。云端同步来的账户需在本机填入 Key 才能转发请求">未配置 Key</span>' : ''}</td>
                <td>${esc(a.base_url)}</td>
                <td>${esc(({openai: 'OpenAI', openai_responses: 'OpenAI Responses', anthropic: 'Anthropic'})[a.api_format] || a.api_format)}</td>
                <td>${accountTypeBadge(a.account_type)}</td>
                <td>${typeSpec(a.account_type).billing === 'subscription'
                    ? currencySymbol(a.currency) + (+(a.monthly_price || 0)).toFixed(2) + (typeSpec(a.account_type).subscription_unit === 'per_key' ? '/密钥·周期' : '/周期')
                    : '-'}</td>
                <td>${a.max_concurrency ? a.max_concurrency + ' 并发' : '无限制'}</td>
                <td>
                    <button class="btn btn--sm" onclick="editAccount(${a.id})">编辑</button>
                    <button class="btn btn--sm" onclick="updateAccountModels(${a.id}, '${esc(a.name)}')">更新模型</button>
                    ${typeSpec(a.account_type).holds_keys
                        ? (a.max_concurrency
                            ? `<button class="btn btn--sm" onclick="testConcurrency(${a.id}, '${esc(a.name)}')" title="按当前并发限额向真实上游并发测试">测试并发</button>`
                            : `<button class="btn btn--sm" disabled title="该账户无并发限额（无限制），无需测试">测试并发</button>`)
                        : `<button class="btn btn--sm" disabled title="该账户类型无直连上游密钥，不支持并发测试">测试并发</button>`}
                </td>
            </tr>
        `;
        }).join('');
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="8" class="td-error">加载失败: ${esc(err.message)}</td></tr>`;
    }
}

async function saveAccount(e) {
    e.preventDefault();
    const form = e.target;
    const id = form.dataset.editId;
    const type = form['account_type'].value || 'api';
    const s = typeSpec(type);
    const { upstream_keys, new_valid_froms, keep_key_ids, keep_valid_froms } = collectKeyRows(form);
    // 按类型组装 payload：非路由类型不带 Base URL 等路由字段。
    const common = {
        name: form['name'].value,
        account_type: type,
        monthly_price: form['monthly_price'] ? (form['monthly_price'].value || 0) : 0,
        currency: form['currency'] ? form['currency'].value : 'CNY',
    };
    if (s.routable) {
        common.base_url = form['base_url'].value;
        common.api_format = form['api_format'].value;
        common.endpoint_path = form['endpoint_path'].value || '';
        common.auth_header = form['auth_header'].value || 'auto';
        common.max_concurrency = form['max_concurrency'].value || null;
    }
    try {
        if (id) {
            const payload = { ...common };
            // Only send the key set when the user actually edited it — a
            // rename-only save must never wipe the existing keys.
            if (_keyRowsEdited) {
                payload.upstream_keys = upstream_keys;
                payload.new_valid_froms = new_valid_froms;
                payload.keep_key_ids = keep_key_ids;
                payload.keep_valid_froms = keep_valid_froms;
                payload.keys_edited = true;
            }
            await proxyApi(`/api/proxy/accounts/${id}`, {
                method: 'PUT',
                body: JSON.stringify(payload),
            });
            showToast('账户已更新');
            ConfigSync.markDirty();
        } else {
            await proxyApi('/api/proxy/accounts', {
                method: 'POST',
                body: JSON.stringify({
                    ...common,
                    upstream_keys,
                    new_valid_froms,
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
        document.getElementById('accountTestConcBtn').style.display = 'none';
        form.querySelector('[type=submit]').textContent = '添加账户';
        closeModal('accountModal');
        loadAccountsTable();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

async function editAccount(id) {
    try {
        await ensureTypeSpecs();
        const accounts = await proxyApi('/api/proxy/accounts');
        const acc = accounts.find((a) => a.id === id);
        if (!acc) return;
        const form = document.querySelector('#accountForm');
        _populateTypeOptions(form['account_type']);
        form['name'].value = acc.name;
        form['base_url'].value = acc.base_url;
        form['api_format'].value = acc.api_format || 'openai';
        form['endpoint_path'].value = acc.endpoint_path || '';
        form['auth_header'].value = acc.auth_header || 'auto';
        form['account_type'].value = acc.account_type || 'api';
        form['monthly_price'].value = typeSpec(acc.account_type).billing === 'subscription' ? (acc.monthly_price || 0) : '';
        if (form['currency']) form['currency'].value = acc.currency || 'CNY';
        form['max_concurrency'].value = acc.max_concurrency || '';
        toggleTypeFields(form['account_type']);
        // Existing keys → masked keep-rows; user adds new rows as needed.
        resetKeyList();
        // Existing keys → masked keep-rows; user adds new rows as needed.
        // 已安排本期期末删除的 key（deleted_at 在未来）移除键灰置。
        resetKeyList();
        const nowUtc = new Date().toISOString();
        (acc.keys || []).forEach((k) => {
            addExistingKeyRow(k.id, k.masked, k.valid_from,
                              !!(k.deleted_at && k.deleted_at > nowUtc));
        });
        // cloud-only 密钥（云端有、本机无明文）→ 补填明文的输入框。
        (acc.cloud_keys || []).forEach((ck) => addCloudKeyRow(id, ck));
        form.dataset.editId = id;
        form.querySelector('[type=submit]').textContent = '保存';
        document.getElementById('accountDeleteBtn').style.display = '';
        // Types without upstream keys have no real upstream → hide the model /
        // concurrency buttons (the key section is handled by type semantics).
        const holdsKeys = typeSpec(acc.account_type).holds_keys;
        document.getElementById('accountModelBtn').style.display = holdsKeys ? '' : 'none';
        document.getElementById('accountTestConcBtn').style.display = holdsKeys ? '' : 'none';
        document.getElementById('accountDeleteBtn').onclick = () => { closeModal('accountModal'); deleteAccount(id, acc.name, acc.account_type); };
        document.getElementById('accountModelBtn').onclick = () => updateAccountModels(id, acc.name);
        document.getElementById('accountTestConcBtn').onclick = () => testConcurrency(id, acc.name, true);
        openModal('accountModal');
    } catch (err) {
        showToast(err.message, 'error');
    }
}

let _deleteAccountPendingId = null;
let _deleteAccountPendingSub = false;

/** Return a hint showing the configured default deletion operation for
 *  subscription accounts (usage-billed types are always immediate). */
async function _deletionOpNote(accountType) {
    if (typeSpec(accountType).deletion !== 'configurable') return '';
    try {
        const cfg = await proxyApi('/api/proxy/billing-config');
        return cfg.cancellation_mode === 'end_of_period'
            ? '\n\n默认操作：到期立即删除 —— 可继续使用至本期最后一天（本期计费，下期不计费）。'
            : '\n\n默认操作：本期立即删除 —— 删除即刻生效，本期仍计费。';
    } catch (_) {
        return '';
    }
}

async function deleteAccount(id, name, accountType) {
    await ensureTypeSpecs();
    const isSubscription = typeSpec(accountType).billing === 'subscription';
    const opNote = await _deletionOpNote(accountType);
    // Count local keys bound to this account.
    let keys = [];
    try { keys = await proxyApi('/api/proxy/keys'); } catch (_) {}
    const bound = keys.filter((k) => k.account_id === id).length;

    if (bound === 0) {
        if (!confirm(`确定删除账户 "${name}"？${opNote}`)) return;
        await _doDeleteAccount(id, 'detach', isSubscription);
        return;
    }
    // Has bound keys → let the user choose cascade vs detach.
    _deleteAccountPendingId = id;
    _deleteAccountPendingSub = isSubscription;
    document.getElementById('deleteAccountMsg').textContent =
        `账户 "${name}" 有 ${bound} 个关联本地密钥，选择删除方式：${opNote}`;
    openModal('deleteAccountModal');
}

async function _doDeleteAccount(id, mode, isSubscription) {
    try {
        const resp = await proxyApi(`/api/proxy/accounts/${id}?mode=${mode}`, { method: 'DELETE' });
        if (resp && resp.deferred) {
            const until = resp.effective_deleted_at
                ? fmtLocal(resp.effective_deleted_at).slice(0, 10) : '';
            showToast(`已安排到期删除，可继续使用至 ${until}（本期仍计费，下期不计费）`);
        } else if (isSubscription) {
            showToast('账户已删除（本期已计费）');
        } else {
            showToast('账户已删除');
        }
        ConfigSync.markDirty();
        closeModal('deleteAccountModal');
        loadAccountsTable();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

function deleteAccountCascade() {
    if (_deleteAccountPendingId != null) _doDeleteAccount(_deleteAccountPendingId, 'cascade', _deleteAccountPendingSub);
}

function deleteAccountDetach() {
    if (_deleteAccountPendingId != null) _doDeleteAccount(_deleteAccountPendingId, 'detach', _deleteAccountPendingSub);
}

async function updateAccountModels(id, name) {
    const btns = document.querySelectorAll('#accountModelBtn, [onclick*="updateAccountModels"]');
    btns.forEach(b => { b.disabled = true; b.textContent = '获取中...'; });
    try {
        const result = await proxyApi(`/api/proxy/accounts/${id}/models`, { method: 'POST', body: '{}' });
        showToast(`${name}: 获取到 ${result.count} 个模型`);
        ConfigSync.markDirty();
    } catch (err) {
        showToast(`获取失败: ${err.message}`, 'error');
    }
    btns.forEach(b => { b.disabled = false; b.textContent = '更新模型'; });
}

async function testConcurrency(id, name, useFormValue) {
    // useFormValue=true 时来自编辑弹窗：测「尚未保存」的输入值，否则用已保存的限额。
    const btns = document.querySelectorAll('[onclick*="testConcurrency"], #accountTestConcBtn');
    btns.forEach(b => { b.disabled = true; b.textContent = '测试中...'; });
    let body = '{}';
    if (useFormValue) {
        const input = document.querySelector('#accountForm [name="max_concurrency"]');
        const val = parseInt(input && input.value, 10);
        if (!val || val < 1) {
            showToast('请先填写有效的并发限额数值', 'error');
            btns.forEach(b => { b.disabled = false; b.textContent = '测试并发'; });
            return;
        }
        body = JSON.stringify({ concurrency: val });
    }
    try {
        const result = await proxyApi(`/api/proxy/accounts/${id}/test-concurrency`, { method: 'POST', body });
        showToast(result.message, result.succeeded === result.concurrency ? 'success' : 'error');
    } catch (err) {
        showToast(err.message, 'error');
    }
    btns.forEach(b => { b.disabled = false; b.textContent = '测试并发'; });
}

async function openAddAccountModal() {
    try {
        await ensureTypeSpecs();
    } catch (err) {
        showToast(`无法加载账户类型定义: ${err.message}`, 'error');
        return;
    }
    const form = document.querySelector('#accountForm');
    if (form) {
        _populateTypeOptions(form['account_type']);
        form.reset();
        form.dataset.editId = '';
        // Field visibility follows the selected type (default api); the key
        // section is toggled by toggleTypeFields via holds_keys.
        toggleTypeFields(form['account_type']);
    }
    resetKeyList();
    const delBtn = document.getElementById('accountDeleteBtn');
    const modelBtn = document.getElementById('accountModelBtn');
    const testConcBtn = document.getElementById('accountTestConcBtn');
    if (delBtn) delBtn.style.display = 'none';
    if (modelBtn) modelBtn.style.display = 'none';
    if (testConcBtn) testConcBtn.style.display = 'none';
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
            <thead><tr><th>名称</th><th>上游密钥</th><th>Base URL</th><th>API 格式</th><th>类型</th><th>订阅月费</th><th>并发限额</th><th>操作</th></tr></thead>
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
                    <div id="accountKeySection" style="margin-bottom:10px;">
                        <label>上游 API Key（多把密钥 = 同一配置的多个槽位；仅本机保存，不上传云端）</label>
                        <div id="keyList" style="margin:4px 0;"></div>
                        <div id="cloudKeyList" style="margin:4px 0;"></div>
                        <button type="button" class="btn btn--sm" onclick="addKeyRow()">+ 添加密钥</button>
                    </div>
                    <div id="routingFields">
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
                        <label>并发限额（可选，留空 = 无限制）
                            <input name="max_concurrency" type="number" step="1" min="1" placeholder="如 3">
                        </label>
                    </div>
                    <label>账户类型
                        <!-- options rendered from /api/proxy/account-types by
                             _populateTypeOptions() on modal open -->
                        <select name="account_type" onchange="toggleTypeFields(this)"></select>
                    </label>
                    <label id="planPriceField" style="display:none;"><span>订阅月费 (<span id="planPriceSymbol">¥</span>/周期)</span>
                        <input name="monthly_price" type="number" step="0.01" min="0" placeholder="如 99">
                    </label>
                    <label id="currencyField" style="display:none;">订阅币种
                        <select name="currency" onchange="updatePlanPriceSymbol()">
                            <option value="CNY">CNY</option>
                            <option value="USD">USD</option>
                        </select>
                    </label>
                    <div style="display:flex; gap:8px;">
                        <button type="submit" class="btn btn--primary">添加账户</button>
                        <button type="button" class="btn btn--sm" id="accountModelBtn" style="display:none">更新模型</button>
                        <button type="button" class="btn btn--sm" id="accountTestConcBtn" style="display:none" title="按当前输入的并发限额测试（无需先保存）">测试并发</button>
                        <button type="button" class="btn btn--sm" id="accountDeleteBtn" style="display:none; color:var(--color-danger);">删除账户</button>
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
                    <button class="btn" style="color:var(--color-danger);" onclick="deleteAccountDetach()">仅解绑密钥（密钥需重新分配）</button>
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
        const keys = await proxyApi('/api/proxy/keys');
        if (!keys.length) {
            tbody.innerHTML = '<tr><td colspan="6" class="td-empty">暂无密钥，请点击"生成密钥"</td></tr>';
            return;
        }
        tbody.innerHTML = keys.map((k) => `
            <tr>
                <td><code class="key-display" title="${esc(k.key_value)}">${esc(k.key_masked)}</code> <button class="btn btn--sm" onclick="copyKey('${esc(k.key_value)}')">复制</button></td>
                <td>${esc(k.label || '-')}</td>
                <td>${k.account_id == null ? '<span style="color:var(--color-text-tertiary);">未分配</span>' : esc(k.account_name || `ID:${k.account_id}`)}</td>
                <td>${k.last_used_at ? esc(fmtLocal(k.last_used_at)) : '从未使用'}</td>
                <td>${k.created_at ? esc(fmtLocal(k.created_at)) : ''}</td>
                <td>
                    <button class="btn btn--sm" onclick="openEditKeyModal(${k.id})">编辑</button>
                    <button class="btn btn--sm" onclick="deleteKey(${k.id}, '${esc(k.label || k.key_masked)}')" style="color:var(--color-danger);">删除</button>
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
        const result = await proxyApi('/api/proxy/keys', {
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
        await proxyApi(`/api/proxy/keys/${id}`, { method: 'DELETE' });
        showToast('密钥已删除');
        ConfigSync.markDirty();
        loadKeysTable();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

async function openEditKeyModal(id) {
    try {
        await ensureTypeSpecs();
        const [keys, accounts] = await Promise.all([
            proxyApi('/api/proxy/keys'),
            proxyApi('/api/proxy/accounts'),
        ]);
        const key = keys.find((k) => k.id === id);
        if (!key) return;

        // Populate form
        document.getElementById('editKeyLabel').value = key.label || '';
        const accountSel = document.getElementById('editKeyAccount');
        accountSel.innerHTML =
            `<option value="" ${key.account_id == null ? 'selected' : ''}>未分配</option>` +
            keyBindingAccounts(accounts).map((a) =>
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
        await proxyApi(`/api/proxy/keys/${id}`, { method: 'PUT', body: JSON.stringify(data) });
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
        await ensureTypeSpecs();
        const accounts = await proxyApi('/api/proxy/accounts');
        const sel = document.getElementById('keyAccountSelect');
        if (!sel) return;
        sel.innerHTML = keyBindingAccounts(accounts)
            .map((a) => `<option value="${a.id}">${esc(a.name)}</option>`).join('');
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
        <div style="margin-bottom:16px; padding:12px 16px; background:var(--color-surface, #fffdfa); border:1px solid var(--color-border); border-radius:8px; font-size:13px; color:var(--color-text-tertiary);">
            <strong style="color:var(--color-text-secondary);">配置说明</strong>
            <div style="margin-top:6px;">一把密钥同时支持三种客户端格式，代理根据请求 URL 自动识别并转换为上游格式</div>
            <div style="margin-top:6px;"><code style="background:var(--color-bg, #f1ece5); padding:2px 6px; border-radius:4px;">BASE_URL = http://localhost:8800/v1</code></div>
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
                <div id="generatedKeyDisplay" style="display:none; margin-top:16px; padding:12px; background:#edf5ea; border-radius:8px;">
                    <strong style="color:#4f7b55">新密钥（仅显示一次）：</strong>
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
        const aggregates = await proxyApi('/api/proxy/aggregates');
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
                    <button class="btn btn--sm" onclick="deleteAggregate(${a.id}, '${esc(a.name)}')" style="color:var(--color-danger);">删除</button>
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
        <button type="button" class="btn btn--sm" onclick="this.parentElement.remove()" style="color:var(--color-danger);">✕</button>
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
        const models = await proxyApi(`/api/proxy/accounts/${acctId}/models`);
        if (!models.length) { sel.innerHTML = '<option value="">该账户暂无模型</option>'; return; }
        const cur = sel.value;
        sel.innerHTML = models.map(m => `<option value="${esc(m)}" ${m===cur?'selected':''}>${esc(m)}</option>`).join('');
    } catch (e) {
        sel.innerHTML = '<option value="">加载失败</option>';
    }
}

async function loadAggAccountCache() {
    if (aggAccountsCache) return;
    await ensureTypeSpecs();
    const accounts = await proxyApi('/api/proxy/accounts');
    aggAccountsCache = routableAccounts(accounts);
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
        const aggregates = await proxyApi('/api/proxy/aggregates');
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
            await proxyApi(`/api/proxy/aggregates/${id}`, { method: 'PUT', body: JSON.stringify({ name, entries }) });
            showToast('聚合账户已更新');
            ConfigSync.markDirty();
        } else {
            await proxyApi('/api/proxy/aggregates', { method: 'POST', body: JSON.stringify({ name, entries }) });
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
        await proxyApi(`/api/proxy/aggregates/${id}`, { method: 'DELETE' });
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

// Cached pricing rows (with slots and input-length tiers) so the edit modal
// can look up the complete current configuration by id.
let _pricingCache = [];
let _pricingOrderSavePromise = null;
let _pricingDragState = null;
const PRICING_DRAG_THRESHOLD = 6;

// ── Time-slot helpers (UTC storage ↔ browser-local UI) ──────────────────
// Slot boundaries are stored as UTC minute-of-day; the UI enters and shows
// them in the computer's local timezone.
function minutesToHHMM(m) {
    m = normalizeMinute(m);
    return String(Math.floor(m / 60)).padStart(2, '0') + ':' + String(m % 60).padStart(2, '0');
}
function hhmmToMinutes(hhmm) {
    var p = String(hhmm || '').split(':').map(Number);
    if (p.length < 2 || isNaN(p[0]) || isNaN(p[1])) return NaN;
    return p[0] * 60 + p[1];
}
function addSlotRow(slot) {
    var rows = document.getElementById('slotRows');
    if (!rows) return;
    var div = document.createElement('div');
    div.className = 'slot-row';
    div.style.cssText = 'display:flex;gap:6px;align-items:center;margin-bottom:6px;';
    var startVal = slot ? minutesToHHMM(utcMinuteToLocal(slot.start_minute)) : '08:00';
    var endVal = slot ? minutesToHHMM(utcMinuteToLocal(slot.end_minute)) : '23:00';
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
            start_minute: localMinuteToUtc(start),
            end_minute: localMinuteToUtc(end),
            multiplier: mult,
        });
    });
    return slots;
}

// ── Input-length tier helpers (decimal K/M UI ↔ integer token API) ──────
const LENGTH_UNIT_FACTORS = { K: 1000, M: 1000000 };

function lengthTierPartsFromTokens(tokens) {
    const n = Number(tokens);
    const unit = n >= LENGTH_UNIT_FACTORS.M ? 'M' : 'K';
    const value = n / LENGTH_UNIT_FACTORS[unit];
    return {
        value: Number.isInteger(value) ? String(value) : String(Number(value.toFixed(6))),
        unit,
    };
}

function lengthTierTokens(value, unit) {
    const number = Number(value);
    const factor = LENGTH_UNIT_FACTORS[unit];
    if (!Number.isFinite(number) || number <= 0 || !factor) {
        throw new Error('输入长度门槛必须是大于 0 的数字');
    }
    const tokens = number * factor;
    const rounded = Math.round(tokens);
    if (!Number.isSafeInteger(rounded) || Math.abs(tokens - rounded) > 1e-6) {
        throw new Error('输入长度门槛必须能精确换算为整数 token');
    }
    return rounded;
}

function addLengthTierRow(tier) {
    const rows = document.getElementById('lengthTierRows');
    if (!rows) return;
    const parts = tier
        ? lengthTierPartsFromTokens(tier.threshold_tokens)
        : { value: '128', unit: 'K' };
    const priceValue = function (name) {
        return tier && tier[name] != null ? tier[name] : '';
    };
    const div = document.createElement('div');
    div.className = 'length-tier-row';
    div.style.cssText = 'display:grid;grid-template-columns:minmax(80px,1fr) 58px repeat(3,minmax(64px,1fr)) auto;gap:6px;align-items:center;margin-bottom:6px;';
    div.innerHTML =
        '<input type="number" class="tier-threshold-value" min="0" step="any" value="' + parts.value + '" aria-label="输入长度数值" placeholder="例如 128">' +
        '<select class="tier-threshold-unit" aria-label="输入长度单位">' +
            '<option value="K"' + (parts.unit === 'K' ? ' selected' : '') + '>K</option>' +
            '<option value="M"' + (parts.unit === 'M' ? ' selected' : '') + '>M</option>' +
        '</select>' +
        '<input type="number" class="tier-input-price" min="0" step="0.0001" value="' + priceValue('input_price') + '" aria-label="条件输入价格" placeholder="输入价">' +
        '<input type="number" class="tier-cache-price" min="0" step="0.0001" value="' + priceValue('cache_read_price') + '" aria-label="条件缓存价格" placeholder="缓存价">' +
        '<input type="number" class="tier-output-price" min="0" step="0.0001" value="' + priceValue('output_price') + '" aria-label="条件输出价格" placeholder="输出价">' +
        '<button type="button" class="btn btn--sm" onclick="removeLengthTierRow(this)">×</button>';
    rows.appendChild(div);
}

function removeLengthTierRow(btn) {
    const row = btn && btn.parentElement;
    if (row) row.remove();
}

function collectLengthTiers() {
    const rows = document.querySelectorAll('#lengthTierRows .length-tier-row');
    const tiers = [];
    const thresholds = new Set();
    rows.forEach(function (row, index) {
        const threshold = lengthTierTokens(
            row.querySelector('.tier-threshold-value').value,
            row.querySelector('.tier-threshold-unit').value,
        );
        if (thresholds.has(threshold)) {
            throw new Error('条件档门槛不能重复');
        }
        thresholds.add(threshold);
        const readPrice = function (selector, label) {
            const raw = row.querySelector(selector).value;
            if (raw === '') return null;
            const value = Number(raw);
            if (!Number.isFinite(value) || value < 0) {
                throw new Error('第 ' + (index + 1) + ' 个条件档的' + label + '必须是非负数字');
            }
            return value;
        };
        const tier = {
            threshold_tokens: threshold,
            input_price: readPrice('.tier-input-price', '输入价格'),
            cache_read_price: readPrice('.tier-cache-price', '缓存价格'),
            output_price: readPrice('.tier-output-price', '输出价格'),
        };
        if (tier.input_price == null && tier.cache_read_price == null && tier.output_price == null) {
            throw new Error('第 ' + (index + 1) + ' 个条件档至少要覆盖一个价格字段');
        }
        tiers.push(tier);
    });
    return tiers.sort(function (a, b) { return a.threshold_tokens - b.threshold_tokens; });
}

function pricingRows(tbody) {
    return Array.from(tbody.querySelectorAll('tr.pricing-row'));
}

function pricingRowIds(tbody) {
    return pricingRows(tbody).map(function (row) {
        return Number(row.dataset.pricingId);
    });
}

function updatePricingOrderLabels(tbody) {
    const rows = pricingRows(tbody);
    rows.forEach(function (row, index) {
        const indexLabel = row.querySelector('.pricing-order-index');
        if (indexLabel) indexLabel.textContent = String(index + 1);
        row.setAttribute('aria-posinset', String(index + 1));
        row.setAttribute('aria-setsize', String(rows.length));
    });
}

function clearPricingDropTarget(tbody) {
    tbody.querySelectorAll('.pricing-row--drop-target').forEach(function (row) {
        row.classList.remove('pricing-row--drop-target');
    });
}

function restorePricingRowOrder(tbody, order) {
    const rowById = new Map(pricingRows(tbody).map(function (row) {
        return [Number(row.dataset.pricingId), row];
    }));
    order.forEach(function (id) {
        const row = rowById.get(Number(id));
        if (row) tbody.appendChild(row);
    });
    updatePricingOrderLabels(tbody);
}

function movePricingRowAtPointer(state, clientY) {
    const rows = pricingRows(state.tbody).filter(function (row) {
        return row !== state.row;
    });
    let before = null;
    for (const row of rows) {
        const rect = row.getBoundingClientRect();
        if (clientY < rect.top + rect.height / 2) {
            before = row;
            break;
        }
    }
    if (before) {
        if (state.row.nextElementSibling !== before) {
            state.tbody.insertBefore(state.row, before);
        }
    } else if (state.row !== state.tbody.lastElementChild) {
        state.tbody.appendChild(state.row);
    }
    clearPricingDropTarget(state.tbody);
    if (before) before.classList.add('pricing-row--drop-target');
    updatePricingOrderLabels(state.tbody);
}

async function persistPricingOrder(order) {
    const tbody = document.querySelector('#pricingTable tbody');
    if (!tbody || _pricingOrderSavePromise) return;
    const table = tbody.closest('table');
    const request = (async function () {
        tbody.dataset.orderSaving = '1';
        tbody.setAttribute('aria-busy', 'true');
        if (table) table.classList.add('pricing-table--saving');
        try {
            await proxyApi('/api/proxy/pricing/reorder', {
                method: 'POST',
                body: JSON.stringify({ ids: order }),
            });
            const pricingById = new Map(_pricingCache.map(function (item) {
                return [Number(item.id), item];
            }));
            _pricingCache = order.map(function (id) {
                return pricingById.get(Number(id));
            }).filter(Boolean);
            ConfigSync.markDirty();
            showToast('定价优先级已更新');
        } catch (err) {
            showToast(err.message, 'error');
            await loadPricingTable();
        } finally {
            delete tbody.dataset.orderSaving;
            tbody.removeAttribute('aria-busy');
            if (table) table.classList.remove('pricing-table--saving');
        }
    })();
    _pricingOrderSavePromise = request;
    try {
        await request;
    } finally {
        if (_pricingOrderSavePromise === request) _pricingOrderSavePromise = null;
    }
}

function handlePricingReorderKeydown(event) {
    if (!['ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) return;
    if (_pricingOrderSavePromise) return;
    const handle = event.currentTarget;
    const row = handle.closest('tr.pricing-row');
    const tbody = row && row.parentElement;
    if (!row || !tbody) return;
    const rows = pricingRows(tbody);
    const currentIndex = rows.indexOf(row);
    let targetIndex = currentIndex;
    if (event.key === 'ArrowUp') targetIndex -= 1;
    if (event.key === 'ArrowDown') targetIndex += 1;
    if (event.key === 'Home') targetIndex = 0;
    if (event.key === 'End') targetIndex = rows.length - 1;
    if (targetIndex < 0 || targetIndex >= rows.length || targetIndex === currentIndex) return;

    event.preventDefault();
    const target = rows[targetIndex];
    if (targetIndex < currentIndex) {
        tbody.insertBefore(row, target);
    } else if (target.nextElementSibling) {
        tbody.insertBefore(row, target.nextElementSibling);
    } else {
        tbody.appendChild(row);
    }
    updatePricingOrderLabels(tbody);
    handle.focus({ preventScroll: true });
    persistPricingOrder(pricingRowIds(tbody));
}

function beginPricingDrag(event) {
    if ((event.button != null && event.button !== 0) || _pricingOrderSavePromise) return;
    const handle = event.currentTarget;
    const row = handle.closest('tr.pricing-row');
    if (!row) return;
    const tbody = row.parentElement;
    handle.focus({ preventScroll: true });
    _pricingDragState = {
        handle,
        row,
        tbody,
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        originalOrder: pricingRowIds(tbody),
        active: false,
    };
    event.preventDefault();
    row.classList.add('pricing-row--pressing');
    try { handle.setPointerCapture(event.pointerId); } catch (_) { /* best effort */ }
}

function updatePricingDrag(event) {
    const state = _pricingDragState;
    if (!state || event.pointerId !== state.pointerId) return;
    const distance = Math.hypot(event.clientX - state.startX, event.clientY - state.startY);
    if (!state.active && distance < PRICING_DRAG_THRESHOLD) return;
    if (!state.active) {
        state.active = true;
        state.row.classList.add('pricing-row--dragging');
        state.tbody.classList.add('pricing-table--dragging');
        state.row.setAttribute('aria-grabbed', 'true');
    }
    event.preventDefault();
    movePricingRowAtPointer(state, event.clientY);
}

function endPricingDrag(event, cancelled) {
    const state = _pricingDragState;
    if (!state || event.pointerId !== state.pointerId) return;
    if (state.active) event.preventDefault();
    try {
        if (state.handle.hasPointerCapture(event.pointerId)) {
            state.handle.releasePointerCapture(event.pointerId);
        }
    } catch (_) { /* best effort */ }
    state.row.classList.remove('pricing-row--pressing', 'pricing-row--dragging');
    state.row.setAttribute('aria-grabbed', 'false');
    state.tbody.classList.remove('pricing-table--dragging');
    clearPricingDropTarget(state.tbody);
    const currentOrder = pricingRowIds(state.tbody);
    const changed = currentOrder.some(function (id, index) {
        return id !== state.originalOrder[index];
    });
    if (cancelled || !changed) {
        if (cancelled) restorePricingRowOrder(state.tbody, state.originalOrder);
    } else {
        persistPricingOrder(currentOrder);
    }
    _pricingDragState = null;
}

function bindPricingDragHandlers(tbody) {
    tbody.querySelectorAll('.pricing-drag-handle').forEach(function (handle) {
        handle.addEventListener('pointerdown', beginPricingDrag);
        handle.addEventListener('pointermove', updatePricingDrag);
        handle.addEventListener('pointerup', function (event) {
            endPricingDrag(event, false);
        });
        handle.addEventListener('pointercancel', function (event) {
            endPricingDrag(event, true);
        });
        handle.addEventListener('keydown', handlePricingReorderKeydown);
    });
}

async function loadPricingTable() {
    const tbody = document.querySelector('#pricingTable tbody');
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="7" class="td-loading">加载中...</td></tr>';
    try {
        const pricing = await proxyApi('/api/proxy/pricing');
        _pricingCache = pricing;
        if (!pricing.length) {
            tbody.innerHTML = '<tr><td colspan="7" class="td-empty">暂无定价</td></tr>';
            return;
        }
        tbody.innerHTML = pricing.map((p, index) => {
            const sym = currencySymbol(p.currency);
            return `
            <tr class="pricing-row" data-pricing-id="${p.id}" aria-posinset="${index + 1}" aria-setsize="${pricing.length}" aria-grabbed="false">
                <td class="pricing-order-cell">
                    <button type="button" class="pricing-drag-handle" title="拖动调整匹配优先级" aria-label="拖动 ${esc(p.model_pattern)} 调整优先级" aria-describedby="pricingOrderHelp" aria-keyshortcuts="ArrowUp ArrowDown Home End">
                        <span class="pricing-drag-grip" aria-hidden="true">⠿</span>
                        <span class="pricing-order-index">${index + 1}</span>
                    </button>
                </td>
                <td><code>${esc(p.model_pattern)}</code></td>
                <td>${sym}${p.input_price.toFixed(4)} / 1M tokens</td>
                <td>${sym}${p.output_price.toFixed(4)} / 1M tokens</td>
                <td>${p.cache_read_price != null ? sym + p.cache_read_price.toFixed(4) + ' / 1M tokens' : '<span style="color:var(--color-text-tertiary);">同输入价</span>'}</td>
                <td><span class="badge ${p.currency === 'USD' ? 'badge--type-plan' : 'badge--type-api'}">${esc(p.currency)}</span></td>
                <td>
                    <button class="btn btn--sm" onclick="editPricing(${p.id})">编辑</button>
                    <button class="btn btn--sm" onclick="deletePricing(${p.id})">删除</button>
                </td>
            </tr>
        `;}).join('');
        updatePricingOrderLabels(tbody);
        bindPricingDragHandlers(tbody);
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="7" class="td-error">加载失败: ${esc(err.message)}</td></tr>`;
    }
}

/// Refresh the pricing-form price labels' currency symbol.
function updatePricingSymbols() {
    const sel = document.querySelector('#pricingForm [name="currency"]');
    const sym = currencySymbol(sel ? sel.value : 'CNY');
    document.querySelectorAll('#pricingForm .price-sym').forEach((s) => { s.textContent = sym; });
}

async function savePricing(e) {
    e.preventDefault();
    const form = e.target;
    const data = Object.fromEntries(new FormData(form));
    const id = form.dataset.editId;
    const cacheRead = data.cache_read_price !== '' ? parseFloat(data.cache_read_price) : null;
    let lengthTiers;
    try {
        lengthTiers = collectLengthTiers();
    } catch (err) {
        showToast(err.message, 'error');
        return;
    }
    const payload = {
        model_pattern: data.model_pattern,
        input_price: parseFloat(data.input_price),
        output_price: parseFloat(data.output_price),
        cache_read_price: cacheRead,
        currency: data.currency || 'CNY',
        slots: collectSlots(),
        length_tiers: lengthTiers,
    };
    try {
        if (id) {
            await proxyApi(`/api/proxy/pricing/${id}`, { method: 'PUT', body: JSON.stringify(payload) });
            showToast('定价已更新');
            ConfigSync.markDirty();
        } else {
            await proxyApi('/api/proxy/pricing', { method: 'POST', body: JSON.stringify(payload) });
            showToast('定价已添加');
            ConfigSync.markDirty();
        }
        form.reset();
        form.dataset.editId = '';
        form.querySelector('[type=submit]').textContent = '添加';
        updatePricingSymbols();
        document.getElementById('slotRows').innerHTML = '';
        document.getElementById('lengthTierRows').innerHTML = '';
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
    if (form['currency']) form['currency'].value = p.currency || 'CNY';
    updatePricingSymbols();
    // Populate time-slot editor (empty for new rows)
    const slotRows = document.getElementById('slotRows');
    if (slotRows) {
        slotRows.innerHTML = '';
        (p.slots || []).forEach(function (s) { addSlotRow(s); });
    }
    const lengthTierRows = document.getElementById('lengthTierRows');
    if (lengthTierRows) {
        lengthTierRows.innerHTML = '';
        (p.length_tiers || []).forEach(function (tier) { addLengthTierRow(tier); });
    }
    form.dataset.editId = id;
    form.querySelector('[type=submit]').textContent = '更新';
    openModal('pricingModal');
}

async function deletePricing(id) {
    if (!confirm('确定删除此定价条目？')) return;
    try {
        await proxyApi(`/api/proxy/pricing/${id}`, { method: 'DELETE' });
        showToast('定价已删除');
        ConfigSync.markDirty();
        loadPricingTable();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

function initPricingPage() {
    const el = document.getElementById('page-settings-pricing');
    if (!el || el.dataset.initialized) return;
    el.dataset.initialized = '1';
    el.innerHTML = `
        <div class="page-header">
            <h1 class="page-title">模型定价管理</h1>
            <p class="page-subtitle">配置模型基本价格（每百万 token，CNY 默认 / 可选 USD）</p>
            <button class="btn btn--primary" onclick="openModal('pricingModal')">+ 添加定价</button>
        </div>
        <table class="mgmt-table pricing-table" id="pricingTable" aria-describedby="pricingOrderHelp">
            <thead><tr><th class="pricing-order-column">顺序</th><th>模型匹配</th><th>输入价格</th><th>输出价格</th><th>缓存命中价格</th><th>货币</th><th>操作</th></tr></thead>
            <tbody></tbody>
        </table>
        <p class="pricing-order-help" id="pricingOrderHelp"><span class="pricing-drag-grip" aria-hidden="true">⠿</span> 拖动左侧把手调整匹配优先级；也可聚焦把手后使用 ↑↓、Home、End 键。</p>
        <div class="modal-overlay" id="pricingModal" style="display:none">
            <div class="modal">
                <div class="modal__header">
                    <h3>模型定价</h3>
                    <button class="modal__close" onclick="closeModal('pricingModal')">&times;</button>
                </div>
                <form id="pricingForm" onsubmit="savePricing(event)" data-edit-id="">
                    <label>模型匹配模式 <input name="model_pattern" required placeholder="例如: deepseek-v4*"></label>
                    <label><span>输入价格 (<span class="price-sym">¥</span>/1M tokens)</span> <input name="input_price" type="number" step="0.0001" required></label>
                    <label><span>输出价格 (<span class="price-sym">¥</span>/1M tokens)</span> <input name="output_price" type="number" step="0.0001" required></label>
                    <label><span>缓存命中价格 (<span class="price-sym">¥</span>/1M tokens，可选)</span> <input name="cache_read_price" type="number" step="0.0001" placeholder="留空 = 与输入价格相同"></label>
                    <label>币种
                        <select name="currency" onchange="updatePricingSymbols()">
                            <option value="CNY">CNY</option>
                            <option value="USD">USD</option>
                        </select>
                    </label>
                    <div style="margin:10px 0;">
                        <div style="font-size:13px;color:var(--color-text-secondary);margin-bottom:6px;">
                            输入长度条件价（达到门槛后生效；留空字段继承基本价）
                        </div>
                        <div style="display:grid;grid-template-columns:minmax(80px,1fr) 58px repeat(3,minmax(64px,1fr)) auto;gap:6px;color:var(--color-text-tertiary);font-size:12px;margin-bottom:5px;">
                            <span>门槛</span><span>单位</span><span>输入价</span><span>缓存价</span><span>输出价</span><span></span>
                        </div>
                        <div id="lengthTierRows"></div>
                        <button type="button" class="btn btn--sm" onclick="addLengthTierRow(null)">+ 添加条件档</button>
                    </div>
                    <div style="margin:10px 0;">
                        <div style="font-size:13px;color:var(--color-text-secondary);margin-bottom:6px;">
                            时段倍率（每日生效，倍率作用于输入/输出/缓存三档价格）
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
    document.getElementById('lengthTierRows').innerHTML = '';
    loadPricingTable();
}
