import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from railcatch.errors import ConfigError
from railcatch.waitlist import (
    PAYMENT_REPEAT,
    Reminder,
    Stage,
    WaitlistEntry,
    WaitlistStore,
    parse_departure,
    send_due_reminders,
)
from tests.fakes import RecordingNotifier


def entry(days_ahead: float = 5, **kw) -> WaitlistEntry:
    base = dict(
        train="KTX 101",
        route="서울→부산",
        depart_at=datetime.now() + timedelta(days=days_ahead),
    )
    base.update(kw)
    return WaitlistEntry(**base)


class TestReminderTiming(unittest.TestCase):
    def test_unregistered_entry_asks_to_register_once(self):
        e = entry()
        self.assertEqual(e.due_reminders(), [Reminder.REGISTER])
        e.reminded[Reminder.REGISTER.value] = datetime.now()
        self.assertEqual(e.due_reminders(), [])

    def test_registered_entry_is_not_nagged_to_register(self):
        e = entry(stage=Stage.REGISTERED)
        self.assertEqual(e.due_reminders(), [])

    def test_day_before_reminder_fires_the_evening_before(self):
        depart = datetime.now().replace(microsecond=0) + timedelta(days=30)
        depart = depart.replace(hour=8, minute=0, second=0)
        e = entry(depart_at=depart, stage=Stage.REGISTERED)

        evening_before = depart - timedelta(days=1)
        self.assertEqual(
            e.due_reminders(now=evening_before.replace(hour=17, minute=0)), [],
            "전날 저녁 전에는 아직 보내지 않는다",
        )
        self.assertEqual(
            e.due_reminders(now=evening_before.replace(hour=19, minute=0)),
            [Reminder.DAY_BEFORE],
        )

    def test_departure_reminder_fires_close_to_departure(self):
        depart = datetime.now() + timedelta(days=2)
        e = entry(depart_at=depart, stage=Stage.REGISTERED)
        e.reminded[Reminder.DAY_BEFORE.value] = datetime.now()
        due = e.due_reminders(now=depart - timedelta(hours=1))
        self.assertEqual(due, [Reminder.DEPARTURE])

    def test_assigned_entry_repeats_payment_reminder(self):
        e = entry(stage=Stage.ASSIGNED)
        now = datetime.now()
        self.assertEqual(e.due_reminders(now), [Reminder.PAYMENT])

        e.reminded[Reminder.PAYMENT.value] = now
        self.assertEqual(e.due_reminders(now + timedelta(minutes=30)), [])
        self.assertEqual(
            e.due_reminders(now + PAYMENT_REPEAT + timedelta(minutes=1)),
            [Reminder.PAYMENT],
            "결제 기한을 놓치면 좌석이 날아가므로 반복해서 알려야 한다",
        )

    def test_assigned_entry_skips_other_reminders(self):
        e = entry(days_ahead=0.1, stage=Stage.ASSIGNED)
        self.assertEqual(e.due_reminders(), [Reminder.PAYMENT])

    def test_done_and_departed_entries_are_silent(self):
        self.assertEqual(entry(stage=Stage.DONE).due_reminders(), [])
        self.assertEqual(entry(days_ahead=-1).due_reminders(), [])


class TestMessages(unittest.TestCase):
    def test_payment_message_includes_deadline(self):
        deadline = datetime.now() + timedelta(minutes=30)
        e = entry(stage=Stage.ASSIGNED, pay_deadline=deadline)
        title, body = e.message_for(Reminder.PAYMENT)
        self.assertIn("결제", title)
        self.assertIn(deadline.strftime("%m/%d %H:%M"), body)

    def test_register_message_mentions_the_train(self):
        title, body = entry().message_for(Reminder.REGISTER)
        self.assertIn("예약대기", title)
        self.assertIn("KTX 101", body)


class TestStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "waitlist.json"

    def tearDown(self):
        self.dir.cleanup()

    def store(self) -> WaitlistStore:
        return WaitlistStore(self.path)

    def test_round_trip(self):
        s = self.store()
        added = s.add(entry(note="차선책"))
        reloaded = self.store().get(added.id)
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.note, "차선책")
        self.assertEqual(reloaded.depart_at, added.depart_at)

    def test_lookup_by_id_prefix(self):
        s = self.store()
        added = s.add(entry())
        self.assertIsNotNone(s.get(added.id[:4]))

    def test_missing_file_is_empty_not_error(self):
        self.assertEqual(self.store().load(), [])

    def test_corrupt_entry_does_not_lose_the_rest(self):
        good = entry()
        self.path.write_text(
            json.dumps([{"broken": True}, good.to_dict()], ensure_ascii=False),
            encoding="utf-8",
        )
        entries = self.store().load()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].train, "KTX 101")

    def test_unreadable_file_raises_clear_error(self):
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ConfigError):
            self.store().load()

    def test_set_stage_resets_payment_reminder(self):
        s = self.store()
        added = s.add(entry(stage=Stage.REGISTERED))
        added.reminded[Reminder.PAYMENT.value] = datetime.now()
        updated = s.set_stage(added.id, Stage.ASSIGNED)
        self.assertNotIn(Reminder.PAYMENT.value, updated.reminded)

    def test_set_stage_unknown_id_raises(self):
        with self.assertRaises(ConfigError):
            self.store().set_stage("nope", Stage.DONE)

    def test_remove(self):
        s = self.store()
        added = s.add(entry())
        self.assertTrue(s.remove(added.id))
        self.assertFalse(s.remove(added.id))
        self.assertEqual(self.store().load(), [])

    def test_purge_departed(self):
        s = self.store()
        s.add(entry(days_ahead=3))
        s.add(entry(days_ahead=-2))
        self.assertEqual(s.purge_departed(), 1)
        self.assertEqual(len(self.store().load()), 1)

    def test_active_sorted_by_departure(self):
        s = self.store()
        s.add(entry(days_ahead=5, train="늦은차"))
        s.add(entry(days_ahead=2, train="빠른차"))
        s.add(entry(days_ahead=3, stage=Stage.DONE, train="완료"))
        self.assertEqual([e.train for e in s.active()], ["빠른차", "늦은차"])


class TestSendDueReminders(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = WaitlistStore(Path(self.dir.name) / "waitlist.json")
        self.notifier = RecordingNotifier()

    def tearDown(self):
        self.dir.cleanup()

    def test_sends_and_records_so_it_does_not_repeat(self):
        self.store.add(entry())
        sent = send_due_reminders(self.store, self.notifier)
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(self.notifier.messages), 1)

        again = send_due_reminders(self.store, self.notifier)
        self.assertEqual(again, [], "같은 알림을 두 번 보내면 안 된다")
        self.assertEqual(len(self.notifier.messages), 1)

    def test_record_survives_restart(self):
        self.store.add(entry())
        send_due_reminders(self.store, self.notifier)

        fresh_store = WaitlistStore(self.store.path)
        self.assertEqual(
            send_due_reminders(fresh_store, self.notifier), [],
            "재시작해도 이미 보낸 알림을 다시 보내면 안 된다",
        )

    def test_notifier_failure_does_not_stop_other_entries(self):
        class Broken(RecordingNotifier):
            def send(self, title, body):
                raise RuntimeError("텔레그램 죽음")

        self.store.add(entry())
        self.store.add(entry(train="KTX 105"))
        sent = send_due_reminders(self.store, Broken())
        self.assertEqual(len(sent), 2, "알림 실패가 진행을 막으면 안 된다")


class TestParseDeparture(unittest.TestCase):
    def test_formats(self):
        day = date(2026, 9, 20)
        self.assertEqual(parse_departure(day, "08:30"), datetime(2026, 9, 20, 8, 30))
        self.assertEqual(parse_departure(day, "0830"), datetime(2026, 9, 20, 8, 30))
        self.assertEqual(parse_departure(day, "830"), datetime(2026, 9, 20, 8, 30))

    def test_rejects_garbage(self):
        for bad in ("아침", "25:00", "8", "123456"):
            with self.assertRaises(ValueError):
                parse_departure(date(2026, 9, 20), bad)


if __name__ == "__main__":
    unittest.main()
