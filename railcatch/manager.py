"""여러 감시를 함께 굴리는 관리자.

사업자별로 provider(=로그인 세션)를 하나만 두고 공유한다. 같은 계정으로
세션을 여러 개 만들면 서버가 이전 세션을 끊어버리는 경우가 있어서다.
공유 세션에는 RateLimiter가 붙어 있으므로 감시가 늘어도 총 요청 속도는
사업자당 상한을 넘지 않는다.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from .blocklist import BlockLog
from .config import Settings
from .errors import ConfigError
from .models import Provider
from .notify import ConsoleNotifier, MultiNotifier, Notifier, TelegramNotifier
from .providers import build_provider
from .providers.base import RailProvider
from .watcher import Watch, WatchSpec, WatchState, WatchStatus

log = logging.getLogger(__name__)


class WatchManager:
    def __init__(self, settings: Settings, notifier: Notifier | None = None) -> None:
        self.settings = settings
        self.notifier = notifier or build_notifier(settings)
        self._providers: dict[Provider, RailProvider] = {}
        self._watches: dict[str, Watch] = {}
        self._lock = threading.Lock()
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.blocks = BlockLog(settings.data_dir / "blocked.json")

    # ── provider 공유 ───────────────────────────────────────
    def provider_for(self, provider: Provider) -> RailProvider:
        with self._lock:
            existing = self._providers.get(provider)
            if existing is not None:
                return existing
            instance = build_provider(
                provider,
                interval=self.settings.poll_interval,
                data_dir=self.settings.data_dir,
                version=self.settings.korail_version or None,
            )
            self._providers[provider] = instance
            return instance

    # ── 감시 ────────────────────────────────────────────────
    def add(self, spec: WatchSpec) -> WatchStatus:
        # 차단된 사업자에는 감시를 걸지 않는다. 실패 로그인만 쌓인다.
        record = self.blocks.active(spec.provider)
        if record is not None:
            raise ConfigError(
                f"{spec.provider.value} 는 현재 차단 상태입니다.\n{record.describe()}"
            )
        creds = self.settings.require_credentials(spec.provider)
        if spec.auto_reserve and not creds.present:
            raise ConfigError("자동 선점을 하려면 계정 정보가 필요합니다.")
        watch = Watch(
            spec,
            self.provider_for(spec.provider),
            self.notifier,
            (creds.user_id, creds.password),
        )
        with self._lock:
            self._watches[watch.status.id] = watch
        watch.start()
        log.info("감시 추가: %s (%s)", spec.title, watch.status.id)
        return watch.status

    def stop(self, watch_id: str) -> bool:
        with self._lock:
            watch = self._watches.get(watch_id)
        if watch is None:
            return False
        watch.stop()
        return True

    def remove(self, watch_id: str) -> bool:
        with self._lock:
            watch = self._watches.pop(watch_id, None)
        if watch is None:
            return False
        watch.stop("삭제됨")
        return True

    def get(self, watch_id: str) -> Watch | None:
        with self._lock:
            return self._watches.get(watch_id)

    def snapshot(self) -> list[dict]:
        return [w.status.to_dict() for w in self._sorted_watches()]

    def _sorted_watches(self) -> list[Watch]:
        with self._lock:
            watches = list(self._watches.values())
        return sorted(watches, key=lambda w: w.status.started_at, reverse=True)

    def clear_finished(self) -> int:
        with self._lock:
            done = [
                wid for wid, w in self._watches.items()
                if w.status.state != WatchState.RUNNING
            ]
            for wid in done:
                self._watches.pop(wid, None)
        return len(done)

    def shutdown(self) -> None:
        for watch in self._sorted_watches():
            watch.stop("종료")
        for watch in self._sorted_watches():
            watch.join(timeout=3.0)
        with self._lock:
            providers = list(self._providers.values())
            self._providers.clear()
        for provider in providers:
            provider.close()


def build_notifier(settings: Settings) -> Notifier:
    channels: list[Notifier] = [ConsoleNotifier()]
    if settings.telegram_enabled:
        try:
            channels.append(
                TelegramNotifier(settings.telegram_token, settings.telegram_chat_id)
            )
        except ConfigError as exc:
            log.warning("텔레그램 알림 비활성화: %s", exc)
    else:
        log.info("텔레그램 미설정 — 콘솔 알림만 사용합니다.")
    return MultiNotifier(channels)
