"""로컬 웹 UI.

stdlib http.server 로 충분하다 — 이 서버는 본인 PC에서 본인만 쓴다.
기본 바인딩이 127.0.0.1인 이유도 같다. 인증이 없으므로 절대 0.0.0.0으로
열지 말 것. (계정 정보로 예매를 실행하는 API다.)
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime, timedelta
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..errors import ConfigError, RailCatchError
from ..manager import WatchManager
from ..models import Provider, SeatClass, TimeWindow, parse_date
from ..watcher import WatchSpec

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


class Handler(BaseHTTPRequestHandler):
    server_version = "railcatch"
    protocol_version = "HTTP/1.1"

    def __init__(self, *args: Any, manager: WatchManager, **kwargs: Any) -> None:
        self.manager = manager
        super().__init__(*args, **kwargs)

    # ── 라우팅 ──────────────────────────────────────────────
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        if path == "/api/watches":
            return self._json({"watches": self.manager.snapshot()})
        if path == "/api/meta":
            return self._json(self._meta())
        self._error(404, "not found")

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            body = self._read_json()
        except ValueError as exc:
            return self._error(400, str(exc))

        try:
            if path == "/api/watches":
                spec = _spec_from_payload(body)
                status = self.manager.add(spec)
                return self._json({"watch": status.to_dict()}, status=201)
            if path == "/api/watches/stop":
                ok = self.manager.stop(str(body.get("id", "")))
                return self._json({"ok": ok}, status=200 if ok else 404)
            if path == "/api/watches/remove":
                ok = self.manager.remove(str(body.get("id", "")))
                return self._json({"ok": ok}, status=200 if ok else 404)
            if path == "/api/watches/clear":
                return self._json({"removed": self.manager.clear_finished()})
        except (ConfigError, ValueError) as exc:
            return self._error(400, str(exc))
        except RailCatchError as exc:
            return self._error(502, str(exc))
        self._error(404, "not found")

    # ── 응답 헬퍼 ───────────────────────────────────────────
    def _meta(self) -> dict[str, Any]:
        settings = self.manager.settings
        return {
            # 코레일을 먼저 둔다 — 통합 이후 SRT 예매 경로는 막힌 상태다.
            "providers": [
                {
                    "value": p.value,
                    "label": "코레일 (KTX/SRT 통합)" if p is Provider.KORAIL else "SRT (레거시)",
                    "configured": settings.credentials(p).present,
                }
                for p in (Provider.KORAIL, Provider.SRT)
            ],
            "seat_classes": [{"value": s.value, "label": s.label} for s in SeatClass],
            "poll_interval": settings.poll_interval,
            "telegram": settings.telegram_enabled,
            "today": date.today().isoformat(),
            "max_day": (date.today() + timedelta(days=30)).isoformat(),
        }

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > 64 * 1024:
            raise ValueError("요청 본문이 너무 큽니다.")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"JSON 파싱 실패: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("JSON 객체를 기대했습니다.")
        return data

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._respond(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status=status)

    def _send_file(self, path: Path, content_type: str) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            return self._error(404, "not found")
        self._respond(200, body, content_type)

    def _respond(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)


def _spec_from_payload(body: dict[str, Any]) -> WatchSpec:
    """웹 폼 입력을 WatchSpec으로. 잘못된 입력은 ValueError로 400을 만든다."""
    required = ("provider", "dep", "arr", "day")
    missing = [k for k in required if not str(body.get(k, "")).strip()]
    if missing:
        raise ValueError(f"필수 항목이 비었습니다: {', '.join(missing)}")

    try:
        provider = Provider(str(body["provider"]))
    except ValueError as exc:
        raise ValueError(f"알 수 없는 사업자: {body['provider']!r}") from exc
    try:
        seat_class = SeatClass(str(body.get("seat_class", "any")))
    except ValueError as exc:
        raise ValueError(f"알 수 없는 좌석 등급: {body.get('seat_class')!r}") from exc

    day = parse_date(str(body["day"]))
    if day < date.today():
        raise ValueError("지난 날짜는 감시할 수 없습니다.")

    window = TimeWindow.parse(str(body.get("window") or "00:00-23:59"))

    numbers = body.get("train_numbers") or []
    if isinstance(numbers, str):
        numbers = [n for n in numbers.replace(",", " ").split() if n]
    train_numbers = tuple(str(n).strip().lstrip("0") or "0" for n in numbers)

    expires_at = None
    if body.get("expires_at"):
        expires_at = datetime.fromisoformat(str(body["expires_at"]))

    return WatchSpec(
        provider=provider,
        dep=str(body["dep"]).strip(),
        arr=str(body["arr"]).strip(),
        day=day,
        window=window,
        seat_class=seat_class,
        passengers=int(body.get("passengers", 1)),
        auto_reserve=bool(body.get("auto_reserve", True)),
        train_numbers=train_numbers,
        expires_at=expires_at,
        label=str(body.get("label", "")).strip(),
    )


def serve(manager: WatchManager, host: str, port: int) -> ThreadingHTTPServer:
    if host not in ("127.0.0.1", "localhost", "::1"):
        log.warning(
            "⚠ %s 로 바인딩합니다. 이 서버에는 인증이 없고 계정으로 예매를 실행합니다. "
            "신뢰할 수 없는 네트워크에 노출하지 마세요.",
            host,
        )
    httpd = ThreadingHTTPServer((host, port), partial(Handler, manager=manager))
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, name="web", daemon=True)
    thread.start()
    return httpd
