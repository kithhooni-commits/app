"""사업자 중립 도메인 모델.

SRT와 코레일은 응답 스키마가 전혀 다르므로, 각 provider가 자기 응답을
여기 정의된 타입으로 정규화한다. 감시 엔진과 웹 UI는 이 타입만 안다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, time
from enum import Enum
from typing import Any


class Provider(str, Enum):
    SRT = "srt"
    KORAIL = "korail"


class SeatClass(str, Enum):
    """감시 대상 좌석 등급."""

    GENERAL = "general"      # 일반실
    SPECIAL = "special"      # 특실 / 우등실
    ANY = "any"              # 아무거나

    @property
    def label(self) -> str:
        return {"general": "일반실", "special": "특실", "any": "일반실+특실"}[self.value]


class Availability(str, Enum):
    AVAILABLE = "available"          # 즉시 예약 가능
    WAITLIST = "waitlist"            # 예약대기 가능
    SOLD_OUT = "sold_out"
    UNKNOWN = "unknown"

    @property
    def is_catchable(self) -> bool:
        return self is Availability.AVAILABLE


@dataclass(frozen=True)
class Station:
    """역. code는 사업자별로 다르므로 provider와 함께 의미를 가진다."""

    name: str
    code: str
    provider: Provider


@dataclass(frozen=True)
class Train:
    """검색 결과 한 편성.

    Attributes:
        raw: provider 원본 응답. 예약 요청 시 그대로 되돌려줘야 하는
            필드들(차수, 열차그룹코드 등)이 사업자마다 달라서 통째로 보관한다.
    """

    provider: Provider
    train_name: str          # "SRT", "KTX", "KTX-산천", "ITX-새마을" ...
    train_number: str        # "301"
    dep_station: str
    arr_station: str
    dep_at: datetime
    arr_at: datetime
    general: Availability = Availability.UNKNOWN
    special: Availability = Availability.UNKNOWN
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def key(self) -> str:
        """같은 열차인지 식별하는 안정적인 키 (중복 알림 억제용)."""
        return f"{self.provider.value}:{self.train_number}:{self.dep_at.isoformat()}"

    @property
    def duration_min(self) -> int:
        return int((self.arr_at - self.dep_at).total_seconds() // 60)

    def availability_for(self, seat_class: SeatClass) -> Availability:
        if seat_class is SeatClass.GENERAL:
            return self.general
        if seat_class is SeatClass.SPECIAL:
            return self.special
        # ANY: 하나라도 잡히면 잡힌 것
        for av in (self.general, self.special):
            if av.is_catchable:
                return av
        for av in (self.general, self.special):
            if av is Availability.WAITLIST:
                return av
        return Availability.SOLD_OUT

    def catchable_classes(self, seat_class: SeatClass) -> list[SeatClass]:
        """실제로 예약 시도할 좌석 등급을, 시도할 순서대로."""
        if seat_class is SeatClass.GENERAL:
            return [SeatClass.GENERAL] if self.general.is_catchable else []
        if seat_class is SeatClass.SPECIAL:
            return [SeatClass.SPECIAL] if self.special.is_catchable else []
        out = []
        if self.general.is_catchable:
            out.append(SeatClass.GENERAL)
        if self.special.is_catchable:
            out.append(SeatClass.SPECIAL)
        return out

    def summary(self) -> str:
        return (
            f"{self.train_name} {self.train_number} "
            f"{self.dep_station}({self.dep_at:%H:%M}) → "
            f"{self.arr_station}({self.arr_at:%H:%M})"
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("raw", None)
        d["provider"] = self.provider.value
        d["dep_at"] = self.dep_at.isoformat()
        d["arr_at"] = self.arr_at.isoformat()
        d["general"] = self.general.value
        d["special"] = self.special.value
        d["duration_min"] = self.duration_min
        d["summary"] = self.summary()
        return d


@dataclass(frozen=True)
class Reservation:
    """선점 성공한 예약. 결제는 사용자가 앱/웹에서 직접 한다."""

    provider: Provider
    reservation_number: str
    train: Train
    seat_class: SeatClass
    total_price: int | None = None
    pay_deadline: datetime | None = None
    seats: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider.value,
            "reservation_number": self.reservation_number,
            "train": self.train.to_dict(),
            "seat_class": self.seat_class.value,
            "total_price": self.total_price,
            "pay_deadline": self.pay_deadline.isoformat() if self.pay_deadline else None,
            "seats": list(self.seats),
        }


@dataclass(frozen=True)
class TimeWindow:
    """출발 시각 허용 구간. end가 start보다 이르면 자정을 넘긴 것으로 본다."""

    start: time = time(0, 0)
    end: time = time(23, 59)

    def contains(self, t: time) -> bool:
        if self.start <= self.end:
            return self.start <= t <= self.end
        return t >= self.start or t <= self.end

    @classmethod
    def parse(cls, text: str) -> TimeWindow:
        """'08:00-12:30' 또는 '08:00~12:30' 형식을 파싱."""
        parts = re.split(r"[-~]", text.strip())
        if len(parts) != 2:
            raise ValueError(f"시간 구간 형식이 잘못되었습니다: {text!r} (예: 08:00-12:30)")
        return cls(_parse_time(parts[0]), _parse_time(parts[1]))

    def __str__(self) -> str:
        return f"{self.start:%H:%M}-{self.end:%H:%M}"


def _parse_time(text: str) -> time:
    text = text.strip()
    m = re.fullmatch(r"(\d{1,2}):?(\d{2})?", text)
    if not m:
        raise ValueError(f"시각 형식이 잘못되었습니다: {text!r}")
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"시각 범위를 벗어났습니다: {text!r}")
    return time(hour, minute)


def parse_date(text: str) -> date:
    """'2026-09-20', '20260920', '9/20' 형식을 받는다."""
    text = text.strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    m = re.fullmatch(r"(\d{1,2})[/.](\d{1,2})", text)
    if m:
        today = date.today()
        month, day = int(m.group(1)), int(m.group(2))
        year = today.year + (1 if (month, day) < (today.month, today.day) else 0)
        return date(year, month, day)
    raise ValueError(f"날짜 형식이 잘못되었습니다: {text!r} (예: 2026-09-20)")
