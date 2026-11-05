/* Dyson Pi dashboard: vanilla JS, no build step. */
'use strict';

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
let S = null;            // latest status snapshot
let sinceBase = null;    // {since: Date, label}
let eventsOldest = null;

// ------------------------------------------------------------------ helpers
async function api(path, { method = 'GET', body, allow409 = true } = {}) {
  const opts = { method, headers: { 'X-Requested-With': 'dyson-pi' } };
  if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  const r = await fetch(path, opts);
  if (r.status === 401) { location.href = '/login'; throw new Error('not logged in'); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok && !(allow409 && r.status === 409)) {
    const d = data.detail;
    throw new Error(typeof d === 'string' ? d : Array.isArray(d) ? d.map((x) => x.msg).join('; ') : `HTTP ${r.status}`);
  }
  return data;
}

function toast(msg, ms = 3500) {
  const t = $('#toast');
  t.textContent = msg; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => { t.hidden = true; }, ms);
}

function fmtDur(sec) {
  if (sec == null) return '';
  sec = Math.max(0, Math.round(sec));
  const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${s}s`;
  return `${s}s`;
}

function fmtTime(iso, withDay = true) {
  if (!iso) return '–';
  const d = new Date(iso);
  const opts = { hour: '2-digit', minute: '2-digit' };
  if (withDay) Object.assign(opts, { weekday: 'short', day: 'numeric', month: 'short' });
  return d.toLocaleString(undefined, opts);
}

function relTime(iso) {
  if (!iso) return '';
  const diff = (new Date(iso) - Date.now()) / 1000;
  return diff >= 0 ? `in ${fmtDur(diff)}` : `${fmtDur(-diff)} ago`;
}

const OUTCOME_LABEL = {
  success: 'Started ✔', completed: 'Delivered', failed: 'FAILED', running: 'Running…',
  refused_not_present: 'Refused', blocked_disabled: 'Blocked (disabled)', blocked_cooldown: 'Cooldown',
  blocked_busy: 'Busy', blocked_daily_limit: 'Daily limit', aborted: 'Aborted', error: 'Error',
};
const outcomeClass = (o) => (o.startsWith('blocked') ? 'blocked' : o);
const outcomeHtml = (o) => `<span class="outcome ${outcomeClass(o)}">${esc(OUTCOME_LABEL[o] || o)}</span>`;

// ------------------------------------------------------------------ tabs
function showTab(name) {
  $$('.tabs button').forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
  $$('.tab').forEach((t) => t.classList.toggle('active', t.id === `tab-${name}`));
  try { localStorage.setItem('tab', name); } catch (_) { /* ignore */ }
  ({ schedules: loadSchedules, history: loadHistory, maint: loadMaint, tools: loadTools })[name]?.();
  window.scrollTo(0, 0);
}
$$('.tabs button').forEach((b) => b.addEventListener('click', () => showTab(b.dataset.tab)));

// ------------------------------------------------------------------ status rendering
function renderStatus(s) {
  S = s;
  const r = s.robot, a = s.actuator;
  $('#clock').textContent = s.system.local_time.slice(11, 16) + ' ' + s.system.timezone.split('/').pop();

  const card = $('#robot-card');
  card.className = `card robot ${r.state}`;
  $('#robot-state').textContent = { docked: 'Docked', away: 'Away cleaning', unknown: 'Unknown' }[r.state];
  sinceBase = r.since ? new Date(r.since) : null;
  tickSince();
  let hint = '';
  if (r.settling) hint = `Sensor settling → ${r.settling === 'absent' ? 'away' : 'docked'}… `;
  if (r.state === 'docked') {
    if (r.predicted_asleep === true) hint += '💤 Probably asleep: smart start will wake it first.';
    else if (r.predicted_asleep === false) hint += `Probably awake (sleeps in ~${Math.round(r.minutes_until_sleep)} min).`;
    else hint += 'Sleep state unknown (no recent activity recorded).';
  }
  $('#robot-hint').textContent = hint;

  const blocked = a.disabled || a.busy;
  $$('[data-press]').forEach((b) => {
    const needsDock = b.dataset.press !== 'force';
    b.disabled = blocked || (needsDock && r.state !== 'docked');
  });

  // attempt
  const att = a.current || s.last_attempt;
  $('#attempt').innerHTML = att ? `
    <div>${outcomeHtml(att.outcome)} · ${esc(att.mode)} · ${esc(att.trigger)}${att.dry_run ? ' · <b>dry run</b>' : ''}</div>
    <div class="small">${esc(att.message || '')}</div>
    <div class="small muted">${fmtTime(att.started_at)} · ${att.presses} press${att.presses === 1 ? '' : 'es'}</div>`
    : '<span class="muted">No attempts yet</span>';

  const relay = $('#st-relay');
  relay.textContent = s.relay.on ? 'ON' : s.relay.dry_run_pulse ? 'dry' : 'off';
  relay.classList.toggle('on', s.relay.on);
  $('#st-temp').textContent = s.system.cpu_temp_c != null ? `${s.system.cpu_temp_c.toFixed(0)}°C` : 'n/a';
  $('#st-today').textContent = `${a.presses_today}/${a.daily_limit}`;
  $('#st-ip').textContent = s.system.ip || '–';

  const up = s.schedules.upcoming.filter((x) => x.enabled).slice(0, 3);
  $('#home-schedules').innerHTML = up.length ? up.map((x) => `
    <li><div class="grow"><b>${esc(x.name)}</b><div class="small muted">${esc(x.days_label)} ${esc(x.time)} · ${esc(x.mode)}</div></div>
    <div class="small">${fmtTime(x.next_run)}<br><span class="muted">${relTime(x.next_run)}</span></div></li>`).join('')
    : '<li class="muted">No active schedules</li>';

  if (s.simulator) {
    $('#sim-card').hidden = false;
    $('#sim-auto').checked = s.simulator.enabled;
    $('#sim-awake').checked = s.simulator.awake;
    $('#sim-state').textContent = `Simulated robot: ${s.simulator.docked ? 'docked' : 'away'}, ${s.simulator.awake ? 'awake' : 'asleep'}`;
  }

  $('#disable-toggle').checked = a.disabled;
  $('#maint-badge').hidden = !s.maintenance.due;
  renderBanners(s);
  if ($('#tab-tools').classList.contains('active')) renderSysinfo(s);
}

function tickSince() {
  $('#robot-since').textContent = sinceBase ? `for ${fmtDur((Date.now() - sinceBase) / 1000)}` : '';
}
setInterval(tickSince, 1000);

function renderBanners(s) {
  const out = [];
  const b = (cls, html, action) => out.push(`<div class="banner ${cls}"><span>${html}</span>${action || ''}</div>`);
  if (s.alert) b('error', `⚠ Start failed: ${esc(s.alert.message)}`, '<button class="btn tiny" id="alert-dismiss">Dismiss</button>');
  if (s.actuator.disabled) b('error', '⛔ Actuator DISABLED: all presses are blocked');
  for (const f of s.robot.sensor_faults) b('warn', `Sensor fault: ${esc(f)}`);
  if (s.robot.state === 'away') b('info', 'Robot is away (probably cleaning). Presses are blocked; Force press overrides.');
  if (s.robot.state === 'unknown') b('warn', 'Robot presence unknown: waiting for a stable sensor reading.');
  if (s.actuator.dry_run) b('info', 'DRY-RUN mode (config): the relay is never energized.');
  if (s.schedules.waiting_for_ntp) b('warn', '⏱ Waiting for NTP time sync: schedules are paused.');
  if (s.actuator.cooldown_remaining_s > 0) b('info', `Cooldown ${s.actuator.cooldown_remaining_s.toFixed(0)} s`);
  if (s.maintenance.due) b('warn', `🛠 ${s.maintenance.due} maintenance item(s) due`, '<button class="btn tiny" data-goto="maint">Open</button>');
  $('#banners').innerHTML = out.join('');
  $('#alert-dismiss')?.addEventListener('click', async () => { await api('/api/alert/dismiss', { method: 'POST' }); refresh(); });
  $$('[data-goto]').forEach((x) => x.addEventListener('click', () => showTab(x.dataset.goto)));
}

async function refresh() {
  try { renderStatus(await api('/api/status')); } catch (e) { console.warn(e); }
}

// ------------------------------------------------------------------ actions
async function press(mode, { dry = false } = {}) {
  if (mode === 'force' && !confirm('Force press ignores the presence sensor. If the robot is cleaning this may pause it. Press anyway?')) return;
  try {
    const a = await api(`/api/press?mode=${mode}${dry ? '&dry_run=true' : ''}`, { method: 'POST' });
    toast(a.outcome === 'running' ? `${mode} sequence running…` : `${OUTCOME_LABEL[a.outcome] || a.outcome}: ${a.message}`);
  } catch (e) { toast(`Error: ${e.message}`); }
  refresh();
}
$$('[data-press]').forEach((b) => b.addEventListener('click', () => press(b.dataset.press)));

$('#disable-toggle').addEventListener('change', async (e) => {
  const want = e.target.checked;
  if (!want && !confirm('Re-enable the actuator?')) { e.target.checked = true; return; }
  try { await api('/api/actuator/disable', { method: 'POST', body: { disabled: want } }); } catch (ex) { toast(ex.message); }
  refresh();
});

$('#sim-dock').addEventListener('click', () => api('/api/sim/presence', { method: 'POST', body: { present: true } }).then(refresh));
$('#sim-away').addEventListener('click', () => api('/api/sim/presence', { method: 'POST', body: { present: false } }).then(refresh));
$('#sim-auto').addEventListener('change', (e) => api('/api/sim/auto', { method: 'POST', body: { enabled: e.target.checked } }).then(refresh));
$('#sim-awake').addEventListener('change', (e) => api('/api/sim/auto', { method: 'POST', body: { enabled: $('#sim-auto').checked, awake: e.target.checked } }).then(refresh));

// ------------------------------------------------------------------ schedules
$('#sf-days').innerHTML = DAYS.map((d, i) => `<label><input type="checkbox" value="${i}" ${i < 5 ? 'checked' : ''}><span>${d}</span></label>`).join('');

async function loadSchedules() {
  const d = await api('/api/schedules');
  $('#sched-tz').textContent = `(${d.timezone})${d.waiting_for_ntp ? ' · waiting for NTP' : ''}`;
  $('#schedule-list').innerHTML = d.schedules.length ? d.schedules.map((x) => `
    <li data-id="${x.id}">
      <input type="checkbox" class="sch-en" ${x.enabled ? 'checked' : ''} aria-label="enabled">
      <div class="grow"><b>${esc(x.time)} · ${esc(x.name)}</b>
        <div class="small muted">${esc(x.days_label)} · ${esc(x.mode)}</div>
        <div class="small">${x.enabled ? `Next: ${fmtTime(x.next_run)} (${relTime(x.next_run)})` : 'Disabled'}</div></div>
      <button class="btn tiny sch-edit">Edit</button>
      <button class="btn tiny danger sch-del">✕</button>
    </li>`).join('') : '<li class="muted">No schedules yet. Add one below.</li>';
  $$('#schedule-list li[data-id]').forEach((li) => {
    const id = li.dataset.id, x = d.schedules.find((s) => String(s.id) === id);
    $('.sch-en', li).addEventListener('change', async (e) => {
      await api(`/api/schedules/${id}`, { method: 'PATCH', body: { enabled: e.target.checked } }); loadSchedules(); refresh();
    });
    $('.sch-del', li).addEventListener('click', async () => {
      if (!confirm(`Delete schedule "${x.name}"?`)) return;
      await api(`/api/schedules/${id}`, { method: 'DELETE' }); loadSchedules(); refresh();
    });
    $('.sch-edit', li).addEventListener('click', () => {
      $('#sf-id').value = id; $('#sf-name').value = x.name; $('#sf-time').value = x.time; $('#sf-mode').value = x.mode;
      $$('#sf-days input').forEach((c) => { c.checked = x.days.includes(Number(c.value)); });
      $('#schedule-form-title').textContent = 'Edit schedule';
      $('#schedule-form').scrollIntoView({ behavior: 'smooth' });
    });
  });
}

function resetScheduleForm() {
  $('#schedule-form').reset(); $('#sf-id').value = '';
  $$('#sf-days input').forEach((c, i) => { c.checked = i < 5; });
  $('#schedule-form-title').textContent = 'Add schedule';
}
$('#sf-cancel').addEventListener('click', resetScheduleForm);
$('#schedule-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const body = {
    name: $('#sf-name').value || 'Cleaning', time: $('#sf-time').value, mode: $('#sf-mode').value,
    days: $$('#sf-days input:checked').map((c) => Number(c.value)),
  };
  if (!body.days.length) { toast('Pick at least one day'); return; }
  const id = $('#sf-id').value;
  try {
    await api(id ? `/api/schedules/${id}` : '/api/schedules', { method: id ? 'PATCH' : 'POST', body });
    toast('Schedule saved'); resetScheduleForm(); loadSchedules(); refresh();
  } catch (ex) { toast(ex.message); }
});

// ------------------------------------------------------------------ history
function eventLi(e) {
  return `<li class="lvl-${esc(e.level)}"><div class="grow">${esc(e.message)}<div class="small muted">${fmtTime(e.ts)} · ${esc(e.kind)}</div></div></li>`;
}
async function loadHistory() {
  const [runs, atts, evs] = await Promise.all([api('/api/runs?limit=30'), api('/api/attempts?limit=30'), api('/api/events?limit=50')]);
  $('#runs').innerHTML = runs.length ? runs.map((r) => `
    <li><div class="grow"><b>${r.departed_at ? fmtTime(r.departed_at) : 'unknown start'}</b>
      <div class="small muted">${r.returned_at ? `returned ${fmtTime(r.returned_at, false)}` : 'in progress'} · ${esc(r.trigger)}${r.approximate ? ' · approx.' : ''}</div></div>
      <div>${r.duration_s != null ? fmtDur(r.duration_s) : r.returned_at ? '?' : '…'}</div></li>`).join('')
    : '<li class="muted">No cleaning runs recorded yet</li>';
  $('#attempts').innerHTML = atts.length ? atts.map((a) => `
    <li><div class="grow">${outcomeHtml(a.outcome)} · ${esc(a.mode)}${a.dry_run ? ' (dry)' : ''}
      <div class="small">${esc(a.message)}</div>
      <div class="small muted">${fmtTime(a.started_at)} · ${esc(a.trigger)} · ${a.presses} press(es)</div></div></li>`).join('')
    : '<li class="muted">No attempts yet</li>';
  $('#events').innerHTML = evs.map(eventLi).join('');
  eventsOldest = evs.length ? evs[evs.length - 1].id : null;
}
$('#events-more').addEventListener('click', async () => {
  if (eventsOldest == null) return;
  const evs = await api(`/api/events?limit=50&before=${eventsOldest}`);
  $('#events').insertAdjacentHTML('beforeend', evs.map(eventLi).join(''));
  if (evs.length) eventsOldest = evs[evs.length - 1].id; else toast('No older events');
});

// ------------------------------------------------------------------ maintenance
async function loadMaint() {
  const m = await api('/api/maintenance');
  const c = m.counters;
  const item = (label, v) => `<div><span class="label">${label}</span><b>${esc(v)}</b></div>`;
  $('#counters').innerHTML = [
    item('Cylinder actuations', c.cylinder_actuations), item('Relay cycles', c.relay_cycles),
    item('Successful starts', c.successful_starts), item('Failed starts', c.failed_starts),
    item('Cleaning runs', c.cleaning_runs), item('Fan run hours', c.fan_hours),
    item('Last successful start', c.last_successful_start ? fmtTime(c.last_successful_start) : '–'),
  ].join('');
  $('#reminders').innerHTML = m.reminders.map((r) => `
    <li><div class="grow"><b>${r.due ? '⚠ ' : ''}${esc(r.title)}</b>
      <div class="small muted">${r.used} / ${r.every} ${esc(r.unit)}${r.last_serviced_at ? ` · serviced ${fmtTime(r.last_serviced_at)}` : ''}</div>
      <div class="progress ${r.due ? 'due' : ''}"><i style="width:${Math.round(r.progress * 100)}%"></i></div></div>
      <button class="btn tiny" data-serviced="${esc(r.id)}">Mark serviced</button></li>`).join('');
  $$('[data-serviced]').forEach((b) => b.addEventListener('click', async () => {
    const note = prompt('Optional note (e.g. what you did):', '') ?? null;
    if (note === null) return;
    await api(`/api/maintenance/${b.dataset.serviced}/serviced`, { method: 'POST', body: { note } });
    toast('Marked serviced'); loadMaint(); refresh();
  }));
  $('#service-log').innerHTML = m.service_log.length ? m.service_log.map((l) => `
    <li><div class="grow"><b>${esc(l.reminder_id)}</b><div class="small muted">${fmtTime(l.ts)} at ${Math.round(l.counter_value)}${l.note ? ' · ' + esc(l.note) : ''}</div></div></li>`).join('')
    : '<li class="muted">Nothing serviced yet</li>';
}

// ------------------------------------------------------------------ tools
const CAL_META = {
  'press.press_duration_s': ['Press duration', 's', 0.05, 'How long the relay holds the button'],
  'press.inter_press_delay_s': ['Inter-press delay', 's', 0.1, 'Gap between wake and start presses (double press)'],
  'press.departure_timeout_s': ['Departure timeout', 's', 1, 'How long to wait for the robot to leave after a press'],
  'press.max_presses': ['Max presses', '', 1, 'Smart start gives up after this many presses'],
  'sensor.debounce_ms': ['Debounce', 'ms', 10, 'Raw contact must be steady this long'],
  'sensor.stability_s': ['Stability', 's', 0.5, 'State must hold this long before it counts'],
};

async function loadTools() {
  if (S) renderSysinfo(S);
  const c = await api('/api/calibration');
  $('#cal-fields').innerHTML = Object.entries(c.fields).map(([k, f]) => {
    const [label, unit, step, hint] = CAL_META[k];
    return `<div class="cal-row"><label for="cal-${k}">${label}${unit ? ` (${unit})` : ''} ${f.overridden ? '<span class="badge on">custom</span>' : ''}</label>
      <input id="cal-${k}" data-key="${k}" type="number" step="${step}" value="${f.value}">
      <div class="hint">${hint}. config.yaml: ${f.file_value}${k === 'press.press_duration_s' ? ` · hard max ${c.hard_max_on_s}s` : ''}</div></div>`;
  }).join('');
  loadTokens();
}

function renderSysinfo(s) {
  const y = s.system;
  $('#sysinfo').innerHTML = `
    <div>Host <b>${esc(y.hostname)}</b> · IP <b>${esc(y.ip || '–')}</b> · Wi-Fi <b>${esc(y.wifi || '–')}</b></div>
    <div>Uptime ${fmtDur(y.uptime_s)} · CPU ${y.cpu_temp_c != null ? y.cpu_temp_c.toFixed(1) + '°C' : 'n/a'} · backend <b>${esc(s.relay.backend)}</b></div>
    <div>Version ${esc(y.version)} · service started ${fmtTime(y.started_at)}</div>`;
}

$('#cal-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const values = {};
  $$('#cal-fields input').forEach((i) => { values[i.dataset.key] = Number(i.value); });
  try { await api('/api/calibration', { method: 'PUT', body: { values } }); toast('Calibration saved'); loadTools(); } catch (ex) { toast(ex.message, 6000); }
});
$('#cal-reset').addEventListener('click', async () => {
  if (!confirm('Reset calibration to config.yaml values?')) return;
  await api('/api/calibration', { method: 'DELETE' }); toast('Calibration reset'); loadTools();
});
$('#cal-test').addEventListener('click', () => press('single'));
$('#t-smart').addEventListener('click', () => press('smart'));
$('#t-smart-dry').addEventListener('click', () => press('smart', { dry: true }));
$('#t-relay').addEventListener('click', async () => {
  if (!confirm('Click the relay with a short pulse? The cylinder will move.')) return;
  try { const a = await api('/api/test/relay', { method: 'POST' }); toast(`${OUTCOME_LABEL[a.outcome] || a.outcome}: ${a.message}`); } catch (e) { toast(e.message); }
  refresh();
});
$('#t-sensor').addEventListener('click', () => {
  const box = $('#sensor-live'); box.hidden = false;
  const end = Date.now() + 30000;
  clearInterval(box._t);
  const step = async () => {
    const left = Math.max(0, Math.round((end - Date.now()) / 1000));
    try {
      const s = await api('/api/status'); const r = s.robot;
      $('#sl-raw').textContent = r.raw_present == null ? '–' : r.raw_present ? 'present' : 'absent';
      $('#sl-pending').textContent = r.settling ? (r.settling === 'absent' ? 'away' : 'docked') : '–';
      $('#sl-state').textContent = r.state;
    } catch (_) { /* ignore */ }
    $('#sl-left').textContent = left ? `Move the robot on/off the dock and watch… ${left}s` : 'Done';
    if (!left) clearInterval(box._t);
  };
  step(); box._t = setInterval(step, 500);
});

async function loadTokens() {
  const t = await api('/api/tokens');
  $('#tokens').innerHTML = t.map((x) => `<li><div class="grow"><b>${esc(x.name)}</b><div class="small muted">created ${fmtTime(x.created_at)} · last used ${x.last_used ? fmtTime(x.last_used) : 'never'}</div></div>
    ${x.name === 'cli-local' ? '<span class="badge">CLI</span>' : `<button class="btn tiny danger" data-revoke="${x.id}">Revoke</button>`}</li>`).join('');
  $$('[data-revoke]').forEach((b) => b.addEventListener('click', async () => {
    if (!confirm('Revoke this token?')) return;
    await api(`/api/tokens/${b.dataset.revoke}`, { method: 'DELETE' }); loadTokens();
  }));
}
$('#token-create').addEventListener('click', async () => {
  const name = $('#token-name').value.trim();
  if (!name) { toast('Give the token a name'); return; }
  const r = await api('/api/tokens', { method: 'POST', body: { name } });
  const box = $('#token-new'); box.hidden = false;
  box.textContent = `Copy now, it will not be shown again:\n\n${r.token}\n\nUse as header:  Authorization: Bearer ${r.token}`;
  $('#token-name').value = ''; loadTokens();
});
$('#pw-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  try {
    await api('/api/password', { method: 'POST', body: { current: $('#pw-cur').value, new: $('#pw-new').value } });
    toast('Password changed, please log in again'); setTimeout(() => { location.href = '/login'; }, 1200);
  } catch (ex) { toast(ex.message); }
});
$('#logout').addEventListener('click', async () => { await api('/api/logout', { method: 'POST' }); location.href = '/login'; });

// ------------------------------------------------------------------ live updates (SSE)
const NOTIFY = /^(press\.(success|failed|completed|aborted|error|refused_not_present|blocked_\w+)|robot\.(departed|returned)|relay\.watchdog|schedule\.(fire|skipped|missed))$/;
function connect() {
  const es = new EventSource('/api/stream');
  es.addEventListener('open', () => $('#conn').className = 'dot on');
  es.addEventListener('status', (m) => renderStatus(JSON.parse(m.data)));
  es.addEventListener('event', (m) => {
    const e = JSON.parse(m.data);
    if (NOTIFY.test(e.kind)) toast(e.message);
    if ($('#tab-history').classList.contains('active')) $('#events').insertAdjacentHTML('afterbegin', eventLi(e));
  });
  es.addEventListener('error', async () => {
    $('#conn').className = 'dot off';
    if (es.readyState === EventSource.CLOSED) { es.close(); setTimeout(connect, 3000); }
    try { await api('/api/status'); } catch (_) { /* api() redirects on 401 */ }
  });
}

document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
refresh();
connect();
let initialTab = 'home';
try { initialTab = localStorage.getItem('tab') || 'home'; } catch (_) { /* ignore */ }
showTab(initialTab);
