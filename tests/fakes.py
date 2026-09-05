"""테스트용 가짜 사업자와 알림 채널."""

from __future__ import annotations

import threading
from datetime import date, datetime, time, timedelta

from railcatch.errors import LoginError, SoldOutError
from railcatch.models import Availability, Provider, Reservation, SeatClass, Station, Train
from railcatch.notify.base import Notifier
from railcatch.providers.base import RailProvider


def make_train(
    number: str = "301",
    *,
    hour: int = 10,
    general: Availability = Availability.SOLD_OUT,
    special: Availability = Availability.SOLD_OUT,
    day: date | None = None,
) -> Train:
    day = day or date.today()
    dep = datetime.combine(day, time(hour, 0))
    return Train(
        provider=Provider.SRT,
        train_name="SRT",
        train_number=number,
        dep_station="수서",
        arr_station="부산",
        dep_at=dep,
        arr_at=dep + timedelta(hours=2, minutes=40),
        general=general,
        special=special,
        raw={"trnNo": number},
    )


class FakeProvider(RailProvider):
    """스크립트대로 응답하는 사업자.

    schedule: 매 search() 호출마다 반환할 열차 목록의 리스트.
              소진되면 마지막 항목을 계속 반환한다.
    """

    provider = Provider.SRT
    display_name = "가짜"

    def __init__(
        self,
        schedule: list[list[Train]],
        *,
        reserve_fails: int = 0,
        login_error: bool = False,
    ) -> None:
        self.schedule = schedule
        self.reserve_fails = reserve_fails
        self.login_error = login_error
        self.searches = 0
        self.reserve_calls: list[tuple[Train, SeatClass]] = []
        self._logged_in = False
        self.logged_out = False
        self.searched = threading.Event()

    def login(self, user_id: str, password: str) -> None:
        if self.login_error:
            raise LoginError("아이디 또는 비밀번호를 확인하세요.")
        self._logged_in = True

    def logout(self) -> None:
        self.logged_out = True
        self._logged_in = False

    @property
    def logged_in(self) -> bool:
        return self._logged_in

    def stations(self) -> list[Station]:
        return [Station("수서", "0551", Provider.SRT), Station("부산", "0020", Provider.SRT)]

    def search(self, dep, arr, day, after, *, passengers=1, include_soldout=True):  # type: ignore[no-untyped-def]
        index = min(self.searches, len(self.schedule) - 1)
        self.searches += 1
        self.searched.set()
        return list(self.schedule[index])

    def reserve(self, train, seat_class, *, passengers=1):  # type: ignore[no-untyped-def]
        self.reserve_calls.append((train, seat_class))
        if self.reserve_fails > 0:
            self.reserve_fails -= 1
            raise SoldOutError()
        return Reservation(
            provider=Provider.SRT,
            reservation_number="ABC12345",
            train=train,
            seat_class=seat_class,
            total_price=59800,
            pay_deadline=datetime.now() + timedelta(minutes=10),
        )


class RecordingNotifier(Notifier):
    name = "recording"

    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []
        self.received = threading.Event()

    def send(self, title: str, body: str) -> None:
        self.messages.append((title, body))
        self.received.set()
