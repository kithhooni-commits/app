"""감시 엔진.

WatchSpec 하나 = 백그라운드 스레드 하나. 스레드는 조회 → 필터 → (조건 충족 시)
선점 → 알림 순서로 돌고, 성공하거나 중단될 때까지 계속한다.

설계상 중요한 점 두 가지:
1. 선점은 성공 즉시 감시를 끝낸다. 중복 예약은 사용자 손해다.
2. BlockedError는 즉시 감시를 끝낸다. 차단당한 뒤 계속 두드리면 계정이 위험하다.
"""

from __future__ import annotations

import logging
import threading
import time as _time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any, Callable

from .errors import BlockedError, LoginError, RailCatchError, ResponseError, SoldOutError
from .models import Provider, Reservation, SeatClass, TimeWindow, Train
from .notify.base import Notifier
from .providers.base import RailProvider

log = logging.getLogger(__name__)

#: 연속 오류가 이만큼 쌓이면 감시를 포기한다.
MAX_CONSECUTIVE_ERRORS = 12

#: 매진 상태에서 다시 로그인 세션을 확인하는 주기 (초).
SESSION_REFRESH_SEC = 25 * 60

#: 한 번의 조회 주기가 최소한 이만큼은 걸리게 한다 (바쁜 대기 방지).
MIN_TICK_SEC = 0.05


@dataclass
class WatchSpec:
    """무엇을 어떤 조건으로 감시할지."""

    provider: Provider
    dep: str
    arr: str
    day: date
    window: TimeWindow = field(default_factory=TimeWindow)
    seat_class: SeatClass = SeatClass.ANY
    passengers: int = 1
    auto_reserve: bool = True
    #: 지정하면 이 열차번호들만 본다. 비면 전체.
    train_numbers: tuple[str, ...] = ()
    #: 감시 만료 시각. 지나면 자동 종료.
    expires_at: datetime | None = None
    label: str = ""

    def __post_init__(self) -> None:
        if self.passengers < 1:
            raise ValueError("인원은 1명 이상이어야 합니다.")
        if self.passengers > 9:
            raise ValueError("한 번에 예약 가능한 인원은 9명까지입니다.")

    @property
    def title(self) -> str:
        if self.label:
            return self.label
        provider_name = "SRT" if self.provider is Provider.SRT else "코레일"
        return (
            f"[{provider_name}] {self.dep}→{self.arr} "
            f"{self.day:%m/%d} {self.window} {self.seat_class.label} {self.passengers}명"
        )

    def matches(self, train: Train) -> bool:
        if not self.window.contains(train.dep_at.time()):
            return False
        if self.train_numbers and train.train_number not in self.train_numbers:
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider.value,
            "dep": self.dep,
            "arr": self.arr,
            "day": self.day.isoformat(),
            "window": str(self.window),
            "seat_class": self.seat_class.value,
            "passengers": self.passengers,
            "auto_reserve": self.auto_reserve,
            "train_numbers": list(self.train_numbers),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "label": self.label,
        }


class WatchState(str):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass
class WatchStatus:
    id: str
    spec: WatchSpec
    state: str = WatchState.RUNNING
    attempts: int = 0
    found: int = 0                      # 조건에 맞는 열차를 발견한 횟수
    errors: int = 0
    last_message: str = "시작 대기 중"
    last_checked_at: datetime | None = None
    started_at: datetime = field(default_factory=datetime.now)
    finished_at: datetime | None = None
    reservation: Reservation | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "spec": self.spec.to_dict(),
            "title": self.spec.title,
            "state": self.state,
            "attempts": self.attempts,
            "found": self.found,
            "errors": self.errors,
            "last_message": self.last_message,
            "last_checked_at": self.last_checked_at.isoformat() if self.last_checked_at else None,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "reservation": self.reservation.to_dict() if self.reservation else None,
        }


class Watch:
    """WatchSpec 하나를 실행하는 감시 스레드."""

    def __init__(
        self,
        spec: WatchSpec,
        provider: RailProvider,
        notifier: Notifier,
        credentials: tuple[str, str],
        *,
        watch_id: str | None = None,
        on_change: Callable[[WatchStatus], None] | None = None,
    ) -> None:
        self.spec = spec
        self.provider = provider
        self.notifier = notifier
        self.credentials = credentials
        self.status = WatchStatus(id=watch_id or uuid.uuid4().hex[:8], spec=spec)
        self._on_change = on_change
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_login_at = 0.0

    # ── 수명주기 ────────────────────────────────────────────
    def start(self) -> "Watch":
        if self._thread is not None:
            raise RuntimeError("이미 시작된 감시입니다.")
        self._thread = threading.Thread(target=self._run, name=f"watch-{self.status.id}", daemon=True)
        self._thread.start()
        return self

    def stop(self, reason: str = "사용자가 중단했습니다") -> None:
        if self.status.state == WatchState.RUNNING:
            self._finish(WatchState.STOPPED, reason)
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    @property
    def running(self) -> bool:
        return self.status.state == WatchState.RUNNING and not self._stop.is_set()

    # ── 본체 ────────────────────────────────────────────────
    def _run(self) -> None:
        try:
            self._ensure_login()
        except (LoginError, RailCatchError) as exc:
            self._finish(WatchState.FAILED, f"로그인 실패: {exc}")
            self.notifier.safe_send("감시 시작 실패", f"{self.spec.title}\n{exc}")
            return

        consecutive_errors = 0
        self._note("감시 시작")

        while not self._stop.is_set():
            if self._expired():
                self._finish(WatchState.STOPPED, "감시 기한이 지나 종료했습니다.")
                return
            try:
                started = _time.monotonic()
                caught = self._tick()
                consecutive_errors = 0
                if caught:
                    return
                # 요청 간격은 provider의 RateLimiter가 책임진다. 여기서는 그것이
                # 어떤 이유로든 동작하지 않았을 때 바쁜 대기가 되지 않게만 막는다.
                elapsed = _time.monotonic() - started
                if elapsed < MIN_TICK_SEC:
                    self._sleep(MIN_TICK_SEC - elapsed)
            except BlockedError as exc:
                self._finish(WatchState.FAILED, str(exc))
                self.notifier.safe_send(
                    "⛔ 감시 중단 (요청 거부)",
                    f"{self.spec.title}\n{exc}\n\n한동안 조회를 멈추는 것을 권합니다.",
                )
                return
            except LoginError as exc:
                self._finish(WatchState.FAILED, f"로그인 실패: {exc}")
                self.notifier.safe_send("감시 중단", f"{self.spec.title}\n{exc}")
                return
            except RailCatchError as exc:
                consecutive_errors += 1
                self.status.errors += 1
                self._note(f"오류({consecutive_errors}/{MAX_CONSECUTIVE_ERRORS}): {exc}")
                log.warning("[%s] %s", self.status.id, exc)
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    self._finish(WatchState.FAILED, f"연속 오류로 중단: {exc}")
                    self.notifier.safe_send("감시 중단", f"{self.spec.title}\n{exc}")
                    return
                self._sleep(min(5.0 * consecutive_errors, 60.0))
            except Exception as exc:  # noqa: BLE001 - 스레드가 조용히 죽지 않게
                log.exception("[%s] 예기치 못한 오류", self.status.id)
                self._finish(WatchState.FAILED, f"예기치 못한 오류: {exc}")
                self.notifier.safe_send("감시 중단", f"{self.spec.title}\n{exc}")
                return

    def _tick(self) -> bool:
        """한 번 조회한다. 잡았으면 True."""
        self.status.attempts += 1
        self.status.last_checked_at = datetime.now()

        trains = self.provider.search(
            self.spec.dep,
            self.spec.arr,
            self.spec.day,
            self.spec.window.start,
            passengers=self.spec.passengers,
        )
        candidates = [t for t in trains if self.spec.matches(t)]
        if not candidates:
            self._note(f"조건에 맞는 열차 없음 (조회 {len(trains)}편)")
            return False

        open_trains = [t for t in candidates if t.catchable_classes(self.spec.seat_class)]
        if not open_trains:
            self._note(f"{len(candidates)}편 감시 중 · 빈자리 없음")
            return False

        self.status.found += 1
        if not self.spec.auto_reserve:
            self._finish(WatchState.SUCCEEDED, f"빈자리 발견: {open_trains[0].summary()}")
            self.notifier.safe_send(
                "🎫 빈자리 발견",
                self._describe_open(open_trains) + "\n\n앱에서 직접 예매하세요.",
            )
            return True

        return self._try_reserve(open_trains)

    def _try_reserve(self, open_trains: list[Train]) -> bool:
        """빈자리가 보이는 열차들을 순서대로 선점 시도."""
        sold_out_again = 0
        for train in open_trains:
            for seat_class in train.catchable_classes(self.spec.seat_class):
                if self._stop.is_set():
                    return False
                self._note(f"선점 시도: {train.summary()} [{seat_class.label}]")
                try:
                    reservation = self.provider.reserve(
                        train, seat_class, passengers=self.spec.passengers
                    )
                except SoldOutError:
                    sold_out_again += 1
                    log.info("[%s] 선점 직전 매진: %s", self.status.id, train.summary())
                    continue
                except ResponseError as exc:
                    if not exc.retryable:
                        raise
                    log.warning("[%s] 선점 실패(재시도 가능): %s", self.status.id, exc)
                    continue

                self.status.reservation = reservation
                self._finish(WatchState.SUCCEEDED, f"선점 성공! 예약번호 {reservation.reservation_number}")
                self.notifier.safe_send("✅ 좌석 선점 완료", _reservation_message(reservation))
                return True

        if sold_out_again:
            self._note(f"빈자리를 봤지만 {sold_out_again}건 모두 선점 직전 마감")
        return False

    # ── 보조 ────────────────────────────────────────────────
    def _ensure_login(self) -> None:
        if self.provider.logged_in and (_time.monotonic() - self._last_login_at) < SESSION_REFRESH_SEC:
            return
        user_id, password = self.credentials
        self.provider.login(user_id, password)
        self._last_login_at = _time.monotonic()

    def _expired(self) -> bool:
        if self.spec.expires_at and datetime.now() >= self.spec.expires_at:
            return True
        # 출발일이 지난 감시는 의미가 없다.
        return self.spec.day < date.today()

    def _describe_open(self, trains: list[Train]) -> str:
        lines = [self.spec.title, ""]
        for t in trains[:5]:
            avail = t.availability_for(self.spec.seat_class)
            lines.append(f"· {t.summary()} [{avail.value}]")
        if len(trains) > 5:
            lines.append(f"… 외 {len(trains) - 5}편")
        return "\n".join(lines)

    def _note(self, message: str) -> None:
        self.status.last_message = message
        log.debug("[%s] %s", self.status.id, message)
        self._emit()

    def _finish(self, state: str, message: str) -> None:
        self.status.state = state
        self.status.last_message = message
        self.status.finished_at = datetime.now()
        self._stop.set()
        log.info("[%s] %s — %s", self.status.id, state, message)
        self._emit()

    def _emit(self) -> None:
        if self._on_change is not None:
            try:
                self._on_change(self.status)
            except Exception:  # noqa: BLE001
                log.debug("상태 콜백 실패", exc_info=True)

    def _sleep(self, seconds: float) -> None:
        self._stop.wait(seconds)


def _reservation_message(r: Reservation) -> str:
    lines = [
        r.train.summary(),
        f"좌석: {r.seat_class.label}",
        f"예약번호: {r.reservation_number}",
    ]
    if r.total_price is not None:
        lines.append(f"결제금액: {r.total_price:,}원")
    if r.pay_deadline:
        lines.append(f"결제기한: {r.pay_deadline:%m/%d %H:%M}")
    lines.append("")
    lines.append("⚠ 기한 내에 앱/홈페이지에서 결제해야 예약이 유지됩니다.")
    return "\n".join(lines)
