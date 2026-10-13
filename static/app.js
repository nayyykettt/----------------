'use strict';
// Все даты между клиентом и сервером — местное время выбранного города, строки 'YYYY-MM-DDTHH:MM:SS'.
// Календарь работает в timeZone 'UTC', поэтому показывает эти строки как есть, без пересчёта в пояс браузера.

const $ = s => document.querySelector(s);
const WD = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'];
const RR = ['MO', 'TU', 'WE', 'TH', 'FR', 'SA', 'SU'];
const BASE_PROFILES = {physical: 'Физическая', mental: 'Умственная', routine: 'Рутина'};
const profName = (k, p) => p?.name || BASE_PROFILES[k] || k;
let state = null, offset = 0, suggest = null;

async function api(method, url, body) {
  const opt = {method};
  if (body !== undefined) {
    opt.body = typeof body === 'string' ? body : JSON.stringify(body);
    opt.headers = {'Content-Type': typeof body === 'string' ? 'text/calendar' : 'application/json'};
  }
  const r = await fetch(url, opt);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
const iso = d => d.toISOString().slice(0, 19);
const nowLocal = () => new Date(Date.now() + offset).toISOString().slice(0, 19);
const hm = s => s.slice(11, 16);
const dayName = s => { const d = new Date(s.slice(0, 10) + 'T00:00:00Z'); return WD[(d.getUTCDay() + 6) % 7]; };
const human = s => `${dayName(s)} ${s.slice(8, 10)}.${s.slice(5, 7)} ${hm(s)}`;
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));

function toast(msg, kind = '') {
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.innerHTML = `<span>${esc(msg)}</span><button aria-label="Закрыть"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg></button>`;
  el.querySelector('button').onclick = () => el.remove();
  $('#toasts').append(el);
  setTimeout(() => el.remove(), 20000);
}
const toasts = r => (r?.messages || []).forEach(m => toast(m));
async function guard(fn) { try { return await fn(); } catch (e) { toast(e.message, 'error'); } }

// ---------- цвет температуры: фиксированная шкала −30…+35 °C ----------
const STOPS = [[-30, [8, 29, 88]], [-15, [33, 102, 172]], [0, [146, 197, 222]], [10, [240, 240, 210]],
  [20, [253, 174, 97]], [28, [215, 48, 39]], [35, [103, 0, 13]]];
function tempRgb(t) {
  t = Math.max(-30, Math.min(35, t));
  for (let i = 1; i < STOPS.length; i++) {
    const [t1, c1] = STOPS[i], [t0, c0] = STOPS[i - 1];
    if (t <= t1) { const k = (t - t0) / (t1 - t0); return c0.map((v, j) => Math.round(v + (c1[j] - v) * k)); }
  }
  return STOPS.at(-1)[1];
}
function renderLegend() {
  const grad = STOPS.map(([t, c]) => `rgb(${c}) ${((t + 30) / 65 * 100).toFixed(1)}%`).join(',');
  $('#legend').innerHTML = `<div class="bar" style="background:linear-gradient(90deg,${grad})"></div>
    <div class="ticks"><span>−30</span><span>−15</span><span>0</span><span>+15</span><span>+35</span></div>
    <h3>Вероятность осадков</h3>
    <div class="rain-legend">${[1, 2, 3].map(i => `<div><span class="rain-${i}"></span>${i * 30} %</div>`).join('')}</div>`;
}

// ---------- календарь ----------
const cal = new FullCalendar.Calendar($('#calendar'), {
  locale: 'ru', timeZone: 'UTC', now: () => nowLocal(), initialView: 'timeGridWeek', headerToolbar: false,
  firstDay: 1, nowIndicator: true, height: '100%', scrollTime: '07:00:00', allDayText: 'весь день',
  snapDuration: '00:15:00',  // шаг перетаскивания, растягивания и выделения
  slotLabelFormat: {hour: '2-digit', minute: '2-digit', hour12: false},
  eventTimeFormat: {hour: '2-digit', minute: '2-digit', hour12: false},
  dayHeaderContent: a => a.view.type === 'dayGridMonth' ? a.text
    : {html: `<span class="dn">${WD[(a.date.getUTCDay() + 6) % 7]}</span><span class="dd">${a.date.getUTCDate()}</span>`},
  editable: true, selectable: true, selectMirror: true, droppable: true, dayMaxEvents: true,
  events: (info, ok, fail) => loadEvents(info).then(ok, e => { toast(e.message, 'error'); fail(e); }),
  datesSet: () => {
    $('#title').textContent = cal.view.title;
    document.querySelectorAll('.seg button').forEach(b => b.classList.toggle('on', b.dataset.view === cal.view.type));
  },
  select: info => { cal.unselect(); openEvent({start: iso(info.start), end: iso(info.end), all_day: info.allDay}); },
  eventClick: info => onClick(info.event),
  eventDrop: info => onMove(info),
  eventResize: info => onMove(info),
  drop: info => guard(() => newTask(info.draggedEl.dataset.id)),
  eventDidMount: info => { const t = info.event.extendedProps.tip; if (t) info.el.title = t; },
});

async function loadEvents(info) {
  const d = await api('GET', `/api/calendar?start=${iso(info.start)}&end=${iso(info.end)}`);
  const timed = cal.view.type !== 'dayGridMonth';
  const out = [];
  for (const e of d.events) {
    out.push({id: `e|${e.event_id}|${e.occ}`, title: (e.recurring ? '↻ ' : '') + e.title, start: e.start, end: e.end,
      allDay: e.all_day, classNames: ['hard'], extendedProps: {kind: 'event', ev: e,
        tip: `${e.title}\nЖёсткое событие${e.source === 'ics' ? ' (из .ics)' : ''}${e.recurring ? ', повторяется' : ''}`}});
  }
  for (const t of d.tasks) {
    const cls = ['task'];
    if (t.pinned) cls.push('pinned');
    if (t.highlight) cls.push('moved');
    if (t.warnings) cls.push('warn');
    const tip = [t.title, t.pinned ? '🔒 закреплена вручную' : 'гибкая задача',
      t.warnings && '⚠ ' + t.warnings, t.highlight && t.note].filter(Boolean).join('\n');
    out.push({id: `t|${t.id}`, title: (t.pinned ? '🔒 ' : '') + (t.warnings ? '⚠ ' : '') + t.title,
      start: t.start, end: t.end, classNames: cls, backgroundColor: t.color, borderColor: t.color,
      durationEditable: false, extendedProps: {kind: 'task', task: t, tip}});
    if (timed) {
      if (t.buf_start < t.start) out.push({start: t.buf_start, end: t.start, display: 'background', classNames: ['buffer']});
      if (t.end < t.buf_end) out.push({start: t.end, end: t.buf_end, display: 'background', classNames: ['buffer']});
    }
  }
  if (timed) {
    for (const s of d.sleep) out.push({start: s.start, end: s.end, display: 'background', classNames: ['sleep'],
      extendedProps: {tip: 'Сон'}});
    for (const w of d.weather) {
      const [r, g, b] = tempRgb(w.temp), t = Math.round(w.temp);
      const lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;  // тёмный текст на светлых ячейках
      const pp = w.pp ?? 0;
      out.push({start: w.start, end: w.end, display: 'background', backgroundColor: `rgba(${r},${g},${b},0.85)`,
        title: (t > 0 ? '+' : t < 0 ? '−' : '') + Math.abs(t) + '°',
        classNames: ['wx', lum > 0.55 ? 'ink-dark' : 'ink-light', pp >= 30 ? `rain-${Math.min(3, Math.floor(pp / 30))}` : ''].filter(Boolean),
        extendedProps: {tip: `${hm(w.start)}: ${w.temp > 0 ? '+' : ''}${w.temp.toFixed(0)} °C\n` +
          `Осадки: ${w.pr ?? 0} мм, вероятность ${w.pp ?? 0}%${w.phen.length ? ' (' + w.phen.join(', ') + ')' : ''}\n` +
          `Облачность: ${w.cloud ?? 0}%\nВетер: ${(w.wind ?? 0).toFixed(1)} м/с, порывы ${(w.gust ?? 0).toFixed(1)} м/с`}});
    }
  }
  if (suggest) suggest.slots.forEach((s, i) => out.push({
    id: `s|${i}`, title: `Вариант ${i + 1}` + (s.violations.length ? ' ⚠' : ''), start: s.start, end: s.end,
    editable: false, classNames: ['slot', s.violations.length ? 'worst' : 'good'],
    extendedProps: {kind: 'slot', slot: s, tip: s.violations.length ? 'Нарушено: ' + s.violations.join('; ') : 'Все правила соблюдены'}}));
  return out;
}

async function refresh() {
  state = await api('GET', '/api/state');
  const prev = offset;
  offset = Date.parse(state.now + 'Z') - Date.now();
  if (Math.abs(prev - offset) > 60000) cal.setOption('now', () => nowLocal());  // сменился пояс города
  renderSide();
  cal.refetchEvents();
}

function onClick(ev) {
  const p = ev.extendedProps;
  if (p.kind === 'slot') return guard(async () => {
    await api('PUT', `/api/tasks/${suggest.taskId}`, {start: p.slot.start, pinned: false});
    toast(`«${suggest.title}» поставлена на ${human(p.slot.start)}`);
    suggest = null; renderBanner(); await refresh();
  });
  if (p.kind === 'event') return openEvent(p.ev);
  if (p.kind === 'task') return openTask(p.task);
}

async function onMove(info) {
  const p = info.event.extendedProps;
  const res = await guard(async () => {
    if (p.kind === 'task') {
      await api('PUT', `/api/tasks/${p.task.id}`, {start: iso(info.event.start), pinned: true});
      toast(`«${p.task.title}» закреплена на ${human(iso(info.event.start))}`);
      return true;
    }
    if (p.kind !== 'event') return false;
    const scope = p.ev.recurring ? await askScope() : 'all';
    if (!scope) return false;
    const allDay = info.event.allDay;
    const start = iso(info.event.start);
    const end = info.event.end ? iso(info.event.end) : iso(new Date(info.event.start.getTime() + (allDay ? 864e5 : 36e5)));
    toasts(await api('PUT', `/api/events/${p.ev.event_id}`, {scope, occ: p.ev.occ, title: p.ev.title, start, end, all_day: allDay}));
    return true;
  });
  if (!res) info.revert();
  await refresh();
}

function askScope() {
  const dlg = $('#scope-dlg');
  return new Promise(res => { dlg.onclose = () => res(dlg.returnValue || null); dlg.returnValue = ''; dlg.showModal(); });
}
function dialog(dlg) {
  return new Promise(res => { dlg.onclose = () => res(dlg.returnValue); dlg.returnValue = 'cancel'; dlg.showModal(); });
}

// ---------- события ----------
function parseRule(r) { return Object.fromEntries((r || '').replace(/^RRULE:/, '').split(';').filter(Boolean).map(x => x.split('='))); }

async function openEvent(ev) {
  const dlg = $('#event-dlg'), f = dlg.querySelector('form');
  const existing = !!ev.event_id;
  $('#event-head').textContent = existing ? 'Событие' : 'Новое событие';
  $('#event-del').hidden = !existing;
  f.title.value = ev.title || '';
  f.all_day.checked = !!ev.all_day;
  if (ev.all_day) {  // у событий на весь день конец исключающий — показываем последний день
    const e = new Date(ev.end.slice(0, 10) + 'T00:00:00Z'); e.setUTCDate(e.getUTCDate() - 1);
    f.start.value = ev.start.slice(0, 10) + 'T09:00';
    f.end.value = (e.toISOString().slice(0, 10) < ev.start.slice(0, 10) ? ev.start.slice(0, 10) : e.toISOString().slice(0, 10)) + 'T10:00';
  } else { f.start.value = ev.start.slice(0, 16); f.end.value = ev.end.slice(0, 16); }
  const rule = parseRule(ev.rrule);
  f.freq.value = ['DAILY', 'WEEKLY', 'MONTHLY'].includes(rule.FREQ) ? rule.FREQ : '';
  f.interval.value = rule.INTERVAL || 1;
  const days = (rule.BYDAY || '').split(',');
  $('#byday').innerHTML = RR.map((d, i) => `<label><input type="checkbox" name="byday" value="${d}" ${days.includes(d) ? 'checked' : ''}> ${WD[i]}</label>`).join('');
  f.ends.value = rule.UNTIL ? 'until' : 'never';
  f.until.value = rule.UNTIL ? `${rule.UNTIL.slice(0, 4)}-${rule.UNTIL.slice(4, 6)}-${rule.UNTIL.slice(6, 8)}` : '';
  const complex = Object.keys(rule).some(k => !['FREQ', 'INTERVAL', 'BYDAY', 'UNTIL', 'WKST'].includes(k)) ||
    (rule.FREQ && !f.freq.value);
  $('#rrule-note').textContent = complex ? `Правило повтора из файла: ${ev.rrule}. Оно сохранится, если не менять поля повтора.` : '';
  let repeatTouched = false;
  f.querySelectorAll('[name=freq],[name=interval],[name=byday],[name=ends],[name=until]').forEach(i => i.onchange = () => { repeatTouched = true; syncRep(); });
  const syncRep = () => dlg.querySelectorAll('.rep').forEach(e => e.hidden = !f.freq.value || (e.classList.contains('weekly') && f.freq.value !== 'WEEKLY'));
  syncRep();

  const act = await dialog(dlg);
  if (act === 'cancel') return;
  await guard(async () => {
    if (act === 'delete') {
      const scope = ev.recurring ? await askScope() : 'all';
      if (!scope) return;
      await api('DELETE', `/api/events/${ev.event_id}?scope=${scope}&occ=${encodeURIComponent(ev.occ)}`);
      return refresh();
    }
    let rrule = null;
    if (f.freq.value) {
      rrule = `FREQ=${f.freq.value};INTERVAL=${Math.max(1, +f.interval.value || 1)}`;
      const bd = [...f.querySelectorAll('[name=byday]:checked')].map(i => i.value);
      if (f.freq.value === 'WEEKLY' && bd.length) rrule += ';BYDAY=' + bd.join(',');
      if (f.ends.value === 'until' && f.until.value) rrule += `;UNTIL=${f.until.value.replace(/-/g, '')}T235959`;
    }
    const allDay = f.all_day.checked;
    let start = f.start.value + ':00', end = f.end.value + ':00';
    if (allDay) {
      start = f.start.value.slice(0, 10);
      const e = new Date(f.end.value.slice(0, 10) + 'T00:00:00Z'); e.setUTCDate(e.getUTCDate() + 1);
      end = e.toISOString().slice(0, 10);
    }
    const body = {title: f.title.value, start, end, all_day: allDay};
    if (!existing) { body.rrule = rrule; toasts(await api('POST', '/api/events', body)); return refresh(); }
    if (repeatTouched) body.rrule = rrule;
    const scope = ev.recurring ? await askScope() : 'all';
    if (!scope) return;
    toasts(await api('PUT', `/api/events/${ev.event_id}`, {...body, scope, occ: ev.occ}));
    await refresh();
  });
}

// ---------- задачи и шаблоны ----------
async function newTask(templateId) {
  const r = await api('POST', '/api/tasks', {template_id: +templateId});
  await showSlots(r.id);
}

async function showSlots(taskId) {
  const r = await api('GET', `/api/tasks/${taskId}/slots`);
  suggest = {taskId, ...r};
  await refresh();
  suggest.title = state.tasks.find(x => x.id === taskId)?.title || 'Задача';
  if (r.slots.length) {
    if (cal.view.type === 'dayGridMonth') cal.changeView('timeGridWeek');
    cal.gotoDate(r.slots[0].start.slice(0, 10));
  }
  renderBanner();
}

function renderBanner() {
  const b = $('#banner');
  if (!suggest) { b.hidden = true; return; }
  b.hidden = false;
  b.className = suggest.slots.length ? (suggest.worst ? 'worst' : 'good') : 'none';
  let html = `<strong>«${esc(suggest.title)}»</strong>: `;
  if (!suggest.slots.length) html += `подобрать время не удалось — ${esc(suggest.reason)}. Задача осталась в «Не размещены».`;
  else if (suggest.worst) html += 'слотов без нарушений нет, показаны лучшие из худших. Кликните по варианту:' +
    '<ol>' + suggest.slots.map(s => `<li>${human(s.start)} — ${esc(s.violations.join('; '))}</li>`).join('') + '</ol>';
  else html += `подсвечено вариантов: ${suggest.slots.length}. Кликните по одному, чтобы поставить задачу. ` +
    suggest.slots.map((s, i) => `<button class="small" data-go="${s.start}">${i + 1}: ${human(s.start)}</button>`).join(' ');
  b.innerHTML = html + ' <span class="grow"></span><button id="banner-close" class="icon small" aria-label="Закрыть"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg></button>';
  $('#banner-close').onclick = () => { suggest = null; renderBanner(); cal.refetchEvents(); };
  b.querySelectorAll('[data-go]').forEach(x => x.onclick = () => cal.gotoDate(x.dataset.go.slice(0, 10)));
}

async function openTask(t) {
  if (t.highlight) guard(() => api('PUT', `/api/tasks/${t.id}`, {highlight: false}).then(refresh));
  const dlg = $('#task-dlg');
  $('#task-head').textContent = (t.pinned ? '🔒 ' : '') + t.title;
  $('#task-info').innerHTML = `${human(t.start)} – ${hm(t.end)}<br>` +
    (t.pinned ? 'Закреплена вручную: автоперенос её не трогает.' : 'Не закреплена: при плохой погоде перенесётся автоматически.') +
    (t.warnings ? `<br><span class="warn-text">⚠ ${esc(t.warnings)}</span>` : '') +
    (t.note ? `<br><span class="hint">${esc(t.note)}</span>` : '');
  $('#task-unpin').hidden = !t.pinned;
  const tid = state.tasks.find(x => x.id === t.id)?.template_id;
  const tpl = state.templates.find(x => x.id === tid);
  $('#task-edit').hidden = !tpl?.oneoff;
  const act = await dialog(dlg);
  await guard(async () => {
    if (act === 'slots') return showSlots(t.id);
    if (act === 'edit') return openTemplate(tpl);
    if (act === 'unpin') await api('PUT', `/api/tasks/${t.id}`, {pinned: false});
    if (act === 'unplace') await api('PUT', `/api/tasks/${t.id}`, {start: null});
    if (act === 'delete') await api('DELETE', `/api/tasks/${t.id}`);
    if (['unpin', 'unplace', 'delete'].includes(act)) await refresh();
  });
}

function renderSide() {
  $('#city').value = state.city.name;
  $('#city').title = `${state.city.name}, ${state.city.country} · ${state.city.timezone}`;
  $('#forecast-info').textContent = state.forecast_end
    ? `Прогноз до ${human(state.forecast_end)} (${state.city.timezone})${state.fake_weather ? ' · ПОДМЕНА: дождь' : ''}`
    : 'Прогноза нет — задачи с учётом погоды не планируются';
  $('#templates').innerHTML = state.templates.filter(t => !t.oneoff).map(t => `
    <div class="tpl" data-id="${t.id}" style="--c:${esc(t.color)}" title="Правый клик — настройка">
      <span class="name">${esc(t.name)}</span>
      <span class="meta"><span>${t.duration} мин</span><span>П${t.priority}</span>${t.use_weather ? '<span>☂</span>' : ''}</span>
      <button class="icon small add" title="Создать задачу" aria-label="Создать задачу"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg></button>
    </div>`).join('');
  document.querySelectorAll('.tpl').forEach(el => {
    const t = state.templates.find(x => x.id === +el.dataset.id);
    el.oncontextmenu = e => { e.preventDefault(); openTemplate(t); };
    el.querySelector('.add').onclick = () => guard(() => newTask(t.id));
  });
  const un = state.tasks.filter(t => !t.start).sort((a, b) => a.priority - b.priority);
  $('#un-count').textContent = un.length ? '· ' + un.length : '';
  $('#unplaced').innerHTML = un.length ? un.map(t => `
    <div class="un" style="--c:${esc(t.color)}">
      <div class="un-head"><strong>${esc(t.title)}</strong><span class="prio">П${t.priority}</span></div>
      <div class="reason">${esc(t.reason || '')}</div>
      <button class="small" data-slots="${t.id}">Подобрать время</button>
      <button class="small ghost" data-del="${t.id}">Удалить</button>
    </div>`).join('') : '<p class="hint">Все задачи размещены</p>';
  document.querySelectorAll('[data-slots]').forEach(b => b.onclick = () => guard(() => showSlots(+b.dataset.slots)));
  document.querySelectorAll('[data-del]').forEach(b => b.onclick = () => guard(async () => {
    await api('DELETE', `/api/tasks/${b.dataset.del}`);
    if (suggest?.taskId === +b.dataset.del) { suggest = null; renderBanner(); }
    await refresh();
  }));
}

async function openTemplate(t, oneoff = false) {
  const dlg = $('#tpl-dlg'), f = dlg.querySelector('form');
  oneoff = oneoff || !!t?.oneoff;
  $('#tpl-head').textContent = oneoff ? 'Своя задача' : t?.name || 'Новый шаблон';
  f.activity.innerHTML = '<option value="">Без профиля</option>' + Object.entries(state.settings.profiles)
    .map(([k, p]) => `<option value="${esc(k)}">${esc(profName(k, p))} (${p.from}–${p.to})</option>`).join('');
  t = t || {name: oneoff ? 'Своя задача' : 'Новый шаблон', activity: oneoff ? '' : 'routine', duration: 60, buf_before: 0,
    buf_after: 0, win_from: oneoff ? '00:00' : '08:00', win_to: oneoff ? '23:59' : '21:00', bad_days: [], use_weather: false, t_min: 0, t_max: 30, precip_max: 30, cloud_max: 100,
    wind_max: 10, bad_phen: ['rain', 'snow', 'thunder'], priority: 2, min_gap: 0, color: '#00897b'};
  $('#tpl-del').hidden = !t.id || oneoff;
  dlg.style.setProperty('--c', t.color);
  for (const k of ['name', 'activity', 'duration', 'buf_before', 'buf_after', 'win_from', 'win_to', 't_min', 't_max',
    'precip_max', 'cloud_max', 'wind_max', 'priority', 'min_gap', 'color']) f[k].value = t[k];
  f.use_weather.checked = t.use_weather;
  $('#tpl-days').innerHTML = WD.map((d, i) => `<label><input type="checkbox" name="bad_days" value="${i}" ${t.bad_days.includes(i) ? 'checked' : ''}> ${d}</label>`).join('');
  f.querySelectorAll('[name=bad_phen]').forEach(i => i.checked = t.bad_phen.includes(i.value));
  const sync = () => $('#tpl-weather').disabled = !f.use_weather.checked;
  f.use_weather.onchange = sync; sync();
  const act = await dialog(dlg);
  await guard(async () => {
    if (act === 'delete') {
      if (!confirm(`Удалить шаблон «${t.name}» и все его задачи?`)) return;
      await api('DELETE', `/api/templates/${t.id}`);
      return refresh();
    }
    if (act !== 'save') return;
    const body = {oneoff, use_weather: f.use_weather.checked,
      bad_days: [...f.querySelectorAll('[name=bad_days]:checked')].map(i => +i.value),
      bad_phen: [...f.querySelectorAll('[name=bad_phen]:checked')].map(i => i.value)};
    for (const k of ['name', 'activity', 'win_from', 'win_to', 'color']) body[k] = f[k].value;
    for (const k of ['duration', 'buf_before', 'buf_after', 't_min', 't_max', 'precip_max', 'cloud_max', 'wind_max',
      'priority', 'min_gap']) body[k] = +f[k].value;
    const r = await api(t.id ? 'PUT' : 'POST', t.id ? `/api/templates/${t.id}` : '/api/templates', body);
    toasts(r);
    if (oneoff && !t.id) return newTask(r.id);  // сразу показать места для новой задачи
    await refresh();
  });
}

// ---------- настройки ----------
async function openSettings() {
  const dlg = $('#settings-dlg'), f = dlg.querySelector('form');
  const st = structuredClone(state.settings);
  f.sleep_mode.value = st.sleep.mode;
  f.wd_bed.value = st.sleep.weekday.bed; f.wd_wake.value = st.sleep.weekday.wake;
  f.we_bed.value = st.sleep.weekend.bed; f.we_wake.value = st.sleep.weekend.wake;
  $('#sleep-daily').innerHTML = WD.map((d, i) => `<div class="row"><span class="lbl">${d}</span>
    отбой <input type="time" name="d_bed" value="${st.sleep.days[i].bed}"> подъём <input type="time" name="d_wake" value="${st.sleep.days[i].wake}">
    ${i === 0 ? '<button type="button" id="copy-wd" class="small">Скопировать на будни</button>' : ''}</div>`).join('');
  $('#copy-wd').onclick = () => {
    const b = f.querySelectorAll('[name=d_bed]'), w = f.querySelectorAll('[name=d_wake]');
    for (let i = 1; i < 5; i++) { b[i].value = b[0].value; w[i].value = w[0].value; }
  };
  const sync = () => { $('#sleep-simple').hidden = f.sleep_mode.value !== 'simple'; $('#sleep-daily').hidden = f.sleep_mode.value !== 'daily'; };
  f.querySelectorAll('[name=sleep_mode]').forEach(i => i.onchange = sync); sync();
  const profRow = (k, p) => `<div class="row prof" data-key="${esc(k)}">` +
    (BASE_PROFILES[k] ? `<span class="lbl">${BASE_PROFILES[k]}</span>`
      : `<input class="lbl" name="p_name" value="${esc(p.name || '')}" placeholder="Название" required>`) +
    ` с <input type="time" name="p_from" value="${p.from}" required> до <input type="time" name="p_to" value="${p.to}" required>` +
    (BASE_PROFILES[k] ? '' : ' <button type="button" class="small danger" data-rm>Удалить</button>') + '</div>';
  $('#profiles').innerHTML = Object.entries(st.profiles).map(([k, p]) => profRow(k, p)).join('');
  const bindRm = () => $('#profiles').querySelectorAll('[data-rm]').forEach(b => b.onclick = () => b.closest('.prof').remove());
  bindRm();
  $('#profile-add').onclick = () => {
    $('#profiles').insertAdjacentHTML('beforeend', profRow('c' + Date.now(), {name: '', from: '09:00', to: '18:00'}));
    bindRm();
    $('#profiles .prof:last-child [name=p_name]').focus();
  };
  $('#fake-state').textContent = state.fake_weather ? 'Сейчас прогноз подменён: дождь везде, кроме последних 2 дней прогноза.' : 'Используется настоящий прогноз Open-Meteo.';
  const fake = mode => guard(async () => { toasts(await api('POST', '/api/weather/fake', {mode})); dlg.close('cancel'); await refresh(); });
  $('#fake-rain').onclick = () => fake('rain');
  $('#fake-off').onclick = () => fake('off');

  if (await dialog(dlg) !== 'save') return;
  st.sleep.mode = f.sleep_mode.value;
  st.sleep.weekday = {bed: f.wd_bed.value, wake: f.wd_wake.value};
  st.sleep.weekend = {bed: f.we_bed.value, wake: f.we_wake.value};
  const b = f.querySelectorAll('[name=d_bed]'), w = f.querySelectorAll('[name=d_wake]');
  st.sleep.days = WD.map((_, i) => ({bed: b[i].value, wake: w[i].value}));
  st.profiles = Object.fromEntries([...$('#profiles').querySelectorAll('.prof')].map(r => [r.dataset.key, {
    ...(BASE_PROFILES[r.dataset.key] ? {} : {name: r.querySelector('[name=p_name]').value.trim() || 'Без названия'}),
    from: r.querySelector('[name=p_from]').value, to: r.querySelector('[name=p_to]').value}]));
  await guard(async () => { toasts(await api('PUT', '/api/settings', st)); await refresh(); });
}

// ---------- город ----------
let cityTimer;
$('#city').oninput = () => {
  clearTimeout(cityTimer);
  const q = $('#city').value.trim();
  if (q.length < 2) { $('#city-list').hidden = true; return; }
  cityTimer = setTimeout(() => guard(async () => {
    const res = await api('GET', '/api/geocode?q=' + encodeURIComponent(q));
    const ul = $('#city-list');
    ul.innerHTML = res.length ? res.map((c, i) => `<li tabindex="0" data-i="${i}">${esc(c.name)}<span class="meta">${esc([c.admin, c.country].filter(Boolean).join(', '))} · ${esc(c.timezone)}</span></li>`).join('')
      : '<li class="meta">Ничего не найдено</li>';
    ul.hidden = false;
    ul.querySelectorAll('[data-i]').forEach(li => li.onclick = li.onkeydown = e => {
      if (e.type === 'keydown' && e.key !== 'Enter') return;
      ul.hidden = true;
      guard(async () => { toasts(await api('POST', '/api/city', res[+li.dataset.i])); suggest = null; renderBanner(); await refresh(); });
    });
  }), 350);
};
$('#city').onblur = () => setTimeout(() => { $('#city-list').hidden = true; if (state) $('#city').value = state.city.name; }, 200);

// ---------- верхняя панель ----------
$('#today').onclick = () => cal.today();
$('#prev').onclick = () => cal.prev();
$('#next').onclick = () => cal.next();
document.querySelectorAll('.seg button').forEach(b => b.onclick = () => cal.changeView(b.dataset.view));
$('#import').onclick = () => $('#ics-file').click();
$('#ics-file').onchange = async e => {
  const file = e.target.files[0];
  e.target.value = '';
  if (file) await guard(async () => { toasts(await api('POST', '/api/import', await file.text())); await refresh(); });
};
$('#settings-btn').onclick = openSettings;
$('#tpl-new').onclick = () => openTemplate(null);
$('#oneoff-new').onclick = () => openTemplate(null, true);
$('#autoplace').onclick = () => guard(async () => { toasts(await api('POST', '/api/tasks/autoplace')); await refresh(); });

new FullCalendar.Draggable($('#templates'), {itemSelector: '.tpl'});

async function refreshWeather() {
  await guard(async () => toasts(await api('POST', '/api/weather/refresh')));
  await guard(refresh);
}

(async () => {
  renderLegend();
  await guard(refresh);
  cal.render();
  await refreshWeather();
  setInterval(refreshWeather, 3600 * 1000);
})();
