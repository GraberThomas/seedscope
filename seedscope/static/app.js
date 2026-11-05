import { barChart, calendarChart, h, heatmapChart, hideTip, lineChart, rankedBars } from './charts.js';

const view = document.getElementById('view');
const WEEKDAYS = ['Lun', 'Mar', 'Mer', 'Jeu', 'Ven', 'Sam', 'Dim'];
const RANGES = [['24h', '24 h'], ['7d', '7 j'], ['30d', '30 j'], ['1y', '1 an'], ['all', 'Tout']];
const NAV = [['/', 'Aperçu'], ['/activite', 'Activité'], ['/torrents', 'Torrents'], ['/trackers', 'Trackers'], ['/rentabilite', 'Rentabilité']];
const NO_DATA = 'Pas encore assez de données : la collecte vient de démarrer, cette vue se remplira avec le temps.';
const UP = 'var(--series-1)';
const DOWN = 'var(--series-2)';
const SLOT_COLORS = ['var(--series-1)', 'var(--series-2)', 'var(--series-3)', 'var(--series-4)', 'var(--series-5)', 'var(--series-6)', 'var(--series-7)', 'var(--others)'];

const ui = {
  range: '7d',
  breakdownBy: 'tracker',
  deadDays: 30,
  contentMode: 'dead',
  contentSort: { key: 'size', desc: true },
  contentPage: 0,
  torrents: { sort: 'uploaded', desc: true, q: '', tracker: '', category: '', tag: '', status: 'active', page: 0 },
};
const PAGE_SIZE = 50;

// ---- Formatting ----
const number = (value, digits = 2) => value.toLocaleString('fr-FR', { minimumFractionDigits: digits, maximumFractionDigits: digits });
export function bytes(value) {
  if (value === null || value === undefined) return '–';
  const units = ['o', 'Kio', 'Mio', 'Gio', 'Tio', 'Pio'];
  let scaled = Math.abs(value);
  let unit = 0;
  while (scaled >= 1024 && unit < units.length - 1) { scaled /= 1024; unit += 1; }
  const digits = unit === 0 ? 0 : scaled >= 100 ? 0 : scaled >= 10 ? 1 : 2;
  return `${number(scaled, digits)} ${units[unit]}`;
}
const speed = (value) => `${bytes(value)}/s`;
// Axis ticks are round values: no trailing zeros, one style per axis.
function bytesAxis(value) {
  const units = ['o', 'Kio', 'Mio', 'Gio', 'Tio', 'Pio'];
  let scaled = value;
  let unit = 0;
  while (scaled >= 1024 && unit < units.length - 1) { scaled /= 1024; unit += 1; }
  return `${scaled.toLocaleString('fr-FR', { maximumFractionDigits: 2 })} ${units[unit]}`;
}
const speedAxis = (value) => `${bytesAxis(value)}/s`;
const ratioText = (value) => (value === null || value === undefined ? '–' : number(value));
const times = (value) => `${number(value)} ×`;
function duration(seconds) {
  if (!seconds || seconds < 0) return '–';
  const days = Math.floor(seconds / 86400);
  if (days >= 365) return `${number(days / 365, 1)} an${days >= 730 ? 's' : ''}`;
  if (days >= 1) return `${days} j`;
  const hours = Math.floor(seconds / 3600);
  return hours >= 1 ? `${hours} h` : `${Math.floor(seconds / 60)} min`;
}
const dateOf = (ts) => (ts > 0 ? new Date(ts * 1000).toLocaleDateString('fr-FR', { dateStyle: 'medium' }) : '–');
function ago(ts) {
  if (!ts || ts <= 0) return 'jamais';
  const delta = Date.now() / 1000 - ts;
  if (delta < 90) return 'à l’instant';
  return `il y a ${duration(delta)}`;
}
const dayLabel = (key) => new Date(`${key}T12:00:00`).toLocaleDateString('fr-FR', { dateStyle: 'full' });
function bucketLabels(kind, keys) {
  if (kind === 'hour') {
    const hours = keys.map((k) => new Date(k * 1000));
    return {
      labels: hours.map((d) => `${d.getHours()} h`),
      tips: hours.map((d) => d.toLocaleString('fr-FR', { dateStyle: 'medium', timeStyle: 'short' })),
    };
  }
  if (kind === 'day') {
    const days = keys.map((k) => new Date(`${k}T12:00:00`));
    return {
      labels: days.map((d) => d.toLocaleDateString('fr-FR', { day: 'numeric', month: 'short' })),
      tips: keys.map(dayLabel),
    };
  }
  const months = keys.map((k) => new Date(`${k}-15T12:00:00`));
  return {
    labels: months.map((d) => d.toLocaleDateString('fr-FR', { month: 'short', year: '2-digit' })),
    tips: months.map((d) => d.toLocaleDateString('fr-FR', { month: 'long', year: 'numeric' })),
  };
}
const trackerName = (key) => (key === null ? 'Autres' : key || 'Inconnu');
const categoryName = (key) => (key === null ? 'Autres' : key || 'Sans catégorie');

// ---- Data access ----
async function api(path) {
  const response = await fetch(path);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.error || `HTTP ${response.status}`);
  }
  return response.json();
}
const query = (params) => {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null) search.set(key, value);
  }
  return search.toString();
};

// ---- Building blocks ----
function card(title, subtitle, ...content) {
  return h('section', { class: 'card' },
    h('div', { class: 'card-head' }, h('h2', { text: title }), subtitle ? h('span', { class: 'subtle', text: subtitle }) : null),
    ...content);
}

function tile(label, value, delta, valueClass = '') {
  return h('div', { class: 'card tile' }, h('div', { class: 'label', text: label }), h('div', { class: `value ${valueClass}`, text: value }), delta || null);
}

function periodDelta(current, previous, label) {
  if (previous === null || previous === undefined) return h('span', { class: 'delta', text: 'Comparaison bientôt disponible' });
  if (previous === 0) return h('span', { class: 'delta', text: `Rien sur les ${label} précédents` });
  const change = (current - previous) / previous;
  const up = change >= 0;
  return h('span', {
    class: `delta ${up ? 'up' : 'down'}`,
    text: `${up ? '▲ +' : '▼ −'}${Math.abs(change * 100).toFixed(0)} % par rapport aux ${label} précédents`,
  });
}

function segmented(label, options, current, onChange) {
  return h('div', { class: 'segmented', role: 'group', 'aria-label': label },
    options.map(([value, text]) => h('button', {
      type: 'button', text, 'aria-pressed': String(value === current), onclick: () => onChange(value),
    })));
}

function select(label, options, current, onChange) {
  const element = h('select', { onchange: (event) => onChange(event.target.value) },
    options.map(([value, text]) => {
      const option = h('option', { value, text });
      option.selected = value === current;
      return option;
    }));
  return h('label', { class: 'field' }, label, element);
}

function dataTable(columns, rows, { sort, onSort, empty } = {}) {
  if (!rows.length) return h('div', { class: 'empty', text: empty || 'Aucun résultat.' });
  const headCells = columns.map((column) => {
    const active = sort && sort.key === column.key;
    const className = column.num ? 'num' : null;
    const attrs = { class: className, scope: 'col' };
    if (active) attrs['aria-sort'] = sort.desc ? 'descending' : 'ascending';
    const text = column.label + (active ? (sort.desc ? ' ↓' : ' ↑') : '');
    if (column.sortable && onSort) {
      return h('th', attrs, h('button', { type: 'button', text, onclick: () => onSort(column.key) }));
    }
    return h('th', { ...attrs, text });
  });
  const body = rows.map((row) => h('tr', {}, columns.map((column) => {
    const content = column.render ? column.render(row) : column.format ? column.format(row[column.key]) : row[column.key];
    const className = column.num ? 'num' : column.className;
    if (content instanceof Node) return h('td', { class: className }, content);
    return h('td', { class: className, text: content ?? '–' });
  })));
  return h('div', { class: 'table-wrap' }, h('table', {}, h('thead', {}, h('tr', {}, headCells)), h('tbody', {}, body)));
}

// Every chart has a table twin, built only when opened.
function tableView(buildColumns, buildRows) {
  const details = h('details', { class: 'table-view' }, h('summary', { text: 'Voir le tableau' }));
  details.addEventListener('toggle', () => {
    if (details.open && details.children.length === 1) {
      details.append(dataTable(buildColumns(), buildRows()));
    }
  }, { once: false });
  return details;
}

const torrentLink = (row) => h('a', { href: `#/torrent/${row.hash}`, text: row.name });
function tags(value) {
  const list = (value || '').split(',').map((t) => t.trim()).filter(Boolean);
  return list.length ? h('span', {}, list.map((tag) => h('span', { class: 'tag', text: tag }))) : '';
}

function pager(total, page, onPage, size = PAGE_SIZE) {
  if (total <= size) return null;
  const pages = Math.ceil(total / size);
  return h('div', { class: 'pager' },
    h('button', { class: 'ghost', type: 'button', text: 'Précédent', disabled: page <= 0, onclick: () => onPage(page - 1) }),
    h('span', { text: `Page ${page + 1} sur ${pages} (${total})` }),
    h('button', { class: 'ghost', type: 'button', text: 'Suivant', disabled: page >= pages - 1, onclick: () => onPage(page + 1) }));
}

// ---- Pages ----
async function overview() {
  const [summary, series] = await Promise.all([api('/api/summary'), api(`/api/timeseries?range=${ui.range}`)]);
  const { labels, tips } = bucketLabels(series.volume.kind, series.volume.keys);
  const volumeName = { hour: 'heure', day: 'jour', month: 'mois' }[series.volume.kind];

  const speedTable = tableView(
    () => [
      { key: 'time', label: 'Date' },
      { key: 'up', label: 'Envoi', num: true, format: speed },
      { key: 'dl', label: 'Réception', num: true, format: speed },
    ],
    () => series.ts.slice(-200).reverse().map((ts, i) => {
      const index = series.ts.length - 1 - i;
      return { time: new Date(ts * 1000).toLocaleString('fr-FR'), up: series.up[index], dl: series.dl[index] };
    }),
  );
  const volumeTable = tableView(
    () => [
      { key: 'label', label: 'Période' },
      { key: 'up', label: 'Envoyé', num: true, format: bytes },
    ],
    () => series.volume.keys.map((_, i) => ({ label: tips[i], up: series.volume.up[i] })).reverse(),
  );
  const totalsTable = () => tableView(
    () => [
      { key: 'time', label: 'Date' },
      { key: 'ul', label: 'Envoyé au total', num: true, format: bytes },
      { key: 'ratio', label: 'Ratio', num: true, format: ratioText },
    ],
    () => series.ts.slice(-200).reverse().map((ts, i) => {
      const index = series.ts.length - 1 - i;
      return { time: new Date(ts * 1000).toLocaleString('fr-FR'), ul: series.alltime_ul[index], ratio: series.ratio[index] };
    }),
  );

  return [
    h('div', { class: 'hero-row' },
      h('section', { class: 'card hero' },
        h('div', { class: 'label', text: 'Ratio global' }),
        h('div', { class: 'value', text: ratioText(summary.ratio) }),
        h('div', { class: 'subtle', text: `${bytes(summary.alltime_ul)} envoyés pour ${bytes(summary.alltime_dl)} reçus` })),
      h('div', { class: 'tiles' },
        tile('Envoi en cours', speed(summary.up_speed)),
        tile('Envoyé aujourd’hui', bytes(summary.up_today)),
        tile('Envoyé sur 7 jours', bytes(summary.up_7d), periodDelta(summary.up_7d, summary.up_7d_previous, '7 jours')),
        tile('Envoyé sur 30 jours', bytes(summary.up_30d), periodDelta(summary.up_30d, summary.up_30d_previous, '30 jours')),
        tile('Torrents', summary.torrents.toLocaleString('fr-FR'), h('span', { class: 'delta', text: `${summary.contents} contenus, ${summary.active} actifs` })),
        tile('Volume seedé', `≈ ${bytes(summary.size_unique)}`, h('span', { class: 'delta', text: `Estimation haute, sans doublons (cumul : ${bytes(summary.size_total)})` })))),
    h('div', { class: 'filters' },
      segmented('Période', RANGES, ui.range, (value) => { ui.range = value; render(); })),
    card('Débits', 'Moyenne par intervalle',
      lineChart({
        ts: series.ts,
        series: [{ name: 'Envoi', color: UP, values: series.up }, { name: 'Réception', color: DOWN, values: series.dl }],
        format: speed, axisFormat: speedAxis, binary: true, empty: NO_DATA, ariaLabel: 'Débits d’envoi et de réception',
      }),
      speedTable),
    h('div', { class: 'grid-2' },
      card(`Envoyé par ${volumeName}`, null,
        barChart({
          labels, tipLabels: tips,
          series: [{ name: 'Envoyé', color: UP, values: series.volume.up }],
          format: bytes, axisFormat: bytesAxis, empty: NO_DATA, ariaLabel: `Volume envoyé par ${volumeName}`,
        }),
        volumeTable),
      card('Upload cumulé', 'Depuis le premier usage de qBittorrent',
        lineChart({
          ts: series.ts,
          series: [{ name: 'Envoyé au total', color: UP, values: series.alltime_ul }],
          format: bytes, axisFormat: bytesAxis, binary: true, zero: false, empty: NO_DATA, ariaLabel: 'Upload cumulé',
        }),
        totalsTable())),
    card('Évolution du ratio', 'Tel que calculé par qBittorrent : il peut différer de celui des trackers',
      lineChart({
        ts: series.ts,
        series: [{ name: 'Ratio', color: UP, values: series.ratio }],
        format: (v) => number(v), zero: false, height: 200, empty: NO_DATA, ariaLabel: 'Évolution du ratio global',
      }),
      totalsTable()),
  ];
}

async function activity() {
  const [live, cal, heat] = await Promise.all([api('/api/live'), api('/api/calendar'), api('/api/heatmap')]);
  const uploading = live.rows.filter((row) => row.up_speed > 0 || row.dl_speed > 0);
  const lookup = new Map(heat.cells.map((cell) => [`${cell.weekday}-${cell.hour}`, cell.up]));
  return [
    card('Top du moment', live.ts ? `Mis à jour ${ago(live.ts)}` : null,
      dataTable([
        { key: 'name', label: 'Torrent', className: 'name', render: torrentLink },
        { key: 'tracker', label: 'Tracker', className: 'nowrap', format: trackerName },
        { key: 'up_speed', label: 'Envoi', num: true, format: speed },
        { key: 'dl_speed', label: 'Réception', num: true, format: speed },
        { key: 'leechers', label: 'Pairs', num: true },
        { key: 'ratio', label: 'Ratio', num: true, format: ratioText },
      ], uploading, { empty: 'Aucun transfert en ce moment.' })),
    card('Upload par jour', '371 derniers jours',
      calendarChart({ ...cal, format: bytes, formatDay: dayLabel, empty: NO_DATA }),
      tableView(
        () => [{ key: 'label', label: 'Jour' }, { key: 'up', label: 'Envoyé', num: true, format: bytes }],
        () => [...cal.days].reverse().map((day) => ({ label: dayLabel(day.day), up: day.up })),
      )),
    card('Heures actives', `Upload moyen par heure, sur ${heat.days} jours`,
      heatmapChart({ cells: heat.cells, format: bytes, weekdays: WEEKDAYS, empty: NO_DATA }),
      tableView(
        () => [{ key: 'day', label: 'Jour' }, ...Array.from({ length: 24 }, (_, hour) => ({ key: `h${hour}`, label: `${hour} h`, num: true, format: bytes }))],
        () => WEEKDAYS.map((day, weekday) => {
          const row = { day };
          for (let hour = 0; hour < 24; hour += 1) row[`h${hour}`] = lookup.get(`${weekday}-${hour}`) ?? null;
          return row;
        }),
      )),
  ];
}

async function torrentsPage() {
  const state = ui.torrents;
  const params = {
    sort: state.sort, dir: state.desc ? 'desc' : 'asc', q: state.q, status: state.status,
    limit: PAGE_SIZE, offset: state.page * PAGE_SIZE,
    tracker: state.tracker === '' ? null : state.tracker === '__none' ? '' : state.tracker,
    category: state.category === '' ? null : state.category === '__none' ? '' : state.category,
    tag: state.tag === '' ? null : state.tag,
  };
  const [data, filters] = await Promise.all([api(`/api/torrents?${query(params)}`), api('/api/filters')]);
  const update = (changes) => { Object.assign(state, { page: 0 }, changes); render(); };
  const onSort = (key) => {
    update(state.sort === key ? { desc: !state.desc } : { sort: key, desc: !['name', 'tracker', 'category'].includes(key) });
  };
  let timer;
  const search = h('input', {
    type: 'search', placeholder: 'Rechercher un nom…', 'aria-label': 'Rechercher un torrent', value: state.q,
    oninput: (event) => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        update({ q: event.target.value });
      }, 300);
    },
  });
  const noneValue = (value) => (value === '' ? '__none' : value);
  return [
    h('div', { class: 'filters' },
      search,
      select('Tracker', [['', 'Tous'], ...filters.trackers.map((t) => [noneValue(t), trackerName(t)])], state.tracker, (v) => update({ tracker: v })),
      select('Catégorie', [['', 'Toutes'], ...filters.categories.map((c) => [noneValue(c), categoryName(c)])], state.category, (v) => update({ category: v })),
      select('Tag', [['', 'Tous'], ...filters.tags.map((t) => [t, t])], state.tag, (v) => update({ tag: v })),
      select('État', [['active', 'Présents'], ['removed', 'Supprimés'], ['all', 'Tous']], state.status, (v) => update({ status: v }))),
    h('section', { class: 'card' },
      dataTable([
        { key: 'name', label: 'Torrent', sortable: true, className: 'name', render: torrentLink },
        { key: 'tracker', label: 'Tracker', sortable: true, className: 'nowrap', format: trackerName },
        { key: 'category', label: 'Catégorie', sortable: true, className: 'nowrap', format: (v) => categoryName(v) },
        { key: 'tags', label: 'Tags', render: (row) => tags(row.tags) },
        { key: 'size', label: 'Taille', sortable: true, num: true, format: bytes },
        { key: 'ratio', label: 'Ratio', sortable: true, num: true, format: ratioText },
        { key: 'uploaded', label: 'Envoyé', sortable: true, num: true, format: bytes },
        { key: 'up_7d', label: '7 j', sortable: true, num: true, format: bytes },
        { key: 'up_30d', label: '30 j', sortable: true, num: true, format: bytes },
        { key: 'seeding_time', label: 'Seed', sortable: true, num: true, format: duration },
        { key: 'added_on', label: 'Ajouté', sortable: true, num: true, format: dateOf },
        { key: 'last_activity', label: 'Activité', sortable: true, num: true, format: ago },
      ], data.rows, { sort: { key: state.sort, desc: state.desc }, onSort }),
      pager(data.total, state.page, (page) => { state.page = page; render(); })),
  ];
}

async function torrentDetail(hash) {
  const data = await api(`/api/torrent/${encodeURIComponent(hash)}`);
  const t = data.torrent;
  const days = data.days.map((day) => new Date(`${day}T12:00:00`));
  const ts = days.map((d) => Math.floor(d.getTime() / 1000));
  const sinceNote = `Historique détaillé depuis le ${dateOf(t.first_seen)}, début du suivi de ce torrent`;
  const { labels, tips } = bucketLabels('day', data.days);
  return [
    h('a', { class: 'back', href: '#/torrents', text: '← Tous les torrents' }),
    h('h1', { text: t.name }),
    t.removed_at ? h('div', { class: 'banner', role: 'status' }, h('strong', { text: '⚠ Supprimé' }), `Retiré de qBittorrent vers le ${dateOf(t.removed_at)}. Son historique est conservé.`) : null,
    h('div', { class: 'tiles' },
      tile('Ratio', ratioText(t.ratio)),
      tile('Envoyé', bytes(t.uploaded)),
      tile('Reçu', bytes(t.downloaded)),
      tile('Taille', bytes(t.size)),
      tile('Temps de seed', duration(t.seeding_time)),
      tile('Ajouté le', dateOf(t.added_on)),
      tile('Dernière activité', ago(t.last_activity)),
      tile('Tracker', trackerName(t.tracker), null, 'long'),
      tile('Seeders / leechers', `${t.num_complete} / ${t.num_incomplete}`)),
    t.tags ? h('div', {}, tags(t.tags)) : null,
    h('div', { class: 'grid-2' },
      card('Envoyé par jour', sinceNote,
        barChart({
          labels, tipLabels: tips, series: [{ name: 'Envoyé', color: UP, values: data.up }],
          format: bytes, axisFormat: bytesAxis, empty: 'Aucun envoi enregistré depuis le début du suivi.', ariaLabel: 'Volume envoyé par jour pour ce torrent',
        }),
        data.days.length === 0 ? null : tableView(
          () => [{ key: 'label', label: 'Jour' }, { key: 'up', label: 'Envoyé', num: true, format: bytes }, { key: 'dl', label: 'Reçu', num: true, format: bytes }],
          () => data.days.map((day, i) => ({ label: dayLabel(day), up: data.up[i], dl: data.dl[i] })).reverse(),
        )),
      card('Upload cumulé', sinceNote,
        lineChart({
          ts, series: [{ name: 'Envoyé au total', color: UP, values: data.uploaded_total }],
          format: bytes, axisFormat: bytesAxis, binary: true, zero: false, empty: 'Il faut au moins deux jours d’envois pour tracer cette courbe.', ariaLabel: 'Upload cumulé de ce torrent',
        }))),
    data.siblings.length ? card('Même contenu sur d’autres trackers', 'Cross-seeds partageant ces données',
      dataTable([
        { key: 'name', label: 'Torrent', className: 'name', render: torrentLink },
        { key: 'tracker', label: 'Tracker', className: 'nowrap', format: trackerName },
        { key: 'uploaded', label: 'Envoyé', num: true, format: bytes },
        { key: 'ratio', label: 'Ratio', num: true, format: ratioText },
      ], data.siblings)) : null,
  ];
}

async function trackers() {
  const data = await api(`/api/breakdown?${query({ by: ui.breakdownBy, range: ui.range })}`);
  const nameOf = ui.breakdownBy === 'tracker' ? trackerName : categoryName;
  const noun = ui.breakdownBy === 'tracker' ? 'tracker' : 'catégorie';
  const { labels, tips } = bucketLabels(data.kind, data.keys);
  const series = data.series.map((s) => ({ name: nameOf(s.key), color: SLOT_COLORS[s.slot], values: s.values }));
  const colorOf = new Map(data.series.map((s) => [s.key, SLOT_COLORS[s.slot]]));
  const ranked = [...data.rows].sort((a, b) => b.up_period - a.up_period).filter((row) => row.up_period > 0);
  return [
    h('div', { class: 'filters' },
      segmented('Regrouper par', [['tracker', 'Trackers'], ['category', 'Catégories']], ui.breakdownBy, (value) => { ui.breakdownBy = value; render(); }),
      segmented('Période', RANGES, ui.range, (value) => { ui.range = value; render(); })),
    card(`Envoyé par ${noun}`, null,
      barChart({
        labels, tipLabels: tips, series,
        format: bytes, axisFormat: bytesAxis, empty: NO_DATA, ariaLabel: `Volume envoyé par ${noun}`,
      }),
      tableView(
        () => [{ key: 'label', label: 'Période' }, ...series.map((s, i) => ({ key: `s${i}`, label: s.name, num: true, format: bytes }))],
        () => data.keys.map((_, i) => {
          const row = { label: tips[i] };
          series.forEach((s, k) => { row[`s${k}`] = s.values[i]; });
          return row;
        }).reverse(),
      )),
    h('div', { class: 'grid-2' },
      card('Part de l’upload sur la période', null,
        ranked.length
          ? rankedBars(ranked.map((row) => ({ key: row.key, label: nameOf(row.key), value: row.up_period })), bytes, (row) => colorOf.get(row.key) || 'var(--others)')
          : h('div', { class: 'empty', text: NO_DATA })),
      card('Totaux', 'Compteurs de qBittorrent, torrents supprimés inclus une fois suivis',
        dataTable([
          { key: 'key', label: noun === 'tracker' ? 'Tracker' : 'Catégorie', format: nameOf },
          { key: 'torrents', label: 'Torrents', num: true },
          { key: 'size_unique', label: 'Volume', num: true, format: bytes },
          { key: 'uploaded', label: 'Envoyé', num: true, format: bytes },
          { key: 'downloaded', label: 'Reçu', num: true, format: bytes },
          { key: 'ratio', label: 'Ratio', num: true, format: ratioText },
        ], data.rows))),
  ];
}

async function profitability() {
  const dead = ui.contentMode === 'dead';
  const sort = ui.contentSort;
  const params = {
    sort: sort.key, dir: sort.desc ? 'desc' : 'asc', limit: PAGE_SIZE, offset: ui.contentPage * PAGE_SIZE,
    dead_days: dead ? ui.deadDays : null,
  };
  const [data, deadSummary] = await Promise.all([
    api(`/api/contents?${query(params)}`),
    api(`/api/contents?${query({ dead_days: ui.deadDays, limit: 1 })}`),
  ]);
  const share = deadSummary.all_size ? Math.round((deadSummary.total_size / deadSummary.all_size) * 100) : 0;
  const onSort = (key) => {
    ui.contentSort = sort.key === key ? { key, desc: !sort.desc } : { key, desc: key !== 'name' };
    ui.contentPage = 0;
    render();
  };
  return [
    h('p', { class: 'note', text: 'Les cross-seeds d’un même contenu sont regroupés : les supprimer un par un ne libère rien. Cette page est une aide à la décision, SeedScope ne supprime jamais rien.' }),
    h('div', { class: 'tiles' },
      tile('Volume seedé', `≈ ${bytes(deadSummary.all_size)}`, h('span', { class: 'delta', text: 'Estimation haute' })),
      tile(`Sans activité depuis ${ui.deadDays} jours`, `${deadSummary.total} contenus`),
      tile('Espace concerné', `≈ ${bytes(deadSummary.total_size)}`, h('span', { class: 'delta', text: `${share} % du volume seedé` }))),
    h('div', { class: 'filters' },
      segmented('Vue', [['dead', 'Poids morts'], ['all', 'Tous les contenus']], ui.contentMode, (value) => {
        ui.contentMode = value;
        ui.contentSort = value === 'dead' ? { key: 'size', desc: true } : { key: 'efficiency', desc: true };
        ui.contentPage = 0;
        render();
      }),
      segmented('Seuil d’inactivité', [[30, '30 j'], [60, '60 j'], [90, '90 j'], [180, '180 j'], [365, '1 an']], ui.deadDays, (value) => {
        ui.deadDays = value; ui.contentPage = 0; render();
      })),
    h('section', { class: 'card' },
      dataTable([
        { key: 'name', label: 'Contenu', sortable: true, className: 'name', render: torrentLink },
        { key: 'trackers', label: 'Trackers', render: (row) => tags(row.trackers) },
        { key: 'torrents', label: 'Torrents', sortable: true, num: true },
        { key: 'size', label: 'Taille', sortable: true, num: true, format: bytes },
        { key: 'uploaded', label: 'Envoyé', sortable: true, num: true, format: bytes },
        { key: 'efficiency', label: 'Envoyé / taille', sortable: true, num: true, format: times },
        { key: 'up_30d', label: '30 j', sortable: true, num: true, format: bytes },
        { key: 'last_activity', label: 'Activité', sortable: true, num: true, format: ago },
      ], data.rows, {
        sort, onSort,
        empty: dead ? `Aucun contenu inactif depuis ${ui.deadDays} jours.` : 'Aucun contenu.',
      }),
      pager(data.total, ui.contentPage, (page) => { ui.contentPage = page; render(); })),
  ];
}

// ---- Router, status, theme ----
const AUTO_REFRESH = new Set(['/', '/activite']);
let renderToken = 0;

function currentRoute() {
  return location.hash.replace(/^#/, '') || '/';
}

async function pageFor(route) {
  if (route === '/') return overview();
  if (route === '/activite') return activity();
  if (route === '/torrents') return torrentsPage();
  if (route === '/trackers') return trackers();
  if (route === '/rentabilite') return profitability();
  if (route.startsWith('/torrent/')) return torrentDetail(route.slice('/torrent/'.length));
  return [h('div', { class: 'empty', text: 'Page introuvable.' })];
}

async function render({ focus = false } = {}) {
  const token = ++renderToken;
  const route = currentRoute();
  hideTip();
  for (const link of document.querySelectorAll('#nav a')) {
    const target = link.getAttribute('href').slice(1);
    const active = target === route || (target === '/torrents' && route.startsWith('/torrent/'));
    if (active) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current');
  }
  view.classList.add('loading');
  try {
    const content = await pageFor(route);
    if (token !== renderToken) return;
    const active = document.activeElement;
    const restoreSearch = active && active.matches('input[type="search"]') ? active.selectionStart : null;
    view.replaceChildren(...content.filter(Boolean));
    if (restoreSearch !== null) {
      const input = view.querySelector('input[type="search"]');
      if (input) { input.focus(); input.setSelectionRange(restoreSearch, restoreSearch); }
    }
    if (focus) { window.scrollTo(0, 0); view.focus({ preventScroll: true }); }
  } catch (error) {
    if (token !== renderToken) return;
    view.replaceChildren(h('div', { class: 'banner', role: 'alert' }, h('strong', { text: '⚠ Erreur' }), error.message));
  } finally {
    if (token === renderToken) view.classList.remove('loading');
  }
}

async function refreshStatus() {
  const status = document.getElementById('status');
  try {
    const summary = await api('/api/summary');
    const stale = !summary.last_sample || Date.now() / 1000 - summary.last_sample > 300;
    const failed = Boolean(summary.collector_error) || stale;
    status.className = `status ${failed ? 'error' : 'ok'}`;
    const label = summary.collector_error
      ? `Collecte en erreur : ${summary.collector_error}`
      : stale ? 'Collecte en attente' : `Collecte active, ${ago(summary.last_sample)}`;
    status.replaceChildren(h('span', { class: 'icon', text: failed ? '⚠' : '✓' }), label);
  } catch {
    status.className = 'status error';
    status.replaceChildren(h('span', { class: 'icon', text: '⚠' }), 'Serveur injoignable');
  }
}

function initTheme() {
  const button = document.getElementById('theme');
  const apply = (theme) => {
    if (theme) document.documentElement.dataset.theme = theme;
    else delete document.documentElement.dataset.theme;
  };
  const isDark = () => (document.documentElement.dataset.theme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')) === 'dark';
  const label = () => { button.textContent = isDark() ? 'Thème clair' : 'Thème sombre'; };
  let saved = null;
  try { saved = localStorage.getItem('seedscope-theme'); } catch { /* storage unavailable */ }
  apply(saved);
  label();
  button.addEventListener('click', () => {
    const next = isDark() ? 'light' : 'dark';
    apply(next);
    label();
    try { localStorage.setItem('seedscope-theme', next); } catch { /* storage unavailable */ }
  });
}

document.getElementById('nav').append(...NAV.map(([path, text]) => h('a', { href: `#${path}`, text })));
initTheme();
window.addEventListener('hashchange', () => render({ focus: true }));
document.addEventListener('scroll', hideTip, { passive: true });
render();
refreshStatus();
setInterval(() => {
  refreshStatus();
  // Never replace a page the reader is using: keep open tables and keyboard focus.
  const busy = view.querySelector('details[open]') || (view.contains(document.activeElement) && document.activeElement !== view);
  if (AUTO_REFRESH.has(currentRoute()) && !busy && !document.hidden) render();
}, 60000);
