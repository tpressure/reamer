(() => {
  const byId = id => document.getElementById(id);
  const about = byId('about-dialog');
  byId('about-open').addEventListener('click', () => {
    const logo = byId('about-logo');
    if (!logo.hasAttribute('src')) logo.src = logo.dataset.src;
    about.showModal();
  });
  about.addEventListener('click', event => {
    const bounds = about.getBoundingClientRect();
    if (event.target === about && (event.clientX < bounds.left || event.clientX > bounds.right
      || event.clientY < bounds.top || event.clientY > bounds.bottom)) about.close();
  });
  const tabs = [byId('overview-tab'), byId('details-tab'), byId('statistic-tab')];
  const picker = byId('statistics-vm');
  const chart = byId('statistics-chart');
  const metricButtons = [...document.querySelectorAll('[data-metric]')];
  const ns = 'http://www.w3.org/2000/svg';
  let active = false, selected = '', metric = 'migration', data = null;
  let timer, request, generation = 0, optionSignature = '';
  let timeWindow = null, rangeDomain = null, rangeDrag = null, rangeSignature = '';
  const rows = new Map();
  const number = value => new Intl.NumberFormat(undefined, {maximumFractionDigits: 1}).format(value);
  const duration = value => value == null ? '—' : `${number(value)} ms`;
  const percent = value => value == null ? '—' : `${number(value)}%`;
  const dateTime = value => new Date(value).toLocaleString();
  const setText = (element, value) => { if (element.textContent !== value) element.textContent = value; };

  function activate(index) {
    tabs.forEach((tab, i) => {
      tab.setAttribute('aria-selected', String(i === index));
      tab.tabIndex = i === index ? 0 : -1;
      byId(tab.getAttribute('aria-controls')).hidden = i !== index;
    });
    active = tabs[index].id === 'statistic-tab';
    clearTimeout(timer);
    if (active) refresh();
    else { generation++; request?.abort(); }
  }
  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => activate(index));
    tab.addEventListener('keydown', event => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1
        : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
      tabs[next].focus(); activate(next);
    });
  });
  const matrix = byId('fleet-overview');
  ['pointerover', 'focusin'].forEach(event => matrix.addEventListener(event, event => {
    const square = event.target.closest('.vm-square');
    if (square) setText(byId('vm-reading'), square.getAttribute('aria-label'));
  }));
  matrix.addEventListener('click', event => {
    const square = event.target.closest('.vm-square');
    if (!square) return;
    activate(1);
    const row = [...document.querySelectorAll('#details-panel tr[data-client-id]')]
      .find(row => row.dataset.clientId === square.dataset.clientId);
    if (row) { row.scrollIntoView({block: 'center'}); row.focus({preventScroll: true}); }
  });
  function selectVm(value) {
    timeWindow = null;
    selected = value; picker.value = value;
    setText(byId('statistics-chart-note'), 'Loading VM history…');
    refresh();
  }
  picker.addEventListener('change', () => selectVm(picker.value));
  metricButtons.forEach(button => button.addEventListener('click', () => {
    metric = button.dataset.metric;
    metricButtons.forEach(item => item.setAttribute('aria-pressed', String(item === button)));
    if (data) renderChart();
  }));

  async function refresh() {
    if (!active) return;
    clearTimeout(timer);
    request?.abort();
    const controller = new AbortController();
    request = controller;
    const current = ++generation;
    const timeout = setTimeout(() => controller.abort(), 5000);
    try {
      const response = await fetch('/statistics' + (selected ? '?vm=' + encodeURIComponent(selected) : ''), {cache: 'no-store', signal: controller.signal});
      if (!response.ok) throw new Error('Statistics unavailable');
      const next = await response.json();
      if (current !== generation || !active) return;
      data = next; selected = data.selected || '';
      renderPicker(); renderTable(); renderChart();
      setText(byId('statistics-update'), 'Updated ' + new Date().toLocaleTimeString());
    } catch (error) {
      if (current === generation && active) setText(byId('statistics-update'), 'Updates interrupted · retrying…');
    } finally {
      clearTimeout(timeout);
      if (current === generation && active) timer = setTimeout(refresh, 2000);
    }
  }
  function renderPicker() {
    const vms = [...data.vms].sort((a, b) => a.name.localeCompare(b.name));
    const signature = JSON.stringify(vms.map(vm => [vm.id, vm.name]));
    if (signature !== optionSignature) {
      optionSignature = signature;
      picker.replaceChildren(new Option('All VMs', ''));
      vms.forEach(vm => picker.add(new Option(vm.name, vm.id)));
    }
    picker.value = selected;
  }
  function renderTable() {
    const body = byId('statistics-rows');
    const focused = document.activeElement.closest('[data-vm]')?.dataset.vm;
    const vms = [...data.vms].sort((a, b) => (b.migration?.last_ms ?? -1) - (a.migration?.last_ms ?? -1) || a.name.localeCompare(b.name));
    setText(byId('statistics-vm-count'), `· ${vms.length}`);
    if (!vms.length) {
      rows.clear();
      const row = document.createElement('tr'), cell = document.createElement('td');
      cell.colSpan = 8; cell.className = 'empty-state';
      cell.textContent = 'No VMs yet. Statistics appear when clients or compute nodes report.';
      row.append(cell); body.replaceChildren(row); return;
    }
    if (!rows.size) body.replaceChildren();
    const ids = new Set(vms.map(vm => vm.id));
    for (const [id, row] of rows) if (!ids.has(id)) { row.remove(); rows.delete(id); }
    vms.forEach((vm, index) => {
      let row = rows.get(vm.id);
      if (!row) {
        row = document.createElement('tr');
        const name = document.createElement('th'); name.scope = 'row'; name.className = 'vm-cell';
        const button = document.createElement('button'); button.type = 'button'; button.className = 'vm-name'; button.dataset.vm = vm.id;
        button.addEventListener('click', () => selectVm(vm.id));
        const node = document.createElement('span'); node.className = 'vm-node';
        name.append(button, node); row.append(name);
        for (let i = 0; i < 7; i++) { const cell = document.createElement('td'); cell.className = 'numeric'; row.append(cell); }
        row.children[1].classList.add('last-downtime'); rows.set(vm.id, row);
      }
      row.classList.toggle('selected', vm.id === selected);
      const button = row.querySelector('button'); setText(button, vm.name); button.title = vm.vm_uuid || vm.name;
      button.setAttribute('aria-pressed', String(vm.id === selected));
      const summary = vm.migration;
      let node = summary?.node || 'Node unknown';
      if (summary?.node_stale && summary.node) node += ' · report stale';
      if (!vm.connected) node += ' · no recent heartbeat';
      setText(row.querySelector('.vm-node'), node);
      const cells = [duration(summary?.last_ms), duration(summary?.min_ms), duration(summary?.avg_ms), duration(summary?.max_ms), String(summary?.count || 0), percent(vm.cpu_percent), percent(vm.memory_percent)];
      cells.forEach((value, i) => setText(row.children[i + 1], value));
      row.children[1].title = summary?.last_at ? dateTime(summary.last_at) : 'No completed migration reported';
      if (body.children[index] !== row) body.insertBefore(row, body.children[index] || null);
    });
    if (focused && rows.has(focused)) rows.get(focused).querySelector('button').focus({preventScroll: true});
  }
  function svgElement(tag, attrs = {}, text = '') {
    const element = document.createElementNS(ns, tag);
    Object.entries(attrs).forEach(([key, value]) => element.setAttribute(key, value));
    if (text) element.textContent = text;
    return element;
  }
  function usagePoints(vms) {
    const times = new Map();
    vms.forEach(vm => vm.history.forEach(sample => {
      const value = sample[metric];
      if (value == null) return;
      if (!times.has(sample.at)) times.set(sample.at, []);
      times.get(sample.at).push(value);
    }));
    return [...times].sort((a, b) => a[0] - b[0]).map(([at, values]) => ({at, value: values.reduce((sum, v) => sum + v, 0) / values.length, min: Math.min(...values), max: Math.max(...values), count: values.length, key: String(Math.round(at))}));
  }
  const rangeControls = ['start', 'end', 'window'].map(name => byId('migration-time-' + name));
  const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
  function changeRange(kind, value, initial) {
    const [min, max] = rangeDomain;
    const gap = (max - min) / 1000;
    let [start, end] = initial;
    if (kind === 'start') start = clamp(value, min, end - gap);
    else if (kind === 'end') end = clamp(value, start + gap, max);
    else {
      const width = end - start;
      start = clamp(value, min, max - width); end = start + width;
    }
    timeWindow = start <= min && end >= max ? null : [start, end];
    renderChart();
  }
  rangeControls.forEach(control => {
    const kind = control.id.replace('migration-time-', '');
    control.addEventListener('pointerdown', event => {
      if (control.disabled || event.button !== 0 || !rangeDomain) return;
      event.preventDefault(); control.focus({preventScroll: true});
      control.setPointerCapture(event.pointerId);
      rangeDrag = {kind, x: event.clientX, initial: [...(timeWindow || rangeDomain)],
        domain: [...rangeDomain], width: byId('migration-time-track').getBoundingClientRect().width};
    });
    control.addEventListener('pointermove', event => {
      if (!rangeDrag || !control.hasPointerCapture(event.pointerId)) return;
      const drag = rangeDrag;
      const offset = (event.clientX - drag.x) / drag.width * (drag.domain[1] - drag.domain[0]);
      changeRange(drag.kind, drag.initial[drag.kind === 'end' ? 1 : 0] + offset, drag.initial);
    });
    const finish = () => { if (rangeDrag) { rangeDrag = null; if (data) renderChart(); } };
    control.addEventListener('lostpointercapture', finish);
    control.addEventListener('pointercancel', finish);
    control.addEventListener('keydown', event => {
      if (!rangeDomain || !['ArrowLeft', 'ArrowRight', 'ArrowDown', 'ArrowUp', 'Home', 'End', 'PageUp', 'PageDown'].includes(event.key)) return;
      event.preventDefault();
      const initial = timeWindow || rangeDomain;
      const step = (rangeDomain[1] - rangeDomain[0]) / 100 * (event.key.startsWith('Page') || event.shiftKey ? 10 : 1);
      const direction = ['ArrowLeft', 'ArrowDown', 'PageDown'].includes(event.key) ? -1 : 1;
      const value = event.key === 'Home' ? rangeDomain[0] : event.key === 'End' ? rangeDomain[1]
        : initial[kind === 'end' ? 1 : 0] + direction * step;
      changeRange(kind, value, initial);
    });
  });
  byId('migration-time-reset').addEventListener('click', () => { timeWindow = null; renderChart(); });
  function renderTimeRange(points) {
    const visible = (metric === 'migration' || metric === 'iterations') && points.length > 0;
    byId('migration-time-range').hidden = !visible;
    if (!visible) return null;
    rangeDomain = rangeDrag?.domain || [points[0].at, points.at(-1).at];
    const [min, max] = rangeDomain, span = max - min;
    if (timeWindow && !rangeDrag) {
      const width = Math.min(timeWindow[1] - timeWindow[0], span);
      const start = clamp(timeWindow[0], min, max - width);
      timeWindow = width === span ? null : [start, start + width];
    }
    const bounds = timeWindow || rangeDomain;
    const [start, end] = bounds;
    const left = span ? (start - min) / span * 100 : 0;
    const right = span ? (end - min) / span * 100 : 100;
    rangeControls[0].style.left = left + '%';
    rangeControls[1].style.left = right + '%';
    rangeControls[2].style.left = left + '%';
    rangeControls[2].style.width = (right - left) + '%';
    rangeControls.forEach((control, index) => {
      control.disabled = !span || (index === 2 && !timeWindow);
      control.setAttribute('aria-valuemin', index === 1 ? start : min);
      control.setAttribute('aria-valuemax', index === 0 ? end : index === 2 ? max - (end - start) : max);
      control.setAttribute('aria-valuenow', index === 1 ? end : start);
      control.setAttribute('aria-valuetext', index === 2 ? `${dateTime(start * 1000)} to ${dateTime(end * 1000)}` : dateTime(bounds[index] * 1000));
    });
    setText(byId('migration-time-label'), `${dateTime(start * 1000)} — ${dateTime(end * 1000)}`);
    byId('migration-time-reset').disabled = !timeWindow;
    const signature = points.map(point => point.at).join(',') + ':' + rangeDomain.join(',');
    if (signature !== rangeSignature) {
      rangeSignature = signature;
      byId('migration-time-events').replaceChildren(...points.map(point => {
        const tick = document.createElement('i'); tick.style.left = (span ? (point.at - min) / span * 100 : 50) + '%'; return tick;
      }));
    }
    return bounds;
  }
  function renderChart() {
    const chosen = data.vms.find(vm => vm.id === selected);
    const vms = chosen ? [chosen] : data.vms;
    const iterations = metric === 'iterations';
    const migration = metric === 'migration' || iterations;
    const format = iterations ? value => `${number(value)} iterations` : duration;
    const names = new Map(data.vms.map(vm => [vm.vm_uuid, vm.name]));
    const available = migration ? data.events.filter(event => !iterations || event.iterations != null).map(event => ({at: Date.parse(event.at) / 1000, value: iterations ? event.iterations : event.downtime_ms, name: names.get(event.vm_uuid) || event.vm_uuid, key: event.vm_uuid + event.at})) : usagePoints(vms);
    const bounds = renderTimeRange(available);
    const points = migration && bounds ? available.filter(point => point.at >= bounds[0] && point.at <= bounds[1]) : available;
    const label = iterations ? 'Migration iterations' : migration ? 'Migration downtime' : metric === 'cpu_percent' ? 'CPU usage' : 'Memory usage';
    const total = vms.reduce((sum, vm) => sum + (vm.migration?.count || 0), 0);
    setText(byId('statistics-chart-title'), label + ' over time');
    const scope = chosen ? chosen.name : 'All VMs';
    setText(byId('statistics-chart-note'), migration
      ? `${scope} · ${timeWindow ? `${points.length} in selected interval · ` : ''}${iterations ? `${available.length} migrations with iteration counts · before switchover` : `${available.length < total ? `latest ${available.length} of ${total}` : available.length} completed migrations`}${timeWindow ? ' available' : ' · each point is one migration'}${iterations && available.length < data.events.length ? ` · ${data.events.length - available.length} without counts` : ''}`
      : `${scope} · last 60 seconds${chosen ? '' : ' · average line, min–max band'}`);
    setText(byId('statistics-reading'), points.length ? (migration ? format(points.at(-1).value) : percent(points.at(-1).value)) : '—');
    byId('statistics-reading').title = migration ? 'Most recent migration in this chart' : 'Most recent sample in this chart';
    const previousPoint = chart.querySelector(':focus')?.dataset.key;
    chart.replaceChildren();
    setText(byId('statistics-point'), 'Hover or focus a point to inspect it.');
    if (!points.length) {
      const empty = document.createElement('div'); empty.className = 'empty-state';
      const heading = document.createElement('strong'); heading.textContent = migration ? (available.length ? 'No migrations in this interval' : iterations ? 'No migration iteration counts yet' : 'No completed migrations yet') : 'No recent usage samples';
      const hint = document.createElement('span'); hint.textContent = migration ? (available.length ? 'Expand the interval or choose Full range to see more migrations.' : iterations ? 'Update the compute-node reporter to collect precopy iterations from migration logs.' : 'Downtime history appears when compute nodes report completed migrations.') : 'Usage appears when guest clients send heartbeats. Only the last 60 seconds are retained.';
      empty.append(heading, hint); chart.append(empty); setText(byId('statistics-point'), ''); return;
    }
    const width = Math.max(280, chart.clientWidth), height = 245;
    const left = 62, right = width - 12, top = 12, bottom = height - 35;
    let start = migration ? bounds[0] : data.now - 60;
    let end = migration ? bounds[1] : data.now;
    if (start === end) { start -= 60; end += 60; }
    const maximum = migration ? Math.max(1, ...points.map(point => point.value)) : 100;
    const power = 10 ** Math.floor(Math.log10(maximum));
    const ymax = migration ? Math.ceil(maximum / power) * power : 100;
    const x = at => left + (at - start) / (end - start) * (right - left);
    const y = value => bottom - value / ymax * (bottom - top);
    const svg = svgElement('svg', {viewBox: `0 0 ${width} ${height}`, role: 'group', 'aria-label': `${label}, ${scope}`, class: metric === 'memory_percent' ? 'memory' : ''});
    const yTicks = iterations ? Math.min(4, ymax) : 4;
    for (let i = 0; i <= yTicks; i++) {
      const value = iterations ? Math.round(ymax * i / yTicks) : ymax * i / yTicks;
      svg.append(svgElement('line', {x1: left, y1: y(value), x2: right, y2: y(value), class: 'chart-grid'}));
      svg.append(svgElement('text', {x: left - 9, y: y(value) + 4, 'text-anchor': 'end'}, number(value) + (iterations ? '' : migration ? ' ms' : '%')));
    }
    const ticks = width < 500 ? 2 : 4;
    for (let i = 0; i <= ticks; i++) {
      const at = start + (end - start) * i / ticks;
      const date = new Date(at * 1000);
      const text = migration ? date.toLocaleString(undefined, end - start > 86400 ? {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'} : {hour: '2-digit', minute: '2-digit', second: '2-digit'}) : (i === ticks ? 'Now' : `${Math.round(data.now - at)}s ago`);
      svg.append(svgElement('text', {x: x(at), y: height - 8, 'text-anchor': i === 0 ? 'start' : i === ticks ? 'end' : 'middle'}, text));
    }
    if (!migration) {
      const groups = [];
      points.forEach(point => {
        if (!groups.length || point.at - groups.at(-1).at(-1).at > 1.5) groups.push([]);
        groups.at(-1).push(point);
      });
      groups.forEach(group => {
        if (!chosen) svg.append(svgElement('polygon', {points: [...group.map(p => `${x(p.at)},${y(p.max)}`), ...[...group].reverse().map(p => `${x(p.at)},${y(p.min)}`)].join(' '), class: 'plot-band'}));
        svg.append(svgElement('polyline', {points: group.map(p => `${x(p.at)},${y(p.value)}`).join(' '), class: 'plot-line'}));
      });
    }
    points.forEach(point => {
      if (migration) svg.append(svgElement('line', {x1: x(point.at), x2: x(point.at), y1: bottom, y2: y(point.value), class: 'plot-stem'}));
      const details = migration ? `${point.name} · ${dateTime(point.at * 1000)} · ${iterations ? format(point.value) + ' before switchover' : 'downtime ' + duration(point.value)}` : `${dateTime(point.at * 1000)} · ${label} ${percent(point.value)}${chosen ? '' : ` · ${point.count} VMs · range ${percent(point.min)}–${percent(point.max)}`}`;
      const circle = svgElement('circle', {cx: x(point.at), cy: y(point.value), r: migration ? 4.5 : 3, class: 'plot-point', tabindex: 0, role: 'button', 'aria-label': details, 'data-key': point.key});
      circle.append(svgElement('title', {}, details));
      ['mouseenter', 'focus', 'click'].forEach(name => circle.addEventListener(name, () => setText(byId('statistics-point'), details)));
      circle.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); setText(byId('statistics-point'), details); } });
      svg.append(circle);
    });
    chart.append(svg);
    if (previousPoint) [...svg.querySelectorAll('.plot-point')].find(point => point.dataset.key === previousPoint)?.focus({preventScroll: true});
  }
  let previousWidth = 0;
  new ResizeObserver(entries => {
    const width = Math.round(entries[0].contentRect.width);
    if (width && width !== previousWidth) { previousWidth = width; if (active && data) renderChart(); }
  }).observe(chart);
})();
