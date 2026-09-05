import json
import unittest
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from railcatch.config import Credentials, Settings
from railcatch.manager import WatchManager
from railcatch.models import Provider
from railcatch.waitlist import WaitlistStore
from railcatch.web.server import _entry_from_payload, _spec_from_payload, serve
from tests.fakes import RecordingNotifier


class TestSpecFromPayload(unittest.TestCase):
    def payload(self, **kw):
        base = {
            "provider": "srt", "dep": "수서", "arr": "부산",
            "day": date.today().isoformat(), "window": "08:00-12:30",
        }
        base.update(kw)
        return base

    def test_parses_full_payload(self):
        spec = _spec_from_payload(self.payload(passengers=2, train_numbers="301, 0305"))
        self.assertEqual(spec.passengers, 2)
        self.assertEqual(spec.train_numbers, ("301", "305"))
        self.assertTrue(spec.auto_reserve)

    def test_train_numbers_accepts_list(self):
        spec = _spec_from_payload(self.payload(train_numbers=["301", "305"]))
        self.assertEqual(spec.train_numbers, ("301", "305"))

    def test_rejects_missing_fields(self):
        with self.assertRaises(ValueError):
            _spec_from_payload({"provider": "srt"})

    def test_rejects_unknown_provider(self):
        with self.assertRaises(ValueError):
            _spec_from_payload(self.payload(provider="korail-plus"))

    def test_rejects_past_date(self):
        with self.assertRaises(ValueError):
            _spec_from_payload(self.payload(day=(date.today() - timedelta(days=1)).isoformat()))

    def test_rejects_bad_window(self):
        with self.assertRaises(ValueError):
            _spec_from_payload(self.payload(window="아침에"))

    def test_rejects_too_many_passengers(self):
        with self.assertRaises(ValueError):
            _spec_from_payload(self.payload(passengers=99))


class TestHttpApi(unittest.TestCase):
    """실제 소켓을 띄워 라우팅과 오류 응답을 확인한다."""

    @classmethod
    def setUpClass(cls):
        settings = Settings.load(Path("/nonexistent"))
        settings.srt = Credentials("tester", "secret")
        settings.data_dir = Path("/tmp/railcatch-test-data")
        cls.manager = WatchManager(settings, notifier=RecordingNotifier())
        cls.store = WaitlistStore(settings.data_dir / "waitlist-test.json")
        cls.httpd = serve(cls.manager, cls.store, "127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.manager.shutdown()
        cls.store.path.unlink(missing_ok=True)

    def get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
            return r.status, json.loads(r.read())

    def post(self, path, body):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_index_served(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=5) as r:
            self.assertEqual(r.status, 200)
            self.assertIn(b"railcatch", r.read())

    def test_meta_reports_configured_providers(self):
        status, data = self.get("/api/meta")
        self.assertEqual(status, 200)
        by_value = {p["value"]: p for p in data["providers"]}
        self.assertTrue(by_value[Provider.SRT.value]["configured"])
        self.assertFalse(by_value[Provider.KORAIL.value]["configured"])
        self.assertGreaterEqual(data["poll_interval"], 2.0)

    def test_watches_starts_empty(self):
        status, data = self.get("/api/watches")
        self.assertEqual(status, 200)
        self.assertEqual(data["watches"], [])

    def test_bad_payload_returns_400_with_reason(self):
        status, data = self.post("/api/watches", {"provider": "srt"})
        self.assertEqual(status, 400)
        self.assertIn("필수 항목", data["error"])

    def test_missing_credentials_returns_400(self):
        status, data = self.post("/api/watches", {
            "provider": "korail", "dep": "서울", "arr": "부산",
            "day": date.today().isoformat(),
        })
        self.assertEqual(status, 400)
        self.assertIn("KORAIL_ID", data["error"])

    def test_stop_unknown_watch_returns_404(self):
        status, _ = self.post("/api/watches/stop", {"id": "nope"})
        self.assertEqual(status, 404)

    def test_unknown_route_returns_404(self):
        status, _ = self.post("/api/nope", {})
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()


class TestEntryFromPayload(unittest.TestCase):
    def payload(self, **kw):
        base = {
            "dep": "서울", "arr": "부산",
            "day": (date.today() + timedelta(days=7)).isoformat(),
            "time": "08:30", "train_type": "KTX", "train_number": "101",
        }
        base.update(kw)
        return base

    def test_builds_entry_from_dropdowns(self):
        e = _entry_from_payload(self.payload(seat_class="general", note="메모"))
        self.assertEqual(e.train, "KTX 101")
        self.assertEqual(e.route, "서울→부산")
        self.assertEqual(e.depart_at.strftime("%H:%M"), "08:30")
        self.assertEqual(e.seat_class.value, "general")
        self.assertEqual(e.note, "메모")
        self.assertEqual(e.stage.value, "planned")

    def test_train_number_is_optional(self):
        e = _entry_from_payload(self.payload(train_number=""))
        self.assertEqual(e.train, "KTX")

    def test_seat_class_defaults_to_any(self):
        self.assertEqual(_entry_from_payload(self.payload()).seat_class.value, "any")

    def test_rejects_same_station(self):
        with self.assertRaises(ValueError):
            _entry_from_payload(self.payload(arr="서울"))

    def test_rejects_missing_fields(self):
        with self.assertRaises(ValueError) as ctx:
            _entry_from_payload({"dep": "서울"})
        self.assertIn("arr", str(ctx.exception))

    def test_rejects_past_departure(self):
        with self.assertRaises(ValueError):
            _entry_from_payload(self.payload(day="2020-01-01"))

    def test_rejects_bad_time(self):
        with self.assertRaises(ValueError):
            _entry_from_payload(self.payload(time="아침"))

    def test_rejects_unknown_seat_class(self):
        with self.assertRaises(ValueError):
            _entry_from_payload(self.payload(seat_class="입석"))


class TestWaitlistApi(TestHttpApi):
    """예약대기 엔드포인트. TestHttpApi의 서버/헬퍼를 그대로 쓴다."""

    def setUp(self):
        for e in list(self.store.load()):
            self.store.remove(e.id)

    def add(self, **kw):
        body = {
            "dep": "서울", "arr": "부산",
            "day": (date.today() + timedelta(days=7)).isoformat(),
            "time": "08:30", "train_type": "KTX", "train_number": "101",
        }
        body.update(kw)
        return self.post("/api/waitlist", body)

    def test_meta_exposes_dropdown_choices(self):
        _, data = self.get("/api/meta")
        self.assertIn("서울", data["stations"])
        self.assertIn("부산", data["stations"])
        self.assertIn("KTX", data["train_types"])
        self.assertEqual(
            {s["value"] for s in data["seat_classes"]}, {"general", "special", "any"}
        )

    def test_add_then_list(self):
        status, data = self.add(seat_class="special")
        self.assertEqual(status, 201)
        self.assertEqual(data["entry"]["seat_class"], "special")

        _, listed = self.get("/api/waitlist")
        self.assertEqual(len(listed["entries"]), 1)
        self.assertEqual(listed["entries"][0]["train"], "KTX 101")

    def test_add_rejects_bad_input_with_reason(self):
        status, data = self.add(arr="서울")
        self.assertEqual(status, 400)
        self.assertIn("같습니다", data["error"])

    def test_stage_transitions(self):
        _, created = self.add()
        entry_id = created["entry"]["id"]

        _, data = self.post("/api/waitlist/stage", {"id": entry_id, "stage": "registered"})
        self.assertEqual(data["entry"]["stage"], "registered")

        _, data = self.post(
            "/api/waitlist/stage",
            {"id": entry_id, "stage": "assigned", "deadline": "18:40"},
        )
        self.assertEqual(data["entry"]["stage"], "assigned")
        self.assertTrue(data["entry"]["pay_deadline"].endswith("18:40:00"))

    def test_stage_unknown_id_returns_400(self):
        status, _ = self.post("/api/waitlist/stage", {"id": "nope", "stage": "done"})
        self.assertEqual(status, 400)

    def test_remove(self):
        _, created = self.add()
        status, data = self.post("/api/waitlist/remove", {"id": created["entry"]["id"]})
        self.assertEqual((status, data["ok"]), (200, True))
        self.assertEqual(self.get("/api/waitlist")[1]["entries"], [])

    def test_remove_unknown_returns_404(self):
        status, _ = self.post("/api/waitlist/remove", {"id": "nope"})
        self.assertEqual(status, 404)
