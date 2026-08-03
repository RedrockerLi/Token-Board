/**
 * utils.js — Shared utility functions.
 *
 * Loaded first (before all other app scripts) so helpers like fmtNum and esc
 * are available everywhere.
 */

// ── Time formatting (UTC → UTC+8) ─────────────────────────────────────────

/**
 * Convert a UTC datetime string from the databases ("YYYY-MM-DD HH:MM:SS")
 * into a UTC+8 (Asia/Shanghai) display string.  Falls back to the input on
 * unparseable values.  All frontend time displays use this so every interface
 * shows UTC+8 regardless of the browser's own timezone.
 */
function fmtUtc8(utcStr) {
    if (utcStr == null || utcStr === '') return '';
    var d = new Date(String(utcStr).replace(' ', 'T') + 'Z');
    if (isNaN(d.getTime())) return String(utcStr);
    return d.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false });
}

/** "YYYY-MM-DD HH:MM[:SS]" (UTC) → "HH:MM" in UTC+8 (for chart x-axis labels). */
function fmtUtc8HHMM(utcStr) {
    if (utcStr == null || utcStr === '') return '';
    var s = String(utcStr);
    if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/.test(s)) s = s + ':00';
    var out = fmtUtc8(s);
    if (!out) return '';
    // toLocaleString output ends with "YYYY/M/D HH:MM:SS" — take the time part.
    var m = out.match(/(\d{1,2}:\d{2})/);
    return m ? m[1] : out;
}

// ── Number formatting ───────────────────────────────────────────────────────

function fmtNum(n) {
    if (n == null || isNaN(n)) return '--';
    if (n >= 100_000_000_000) return (n / 1_000_000_000).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' B';
    if (n >= 100_000_000) return (n / 1_000_000).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' M';
    if (n >= 100_000) return (n / 1_000).toLocaleString('en-US', {minimumFractionDigits: 1, maximumFractionDigits: 1}) + ' K';
    return n.toLocaleString('en-US');
}

// ── HTML escaping ───────────────────────────────────────────────────────────

function esc(s) {
    if (s == null) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
