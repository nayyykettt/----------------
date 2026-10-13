"""Подбор времени для гибких задач. Чистые функции без БД и сети.

ctx — словарь:
  now          aware datetime (текущий момент)
  tz           ZoneInfo города
  events_busy  [(start, end)] — жёсткие события (aware)
  placed       [{id, tpl, start, end, span, pinned}] — поставленные задачи; span = с буферами
  sleep        настройки сна
  profiles     {activity: {'from': 'HH:MM', 'to': 'HH:MM', 'name'?: свой профиль}}; нет профиля — нет проверки
  forecast     {'hours': {epoch_часа: {...}}, 'end': aware} или None
"""
from collections import Counter
from datetime import datetime, time, timedelta, timezone

UTC = timezone.utc
STEP = timedelta(minutes=15)
WD = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс']
PHEN = {'rain': 'дождь', 'snow': 'снег', 'thunder': 'гроза', 'fog': 'туман'}
ACTIVITY = {'physical': 'физическая', 'mental': 'умственная', 'routine': 'рутина'}
REJECT = {'day': 'запрещённые дни', 'window': 'окно шаблона', 'sleep': 'сон',
          'busy': 'занятое время', 'forecast': 'нет прогноза на эти часы'}


def phenomena(code):
    """Явления по коду погоды WMO (Open-Meteo weather_code)."""
    s = set()
    if 51 <= code <= 67 or 80 <= code <= 82:
        s.add('rain')
    if 71 <= code <= 77 or code in (85, 86):
        s.add('snow')
    if code >= 95:
        s.add('thunder')
    if code in (45, 48):
        s.add('fog')
    return s


def minutes(hhmm):
    h, m = hhmm.split(':')
    return int(h) * 60 + int(m)


def at(d, mins, tz):
    return (datetime.combine(d, time(), tz) + timedelta(minutes=mins)).astimezone(UTC)


def fmt(dt, tz):
    l = dt.astimezone(tz)
    return f'{WD[l.weekday()]} {l:%d.%m %H:%M}'


def overlaps(span, intervals):
    return any(span[0] < e and s < span[1] for s, e in intervals)


def day_sleep(sleep, d):
    if sleep.get('mode') == 'daily':
        return sleep['days'][d.weekday()]
    return sleep['weekend' if d.weekday() >= 5 else 'weekday']


def sleep_intervals(sleep, tz, d_from, d_to):
    """Ночи: отбой дня d → подъём дня d+1. Отбой до полудня считается уже после полуночи."""
    out, d = [], d_from - timedelta(days=1)
    while d <= d_to:
        bed = minutes(day_sleep(sleep, d)['bed'])
        if bed < 12 * 60:
            bed += 1440
        wake = minutes(day_sleep(sleep, d + timedelta(days=1))['wake']) + 1440
        if wake > bed:
            out.append((at(d, bed, tz), at(d, wake, tz)))
        d += timedelta(days=1)
    return out


def hours_of(forecast, s, e):
    """Часы прогноза, покрывающие [s, e); None, если хоть одного нет."""
    t, out = int(s.timestamp()) // 3600 * 3600, []
    while t < e.timestamp():
        h = forecast['hours'].get(t)
        if h is None:
            return None
        out.append(h)
        t += 3600
    return out


def weather_check(tpl, hours):
    """-> ([(текст нарушения, вес)], комфорт 0..1). Ветер — средний (не порывы)."""
    v = []
    temps = [h['temp'] for h in hours]
    lo, hi = min(temps), max(temps)
    if lo < tpl['t_min']:
        v.append((f'температура {lo:+.0f}°C при минимуме {tpl["t_min"]:+g}', 1 + (tpl['t_min'] - lo) / 5))
    if hi > tpl['t_max']:
        v.append((f'температура {hi:+.0f}°C при максимуме {tpl["t_max"]:+g}', 1 + (hi - tpl['t_max']) / 5))
    pp = max(h['pp'] or 0 for h in hours)
    if pp > tpl['precip_max']:
        v.append((f'вероятность осадков {pp:.0f}% при пороге {tpl["precip_max"]:g}%', 1 + (pp - tpl['precip_max']) / 20))
    cl = max(h['cloud'] or 0 for h in hours)
    if cl > tpl['cloud_max']:
        v.append((f'облачность {cl:.0f}% при пороге {tpl["cloud_max"]:g}%', 1 + (cl - tpl['cloud_max']) / 20))
    wind = max(h['wind'] or 0 for h in hours)
    if wind > tpl['wind_max']:
        v.append((f'ветер {wind:.0f} м/с при пороге {tpl["wind_max"]:g}', 1 + (wind - tpl['wind_max']) / 2))
    seen = set().union(*(phenomena(h['code'] or 0) for h in hours))
    for p in sorted(seen & set(tpl['bad_phen'])):
        v.append((PHEN[p], 2))
    mid, half = (tpl['t_min'] + tpl['t_max']) / 2, max((tpl['t_max'] - tpl['t_min']) / 2, 1)
    comfort = max(0.0, 1 - abs(sum(temps) / len(temps) - mid) / half)
    return v, comfort


def find_slots(tpl, ctx, task_id=None, n=5):
    """-> {'slots': [{start, end, violations, ...}], 'worst': bool, 'reason': str|None}"""
    tz, now, fc = ctx['tz'], ctx['now'], ctx.get('forecast')
    dur = timedelta(minutes=tpl['duration'])
    bb, ba = timedelta(minutes=tpl['buf_before']), timedelta(minutes=tpl['buf_after'])
    if fc:
        horizon = fc['end']
    elif tpl['use_weather']:
        return {'slots': [], 'worst': False, 'reason': 'нет прогноза на эти дни'}
    else:
        horizon = now + timedelta(days=7)
    busy = list(ctx['events_busy']) + [p['span'] for p in ctx['placed'] if p['id'] != task_id]
    same = [p['start'] for p in ctx['placed'] if p['id'] != task_id and p['tpl']['id'] == tpl['id']]
    sleeps = sleep_intervals(ctx['sleep'], tz, now.astimezone(tz).date(), horizon.astimezone(tz).date())
    w0, w1 = minutes(tpl['win_from']), minutes(tpl['win_to'])
    prof = ctx['profiles'].get(tpl['activity'])
    gap = tpl.get('min_gap', 0)

    s = datetime.fromtimestamp((int(now.timestamp()) // 900 + 1) * 900, UTC)  # строго позже «сейчас»
    rejected, cands = Counter(), []
    while s + dur <= horizon:
        e, ls = s + dur, s.astimezone(tz)
        span = (s - bb, e + ba)
        m0 = ls.hour * 60 + ls.minute
        m1 = m0 + tpl['duration']
        if ls.weekday() in tpl['bad_days']:
            rejected['day'] += 1
        elif not (w0 <= m0 and m1 <= w1):
            rejected['window'] += 1
        elif overlaps(span, sleeps):
            rejected['sleep'] += 1
        elif overlaps(span, busy):
            rejected['busy'] += 1
        else:
            viol, comfort = [], 1.0
            hrs = hours_of(fc, s, e) if tpl['use_weather'] else []
            if hrs is None:
                rejected['forecast'] += 1
                s += STEP
                continue
            if hrs:
                viol, comfort = weather_check(tpl, hrs)
            if prof and not (minutes(prof['from']) <= m0 and m1 <= minutes(prof['to'])):
                viol.append((f'вне окна профиля «{prof.get("name") or ACTIVITY.get(tpl["activity"], tpl["activity"])}» '
                             f'{prof["from"]}–{prof["to"]}', 1))
            for o in same:
                days = abs((ls.date() - o.astimezone(tz).date()).days)
                if gap > 0 and days <= gap:  # gap = минимум дней отдыха между задачами
                    viol.append((f'рядом с другой задачей шаблона ({fmt(o, tz)}), нужен интервал {gap} дн.', 1))
                    break
            cands.append({'start': s, 'end': e, 'violations': [t for t, _ in viol],
                          'penalty': sum(w for _, w in viol), 'score': comfort})
        s += STEP

    if not cands:
        if not rejected:
            return {'slots': [], 'worst': False, 'reason': 'в пределах прогноза не осталось времени'}
        why = ', '.join(REJECT[k] for k, _ in rejected.most_common())
        return {'slots': [], 'worst': False,
                'reason': f'нет свободного слота до {fmt(horizon, tz)} (мешают: {why})'}
    clean = [c for c in cands if not c['violations']]
    pool = sorted(clean, key=lambda c: -c['score']) if clean else \
        sorted(cands, key=lambda c: (c['penalty'], -c['score']))
    picked = []
    for one_per_day in (True, False):  # сначала по одному варианту на день, чтобы был выбор
        for c in pool:
            if len(picked) >= n:
                break
            if any(c is p or (c['start'] < p['end'] and p['start'] < c['end']) or
                   (one_per_day and c['start'].astimezone(tz).date() == p['start'].astimezone(tz).date())
                   for p in picked):
                continue
            picked.append(c)
    return {'slots': picked, 'worst': not clean, 'reason': None}


def problems(p, ctx):
    """Почему поставленная задача сейчас плоха (перекрыта событием или погода нарушает правила)."""
    return ', '.join(problem_list(p, ctx))


def kinds(texts):
    """Вид нарушения без чисел («ветер 9 м/с…» → «ветер»), чтобы колебания прогноза не считались новой проблемой."""
    return {t.split(' ')[0] for t in texts}


def problem_list(p, ctx):
    out = []
    if overlaps(p['span'], ctx['events_busy']):
        out.append('пересечение с событием')
    if p['tpl']['use_weather'] and ctx.get('forecast'):
        hrs = hours_of(ctx['forecast'], p['start'], p['end'])
        if hrs:
            out += [t for t, _ in weather_check(p['tpl'], hrs)[0]]
    return out


def make_placed(task_id, tpl, start, pinned=False, accepted=()):
    end = start + timedelta(minutes=tpl['duration'])
    span = (start - timedelta(minutes=tpl['buf_before']), end + timedelta(minutes=tpl['buf_after']))
    return {'id': task_id, 'tpl': tpl, 'start': start, 'end': end, 'span': span, 'pinned': pinned,
            'accepted': set(accepted)}


def place_by_priority(items, ctx):
    """items: [(task_id, tpl)]. Ставит в лучший слот без нарушений, по возрастанию приоритета.
    Меняет ctx['placed']. -> {task_id: (start|None, reason|None)}"""
    res = {}
    for tid, tpl in sorted(items, key=lambda x: x[1]['priority']):
        r = find_slots(tpl, ctx, task_id=tid, n=1)
        if r['slots'] and not r['worst']:
            start = r['slots'][0]['start']
            ctx['placed'] = [p for p in ctx['placed'] if p['id'] != tid] + [make_placed(tid, tpl, start)]
            res[tid] = (start, None)
        elif r['slots']:
            res[tid] = (None, 'нет слота без нарушений: ' + '; '.join(r['slots'][0]['violations']))
        else:
            res[tid] = (None, r['reason'])
    return res


def autoshift(ctx):
    """Автоперенос незакреплённых будущих задач, у которых появилось новое нарушение (погода или событие).
    Нарушения, уже принятые при постановке (accepted), повод для переноса не дают.
    Если лучшего слота нет, задача остаётся на месте с предупреждением — из календаря не снимается.
    -> [{'id', 'old', 'new', 'accepted', 'message'}]"""
    tz, now = ctx['tz'], ctx['now']
    todo = {}
    for p in ctx['placed']:
        if not p['pinned'] and p['start'] > now:
            probs = problem_list(p, ctx)
            if kinds(probs) - p['accepted']:
                todo[p['id']] = (p, probs)
    res = place_by_priority([(k, p['tpl']) for k, (p, _) in todo.items()], ctx)
    out = []
    for tid, (new, _) in res.items():
        p, probs = todo[tid]
        name, why = p['tpl']['name'], ', '.join(probs)
        if new:
            msg = f'«{name}» перенесена с {fmt(p["start"], tz)} на {fmt(new, tz)}: {why}'
            accepted = set()
        else:
            msg = f'«{name}» ({fmt(p["start"], tz)}): {why}. Лучшего слота нет — задача оставлена на месте'
            accepted = kinds(probs)
        out.append({'id': tid, 'old': p['start'], 'new': new, 'accepted': accepted, 'message': msg})
    return out
