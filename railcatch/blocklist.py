"""차단 기록.

코레일이 자동화 요청을 차단(`MACRO ERROR`)하면 그 사실을 파일에 남기고,
이후에는 네트워크 요청을 보내기 전에 먼저 거절한다.

이유: 차단은 재시도로 풀리지 않는다. 계속 두드려봐야 실패 로그인만 쌓이고,
그게 쌓이면 클라이언트 차단(계정은 멀쩡한 상태)이 계정 플래그로 번질 수 있다.
도구가 사용자를 그 방향으로 끌고 가서는 안 된다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .models import Provider

#: 이 기간이 지나면 한 번 더 시도해 볼 수 있게 한다. 사업자가 정책을
#: 되돌리는 경우가 있으므로 영구 차단으로 두지는 않는다.
COOLDOWN = timedelta(hours=24)


@dataclass(frozen=True)
class BlockRecord:
    provider: Provider
    at: datetime
    reason: str

    @property
    def expires_at(self) -> datetime:
        return self.at + COOLDOWN

    @property
    def expired(self) -> bool:
        return datetime.now() >= self.expires_at

    def describe(self) -> str:
        remaining = self.expires_at - datetime.now()
        hours = max(0, int(remaining.total_seconds() // 3600))
        return (
            f"{self.at:%m/%d %H:%M} 에 차단되었습니다: {self.reason}\n"
            f"약 {hours}시간 뒤에 자동으로 다시 시도할 수 있습니다.\n"
            f"지금 바로 시도하려면 --force 를 붙이세요 "
            f"(실패 로그인이 쌓이면 계정에 좋지 않습니다)."
        )


class BlockLog:
    """사업자별 차단 기록을 JSON 파일 하나에 보관한다."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def get(self, provider: Provider) -> BlockRecord | None:
        raw = self._load().get(provider.value)
        if not isinstance(raw, dict):
            return None
        try:
            return BlockRecord(
                provider=provider,
                at=datetime.fromisoformat(raw["at"]),
                reason=str(raw.get("reason", "")),
            )
        except (KeyError, ValueError):
            return None

    def active(self, provider: Provider) -> BlockRecord | None:
        """아직 유효한 차단 기록. 만료됐으면 None."""
        record = self.get(provider)
        return None if record is None or record.expired else record

    def record(self, provider: Provider, reason: str) -> BlockRecord:
        record = BlockRecord(provider=provider, at=datetime.now(), reason=reason)
        data = self._load()
        data[provider.value] = {"at": record.at.isoformat(), "reason": reason}
        self._save(data)
        return record

    def clear(self, provider: Provider) -> bool:
        data = self._load()
        if data.pop(provider.value, None) is None:
            return False
        self._save(data)
        return True
