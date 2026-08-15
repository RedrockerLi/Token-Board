'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const source = fs.readFileSync(
    path.join(__dirname, '..', '..', 'static', 'js', 'utils.js'),
    'utf8',
);
const context = { Date, isNaN, Math, String, Number };
vm.createContext(context);
vm.runInContext(source, context, { filename: 'utils.js' });

const {
    localMinuteToUtc,
    utcMinuteToLocal,
    localDateToUtcDate,
    utcDateToLocalDate,
    localDateToUtcRange,
} = context;

assert.strictEqual(localMinuteToUtc(9 * 60, -480), 60);
assert.strictEqual(localMinuteToUtc(14 * 60, -480), 360);
assert.strictEqual(utcMinuteToLocal(60, -480), 9 * 60);
assert.strictEqual(utcMinuteToLocal(360, -480), 14 * 60);

// New York summer/winter offsets and a cross-midnight conversion.
assert.strictEqual(localMinuteToUtc(9 * 60, 240), 13 * 60);
assert.strictEqual(localMinuteToUtc(9 * 60, 300), 14 * 60);
assert.strictEqual(utcMinuteToLocal(60, 240), 21 * 60);
assert.strictEqual(utcMinuteToLocal(60, 300), 20 * 60);
assert.strictEqual(localMinuteToUtc(0, -840), 600);
assert.strictEqual(utcMinuteToLocal(0, -840), 840);

// Cross-midnight ranges must remain valid after normalization.
assert.strictEqual(localMinuteToUtc(23 * 60, -480), 15 * 60);
assert.strictEqual(localMinuteToUtc(2 * 60, -480), 18 * 60);
assert.strictEqual(utcMinuteToLocal(1320, -480), 6 * 60);
assert.strictEqual(utcMinuteToLocal(120, -480), 10 * 60);

const timezone = process.argv[2] || 'Asia/Shanghai';
const expectedLocalDate = {
    'Asia/Shanghai': '2026-08-14',
    'America/New_York': '2026-08-13',
    UTC: '2026-08-14',
    'Pacific/Kiritimati': '2026-08-14',
}[timezone];
if (!expectedLocalDate) throw new Error(`unexpected timezone: ${timezone}`);

assert.strictEqual(utcDateToLocalDate('2026-08-14'), expectedLocalDate);
assert.strictEqual(localDateToUtcDate(expectedLocalDate), '2026-08-14');

const range = localDateToUtcRange(expectedLocalDate);
assert.strictEqual(range.length, 2);
const expectedRanges = {
    'Asia/Shanghai': ['2026-08-13T16:00:00.000Z', '2026-08-14T15:59:59.999Z'],
    'America/New_York': ['2026-08-13T04:00:00.000Z', '2026-08-14T03:59:59.999Z'],
    UTC: ['2026-08-14T00:00:00.000Z', '2026-08-14T23:59:59.999Z'],
    'Pacific/Kiritimati': ['2026-08-13T10:00:00.000Z', '2026-08-14T09:59:59.999Z'],
}[timezone];
assert.strictEqual(range[0], expectedRanges[0]);
assert.strictEqual(range[1], expectedRanges[1]);

console.log(`time utils ok: ${timezone}`);
