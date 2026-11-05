// Dependency-free SVG charts. Every label is inserted through textContent:
// torrent names come from third parties and are untrusted.

const SVG_NS = 'http://www.w3.org/2000/svg';
const SEQ_CLASSES = 5;

export function h(tag, props = {}, ...children) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === 'class') element.className = value;
    else if (key === 'text') element.textContent = value;
    else if (key.startsWith('on')) element.addEventListener(key.slice(2), value);
    else if (key === 'value') element.value = value;
    else element.setAttribute(key, value === true ? '' : value);
  }
  for (const child of children.flat()) {
    if (child !== null && child !== undefined && child !== false) element.append(child);
  }
  return element;
}

function svg(tag, attrs = {}, parent = null) {
  const element = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === 'fill' || key === 'stroke' || key === 'fillOpacity') element.style[key] = value;
    else if (key === 'text') element.textContent = value;
    else element.setAttribute(key, value);
  }
  if (parent) parent.append(element);
  return element;
}

// ---- Tooltip: one shared element, values first, labels after ----
const tooltip = () => document.getElementById('tooltip');

function showTip(x, y, title, rows) {
  const tip = tooltip();
  tip.replaceChildren(
    h('div', { class: 'tip-title', text: title }),
    ...rows.map((row) => {
      const key = h('span', { class: 'key-line' });
      if (row.color) key.style.background = row.color;
      return h('div', { class: 'tip-row' },
        row.color ? key : null,
        h('strong', { text: row.value }),
        row.label ? h('span', { text: row.label }) : null);
    }),
  );
  tip.hidden = false;
  const box = tip.getBoundingClientRect();
  const left = x + 14 + box.width > innerWidth ? Math.max(4, x - box.width - 14) : x + 14;
  const top = Math.min(Math.max(4, y - box.height / 2), innerHeight - box.height - 4);
  tip.style.left = `${left}px`;
  tip.style.top = `${top}px`;
}

export function hideTip() {
  const tip = tooltip();
  if (tip) tip.hidden = true;
}

// ---- Scales ----
function niceDecimal(raw) {
  const power = 10 ** Math.floor(Math.log10(raw));
  const scaled = raw / power;
  return ([1, 2, 2.5, 5, 10].find((v) => v >= scaled) ?? 10) * power;
}

// Binary values step in round fractions of the largest unit (8,4 / 8,5 / 8,6 Tio).
function niceStep(raw, binary, max) {
  if (raw <= 0) return 1;
  if (!binary) return niceDecimal(raw);
  const unit = 1024 ** Math.floor(Math.log(Math.max(max, 1)) / Math.log(1024));
  return niceDecimal(raw / unit) * unit;
}

export function ticks(min, max, binary, count = 4) {
  const span = max - min || Math.abs(max) || 1;
  const step = niceStep(span / count, binary, Math.abs(max));
  const low = Math.floor(min / step) * step;
  const values = [];
  for (let i = 0; low + (i - 1) * step < max - step * 1e-9; i += 1) values.push(Number((low + i * step).toFixed(6)));
  return values;
}

function timeLabel(ts, span) {
  const date = new Date(ts * 1000);
  if (span <= 2 * 86400) return date.toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' });
  if (span <= 7 * 86400) return `${date.toLocaleDateString('fr-FR', { day: 'numeric', month: 'short' })} ${date.getHours()} h`;
  if (span <= 120 * 86400) return date.toLocaleDateString('fr-FR', { day: 'numeric', month: 'short' });
  return date.toLocaleDateString('fr-FR', { month: 'short', year: '2-digit' });
}

function observeWidth(container, draw) {
  let last = 0;
  const redraw = () => {
    const width = Math.round(container.clientWidth);
    if (width > 0 && width !== last) {
      last = width;
      draw(width);
    }
  };
  new ResizeObserver(redraw).observe(container);
  queueMicrotask(redraw);
}

export function legend(items, kind = 'line') {
  if (items.length < 2) return null;
  return h('div', { class: 'legend' }, items.map((item) => {
    const key = h('span', { class: kind === 'line' ? 'key-line' : 'key-box' });
    key.style.background = item.color;
    return h('span', {}, key, document.createTextNode(item.name));
  }));
}

function emptyState(message) {
  return h('div', { class: 'empty', text: message });
}

// ---- Which series the reader chose to hide, remembered per chart ----
const HIDDEN_STORAGE = 'seedscope-hidden-series';

function readHidden() {
  try {
    return JSON.parse(localStorage.getItem(HIDDEN_STORAGE)) || {};
  } catch {
    return {};
  }
}

function writeHidden(chartId, names) {
  const all = readHidden();
  if (names.size) all[chartId] = [...names];
  else delete all[chartId];
  try {
    localStorage.setItem(HIDDEN_STORAGE, JSON.stringify(all));
  } catch { /* storage unavailable: the choice lasts until the next redraw */ }
}

// ---- Line chart with a crosshair that snaps to the nearest position ----
export function lineChart({ ts, series, format, axisFormat, binary = false, zero = true, area = false, height = 240, empty, ariaLabel, connectGaps = false, chartId }) {
  const points = ts.length;
  const usable = series.some((s) => s.values.filter((v) => v !== null).length >= 2);
  if (!usable) return emptyState(empty);

  const id = chartId || ariaLabel;
  const hidden = new Set((readHidden()[id] || []).filter((name) => series.some((s) => s.name === name)));
  if (hidden.size >= series.length) hidden.clear();

  const box = h('div', { class: 'chart' });
  const span = ts[points - 1] - ts[0] || 1;
  const gaps = ts.slice(1).map((t, i) => t - ts[i]).sort((a, b) => a - b);
  // Sparse series (samples stored on change) are drawn as one continuous line.
  const breakAfter = connectGaps ? Infinity : (gaps[Math.floor(gaps.length / 2)] || 60) * 5;
  let lastWidth = 0;

  const draw = (width) => {
    lastWidth = width;
    const shown = series.filter((s) => !hidden.has(s.name));
    const present = shown.flatMap((s) => s.values).filter((v) => v !== null);
    if (!present.length) {
      box.replaceChildren(emptyState(empty));
      return;
    }
    let min = zero ? 0 : Math.min(...present);
    let max = Math.max(...present);
    if (max === min) max = min + (min || 1);
    if (!zero) {
      const pad = (max - min) * 0.1;
      min = Math.max(0, min - pad);
      max += pad;
    }
    const yTicks = ticks(min, max, binary);
    const yMin = zero ? 0 : yTicks[0];
    const yMax = yTicks[yTicks.length - 1];
    // Without a dedicated formatter, ticks get just enough decimals to stay distinct.
    const tickStep = yTicks.length > 1 ? yTicks[1] - yTicks[0] : 1;
    const tickDigits = Math.max(0, Math.ceil(-Math.log10(tickStep) - 1e-9));
    const axisText = axisFormat || ((value) => value.toLocaleString('fr-FR', { minimumFractionDigits: tickDigits, maximumFractionDigits: tickDigits }));

    const margin = { top: 16, right: shown.length === 1 ? 76 : 14, bottom: 26, left: 70 };
    const plotW = Math.max(40, width - margin.left - margin.right);
    const plotH = height - margin.top - margin.bottom;
    const x = (t) => margin.left + ((t - ts[0]) / span) * plotW;
    const y = (v) => margin.top + plotH - ((v - yMin) / (yMax - yMin)) * plotH;

    const root = svg('svg', {
      viewBox: `0 0 ${width} ${height}`, height, role: 'img', tabindex: 0,
      'aria-label': `${ariaLabel}. Touches fléchées pour parcourir les valeurs ; le tableau ci-dessous les reprend.`,
    });
    for (const tick of yTicks) {
      svg('line', { x1: margin.left, x2: margin.left + plotW, y1: y(tick), y2: y(tick), stroke: tick === yMin ? 'var(--axis)' : 'var(--grid)' }, root);
      svg('text', { x: margin.left - 8, y: y(tick) + 4, 'text-anchor': 'end', class: 'axis-label', text: axisText(tick) }, root);
    }
    const xCount = Math.max(2, Math.min(6, Math.floor(plotW / 110)));
    for (let i = 0; i <= xCount; i += 1) {
      const t = ts[0] + (span * i) / xCount;
      const anchor = i === 0 ? 'start' : i === xCount ? 'end' : 'middle';
      svg('text', { x: x(t), y: height - 8, 'text-anchor': anchor, class: 'axis-label', text: timeLabel(t, span) }, root);
    }

    for (const s of shown) {
      let path = '';
      const segments = [];
      let start = null;
      let previous = null;
      s.values.forEach((value, i) => {
        if (value === null) { previous = null; return; }
        const brokenGap = previous !== null && ts[i] - ts[previous] > breakAfter;
        if (previous === null || brokenGap) {
          if (start !== null && previous !== null) segments.push([start, previous]);
          start = i;
          path += `M${x(ts[i]).toFixed(1)} ${y(value).toFixed(1)}`;
        } else {
          path += `L${x(ts[i]).toFixed(1)} ${y(value).toFixed(1)}`;
        }
        previous = i;
      });
      if (start !== null && previous !== null) segments.push([start, previous]);
      if (area) {
        const base = y(yMin).toFixed(1);
        let fill = '';
        for (const [a, b] of segments) {
          fill += `M${x(ts[a]).toFixed(1)} ${base}`;
          for (let i = a; i <= b; i += 1) if (s.values[i] !== null) fill += `L${x(ts[i]).toFixed(1)} ${y(s.values[i]).toFixed(1)}`;
          fill += `L${x(ts[b]).toFixed(1)} ${base}Z`;
        }
        svg('path', { d: fill, fill: s.color, fillOpacity: 0.1 }, root);
      }
      svg('path', { d: path, fill: 'none', stroke: s.color, 'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }, root);
    }

    if (shown.length === 1) {
      const values = shown[0].values;
      let last = values.length - 1;
      while (last > 0 && values[last] === null) last -= 1;
      if (values[last] !== null) {
        svg('circle', { cx: x(ts[last]), cy: y(values[last]), r: 4, fill: shown[0].color, stroke: 'var(--surface)', 'stroke-width': 2 }, root);
        svg('text', { x: x(ts[last]) + 8, y: y(values[last]) + 4, class: 'end-label', text: format(values[last]) }, root);
      }
    }

    const cross = svg('g', { visibility: 'hidden' }, root);
    const crossLine = svg('line', { y1: margin.top, y2: margin.top + plotH, stroke: 'var(--axis)' }, cross);
    const dots = shown.map((s) => svg('circle', { r: 4, fill: s.color, stroke: 'var(--surface)', 'stroke-width': 2 }, cross));
    const nearest = (target) => {
      let low = 0;
      let high = points - 1;
      while (high - low > 1) {
        const mid = (low + high) >> 1;
        if (ts[mid] < target) low = mid; else high = mid;
      }
      return target - ts[low] < ts[high] - target ? low : high;
    };
    let current = points - 1;
    const select = (index, clientX, clientY) => {
      current = index;
      const px = x(ts[index]);
      crossLine.setAttribute('x1', px);
      crossLine.setAttribute('x2', px);
      shown.forEach((s, i) => {
        const value = s.values[index];
        dots[i].setAttribute('visibility', value === null ? 'hidden' : 'visible');
        if (value !== null) {
          dots[i].setAttribute('cx', px);
          dots[i].setAttribute('cy', y(value));
        }
      });
      cross.setAttribute('visibility', 'visible');
      const title = new Date(ts[index] * 1000).toLocaleString('fr-FR', { dateStyle: 'medium', timeStyle: span > 120 * 86400 ? undefined : 'short' });
      showTip(clientX, clientY, title, shown.map((s) => ({
        color: s.color, value: s.values[index] === null ? '–' : format(s.values[index]), label: s.name,
      })));
    };
    const overlay = svg('rect', { x: margin.left, y: margin.top, width: plotW, height: plotH, fill: 'transparent' }, root);
    const scale = () => root.getBoundingClientRect().width / width;
    overlay.addEventListener('pointermove', (event) => {
      const rect = root.getBoundingClientRect();
      const localX = (event.clientX - rect.left) / scale();
      select(nearest(ts[0] + ((localX - margin.left) / plotW) * span), event.clientX, event.clientY);
    });
    const leave = () => { cross.setAttribute('visibility', 'hidden'); hideTip(); };
    overlay.addEventListener('pointerleave', leave);
    root.addEventListener('blur', leave);
    root.addEventListener('keydown', (event) => {
      if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
      event.preventDefault();
      const next = Math.min(points - 1, Math.max(0, current + (event.key === 'ArrowLeft' ? -1 : 1)));
      const rect = root.getBoundingClientRect();
      select(next, rect.left + x(ts[next]) * scale(), rect.top + margin.top + 20);
    });
    box.replaceChildren(root);
  };

  const toggles = series.length < 2 ? null : h('div', { class: 'legend legend-toggles', role: 'group', 'aria-label': 'Courbes affichées' });
  if (toggles) {
    const syncToggles = () => {
      const lastShown = series.length - hidden.size === 1;
      for (const button of toggles.children) {
        const visible = !hidden.has(button.dataset.name);
        button.setAttribute('aria-pressed', String(visible));
        // One curve always stays: an empty chart would answer nothing.
        button.disabled = visible && lastShown;
        button.title = button.disabled ? 'Au moins une courbe reste affichée' : `${visible ? 'Masquer' : 'Afficher'} cette courbe`;
      }
    };
    for (const s of series) {
      const key = h('span', { class: 'key-line' });
      key.style.background = s.color;
      toggles.append(h('button', {
        type: 'button',
        'data-name': s.name,
        onclick: (event) => {
          if (hidden.has(s.name)) hidden.delete(s.name); else hidden.add(s.name);
          writeHidden(id, hidden);
          syncToggles();
          hideTip();
          if (lastWidth) draw(lastWidth);
          // After a mouse click, focus must not keep the page from refreshing itself.
          if (event.detail > 0) event.currentTarget.blur();
        },
      }, key, document.createTextNode(s.name)));
    }
    syncToggles();
  }
  observeWidth(box, draw);
  return h('div', {}, toggles, box);
}

// ---- Column chart, stacked when given several series ----
export function barChart({ labels, tipLabels, series, format, axisFormat, binary = true, height = 240, empty, ariaLabel, totalLabel = 'Total' }) {
  const totals = labels.map((_, i) => series.reduce((sum, s) => sum + (s.values[i] || 0), 0));
  const peak = Math.max(0, ...totals);
  if (peak === 0) return emptyState(empty);
  const box = h('div', { class: 'chart' });
  const wrap = h('div', {}, legend(series, 'box'), box);
  const yTicks = ticks(0, peak, binary);
  const yMax = yTicks[yTicks.length - 1];

  observeWidth(box, (width) => {
    const margin = { top: 12, right: 8, bottom: 26, left: 70 };
    const plotW = Math.max(40, width - margin.left - margin.right);
    const plotH = height - margin.top - margin.bottom;
    const band = plotW / labels.length;
    const thickness = Math.max(1, Math.min(24, band - 2));
    const y = (v) => (v / yMax) * plotH;
    const base = margin.top + plotH;
    const root = svg('svg', {
      viewBox: `0 0 ${width} ${height}`, height, role: 'img', tabindex: 0,
      'aria-label': `${ariaLabel}. Touches fléchées pour parcourir les valeurs ; le tableau ci-dessous les reprend.`,
    });
    for (const tick of yTicks) {
      svg('line', { x1: margin.left, x2: margin.left + plotW, y1: base - y(tick), y2: base - y(tick), stroke: tick === 0 ? 'var(--axis)' : 'var(--grid)' }, root);
      svg('text', { x: margin.left - 8, y: base - y(tick) + 4, 'text-anchor': 'end', class: 'axis-label', text: (axisFormat || format)(tick) }, root);
    }
    const every = Math.max(1, Math.ceil(labels.length / Math.max(2, Math.floor(plotW / 70))));
    labels.forEach((label, i) => {
      if (i % every === 0) {
        svg('text', { x: margin.left + band * (i + 0.5), y: height - 8, 'text-anchor': 'middle', class: 'axis-label', text: label }, root);
      }
    });

    const columns = labels.map((_, i) => {
      const column = svg('g', { class: 'mark' }, root);
      const cx = margin.left + band * i + (band - thickness) / 2;
      let top = base;
      const parts = series.map((s) => s.values[i] || 0);
      const lastIndex = parts.reduce((found, value, k) => (value > 0 ? k : found), -1);
      parts.forEach((value, k) => {
        if (value <= 0) return;
        const rawHeight = y(value);
        const barHeight = Math.max(1, k === lastIndex ? rawHeight : rawHeight - 2);
        const r = k === lastIndex ? Math.min(4, thickness / 2, barHeight) : 0;
        const topY = top - barHeight;
        svg('path', {
          d: `M${cx} ${top}V${topY + r}Q${cx} ${topY} ${cx + r} ${topY}H${cx + thickness - r}Q${cx + thickness} ${topY} ${cx + thickness} ${topY + r}V${top}Z`,
          fill: series[k].color,
        }, column);
        top -= rawHeight;
      });
      return column;
    });

    let current = labels.length - 1;
    const select = (index, clientX, clientY) => {
      columns[current]?.classList.remove('active');
      current = index;
      columns[index].classList.add('active');
      const rows = series
        .map((s) => ({ color: s.color, value: format(s.values[index] || 0), label: s.name, raw: s.values[index] || 0 }))
        .filter((row) => series.length === 1 || row.raw > 0);
      if (series.length > 1) rows.push({ value: format(totals[index]), label: totalLabel });
      showTip(clientX, clientY, (tipLabels || labels)[index], rows);
    };
    labels.forEach((_, i) => {
      const hit = svg('rect', { x: margin.left + band * i, y: margin.top, width: band, height: plotH, fill: 'transparent' }, root);
      hit.addEventListener('pointermove', (event) => select(i, event.clientX, event.clientY));
      hit.addEventListener('pointerleave', () => { columns[i].classList.remove('active'); hideTip(); });
    });
    root.addEventListener('blur', () => { columns[current]?.classList.remove('active'); hideTip(); });
    root.addEventListener('keydown', (event) => {
      if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
      event.preventDefault();
      const next = Math.min(labels.length - 1, Math.max(0, current + (event.key === 'ArrowLeft' ? -1 : 1)));
      const rect = root.getBoundingClientRect();
      const ratio = rect.width / width;
      select(next, rect.left + (margin.left + band * (next + 0.5)) * ratio, rect.top + 40);
    });
    box.replaceChildren(root);
  });
  return wrap;
}

// ---- Sequential scales (one hue, light to dark) ----
function sequential(values) {
  const peak = Math.max(0, ...values);
  return (value) => {
    if (value <= 0 || peak === 0) return 'var(--seq-0)';
    const level = Math.min(SEQ_CLASSES, Math.ceil(Math.sqrt(value / peak) * SEQ_CLASSES));
    return `var(--seq-${Math.max(1, level)})`;
  };
}

function scaleLegend(low, high) {
  return h('div', { class: 'scale', 'aria-hidden': 'true' },
    h('span', { class: 'gap', text: low }),
    [1, 2, 3, 4, 5].map((level) => {
      const swatch = h('span', { class: 'key-box' });
      swatch.style.background = `var(--seq-${level})`;
      return swatch;
    }),
    h('span', { class: 'gap', text: high }));
}

// ---- Year calendar: one cell per day ----
export function calendarChart({ start, end, days, format, formatDay, empty }) {
  if (!days.length) return emptyState(empty);
  const byDay = new Map(days.map((d) => [d.day, d.up]));
  const color = sequential(days.map((d) => d.up));
  const left = 22;
  const top = 18;
  const first = new Date(`${start}T12:00:00`);
  first.setDate(first.getDate() - ((first.getDay() + 6) % 7)); // back to Monday
  const last = new Date(`${end}T12:00:00`);
  const weeks = Math.ceil((last - first) / (7 * 86400000) + 1 / 7);
  const box = h('div', { class: 'chart chart-scroll' });
  observeWidth(box, (width) => {
    const size = Math.max(10, Math.min(20, Math.floor((width - left) / weeks) - 2));
    const step = size + 2;
    const root = svg('svg', { width: left + weeks * step, height: top + 7 * step, role: 'img', 'aria-label': 'Upload par jour ; le tableau ci-dessous reprend les valeurs.' });
    ['L', '', 'M', '', 'V', '', 'D'].forEach((label, i) => {
      if (label) svg('text', { x: 0, y: top + i * step + size - 3, class: 'axis-label', text: label }, root);
    });
    let lastLabelWeek = -4;
    let previousMonth = first.getMonth();
    for (let cursor = new Date(first), index = 0; cursor <= last; cursor.setDate(cursor.getDate() + 1), index += 1) {
      const week = Math.floor(index / 7);
      const weekday = index % 7;
      const key = `${cursor.getFullYear()}-${String(cursor.getMonth() + 1).padStart(2, '0')}-${String(cursor.getDate()).padStart(2, '0')}`;
      if (weekday === 0 && (cursor.getMonth() !== previousMonth || index === 0)) {
        previousMonth = cursor.getMonth();
        // The partial month at the left edge gives way to the next label.
        const nextMonthIsClose = index === 0 && cursor.getDate() > 14;
        if (!nextMonthIsClose && week - lastLabelWeek >= 3) {
          svg('text', { x: left + week * step, y: 11, class: 'axis-label', text: cursor.toLocaleDateString('fr-FR', { month: 'short' }) }, root);
          lastLabelWeek = week;
        }
      }
      const has = byDay.has(key);
      const value = byDay.get(key) || 0;
      const cell = svg('rect', {
        x: left + week * step, y: top + weekday * step, width: size, height: size, rx: 2, class: 'cell',
        fill: has ? color(value) : 'var(--seq-none)',
      }, root);
      const label = formatDay(key);
      cell.addEventListener('pointermove', (event) => showTip(event.clientX, event.clientY, label, [{ value: has ? format(value) : 'Pas de données' }]));
      cell.addEventListener('pointerleave', hideTip);
    }
    box.replaceChildren(root);
  });
  return h('div', {}, box, scaleLegend('Moins', 'Plus'));
}

// ---- Weekday / hour heatmap ----
export function heatmapChart({ cells, format, weekdays, empty }) {
  if (!cells.length) return emptyState(empty);
  const box = h('div', { class: 'chart' });
  const wrap = h('div', {}, box, scaleLegend('Moins', 'Plus'));
  const lookup = new Map(cells.map((c) => [`${c.weekday}-${c.hour}`, c]));
  const color = sequential(cells.map((c) => c.up));
  observeWidth(box, (width) => {
    const left = 34;
    const top = 16;
    const gap = 2;
    const cellW = Math.max(6, (width - left) / 24 - gap);
    const cellH = 22;
    const height = top + 7 * (cellH + gap);
    const root = svg('svg', { viewBox: `0 0 ${width} ${height}`, height, role: 'img', 'aria-label': 'Upload moyen par jour de la semaine et par heure ; le tableau ci-dessous reprend les valeurs.' });
    for (let hour = 0; hour < 24; hour += 3) {
      svg('text', { x: left + hour * (cellW + gap), y: 11, class: 'axis-label', text: `${hour} h` }, root);
    }
    weekdays.forEach((name, day) => {
      svg('text', { x: 0, y: top + day * (cellH + gap) + 15, class: 'axis-label', text: name }, root);
      for (let hour = 0; hour < 24; hour += 1) {
        const data = lookup.get(`${day}-${hour}`);
        const cell = svg('rect', {
          x: left + hour * (cellW + gap), y: top + day * (cellH + gap), width: cellW, height: cellH, rx: 2, class: 'cell',
          fill: data ? color(data.up) : 'var(--seq-none)',
        }, root);
        cell.addEventListener('pointermove', (event) => showTip(event.clientX, event.clientY, `${name} ${hour} h – ${hour + 1} h`, [
          data ? { value: format(data.up), label: `par heure en moyenne (${data.samples} mesures)` } : { value: 'Pas de données' },
        ]));
        cell.addEventListener('pointerleave', hideTip);
      }
    });
    box.replaceChildren(root);
  });
  return wrap;
}

// ---- Sparkline: daily columns for a table row, the row's numbers carry the values ----
export function sparkline(values) {
  const peak = Math.max(0, ...values);
  const width = 90;
  const height = 22;
  const root = svg('svg', { width, height, viewBox: `0 0 ${width} ${height}`, class: 'spark', 'aria-hidden': 'true' });
  svg('line', { x1: 0, x2: width, y1: height - 0.5, y2: height - 0.5, stroke: 'var(--axis)' }, root);
  if (peak === 0) return root;
  const band = width / values.length;
  values.forEach((value, i) => {
    if (value <= 0) return;
    const barHeight = Math.max(2, (value / peak) * (height - 2));
    svg('rect', { x: i * band, y: height - barHeight, width: Math.max(1, band - 1), height: barHeight, fill: 'var(--series-1)' }, root);
  });
  return root;
}

// ---- Ranked horizontal bars: same baseline, largest first ----
export function rankedBars(rows, format, colorOf) {
  const peak = Math.max(0, ...rows.map((row) => row.value)) || 1;
  return h('div', { class: 'ranked' }, rows.flatMap((row) => {
    const fill = h('div', { class: 'fill' });
    fill.style.width = `${(row.value / peak) * 100}%`;
    fill.style.background = colorOf(row);
    return [
      h('span', { text: row.label }),
      h('div', { class: 'track' }, fill),
      h('span', { class: 'val', text: format(row.value) }),
    ];
  }));
}
