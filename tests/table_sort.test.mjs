import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

// Served as a browser module, with no package.json for Node to read that from.
const source = await readFile(new URL('../seedscope/static/table-sort.js', import.meta.url), 'utf8');
const { sortRows, startsDescending } = await import(`data:text/javascript,${encodeURIComponent(source)}`);

const names = (rows) => rows.map((row) => row.name);

test('numbers sort by value, both ways', () => {
  const rows = [{ name: 'a', up: 9 }, { name: 'b', up: 100 }, { name: 'c', up: 20 }];
  assert.deepEqual(names(sortRows(rows, { key: 'up' }, true)), ['b', 'c', 'a']);
  assert.deepEqual(names(sortRows(rows, { key: 'up' }, false)), ['a', 'c', 'b']);
});

test('unknown values stay last in both directions', () => {
  const rows = [{ name: 'a', n: -1 }, { name: 'b', n: 5 }, { name: 'c', n: null }, { name: 'd', n: 2 }];
  assert.deepEqual(names(sortRows(rows, { key: 'n' }, true)), ['b', 'd', 'a', 'c']);
  assert.deepEqual(names(sortRows(rows, { key: 'n' }, false)), ['d', 'b', 'a', 'c']);
});

test('zero is a value, not an unknown', () => {
  const rows = [{ name: 'a', n: 0 }, { name: 'b', n: null }, { name: 'c', n: 3 }];
  assert.deepEqual(names(sortRows(rows, { key: 'n' }, false)), ['a', 'c', 'b']);
});

test('text ignores case and accents, and orders embedded numbers', () => {
  const rows = [{ name: 'Zèbre' }, { name: 'été' }, { name: 'Film 10' }, { name: 'Film 9' }];
  assert.deepEqual(names(sortRows(rows, { key: 'name' }, false)), ['été', 'Film 9', 'Film 10', 'Zèbre']);
});

test('dates follow the order of the rows, not their text', () => {
  const newestFirst = [{ name: '01/12/2026' }, { name: '30/11/2026' }, { name: '02/11/2026' }];
  const column = { key: 'name', order: -1 };
  assert.deepEqual(names(sortRows(newestFirst, column, true)), ['01/12/2026', '30/11/2026', '02/11/2026']);
  assert.deepEqual(names(sortRows(newestFirst, column, false)), ['02/11/2026', '30/11/2026', '01/12/2026']);
  const oldestFirst = [...newestFirst].reverse();
  assert.deepEqual(names(sortRows(oldestFirst, { key: 'name', order: 1 }, true)), ['01/12/2026', '30/11/2026', '02/11/2026']);
});

test('sparklines sort by their total, tracker sets by their names', () => {
  const rows = [{ name: 'a', spark: [1, 1] }, { name: 'b', spark: [5, 0] }];
  assert.deepEqual(names(sortRows(rows, { key: 'spark' }, true)), ['b', 'a']);
  const sets = [{ name: 'a', trackers: new Set(['z']) }, { name: 'b', trackers: new Set(['c', 'd']) }];
  assert.deepEqual(names(sortRows(sets, { key: 'trackers' }, false)), ['b', 'a']);
});

test('sortValue replaces the raw field, and the input keeps its order', () => {
  const rows = [{ name: 'a', removed_at: null }, { name: 'b', removed_at: 5 }];
  const column = { key: 'state', sortValue: (row) => (row.removed_at ? 1 : 0) };
  assert.deepEqual(names(sortRows(rows, column, true)), ['b', 'a']);
  assert.deepEqual(names(rows), ['a', 'b']);
});

test('numbers and dates start from the largest, text from A', () => {
  assert.equal(startsDescending({ key: 'up', num: true }, [{}]), true);
  assert.equal(startsDescending({ key: 'time', order: -1 }, [{}]), true);
  assert.equal(startsDescending({ key: 'spark' }, [{ spark: [1] }]), true);
  assert.equal(startsDescending({ key: 'name' }, [{ name: 'a' }]), false);
});
