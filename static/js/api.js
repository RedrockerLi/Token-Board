/**
 * api.js — HTTP communication layer.
 *
 * Provides fetch helpers, URL parameter construction, number formatting,
 * and thin wrappers around each backend API endpoint.
 */

// ── Number formatters (used globally by charts.js and dashboard.js) ──

function fmtNum(n) {
    if (n == null || isNaN(n)) return '--';
    if (n >= 1_000_000_000) return (n / 1_000_000_000).toFixed(2) + ' B';
    if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + ' M';
    if (n >= 10_000) return (n / 1_000).toFixed(1) + ' K';
    return n.toLocaleString('en-US');
}

function fmtCost(n) {
    if (n == null || isNaN(n)) return '--';
    return '¥' + n.toFixed(2);
}

// ── Fetch primitives ──

async function fetchJSON(url) {
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`HTTP ${resp.status} for ${url}`);
    return resp.json();
}

/** Build a relative URL with optional extra params and the global api_key_name. */
function buildParams(baseUrl, extraParams) {
    const url = new URL(baseUrl, window.location.origin);
    for (const [k, v] of Object.entries(extraParams || {})) {
        if (v != null) url.searchParams.set(k, v);
    }
    if (typeof currentKeyName !== 'undefined' && currentKeyName) {
        url.searchParams.set('api_key_name', currentKeyName);
    }
    return url.pathname + url.search;
}

// ── API wrappers ──

async function fetchSummary() {
    return fetchJSON(buildParams('/api/summary'));
}

async function fetchDaily(year, month, model) {
    return fetchJSON(buildParams('/api/daily', { year, month, model }));
}

async function fetchMonthly(model) {
    return fetchJSON(buildParams('/api/monthly', { model }));
}

async function fetchTokenTypes() {
    return fetchJSON(buildParams('/api/token_types'));
}

async function fetchRefresh() {
    return fetchJSON('/api/refresh');
}
