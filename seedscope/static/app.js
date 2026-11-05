import { barChart, calendarChart, h, heatmapChart, hideTip, lineChart, rankedBars, sparkline } from './charts.js';

const view = document.getElementById('view');
const WEEKDAYS = ['Lun', 'Mar', 'Mer', 'Jeu', 'Ven', 'Sam', 'Dim'];
const RANGES = [['1h', '1 h'], ['6h', '6 h'], ['24h', '24 h'], ['3d', '3 j'], ['7d', '7 j'], ['30d', '30 j'], ['90d', '90 j'], ['180d', '6 mois'], ['1y', '1 an'], ['all', 'Tout']];
const WINDOWS = [[1, '24 h'], [7, '7 j'], [30, '30 j'], [90, '90 j'], [365, '1 an']];
const BUCKET_NAMES = { min5: 'tranche de 5 min', min30: 'tranche de 30 min', hour: 'heure', hour6: 'tranche de 6 h', day: 'jour', week: 'semaine', month: 'mois' };
const NAV = [['/', 'Aperçu'], ['/activite', 'Activité'], ['/contenus', 'Contenus'], ['/torrents', 'Torrents'], ['/popularite', 'Popularité'], ['/trackers', 'Trackers'], ['/rentabilite', 'Rentabilité'], ['/parametres', 'Paramètres']];
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
  contentWindow: 30,
  popularity: { mode: 'popularity', scope: 'content', page: 0 },
  liveScope: 'content',
  contents: { sort: 'uploaded', desc: true, q: '', tracker: '', category: '', tag: '', window: 30, page: 0 },
  torrents: { sort: 'uploaded', desc: true, q: '', tracker: '', category: '', tag: '', status: 'active', window: 7, page: 0 },
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
const popularityText = (value) => (value === null || value === undefined ? '–' : number(value, 3));
const swarm = (value) => (value === null || value === undefined || value < 0 ? '–' : value.toLocaleString('fr-FR'));
const windowName = (window) => WINDOWS.find(([value]) => value === window)[1];
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
  if (kind === 'min5' || kind === 'min30' || kind === 'hour' || kind === 'hour6') {
    const times = keys.map((k) => new Date(k * 1000));
    const label = (d) => {
      if (kind === 'hour') return `${d.getHours()} h`;
      if (kind === 'hour6') return `${d.toLocaleDateString('fr-FR', { day: 'numeric', month: 'short' })} ${d.getHours()} h`;
      return d.toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' });
    };
    return {
      labels: times.map(label),
      tips: times.map((d) => d.toLocaleString('fr-FR', { dateStyle: 'medium', timeStyle: 'short' })),
    };
  }
  if (kind === 'day') {
    const days = keys.map((k) => new Date(`${k}T12:00:00`));
    return {
      labels: days.map((d) => d.toLocaleDateString('fr-FR', { day: 'numeric', month: 'short' })),
      tips: keys.map(dayLabel),
    };
  }
  if (kind === 'week') {
    const days = keys.map((k) => new Date(`${k}T12:00:00`));
    return {
      labels: days.map((d) => d.toLocaleDateString('fr-FR', { day: 'numeric', month: 'short' })),
      tips: days.map((d) => `Semaine du ${d.toLocaleDateString('fr-FR', { dateStyle: 'long' })}`),
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

const rangeName = () => RANGES.find(([value]) => value === ui.range)[1];
const rangeFilter = () => segmented('Période', RANGES, ui.range, (value) => { ui.range = value; render(); });

function periodDelta(current, previous, reference) {
  if (previous === null || previous === undefined) return h('span', { class: 'delta', text: 'Comparaison bientôt disponible' });
  if (previous === 0) return h('span', { class: 'delta', text: `Rien par rapport ${reference}` });
  const change = (current - previous) / previous;
  const up = change >= 0;
  return h('span', {
    class: `delta ${up ? 'up' : 'down'}`,
    text: `${up ? '▲ +' : '▼ −'}${Math.abs(change * 100).toFixed(0)} % par rapport ${reference}`,
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
const contentHref = (key) => `#/contenu/${encodeURIComponent(key)}`;
const contentLink = (row) => h('a', { href: contentHref(row.content_key), text: row.name });
const scopeFilter = (current, onChange) => segmented('Regrouper par', [['content', 'Par contenu'], ['torrent', 'Par torrent']], current, onChange);
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
  const [summary, series, top] = await Promise.all([api('/api/summary'), api(`/api/timeseries?range=${ui.range}`), api(`/api/contents/top?range=${ui.range}`)]);
  const { labels, tips } = bucketLabels(series.volume.kind, series.volume.keys);
  const volumeName = BUCKET_NAMES[series.volume.kind];
  const stats = series.stats;
  const allTime = ui.range === 'all';
  const delta = allTime
    ? null
    : periodDelta(stats.up, stats.previous_up, 'à la période précédente');

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
        tile('Envoyé sur 7 jours', bytes(summary.up_7d), periodDelta(summary.up_7d, summary.up_7d_previous, 'aux 7 jours précédents')),
        tile('Envoyé sur 30 jours', bytes(summary.up_30d), periodDelta(summary.up_30d, summary.up_30d_previous, 'aux 30 jours précédents')),
        tile('Contenus', summary.contents.toLocaleString('fr-FR'), h('span', { class: 'delta', text: `${summary.torrents} torrents, ${summary.active} actifs` })),
        tile('Volume seedé', `≈ ${bytes(summary.size_unique)}`, h('span', { class: 'delta', text: `Estimation haute, sans doublons (cumul : ${bytes(summary.size_total)})` })))),
    h('div', { class: 'filters' }, rangeFilter()),
    h('div', { class: 'stat-row' },
      tile(allTime ? 'Envoyé depuis le début du suivi' : `Envoyé sur ${rangeName()}`, bytes(stats.up), delta),
      tile('Reçu', bytes(stats.dl)),
      tile('Ratio de la période', stats.ratio === null ? 'Rien reçu' : ratioText(stats.ratio)),
      tile('Débit moyen d’envoi', speed(stats.avg_up)),
      tile('Pic d’envoi', speed(stats.peak_up))),
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
    card('Contenus qui ont le plus envoyé', 'Sur la période choisie, tous trackers confondus',
      dataTable([
        { key: 'name', label: 'Contenu', className: 'name', render: contentLink },
        { key: 'trackers', label: 'Trackers', render: (row) => tags(row.trackers) },
        { key: 'up_period', label: 'Sur la période', num: true, format: bytes },
        { key: 'uploaded', label: 'Envoyé au total', num: true, format: bytes },
        { key: 'efficiency', label: 'Envoyé / taille', num: true, format: times },
        { key: 'popularity', label: 'Popularité', num: true, format: popularityText },
      ], top.rows, { empty: 'Aucun envoi enregistré sur cette période.' })),
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
  const groups = new Map();
  for (const row of uploading) {
    const group = groups.get(row.content_key) || { content_key: row.content_key, name: row.name, up_speed: 0, dl_speed: 0, leechers: 0, torrents: 0, trackers: new Set() };
    group.up_speed += row.up_speed;
    group.dl_speed += row.dl_speed;
    group.leechers += row.leechers;
    group.torrents += 1;
    group.trackers.add(trackerName(row.tracker));
    groups.set(row.content_key, group);
  }
  const contentRows = [...groups.values()].sort((a, b) => b.up_speed - a.up_speed);
  const liveTable = ui.liveScope === 'content'
    ? dataTable([
      { key: 'name', label: 'Contenu', className: 'name', render: contentLink },
      { key: 'trackers', label: 'Trackers', render: (row) => tags([...row.trackers].join(',')) },
      { key: 'up_speed', label: 'Envoi', num: true, format: speed },
      { key: 'dl_speed', label: 'Réception', num: true, format: speed },
      { key: 'leechers', label: 'Pairs', num: true },
    ], contentRows, { empty: 'Aucun transfert en ce moment.' })
    : dataTable([
      { key: 'name', label: 'Torrent', className: 'name', render: torrentLink },
      { key: 'tracker', label: 'Tracker', className: 'nowrap', format: trackerName },
      { key: 'up_speed', label: 'Envoi', num: true, format: speed },
      { key: 'dl_speed', label: 'Réception', num: true, format: speed },
      { key: 'leechers', label: 'Pairs', num: true },
      { key: 'ratio', label: 'Ratio', num: true, format: ratioText },
    ], uploading, { empty: 'Aucun transfert en ce moment.' });
  const lookup = new Map(heat.cells.map((cell) => [`${cell.weekday}-${cell.hour}`, cell.up]));
  return [
    h('div', { class: 'filters' }, scopeFilter(ui.liveScope, (value) => { ui.liveScope = value; render(); })),
    card('Top du moment', live.ts ? `Mis à jour ${ago(live.ts)}` : null, liveTable),
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
    limit: PAGE_SIZE, offset: state.page * PAGE_SIZE, window: state.window,
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
      select('État', [['active', 'Présents'], ['removed', 'Supprimés'], ['all', 'Tous']], state.status, (v) => update({ status: v })),
      select('Envoyé sur', WINDOWS.map(([value, label]) => [String(value), label]), String(state.window), (v) => update({ window: Number(v) }))),
    h('section', { class: 'card' },
      dataTable([
        { key: 'name', label: 'Torrent', sortable: true, className: 'name', render: torrentLink },
        { key: 'tracker', label: 'Tracker', sortable: true, className: 'nowrap', format: trackerName },
        { key: 'category', label: 'Catégorie', sortable: true, className: 'nowrap', format: (v) => categoryName(v) },
        { key: 'tags', label: 'Tags', render: (row) => tags(row.tags) },
        { key: 'size', label: 'Taille', sortable: true, num: true, format: bytes },
        { key: 'ratio', label: 'Ratio', sortable: true, num: true, format: ratioText },
        { key: 'uploaded', label: 'Envoyé', sortable: true, num: true, format: bytes },
        { key: 'up_period', label: `Sur ${windowName(data.window)}`, sortable: true, num: true, format: bytes },
        { key: 'spark', label: '30 jours', render: (row) => sparkline(row.spark) },
        { key: 'popularity', label: 'Popularité', sortable: true, num: true, format: popularityText },
        { key: 'seeders', label: 'Seeders', sortable: true, num: true, render: (row) => swarm(row.num_complete) },
        { key: 'leechers', label: 'Leechers', sortable: true, num: true, render: (row) => swarm(row.num_incomplete) },
        { key: 'seeding_time', label: 'Seed', sortable: true, num: true, format: duration },
        { key: 'added_on', label: 'Ajouté', sortable: true, num: true, format: dateOf },
        { key: 'last_activity', label: 'Activité', sortable: true, num: true, format: ago },
      ], data.rows, { sort: { key: state.sort, desc: state.desc }, onSort }),
      pager(data.total, state.page, (page) => { state.page = page; render(); })),
  ];
}

async function torrentDetail(hash) {
  const data = await api(`/api/torrent/${encodeURIComponent(hash)}?range=${ui.range}`);
  const t = data.torrent;
  const { ts } = data.series;
  const { labels, tips } = bucketLabels(data.volume.kind, data.volume.keys);
  const since = data.first_sample ? `Suivi détaillé depuis le ${dateOf(data.first_sample)}` : 'Première mesure au plus tard dans l’heure';
  const noCurve = 'Pas encore assez de mesures sur cette période : elles sont prises toutes les 5 minutes quand quelque chose change, et au moins une fois par heure.';
  const curve = (title, subtitle, options) => card(title, subtitle, lineChart({ ts, connectGaps: true, zero: false, height: 200, empty: noCurve, ariaLabel: `${title} de ce torrent`, ...options }));
  const samplesTable = () => tableView(
    () => [
      { key: 'time', label: 'Date' },
      { key: 'uploaded', label: 'Envoyé', num: true, format: bytes },
      { key: 'ratio', label: 'Ratio', num: true, format: ratioText },
      { key: 'popularity', label: 'Popularité', num: true, format: popularityText },
      { key: 'seeders', label: 'Seeders', num: true, format: swarm },
      { key: 'leechers', label: 'Leechers', num: true, format: swarm },
    ],
    () => ts.slice(-200).map((time, i) => {
      const index = Math.max(0, ts.length - 200) + i;
      return {
        time: new Date(ts[index] * 1000).toLocaleString('fr-FR'),
        uploaded: data.series.uploaded[index],
        ratio: data.series.ratio[index],
        popularity: data.series.popularity[index],
        seeders: data.series.seeders[index],
        leechers: data.series.leechers[index],
      };
    }).reverse(),
  );
  return [
    h('a', { class: 'back', href: '#/torrents', text: '← Tous les torrents' }),
    h('h1', { text: t.name }),
    t.removed_at ? h('div', { class: 'banner', role: 'status' }, h('strong', { text: '⚠ Supprimé' }), `Retiré de qBittorrent vers le ${dateOf(t.removed_at)}. Son historique est conservé.`) : null,
    h('div', { class: 'tiles' },
      tile('Ratio', ratioText(t.ratio)),
      tile('Envoyé', bytes(t.uploaded), h('span', { class: 'delta', text: data.up_per_day === null ? '' : `≈ ${bytes(data.up_per_day)} par jour depuis l’ajout` })),
      tile('Reçu', bytes(t.downloaded)),
      tile('Taille', bytes(t.size)),
      tile('Popularité', popularityText(t.popularity), h('span', { class: 'delta', text: 'Ratio par mois actif' })),
      tile('Seeders / leechers', `${swarm(t.num_complete)} / ${swarm(t.num_incomplete)}`),
      tile('Temps de seed', duration(t.seeding_time)),
      tile('Ajouté le', dateOf(t.added_on)),
      tile('Dernière activité', ago(t.last_activity)),
      tile('Tracker', trackerName(t.tracker), null, 'long')),
    t.tags ? h('div', {}, tags(t.tags)) : null,
    data.siblings.length ? h('a', { class: 'back', href: contentHref(t.content_key), text: `Voir le contenu complet (${data.siblings.length + 1} torrents) →` }) : null,
    h('div', { class: 'filters' }, rangeFilter(), h('span', { class: 'subtle', text: since })),
    card(`Envoyé par ${BUCKET_NAMES[data.volume.kind]}`, `${bytes(data.up_period)} sur la période affichée`,
      barChart({
        labels, tipLabels: tips, series: [{ name: 'Envoyé', color: UP, values: data.volume.up }],
        format: bytes, axisFormat: bytesAxis, height: 200, empty: 'Aucun envoi enregistré sur cette période.', ariaLabel: 'Volume envoyé par ce torrent',
      }),
      data.volume.up.some((value) => value > 0) ? tableView(
        () => [{ key: 'label', label: 'Période' }, { key: 'up', label: 'Envoyé', num: true, format: bytes }, { key: 'dl', label: 'Reçu', num: true, format: bytes }],
        () => data.volume.keys.map((_, i) => ({ label: tips[i], up: data.volume.up[i], dl: data.volume.dl[i] })).reverse(),
      ) : null),
    h('div', { class: 'grid-2' },
      curve('Upload cumulé', null, { series: [{ name: 'Envoyé', color: UP, values: data.series.uploaded }], format: bytes, axisFormat: bytesAxis, binary: true }),
      curve('Ratio', null, { series: [{ name: 'Ratio', color: UP, values: data.series.ratio }], format: (v) => number(v, 3) }),
      curve('Popularité', 'Ratio divisé par le nombre de mois actifs', { series: [{ name: 'Popularité', color: UP, values: data.series.popularity }], format: (v) => number(v, 4) }),
      curve('Seeders et leechers', 'Tels qu’annoncés par le tracker', {
        series: [{ name: 'Seeders', color: UP, values: data.series.seeders }, { name: 'Leechers', color: DOWN, values: data.series.leechers }],
        format: (v) => number(v, Number.isInteger(v) ? 0 : 1), zero: true,
      })),
    h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', { text: 'Mesures de ces courbes' })), samplesTable()),
    data.siblings.length ? card('Même contenu sur d’autres trackers', 'Cross-seeds partageant ces données',
      dataTable([
        { key: 'name', label: 'Torrent', className: 'name', render: torrentLink },
        { key: 'tracker', label: 'Tracker', className: 'nowrap', format: trackerName },
        { key: 'uploaded', label: 'Envoyé', num: true, format: bytes },
        { key: 'ratio', label: 'Ratio', num: true, format: ratioText },
        { key: 'popularity', label: 'Popularité', num: true, format: popularityText },
      ], data.siblings)) : null,
  ];
}

async function contentsPage() {
  const state = ui.contents;
  const params = {
    sort: state.sort, dir: state.desc ? 'desc' : 'asc', q: state.q, window: state.window,
    limit: PAGE_SIZE, offset: state.page * PAGE_SIZE,
    tracker: state.tracker === '' ? null : state.tracker === '__none' ? '' : state.tracker,
    category: state.category === '' ? null : state.category === '__none' ? '' : state.category,
    tag: state.tag === '' ? null : state.tag,
  };
  const [data, summary, filters] = await Promise.all([
    api(`/api/contents?${query(params)}`), api('/api/contents/summary'), api('/api/filters'),
  ]);
  const update = (changes) => { Object.assign(state, { page: 0 }, changes); render(); };
  const onSort = (key) => {
    update(state.sort === key ? { desc: !state.desc } : { sort: key, desc: key !== 'name' });
  };
  let timer;
  const search = h('input', {
    type: 'search', placeholder: 'Rechercher un nom…', 'aria-label': 'Rechercher un contenu', value: state.q,
    oninput: (event) => {
      clearTimeout(timer);
      timer = setTimeout(() => { update({ q: event.target.value }); }, 300);
    },
  });
  const noneValue = (value) => (value === '' ? '__none' : value);
  return [
    h('div', { class: 'stat-row' },
      tile('Contenus', summary.contents.toLocaleString('fr-FR'), h('span', { class: 'delta', text: `${summary.cross_seeded} sur plusieurs trackers` })),
      tile('Volume seedé', `≈ ${bytes(summary.size)}`, h('span', { class: 'delta', text: 'Estimation haute' })),
      tile('Envoyé', bytes(summary.uploaded), h('span', { class: 'delta', text: 'Torrents présents, tous trackers confondus' })),
      tile('Envoyé / taille', summary.efficiency === null ? '–' : times(summary.efficiency)),
      tile('Jamais envoyés', summary.never_uploaded.toLocaleString('fr-FR'), h('span', { class: 'delta', text: `≈ ${bytes(summary.never_uploaded_size)}` }))),
    h('div', { class: 'filters' },
      search,
      select('Tracker', [['', 'Tous'], ...filters.trackers.map((t) => [noneValue(t), trackerName(t)])], state.tracker, (v) => update({ tracker: v })),
      select('Catégorie', [['', 'Toutes'], ...filters.categories.map((c) => [noneValue(c), categoryName(c)])], state.category, (v) => update({ category: v })),
      select('Tag', [['', 'Tous'], ...filters.tags.map((t) => [t, t])], state.tag, (v) => update({ tag: v })),
      select('Envoyé sur', WINDOWS.map(([value, label]) => [String(value), label]), String(state.window), (v) => update({ window: Number(v) }))),
    h('section', { class: 'card' },
      dataTable([
        { key: 'name', label: 'Contenu', sortable: true, className: 'name', render: contentLink },
        { key: 'trackers', label: 'Trackers', render: (row) => tags(row.trackers) },
        { key: 'size', label: 'Taille', sortable: true, num: true, format: bytes },
        { key: 'uploaded', label: 'Envoyé', sortable: true, num: true, format: bytes },
        { key: 'efficiency', label: 'Envoyé / taille', sortable: true, num: true, format: times },
        { key: 'ratio', label: 'Ratio', sortable: true, num: true, format: ratioText },
        { key: 'up_period', label: `Sur ${windowName(data.window)}`, sortable: true, num: true, format: bytes },
        { key: 'spark', label: '30 jours', render: (row) => sparkline(row.spark) },
        { key: 'popularity', label: 'Popularité', sortable: true, num: true, format: popularityText },
        { key: 'seeders', label: 'Seeders', sortable: true, num: true, format: swarm },
        { key: 'leechers', label: 'Leechers', sortable: true, num: true, format: swarm },
        { key: 'added_on', label: 'Ajouté', sortable: true, num: true, format: dateOf },
        { key: 'last_activity', label: 'Activité', sortable: true, num: true, format: ago },
      ], data.rows, { sort: { key: state.sort, desc: state.desc }, onSort }),
      pager(data.total, state.page, (page) => { state.page = page; render(); })),
  ];
}

async function contentDetail(key) {
  const data = await api(`/api/content?${query({ key, range: ui.range })}`);
  const c = data.content;
  const { ts } = data.series;
  const { labels, tips } = bucketLabels(data.volume.kind, data.volume.keys);
  const since = data.first_sample ? `Suivi détaillé depuis le ${dateOf(data.first_sample)}` : 'Première mesure au plus tard dans l’heure';
  const noCurve = 'Pas encore assez de mesures sur cette période : elles sont prises quand quelque chose change, et au moins une fois par heure.';
  const trackerSeries = (items) => items.map((item) => ({ name: trackerName(item.key), color: SLOT_COLORS[item.slot], values: item.values }));
  const curve = (title, subtitle, options) => card(title, subtitle, lineChart({ ts, connectGaps: true, zero: false, height: 200, empty: noCurve, ariaLabel: `${title} de ce contenu`, ...options }));
  const volumeSeries = trackerSeries(data.volume.series);
  const volumeTotal = data.volume.keys.map((_, i) => volumeSeries.reduce((sum, s) => sum + s.values[i], 0));
  const samplesTable = () => tableView(
    () => [
      { key: 'time', label: 'Date' },
      { key: 'uploaded', label: 'Envoyé', num: true, format: bytes },
      { key: 'efficiency', label: 'Envoyé / taille', num: true, format: times },
      { key: 'popularity', label: 'Popularité', num: true, format: popularityText },
      { key: 'seeders', label: 'Seeders', num: true, format: swarm },
      { key: 'leechers', label: 'Leechers', num: true, format: swarm },
    ],
    () => ts.map((time, index) => ({
      time: new Date(time * 1000).toLocaleString('fr-FR'),
      uploaded: data.series.uploaded[index],
      efficiency: data.series.efficiency[index],
      popularity: data.series.popularity[index],
      seeders: data.series.seeders[index],
      leechers: data.series.leechers[index],
    })).reverse(),
  );
  return [
    h('a', { class: 'back', href: '#/contenus', text: '← Tous les contenus' }),
    h('h1', { text: c.name }),
    c.active === 0 ? h('div', { class: 'banner', role: 'status' }, h('strong', { text: '⚠ Supprimé' }), 'Plus aucun torrent de ce contenu dans qBittorrent. Son historique est conservé.') : null,
    h('div', { class: 'tiles' },
      tile('Envoyé / taille', times(c.efficiency), h('span', { class: 'delta', text: 'Tous trackers confondus' })),
      tile('Ratio', ratioText(c.ratio), h('span', { class: 'delta', text: `${bytes(c.downloaded)} reçus` })),
      tile('Envoyé', bytes(c.uploaded), h('span', { class: 'delta', text: c.up_per_day === null ? '' : `≈ ${bytes(c.up_per_day)} par jour depuis l’ajout` })),
      tile('Taille', bytes(c.size)),
      tile('Popularité', popularityText(c.popularity), h('span', { class: 'delta', text: 'La meilleure de ses torrents' })),
      tile('Seeders / leechers', `${swarm(c.seeders)} / ${swarm(c.leechers)}`, h('span', { class: 'delta', text: 'Plus gros tracker / total' })),
      tile('Temps de seed', duration(c.seeding_time)),
      tile('Ajouté le', dateOf(c.added_on)),
      tile('Dernière activité', ago(c.last_activity)),
      tile('Torrents', `${c.active} sur ${c.torrents}`, h('span', { class: 'delta', text: 'Présents / suivis' }))),
    h('div', {}, tags(c.trackers.join(',')), tags(c.categories.join(','))),
    h('div', { class: 'filters' }, rangeFilter(), h('span', { class: 'subtle', text: since })),
    card(`Envoyé par ${BUCKET_NAMES[data.volume.kind]}`, `${bytes(data.up_period)} sur la période affichée`,
      barChart({
        labels, tipLabels: tips, series: volumeSeries, format: bytes, axisFormat: bytesAxis, height: 220,
        empty: 'Aucun envoi enregistré sur cette période.', ariaLabel: 'Volume envoyé par ce contenu, par tracker',
      }),
      volumeTotal.some((value) => value > 0) ? tableView(
        () => [{ key: 'label', label: 'Période' }, ...volumeSeries.map((s, i) => ({ key: `s${i}`, label: s.name, num: true, format: bytes }))],
        () => data.volume.keys.map((_, i) => {
          const row = { label: tips[i] };
          volumeSeries.forEach((s, k) => { row[`s${k}`] = s.values[i]; });
          return row;
        }).reverse(),
      ) : null),
    h('div', { class: 'grid-2' },
      curve('Upload cumulé par tracker', null, { series: trackerSeries(data.series.by_tracker), format: bytes, axisFormat: bytesAxis, binary: true }),
      curve('Envoyé / taille', 'Combien de fois ce contenu a été renvoyé', { series: [{ name: 'Envoyé / taille', color: UP, values: data.series.efficiency }], format: (v) => number(v, 3) }),
      curve('Popularité', 'La meilleure de ses torrents', { series: [{ name: 'Popularité', color: UP, values: data.series.popularity }], format: (v) => number(v, 4) }),
      curve('Seeders et leechers', 'Additionnés sur ses trackers', {
        series: [{ name: 'Seeders', color: UP, values: data.series.seeders }, { name: 'Leechers', color: DOWN, values: data.series.leechers }],
        format: (v) => number(v, 0), zero: true,
      })),
    h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', { text: 'Mesures de ces courbes' })), samplesTable()),
    card('Ses torrents', null,
      dataTable([
        { key: 'name', label: 'Torrent', className: 'name', render: torrentLink },
        { key: 'tracker', label: 'Tracker', className: 'nowrap', format: trackerName },
        { key: 'uploaded', label: 'Envoyé', num: true, format: bytes },
        { key: 'ratio', label: 'Ratio', num: true, format: ratioText },
        { key: 'spark', label: '30 jours', render: (row) => sparkline(row.spark) },
        { key: 'popularity', label: 'Popularité', num: true, format: popularityText },
        { key: 'seeders', label: 'Seeders', num: true, render: (row) => swarm(row.num_complete) },
        { key: 'leechers', label: 'Leechers', num: true, render: (row) => swarm(row.num_incomplete) },
        { key: 'state', label: 'État', render: (row) => (row.removed_at ? 'Supprimé' : 'Présent') },
      ], data.torrents)),
  ];
}

const POPULARITY_MODES = {
  popularity: { label: 'Les plus populaires', sort: 'popularity', desc: true, note: 'Ratio divisé par le nombre de mois actifs : ce qui a le mieux tourné pour toi, selon qBittorrent.' },
  leechers: { label: 'Les plus demandés', sort: 'leechers', desc: true, note: 'Leechers annoncés par les trackers en ce moment.' },
  lonely: { label: 'Les moins seedés', sort: 'seeders', desc: false, note: 'Seeders annoncés par les trackers : ces contenus dépendent le plus de toi.' },
};

async function popularityPage() {
  const state = ui.popularity;
  const mode = POPULARITY_MODES[state.mode];
  const byContent = state.scope === 'content';
  const listParams = { sort: mode.sort, dir: mode.desc ? 'desc' : 'asc', known_swarm: 1, limit: PAGE_SIZE, offset: state.page * PAGE_SIZE, window: 30 };
  const [summary, data] = await Promise.all([
    api('/api/popularity'),
    api(`${byContent ? '/api/contents' : '/api/torrents'}?${query(listParams)}`),
  ]);
  const trackerRows = summary.trackers.filter((row) => row.popularity > 0);
  const columns = byContent
    ? [
      { key: 'name', label: 'Contenu', className: 'name', render: contentLink },
      { key: 'trackers', label: 'Trackers', render: (row) => tags(row.trackers) },
      { key: 'popularity', label: 'Popularité', num: true, format: popularityText },
      { key: 'seeders', label: 'Seeders', num: true, format: swarm },
      { key: 'leechers', label: 'Leechers', num: true, format: swarm },
      { key: 'efficiency', label: 'Envoyé / taille', num: true, format: times },
    ]
    : [
      { key: 'name', label: 'Torrent', className: 'name', render: torrentLink },
      { key: 'tracker', label: 'Tracker', className: 'nowrap', format: trackerName },
      { key: 'popularity', label: 'Popularité', num: true, format: popularityText },
      { key: 'seeders', label: 'Seeders', num: true, render: (row) => swarm(row.num_complete) },
      { key: 'leechers', label: 'Leechers', num: true, render: (row) => swarm(row.num_incomplete) },
      { key: 'ratio', label: 'Ratio', num: true, format: ratioText },
    ];
  return [
    h('div', { class: 'stat-row' },
      tile('Popularité moyenne', popularityText(summary.popularity), h('span', { class: 'delta', text: 'Ratio par mois actif, par torrent' })),
      tile('Torrents demandés', summary.wanted.toLocaleString('fr-FR'), h('span', { class: 'delta', text: 'Au moins un leecher annoncé' })),
      tile('Leechers annoncés', summary.leechers.toLocaleString('fr-FR')),
      tile('Torrents presque sans seeder', summary.lonely.toLocaleString('fr-FR'), h('span', { class: 'delta', text: 'Un seeder annoncé au plus' }))),
    card('Popularité moyenne par tracker', null,
      trackerRows.length
        ? rankedBars(trackerRows.map((row) => ({ key: row.key, label: trackerName(row.key), value: row.popularity })), (v) => number(v, 3), () => UP)
        : h('div', { class: 'empty', text: NO_DATA }),
      tableView(
        () => [
          { key: 'key', label: 'Tracker', format: trackerName },
          { key: 'torrents', label: 'Torrents', num: true },
          { key: 'popularity', label: 'Popularité', num: true, format: popularityText },
          { key: 'seeders', label: 'Seeders en moyenne', num: true, format: (v) => (v === null ? '–' : number(v, 1)) },
          { key: 'leechers', label: 'Leechers', num: true, format: swarm },
        ],
        () => summary.trackers,
      )),
    h('div', { class: 'filters' },
      scopeFilter(state.scope, (value) => { state.scope = value; state.page = 0; render(); }),
      segmented('Classement', Object.entries(POPULARITY_MODES).map(([value, item]) => [value, item.label]), state.mode, (value) => {
        state.mode = value; state.page = 0; render();
      }),
      h('span', { class: 'subtle', text: mode.note })),
    h('section', { class: 'card' },
      dataTable([...columns,
        { key: 'up_period', label: 'Sur 30 j', num: true, format: bytes },
        { key: 'spark', label: '30 jours', render: (row) => sparkline(row.spark) },
      ], data.rows),
      pager(data.total, state.page, (page) => { state.page = page; render(); })),
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
      rangeFilter()),
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
    dead_days: dead ? ui.deadDays : null, window: ui.contentWindow,
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
      }),
      select('Envoyé sur', WINDOWS.map(([value, label]) => [String(value), label]), String(ui.contentWindow), (v) => {
        ui.contentWindow = Number(v); render();
      })),
    h('section', { class: 'card' },
      dataTable([
        { key: 'name', label: 'Contenu', sortable: true, className: 'name', render: contentLink },
        { key: 'trackers', label: 'Trackers', render: (row) => tags(row.trackers) },
        { key: 'torrents', label: 'Torrents', sortable: true, num: true },
        { key: 'size', label: 'Taille', sortable: true, num: true, format: bytes },
        { key: 'uploaded', label: 'Envoyé', sortable: true, num: true, format: bytes },
        { key: 'efficiency', label: 'Envoyé / taille', sortable: true, num: true, format: times },
        { key: 'up_period', label: `Sur ${windowName(data.window)}`, sortable: true, num: true, format: bytes },
        { key: 'popularity', label: 'Popularité', sortable: true, num: true, format: popularityText },
        { key: 'leechers', label: 'Leechers', sortable: true, num: true, format: swarm },
        { key: 'last_activity', label: 'Activité', sortable: true, num: true, format: ago },
      ], data.rows, {
        sort, onSort,
        empty: dead ? `Aucun contenu inactif depuis ${ui.deadDays} jours.` : 'Aucun contenu.',
      }),
      pager(data.total, ui.contentPage, (page) => { ui.contentPage = page; render(); })),
  ];
}

function intervalName(seconds) {
  if (seconds < 60) return `${seconds} secondes`;
  if (seconds === 60) return '1 minute';
  if (seconds === 3600) return '1 heure';
  return `${seconds / 60} minutes`;
}

async function settingsPage() {
  const current = await api('/api/settings');
  const options = (values) => values.map((value) => [String(value), intervalName(value)]);
  let globalInterval = current.global_interval;
  let torrentInterval = current.torrent_interval;
  const message = h('span', { class: 'subtle', role: 'status' });
  const save = async () => {
    message.textContent = 'Enregistrement…';
    try {
      const response = await fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ global_interval: globalInterval, torrent_interval: torrentInterval }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
      message.textContent = '✓ Enregistré, appliqué immédiatement.';
    } catch (error) {
      message.textContent = `⚠ Échec : ${error.message}`;
    }
  };
  return [
    h('h1', { text: 'Paramètres' }),
    h('section', { class: 'card settings' },
      h('div', { class: 'card-head' }, h('h2', { text: 'Fréquence de collecte' })),
      h('div', { class: 'setting' },
        select('Stats globales', options(current.global_options), String(globalInterval), (value) => { globalInterval = Number(value); }),
        h('p', { class: 'subtle', text: 'Débits, totaux envoyés et reçus, torrents en cours de transfert.' })),
      h('div', { class: 'setting' },
        select('Détail par torrent', options(current.torrent_options), String(torrentInterval), (value) => { torrentInterval = Number(value); }),
        h('p', { class: 'subtle', text: 'Upload, ratio, popularité, seeders et leechers de chaque torrent. Ne peut pas être plus fréquent que les stats globales.' })),
      h('p', { class: 'note', text: 'Une collecte plus fréquente donne des courbes plus fines, mais la base grossit plus vite et qBittorrent est davantage sollicité.' }),
      h('div', { class: 'filters' },
        h('button', { class: 'ghost', type: 'button', text: 'Enregistrer', onclick: save }),
        message)),
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
  if (route === '/contenus') return contentsPage();
  if (route.startsWith('/contenu/')) return contentDetail(decodeURIComponent(route.slice('/contenu/'.length)));
  if (route === '/torrents') return torrentsPage();
  if (route === '/popularite') return popularityPage();
  if (route === '/parametres') return settingsPage();
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
    const active = target === route || (target === '/torrents' && route.startsWith('/torrent/')) || (target === '/contenus' && route.startsWith('/contenu/'));
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
    const stale = !summary.last_sample || Date.now() / 1000 - summary.last_sample > Math.max(300, summary.global_interval * 3);
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

function initRefresh() {
  const button = document.getElementById('refresh');
  const status = document.getElementById('status');
  const say = (icon, text, ok) => {
    status.className = `status ${ok ? 'ok' : 'error'}`;
    status.replaceChildren(h('span', { class: 'icon', text: icon }), text);
  };
  button.addEventListener('click', async () => {
    button.disabled = true;
    button.textContent = 'Actualisation…';
    try {
      const response = await fetch('/api/collect', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      const body = await response.json();
      if (response.status === 429) {
        say('…', 'Collecte déjà demandée, patiente quelques secondes', true);
        return;
      }
      if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
      // A full collection takes a moment: follow it, then redraw the page.
      for (let attempt = 0; attempt < 30; attempt += 1) {
        await new Promise((resolve) => { setTimeout(resolve, 1000); });
        const summary = await api('/api/summary');
        if (summary.last_full_success > body.previous_full_success) {
          await render();
          say('✓', 'Données actualisées à l’instant', true);
          return;
        }
        if (summary.collector_error) throw new Error(summary.collector_error);
      }
      throw new Error('pas de réponse de la collecte après 30 secondes');
    } catch (error) {
      say('⚠', `Actualisation impossible : ${error.message}`, false);
    } finally {
      button.disabled = false;
      button.textContent = 'Actualiser';
    }
  });
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
initRefresh();
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
