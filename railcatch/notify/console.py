"""터미널 알림. 항상 켜져 있는 기본 채널."""

from __future__ import annotations

import sys
from datetime import datetime

from .base import Notifier

BELL = "\a"


class ConsoleNotifier(Notifier):
    name = "console"

    def __init__(self, bell: bool = True) -> None:
        self.bell = bell

    def send(self, title: str, body: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        prefix = BELL if self.bell else ""
        print(f"{prefix}\n[{stamp}] ★ {title}\n{body}\n", file=sys.stderr, flush=True)
