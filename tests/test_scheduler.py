import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import scheduler as sch

TZ = ZoneInfo('Europe/Moscow')
UTC = timezone.utc
NOW = datetime(2026, 10, 12, 6, 0, tzinfo=TZ)  # понедельник 06:00 по Москве


def T(day, h, m=0):
    """Момент: NOW-дата + day дней, h:m по Москве."""
    return datetime.combine(NOW.date() + timedelta(days=day), datetime.min.time(), TZ).replace(hour=h, minute=m)


def forecast(days=7, **over):
    start = int(datetime.combine(NOW.date(), datetime.min.time(), TZ).timestamp())
    hours = {}
    for i in range(days * 24):
        t = start + i * 3600
        hours[t] = {'t': t, 'temp': 15, 'pp': 0, 'pr': 0, 'cloud': 20, 'wind': 2, 'gust': 4, 'code': 0, **over}
    return {'hours': hours, 'end': datetime.fromtimestamp(start + days * 24 * 3600, UTC)}


def tpl(**kw):
    t = {'id': 1, 'name': 'Тренировка', 'activity': 'physical', 'duration': 60, 'buf_before': 15, 'buf_after': 30,
         'win_from': '07:00', 'win_to': '14:00', 'bad_days': [6], 'use_weather': True, 't_min': 5, 't_max': 25,
         'precip_max': 20, 'cloud_max': 70, 'wind_max': 8, 'bad_phen': ['rain', 'snow', 'thunder'],
         'priority': 1, 'min_gap': 1}
    t.update(kw)
    return t


def ctx(**kw):
    c = {'now': NOW.astimezone(UTC), 'tz': TZ, 'events_busy': [], 'placed': [],
         'sleep': {'mode': 'simple', 'weekday': {'bed': '23:00', 'wake': '07:00'},
                   'weekend': {'bed': '23:00', 'wake': '07:00'}},
         'profiles': {'physical': {'from': '07:00', 'to': '14:00'}}, 'forecast': forecast()}
    c.update(kw)
    return c


def all_cands(t, c):
    """Все допустимые слоты (n с запасом)."""
    return sch.find_slots(t, c, n=1000)


class FindSlots(unittest.TestCase):
    def test_returns_3_to_5_clean_slots(self):
        r = sch.find_slots(tpl(), ctx())
        self.assertFalse(r['worst'])
        self.assertTrue(3 <= len(r['slots']) <= 5)
        self.assertTrue(all(not s['violations'] for s in r['slots']))

    def test_buffers_do_not_overlap_events(self):
        ev = (T(1, 9), T(1, 10))
        r = all_cands(tpl(), ctx(events_busy=[ev]))
        for s in r['slots']:
            span = (s['start'] - timedelta(minutes=15), s['end'] + timedelta(minutes=30))
            self.assertFalse(span[0] < ev[1] and ev[0] < span[1], s['start'])
        starts = {s['start'] for s in r['slots']}
        self.assertNotIn(T(1, 7, 45).astimezone(UTC), starts)  # 07:45–08:45 + буфер 30 мин = до 09:15
        self.assertNotIn(T(1, 10).astimezone(UTC), starts)     # буфер до (09:45) на событии

    def test_buffers_do_not_overlap_other_tasks_buffers(self):
        other = sch.make_placed(99, tpl(id=2, buf_after=60), T(1, 8))  # занято 07:45–10:00
        r = all_cands(tpl(min_gap=0), ctx(placed=[other]))
        for s in r['slots']:
            self.assertFalse(s['start'] - timedelta(minutes=15) < other['span'][1] and other['span'][0] < s['end'] + timedelta(minutes=30))

    def test_sleep(self):
        t = tpl(win_from='00:00', win_to='23:59', buf_before=0, buf_after=0, bad_days=[])
        r = all_cands(t, ctx())
        for s in r['slots']:
            ls, le = s['start'].astimezone(TZ), s['end'].astimezone(TZ)
            self.assertGreaterEqual(ls.hour, 7)
            self.assertTrue(le.hour < 23 or (le.hour == 23 and le.minute == 0))

    def test_sleep_daily_mode_and_after_midnight_bedtime(self):
        days = [{'bed': '23:00', 'wake': '07:00'}] * 7
        days[0] = {'bed': '01:00', 'wake': '07:00'}  # в пн ложимся в 01:00 ночи на вт
        iv = sch.sleep_intervals({'mode': 'daily', 'days': days}, TZ, NOW.date(), NOW.date())
        self.assertIn((T(1, 1).astimezone(UTC), T(1, 7).astimezone(UTC)), iv)

    def test_window_and_forbidden_days(self):
        r = all_cands(tpl(min_gap=0), ctx())
        self.assertTrue(r['slots'])
        for s in r['slots']:
            ls, le = s['start'].astimezone(TZ), s['end'].astimezone(TZ)
            self.assertNotEqual(ls.weekday(), 6)
            self.assertGreaterEqual(ls.hour * 60 + ls.minute, 7 * 60)
            self.assertLessEqual(le.hour * 60 + le.minute, 14 * 60)

    def test_starts_after_now_and_inside_horizon(self):
        c = ctx(forecast=forecast(days=3))
        r = all_cands(tpl(min_gap=0, bad_days=[]), c)
        for s in r['slots']:
            self.assertGreater(s['start'], c['now'])
            self.assertLessEqual(s['end'], c['forecast']['end'])

    def test_weather_thresholds_exclude_bad_hours(self):
        fc = forecast()
        bad = int(T(1, 9).timestamp())
        fc['hours'][bad] = {**fc['hours'][bad], 'code': 63, 'pp': 90}
        r = all_cands(tpl(min_gap=0), ctx(forecast=fc))
        for s in r['slots']:
            self.assertFalse(s['start'] < T(1, 10) and T(1, 9) < s['end'])

    def test_weather_check_messages(self):
        t = tpl()
        hrs = [{'temp': 30, 'pp': 50, 'cloud': 90, 'wind': 10, 'code': 95}]
        texts = [x for x, _ in sch.weather_check(t, hrs)[0]]
        self.assertIn('ветер 10 м/с при пороге 8', texts)
        self.assertIn('температура +30°C при максимуме +25', texts)
        self.assertIn('вероятность осадков 50% при пороге 20%', texts)
        self.assertIn('облачность 90% при пороге 70%', texts)
        self.assertIn('гроза', texts)

    def test_comfort_prefers_middle_temperature(self):
        t = tpl()
        self.assertGreater(sch.weather_check(t, [{'temp': 15, 'pp': 0, 'cloud': 0, 'wind': 0, 'code': 0}])[1],
                           sch.weather_check(t, [{'temp': 22, 'pp': 0, 'cloud': 0, 'wind': 0, 'code': 0}])[1])

    def test_ignores_weather_when_disabled(self):
        r = sch.find_slots(tpl(use_weather=False), ctx(forecast=forecast(code=63, pp=100)))
        self.assertFalse(r['worst'])

    def test_min_gap_between_same_template(self):
        placed = sch.make_placed(50, tpl(), T(2, 9))  # среда
        r = all_cands(tpl(), ctx(placed=[placed]))
        days = {s['start'].astimezone(TZ).date() for s in r['slots']}
        for d in (1, 2, 3):
            self.assertNotIn(T(d, 0).date(), days)

    def test_min_gap_violation_listed_when_only_option(self):
        placed = sch.make_placed(50, tpl(), T(2, 9))
        c = ctx(placed=[placed], forecast=forecast(days=4))  # пн(без утра)…чт; пн-ст-чт рядом со средой
        r = sch.find_slots(tpl(bad_days=[0, 4, 5, 6]), c)
        self.assertTrue(r['worst'])
        self.assertTrue(all(any('интервал' in v for v in s['violations']) for s in r['slots']))

    def test_profile_window_is_soft(self):
        c = ctx(profiles={'physical': {'from': '18:00', 'to': '20:00'}})
        r = sch.find_slots(tpl(), c)
        self.assertTrue(r['worst'])
        self.assertTrue(any('вне окна профиля' in v for v in r['slots'][0]['violations']))

    def test_custom_profile_and_no_profile(self):
        c = ctx(profiles={'c1': {'name': 'Вечер', 'from': '18:00', 'to': '20:00'}})
        r = sch.find_slots(tpl(activity='c1'), c)
        self.assertIn('вне окна профиля «Вечер» 18:00–20:00', r['slots'][0]['violations'])
        free = tpl(activity='', win_from='00:00', win_to='23:59', bad_days=[], use_weather=False, min_gap=0)
        r = all_cands(free, c)
        self.assertFalse(r['worst'])
        hours = {s['start'].astimezone(TZ).hour for s in r['slots']}
        self.assertEqual(min(hours), 7)
        self.assertEqual(max(hours), 21)  # конец ≤ 23:00 — ограничивает только сон

    def test_best_of_worst(self):
        r = sch.find_slots(tpl(), ctx(forecast=forecast(wind=10)))
        self.assertTrue(r['worst'])
        self.assertTrue(r['slots'])
        self.assertIn('ветер 10 м/с при пороге 8', r['slots'][0]['violations'])

    def test_best_of_worst_ranks_lighter_violations_first(self):
        fc = forecast(wind=20)
        for t, h in fc['hours'].items():
            if datetime.fromtimestamp(t, TZ).date() == T(3, 0).date():
                h['wind'] = 9
        r = sch.find_slots(tpl(), ctx(forecast=fc))
        self.assertEqual(r['slots'][0]['start'].astimezone(TZ).date(), T(3, 0).date())

    def test_hard_constraints_unsatisfiable(self):
        r = sch.find_slots(tpl(bad_days=list(range(7))), ctx())
        self.assertEqual(r['slots'], [])
        self.assertIn('запрещённые дни', r['reason'])

    def test_no_forecast(self):
        r = sch.find_slots(tpl(), ctx(forecast=None))
        self.assertEqual(r['reason'], 'нет прогноза на эти дни')
        self.assertTrue(sch.find_slots(tpl(use_weather=False), ctx(forecast=None))['slots'])


class AutoShift(unittest.TestCase):
    def rainy_tuesday(self):
        fc = forecast()
        for t, h in fc['hours'].items():
            if datetime.fromtimestamp(t, TZ).date() == T(1, 0).date():
                h.update(code=63, pp=90)
        return fc

    def test_moves_unpinned_task_later_with_message(self):
        p = sch.make_placed(7, tpl(), T(1, 9))
        c = ctx(placed=[p], forecast=self.rainy_tuesday())
        ch = sch.autoshift(c)
        self.assertEqual(len(ch), 1)
        self.assertGreater(ch[0]['new'], c['now'])
        self.assertNotEqual(ch[0]['new'].astimezone(TZ).date(), T(1, 0).date())
        self.assertIn('перенесена с вт 13.10 09:00', ch[0]['message'])
        self.assertIn('дождь', ch[0]['message'])

    def test_pinned_task_stays_but_gets_warning(self):
        p = sch.make_placed(7, tpl(), T(1, 9), pinned=True)
        c = ctx(placed=[p], forecast=self.rainy_tuesday())
        self.assertEqual(sch.autoshift(c), [])
        self.assertIn('дождь', sch.problems(p, c))

    def test_good_weather_task_untouched(self):
        c = ctx(placed=[sch.make_placed(7, tpl(), T(1, 9))])
        self.assertEqual(sch.autoshift(c), [])

    def test_no_slot_keeps_task_in_place(self):
        c = ctx(placed=[sch.make_placed(7, tpl(), T(1, 9))], forecast=forecast(code=63, pp=90))
        ch = sch.autoshift(c)
        self.assertIsNone(ch[0]['new'])
        self.assertIn('оставлена на месте', ch[0]['message'])
        self.assertEqual(ch[0]['accepted'], {'дождь', 'вероятность'})
        self.assertEqual([p['id'] for p in c['placed']], [7])

    def test_accepted_violations_do_not_trigger_move(self):
        # задачу поставили в «лучший из худших» слот с ветром — при обновлении страницы она не трогается
        p = sch.make_placed(7, tpl(), T(1, 9), accepted={'ветер'})
        self.assertEqual(sch.autoshift(ctx(placed=[p], forecast=forecast(wind=10))), [])
        p = sch.make_placed(7, tpl(), T(1, 9), accepted={'ветер'})  # а новый дождь — уже повод
        self.assertEqual(len(sch.autoshift(ctx(placed=[p], forecast=forecast(wind=10, code=63)))), 1)

    def test_overlap_with_new_event_triggers_move(self):
        p = sch.make_placed(7, tpl(), T(1, 9))
        ch = sch.autoshift(ctx(placed=[p], events_busy=[(T(1, 9, 30), T(1, 11))]))
        self.assertEqual(len(ch), 1)
        self.assertIn('пересечение с событием', ch[0]['message'])

    def test_priority_order(self):
        # свободно только одно утро; задача с приоритетом 1 должна получить его первой
        c = ctx(forecast=forecast(days=1))
        low, high = tpl(id=2, priority=3, min_gap=0), tpl(id=1, priority=1, min_gap=0, duration=360, win_from='07:15')
        res = sch.place_by_priority([(1, low), (2, high)], c)
        self.assertIsNotNone(res[2][0])
        self.assertIsNone(res[1][0])


if __name__ == '__main__':
    unittest.main()
