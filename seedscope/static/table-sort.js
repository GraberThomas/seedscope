// Sorting of tables shown in full. Paginated tables are sorted by the server instead:
// sorting them here would only reorder the current page.

const collator = new Intl.Collator('fr', { numeric: true, sensitivity: 'base' });

function normalize(value) {
  if (value instanceof Set) return [...value].join(', ');
  if (Array.isArray(value)) return value.reduce((sum, item) => sum + (item || 0), 0);
  if (typeof value === 'number' && (Number.isNaN(value) || value < 0)) return null; // qBittorrent: -1 = unknown
  if (value === undefined || value === '') return null;
  return value;
}

// ``order`` marks a date shown as text: rows are already in time order, 1 when the oldest
// comes first and -1 when the newest does.
function valueOf(column, row, index) {
  if (column.order) return index * column.order;
  return normalize(column.sortValue ? column.sortValue(row) : row[column.key]);
}

export function startsDescending(column, rows) {
  return Boolean(column.num || column.order || Array.isArray(rows[0]?.[column.key]));
}

export function sortRows(rows, column, desc) {
  const direction = desc ? -1 : 1;
  return rows
    .map((row, index) => ({ row, index, value: valueOf(column, row, index) }))
    .sort((a, b) => {
      // Unknown values stay last, whichever the direction.
      if (a.value === null || b.value === null) {
        return (a.value === null) - (b.value === null) || a.index - b.index;
      }
      const compared = typeof a.value === 'number' && typeof b.value === 'number'
        ? a.value - b.value
        : collator.compare(String(a.value), String(b.value));
      return compared * direction || a.index - b.index;
    })
    .map((item) => item.row);
}
