import json
import unittest
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from railcatch.config import Credentials, Settings
from railcatch.manager import WatchManager
from railcatch.models import Provider
from railcatch.web.server import _spec_from_payload, serve
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
        cls.httpd = serve(cls.manager, "127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.manager.shutdown()

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
