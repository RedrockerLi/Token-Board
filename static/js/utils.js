/**
 * utils.js — Shared utility functions.
 *
 * Loaded first (before all other app scripts) so helpers like fmtNum and esc
 * are available everywhere.
 */

// ── Time formatting (ISO UTC → browser-local display) ────────────────────

function _pad2(n) { return String(n).padStart(2, '0'); }

/** ISO UTC timestamp ("YYYY-MM-DDTHH:MM:SS[.fff]Z") → local "YYYY-MM-DD HH:MM:SS". */
function fmtLocal(isoStr) {
    if (isoStr == null || isoStr === '') return '';
    var d = new Date(String(isoStr));
    if (isNaN(d.getTime())) return '';
    return d.getFullYear() + '-' + _pad2(d.getMonth() + 1) + '-' + _pad2(d.getDate())
        + ' ' + _pad2(d.getHours()) + ':' + _pad2(d.getMinutes()) + ':' + _pad2(d.getSeconds());
}

/** ISO UTC timestamp → local "HH:MM" (for chart x-axis labels). */
function fmtLocalHHMM(isoStr) {
    var s = fmtLocal(isoStr);
    return s ? s.slice(11, 16) : '';
}

function normalizeMinute(minute) {
    return ((Math.round(minute) % 1440) + 1440) % 1440;
}

/** Convert a recurring local minute-of-day to UTC minute-of-day. */
function localMinuteToUtc(minute, offsetMinutes) {
    var offset = offsetMinutes == null ? new Date().getTimezoneOffset() : offsetMinutes;
    return normalizeMinute(minute + offset);
}

/** Convert a recurring UTC minute-of-day to browser-local minute-of-day. */
function utcMinuteToLocal(minute, offsetMinutes) {
    var offset = offsetMinutes == null ? new Date().getTimezoneOffset() : offsetMinutes;
    return normalizeMinute(minute - offset);
}

function _parseCalendarDate(value) {
    if (!value) return null;
    var p = String(value).split('-').map(Number);
    if (p.length !== 3 || p.some(isNaN)) return null;
    return p;
}

function _formatUtcDate(date) {
    return date.getUTCFullYear() + '-' + _pad2(date.getUTCMonth() + 1) + '-' + _pad2(date.getUTCDate());
}

function _formatLocalDate(date) {
    return date.getFullYear() + '-' + _pad2(date.getMonth() + 1) + '-' + _pad2(date.getDate());
}

/** Local date → the UTC date whose midnight is shown on that local date. */
function localDateToUtcDate(localDate) {
    var p = _parseCalendarDate(localDate);
    if (!p) return '';
    for (var delta = -1; delta <= 1; delta++) {
        var candidate = new Date(Date.UTC(p[0], p[1] - 1, p[2] + delta));
        if (_formatLocalDate(candidate) === String(localDate)) return _formatUtcDate(candidate);
    }
    return '';
}

/** UTC calendar date "YYYY-MM-DD" → local calendar date "YYYY-MM-DD". */
function utcDateToLocalDate(utcDate) {
    var p = _parseCalendarDate(utcDate);
    if (!p) return '';
    var d = new Date(Date.UTC(p[0], p[1] - 1, p[2]));
    if (isNaN(d.getTime())) return '';
    return _formatLocalDate(d);
}

/** Local date "YYYY-MM-DD" → [UTC start ISO, UTC end ISO] covering the local day. */
function localDateToUtcRange(localDate) {
    if (!localDate) return null;
    var p = String(localDate).split('-').map(Number);
    if (p.length !== 3 || isNaN(p[0]) || isNaN(p[1]) || isNaN(p[2])) return null;
    var start = new Date(p[0], p[1] - 1, p[2]);
    if (isNaN(start.getTime())) return null;
    var end = new Date(p[0], p[1] - 1, p[2] + 1);
    return [start.toISOString(), new Date(end.getTime() - 1).toISOString()];
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
