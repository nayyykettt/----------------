"""Погодный планировщик: локальный HTTP-сервер + SQLite. Запуск: python3 server.py"""
import json
import mimetypes
import os
import re
import sqlite3
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import ical
import scheduler as sch

UTC = timezone.utc
FMT = '%Y-%m-%dT%H:%M:%S'
ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get('PLANNER_DB', os.path.join(ROOT, 'data.sqlite3'))
LOCK = threading.Lock()

DEFAULT_CITY = {'name': 'Москва', 'country': 'Россия', 'lat': 55.75204, 'lon': 37.61781, 'timezone': 'Europe/Moscow'}
DEFAULT_SETTINGS = {
    'sleep': {'mode': 'simple', 'weekday': {'bed': '23:00', 'wake': '07:00'},
              'weekend': {'bed': '00:00', 'wake': '09:00'},
              'days': [{'bed': '23:00', 'wake': '07:00'}] * 5 + [{'bed': '00:00', 'wake': '09:00'}] * 2},
    'profiles': {'physical': {'from': '07:00', 'to': '14:00'}, 'mental': {'from': '16:00', 'to': '22:00'},
                 'routine': {'from': '10:00', 'to': '20:00'}},
}
TPL_DEFAULTS = {'name': 'Новый шаблон', 'activity': 'routine', 'duration': 60, 'buf_before': 0, 'buf_after': 0,
                'win_from': '08:00', 'win_to': '21:00', 'bad_days': [], 'use_weather': False,
                't_min': 0, 't_max': 30, 'precip_max': 30, 'cloud_max': 100, 'wind_max': 10,
                'bad_phen': ['rain', 'snow', 'thunder'], 'priority': 2, 'min_gap': 0, 'color': '#2e7d32',
                'oneoff': False}  # oneoff — скрытый шаблон «своей задачи», живёт вместе с ней
PRESETS = [
    {'name': 'Тренировка на улице', 'activity': 'physical', 'duration': 60, 'buf_before': 15, 'buf_after': 30,
     'win_from': '07:00', 'win_to': '14:00', 'bad_days': [6], 'use_weather': True, 't_min': 5, 't_max': 25,
     'precip_max': 20, 'cloud_max': 70, 'wind_max': 8, 'bad_phen': ['rain', 'snow', 'thunder'], 'priority': 1,
     'min_gap': 1, 'color': '#d84315'},
    {'name': 'Прогулка', 'activity': 'physical', 'duration': 45, 'buf_before': 5, 'buf_after': 5,
     'win_from': '08:00', 'win_to': '21:00', 'use_weather': True, 't_min': 0, 't_max': 28, 'precip_max': 30,
     'cloud_max': 100, 'wind_max': 10, 'priority': 2, 'color': '#2e7d32'},
    {'name': 'Поход в магазин', 'activity': 'routine', 'duration': 40, 'buf_before': 15, 'buf_after': 15,
     'win_from': '09:00', 'win_to': '21:00', 'priority': 3, 'min_gap': 2, 'color': '#6a1b9a'},
    {'name': 'Учёба', 'activity': 'mental', 'duration': 90, 'buf_before': 0, 'buf_after': 10,
     'win_from': '09:00', 'win_to': '22:00', 'priority': 1, 'color': '#1565c0'},
]


# ---------- БД ----------

@contextmanager
def db():
    """Соединение с транзакцией: commit при успехе, rollback при ошибке, затем закрытие."""
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        with c:
            yield c
    finally:
        c.close()


def init_db():
    with db() as c:
        c.executescript('''
        create table if not exists kv(key text primary key, value text);
        create table if not exists events(id integer primary key, title text, start text, end text,
            all_day int default 0, tzid text, rrule text, exdates text default '[]',
            source text default 'manual', uid text);
        create table if not exists templates(id integer primary key, data text);
        create table if not exists tasks(id integer primary key, template_id int, start text,
            pinned int default 0, reason text, highlight int default 0, note text);
        ''')
        if 'accepted' not in [r[1] for r in c.execute('pragma table_info(tasks)')]:
            c.execute("alter table tasks add column accepted text default '[]'")
        if not c.execute('select 1 from templates limit 1').fetchone():
            for t in PRESETS:
                c.execute('insert into templates(data) values(?)', (json.dumps({**TPL_DEFAULTS, **t}),))


def kv_get(c, key, default=None):
    r = c.execute('select value from kv where key=?', (key,)).fetchone()
    return json.loads(r[0]) if r else default


def kv_set(c, key, value):
    c.execute('insert or replace into kv(key, value) values(?, ?)', (key, json.dumps(value)))


def city(c):
    return kv_get(c, 'city', DEFAULT_CITY)


def city_tz(c):
    return ZoneInfo(city(c)['timezone'])


def settings(c):
    return {**DEFAULT_SETTINGS, **kv_get(c, 'settings', {})}


def templates(c):
    return {r['id']: {**TPL_DEFAULTS, **json.loads(r['data']), 'id': r['id']}
            for r in c.execute('select * from templates order by id')}


def local(dt, tz):
    return dt.astimezone(tz).strftime(FMT)


def from_local(s, tz):
    return datetime.fromisoformat(s.rstrip('Z')[:19]).replace(tzinfo=tz)


# ---------- погода ----------

FIELDS = 'temperature_2m,precipitation_probability,precipitation,cloud_cover,wind_speed_10m,wind_gusts_10m,weather_code'


def fetch_json(url):
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.load(r)


def refresh_forecast(c):
    cy = city(c)
    q = urllib.parse.urlencode({'latitude': cy['lat'], 'longitude': cy['lon'], 'hourly': FIELDS,
                                'wind_speed_unit': 'ms', 'timeformat': 'unixtime', 'forecast_days': 7,
                                'timezone': cy['timezone']})
    h = fetch_json('https://api.open-meteo.com/v1/forecast?' + q)['hourly']
    hours = [{'t': t, 'temp': h['temperature_2m'][i], 'pp': h['precipitation_probability'][i],
              'pr': h['precipitation'][i], 'cloud': h['cloud_cover'][i], 'wind': h['wind_speed_10m'][i],
              'gust': h['wind_gusts_10m'][i], 'code': h['weather_code'][i]}
             for i, t in enumerate(h['time']) if h['temperature_2m'][i] is not None]
    kv_set(c, 'forecast', {'hours': hours, 'fetched': datetime.now(UTC).isoformat(), 'city': cy['name']})


def forecast(c):
    """{'hours': {epoch: {...}}, 'end': aware} или None. Учитывает тестовую подмену «дождь»."""
    f = kv_get(c, 'forecast')
    if not f or not f['hours'] or f.get('city') != city(c)['name']:
        return None
    fake = kv_get(c, 'fake_weather')
    dry_from = max(h['t'] for h in f['hours']) - 48 * 3600  # при подмене последние 2 дня сухие — есть куда перенести
    hours = {}
    for h in f['hours']:
        if fake == 'rain' and h['t'] < dry_from:
            h = {**h, 'pp': 95, 'pr': 3.0, 'code': 63, 'cloud': 100}
        hours[h['t']] = h
    return {'hours': hours, 'end': datetime.fromtimestamp(max(hours) + 3600, UTC)}


# ---------- контекст планировщика ----------

def ctx_for(c):
    tz = city_tz(c)
    now = datetime.now(UTC)
    fc = forecast(c)
    end = (fc['end'] if fc else now + timedelta(days=7)) + timedelta(days=1)
    tpls = templates(c)
    busy = [(st, en) for ev in c.execute('select * from events where all_day=0')
            for _, st, en in ical.occurrences(dict(ev), tz, now - timedelta(days=1), end)]
    placed = [sch.make_placed(t['id'], tpls[t['template_id']], datetime.fromisoformat(t['start']), bool(t['pinned']),
                              json.loads(t['accepted'] or '[]'))
              for t in c.execute('select * from tasks where start is not null') if t['template_id'] in tpls]
    st = settings(c)
    return {'now': now, 'tz': tz, 'events_busy': busy, 'placed': placed, 'sleep': st['sleep'],
            'profiles': st['profiles'], 'forecast': fc}, tpls


def apply_changes(c, changes):
    for ch in changes:
        if ch['new']:
            c.execute("update tasks set start=?, reason=null, highlight=1, note=?, accepted='[]' where id=?",
                      (ch['new'].isoformat(), ch['message'], ch['id']))
        else:
            c.execute('update tasks set accepted=?, note=? where id=?',
                      (json.dumps(sorted(ch['accepted'])), ch['message'], ch['id']))
    return [ch['message'] for ch in changes]


def run_autoshift(c):
    ctx, _ = ctx_for(c)
    return apply_changes(c, sch.autoshift(ctx))


# ---------- обработчики API ----------

def get_state(c, q, body):
    ctx, tpls = ctx_for(c)
    tz = ctx['tz']
    tasks = []
    placed = {p['id']: p for p in ctx['placed']}
    for t in c.execute('select * from tasks order by id'):
        tpl = tpls.get(t['template_id'])
        if not tpl:
            continue
        p = placed.get(t['id'])
        tasks.append({'id': t['id'], 'template_id': tpl['id'], 'title': tpl['name'], 'color': tpl['color'],
                      'priority': tpl['priority'], 'start': local(p['start'], tz) if p else None,
                      'pinned': bool(t['pinned']), 'reason': t['reason'], 'note': t['note']})
    fc = ctx['forecast']
    return {'now': local(ctx['now'], tz), 'city': city(c), 'settings': settings(c), 'templates': list(tpls.values()),
            'tasks': tasks, 'fake_weather': kv_get(c, 'fake_weather'),
            'forecast_end': local(fc['end'], tz) if fc else None}


def get_calendar(c, q, body):
    ctx, tpls = ctx_for(c)
    tz = ctx['tz']
    lo, hi = from_local(q['start'], tz), from_local(q['end'], tz)
    events = []
    for ev in c.execute('select * from events'):
        ev = dict(ev)
        for o, st, en in ical.occurrences(ev, tz, lo, hi):
            if ev['all_day']:
                s, e = o.date().isoformat(), (o + (en - st)).date().isoformat()
            else:
                s, e = local(st, tz), local(en, tz)
            events.append({'event_id': ev['id'], 'occ': o.strftime(FMT), 'title': ev['title'], 'start': s, 'end': e,
                           'all_day': bool(ev['all_day']), 'recurring': bool(ev['rrule']), 'rrule': ev['rrule'],
                           'source': ev['source'], 'series_start': ev['start'], 'series_end': ev['end']})
    tasks = []
    rows = {t['id']: t for t in c.execute('select * from tasks where start is not null')}
    for p in ctx['placed']:
        if p['span'][0] < hi and p['span'][1] > lo:
            t = rows[p['id']]
            tasks.append({'id': p['id'], 'title': p['tpl']['name'], 'color': p['tpl']['color'],
                          'start': local(p['start'], tz), 'end': local(p['end'], tz),
                          'buf_start': local(p['span'][0], tz), 'buf_end': local(p['span'][1], tz),
                          'pinned': p['pinned'], 'highlight': bool(t['highlight']), 'note': t['note'],
                          'warnings': sch.problems(p, ctx)})
    sleep = [{'start': local(s, tz), 'end': local(e, tz)}
             for s, e in sch.sleep_intervals(ctx['sleep'], tz, lo.date(), hi.date()) if s < hi and e > lo]
    weather = []
    if ctx['forecast']:
        for t, h in sorted(ctx['forecast']['hours'].items()):
            st = datetime.fromtimestamp(t, UTC)
            if lo <= st < hi:
                weather.append({**h, 'start': local(st, tz), 'end': local(st + timedelta(hours=1), tz),
                                'phen': [sch.PHEN[p] for p in sorted(sch.phenomena(h['code'] or 0))]})
    return {'events': events, 'tasks': tasks, 'sleep': sleep, 'weather': weather}


def _int(v, name):
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ValueError(f'Поле «{name}» должно быть числом')


def _ev_fields(body, tz_name, tz):
    """Время из формы (местное время города) → время в поясе события."""
    title = (body.get('title') or '').strip() or 'Без названия'
    all_day = bool(body.get('all_day'))
    if all_day:
        s = datetime.fromisoformat(body['start'][:10])
        e = datetime.fromisoformat(body['end'][:10])
        e = max(e, s + timedelta(days=1))
    else:
        etz = ical.zone(tz_name, tz)
        s = from_local(body['start'], tz).astimezone(etz).replace(tzinfo=None)
        e = from_local(body['end'], tz).astimezone(etz).replace(tzinfo=None)
        if e <= s:
            raise ValueError('Окончание должно быть позже начала')
    return title, all_day, s, e


def create_event(c, q, body):
    tz = city_tz(c)
    title, all_day, s, e = _ev_fields(body, tz.key, tz)
    cur = c.execute('insert into events(title, start, end, all_day, tzid, rrule) values(?,?,?,?,?,?)',
                    (title, s.strftime(FMT), e.strftime(FMT), int(all_day), None if all_day else tz.key,
                     body.get('rrule') or None))
    return {'id': cur.lastrowid, 'messages': run_autoshift(c)}


def _event(c, eid):
    ev = c.execute('select * from events where id=?', (eid,)).fetchone()
    if not ev:
        raise LookupError('Событие не найдено')
    return dict(ev)


def update_event(c, q, body, eid):
    tz = city_tz(c)
    ev = _event(c, int(eid))
    scope = body.get('scope', 'all') if ev['rrule'] else 'all'
    occ = datetime.fromisoformat(body.get('occ') or ev['start'])
    title, all_day, s, e = _ev_fields(body, ev['tzid'], tz)
    series_start = datetime.fromisoformat(ev['start'])
    exd = json.loads(ev['exdates'] or '[]')
    delta = s - occ
    rrule = body['rrule'] if 'rrule' in body else ev['rrule']
    if scope == 'following' and occ == series_start:
        scope = 'all'
    tzid = None if all_day else (ev['tzid'] or tz.key)
    if scope == 'all':
        ns = series_start + delta
        c.execute('update events set title=?, start=?, end=?, all_day=?, tzid=?, rrule=?, exdates=? where id=?',
                  (title, ns.strftime(FMT), (ns + (e - s)).strftime(FMT), int(all_day), tzid, rrule or None,
                   json.dumps([(datetime.fromisoformat(x) + delta).strftime(FMT) for x in exd]), ev['id']))
    elif scope == 'one':
        c.execute('update events set exdates=? where id=?', (json.dumps(exd + [occ.strftime(FMT)]), ev['id']))
        c.execute('insert into events(title, start, end, all_day, tzid, source) values(?,?,?,?,?,?)',
                  (title, s.strftime(FMT), e.strftime(FMT), int(all_day), tzid, ev['source']))
    else:  # following
        c.execute('update events set rrule=? where id=?',
                  (ical.set_until(ev['rrule'], occ - timedelta(seconds=1)), ev['id']))
        later = [(datetime.fromisoformat(x) + delta).strftime(FMT) for x in exd if datetime.fromisoformat(x) >= occ]
        c.execute('insert into events(title, start, end, all_day, tzid, rrule, exdates, source) values(?,?,?,?,?,?,?,?)',
                  (title, s.strftime(FMT), e.strftime(FMT), int(all_day), tzid, rrule or None, json.dumps(later),
                   ev['source']))
    return {'ok': True, 'messages': run_autoshift(c)}


def delete_event(c, q, body, eid):
    ev = _event(c, int(eid))
    scope = q.get('scope', 'all') if ev['rrule'] else 'all'
    occ = datetime.fromisoformat(q.get('occ') or ev['start'])
    if scope == 'following' and occ == datetime.fromisoformat(ev['start']):
        scope = 'all'
    if scope == 'all':
        c.execute('delete from events where id=?', (ev['id'],))
    elif scope == 'one':
        exd = json.loads(ev['exdates'] or '[]') + [occ.strftime(FMT)]
        c.execute('update events set exdates=? where id=?', (json.dumps(exd), ev['id']))
    else:
        c.execute('update events set rrule=? where id=?',
                  (ical.set_until(ev['rrule'], occ - timedelta(seconds=1)), ev['id']))
    return {'ok': True}


def import_ics(c, q, body):
    tz = city_tz(c)
    try:
        new = ical.parse_ics(body, tz)
    except Exception as e:
        raise ValueError(f'Не удалось разобрать файл .ics: {e}')
    if not new:
        raise ValueError('В файле нет событий')
    now = datetime.now(UTC)
    old = [dict(r) for r in c.execute("select * from events where source='ics'")]
    # ponytail: пересечения ищутся в окне ±1 год от сегодня; более далёкие вхождения не сравниваются
    deletes, exd = ical.merge_plan(old, new, tz, now - timedelta(days=366), now + timedelta(days=366))
    for i in deletes:
        c.execute('delete from events where id=?', (i,))
    for ev in old:
        if ev['id'] in exd:
            c.execute('update events set exdates=? where id=?',
                      (json.dumps(json.loads(ev['exdates'] or '[]') + exd[ev['id']]), ev['id']))
    for ev in new:
        c.execute('insert into events(title, start, end, all_day, tzid, rrule, exdates, source, uid) '
                  'values(?,?,?,?,?,?,?,?,?)', (ev['title'], ev['start'], ev['end'], ev['all_day'], ev['tzid'],
                                                 ev['rrule'], json.dumps(ev['exdates']), 'ics', ev['uid']))
    replaced = len(deletes) + sum(len(v) for v in exd.values())
    msgs = [f'Импортировано событий: {len(new)}' + (f', заменено пересекающихся: {replaced}' if replaced else '')]
    return {'messages': msgs + run_autoshift(c)}


def geocode(c, q, body):
    url = 'https://geocoding-api.open-meteo.com/v1/search?' + urllib.parse.urlencode(
        {'name': q.get('q', ''), 'count': 10, 'language': 'ru', 'format': 'json'})
    res = fetch_json(url).get('results') or []
    return [{'name': r['name'], 'country': r.get('country', ''), 'admin': r.get('admin1', ''),
             'lat': r['latitude'], 'lon': r['longitude'], 'timezone': r['timezone']} for r in res if r.get('timezone')]


def set_city(c, q, body):
    ZoneInfo(body['timezone'])  # проверка, что пояс существует
    kv_set(c, 'city', {k: body[k] for k in ('name', 'country', 'lat', 'lon', 'timezone')})
    return weather_refresh(c, q, body)


def weather_refresh(c, q, body):
    msgs = []
    try:
        refresh_forecast(c)
    except Exception as e:
        msgs.append(f'Не удалось обновить прогноз ({e}); используется сохранённый')
    return {'messages': msgs + run_autoshift(c)}


def weather_fake(c, q, body):
    kv_set(c, 'fake_weather', 'rain' if body.get('mode') == 'rain' else None)
    return {'messages': run_autoshift(c)}


def put_settings(c, q, body):
    st = settings(c)
    for key in ('sleep', 'profiles'):
        if key in body:
            st[key] = body[key]
    for d in [st['sleep']['weekday'], st['sleep']['weekend'], *st['sleep']['days'], *st['profiles'].values()]:
        for k, v in d.items():
            if k != 'name' and not re.fullmatch(r'\d\d:\d\d', v):
                raise ValueError('Время указывается в формате ЧЧ:ММ')
    kv_set(c, 'settings', st)
    return {'messages': run_autoshift(c)}


def _tpl_clean(body):
    t = {**TPL_DEFAULTS, **{k: body[k] for k in TPL_DEFAULTS if k in body}}
    for k in ('duration', 'buf_before', 'buf_after', 'priority', 'min_gap'):
        t[k] = _int(t[k], k)
    for k in ('t_min', 't_max', 'precip_max', 'cloud_max', 'wind_max'):
        t[k] = float(t[k])
    if t['duration'] <= 0 or t['priority'] < 1:
        raise ValueError('Длительность должна быть больше 0, приоритет — от 1')
    if sch.minutes(t['win_from']) + t['duration'] > sch.minutes(t['win_to']):
        raise ValueError('Задача не помещается в окно «с» — «до»')
    t['bad_days'] = [int(d) for d in t['bad_days']]
    t['use_weather'] = bool(t['use_weather'])
    t['oneoff'] = bool(t['oneoff'])
    return t


def create_template(c, q, body):
    cur = c.execute('insert into templates(data) values(?)', (json.dumps(_tpl_clean(body)),))
    return {'id': cur.lastrowid}


def update_template(c, q, body, tid):
    c.execute('update templates set data=? where id=?', (json.dumps(_tpl_clean(body)), int(tid)))
    return {'messages': run_autoshift(c)}


def delete_template(c, q, body, tid):
    c.execute('delete from tasks where template_id=?', (int(tid),))
    c.execute('delete from templates where id=?', (int(tid),))
    return {'ok': True}


def create_task(c, q, body):
    tid = _int(body.get('template_id'), 'шаблон')
    if tid not in templates(c):
        raise LookupError('Шаблон не найден')
    cur = c.execute('insert into tasks(template_id, reason) values(?, ?)', (tid, 'время ещё не выбрано'))
    return {'id': cur.lastrowid}


def task_slots(c, q, body, task_id):
    ctx, tpls = ctx_for(c)
    t = c.execute('select * from tasks where id=?', (int(task_id),)).fetchone()
    if not t:
        raise LookupError('Задача не найдена')
    r = sch.find_slots(tpls[t['template_id']], ctx, task_id=t['id'])
    if not r['slots'] and t['start'] is None:
        c.execute('update tasks set reason=? where id=?', (r['reason'], t['id']))
    tz = ctx['tz']
    return {'worst': r['worst'], 'reason': r['reason'],
            'slots': [{'start': local(s['start'], tz), 'end': local(s['end'], tz), 'violations': s['violations']}
                      for s in r['slots']]}


def update_task(c, q, body, task_id):
    tz = city_tz(c)
    tid = int(task_id)
    if 'start' in body:
        if body['start']:
            # нарушения выбранного пользователем слота считаются принятыми: автоперенос из-за них не сработает
            ctx, tpls = ctx_for(c)
            tpl = tpls[c.execute('select template_id from tasks where id=?', (tid,)).fetchone()[0]]
            start = from_local(body['start'], tz)
            others = {**ctx, 'placed': [p for p in ctx['placed'] if p['id'] != tid]}
            accepted = sorted(sch.kinds(sch.problem_list(sch.make_placed(tid, tpl, start), others)))
            c.execute('update tasks set start=?, reason=null, highlight=0, note=null, pinned=?, accepted=? where id=?',
                      (start.astimezone(UTC).isoformat(), int(bool(body.get('pinned'))), json.dumps(accepted), tid))
        else:
            c.execute("update tasks set start=null, pinned=0, highlight=0, reason='снята с календаря вручную' "
                      "where id=?", (tid,))
    elif 'pinned' in body:
        c.execute('update tasks set pinned=? where id=?', (int(bool(body['pinned'])), tid))
    if body.get('highlight') is False:
        c.execute('update tasks set highlight=0 where id=?', (tid,))
    return {'ok': True}


def delete_task(c, q, body, task_id):
    t = c.execute('select template_id from tasks where id=?', (int(task_id),)).fetchone()
    c.execute('delete from tasks where id=?', (int(task_id),))
    if t and templates(c).get(t[0], {}).get('oneoff'):
        c.execute('delete from templates where id=?', (t[0],))
    return {'ok': True}


def autoplace(c, q, body):
    ctx, tpls = ctx_for(c)
    items = [(t['id'], tpls[t['template_id']]) for t in c.execute('select * from tasks where start is null')
             if t['template_id'] in tpls]
    res = sch.place_by_priority(items, ctx)
    placed = 0
    for tid, (start, reason) in res.items():
        if start:
            placed += 1
            c.execute("update tasks set start=?, reason=null, pinned=0, accepted='[]' where id=?", (start.isoformat(), tid))
        else:
            c.execute('update tasks set reason=? where id=?', (reason, tid))
    return {'messages': [f'Размещено задач: {placed} из {len(items)}']}


ROUTES = [
    ('GET', r'/api/state', get_state),
    ('GET', r'/api/calendar', get_calendar),
    ('POST', r'/api/events', create_event),
    ('PUT', r'/api/events/(\d+)', update_event),
    ('DELETE', r'/api/events/(\d+)', delete_event),
    ('POST', r'/api/import', import_ics),
    ('GET', r'/api/geocode', geocode),
    ('POST', r'/api/city', set_city),
    ('POST', r'/api/weather/refresh', weather_refresh),
    ('POST', r'/api/weather/fake', weather_fake),
    ('PUT', r'/api/settings', put_settings),
    ('POST', r'/api/templates', create_template),
    ('PUT', r'/api/templates/(\d+)', update_template),
    ('DELETE', r'/api/templates/(\d+)', delete_template),
    ('POST', r'/api/tasks', create_task),
    ('POST', r'/api/tasks/autoplace', autoplace),
    ('GET', r'/api/tasks/(\d+)/slots', task_slots),
    ('PUT', r'/api/tasks/(\d+)', update_task),
    ('DELETE', r'/api/tasks/(\d+)', delete_task),
]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, data, ctype='application/json; charset=utf-8'):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def handle_any(self, method):
        url = urllib.parse.urlparse(self.path)
        if method == 'GET' and not url.path.startswith('/api/'):
            return self.static(url.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        n = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(n).decode('utf-8', 'replace') if n else ''
        for m, pattern, fn in ROUTES:
            match = re.fullmatch(pattern, url.path)
            if m == method and match:
                try:
                    body = raw if url.path == '/api/import' else (json.loads(raw) if raw else {})
                    with LOCK, db() as c:
                        return self.send(200, fn(c, q, body, *match.groups()))
                except LookupError as e:
                    return self.send(404, {'error': str(e)})
                except (ValueError, KeyError) as e:
                    return self.send(400, {'error': str(e)})
                except Exception as e:  # сеть Open-Meteo и прочее — показываем пользователю, не падаем
                    return self.send(500, {'error': f'Ошибка: {e}'})
        self.send(404, {'error': 'Не найдено'})

    def static(self, path):
        path = 'index.html' if path in ('', '/') else path.lstrip('/')
        full = os.path.realpath(os.path.join(ROOT, 'static', path))
        if not full.startswith(os.path.join(ROOT, 'static') + os.sep) or not os.path.isfile(full):
            return self.send(404, {'error': 'Не найдено'})
        with open(full, 'rb') as f:
            ctype = mimetypes.guess_type(full)[0] or 'application/octet-stream'
            self.send(200, f.read(), ctype + ('; charset=utf-8' if ctype.startswith(('text', 'application/javascript')) else ''))

    def do_GET(self):
        self.handle_any('GET')

    def do_POST(self):
        self.handle_any('POST')

    def do_PUT(self):
        self.handle_any('PUT')

    def do_DELETE(self):
        self.handle_any('DELETE')


if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 8765))
    srv = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    url = f'http://127.0.0.1:{port}/'
    print(f'Погодный планировщик: {url}  (остановить — Ctrl+C)', flush=True)
    if '--no-browser' not in sys.argv:
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
