"""예약대기 관리.

코레일 예약대기는 앱에서 한 번 걸어두면 취소표가 났을 때 순번대로 자동
배정된다. 즉 자동화가 필요한 반복 작업이 아니다. 실제로 사람이 놓치는 지점은
따로 있다.

  1. 후보 열차에 예약대기를 거는 것 자체를 빠뜨린다
  2. 배정됐는데 모르고 있다가 결제 기한을 넘긴다
  3. 대안 열차를 챙기지 않아 선택지가 하나뿐이다

이 모듈은 그 세 가지를 잡는다. 코레일 서버에 요청을 보내지 않으므로
매크로 탐지와 무관하고, 계정 위험도 없다.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from .errors import ConfigError
from .models import SeatClass
from .notify.base import Notifier


class Stage(str, Enum):
    """예약대기 항목의 진행 단계."""

    PLANNED = "planned"        # 걸어야 하는데 아직 안 걸었음
    REGISTERED = "registered"  # 예약대기 등록 완료, 배정 대기 중
    ASSIGNED = "assigned"      # 배정됨 — 결제 필요
    DONE = "done"              # 결제 완료 또는 포기

    @property
    def label(self) -> str:
        return {
            "planned": "미등록",
            "registered": "대기중",
            "assigned": "배정됨",
            "done": "완료",
        }[self.value]


class Reminder(str, Enum):
    """알림 종류. 항목당 종류별로 한 번씩만 보낸다."""

    REGISTER = "register"      # 아직 안 걸었으니 걸어라
    DAY_BEFORE = "day_before"  # 전날: 배정됐는지 확인해라
    DEPARTURE = "departure"    # 출발 임박: 마지막 확인
    PAYMENT = "payment"        # 배정됨: 결제 기한 확인


#: 출발 몇 시간 전에 "임박" 알림을 보낼지.
DEPARTURE_LEAD = timedelta(hours=4)

#: 배정된 항목을 몇 시간마다 다시 알릴지 (결제 기한을 놓치면 좌석이 날아간다).
PAYMENT_REPEAT = timedelta(hours=2)


@dataclass
class WaitlistEntry:
    """예약대기를 걸어둔(또는 걸어야 하는) 열차 하나."""

    train: str                     # "KTX 101"
    route: str                     # "서울→부산"
    depart_at: datetime
    seat_class: SeatClass = SeatClass.ANY
    stage: Stage = Stage.PLANNED
    note: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    created_at: datetime = field(default_factory=datetime.now)
    pay_deadline: datetime | None = None
    #: 이미 보낸 알림 종류 → 마지막 발송 시각
    reminded: dict[str, datetime] = field(default_factory=dict)

    @property
    def title(self) -> str:
        return (
            f"{self.train} {self.route} {self.depart_at:%m/%d %H:%M} "
            f"({self.seat_class.label})"
        )

    @property
    def departed(self) -> bool:
        return datetime.now() >= self.depart_at

    def due_reminders(self, now: datetime | None = None) -> list[Reminder]:
        """지금 보내야 할 알림 종류들."""
        now = now or datetime.now()
        if self.stage is Stage.DONE or self.departed:
            return []

        due: list[Reminder] = []
        if self.stage is Stage.ASSIGNED:
            # 배정된 건은 결제 기한이 있다. 반복해서 알린다.
            last = self.reminded.get(Reminder.PAYMENT.value)
            if last is None or now - last >= PAYMENT_REPEAT:
                due.append(Reminder.PAYMENT)
            return due

        if self.stage is Stage.PLANNED and Reminder.REGISTER.value not in self.reminded:
            due.append(Reminder.REGISTER)

        day_before = datetime.combine(self.depart_at.date() - timedelta(days=1), time(18, 0))
        if now >= day_before and Reminder.DAY_BEFORE.value not in self.reminded:
            due.append(Reminder.DAY_BEFORE)

        if now >= self.depart_at - DEPARTURE_LEAD and Reminder.DEPARTURE.value not in self.reminded:
            due.append(Reminder.DEPARTURE)

        return due

    def message_for(self, kind: Reminder) -> tuple[str, str]:
        """알림 제목과 본문."""
        remaining = self.depart_at - datetime.now()
        left = _humanize(remaining)
        if kind is Reminder.REGISTER:
            return (
                "📌 예약대기 등록하세요",
                f"{self.title}\n출발까지 {left}\n\n"
                f"코레일+ 앱에서 해당 열차의 {self.seat_class.label} 예약대기를 걸어두세요.\n"
                "먼저 걸수록 순번이 앞섭니다.",
            )
        if kind is Reminder.DAY_BEFORE:
            return (
                "🔔 내일 출발 — 배정 확인",
                f"{self.title}\n출발까지 {left}\n\n"
                "예약대기가 배정됐는지 앱에서 확인하세요.\n"
                "배정됐다면 결제 기한이 있습니다.",
            )
        if kind is Reminder.DEPARTURE:
            return (
                "⏰ 출발 임박 — 마지막 확인",
                f"{self.title}\n출발까지 {left}\n\n"
                "아직 배정 안 됐으면 입석·자유석이나 다른 열차를 알아보세요.",
            )
        deadline = (
            f"\n결제기한: {self.pay_deadline:%m/%d %H:%M}" if self.pay_deadline else ""
        )
        return (
            "💳 배정됨 — 결제 필요",
            f"{self.title}{deadline}\n\n기한을 넘기면 좌석이 취소됩니다.",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "train": self.train,
            "route": self.route,
            "depart_at": self.depart_at.isoformat(),
            "seat_class": self.seat_class.value,
            "stage": self.stage.value,
            "note": self.note,
            "created_at": self.created_at.isoformat(),
            "pay_deadline": self.pay_deadline.isoformat() if self.pay_deadline else None,
            "reminded": {k: v.isoformat() for k, v in self.reminded.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WaitlistEntry":
        return cls(
            id=str(data["id"]),
            train=str(data["train"]),
            route=str(data["route"]),
            depart_at=datetime.fromisoformat(data["depart_at"]),
            seat_class=SeatClass(data.get("seat_class", SeatClass.ANY.value)),
            stage=Stage(data.get("stage", "planned")),
            note=str(data.get("note", "")),
            created_at=datetime.fromisoformat(data["created_at"]),
            pay_deadline=(
                datetime.fromisoformat(data["pay_deadline"])
                if data.get("pay_deadline") else None
            ),
            reminded={
                k: datetime.fromisoformat(v)
                for k, v in (data.get("reminded") or {}).items()
            },
        )


class WaitlistStore:
    """예약대기 항목을 JSON 파일 하나에 보관한다.

    항목 수가 수십 개를 넘을 일이 없으므로 DB를 쓰지 않는다.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._entries: list[WaitlistEntry] | None = None

    def load(self) -> list[WaitlistEntry]:
        if self._entries is not None:
            return self._entries
        if not self.path.exists():
            self._entries = []
            return self._entries
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"예약대기 파일을 읽지 못했습니다 ({self.path}): {exc}") from exc

        entries = []
        for item in raw if isinstance(raw, list) else []:
            try:
                entries.append(WaitlistEntry.from_dict(item))
            except (KeyError, ValueError, TypeError):
                # 손상된 항목 하나 때문에 전체를 잃지 않는다.
                continue
        self._entries = entries
        return entries

    def save(self) -> None:
        entries = self.load()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps([e.to_dict() for e in entries], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.path)   # 쓰다 만 파일이 남지 않게

    def add(self, entry: WaitlistEntry) -> WaitlistEntry:
        self.load().append(entry)
        self.save()
        return entry

    def get(self, entry_id: str) -> WaitlistEntry | None:
        for entry in self.load():
            if entry.id == entry_id or entry.id.startswith(entry_id):
                return entry
        return None

    def remove(self, entry_id: str) -> bool:
        entry = self.get(entry_id)
        if entry is None:
            return False
        self.load().remove(entry)
        self.save()
        return True

    def set_stage(self, entry_id: str, stage: Stage, *, pay_deadline: datetime | None = None) -> WaitlistEntry:
        entry = self.get(entry_id)
        if entry is None:
            raise ConfigError(f"그런 항목이 없습니다: {entry_id}")
        entry.stage = stage
        if pay_deadline is not None:
            entry.pay_deadline = pay_deadline
        if stage is Stage.ASSIGNED:
            # 배정 알림은 새 단계이므로 다시 보내야 한다.
            entry.reminded.pop(Reminder.PAYMENT.value, None)
        self.save()
        return entry

    def purge_departed(self) -> int:
        """출발한 항목을 정리한다."""
        entries = self.load()
        keep = [e for e in entries if not e.departed]
        removed = len(entries) - len(keep)
        if removed:
            self._entries = keep
            self.save()
        return removed

    def active(self) -> list[WaitlistEntry]:
        return sorted(
            (e for e in self.load() if e.stage is not Stage.DONE and not e.departed),
            key=lambda e: e.depart_at,
        )


def send_due_reminders(
    store: WaitlistStore,
    notifier: Notifier,
    *,
    now: datetime | None = None,
) -> list[tuple[WaitlistEntry, Reminder]]:
    """보낼 때가 된 알림을 모두 보내고, 보낸 목록을 반환한다."""
    now = now or datetime.now()
    sent: list[tuple[WaitlistEntry, Reminder]] = []
    for entry in store.load():
        for kind in entry.due_reminders(now):
            title, body = entry.message_for(kind)
            notifier.safe_send(title, body)
            entry.reminded[kind.value] = now
            sent.append((entry, kind))
    if sent:
        store.save()
    return sent


class ReminderLoop:
    """예약대기 알림을 주기적으로 확인하는 백그라운드 스레드.

    웹 UI를 켜두면 알림도 같이 돌게 하기 위한 것이다. 코레일 서버에는
    아무 요청도 보내지 않으므로 감시 엔진과 달리 속도 제한이 필요 없다.
    """

    def __init__(self, store: "WaitlistStore", notifier: Notifier, interval: float = 300.0):
        import threading

        self.store = store
        self.notifier = notifier
        self.interval = max(interval, 30.0)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="waitlist-reminder", daemon=True)

    def start(self) -> "ReminderLoop":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        import logging

        log = logging.getLogger(__name__)
        while not self._stop.is_set():
            try:
                self.store.purge_departed()
                for entry, kind in send_due_reminders(self.store, self.notifier):
                    log.info("예약대기 알림: %s [%s]", entry.title, kind.value)
            except Exception:  # noqa: BLE001 - 스레드가 조용히 죽지 않게
                log.exception("예약대기 알림 확인 실패")
            self._stop.wait(self.interval)


def parse_departure(day: date, hhmm: str) -> datetime:
    """'0830' 또는 '08:30' 을 그 날짜의 datetime으로."""
    text = hhmm.strip().replace(":", "")
    if not text.isdigit() or len(text) not in (3, 4):
        raise ValueError(f"출발 시각 형식이 잘못되었습니다: {hhmm!r} (예: 08:30)")
    text = text.zfill(4)
    hour, minute = int(text[:2]), int(text[2:])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"출발 시각 범위를 벗어났습니다: {hhmm!r}")
    return datetime.combine(day, time(hour, minute))


def _humanize(delta: timedelta) -> str:
    total = int(delta.total_seconds())
    if total <= 0:
        return "출발함"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}일 {hours}시간"
    if hours:
        return f"{hours}시간 {minutes}분"
    return f"{minutes}분"


def summarize(entries: Iterable[WaitlistEntry]) -> str:
    entries = list(entries)
    if not entries:
        return "등록된 예약대기 항목이 없습니다."
    lines = []
    for e in entries:
        left = _humanize(e.depart_at - datetime.now())
        lines.append(f"{e.id}  [{e.stage.label:^4}]  {e.title}  (출발까지 {left})")
    return "\n".join(lines)
