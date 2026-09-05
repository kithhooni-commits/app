"""설정 로딩.

우선순위: 환경변수 > .env 파일 > 기본값.
자격증명은 절대 저장소에 커밋되지 않도록 .env 만 쓰고, .gitignore에 넣어둔다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError
from .models import Provider
from .transport import MIN_INTERVAL_SEC

ENV_FILE = ".env"


def load_env_file(path: Path) -> dict[str, str]:
    """아주 단순한 KEY=VALUE 파서. 따옴표와 # 주석을 처리한다."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key.strip()] = value
    return out


@dataclass
class Credentials:
    user_id: str = ""
    password: str = ""

    @property
    def present(self) -> bool:
        return bool(self.user_id and self.password)


@dataclass
class Settings:
    srt: Credentials = field(default_factory=Credentials)
    korail: Credentials = field(default_factory=Credentials)
    telegram_token: str = ""
    telegram_chat_id: str = ""
    poll_interval: float = 3.0
    data_dir: Path = field(default_factory=lambda: Path("data"))
    web_host: str = "127.0.0.1"
    web_port: int = 8777

    def credentials(self, provider: Provider) -> Credentials:
        return self.srt if provider is Provider.SRT else self.korail

    def require_credentials(self, provider: Provider) -> Credentials:
        creds = self.credentials(provider)
        if not creds.present:
            prefix = provider.value.upper()
            raise ConfigError(
                f"{prefix} 계정이 설정되지 않았습니다. .env 에 "
                f"{prefix}_ID / {prefix}_PW 를 넣어주세요."
            )
        return creds

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)

    @classmethod
    def load(cls, root: Path | None = None) -> "Settings":
        root = root or Path.cwd()
        env = {**load_env_file(root / ENV_FILE), **os.environ}

        def get(key: str, default: str = "") -> str:
            return str(env.get(key, default)).strip()

        interval = _positive_float(get("POLL_INTERVAL", "3"), "POLL_INTERVAL")
        if interval < MIN_INTERVAL_SEC:
            # 하한을 조용히 올린다. 더 빠르게 돌리면 차단당하고, 차단당하면 못 잡는다.
            interval = MIN_INTERVAL_SEC

        data_dir = Path(get("DATA_DIR", "data"))
        if not data_dir.is_absolute():
            data_dir = root / data_dir

        return cls(
            srt=Credentials(get("SRT_ID"), get("SRT_PW")),
            korail=Credentials(get("KORAIL_ID"), get("KORAIL_PW")),
            telegram_token=get("TELEGRAM_TOKEN"),
            telegram_chat_id=get("TELEGRAM_CHAT_ID"),
            poll_interval=interval,
            data_dir=data_dir,
            web_host=get("WEB_HOST", "127.0.0.1"),
            web_port=int(_positive_float(get("WEB_PORT", "8777"), "WEB_PORT")),
        )


def _positive_float(raw: str, name: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} 값이 숫자가 아닙니다: {raw!r}") from exc
    if value <= 0:
        raise ConfigError(f"{name} 은 0보다 커야 합니다: {raw!r}")
    return value
