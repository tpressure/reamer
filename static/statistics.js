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
  function renderChart() {
    const chosen = data.vms.find(vm => vm.id === selected);
    const vms = chosen ? [chosen] : data.vms;
    const migration = metric === 'migration';
    const names = new Map(data.vms.map(vm => [vm.vm_uuid, vm.name]));
    const points = migration ? data.events.map(event => ({at: Date.parse(event.at) / 1000, value: event.downtime_ms, name: names.get(event.vm_uuid) || event.vm_uuid, key: event.vm_uuid + event.at})) : usagePoints(vms);
    const label = migration ? 'Migration downtime' : metric === 'cpu_percent' ? 'CPU usage' : 'Memory usage';
    const total = vms.reduce((sum, vm) => sum + (vm.migration?.count || 0), 0);
    setText(byId('statistics-chart-title'), label + ' over time');
    const scope = chosen ? chosen.name : 'All VMs';
    setText(byId('statistics-chart-note'), migration
      ? `${scope} · ${points.length < total ? `latest ${points.length} of ${total}` : points.length} completed migrations · each point is one migration`
      : `${scope} · last 60 seconds${chosen ? '' : ' · average line, min–max band'}`);
    setText(byId('statistics-reading'), points.length ? (migration ? duration(points.at(-1).value) : percent(points.at(-1).value)) : '—');
    byId('statistics-reading').title = migration ? 'Most recent migration in this chart' : 'Most recent sample in this chart';
    const previousPoint = chart.querySelector(':focus')?.dataset.key;
    chart.replaceChildren();
    setText(byId('statistics-point'), 'Hover or focus a point to inspect it.');
    if (!points.length) {
      const empty = document.createElement('div'); empty.className = 'empty-state';
      const heading = document.createElement('strong'); heading.textContent = migration ? 'No completed migrations yet' : 'No recent usage samples';
      const hint = document.createElement('span'); hint.textContent = migration ? 'Downtime history appears when compute nodes report completed migrations.' : 'Usage appears when guest clients send heartbeats. Only the last 60 seconds are retained.';
      empty.append(heading, hint); chart.append(empty); setText(byId('statistics-point'), ''); return;
    }
    const width = Math.max(280, chart.clientWidth), height = 245;
    const left = 62, right = width - 12, top = 12, bottom = height - 35;
    let start = migration ? points[0].at : data.now - 60;
    let end = migration ? points.at(-1).at : data.now;
    if (start === end) { start -= 60; end += 60; }
    const maximum = migration ? Math.max(1, ...points.map(point => point.value)) : 100;
    const power = 10 ** Math.floor(Math.log10(maximum));
    const ymax = migration ? Math.ceil(maximum / power) * power : 100;
    const x = at => left + (at - start) / (end - start) * (right - left);
    const y = value => bottom - value / ymax * (bottom - top);
    const svg = svgElement('svg', {viewBox: `0 0 ${width} ${height}`, role: 'group', 'aria-label': `${label}, ${scope}`, class: metric === 'memory_percent' ? 'memory' : ''});
    for (let i = 0; i <= 4; i++) {
      const value = ymax * i / 4;
      svg.append(svgElement('line', {x1: left, y1: y(value), x2: right, y2: y(value), class: 'chart-grid'}));
      svg.append(svgElement('text', {x: left - 9, y: y(value) + 4, 'text-anchor': 'end'}, number(value) + (migration ? ' ms' : '%')));
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
      const details = migration ? `${point.name} · ${dateTime(point.at * 1000)} · downtime ${duration(point.value)}` : `${dateTime(point.at * 1000)} · ${label} ${percent(point.value)}${chosen ? '' : ` · ${point.count} VMs · range ${percent(point.min)}–${percent(point.max)}`}`;
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
