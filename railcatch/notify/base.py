"""알림 채널 인터페이스."""

from __future__ import annotations

import abc
import logging

log = logging.getLogger(__name__)


class Notifier(abc.ABC):
    name: str

    @abc.abstractmethod
    def send(self, title: str, body: str) -> None: ...

    def safe_send(self, title: str, body: str) -> None:
        """알림 실패가 감시를 멈추게 해서는 안 된다."""
        try:
            self.send(title, body)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s 알림 전송 실패: %s", self.name, exc)


class MultiNotifier(Notifier):
    name = "multi"

    def __init__(self, channels: list[Notifier]) -> None:
        self.channels = channels

    def send(self, title: str, body: str) -> None:
        for ch in self.channels:
            ch.safe_send(title, body)
