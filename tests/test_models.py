import unittest
from datetime import date, time

from railcatch.models import Availability, SeatClass, TimeWindow, parse_date
from tests.fakes import make_train


class TestTimeWindow(unittest.TestCase):
    def test_parse_variants(self):
        self.assertEqual(str(TimeWindow.parse("08:00-12:30")), "08:00-12:30")
        self.assertEqual(str(TimeWindow.parse("8:00~12:30")), "08:00-12:30")

    def test_contains(self):
        w = TimeWindow.parse("08:00-12:30")
        self.assertTrue(w.contains(time(8, 0)))
        self.assertTrue(w.contains(time(12, 30)))
        self.assertFalse(w.contains(time(12, 31)))

    def test_overnight_window_wraps(self):
        w = TimeWindow.parse("22:00-02:00")
        self.assertTrue(w.contains(time(23, 30)))
        self.assertTrue(w.contains(time(1, 0)))
        self.assertFalse(w.contains(time(12, 0)))

    def test_rejects_garbage(self):
        for bad in ("", "08:00", "25:00-26:00", "a-b"):
            with self.assertRaises(ValueError):
                TimeWindow.parse(bad)


class TestParseDate(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(parse_date("2026-09-20"), date(2026, 9, 20))
        self.assertEqual(parse_date("20260920"), date(2026, 9, 20))
        self.assertEqual(parse_date("2026/09/20"), date(2026, 9, 20))

    def test_rejects_garbage(self):
        with self.assertRaises(ValueError):
            parse_date("내일")


class TestTrainAvailability(unittest.TestCase):
    def test_any_prefers_available_over_waitlist(self):
        t = make_train(general=Availability.WAITLIST, special=Availability.AVAILABLE)
        self.assertIs(t.availability_for(SeatClass.ANY), Availability.AVAILABLE)

    def test_any_falls_back_to_waitlist(self):
        t = make_train(general=Availability.WAITLIST, special=Availability.SOLD_OUT)
        self.assertIs(t.availability_for(SeatClass.ANY), Availability.WAITLIST)

    def test_catchable_classes_ordering(self):
        t = make_train(general=Availability.AVAILABLE, special=Availability.AVAILABLE)
        self.assertEqual(
            t.catchable_classes(SeatClass.ANY), [SeatClass.GENERAL, SeatClass.SPECIAL]
        )

    def test_waitlist_is_not_catchable(self):
        t = make_train(general=Availability.WAITLIST)
        self.assertEqual(t.catchable_classes(SeatClass.ANY), [])

    def test_specific_class_ignores_other(self):
        t = make_train(general=Availability.SOLD_OUT, special=Availability.AVAILABLE)
        self.assertEqual(t.catchable_classes(SeatClass.GENERAL), [])
        self.assertEqual(t.catchable_classes(SeatClass.SPECIAL), [SeatClass.SPECIAL])

    def test_key_is_stable(self):
        self.assertEqual(make_train().key, make_train().key)
        self.assertNotEqual(make_train("301").key, make_train("305").key)


if __name__ == "__main__":
    unittest.main()
