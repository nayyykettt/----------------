"""Повторы событий, разбор .ics и правила совмещения импортов. Без БД."""
import json
import re
from bisect import bisect_left
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dateutil.rrule import rruleset, rrulestr

UTC = timezone.utc
FMT = '%Y-%m-%dT%H:%M:%S'


def zone(name, fallback):
    if name == 'UTC':
        return UTC
    try:
        return ZoneInfo(name) if name else fallback
    except Exception:  # нестандартные TZID (например, из Outlook) — считаем временем города
        return fallback


def norm_rrule(rule, tz):
    """UNTIL в UTC или датой → локальное время события (dateutil требует единообразия с dtstart)."""
    def fix(m):
        v = m.group(1)
        if v.endswith('Z'):
            return 'UNTIL=' + datetime.strptime(v, '%Y%m%dT%H%M%SZ').replace(tzinfo=UTC) \
                .astimezone(tz).strftime('%Y%m%dT%H%M%S')
        return f'UNTIL={v}T235959' if len(v) == 8 else 'UNTIL=' + v
    return re.sub(r'UNTIL=([0-9TZ]+)', fix, rule.removeprefix('RRULE:'))


def set_until(rule, dt):
    """Обрезать серию: всё до dt включительно."""
    parts = [p for p in rule.removeprefix('RRULE:').split(';') if p and not p.startswith(('UNTIL=', 'COUNT='))]
    return ';'.join(parts + ['UNTIL=' + dt.strftime('%Y%m%dT%H%M%S')])


def occurrences(ev, city_tz, lo, hi):
    """Вхождения события, пересекающие [lo, hi). -> [(occ_naive, start_aware, end_aware)]"""
    tz = zone(ev['tzid'], city_tz)
    s = datetime.fromisoformat(ev['start'])
    dur = datetime.fromisoformat(ev['end']) - s
    starts = [s]
    if ev['rrule']:
        ex = ev['exdates'] or []
        ex = json.loads(ex) if isinstance(ex, str) else ex
        try:
            rs = rruleset()
            rs.rrule(rrulestr(norm_rrule(ev['rrule'], tz), dtstart=s))
            for x in ex:
                rs.exdate(datetime.fromisoformat(x))
            a = lo.astimezone(tz).replace(tzinfo=None) - dur - timedelta(days=1)
            b = hi.astimezone(tz).replace(tzinfo=None) + timedelta(days=1)
            starts = rs.between(a, b, inc=True)
        except Exception:
            pass  # битое правило — показываем как разовое событие
    out = []
    for o in starts:
        st, en = o.replace(tzinfo=tz), (o + dur).replace(tzinfo=tz)
        if st < hi and en > lo:
            out.append((o, st, en))
    return out


# ---------- .ics ----------

def _dt(params, val, city_tz, target=None):
    """-> (naive в поясе события, имя пояса|None, all_day)"""
    val = val.strip()
    if params.get('VALUE') == 'DATE' or len(val) == 8:
        return datetime.strptime(val[:8], '%Y%m%d'), None, True
    if val.endswith('Z'):
        src = UTC
        aware = datetime.strptime(val, '%Y%m%dT%H%M%SZ').replace(tzinfo=UTC)
    else:
        src = zone(params.get('TZID'), city_tz)
        aware = datetime.strptime(val[:15], '%Y%m%dT%H%M%S').replace(tzinfo=src)
    out = zone(target, city_tz) if target else src
    name = 'UTC' if out is UTC else out.key
    return aware.astimezone(out).replace(tzinfo=None), name, False


def _duration(v):
    m = re.match(r'([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?', v.strip())
    w, d, h, mi, s = (int(x or 0) for x in m.groups()[1:])
    return timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s)


def _text(v):
    return v.replace('\\n', '\n').replace('\\N', '\n').replace('\\,', ',').replace('\\;', ';').replace('\\\\', '\\')


def parse_ics(text, city_tz):
    """-> [dict(title, start, end, all_day, tzid, rrule, exdates[list], source, uid)]"""
    comps, stack = [], []
    for line in re.sub(r'\r?\n[ \t]', '', text).splitlines():
        if line.startswith('BEGIN:'):
            stack.append((line[6:].strip().upper(), {}))
        elif line.startswith('END:'):
            if stack:
                name, props = stack.pop()
                if name == 'VEVENT':
                    comps.append(props)
        elif stack and ':' in line:
            head, val = line.split(':', 1)
            name, *ps = head.split(';')
            params = {k.upper(): v.strip('"') for k, _, v in (p.partition('=') for p in ps)}
            stack[-1][1].setdefault(name.upper(), []).append((params, val))

    events, overrides = [], []
    for p in comps:
        if 'DTSTART' not in p:
            continue
        start, tzid, all_day = _dt(*p['DTSTART'][0], city_tz)
        if 'DTEND' in p:
            end = _dt(*p['DTEND'][0], city_tz, tzid)[0]
        elif 'DURATION' in p:
            end = start + _duration(p['DURATION'][0][1])
        else:
            end = start + (timedelta(days=1) if all_day else timedelta(hours=1))
        if end <= start:
            end = start + (timedelta(days=1) if all_day else timedelta(minutes=30))
        ev = {'title': _text(p.get('SUMMARY', [({}, 'Без названия')])[0][1]),
              'start': start.strftime(FMT), 'end': end.strftime(FMT), 'all_day': int(all_day),
              'tzid': tzid, 'rrule': p['RRULE'][0][1] if 'RRULE' in p else None, 'exdates': [],
              'source': 'ics', 'uid': p.get('UID', [({}, '')])[0][1].strip()}
        for params, val in p.get('EXDATE', []):
            for v in val.split(','):
                ev['exdates'].append(_dt(params, v, city_tz, tzid)[0].strftime(FMT))
        cancelled = p.get('STATUS', [({}, '')])[0][1].strip().upper() == 'CANCELLED'
        if 'RECURRENCE-ID' in p:
            overrides.append((ev, p['RECURRENCE-ID'][0], cancelled))
        elif not cancelled:
            events.append(ev)
    masters = {e['uid']: e for e in events if e['rrule']}
    for ev, (params, val), cancelled in overrides:
        m = masters.get(ev['uid'])
        if m:  # изменённое вхождение серии: убрать его из серии, добавить отдельным событием
            m['exdates'].append(_dt(params, val, city_tz, m['tzid'])[0].strftime(FMT))
        if not cancelled:
            ev['rrule'] = None
            events.append(ev)
    return events


def merge_plan(old_events, new_events, city_tz, lo, hi):
    """Старые импортированные события, пересекающиеся по времени с новым файлом, уступают ему.
    Сравниваются только события одного вида (на весь день / со временем).
    -> (ids к удалению, {id: [exdates к добавлению]})"""
    unions = {}
    for kind in (0, 1):
        iv = sorted((st, en) for ev in new_events if ev['all_day'] == kind
                    for _, st, en in occurrences(ev, city_tz, lo, hi))
        u = []
        for st, en in iv:
            if u and st < u[-1][1]:
                u[-1][1] = max(u[-1][1], en)
            else:
                u.append([st, en])
        unions[kind] = (u, [x[0] for x in u])

    def hit(kind, st, en):
        u, starts = unions[kind]
        i = bisect_left(starts, en) - 1
        return i >= 0 and u[i][1] > st

    deletes, exd = [], {}
    for ev in old_events:
        hits = [o for o, st, en in occurrences(ev, city_tz, lo, hi) if hit(ev['all_day'], st, en)]
        if not hits:
            continue
        if ev['rrule']:
            exd[ev['id']] = [o.strftime(FMT) for o in hits]
        else:
            deletes.append(ev['id'])
    return deletes, exd
