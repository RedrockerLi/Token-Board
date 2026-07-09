/**
 * utils.js — Shared utility functions.
 *
 * Loaded first (before all other app scripts) so helpers like fmtNum,
 * esc, and matchesAny are available everywhere.
 */

// ── Glob / wildcard matching ────────────────────────────────────────────────

/** Convert a shell glob pattern to a RegExp.
 *  Supports * (any chars) and ? (single char), case-insensitive. */
function globToRegex(pattern) {
    var r = pattern.replace(/[.+^${}()|[\]\\]/g, '\\$&')
                   .replace(/\*/g, '.*')
                   .replace(/\?/g, '.');
    return new RegExp('^' + r + '$', 'i');
}

/** Return true if `name` matches any glob pattern in `patterns`. */
function matchesAny(name, patterns) {
    var lower = name.toLowerCase();
    for (var i = 0; i < patterns.length; i++) {
        if (globToRegex(patterns[i]).test(lower)) return true;
    }
    return false;
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
