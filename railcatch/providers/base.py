"""사업자 어댑터 인터페이스.

감시 엔진은 오직 이 인터페이스만 보고 동작한다. 코레일+ 통합으로 특정
사업자의 API가 막히면, 새 어댑터를 여기 규약대로 하나 더 쓰면 된다.
"""

from __future__ import annotations

import abc
from datetime import date, time

from ..models import Provider, Reservation, SeatClass, Station, Train


class RailProvider(abc.ABC):
    """SRT / 코레일 등 예매 백엔드 하나에 대한 어댑터."""

    provider: Provider
    display_name: str

    @abc.abstractmethod
    def login(self, user_id: str, password: str) -> None:
        """세션을 만든다. 실패 시 LoginError."""

    @abc.abstractmethod
    def logout(self) -> None:
        """세션을 정리한다. 실패해도 예외를 던지지 않는다."""

    @property
    @abc.abstractmethod
    def logged_in(self) -> bool: ...

    @abc.abstractmethod
    def search(
        self,
        dep: str,
        arr: str,
        day: date,
        after: time,
        *,
        passengers: int = 1,
        include_soldout: bool = True,
    ) -> list[Train]:
        """`day` `after` 이후 출발 열차 목록. 매진 포함 여부는 인자로 제어."""

    @abc.abstractmethod
    def reserve(self, train: Train, seat_class: SeatClass, *, passengers: int = 1) -> Reservation:
        """좌석을 선점한다. 결제는 하지 않는다.

        좌석이 이미 나갔으면 SoldOutError를 던진다 (감시 계속 진행 신호).
        """

    @abc.abstractmethod
    def stations(self) -> list[Station]:
        """이 사업자가 취급하는 역 목록."""

    def resolve_station(self, name: str) -> Station:
        """역 이름을 Station으로. '서울역' 처럼 접미사가 붙어도 받아준다."""
        from ..errors import ConfigError

        wanted = name.strip()
        table = self.stations()
        by_name = {s.name: s for s in table}
        for candidate in (wanted, wanted.removesuffix("역")):
            if candidate in by_name:
                return by_name[candidate]
        matches = [s for s in table if wanted.removesuffix("역") in s.name]
        if len(matches) == 1:
            return matches[0]
        if matches:
            names = ", ".join(s.name for s in matches[:8])
            raise ConfigError(f"{self.display_name}: '{name}' 이(가) 여러 역과 일치합니다: {names}")
        raise ConfigError(f"{self.display_name}: '{name}' 역을 찾을 수 없습니다.")

    def close(self) -> None:
        try:
            self.logout()
        except Exception:  # noqa: BLE001 - 종료 경로에서 실패를 전파하지 않는다
            pass
