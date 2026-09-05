import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from railcatch.blocklist import COOLDOWN, BlockLog, BlockRecord
from railcatch.models import Provider


class TestBlockLog(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.log = BlockLog(Path(self.dir.name) / "blocked.json")

    def tearDown(self):
        self.dir.cleanup()

    def test_no_record_by_default(self):
        self.assertIsNone(self.log.active(Provider.KORAIL))

    def test_record_then_active(self):
        self.log.record(Provider.KORAIL, "MACRO ERROR")
        record = self.log.active(Provider.KORAIL)
        self.assertIsNotNone(record)
        self.assertEqual(record.reason, "MACRO ERROR")

    def test_record_is_per_provider(self):
        self.log.record(Provider.KORAIL, "MACRO ERROR")
        self.assertIsNone(self.log.active(Provider.SRT))

    def test_survives_reload(self):
        self.log.record(Provider.KORAIL, "MACRO ERROR")
        self.assertIsNotNone(BlockLog(self.log.path).active(Provider.KORAIL))

    def test_expires_after_cooldown(self):
        old = datetime.now() - COOLDOWN - timedelta(minutes=1)
        self.log.path.write_text(
            json.dumps({"korail": {"at": old.isoformat(), "reason": "옛날 차단"}}),
            encoding="utf-8",
        )
        self.assertIsNone(self.log.active(Provider.KORAIL), "쿨다운이 지나면 다시 시도 가능")
        self.assertIsNotNone(self.log.get(Provider.KORAIL), "기록 자체는 남는다")

    def test_clear(self):
        self.log.record(Provider.KORAIL, "MACRO ERROR")
        self.assertTrue(self.log.clear(Provider.KORAIL))
        self.assertFalse(self.log.clear(Provider.KORAIL))
        self.assertIsNone(self.log.active(Provider.KORAIL))

    def test_corrupt_file_is_not_a_block(self):
        self.log.path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(self.log.active(Provider.KORAIL))

    def test_describe_mentions_force_and_reason(self):
        text = BlockRecord(Provider.KORAIL, datetime.now(), "MACRO ERROR").describe()
        self.assertIn("MACRO ERROR", text)
        self.assertIn("--force", text)


class TestManagerRefusesBlockedProvider(unittest.TestCase):
    def test_add_raises_when_blocked(self):
        from datetime import date

        from railcatch.config import Credentials, Settings
        from railcatch.errors import ConfigError
        from railcatch.manager import WatchManager
        from railcatch.models import TimeWindow
        from railcatch.watcher import WatchSpec
        from tests.fakes import RecordingNotifier

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings.load(Path("/nonexistent"))
            settings.data_dir = Path(tmp)
            settings.korail = Credentials("id", "pw")
            manager = WatchManager(settings, notifier=RecordingNotifier())
            manager.blocks.record(Provider.KORAIL, "MACRO ERROR")

            spec = WatchSpec(
                provider=Provider.KORAIL, dep="서울", arr="부산",
                day=date.today(), window=TimeWindow(),
            )
            with self.assertRaises(ConfigError) as ctx:
                manager.add(spec)
            self.assertIn("차단", str(ctx.exception))
            manager.shutdown()


if __name__ == "__main__":
    unittest.main()
