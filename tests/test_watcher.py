import unittest
from datetime import date, datetime, timedelta

from railcatch.errors import BlockedError
from railcatch.models import Availability, Provider, SeatClass, TimeWindow
from railcatch.watcher import Watch, WatchSpec, WatchState
from tests.fakes import FakeProvider, RecordingNotifier, make_train

OPEN = Availability.AVAILABLE
SHUT = Availability.SOLD_OUT
CREDS = ("user", "pw")


def spec(**kw) -> WatchSpec:
    base = dict(
        provider=Provider.SRT,
        dep="수서",
        arr="부산",
        day=date.today(),
        window=TimeWindow.parse("00:00-23:59"),
        seat_class=SeatClass.ANY,
        passengers=1,
        auto_reserve=True,
    )
    base.update(kw)
    return WatchSpec(**base)


def run_watch(provider, notifier, watch_spec=None, timeout=5.0) -> Watch:
    """감시를 끝까지 돌린다. 테스트는 실시간 대기 없이 끝나야 한다."""
    watch = Watch(watch_spec or spec(), provider, notifier, CREDS)
    watch.start()
    watch.join(timeout=timeout)
    watch.stop()
    return watch


class TestReserveFlow(unittest.TestCase):
    def test_reserves_and_stops_on_first_open_seat(self):
        provider = FakeProvider([
            [make_train("301", general=SHUT)],
            [make_train("301", general=OPEN)],
        ])
        notifier = RecordingNotifier()
        watch = run_watch(provider, notifier)

        self.assertEqual(watch.status.state, WatchState.SUCCEEDED)
        self.assertEqual(watch.status.reservation.reservation_number, "ABC12345")
        self.assertEqual(len(provider.reserve_calls), 1, "성공 후에도 예약을 더 시도하면 안 된다")
        self.assertIn("선점", notifier.messages[-1][0] + notifier.messages[-1][1])

    def test_notification_carries_reservation_and_deadline(self):
        provider = FakeProvider([[make_train("301", general=OPEN)]])
        notifier = RecordingNotifier()
        run_watch(provider, notifier)

        title, body = notifier.messages[-1]
        self.assertIn("선점", title)
        self.assertIn("ABC12345", body)
        self.assertIn("결제", body)

    def test_keeps_polling_while_sold_out(self):
        provider = FakeProvider([
            [make_train("301", general=SHUT)],
            [make_train("301", general=SHUT)],
            [make_train("301", general=OPEN)],
        ])
        watch = run_watch(provider, RecordingNotifier())
        self.assertEqual(watch.status.state, WatchState.SUCCEEDED)
        self.assertGreaterEqual(provider.searches, 3)

    def test_retries_next_train_when_seat_vanishes(self):
        provider = FakeProvider(
            [[make_train("301", hour=9, general=OPEN), make_train("305", hour=11, general=OPEN)]],
            reserve_fails=1,
        )
        watch = run_watch(provider, RecordingNotifier())
        self.assertEqual(watch.status.state, WatchState.SUCCEEDED)
        self.assertEqual(len(provider.reserve_calls), 2)
        self.assertEqual(provider.reserve_calls[1][0].train_number, "305")

    def test_notify_only_does_not_reserve(self):
        provider = FakeProvider([[make_train("301", general=OPEN)]])
        notifier = RecordingNotifier()
        watch = run_watch(provider, notifier, spec(auto_reserve=False))

        self.assertEqual(watch.status.state, WatchState.SUCCEEDED)
        self.assertEqual(provider.reserve_calls, [])
        self.assertIn("빈자리", notifier.messages[-1][0])


class TestFiltering(unittest.TestCase):
    def test_ignores_trains_outside_time_window(self):
        provider = FakeProvider([
            [make_train("301", hour=6, general=OPEN)],   # 창 밖
            [make_train("305", hour=10, general=OPEN)],  # 창 안
        ])
        watch = run_watch(provider, RecordingNotifier(), spec(window=TimeWindow.parse("09:00-12:00")))

        self.assertEqual(watch.status.state, WatchState.SUCCEEDED)
        self.assertEqual([t.train_number for t, _ in provider.reserve_calls], ["305"])

    def test_train_number_filter(self):
        provider = FakeProvider([
            [make_train("301", hour=9, general=OPEN), make_train("305", hour=10, general=OPEN)],
        ])
        watch = run_watch(provider, RecordingNotifier(), spec(train_numbers=("305",)))
        self.assertEqual([t.train_number for t, _ in provider.reserve_calls], ["305"])
        self.assertEqual(watch.status.state, WatchState.SUCCEEDED)

    def test_seat_class_filter_skips_wrong_class(self):
        provider = FakeProvider([
            [make_train("301", general=OPEN, special=SHUT)],   # 특실만 원하는데 일반실만 열림
            [make_train("301", general=OPEN, special=OPEN)],
        ])
        watch = run_watch(provider, RecordingNotifier(), spec(seat_class=SeatClass.SPECIAL))
        self.assertEqual(watch.status.state, WatchState.SUCCEEDED)
        self.assertEqual([sc for _, sc in provider.reserve_calls], [SeatClass.SPECIAL])

    def test_waitlist_alone_does_not_trigger(self):
        provider = FakeProvider([
            [make_train("301", general=Availability.WAITLIST)],
            [make_train("301", general=OPEN)],
        ])
        watch = run_watch(provider, RecordingNotifier())
        self.assertEqual(watch.status.state, WatchState.SUCCEEDED)
        self.assertEqual(len(provider.reserve_calls), 1)


class TestFailureHandling(unittest.TestCase):
    def test_login_failure_stops_immediately(self):
        provider = FakeProvider([[make_train("301", general=OPEN)]], login_error=True)
        notifier = RecordingNotifier()
        watch = run_watch(provider, notifier)

        self.assertEqual(watch.status.state, WatchState.FAILED)
        self.assertEqual(provider.searches, 0)
        self.assertIn("로그인", watch.status.last_message)
        self.assertTrue(notifier.messages)

    def test_blocked_stops_watching(self):
        class Blocking(FakeProvider):
            def search(self, *a, **kw):
                self.searches += 1
                raise BlockedError("과도한 요청으로 차단되었습니다")

        provider = Blocking([[]])
        notifier = RecordingNotifier()
        watch = run_watch(provider, notifier)

        self.assertEqual(watch.status.state, WatchState.FAILED)
        self.assertEqual(provider.searches, 1, "차단 후에는 다시 두드리면 안 된다")
        self.assertIn("중단", notifier.messages[-1][0])

    def test_expired_watch_stops(self):
        provider = FakeProvider([[make_train("301", general=SHUT)]])
        watch = run_watch(
            provider, RecordingNotifier(),
            spec(expires_at=datetime.now() - timedelta(seconds=1)),
        )
        self.assertEqual(watch.status.state, WatchState.STOPPED)
        self.assertEqual(provider.searches, 0)

    def test_past_date_stops(self):
        provider = FakeProvider([[make_train("301", general=SHUT)]])
        watch = run_watch(provider, RecordingNotifier(), spec(day=date.today() - timedelta(days=1)))
        self.assertEqual(watch.status.state, WatchState.STOPPED)

    def test_stop_is_idempotent_and_reported(self):
        provider = FakeProvider([[make_train("301", general=SHUT)]])
        watch = Watch(spec(), provider, RecordingNotifier(), CREDS)
        watch.start()
        provider.searched.wait(timeout=3.0)
        watch.stop()
        watch.stop()
        watch.join(timeout=3.0)
        self.assertEqual(watch.status.state, WatchState.STOPPED)
        self.assertFalse(watch.running)


class TestSpecValidation(unittest.TestCase):
    def test_rejects_bad_passenger_counts(self):
        for n in (0, -1, 10):
            with self.assertRaises(ValueError):
                spec(passengers=n)

    def test_status_serializes(self):
        provider = FakeProvider([[make_train("301", general=OPEN)]])
        watch = run_watch(provider, RecordingNotifier())
        payload = watch.status.to_dict()
        self.assertEqual(payload["state"], "succeeded")
        self.assertEqual(payload["reservation"]["reservation_number"], "ABC12345")
        self.assertIn("수서", payload["title"])


if __name__ == "__main__":
    unittest.main()
