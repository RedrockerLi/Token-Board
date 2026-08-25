/**
 * api.js — HTTP communication layer.
 *
 * Provides fetch helpers, URL parameter construction, number formatting,
 * and thin wrappers around each backend API endpoint.
 */

// ── Display filter config (loaded from /static/display_config.json) ──
var displayConfig = { model_aliases: [] };
var _displayConfigLoaded = false;

async function loadDisplayConfig() {
    if (_displayConfigLoaded) return;
    try {
        displayConfig = await requestJSON('/static/display_config.json');
        _displayConfigLoaded = true;
    } catch (e) {
        console.warn('Failed to load display config, using defaults:', e);
        displayConfig = { model_aliases: [] };
        _displayConfigLoaded = true;
    }
}

// ── Number formatters (defined in utils.js, used globally) ──

function fmtCost(n) {
    if (n == null || isNaN(n)) return '--';
    return '¥' + Number(n).toLocaleString('en-US', {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
    });
}

// ── Fetch primitives ──

class HttpError extends Error {
    constructor(url, response, body) {
        const message = body && (body.error || body.message)
            || `HTTP ${response.status} for ${url}`;
        super(message);
        this.name = 'HttpError';
        this.url = url;
        this.status = response.status;
        this.body = body;
    }
}

class ResponseFormatError extends Error {
    constructor(url, response) {
        super(`Expected a JSON response from ${url}, got ${response.status}`);
        this.name = 'ResponseFormatError';
        this.url = url;
        this.status = response.status;
    }
}

/**
 * @typedef {'ok'|'error'|'conflict'|'remote_updated'|'not_found'|'scheduled'} BusinessStatus
 * A 2xx response may still carry a business failure status.  It is returned
 * as data and is deliberately not converted into HttpError.
 */

/** @param {string} url @param {RequestInit & {headers?: object}} options */
async function requestJSON(url, options = {}) {
    const fetchOptions = { ...options };
    const headers = { ...(fetchOptions.headers || {}) };
    if (fetchOptions.body != null && !headers['Content-Type']) {
        headers['Content-Type'] = 'application/json';
    }
    fetchOptions.headers = headers;
    const resp = await fetch(url, fetchOptions);
    if (resp.status === 204) return null;
    const contentType = (resp.headers.get('content-type') || '').toLowerCase();
    const text = await resp.text();
    if (!resp.ok) {
        let body = null;
        if (text) {
            try { body = JSON.parse(text); } catch (_) { /* text only */ }
        }
        throw new HttpError(url, resp, body);
    }
    if (!contentType.includes('json') || !text) {
        throw new ResponseFormatError(url, resp);
    }
    try {
        return JSON.parse(text);
    } catch (_) {
        throw new ResponseFormatError(url, resp);
    }
}

/** JSON-only proxy client; business statuses remain ordinary returned data. */
async function proxyApi(url, options = {}) {
    return requestJSON(url, options);
}

/** Keep non-JSON downloads out of requestJSON's response parser. */
async function requestFile(url, options = {}) {
    const resp = await fetch(url, options);
    if (!resp.ok) {
        const text = await resp.text().catch(() => '');
        let body = null;
        try { body = text ? JSON.parse(text) : null; } catch (_) { /* text only */ }
        throw new HttpError(url, resp, body);
    }
    return resp;
}

/** Build a relative URL with optional extra params, global api_key_name, and platform. */
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
    return requestJSON(buildParams('/api/summary'));
}

async function fetchDaily(year, month, model) {
    return requestJSON(buildParams('/api/daily', { year, month, model }));
}

async function fetchMonthly(model) {
    return requestJSON(buildParams('/api/monthly', { model }));
}

async function fetchTokenTypes() {
    return requestJSON(buildParams('/api/token_types'));
}

// Per-model usage for a given month, across ALL users (no api_key_name) —
// used to determine the deprecated-model set, which is a global concept.
async function fetchModelBreakdownAllUsers(year, month) {
    return requestJSON('/api/model_breakdown?year=' + year + '&month=' + month);
}

async function fetchModels() {
    return requestJSON('/api/models');
}

async function fetchRefresh() {
    return requestJSON('/api/refresh');
}

/** Delete one user's complete usage-dashboard archive on this machine only. */
async function deleteDashboardUserLocal(name, prepare) {
    return proxyApi('/api/proxy/dashboard/users', {
        method: 'DELETE',
        body: JSON.stringify({ name: name, prepare: !!prepare }),
    });
}

/** Upload the already-modified local Dashboard archive to the cloud. */
async function uploadDashboardUserDeletions() {
    return proxyApi('/api/proxy/dashboard/users/upload', {
        method: 'POST',
        body: JSON.stringify({}),
    });
}

// ── Performance metrics API wrappers ──

async function fetchPerfSummary(minutes) {
    return requestJSON(buildParams('/api/proxy/perf/summary', { minutes }));
}

async function fetchPerfLatency(minutes) {
    return requestJSON(buildParams('/api/proxy/perf/latency', { minutes }));
}

async function fetchPerfSpeed(minutes) {
    return requestJSON(buildParams('/api/proxy/perf/speed', { minutes }));
}

async function fetchPerfThroughput(minutes) {
    return requestJSON(buildParams('/api/proxy/perf/throughput', { minutes }));
}

async function fetchPerfModels(minutes) {
    return requestJSON(buildParams('/api/proxy/perf/models', { minutes }));
}

async function fetchPerfUpstreamSuccessRate(minutes) {
    return requestJSON(buildParams('/api/proxy/perf/upstream-success-rate', { minutes }));
}

async function fetchPerfRealtime() {
    return requestJSON('/api/proxy/perf/realtime');
}
