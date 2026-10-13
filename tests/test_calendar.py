import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import ical

TZ = ZoneInfo('Europe/Moscow')
UTC = timezone.utc
LO, HI = datetime(2026, 1, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC)

ICS = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VTIMEZONE
TZID:Europe/Berlin
BEGIN:STANDARD
DTSTART:19701025T030000
END:STANDARD
END:VTIMEZONE
BEGIN:VEVENT
UID:series-1
SUMMARY:Пары\\, лекции
DTSTART;TZID=Europe/Berlin:20261012T090000
DTEND;TZID=Europe/Berlin:20261012T103000
RRULE:FREQ=WEEKLY;BYDAY=MO,WE;UNTIL=20261031T000000Z
EXDATE;TZID=Europe/Berlin:20261014T090000
BEGIN:VALARM
TRIGGER:-PT15M
SUMMARY:alarm
END:VALARM
END:VEVENT
BEGIN:VEVENT
UID:series-1
RECURRENCE-ID;TZID=Europe/Berlin:20261019T090000
SUMMARY:Пары (перенос)
DTSTART;TZID=Europe/Berlin:20261019T120000
DTEND;TZID=Europe/Berlin:20261019T133000
END:VEVENT
BEGIN:VEVENT
UID:one-2
SUMMARY:Врач
DTSTART:20261013T080000Z
DURATION:PT45M
END:VEVENT
BEGIN:VEVENT
UID:allday
SUMMARY:Праздник
DTSTART;VALUE=DATE:20261104
END:VEVENT
END:VCALENDAR
"""


def occ_starts(ev):
    return [st.astimezone(TZ).strftime('%d %H:%M') for _, st, _ in ical.occurrences(ev, TZ, LO, HI)]


class Ics(unittest.TestCase):
    def test_parse(self):
        evs = {e['title']: e for e in ical.parse_ics(ICS, TZ)}
        self.assertEqual(set(evs), {'Пары, лекции', 'Пары (перенос)', 'Врач', 'Праздник'})
        s = evs['Пары, лекции']
        self.assertEqual(s['tzid'], 'Europe/Berlin')
        # пн 12, (ср 14 исключена), пн 19 перенесён отдельным событием, ср 21, пн 26, ср 28; 09:00 Берлина = 10:00 Москвы
        self.assertEqual(occ_starts(s), ['12 10:00', '21 10:00', '26 11:00', '28 11:00'])  # с 25.10 в Берлине зима
        self.assertEqual(occ_starts(evs['Пары (перенос)']), ['19 13:00'])
        self.assertEqual(occ_starts(evs['Врач']), ['13 11:00'])
        self.assertEqual(evs['Врач']['end'], '2026-10-13T08:45:00')
        self.assertTrue(evs['Праздник']['all_day'])

    def test_exdate_and_until(self):
        ev = {'start': '2026-10-12T09:00:00', 'end': '2026-10-12T10:00:00', 'tzid': 'Europe/Moscow',
              'rrule': 'FREQ=DAILY;INTERVAL=2', 'exdates': json.dumps(['2026-10-14T09:00:00'])}
        self.assertEqual(occ_starts(ev)[:3], ['12 09:00', '16 09:00', '18 09:00'])
        ev['rrule'] = ical.set_until(ev['rrule'], datetime(2026, 10, 17, 23, 59))
        self.assertEqual(occ_starts(ev), ['12 09:00', '16 09:00'])

    def test_merge_new_file_wins(self):
        old = [{'id': 1, 'start': '2026-10-13T10:00:00', 'end': '2026-10-13T11:00:00', 'tzid': 'Europe/Moscow',
                'rrule': None, 'exdates': '[]', 'all_day': 0},
               {'id': 2, 'start': '2026-10-12T18:00:00', 'end': '2026-10-12T19:00:00', 'tzid': 'Europe/Moscow',
                'rrule': 'FREQ=DAILY;COUNT=5', 'exdates': '[]', 'all_day': 0},
               {'id': 3, 'start': '2026-10-20T10:00:00', 'end': '2026-10-20T11:00:00', 'tzid': 'Europe/Moscow',
                'rrule': None, 'exdates': '[]', 'all_day': 0}]
        new = [{'start': '2026-10-13T10:30:00', 'end': '2026-10-13T12:00:00', 'tzid': 'Europe/Moscow',
                'rrule': None, 'exdates': [], 'all_day': 0},
               {'start': '2026-10-14T18:30:00', 'end': '2026-10-14T19:30:00', 'tzid': 'Europe/Moscow',
                'rrule': None, 'exdates': [], 'all_day': 0},
               {'start': '2026-10-20T00:00:00', 'end': '2026-10-21T00:00:00', 'tzid': None,
                'rrule': None, 'exdates': [], 'all_day': 1}]
        deletes, exd = ical.merge_plan(old, new, TZ, LO, HI)
        self.assertEqual(deletes, [1])  # событие на весь день не вытесняет событие со временем
        self.assertEqual(exd, {2: ['2026-10-14T18:00:00']})


class ServerFlow(unittest.TestCase):
    """Сценарии приёмки через обработчики сервера на временной БД."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ['PLANNER_DB'] = os.path.join(self.tmp, 'db.sqlite3')
        import importlib
        import server
        self.s = importlib.reload(server)
        self.s.init_db()

    def call(self, fn, body=None, q=None, *args):
        with self.s.db() as c:
            return fn(c, q or {}, body if body is not None else {}, *args)

    def cal(self, start='2026-10-12T00:00:00', end='2026-10-26T00:00:00'):
        return self.call(self.s.get_calendar, q={'start': start, 'end': end})['events']

    def test_delete_one_occurrence_keeps_rest(self):
        self.call(self.s.create_event, {'title': 'Йога', 'start': '2026-10-12T19:00:00', 'end': '2026-10-12T20:00:00',
                                        'rrule': 'FREQ=WEEKLY;BYDAY=MO,WE,FR'})
        evs = self.cal()
        self.assertEqual(len(evs), 6)
        target = evs[1]
        with self.s.db() as c:
            self.s.delete_event(c, {'scope': 'one', 'occ': target['occ']}, {}, str(target['event_id']))
        left = [e['start'] for e in self.cal()]
        self.assertEqual(len(left), 5)
        self.assertNotIn(target['start'], left)

    def test_edit_following_splits_series(self):
        self.call(self.s.create_event, {'title': 'Йога', 'start': '2026-10-12T19:00:00', 'end': '2026-10-12T20:00:00',
                                        'rrule': 'FREQ=DAILY'})
        evs = self.cal('2026-10-12T00:00:00', '2026-10-17T00:00:00')
        with self.s.db() as c:
            self.s.update_event(c, {}, {'scope': 'following', 'occ': evs[2]['occ'], 'title': 'Йога',
                                        'start': '2026-10-14T08:00:00', 'end': '2026-10-14T09:00:00'},
                                str(evs[2]['event_id']))
        starts = [e['start'][5:16] for e in self.cal('2026-10-12T00:00:00', '2026-10-17T00:00:00')]
        self.assertEqual(sorted(starts), ['10-12T19:00', '10-13T19:00', '10-14T08:00', '10-15T08:00', '10-16T08:00'])

    def test_reimport_overlapping_keeps_new(self):
        first = ICS.replace('Врач', 'Старый врач')
        second = "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:x\nSUMMARY:Новый врач\nDTSTART:20261013T081500Z\n" \
                 "DTEND:20261013T090000Z\nEND:VEVENT\nEND:VCALENDAR\n"
        self.call(self.s.import_ics, first)
        self.call(self.s.import_ics, second)
        titles = [e['title'] for e in self.cal()]
        self.assertIn('Новый врач', titles)
        self.assertNotIn('Старый врач', titles)
        self.assertIn('Пары, лекции', titles)

    def test_data_persists(self):
        self.call(self.s.create_event, {'title': 'A', 'start': '2026-10-12T19:00:00', 'end': '2026-10-12T20:00:00'})
        import importlib
        s2 = importlib.reload(self.s)
        with s2.db() as c:
            self.assertEqual([e['title'] for e in s2.get_calendar(c, {'start': '2026-10-12T00:00:00',
                                                                      'end': '2026-10-13T00:00:00'}, {})['events']], ['A'])


if __name__ == '__main__':
    unittest.main()
